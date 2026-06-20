"""Threaded orchestration between the Lichess BOT API and the pygame GUI.

The GUI main loop must never block on network I/O (and pygame is not
thread-safe across threads). ``LichessController`` runs the Lichess event and
game streams on background daemon threads and pushes typed events onto a
``queue.Queue``. The GUI drains that queue each frame and updates the board.

The engine search also runs on the game stream thread (off the GUI thread) so
the UI stays responsive and Lichess clocks keep updating while the engine thinks.

Core logic (``_process_event_stream``, ``_process_game_stream``, ``_maybe_move``,
``_time_budget``) is synchronous and unit-tested directly; the daemon threads
are thin wrappers that feed the client's NDJSON iterators into that logic.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from src.board import Board, STARTING_FEN
from src.game import GameState
from src.moves import uci_to_move, move_to_uci, Move
from src.engine import choose_move
from src.lichess_client import LichessClient, LichessAPIError

logger = logging.getLogger(__name__)

# Lichess game statuses that mean the game is still in progress.
ACTIVE_STATUSES = ("created", "started")


@dataclass(frozen=True)
class ChallengeReceived:
    challenge_id: str
    opponent: str
    speed: str
    variant: str
    color: str  # "white" / "black" / "random"
    rated: bool


@dataclass(frozen=True)
class GameStarted:
    game_id: str
    bot_is_white: bool
    opponent_name: str
    initial_fen: str
    moves: tuple[str, ...]
    wtime: Optional[int]
    btime: Optional[int]


@dataclass(frozen=True)
class GameUpdated:
    game_id: str
    moves: tuple[str, ...]
    wtime: Optional[int]
    btime: Optional[int]
    status: str
    winner: Optional[str]


@dataclass(frozen=True)
class EngineMoved:
    game_id: str
    uci: str


@dataclass(frozen=True)
class GameFinished:
    game_id: str
    status: str
    winner: Optional[str]
    moves: tuple[str, ...]
    initial_fen: str


@dataclass(frozen=True)
class Status:
    message: str


@dataclass(frozen=True)
class Error:
    message: str


# Type alias for the engine move-selection function injected for testability.
ChooseMoveFn = Callable[[Board, Optional[int]], Optional[Move]]


class LichessController:
    """Bridges Lichess BOT API streams and the GUI via a thread-safe queue."""

    def __init__(
        self,
        token: str,
        client: Optional[LichessClient] = None,
        engine_choose: ChooseMoveFn = choose_move,
        default_movetime_ms: int = 1000,
    ) -> None:
        self.client = client if client is not None else LichessClient(token)
        self.engine_choose = engine_choose
        self.default_movetime_ms = default_movetime_ms
        self.event_queue: queue.Queue[object] = queue.Queue()
        self._stop = threading.Event()
        self._username: str = ""
        self._threads: list[threading.Thread] = []

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Fetch the bot profile, then start the event stream thread."""
        try:
            profile = self.client.get_profile()
            self._username = profile.get("username", "") or profile.get("id", "")
        except LichessAPIError as exc:
            self._push(Error(f"profile fetch failed: {exc}"))
            return
        self._push(Status(f"Connected as {self._username}"))
        thread = threading.Thread(target=self._event_thread, daemon=True,
                                  name="lichess-event-stream")
        self._threads.append(thread)
        thread.start()

    def stop(self) -> None:
        """Signal background threads to stop.

        Daemon threads will exit after the next keep-alive line (~7s) or when
        the process exits. Closing an HTTPResponse from another thread is
        unreliable on Windows, so we rely on the stop flag + daemon threads.
        """
        self._stop.set()

    @property
    def username(self) -> str:
        return self._username

    # --- public actions (called from the GUI thread) ------------------------

    def accept_challenge(self, challenge_id: str) -> None:
        try:
            self.client.accept_challenge(challenge_id)
        except LichessAPIError as exc:
            self._push(Error(f"accept failed: {exc}"))

    def decline_challenge(self, challenge_id: str, reason: str = "generic") -> None:
        try:
            self.client.decline_challenge(challenge_id, reason)
        except LichessAPIError as exc:
            self._push(Error(f"decline failed: {exc}"))

    def resign(self, game_id: str) -> None:
        try:
            self.client.resign(game_id)
        except LichessAPIError as exc:
            self._push(Error(f"resign failed: {exc}"))

    def abort(self, game_id: str) -> None:
        try:
            self.client.abort(game_id)
        except LichessAPIError as exc:
            self._push(Error(f"abort failed: {exc}"))

    # --- thread targets -----------------------------------------------------

    def _event_thread(self) -> None:
        try:
            self._process_event_stream(self.client.stream_events())
        except Exception as exc:  # noqa: BLE001 - surface any failure to the GUI
            logger.exception("event stream crashed")
            self._push(Error(f"event stream: {exc}"))

    def _start_game_thread(self, game_id: str) -> None:
        # Drop finished threads so the list does not grow unbounded over many
        # games (daemon threads need not be joined, but we keep live references
        # so they are not garbage-collected while running).
        self._threads = [t for t in self._threads if t.is_alive()]
        thread = threading.Thread(target=self._game_thread, args=(game_id,),
                                  daemon=True, name=f"lichess-game-{game_id}")
        self._threads.append(thread)
        thread.start()

    def _game_thread(self, game_id: str) -> None:
        try:
            self._process_game_stream(game_id, self.client.stream_game(game_id))
        except Exception as exc:  # noqa: BLE001
            logger.exception("game stream crashed for %s", game_id)
            self._push(Error(f"game stream {game_id}: {exc}"))

    # --- core event processing (synchronous, testable) ----------------------

    def _process_event_stream(self, events: Iterator[dict]) -> None:
        for obj in events:
            if self._stop.is_set():
                break
            try:
                typ = obj.get("type")
                if typ == "challenge":
                    ch = obj.get("challenge", obj)
                    self._push(ChallengeReceived(
                        challenge_id=str(ch.get("id", "")),
                        opponent=self._challenge_opponent(ch),
                        speed=str(ch.get("speed", "?")),
                        variant=str(ch.get("variant", {}).get("name", "standard")),
                        color=str(ch.get("color", "random")),
                        rated=bool(ch.get("rated", False)),
                    ))
                elif typ == "gameStart":
                    game = obj.get("game", obj)
                    self._start_game_thread(str(game.get("id", "")))
                elif typ == "gameFinish":
                    game = obj.get("game", obj)
                    self._push(Status(f"Game {game.get('id', '?')} finished"))
                else:
                    logger.debug("unknown event type: %s", typ)
            except Exception as exc:  # noqa: BLE001
                logger.exception("error processing event")
                self._push(Error(f"event: {exc}"))

    def _process_game_stream(self, game_id: str, events: Iterator[dict]) -> None:
        initial_fen = STARTING_FEN
        bot_is_white = True
        opponent_name = ""
        initialized = False

        for obj in events:
            if self._stop.is_set():
                break
            try:
                if "white" in obj and "state" in obj:
                    # gameFull event (first message of the game stream)
                    initial_fen = self._normalize_fen(obj.get("initialFen"))
                    bot_is_white = self._bot_is_white(obj)
                    opponent = obj["black"] if bot_is_white else obj["white"]
                    opponent_name = str(opponent.get("name", "?"))
                    state = obj["state"]
                    initialized = True
                    self._push(GameStarted(
                        game_id=game_id, bot_is_white=bot_is_white,
                        opponent_name=opponent_name, initial_fen=initial_fen,
                        moves=self._moves_list(state),
                        wtime=state.get("wtime"), btime=state.get("btime"),
                    ))
                    if self.is_game_over_status(state.get("status", "started")):
                        self._push_game_finished(game_id, initial_fen, state)
                    else:
                        self._maybe_move(game_id, initial_fen, state, bot_is_white)
                elif "moves" in obj:
                    # gameState event (move update)
                    state = obj
                    self._push(GameUpdated(
                        game_id=game_id, moves=self._moves_list(state),
                        wtime=state.get("wtime"), btime=state.get("btime"),
                        status=str(state.get("status", "started")),
                        winner=state.get("winner"),
                    ))
                    if self.is_game_over_status(state.get("status", "started")):
                        self._push_game_finished(game_id, initial_fen, state)
                    elif initialized:
                        self._maybe_move(game_id, initial_fen, state, bot_is_white)
                elif obj.get("type") == "opponentGone":
                    continue
                else:
                    logger.debug("unknown game event: %s", obj.get("type"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("error processing game event for %s", game_id)
                self._push(Error(f"game {game_id}: {exc}"))

    # --- helpers ------------------------------------------------------------

    def _push(self, event: object) -> None:
        self.event_queue.put(event)

    def _push_game_finished(self, game_id: str, initial_fen: str, state: dict) -> None:
        self._push(GameFinished(
            game_id=game_id,
            status=str(state.get("status", "finished")),
            winner=state.get("winner"),
            moves=self._moves_list(state),
            initial_fen=initial_fen,
        ))

    @staticmethod
    def is_game_over_status(status: Optional[str]) -> bool:
        return status not in ACTIVE_STATUSES

    def _bot_is_white(self, game_full: dict) -> bool:
        white_name = str(game_full.get("white", {}).get("name", "")).lower()
        return white_name == self._username.lower()

    @staticmethod
    def _normalize_fen(fen: Optional[str]) -> str:
        if fen is None or fen == "" or fen == "startpos":
            return STARTING_FEN
        return fen

    @staticmethod
    def _moves_list(state: dict) -> tuple[str, ...]:
        raw = state.get("moves", "") or ""
        return tuple(raw.split()) if raw else ()

    @staticmethod
    def _challenge_opponent(challenge: dict) -> str:
        user = challenge.get("user") or challenge.get("challenger") or {}
        return str(user.get("name") or user.get("id") or "?")

    def _build_board(self, initial_fen: str, moves: tuple[str, ...]) -> Optional[Board]:
        """Rebuild the board from the initial FEN + the full UCI move list."""
        try:
            game = GameState(Board.from_fen(initial_fen))
            for uci in moves:
                game.make_move(uci_to_move(uci))
            return game.board
        except (ValueError, IndexError, KeyError, TypeError) as exc:
            logger.warning("failed to rebuild board: %s", exc)
            return None

    def _time_budget(self, state: dict, bot_is_white: bool) -> int:
        """Simple per-move time budget in ms, derived from the bot's clock."""
        remaining = state.get("wtime") if bot_is_white else state.get("btime")
        inc = state.get("winc") if bot_is_white else state.get("binc")
        if remaining is None or remaining <= 0:
            return self.default_movetime_ms  # correspondence / no clock
        budget = remaining // 20 + (inc or 0) // 2
        return max(200, min(budget, 5000))

    def _maybe_move(self, game_id: str, initial_fen: str, state: dict,
                    bot_is_white: bool) -> None:
        """If it is the bot's turn, think and post a move to Lichess."""
        moves = self._moves_list(state)
        bot_to_move = (len(moves) % 2 == 0) if bot_is_white else (len(moves) % 2 == 1)
        if not bot_to_move:
            return

        board = self._build_board(initial_fen, moves)
        if board is None:
            return

        time_limit_ms = self._time_budget(state, bot_is_white)
        # Let the UI show the engine is thinking before the (possibly slow)
        # synchronous search blocks this game thread.
        self._push(Status("Engine thinking..."))
        move = self.engine_choose(board, time_limit_ms=time_limit_ms)
        if move is None:
            return  # game already over (no legal moves)

        uci = move_to_uci(move)
        try:
            self.client.make_move(game_id, uci)
        except LichessAPIError as exc:
            logger.warning("move post failed for %s: %s", game_id, exc)
            self._push(Error(f"move failed: {exc}"))
            return
        self._push(EngineMoved(game_id=game_id, uci=uci))