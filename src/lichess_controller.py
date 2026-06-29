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

import hashlib
import json
import logging
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from src.board import Board, STARTING_FEN
from src.game import GameState
from src.moves import uci_to_move, move_to_uci, generate_legal_moves, Move
from src.engine import choose_move
from src.lichess_client import LichessClient, LichessAPIError

logger = logging.getLogger(__name__)

# Lichess game statuses that mean the game is still in progress.
ACTIVE_STATUSES = ("created", "started")
# Game-end statuses that never change a rating (the game didn't get played), so
# we skip the post-game rating refresh for them.
ABORT_LIKE_STATUSES = ("aborted", "noStart")
# Lichess per-speed ratings we surface as "the score" (the profile ``perfs`` map
# has one entry per speed; these are the real-time speeds a bot plays).
RATING_SPEEDS = ("bullet", "blitz", "rapid", "classical")
# Delay before re-reading ratings after a finished game, so Lichess has settled
# the new Glicko rating. Configurable (tests pass 0) via ``rating_refresh_delay_s``.
RATING_REFRESH_DELAY_S = 1.5

# How long an auto-accept "claim" holds before a gameStart must confirm it. Bounds
# the window where a declined/expired accepted challenge could otherwise leave the
# bot thinking it is still accepting (and thus never auto-match again).
ACCEPT_CLAIM_TIMEOUT_S = 20.0
# Reconnect backoff ceilings for the event and game streams.
MAX_RECONNECT_BACKOFF_S = 60.0

# Per-account singleton lock (diagnostic, requested by the opponent's
# xmiao_glm.md §3). Two live processes for the SAME account each open an event
# stream, Lichess pushes every gameStart to BOTH, and both connect to the same
# game's stream -- which aborts the game at creation. A localhost TCP lock per
# account lets us prove (and warn) when a second process for our own account is
# already running. The port is deterministic per username so two processes for
# the same account collide on it. SOFT: a collision is logged + flagged, never
# blocks (a transient bind failure must not brick the GUI).
SINGLETON_PORT_BASE = 0x6CC0  # 27840 -- registered range, low collision risk
SINGLETON_PORT_RANGE = 0x400  # 1024 ports

# Our streaming bot can't safely resume a multi-day correspondence game after a
# restart (the event stream only delivers NEW gameStarts, not in-progress ones),
# so auto-accept declines correspondence from configured peers. Real-time speeds
# (blitz/bullet/rapid/classical) are still auto-accepted; "tooSlow" is a Lichess-
# accepted decline-reason code (request param is camelCase; the response key is
# lowercase ``tooslow``).
DECLINE_CORRESPONDENCE_REASON = "tooSlow"
CORRESPONDENCE_DECLINE_MSG = (
    "Declined correspondence challenge from {opp} — "
    "only blitz, bullet, classical, rapid are supported"
)
# The bot's first move of a game is capped low so it lands quickly. Lichess
# disallows a single-player abort once the first move is on the board, so a fast
# opening move locks the game in before an opponent (or impatient human) can
# abort — a 1s search from the start position is plenty for a sound opening.
FIRST_MOVE_BUDGET_MS = 1000

# EXPERIMENT A -- instant opening-book first move (see the
# lichess-instant-abort-duplicate-stream memory). When we are WHITE and it is
# move 1 from the STANDARD starting position, play a book move with NO engine
# think. Originally aimed to land move 1 at ~POST-RTT (~0.3s on our in-process
# controller) -- before a same-owner creation abort (~0.5-1s, faster than any
# engine think) can land, IF the abort respects "a move is on the board".
#
# TESTED & FALSIFIED as a workaround (game CTteTHAU, 2026-06-28): the opponent
# (xmiao_ds, White) played an instant e2e4 (0ms think) and it STILL aborted --
# HTTP 400 "game already over"; the abort beat even the fastest possible first
# move, so it does NOT respect an incoming move. A keep-alive POST refactor
# (A2) was considered to cut the urllib no-keep-alive POST RTT, but rejected:
# the opponent's keep-alive lichess-bot POST already lost the same race.
#
# RETAINED as a TOGGLEABLE DIAGNOSTIC / fast move-1 only, NOT a workaround.
# Set to () to disable (the engine then chooses move 1 as before). It applies
# only on move 1 / only when we are White (when we are Black the opponent,
# White, must move first). No code fix on either side stops the abort; the
# decisive test is the third-party-bot experiment (see the memory). Legality is
# verified against the rebuilt board, so a non-standard initialFen is safe and a
# book entry that is not a legal move 1 (e.g. castling, impossible from the
# start) is silently skipped. Extend the tuple for variety (first legal entry
# played).
_OPENING_BOOK_WHITE_MOVE1: tuple[str, ...] = ("e2e4",)


@dataclass(frozen=True)
class ChallengeReceived:
    challenge_id: str
    opponent: str
    speed: str
    variant: str
    color: str  # "white" / "black" / "random"
    rated: bool


@dataclass(frozen=True)
class ChallengeSent:
    """Emitted when the bot creates an outgoing challenge (manual or auto)."""
    challenge_id: str
    opponent: str
    # The speed/time-control Lichess actually assigned (e.g. "rapid",
    # "correspondence"). If Lichess ignored our clock (e.g. params sent wrong)
    # this is "correspondence" — surfacing it lets the GUI warn loudly instead of
    # silently playing a no-clock game.
    speed: str = ""
    clock: str = ""  # "limit+increment", e.g. "300+3"; "" for correspondence
    # Whether the challenge is rated (True) or casual (False). Echoed from
    # Lichess's challenge JSON when available, else falls back to what we
    # requested (``self.rated``). Surfaced so the GUI can log "rated"/"casual"
    # explicitly — the mode is relevant to the abort investigation (the opponent
    # now declines casual) and is NOT itself an asserted fix.
    rated: bool = False


@dataclass(frozen=True)
class ChallengeDeclined:
    """Emitted when an OUTGOING challenge of ours is declined by the opponent.

    Lichess delivers a ``challengeDeclined`` event on the event stream. Without
    handling it, a decline was silently dropped and the GUI log just stopped at
    "Challenged ..." — leaving the user unable to see *why* no game started (the
    prior failure mode: the opponent declines casual challenges). Surfacing it
    makes the cause visible. ``opponent`` is the account we challenged
    (``destUser``), and ``reason`` is Lichess's decline-reason code when present.
    """
    opponent: str
    reason: str = ""


@dataclass(frozen=True)
class GameStarted:
    game_id: str
    bot_is_white: bool
    opponent_name: str
    initial_fen: str
    moves: tuple[str, ...]
    wtime: Optional[int]
    btime: Optional[int]
    # Lichess speed ("bullet"/"blitz"/"rapid"/"classical"/...). Lets the GUI show
    # the rating for the speed actually being played (the one that refreshes).
    speed: str = ""


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


@dataclass(frozen=True)
class AccountInfo:
    """The connected account's username + per-speed ratings ("the Lichess score").

    Pushed once on connect (right after the profile fetch) and again after each
    finished game (re-fetched), so the GUI can show the account name and a
    real-time rating that refreshes as games complete. ``ratings`` maps a Lichess
    speed (``bullet``/``blitz``/``rapid``/``classical``) to its rating integer;
    it is empty before connect or if the account has played no games.
    """
    username: str
    ratings: dict = field(default_factory=dict)


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
        opponents: tuple[str, ...] = (),
        auto_accept: bool = False,
        auto_challenge: bool = False,
        challenge_period_s: float = 30.0,
        clock_limit_s: Optional[int] = 300,
        clock_increment_s: int = 3,
        rated: bool = False,
        variant: str = "standard",
        color: str = "random",
        singleton_bind: Optional[Callable[[str], bool]] = None,
        rating_refresh_delay_s: float = RATING_REFRESH_DELAY_S,
    ) -> None:
        self.client = client if client is not None else LichessClient(token)
        self.engine_choose = engine_choose
        self.default_movetime_ms = default_movetime_ms
        # Auto-match configuration. ``opponents`` is the set of usernames this
        # bot may auto-challenge and (when filtering) auto-accept challenges from.
        self.opponents: tuple[str, ...] = tuple(opponents)
        self.auto_accept = bool(auto_accept)
        self.auto_challenge = bool(auto_challenge)
        self.challenge_period_s = float(challenge_period_s)
        self.clock_limit_s = clock_limit_s
        self.clock_increment_s = int(clock_increment_s)
        self.rated = bool(rated)
        self.variant = variant
        self.color = color
        self.event_queue: queue.Queue[object] = queue.Queue()
        self._stop = threading.Event()
        self._username: str = ""
        # Per-speed ratings ("the Lichess score"), refreshed after each finished
        # game. Populated from the profile ``perfs`` map; empty until connect.
        self._ratings: dict[str, int] = {}
        # Delay before re-reading ratings after a finished game so Lichess has
        # settled the new rating. Configurable so tests can pass 0.
        self._rating_refresh_delay_s = float(rating_refresh_delay_s)
        # Whether the linked account is a Bot account (``title == "BOT"``). The
        # bot-only endpoints (game stream, make move) 400 for a normal account,
        # so challenges are blocked until this is True. Defaults True so a
        # profile-fetch failure doesn't spuriously lock the GUI.
        self._is_bot: bool = True
        self._threads: list[threading.Thread] = []
        # Live state shared across the event / game / auto-challenge threads.
        self._state_lock = threading.RLock()
        self._active_games: set[str] = set()
        self._pending_outgoing: dict[str, str] = {}  # opponent -> challenge id
        self._accepting: bool = False  # transient: accepted, awaiting gameStart
        self._accepting_until: float = 0.0  # monotonic deadline for the claim
        # Game ids that currently have a live stream thread. Guards against the
        # event stream reconnecting and re-emitting ``gameStart`` for a game we
        # are already streaming (which would start a second, racing thread).
        self._streaming: set[str] = set()
        # Per-game open-stream counter (opponent's xmiao_glm.md §1). n=1 is the
        # normal single stream; n>1 means a DUPLICATE game-stream connection for
        # the same game (a second event-stream connection on this account) -- the
        # abort cause. Increment on open, decrement on close, under the lock.
        self._stream_count: dict[str, int] = {}
        self._challenge_thread: Optional[threading.Thread] = None
        # Per-account singleton lock callable (xmiao_glm.md §3). Returns True iff
        # THIS process exclusively acquired the lock for the account. Default
        # None -> use the real localhost-port bind in start(). Inject a fake in
        # tests so they never open real sockets. SOFT: never blocks.
        self._singleton_bind: Optional[Callable[[str], bool]] = singleton_bind
        self._singleton_sock: Optional[socket.socket] = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Fetch the bot profile, then start the event stream thread."""
        try:
            profile = self.client.get_profile()
            self._username = profile.get("username", "") or profile.get("id", "")
        except LichessAPIError as exc:
            self._push(Error(f"profile fetch failed: {exc}"))
            return
        # Bot-only endpoints (game stream, make move) reject a normal account
        # with HTTP 400 "This endpoint can only be used with a Bot account".
        # Detect that up front so we can block challenges and tell the user to
        # upgrade, rather than letting the first game stream fail cryptically.
        self._is_bot = (profile.get("title") == "BOT")
        # Per-speed ratings ("the Lichess score") for the GUI header.
        self._ratings = self._extract_ratings(profile)
        # Per-account singleton lock (xmiao_glm.md §3): prove we are the ONLY
        # process for this account. A second process double-connects the game
        # stream and aborts every game at creation. SOFT -- a failure is logged +
        # flagged, never blocks. Acquired BEFORE the connect line so the line
        # itself reports the result ("single instance" vs "DUPLICATE").
        single = self._acquire_singleton()
        # Include the OS PID so a second `Connected as <name>` line can be told
        # apart from a reconnect: a reconnect keeps the SAME pid; a second process
        # has a different pid. This lets us prove from our own log that our side
        # did not spawn a second event-stream connection (see the instant-abort
        # analysis — two `Connected as` lines alone do NOT prove a duplicate).
        if single:
            self._push(Status(
                f"Connected as {self._username} (PID {os.getpid()}, single instance)"))
        else:
            self._push(Status(
                f"Connected as {self._username} (PID {os.getpid()}) — DUPLICATE: "
                "another process is already running this account. Two live event "
                "streams double-connect the game stream and abort every game; "
                "close the other instance (check Task Manager for a 2nd process)."))
            self._push(Error(
                f"Duplicate {self._username} instance detected — another process "
                "is already running this account. Close it before playing."))
        if not self._is_bot:
            self._push(Error(
                f"@{self._username} is not a Bot account. The Lichess bot API "
                "requires a Bot account — click 'Upgrade to Bot' (or upgrade at "
                "lichess.org → Settings → API access) before challenging."))
        thread = threading.Thread(target=self._event_thread, daemon=True,
                                  name="lichess-event-stream")
        self._threads.append(thread)
        thread.start()
        self._ensure_challenge_thread()
        # Surface the account name + ratings so the GUI header can render them
        # immediately on connect (and again after each finished game).
        self._push(AccountInfo(username=self._username, ratings=dict(self._ratings)))

    def upgrade_account(self) -> None:
        """Upgrade the linked account to a Bot account (IRREVERSIBLE).

        POSTs ``/api/bot/account/upgrade``, then re-fetches the profile so
        ``is_bot`` reflects the new state and auto-challenge (if configured)
        can begin. Called from the GUI's 'Upgrade to Bot' button.
        """
        try:
            self.client.upgrade_to_bot()
        except LichessAPIError as exc:
            self._push(Error(f"upgrade failed: {exc}"))
            return
        try:
            profile = self.client.get_profile()
            self._username = profile.get("username", "") or profile.get("id", "")
            self._is_bot = (profile.get("title") == "BOT")
            self._ratings = self._extract_ratings(profile)
        except LichessAPIError as exc:
            self._push(Error(f"upgrade sent but profile re-fetch failed: {exc}"))
            return
        self._push(AccountInfo(username=self._username, ratings=dict(self._ratings)))
        if self._is_bot:
            self._push(Status(f"Upgraded to Bot account as {self._username}"))
            self._ensure_challenge_thread()
        else:
            self._push(Error("Upgrade returned success but account is still not a Bot"))

    # --- per-account singleton lock (xmiao_glm.md §3) ----------------------

    @staticmethod
    def _singleton_port_for(username: str) -> int:
        """Deterministic localhost port for an account's singleton lock.

        Same username -> same port (two processes for the same account collide,
        so the second is detected). Different usernames -> different ports (our
        own two bots can coexist). Uses ``hashlib.md5`` -- NOT ``hash()``, which
        is randomized per process and would give each process a different port
        and so defeat the lock. The port lands in the registered range.
        """
        digest = hashlib.md5(username.strip().lower().encode("utf-8"),
                             usedforsecurity=False).digest()
        return SINGLETON_PORT_BASE + (int.from_bytes(digest[:2], "big")
                                      % SINGLETON_PORT_RANGE)

    def _default_singleton_bind(self, username: str) -> bool:
        """Bind a per-account localhost socket. Returns True iff acquired.

        SO_REUSEADDR is deliberately NOT set: on Windows it permits port
        sharing, which would let a second process bind the same port and defeat
        the lock. A failure (port in use -> another process holds it, or a
        transient bind error) returns False; the caller logs + flags it but
        keeps running.
        """
        port = self._singleton_port_for(username or "lichess-bot")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
        except OSError:
            try:
                sock.close()
            except OSError:
                pass
            return False
        self._singleton_sock = sock
        return True

    def _acquire_singleton(self) -> bool:
        """Acquire the per-account singleton lock. SOFT: never raises."""
        bind = self._singleton_bind if self._singleton_bind is not None \
            else self._default_singleton_bind
        try:
            return bool(bind(self._username))
        except Exception as exc:  # noqa: BLE001 - soft; never block on the lock
            logger.debug("singleton bind raised: %s", exc)
            return False

    @property
    def is_bot(self) -> bool:
        return self._is_bot

    def _ensure_challenge_thread(self) -> None:
        """Start the periodic auto-challenge thread if configured and not running."""
        if not (self.auto_challenge and self.opponents):
            return
        if not self._is_bot:
            return  # a normal account cannot play bot games; don't auto-challenge
        if self._challenge_thread is not None and self._challenge_thread.is_alive():
            return
        self._challenge_thread = threading.Thread(
            target=self._challenge_loop, daemon=True,
            name="lichess-auto-challenge")
        self._threads.append(self._challenge_thread)
        self._challenge_thread.start()

    def stop(self) -> None:
        """Signal background threads to stop and tear down open streams.

        Setting ``_stop`` alone does not interrupt a thread blocked reading the
        NDJSON stream — on Windows it only notices at the next keep-alive line
        (~7s). That delay means a controller restart (``_start_lichess`` stops the
        old controller, then opens a new event stream) would briefly run TWO
        event streams for this account, which Lichess sees as a second bot
        instance and aborts games at creation. So we also close the open
        streaming HTTP responses now: closing the socket unblocks the pending
        read and tears the connection down before the new stream opens.
        Guarded with ``getattr`` so a client/mock without ``close_streams`` is fine.
        """
        self._stop.set()
        close = getattr(self.client, "close_streams", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort; stop must not raise
                logger.debug("close_streams failed during stop", exc_info=True)
        # Release the per-account singleton lock so a later start() (or another
        # process) can re-acquire it. Best-effort; stop must not raise.
        sock = self._singleton_sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            self._singleton_sock = None

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

    def set_opponents(self, opponents) -> None:
        """Update the opponent list (used by the GUI when the user types a name)."""
        self.opponents = tuple(opponents)
        self._ensure_challenge_thread()

    def set_auto(self, enabled: bool) -> None:
        """Toggle auto-match (auto-accept incoming + periodic auto-challenge)."""
        self.auto_accept = bool(enabled)
        self.auto_challenge = bool(enabled)
        if enabled:
            self._ensure_challenge_thread()

    def set_rated(self, enabled: bool) -> None:
        """Toggle rated vs casual mode for the NEXT challenge.

        Casual (False) is the default and is a fully supported Lichess bot mode.
        Rated is opt-in — surfaced because the opponent (xmiao_ds) now declines
        casual, NOT because rated is a proven fix for the creation-abort (see
        CLAUDE.md: switching is a knob + the decisive experiment, not an asserted
        cause). This affects `_issue_challenge` via ``self.rated``.
        """
        self.rated = bool(enabled)

    def challenge(self, opponent: str) -> None:
        """Manually challenge a specific ``opponent`` (one-shot, no leader/follower)."""
        opp = (opponent or "").strip()
        if not opp:
            self._push(Error("no opponent specified"))
            return
        if not self._is_bot:
            self._push(Error(
                "Not a Bot account — click 'Upgrade to Bot' before challenging"))
            return
        with self._state_lock:
            prev_id = self._pending_outgoing.pop(opp, None)
        if prev_id:
            self._cancel_safely(prev_id)  # don't orphan a still-open challenge
        cid = self._issue_challenge(opp)
        if cid:
            with self._state_lock:
                self._pending_outgoing[opp] = cid

    # --- thread targets -----------------------------------------------------

    def _event_thread(self) -> None:
        """Run the event stream, reconnecting with backoff until stopped.

        A single dropped connection must not permanently disable auto-match, so
        we re-open the stream after a backoff. A run that lasted a while is
        treated as healthy (reset backoff); a run that died immediately grows
        the backoff up to :data:`MAX_RECONNECT_BACKOFF_S`.
        """
        backoff = 1.0
        while not self._stop.is_set():
            start = time.monotonic()
            try:
                self._process_event_stream(self.client.stream_events(self._stop))
            except Exception as exc:  # noqa: BLE001 - surface, then reconnect
                logger.exception("event stream error")
                self._push(Error(f"event stream: {exc}"))
            if self._stop.is_set():
                break
            backoff = self._next_backoff(backoff, time.monotonic() - start)
            if self._stop.wait(backoff):
                break
            # About to re-open the event stream after it dropped. Log it so
            # reconnects are visible in the activity log: a reconnect is ONE
            # process re-opening its stream, NOT a second bot instance. This
            # line (asked for by the opponent's checklist, XMIAO_GLM.md item 4)
            # lets us prove from our own log whether our side reconnected and how
            # many times — which the instant-abort analysis needs to rule our
            # side in or out (two `Connected as <name>` lines alone do NOT prove a
            # duplicate; a reconnect keeps the SAME pid, a second process a
            # different one).
            self._push(Status(
                "Reconnecting event stream (previous stream closed)"))

    def _game_thread(self, game_id: str) -> None:
        """Run one game stream, reconnecting until the game ends or we stop.

        A 400/404 means the game is not streamable (already finished/aborted,
        or an id from a stale event): reconnecting would just spam the same
        error forever, so we free the slot and stop. Other errors are treated
        as transient and retried with backoff.
        """
        backoff = 1.0
        try:
            while not self._stop.is_set():
                start = time.monotonic()
                try:
                    ended = self._process_game_stream(
                        game_id, self.client.stream_game(game_id, self._stop))
                except LichessAPIError as exc:
                    self._push(Error(f"game stream {game_id}: {exc}"))
                    if getattr(exc, "status", None) in (400, 404):
                        with self._state_lock:
                            self._active_games.discard(game_id)
                        return  # game is gone; retrying cannot help
                    ended = False
                except Exception as exc:  # noqa: BLE001
                    logger.exception("game stream crashed for %s", game_id)
                    self._push(Error(f"game stream {game_id}: {exc}"))
                    ended = False
                if ended or self._stop.is_set():
                    break
                backoff = self._next_backoff(backoff, time.monotonic() - start)
                if self._stop.wait(backoff):
                    break
        finally:
            with self._state_lock:
                self._streaming.discard(game_id)

    @staticmethod
    def _next_backoff(prev: float, ran_for: float) -> float:
        if ran_for > 10.0:
            return 1.0  # the stream was healthy; reset on reconnect
        return min(prev * 2.0, MAX_RECONNECT_BACKOFF_S)

    def _start_game_thread(self, game_id: str) -> None:
        # Drop finished threads so the list does not grow unbounded over many
        # games (daemon threads need not be joined, but we keep live references
        # so they are not garbage-collected while running).
        self._threads = [t for t in self._threads if t.is_alive()]
        thread = threading.Thread(target=self._game_thread, args=(game_id,),
                                  daemon=True, name=f"lichess-game-{game_id}")
        self._threads.append(thread)
        thread.start()

    # --- core event processing (synchronous, testable) ----------------------

    def _process_event_stream(self, events: Iterator[dict]) -> None:
        for obj in events:
            if self._stop.is_set():
                break
            try:
                typ = obj.get("type")
                if typ == "challenge":
                    ch = obj.get("challenge", obj)
                    cid = str(ch.get("id", ""))
                    challenger = ch.get("challenger") or ch.get("user") or {}
                    challenger_name = str(
                        challenger.get("name") or challenger.get("id") or "")
                    dest = ch.get("destUser") or {}
                    me = (self._username or "").lower()
                    direction = str(ch.get("direction", "")).lower()
                    # Treat as outgoing if Lichess says so, OR if we are clearly
                    # the challenger. The ``direction`` field is sometimes absent
                    # (e.g. for challenges we just created), and without this
                    # fallback our own outgoing challenge would be misclassified
                    # as incoming and surface as ``Challenge: <self>``.
                    is_outgoing = direction == "out" or (
                        bool(challenger_name) and challenger_name.lower() == me)
                    if is_outgoing:
                        # Our own outgoing challenge — track it, never auto-accept.
                        opponent = str(dest.get("name") or dest.get("id")
                                       or challenger_name or "?")
                        if cid and opponent != "?":
                            with self._state_lock:
                                self._pending_outgoing[opponent] = cid
                    else:
                        opponent = self._challenge_opponent(ch)
                        speed = str(ch.get("speed", "")).lower()
                        auto = self._should_auto_accept(opponent)
                        if auto and speed == "correspondence":
                            # Decline correspondence: our streaming bot can't
                            # safely resume a multi-day game after a restart, so
                            # don't tie a peer up in one. (Only auto-declines from
                            # configured peers; non-peer/offline correspondence
                            # still surfaces for a manual accept/decline choice.)
                            try:
                                self.client.decline_challenge(
                                    cid, reason=DECLINE_CORRESPONDENCE_REASON)
                                self._push(Status(
                                    CORRESPONDENCE_DECLINE_MSG.format(opp=opponent)))
                            except LichessAPIError as exc:
                                self._push(Error(f"decline failed: {exc}"))
                        elif auto and self._has_pending_outgoing(opponent):
                            # We just challenged this peer (manual or auto) and
                            # their near-simultaneous reverse challenge arrived
                            # before the game started. Accepting it would start a
                            # SECOND game; the two bots can then each latch onto
                            # a different one and both appear to wait on the other
                            # (one aborts). Decline to keep exactly one game.
                            try:
                                self.client.decline_challenge(cid, reason="later")
                                self._push(Status(
                                    f"Declined reverse challenge from {opponent} — "
                                    "already starting a game with them"))
                            except LichessAPIError as exc:
                                self._push(Error(f"decline failed: {exc}"))
                        elif auto and self._claim_accept():
                            # Claim the accept atomically (busy-check + set under
                            # the lock) so two near-simultaneous incoming
                            # challenges, or a challenge arriving while a game
                            # starts, can't start two games.
                            try:
                                self.client.accept_challenge(cid)
                                self._push(
                                    Status(f"Auto-accepted challenge from {opponent}"))
                            except LichessAPIError as exc:
                                self._release_accept()
                                self._push(Error(f"auto-accept failed: {exc}"))
                        else:
                            self._push(ChallengeReceived(
                                challenge_id=cid,
                                opponent=opponent,
                                speed=str(ch.get("speed", "?")),
                                variant=str(ch.get("variant", {}).get("name", "standard")),
                                color=str(ch.get("color", "random")),
                                rated=bool(ch.get("rated", False)),
                            ))
                elif typ == "challengeDeclined":
                    # Our outgoing challenge was declined by the opponent. Surface
                    # it (the GUI logs it) and drop the pending-outgoing tracking
                    # so a near-simultaneous reverse challenge isn't wrongly
                    # declined and the auto-loop can re-challenge.
                    ch = obj.get("challenge", obj)
                    dest = ch.get("destUser") or {}
                    opponent = str(dest.get("name") or dest.get("id")
                                   or self._challenge_opponent(ch))
                    reason = str(
                        obj.get("declineReason") or ch.get("declineReason")
                        or "").strip()
                    self._forget_outgoing(opponent)
                    self._push(ChallengeDeclined(opponent=opponent, reason=reason))
                elif typ == "challengeCanceled":
                    # Our outgoing challenge was canceled (by us) or expired.
                    # Surface + clear tracking so we don't treat it as pending.
                    ch = obj.get("challenge", obj)
                    dest = ch.get("destUser") or {}
                    opponent = str(dest.get("name") or dest.get("id")
                                   or self._challenge_opponent(ch))
                    self._forget_outgoing(opponent)
                    self._push(Status(
                        f"Challenge to {opponent} was canceled or expired"))
                elif typ == "gameStart":
                    game = obj.get("game", obj)
                    gid = str(game.get("id", ""))
                    with self._state_lock:
                        if gid in self._streaming:
                            # Already streaming this game. A benign cause is the
                            # event stream reconnecting and re-emitting gameStart
                            # for an in-progress game; a concerning cause is a
                            # SECOND event-stream connection for this account (a
                            # duplicate bot instance) — the same signature as the
                            # instant-at-creation abort. Log it so the pattern is
                            # visible either way; never start a second stream.
                            self._push(Status(
                                f"Duplicate gameStart for {gid} received — already "
                                "streaming. Common after an event-stream reconnect; "
                                "if it recurs, a second event-stream connection "
                                "(duplicate bot instance) is active for this "
                                "account."))
                            continue
                        self._streaming.add(gid)
                        self._active_games.add(gid)
                        self._accepting = False
                    # Open the game stream FIRST, before the housekeeping cancel
                    # HTTP. The opponent's analysis (xmiao_glm.md, game LtnFaUxZ,
                    # 2026-06-28) found a ~2s gap between our gameStart and our
                    # game-stream open, during which Lichess aborted the game
                    # before we connected. Previously _cancel_all_pending() ran
                    # SYNCHRONOUSLY here, BEFORE _start_game_thread(), a wasted RTT
                    # that delayed the time-critical stream open. Now we spawn the
                    # game thread first so the stream opens immediately; the
                    # cancel runs after, in parallel with the game-thread
                    # handshake, so it no longer gates the connection. (The cancel
                    # is housekeeping that keeps us at one game at a time; running
                    # it after the spawn is safe -- _active_games is already set
                    # under the lock, so the auto-challenge loop won't issue
                    # another.)
                    #
                    # CRITICAL (root cause of the instant-at-creation abort, user
                    # report 2026-06-28): the earlier note that "the just-accepted
                    # challenge 4xx's on cancel" was WRONG. Lichess reuses the
                    # challenge id as the game id, so the pending-outgoing entry
                    # for the challenge we just issued and got accepted has the
                    # SAME id as this game. _cancel_all_pending() used to cancel
                    # it -- POSTing /api/challenge/{id}/cancel on our own
                    # just-accepted 0-move game, which Lichess honors as the
                    # challenger withdrawing and ABORTS the game at creation (0
                    # moves, <1s). The acceptor never cancels (no pending
                    # outgoing), so only bot-vs-bot -- both sides running this code
                    # -- aborted; a human web game between the same two accounts
                    # on the same two machines played fine (user-verified), proving
                    # the abort is this cancel, not a Lichess/owner/IP policy. The
                    # reorder above alone did NOT stop the aborts (the cancel still
                    # ran and still aborted). The actual fix is in
                    # _cancel_safely: it now skips any challenge id that is an
                    # active game, so _cancel_all_pending() here only cancels
                    # OTHER pending challenges (to different peers) and never the
                    # one that became this game.
                    self._start_game_thread(gid)
                    self._cancel_all_pending()
                elif typ == "gameFinish":
                    game = obj.get("game", obj)
                    gid = str(game.get("id", ""))
                    with self._state_lock:
                        self._active_games.discard(gid)
                    # No Status here: the game stream pushes a structured
                    # GameFinished (with status/winner/moves) when it reads the
                    # final state, and that is what the GUI logs as "Game over:
                    # ...". A generic "Game <id> finished" Status would duplicate
                    # that line and can even race ahead of GameStarted (logging
                    # "finished" before "started"), which is confusing.
                else:
                    logger.debug("unknown event type: %s", typ)
            except Exception as exc:  # noqa: BLE001
                logger.exception("error processing event")
                self._push(Error(f"event: {exc}"))

    def _process_game_stream(self, game_id: str, events: Iterator[dict]) -> bool:
        """Process one game stream. Returns True if the game ended (stop reconnecting)."""
        # Log a game-stream OPEN/CLOSE pair with a per-game counter (opponent's
        # xmiao_glm.md §1): n=1 is the normal single stream; n>1 means a
        # DUPLICATE game-stream connection for this game (a second event-stream
        # connection on this account double-connects the same game's stream and
        # aborts it at creation). OPEN fires before the loop; CLOSE fires in the
        # finally so it balances every exit path (normal end, early game-over
        # return, or a raised exception) -- a missing CLOSE would mask a leak.
        n_open = self._game_stream_open(game_id)
        initial_fen = STARTING_FEN
        bot_is_white = True
        opponent_name = ""
        initialized = False
        ended = False
        try:
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
                        # Surface the gameFull's status on arrival (xmiao_glm.md
                        # §2) for EVERY game: LIVE (status=started/created) is
                        # then distinguishable from DEAD (aborted/noStart) in the
                        # log -- a dead gameFull on arrival is the instant-
                        # abort-at-creation signature.
                        self._push(Status(self._format_game_full_status(obj, state)))
                        self._push(GameStarted(
                            game_id=game_id, bot_is_white=bot_is_white,
                            opponent_name=opponent_name, initial_fen=initial_fen,
                            moves=self._moves_list(state),
                            wtime=state.get("wtime"), btime=state.get("btime"),
                            speed=str(obj.get("speed", "") or ""),
                        ))
                        if self.is_game_over_status(state.get("status", "started")):
                            # The gameFull arrived already over: an instant abort at
                            # game creation. We never reach _maybe_move, so no
                            # "Engine thinking..." is pushed — the GUI keys off that
                            # absence to diagnose this case. Surface the ACTUAL status
                            # (aborted vs noStart — different causes) plus the
                            # gameFull's source/speed/variant/titles, and log the
                            # full JSON so the cause is captured even in a headless
                            # run (Lichess gives no explicit abort-by field, so the
                            # status + context is all we have).
                            self._log_already_over_game_full(game_id, obj, state)
                            self._push_game_finished(game_id, initial_fen, state)
                            # Stop reading once the game is over: a trailing gameState
                            # (also "aborted") would otherwise push a SECOND
                            # GameFinished and the GUI would log the abort twice
                            # (game ENTGYOFG, 2026-06-28).
                            return True
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
                            # Stop reading once the game is over (see gameFull branch).
                            return True
                        elif initialized:
                            self._maybe_move(game_id, initial_fen, state, bot_is_white)
                    elif obj.get("type") == "opponentGone":
                        continue
                    else:
                        logger.debug("unknown game event: %s", obj.get("type"))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("error processing game event for %s", game_id)
                    self._push(Error(f"game {game_id}: {exc}"))
            return ended
        finally:
            self._game_stream_close(game_id, n_open)

    def _game_stream_open(self, game_id: str) -> int:
        """Increment the per-game open-stream counter and log OPEN. Returns n."""
        with self._state_lock:
            n = self._stream_count.get(game_id, 0) + 1
            self._stream_count[game_id] = n
        self._push(Status(f"Game stream OPEN id={game_id[:8]} n={n}"))
        if n > 1:
            # n>1 = a second event-stream connection on this account double-
            # connected the game stream -- the instant-at-creation abort cause.
            self._push(Status(
                f"DUPLICATE game stream for {game_id} (n={n})! A second "
                "event-stream connection for this account double-connected the "
                "game stream; Lichess aborts the game at creation."))
        return n

    def _game_stream_close(self, game_id: str, n_open: int) -> None:
        """Decrement the per-game counter and log CLOSE (balances the OPEN)."""
        with self._state_lock:
            cur = self._stream_count.get(game_id, 0)
            self._stream_count[game_id] = max(0, cur - 1)
        self._push(Status(f"Game stream CLOSE id={game_id[:8]} n={n_open}"))

    def _format_game_full_status(self, game_full: dict, state: dict) -> str:
        """One-line gameFull status for the activity log (xmiao_glm.md §2).

        ``LIVE`` for an in-progress game (status in ACTIVE_STATUSES), ``DEAD``
        otherwise (aborted/noStart). A DEAD gameFull on arrival is the instant-
        abort-at-creation signature; logging it for every game lets the two be
        told apart at a glance.
        """
        status = str(state.get("status") or "started")
        w = game_full.get("white") or {}
        b = game_full.get("black") or {}
        wname = str(w.get("name") or w.get("id") or "?")
        bname = str(b.get("name") or b.get("id") or "?")
        marker = "LIVE" if status in ACTIVE_STATUSES else "DEAD"
        return (f"gameFull: status={status}, White={wname}, Black={bname} "
                f"({marker})")

    # --- helpers ------------------------------------------------------------

    def _push(self, event: object) -> None:
        self.event_queue.put(event)

    def _push_game_finished(self, game_id: str, initial_fen: str, state: dict) -> None:
        # A game ending on the game stream also frees the auto-match loop.
        with self._state_lock:
            self._active_games.discard(game_id)
            self._accepting = False
        status = str(state.get("status", "finished"))
        self._push(GameFinished(
            game_id=game_id,
            status=status,
            winner=state.get("winner"),
            moves=self._moves_list(state),
            initial_fen=initial_fen,
        ))
        # Refresh the rating after a real game (a completed game can change it).
        # Aborted/noStart games never affect rating, so skip the fetch for them.
        if status not in ABORT_LIKE_STATUSES:
            self._refresh_ratings()

    def _log_already_over_game_full(self, game_id: str, game_full: dict,
                                    state: dict) -> None:
        """Log everything Lichess told us about a game that was already over
        when we connected (instant abort at creation).

        Lichess gives no explicit "who aborted" field, so the status
        (``aborted`` vs ``noStart`` — different causes) plus the source/speed/
        variant/titles is all we have. The full JSON goes to the logger (for a
        headless run); a concise summary goes to the activity log.
        """
        status = str(state.get("status") or "finished")
        try:
            logger.warning("game %s gameFull already over (status=%s): %s",
                           game_id, status, json.dumps(game_full))
        except (TypeError, ValueError):
            logger.warning("game %s gameFull already over (status=%s)",
                           game_id, status)
        src = str(game_full.get("source", "") or "")
        speed = str(game_full.get("speed", "") or "")
        variant_obj = game_full.get("variant")
        variant = (str(variant_obj.get("name", "")) if isinstance(variant_obj, dict)
                   else str(variant_obj or ""))
        w = game_full.get("white") or {}
        b = game_full.get("black") or {}
        wname = str(w.get("name") or w.get("id") or "?")
        bname = str(b.get("name") or b.get("id") or "?")
        wtitle = str(w.get("title") or "")
        btitle = str(b.get("title") or "")
        parts = [f"status={status}"]
        if src:
            parts.append(f"source={src}")
        if speed:
            parts.append(f"speed={speed}")
        if variant:
            parts.append(f"variant={variant}")
        parts.append(f"white={wname}{'/' + wtitle if wtitle else ''}")
        parts.append(f"black={bname}{'/' + btitle if btitle else ''}")
        self._push(Status(
            f"Game {game_id} was already over when we connected — "
            + ", ".join(parts)
            + ". Aborted at creation (before either side moved)."))

    # --- auto-match helpers ------------------------------------------------

    def _should_auto_accept(self, opponent: str) -> bool:
        """Config/peer check only (fast, lock-free). The busy check happens at
        claim time (:meth:`_claim_accept`) so the decision is atomic.

        Requiring a configured peer prevents the bot from silently accepting
        challenges from arbitrary accounts when Auto is on.
        """
        if not self.auto_accept or not self.opponents:
            return False
        peers = {o.lower() for o in self.opponents}
        return opponent.lower() in peers

    def _accepting_active(self) -> bool:
        """True if an accept claim is still within its timeout (call under lock)."""
        if self._accepting and time.monotonic() > self._accepting_until:
            self._accepting = False  # timed out without a gameStart; release
        return self._accepting

    def _claim_accept(self) -> bool:
        """Atomically claim an accept slot iff idle. Bounds concurrent games to one."""
        with self._state_lock:
            if self._active_games or self._accepting_active():
                return False
            self._accepting = True
            self._accepting_until = time.monotonic() + ACCEPT_CLAIM_TIMEOUT_S
            return True

    def _release_accept(self) -> None:
        with self._state_lock:
            self._accepting = False

    def _has_pending_outgoing(self, opponent: str) -> bool:
        """True if we have an as-yet-unstarted outgoing challenge to ``opponent``.

        Used to decline a peer's reverse challenge that lands while our own
        challenge to them is still pending (would otherwise start a second game).
        """
        key = (opponent or "").lower()
        if not key:
            return False
        with self._state_lock:
            return any(k.lower() == key for k in self._pending_outgoing)

    def _forget_outgoing(self, opponent: str) -> None:
        """Drop any pending-outgoing entry for ``opponent`` (case-insensitive).

        Called when our challenge to them is declined / canceled / expired so we
        don't keep treating it as pending — a stale entry would wrongly decline a
        peer's near-simultaneous reverse challenge (the double-game guard) and
        block the auto-challenge loop from re-issuing.
        """
        key = (opponent or "").lower()
        if not key:
            return
        with self._state_lock:
            for k in [k for k in self._pending_outgoing if k.lower() == key]:
                self._pending_outgoing.pop(k, None)

    def _auto_challenge_targets(self) -> list[str]:
        """Leader/follower: only challenge peers whose username sorts after ours.

        When two identical bots each list the other as a peer and both have Auto
        on, exactly one of them satisfies ``me < peer`` — so only one challenge
        is ever created and exactly one game starts (no double games).
        """
        me = (self._username or "").lower()
        return [o for o in self.opponents if o.lower() > me]

    def _cancel_safely(self, challenge_id: str) -> None:
        if not challenge_id:
            return
        with self._state_lock:
            # A challenge that was accepted is now the LIVE, active 0-move game:
            # Lichess reuses the challenge id as the game id, so the pending-
            # outgoing entry whose id == an active game id IS the game we just
            # started. Canceling it POSTs /api/challenge/{id}/cancel on our own
            # just-accepted challenge, which Lichess honors as the challenger
            # withdrawing from their own 0-move game -> the game is ABORTED at
            # creation (0 moves, <1s). This was the root cause of the instant-
            # at-creation abort (user report, 2026-06-28): on gameStart the
            # CHALLENGER's _cancel_all_pending() fired this cancel and aborted
            # its own just-started game; the ACCEPTOR never cancels (it has no
            # pending outgoing), which is why only bot-vs-bot (both sides running
            # this code) aborted while a human web game between the same two
            # accounts on the same two machines played fine (user-verified) --
            # the abort is this cancel call, NOT a Lichess/owner/IP policy. Skip
            # the cancel and let the game play; the caller has already cleared
            # the _pending_outgoing entry, so nothing lingers.
            if challenge_id in self._active_games:
                return
        try:
            self.client.cancel_challenge(challenge_id)
        except LichessAPIError:
            logger.debug("cancel %s failed (ignored)", challenge_id)

    def _cancel_all_pending(self) -> None:
        """Cancel every tracked outgoing challenge (HTTP outside the lock).

        Safe to call on gameStart: :meth:`_cancel_safely` skips any entry whose
        id is the active game (the just-accepted challenge), so this only
        cancels OTHER pending challenges (one-game-at-a-time housekeeping) and
        never aborts the game that just started.
        """
        with self._state_lock:
            items = list(self._pending_outgoing.values())
            self._pending_outgoing.clear()
        for cid in items:
            self._cancel_safely(cid)

    def _issue_challenge(self, opponent: str) -> Optional[str]:
        """Create a challenge to ``opponent`` and announce it. Returns its id.

        Tracking/cancellation of the id is the caller's responsibility (so the
        auto-step can re-check idleness before committing).
        """
        try:
            result = self.client.create_challenge(
                opponent, rated=self.rated, clock_limit_s=self.clock_limit_s,
                clock_increment_s=self.clock_increment_s, color=self.color,
                variant=self.variant)
        except LichessAPIError as exc:
            self._push(Error(f"challenge {opponent} failed: {exc}"))
            return None
        cid = ""
        speed = ""
        clock_str = ""
        rated = self.rated
        if isinstance(result, dict):
            ch = result.get("challenge")
            ch = ch if isinstance(ch, dict) else result
            cid = str(ch.get("id") or result.get("id") or "")
            speed = str(ch.get("speed", "")).lower()
            clk = ch.get("clock") or {}
            if isinstance(clk, dict) and clk.get("limit") is not None:
                clock_str = f"{clk.get('limit')}+{clk.get('increment', 0)}"
            # Lichess echoes the rated flag on the challenge object; trust it when
            # present, else keep what we requested (``self.rated``).
            if "rated" in ch:
                rated = bool(ch.get("rated"))
        self._push(ChallengeSent(challenge_id=cid, opponent=opponent,
                                 speed=speed, clock=clock_str, rated=rated))
        # No separate "Challenged … waiting for accept" Status: ChallengeSent is
        # the structured signal, and the GUI sets the status text + logs the line
        # from it. A second Status would only duplicate the log line.
        return cid or None

    def _challenge_loop(self) -> None:
        """Periodically issue auto-challenges while idle (runs on a daemon thread)."""
        while not self._stop.is_set() and not self._username:
            if self._stop.wait(0.5):
                return
        while not self._stop.is_set():
            try:
                self._auto_challenge_step()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                logger.exception("auto-challenge step failed")
                self._push(Error(f"auto-challenge: {exc}"))
            if self._stop.wait(self.challenge_period_s):
                return

    def _auto_challenge_step(self) -> None:
        """One auto-challenge tick: challenge at most one leader/follower peer.

        Issues only a single challenge per tick (so multiple peers can't accept
        simultaneously and start several games), cancels any previously tracked
        outgoing challenge to that peer first, and aborts the new challenge if a
        game starts while it is being created.
        """
        if not self.auto_challenge or not self.opponents:
            return
        targets = self._auto_challenge_targets()
        for opp in targets:
            with self._state_lock:
                if self._active_games or self._accepting_active():
                    return
                prev_id = self._pending_outgoing.pop(opp, None)
            if prev_id:
                self._cancel_safely(prev_id)  # avoid stacking duplicate challenges
            cid = self._issue_challenge(opp)
            if cid:
                with self._state_lock:
                    if self._active_games or self._accepting_active():
                        # A game started while this challenge was being created.
                        # Cancel the fresh challenge -- UNLESS it itself was just
                        # accepted and became that active game (same id), in which
                        # case _cancel_safely skips it so we don't abort our own
                        # just-started game (see the gameStart note + _cancel_safely).
                        self._cancel_safely(cid)
                    else:
                        self._pending_outgoing[opp] = cid
            return  # one peer per tick

    @staticmethod
    def is_game_over_status(status: Optional[str]) -> bool:
        return status not in ACTIVE_STATUSES

    @staticmethod
    def _extract_ratings(profile: dict) -> dict[str, int]:
        """Pull the per-speed ratings ("the Lichess score") out of a profile.

        The Lichess profile carries a ``perfs`` map keyed by speed
        (``bullet``/``blitz``/``rapid``/``classical``/...); each value has a
        ``rating`` integer. Returns only the real-time speeds in
        :data:`RATING_SPEEDS` that are present, as ``{speed: rating}`` (ints).
        Tolerant of a missing/odd ``perfs`` (returns ``{}``).
        """
        perfs = profile.get("perfs") if isinstance(profile, dict) else None
        if not isinstance(perfs, dict):
            return {}
        ratings: dict[str, int] = {}
        for speed in RATING_SPEEDS:
            perf = perfs.get(speed)
            if isinstance(perf, dict):
                rating = perf.get("rating")
                if isinstance(rating, (int, float)):
                    ratings[speed] = int(rating)
        return ratings

    def _refresh_ratings(self, delay_s: Optional[float] = None) -> None:
        """Re-fetch the profile and push an :class:`AccountInfo` with new ratings.

        Runs on a short-lived daemon thread so it never blocks the event/game
        streams or the GUI. Called after a finished game so the GUI's rating
        refreshes in real time; the delay gives Lichess a moment to finalize the
        new Glicko rating before we read it back. Best-effort: a fetch failure is
        logged and dropped (the GUI keeps the last-known rating).
        """
        if self._stop.is_set():
            return
        if delay_s is None:
            delay_s = self._rating_refresh_delay_s

        def _worker() -> None:
            if delay_s > 0 and self._stop.wait(delay_s):
                return  # stopped during the delay
            try:
                profile = self.client.get_profile()
            except LichessAPIError as exc:
                logger.debug("rating refresh failed: %s", exc)
                return
            if not isinstance(profile, dict):
                return
            self._username = (profile.get("username") or profile.get("id")
                              or self._username)
            self._ratings = self._extract_ratings(profile)
            self._push(AccountInfo(username=self._username,
                                   ratings=dict(self._ratings)))

        # Drop finished threads so the list does not grow unbounded over many
        # games (mirrors _start_game_thread).
        self._threads = [t for t in self._threads if t.is_alive()]
        thread = threading.Thread(target=_worker, daemon=True,
                                  name="lichess-rating-refresh")
        self._threads.append(thread)
        thread.start()

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

    def _instant_first_move(self, board: Board, initial_fen: str,
                            moves: tuple[str, ...], bot_is_white: bool) -> Optional[str]:
        """Return an instant opening-book UCI move for our first move as White
        from the standard starting position, else None.

        See ``_OPENING_BOOK_WHITE_MOVE1`` for the rationale and the TESTED &
        FALSIFIED status (Experiment A: the abort beats even a 0ms-think move, so
        this is a toggle/diagnostic, not a workaround). The first book entry that
        is legal in the rebuilt board is returned; legality is checked by
        UCI-string membership, so a misconfigured/illegal
        entry is silently skipped and None is returned if none match.
        """
        if not bot_is_white or moves or initial_fen != STARTING_FEN:
            return None
        legal = {move_to_uci(m) for m in generate_legal_moves(board, board.active_color)}
        for book in _OPENING_BOOK_WHITE_MOVE1:
            if book in legal:
                return book
        return None

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

        # EXPERIMENT A (TESTED & FALSIFIED as a workaround; retained as a
        # toggle/diagnostic -- see _OPENING_BOOK_WHITE_MOVE1): instant
        # opening-book first move as White from the standard start position.
        # Skips the engine entirely so move 1 lands at ~POST-RTT. NB: the
        # opponent tested this (CTteTHAU) and the abort still beat an instant
        # move, so this does NOT reliably prevent the abort -- it is kept only
        # as a fast move-1 / diagnostic. Falls back to the engine otherwise
        # (and for every other move).
        book_uci = self._instant_first_move(board, initial_fen, moves, bot_is_white)
        if book_uci is not None:
            self._push(Status(f"Playing opening book ({book_uci})..."))
            uci = book_uci
        else:
            time_limit_ms = self._time_budget(state, bot_is_white)
            if len(moves) <= 1:
                # The bot's first move of the game: think briefly so the move
                # lands before the opponent can abort (Lichess disallows a
                # single-player abort once a move is on the board). min() keeps
                # the cap from raising an already-smaller late-game budget.
                time_limit_ms = min(time_limit_ms, FIRST_MOVE_BUDGET_MS)
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