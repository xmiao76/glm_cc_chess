"""Pygame-based chess GUI for the chess application.

Provides a visual chessboard with click-to-move interaction,
legal move highlighting, and game status display.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import time
import pygame
from src.board import Board, STARTING_FEN
from src.game import GameState
from src.moves import generate_legal_moves, move_to_algebraic, uci_to_move, Move
from src.engine import ChessEngine, choose_move
from src.lichess_controller import (
    LichessController, ChallengeReceived, ChallengeSent, ChallengeDeclined,
    GameStarted, GameUpdated, EngineMoved, GameFinished, Status, Error,
    AccountInfo,
)
from src.gui_textinput import TextInput, CAPTURE_VERSION
from src.clipboard_util import paste_from_clipboard

logger = logging.getLogger(__name__)


def _parse_int_env(name: str, default: int) -> int:
    """Read an integer env var, falling back to ``default`` on absence/parse error."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("ignoring invalid %s=%r (expected int)", name, raw)
        return default


def _parse_bool_env(name: str) -> bool:
    """Read a boolean env var (``1``/``true``/``yes``/``on``)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _resource_path(relative_path: str) -> str:
    """Return the absolute path to a bundled resource, works in dev and PyInstaller."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        base = sys._MEIPASS
    else:
        # Running from source
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


def _token_fingerprint(token: str) -> str:
    """Structural metadata of a token for the activity log -- NEVER the token.

    Length + per-character-class counts + whether it has the conventional
    ``lip_`` prefix. This lets the log distinguish a correctly-captured token
    (len 24, mixed case) from a mangled one (capitals dropped -> len 14,
    upper=0; or case-folded -> upper=0) WITHOUT ever revealing the secret: only
    the shape is logged, never the actual characters. A 24-char alphanumeric
    token has far too much entropy for these counts to narrow it down.
    """
    return (
        f"len={len(token)} "
        f"upper={sum(c.isupper() for c in token)} "
        f"lower={sum(c.islower() for c in token)} "
        f"digit={sum(c.isdigit() for c in token)} "
        f"under={token.count('_')} "
        f"other={sum(not (c.isalnum() or c == '_') for c in token)} "
        f"prefix_lip={token.startswith('lip_')}"
    )


def _build_identity() -> str:
    """One-line identity of the running binary for the activity log.

    Includes the capture-code version and, when frozen, the executable's file
    name + build time, so a copied log shows EXACTLY which build the user ran --
    ruling out a stale EXE when a 401 keeps recurring after a fix ships.
    """
    parts = [f"capture={CAPTURE_VERSION}"]
    if getattr(sys, "frozen", False):
        exe = sys.executable or ""
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(os.path.getmtime(exe)))
            parts.append(f"exe={os.path.basename(exe)} built={mtime}")
        except OSError:
            parts.append(f"exe={os.path.basename(exe)}")
    else:
        parts.append("source")
    return " ".join(parts)


# Constants
SQUARE_SIZE = 80
BOARD_SIZE = SQUARE_SIZE * 8
# The side panel is resizable: the window can be widened to enlarge the panel
# so the full activity-log text is visible (lines used to be truncated to 30
# chars and clipped by the fixed 220px panel). The board stays 640x640 at the
# top-left; only the width is flexible, and extra width flows into the panel.
MIN_PANEL_WIDTH = 260
DEFAULT_PANEL_WIDTH = 380
# Characters allowed in a Lichess BOT token. The lichess-org/api OpenAPI spec
# defines access tokens as ``^[A-Za-z0-9_]+$`` (alphanumeric + underscore only --
# NO hyphen; the older 40-char form and the newer ``lip_...`` form both match).
# Used by the in-GUI token field so a pasted token keeps only legal chars.
_LICHESS_TOKEN_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)
# Max token length the field will accept. The lichess-org/api spec says: "The
# length of tokens can be increased without notice. Make sure your application
# can handle at least 512 characters." A 128-char cap TRUNCATED longer tokens,
# so the controller sent a partial token -> Lichess returned HTTP 401 "No such
# token" -> every Connect showed "profile fetch failed" (the "Profile Fetch
# Error"). 1024 comfortably covers the spec's 512-char minimum with headroom
# for the "increased without notice" case.
_LICHESS_TOKEN_MAX_LEN = 1024
# Seconds with no first move before we proactively warn that the side to move
# first isn't playing. Lichess aborts a game after ~15-30s with no first move, so
# 20s gives a heads-up just before the abort.
NO_FIRST_MOVE_WARN_S = 20
# Lichess statuses that mean "the game didn't get played". ``aborted`` is a
# mid/start abort by a player or the no-first-move timeout; ``noStart`` means
# Lichess aborted it BEFORE it started (a creation-time conflict — e.g. a
# duplicate event-stream connection). Both render as "Game aborted" but have
# different causes, so the diagnostic logs the actual status.
ABORT_LIKE_STATUSES = ("aborted", "noStart")
# A 0-move game that ends this fast after GameStarted was NOT aborted by the
# ~15-30s no-first-move timeout — the gameFull arrived already over (an instant
# abort at creation, e.g. a duplicate event-stream connection conflict). Used
# only for the Black case: when we're Black we never start thinking (we wait on
# White), so engine_started can't tell instant-abort from a real timeout, but
# the elapsed time can (sub-second vs ~15-30s).
INSTANT_ABORT_THRESHOLD_S = 5
WINDOW_HEIGHT = BOARD_SIZE
FPS = 60

# Colors
LIGHT_SQUARE = (240, 217, 181)
DARK_SQUARE = (181, 136, 99)
HIGHLIGHT_COLOR = (186, 202, 68, 180)
SELECTED_COLOR = (246, 246, 105, 200)
LEGAL_MOVE_COLOR = (106, 176, 76, 120)
LAST_MOVE_COLOR = (155, 199, 0, 100)
CHECK_COLOR = (235, 97, 80, 160)
BG_COLOR = (48, 46, 43)
PANEL_COLOR = (39, 37, 34)
TEXT_COLOR = (200, 200, 200)
BUTTON_COLOR = (80, 120, 80)
BUTTON_HOVER_COLOR = (100, 150, 100)
BUTTON_TEXT_COLOR = (255, 255, 255)

# Unicode chess pieces mapped to piece codes
PIECE_SYMBOLS = {
    "wK": "\u2654", "wQ": "\u2655", "wR": "\u2656",
    "wB": "\u2657", "wN": "\u2658", "wP": "\u2659",
    "bK": "\u265A", "bQ": "\u265B", "bR": "\u265C",
    "bB": "\u265D", "bN": "\u265E", "bP": "\u265F",
}


class ChessGUI:
    """Pygame-based chess GUI."""

    def __init__(self) -> None:
        pygame.init()
        # Resizable window: the board is fixed at 640x640 in the top-left, and
        # the side panel absorbs any extra width so the user can enlarge it to
        # read the full (wrapped) panel text. The height is held at BOARD_SIZE
        # (see _handle_resize) so the board is always fully visible -- only the
        # panel width is flexible.
        self.panel_width = DEFAULT_PANEL_WIDTH
        self.window_width = BOARD_SIZE + self.panel_width
        self.screen = pygame.display.set_mode(
            (self.window_width, WINDOW_HEIGHT), pygame.RESIZABLE)
        pygame.display.set_caption("GLM CC Chess")
        self.clock = pygame.time.Clock()

        # Fonts — use bundled TTF to avoid SysFont crash on some Windows PCs
        symbol_font_path = _resource_path(os.path.join("fonts", "NotoSansSymbols2.ttf"))
        self.piece_font = pygame.font.Font(symbol_font_path, 58)
        self.small_font = pygame.font.Font(None, 22)
        self.medium_font = pygame.font.Font(None, 24)
        self.large_font = pygame.font.Font(None, 36)

        # Game state
        self.game = GameState()
        self.engine = ChessEngine(max_depth=4, time_limit=1.5)
        self.player_color = "w"  # Human plays white by default
        self.flipped = False  # Whether board is displayed from black's perspective
        self.selected_square: tuple[int, int] | None = None
        self.legal_moves_for_selected: list[Move] = []
        self.last_move: Move | None = None
        self.game_over = False
        self.game_result = ""
        self.is_engine_thinking = False
        self.move_history: list[str] = []
        self.captured_white: list[str] = []
        self.captured_black: list[str] = []
        self.promotion_pending: Move | None = None  # Move awaiting promotion choice
        self.promotion_moves: list[Move] = []  # All promotion moves for the pending square
        self.mode = "menu"  # "menu", "play", "engine_vs_engine", "lichess"
        self.engine_vs_engine_delay = 500  # ms between moves
        self.last_engine_move_time = 0

        # Lichess BOT integration state (only used in "lichess" mode)
        self.lichess_controller: LichessController | None = None
        self.lichess_game: dict | None = None  # current live/reviewed game
        self.pending_challenge = None  # ChallengeReceived awaiting user decision
        self.lichess_status = "Idle"
        self.review_mode = False
        self.review_index = 0
        # Live UI feedback so the user can see what the bot is doing while idle
        self.lichess_log: list[str] = []  # recent human-readable events
        self.lichess_last_event_ticks = 0  # pygame ticks of last received event
        self.lichess_last_move_text = ""  # e.g. "Move 12: e2e4"
        self.lichess_connected = False
        # Connected-account display ("the Lichess score"): account name + per-speed
        # ratings, pushed by the controller on connect and after each finished game.
        self.lichess_account_name = ""
        self.lichess_ratings: dict[str, int] = {}
        self.lichess_last_speed = ""  # speed of the current/last game (for the header)
        # Challenge initiation + auto-match UI state
        self.lichess_opponent = ""  # committed opponent username
        self.lichess_auto = False  # auto-match (challenge + accept) toggle
        self.lichess_rated = False  # casual by default; rated is opt-in (NOT a proven abort fix)
        self.lichess_last_rated: bool | None = None  # mode of our last outgoing challenge (for the abort headline)
        self.lichess_clock_limit_s = 300
        self.lichess_clock_increment_s = 3
        self.lichess_opponent_field: TextInput | None = None
        # Masked text field for entering the Lichess BOT token in the UI
        # (Connect button) instead of setting the env var beforehand. Only
        # created when entering Lichess mode.
        self.lichess_token_field: TextInput | None = None

        # Pre-render piece surfaces
        self.piece_surfaces: dict[str, pygame.Surface] = {}
        self._render_pieces()

    def _render_pieces(self) -> None:
        """Pre-render all chess piece symbols to surfaces."""
        for code, symbol in PIECE_SYMBOLS.items():
            if code[0] == "w":
                fill_color = (255, 255, 255)
                outline_color = (0, 0, 0)
            else:
                fill_color = (30, 30, 30)
                outline_color = (200, 200, 200)

            surface = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
            text_surf = self.piece_font.render(symbol, True, fill_color)
            outline_surf = self.piece_font.render(symbol, True, outline_color)
            # Center the piece on the square
            text_rect = text_surf.get_rect(center=(SQUARE_SIZE // 2, SQUARE_SIZE // 2))
            # Draw outline at 8 offsets for a clean border
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    surface.blit(outline_surf, (text_rect.x + dx, text_rect.y + dy))
            # Draw fill on top
            surface.blit(text_surf, text_rect)
            self.piece_surfaces[code] = surface

    def reset_game(self) -> None:
        """Reset the game to starting position."""
        self.game = GameState()
        self.selected_square = None
        self.legal_moves_for_selected = []
        self.last_move = None
        self.game_over = False
        self.game_result = ""
        self.is_engine_thinking = False
        self.move_history = []
        self.captured_white = []
        self.captured_black = []
        self.promotion_pending = None
        self.promotion_moves = []
        self.last_engine_move_time = pygame.time.get_ticks()

    # --- Lichess BOT integration -----------------------------------------

    def _get_lichess_token(self, config_path: str | None = None) -> str | None:
        """Read the Lichess BOT token from the env var, then a gitignored config.

        Order: ``LICHESS_BOT_TOKEN`` env var, then ``lichess/config.yml`` (or
        ``config_path`` if given, for testability). The placeholder value
        ``LICHESS_BOT_TOKEN`` (from the example file) is rejected so an unedited
        copy is never used as a real token. The token is never logged and never
        rendered unmasked: when an env/config token is found it is prefilled into
        the masked token field (``TextInput(mask=True)``, which renders only ``*``
        per char) for the session, and the UI's Connect button sets the env var
        from that field — see :meth:`_connect_with_token`.
        """
        token = os.environ.get("LICHESS_BOT_TOKEN")
        if token and token.strip():
            return token.strip()
        if config_path is None:
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            cfg_path = os.path.join(repo_root, "lichess", "config.yml")
        else:
            cfg_path = config_path
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        stripped = line.strip()
                        if stripped.startswith("token:"):
                            value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                            if value and "LICHESS_BOT_TOKEN" not in value:
                                return value
            except OSError as exc:
                logger.warning("could not read %s: %s", cfg_path, exc)
        return None

    @staticmethod
    def _fmt_clock(ms) -> str:
        """Format a Lichess clock (ms) as m:ss, or '--:--' if unknown."""
        if ms is None or ms < 0:
            return "--:--"
        secs = int(ms) // 1000
        return f"{secs // 60}:{secs % 60:02d}"

    def _lichess_speed_for_clock(self) -> str:
        """Best-guess Lichess speed for the configured clock (for the pre-game
        header, before any game sets ``lichess_last_speed``).

        Mirrors Lichess's own classification by estimated game duration
        (``limit + increment*40`` seconds): <30 ultraBullet, <180 bullet, <480
        blitz, <1500 rapid, else classical. So the default 5+3 (300+120=420s) is
        blitz, 10+0 is rapid, 3+2 is blitz, 1+0 is bullet.
        """
        limit = self.lichess_clock_limit_s or 0
        inc = self.lichess_clock_increment_s or 0
        if not limit:
            return "rapid"  # correspondence has no clock (we decline it anyway)
        est = limit + inc * 40
        if est < 30:
            return "ultraBullet"
        if est < 180:
            return "bullet"
        if est < 480:
            return "blitz"
        if est < 1500:
            return "rapid"
        return "classical"

    def _lichess_rating_text(self) -> str:
        """Compact "<speed> <rating>" for the header, or '' if no ratings yet.

        Prefers the rating for the speed actually being played (the one that
        refreshes after each game); falls back to the configured clock's speed,
        then to any available rating. Empty before connect / if the account has
        no games.
        """
        ratings = self.lichess_ratings or {}
        if not ratings:
            return ""
        speed = (self.lichess_last_speed
                 or self._lichess_speed_for_clock() or "")
        if speed and speed in ratings:
            return f"{speed} {ratings[speed]}"
        for s in ("rapid", "blitz", "classical", "bullet"):
            if s in ratings:
                return f"{s} {ratings[s]}"
        return ""

    def _lichess_result_text(self, status: str, winner) -> str:
        """Map a Lichess game-end status to a human-readable result."""
        if status in ABORT_LIKE_STATUSES:
            return "Game aborted"
        if status in ("draw", "stalemate"):
            return "Draw"
        if status in ("mate", "resign", "outoftime", "timeout", "cheat"):
            if self.lichess_game:
                bot_white = self.lichess_game.get("bot_is_white", True)
                bot_won = (winner == "white" and bot_white) or (winner == "black" and not bot_white)
                return "You won!" if bot_won else "You lost"
        return status.capitalize() if status else "Finished"

    def _log_lichess(self, message: str) -> None:
        """Append a human-readable event to the on-screen activity log (capped).

        Each line is prefixed with a wall-clock ``HH:MM:SS`` timestamp (matching
        the opponent lichess-bot's log format) so the two logs can be correlated
        and — crucially — the same-second "instant abort at creation" timing is
        visible. That timing is the evidence that distinguishes an instant abort
        (a duplicate-connection conflict) from the ~15-30s no-first-move timeout.
        """
        stamp = time.strftime("%H:%M:%S")
        self.lichess_log.append(f"{stamp} {message}")
        # Keep a generous rolling history so Copy Log captures the FULL sequence
        # of a game (challenge -> GameStarted -> ... -> abort diagnostic), not
        # just the last few lines — the challenge line is the evidence that we
        # sent exactly one challenge, and it must survive until the user copies.
        # The on-screen panel renders only the tail (see _draw_lichess_panel).
        if len(self.lichess_log) > 100:
            self.lichess_log = self.lichess_log[-100:]
        self.lichess_last_event_ticks = pygame.time.get_ticks()

    def _log_abort_diagnostic(self) -> None:
        """Log a tailored explanation of why the game was aborted/noStart.

        Uses the ACTUAL Lichess status — ``noStart`` (aborted BEFORE it started,
        a creation-time conflict) is a different cause from ``aborted`` (a
        player/no-first-move abort) — plus how many moves were played, which
        color we were, whether we ever started thinking, and how long the game
        lasted.
        """
        game = self.lichess_game or {}
        moves_played = len(game.get("moves", []))
        bot_is_white = game.get("bot_is_white")
        opp = game.get("opponent_name", "the opponent")
        engine_started = bool(game.get("engine_started"))
        status = str(game.get("status") or "aborted")
        started = game.get("started_ticks")
        dur = ""
        elapsed_s: float | None = None
        if started is not None:
            elapsed_s = max(0.0, (pygame.time.get_ticks() - started) / 1000.0)
            # Sub-second precision matters for the instant-abort-at-creation
            # case (the game aborted in the same second it started); >=1s rounds.
            if elapsed_s >= 1:
                dur = f" after {elapsed_s:.0f}s"
            else:
                dur = f" within {elapsed_s:.1f}s of start"
        # Concise factual headline FIRST — readable at a glance and in Copy Log
        # (the detailed cause analysis below is a wall of text the panel
        # truncates to 30 chars). Surfacing the rated/casual mode is the key
        # bit: game ENTGYOFG (2026-06-28) was RATED yet still aborted, which
        # falsifies the opponent's "casual is auto-aborted, rated works" claim.
        if bot_is_white is True:
            color_name = "White"
        elif bot_is_white is False:
            color_name = "Black"
        else:
            color_name = "?"
        mode_tag = ""
        if self.lichess_last_rated is True:
            mode_tag = " [rated]"
        elif self.lichess_last_rated is False:
            mode_tag = " [casual]"
        self._log_lichess(
            f"Abort: {moves_played} move(s), we were {color_name}{mode_tag}, "
            f"status '{status}'{dur}. Cause below.")
        if status == "noStart":
            # Lichess aborted the game BEFORE it started — a creation-time
            # conflict, NOT the ~15-30s no-first-move timeout. The game was
            # dead-on-arrival: neither side moved, regardless of color. Be HONEST:
            # name NO proven cause, demote the duplicate-stream candidate (both
            # sides single-instance in recent cycles), and lead with the live
            # unverified server-side candidates (same-IP / same-owner) + the
            # decisive checks.
            us = "White (we move first)" if bot_is_white else "Black (waiting on White)"
            self._log_lichess(
                f"Game never started{dur} — Lichess status 'noStart' (aborted "
                f"BEFORE it started, NOT a no-first-move timeout). We were {us} "
                f"but the gameFull arrived already over, so neither side moved. "
                f"This is a creation-time conflict. Candidate causes (NONE "
                f"proven): (1) a DUPLICATE event-stream connection on one account "
                f"— two bot instances both receive the gameStart (Lichess pushes "
                f"it to every live stream) and both connect to this game's stream, "
                f"and the conflict aborts it at creation, whether our one "
                f"challenge was received once or twice (the gameStart, not the "
                f"challenge, double-connects). This candidate is WEAKENED: recent "
                f"cycles had BOTH sides confirmed single-instance (singleton lock "
                f"+ one PID + one challenge id + single receipt/accept), so a "
                f"two-process duplicate is largely ruled out. (2) A Lichess-side "
                f"abort for two bots owned by the same person / on the same public "
                f"IP — an UNVERIFIED server-side rule (no documented policy) that "
                f"fits the recurring 0-move abort pattern. The 'Connected as "
                f"<name>' line count does NOT prove a duplicate (lichess-bot logs "
                f"'Connected' on each reconnect, so two 'Connected as {opp}' lines "
                f"can be ONE process). Decisive checks to actually prove the "
                f"cause: run `curl ifconfig.me` on EACH machine and compare the "
                f"public IPs (different -> same-IP ruled out, same-owner is the "
                f"cause; same -> use a cellular hotspot for a real different-IP "
                f"test, NOT a VPN — Lichess flags VPN IPs); challenge a "
                f"THIRD-PARTY bot (different owner) from the same machine — if it "
                f"plays, same-owner is the cause; run the two bots on DIFFERENT "
                f"networks/public IPs. Meanwhile, run only ONE bot process per "
                f"account (a second double-connects and aborts every game).")
            return
        if moves_played == 0:
            if bot_is_white:
                # We were White (we move first) yet nothing was played. The two
                # causes are distinct, so they get distinct messages.
                if engine_started:
                    # We were thinking when the game died — the gameFull was LIVE
                    # (we reached _maybe_move), so the game aborted during our
                    # think before the first move landed. Causes: a manual opponent
                    # abort, a duplicate-stream conflict (WEAKENED — both sides
                    # single-instance in recent cycles), OR a Lichess-side abort
                    # for same-owner/same-IP bots. Keep the "had started thinking"
                    # clause so this isn't mistaken for the already-over-on-connect
                    # case, and point at the decisive checks.
                    self._log_lichess(
                        f"Aborted{dur} with no moves played — we were White (we "
                        f"move first). We had started thinking, but the game was "
                        f"aborted before our first move landed — so the gameFull "
                        f"was LIVE and the abort happened mid-think. Causes: a "
                        f"manual opponent abort; a DUPLICATE event-stream "
                        f"connection on one account (two bot instances both "
                        f"receive the gameStart and both connect to this game's "
                        f"stream — WEAKENED, both sides were single-instance in "
                        f"recent cycles); or a Lichess-side abort for two bots "
                        f"owned by the same person / on the same public IP — an "
                        f"UNVERIFIED server-side rule. The 'Connected as <name>' "
                        f"line count does NOT prove a duplicate. Decisive checks: "
                        f"run `curl ifconfig.me` on EACH machine and compare the "
                        f"public IPs; challenge a THIRD-PARTY bot (different owner) "
                        f"from the same machine — if it plays, same-owner is the "
                        f"cause; run the two bots on DIFFERENT networks/public IPs. "
                        f"Meanwhile, run only ONE bot process per account (a "
                        f"second double-connects and aborts every game).")
                else:
                    # We never started thinking — the gameFull arrived already over
                    # (a creation/early abort, NOT the ~15-30s no-first-move timeout).
                    # Be HONEST: name NO proven cause, demote the duplicate-stream
                    # candidate (both sides single-instance in recent cycles), and
                    # lead with the live unverified server-side candidates (same-IP
                    # / same-owner) + the decisive checks. Our elapsed clock starts
                    # when WE connect, so "already over when we connected" does NOT
                    # by itself prove a creation-time abort.
                    self._log_lichess(
                        f"Aborted{dur} with no moves played — we were White (we "
                        f"move first). We never started thinking — the game was "
                        f"already over when we connected (the gameFull carried a "
                        f"'{status}' status). That timing — sub-second, 0 moves, "
                        f"gameFull already over — is a creation/early abort, NOT the "
                        f"~15-30s no-first-move timeout. KEY LIMITATION: our elapsed "
                        f"clock starts when WE connect, so 'already over when we "
                        f"connected' does NOT by itself prove the game was aborted "
                        f"at creation. Candidate causes (NONE proven): (1) a "
                        f"DUPLICATE event-stream connection on one account — two "
                        f"bot instances both receive the gameStart (Lichess pushes "
                        f"it to every live stream) and both connect to this game's "
                        f"stream, and Lichess aborts the conflict, whether our one "
                        f"challenge was received once or twice (the gameStart, not "
                        f"the challenge, double-connects). This candidate is "
                        f"WEAKENED: recent cycles had BOTH sides confirmed "
                        f"single-instance (singleton lock + one PID + one challenge "
                        f"id + single receipt/accept), so a two-process duplicate is "
                        f"largely ruled out. (2) A Lichess-side abort for two bots "
                        f"owned by the same person / on the same public IP — an "
                        f"UNVERIFIED server-side rule (no documented policy) that "
                        f"fits the recurring 0-move abort pattern across colors and "
                        f"orderings. The 'Connected as <name>' line count does NOT "
                        f"prove a duplicate (lichess-bot logs 'Connected' on each "
                        f"reconnect, so two 'Connected as {opp}' lines can be ONE "
                        f"process). Decisive checks to actually prove the cause: run "
                        f"`curl ifconfig.me` on EACH machine and compare the public "
                        f"IPs (different -> same-IP ruled out, same-owner is the "
                        f"cause; same -> use a cellular hotspot for a real "
                        f"different-IP test, NOT a VPN — Lichess flags VPN IPs); "
                        f"challenge a THIRD-PARTY bot (different owner) from the "
                        f"same machine — if it plays, same-owner is the cause; run "
                        f"the two bots on DIFFERENT networks/public IPs — if games "
                        f"then play, same-IP was a factor. Meanwhile, run only ONE "
                        f"bot process per account (a second double-connects and "
                        f"aborts every game).")
            else:
                # We were Black, waiting for White to move first. We never start
                # thinking while Black (not our turn until White moves), so
                # engine_started can't tell the causes apart — the elapsed time
                # can. Three regimes by elapsed:
                #  - sub-second: the gameFull arrived already over (instant abort
                #    at creation — a duplicate event-stream conflict).
                #  - a few seconds, but under the ~15-30s no-first-move timeout:
                #    the game was LIVE then aborted mid-way before White's first
                #    move — a conflict (one account connected to the game stream
                #    twice) or a manual Abort. NOT a timeout, NOT instant.
                #  - >= NO_FIRST_MOVE_WARN_S: White never moved -> the genuine
                #    no-first-move timeout (the stall warning would have fired).
                instant = elapsed_s is not None and elapsed_s < INSTANT_ABORT_THRESHOLD_S
                stalled = elapsed_s is not None and elapsed_s >= NO_FIRST_MOVE_WARN_S
                if instant:
                    # IMPORTANT: "already over when we connected" does NOT prove
                    # the game was aborted at creation. Our elapsed clock starts
                    # when WE connect, so a game that actually lived a few
                    # seconds (a mid-think abort while White was thinking) and
                    # that we connected to slightly late produces the IDENTICAL
                    # signature on our side — game LtnFaUxZ (2026-06-28) turned
                    # out to be exactly that (the opponent logged "Engine
                    # thinking" + status "started", then its move got "game
                    # already over"). So we must NOT assert "instant abort at
                    # creation" as fact; we name the limitation, demote the
                    # duplicate-stream candidate (both sides were single-instance
                    # in recent cycles), add the Lichess-side same-owner/same-IP
                    # candidate, and point at the decisive experiments.
                    self._log_lichess(
                        f"Aborted{dur} with no moves played — we were Black "
                        f"(waiting for White). The gameFull arrived already over "
                        f"when we connected (it carried a '{status}' status), so 0 "
                        f"moves were played. KEY LIMITATION: 'already over when we "
                        f"connected' does NOT prove the game was aborted at "
                        f"creation — our elapsed clock starts when WE connect, so a "
                        f"game that actually lived a few seconds (a mid-think abort "
                        f"while White was thinking) and that we connected to "
                        f"slightly late produces the IDENTICAL signature on our "
                        f"side; we cannot tell the two apart from our log alone "
                        f"(cross-check the opponent's log: did it show 'Engine "
                        f"thinking'/'started' before the abort?). Candidate causes "
                        f"(NONE proven): (1) a DUPLICATE event-stream connection on "
                        f"one account — two bot instances on one account both "
                        f"receive the gameStart and both connect to this game's "
                        f"stream, and Lichess aborts the conflict, whether our one "
                        f"challenge was received once or twice (the gameStart, not "
                        f"the challenge, double-connects). This candidate is now "
                        f"WEAKENED: recent cycles had BOTH sides confirmed "
                        f"single-instance (singleton lock + one PID + one challenge "
                        f"id + single receipt/accept), so a two-process duplicate is "
                        f"largely ruled out — though a silently-deduped second "
                        f"stream on the opponent's ({opp}) side remains possible. "
                        f"(2) A Lichess-side abort for two bots owned by the same "
                        f"person / on the same public IP — an UNVERIFIED "
                        f"server-side rule (no documented policy); it fits the "
                        f"recurring pattern of 0-move aborts across colors and "
                        f"orderings that survive every duplicate fix. The 'Connected "
                        f"as <name>' line count does NOT prove a duplicate "
                        f"(lichess-bot logs 'Connected' on each reconnect, so two "
                        f"lines can be one process). Decisive checks to actually "
                        f"prove the cause: run `curl ifconfig.me` on EACH machine "
                        f"and compare the public IPs (different -> same-IP ruled "
                        f"out, same-owner is the cause; same -> use a cellular "
                        f"hotspot for a real different-IP test, NOT a VPN — Lichess "
                        f"flags VPN IPs); challenge a THIRD-PARTY bot (not owned by "
                        f"you) from the same machine — if it plays normally, the "
                        f"cause is the same-owner pairing, not a code bug; run the "
                        f"two bots on DIFFERENT networks/public IPs — if games then "
                        f"play, same-IP was a factor. Meanwhile, run only ONE bot "
                        f"process per account (a second double-connects and aborts "
                        f"every game).")
                elif not stalled:
                    # Mid-think abort: the game was live, then aborted at a few
                    # seconds — before the timeout and after creation. Our single
                    # instance cannot cause this (gameStart is deduped, and abort
                    # is only the manual button), so it's a second connection on
                    # one account or a manual Abort — NOT "opponent not playing".
                    self._log_lichess(
                        f"Aborted{dur} with no moves played — we were Black, "
                        f"waiting for White. The game was LIVE but aborted before "
                        f"White's first move landed and well before the ~15-30s "
                        f"no-first-move timeout (the 20s stall warning never "
                        f"fired) — so this is NOT a timeout and NOT an "
                        f"instant-at-creation abort: the game was aborted mid-way. "
                        f"Two causes: (1) one account connected to the game stream "
                        f"TWO times — a second GUI window, lichess-bot for "
                        f"xmiao_glm alongside this GUI, or the opponent ({opp}) "
                        f"running two bot processes — and Lichess aborted the "
                        f"conflict; or (2) the Abort button was clicked. If you "
                        f"did not click Abort, check Task Manager for extra "
                        f"python/lichess-bot processes on EITHER account (one "
                        f"connection per account; a second double-connects the "
                        f"game stream and aborts every game).")
                else:
                    self._log_lichess(
                        f"Aborted{dur} with no moves played — White ({opp}) never "
                        "made the first move. Lichess aborts a game automatically "
                        "if no first move is made within ~15-30s, so the opponent "
                        "bot is not playing: it is not running, not upgraded to a "
                        "Bot account, or its engine failed to start. Check that "
                        "bot.")
        else:
            self._log_lichess(
                f"Aborted{dur} — a player aborted the game mid-way "
                f"({moves_played} move(s) played).")

    def _warn_if_opponent_stalled(self) -> None:
        """Proactively warn when the side to move first hasn't, before the abort.

        Called once per frame from the main loop. If the game is live, no move has
        been played, and NO_FIRST_MOVE_WARN_S have elapsed, log one warning so the
        activity log shows the stall *before* the abort lands (the abort message
        alone arrives too late to be actionable).
        """
        game = self.lichess_game
        if not game or game.get("over"):
            return
        if game.get("no_first_move_warned"):
            return
        if game.get("moves"):  # at least one move — game is progressing
            return
        started = game.get("started_ticks")
        if not started:
            return
        elapsed = (pygame.time.get_ticks() - started) / 1000.0
        if elapsed < NO_FIRST_MOVE_WARN_S:
            return
        game["no_first_move_warned"] = True
        bot_is_white = game.get("bot_is_white")
        opp = game.get("opponent_name", "the opponent")
        if bot_is_white:
            self._log_lichess(
                f"No first move after {elapsed:.0f}s — we are White and haven't "
                "moved. Our engine may be stuck; the game will abort soon if no "
                "move is made.")
        else:
            self._log_lichess(
                f"No first move after {elapsed:.0f}s — White ({opp}) hasn't "
                "moved. The opponent bot isn't playing; the game will abort "
                "soon (~15-30s no-first-move rule) if White doesn't move.")

    @staticmethod
    def _wrap_text(text: str, max_chars: int) -> list[str]:
        """Greedy word-wrap into lines of at most ``max_chars`` characters."""
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            if len(current) + len(word) + (1 if current else 0) <= max_chars:
                current = f"{current} {word}" if current else word
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    @staticmethod
    def _wrap_text_px(text: str, font: pygame.font.Font, max_px: int) -> list[str]:
        """Greedy word-wrap so each line's pixel width is ``<= max_px``.

        Unlike :meth:`_wrap_text` (char-count based), this measures with the
        actual font, so it wraps correctly at any panel width -- the activity
        log uses it so widening the window reveals the whole message that the
        fixed 220px panel used to clip. A single word wider than ``max_px``
        (a long URL or token fingerprint) is hard-broken char by char so it
        still wraps instead of overflowing the panel.
        """
        words = (text or "").split()
        lines: list[str] = []
        cur = ""
        for word in words:
            candidate = f"{cur} {word}" if cur else word
            if font.size(candidate)[0] <= max_px:
                cur = candidate
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if font.size(word)[0] <= max_px:
                cur = word
            else:
                chunk = ""
                for ch in word:
                    if font.size(chunk + ch)[0] <= max_px:
                        chunk += ch
                    else:
                        if chunk:
                            lines.append(chunk)
                        chunk = ch
                cur = chunk
        if cur:
            lines.append(cur)
        return lines or [""]

    def _start_lichess(self) -> None:
        """Enter Lichess mode: reset state, build the input fields, connect if a
        token is already known.

        A token already in the env var / config file is used immediately
        (backward-compatible auto-connect). Otherwise the masked token input is
        shown and the user enters it in the UI and clicks Connect — see
        :meth:`_connect_with_token`.
        """
        # Stop any controller still running before creating a new one. ``stop()``
        # only sets a flag (the daemon threads/HTTP connections linger a few
        # seconds), so without this, re-entering Lichess mode would leave the OLD
        # event-stream thread alive alongside the NEW one — a DUPLICATE
        # event-stream connection for this account, which is itself a cause of
        # the instant-at-creation abort (two game-stream connections conflict).
        self._stop_lichess_if_running()
        self.reset_game()
        self.lichess_game = None
        self.pending_challenge = None
        self.review_mode = False
        self.review_index = 0
        self.lichess_log = []
        self.lichess_last_move_text = ""
        self.lichess_connected = False
        self.lichess_account_name = ""
        self.lichess_ratings = {}
        self.lichess_last_speed = ""
        self.lichess_last_event_ticks = pygame.time.get_ticks()
        self.mode = "lichess"
        # Log which binary build this is so a copied activity log can confirm the
        # user is running the latest EXE (not a stale copy) when a 401 recurs.
        self._log_lichess(f"Build: {_build_identity()}")
        # Auto-match settings from env (GUI can override at runtime).
        self.lichess_opponent = os.environ.get("LICHESS_OPPONENT", "").strip()
        self.lichess_auto = _parse_bool_env("LICHESS_AUTO_MATCH")
        self.lichess_clock_limit_s = _parse_int_env("LICHESS_CLOCK_LIMIT", 300)
        self.lichess_clock_increment_s = _parse_int_env("LICHESS_CLOCK_INCREMENT", 3)
        # rated (True) vs casual (False, default). Casual preserves prior
        # behavior; LICHESS_RATED opts into rated. The mode is logged on every
        # challenge so the user can confirm what we send (the opponent asked).
        # NOTE: whether rated *fixes* the instant-abort is UNVERIFIED — casual is
        # a supported bot mode — so this is a knob + a logged signal, not a fix.
        self.lichess_rated = _parse_bool_env("LICHESS_RATED")
        self.lichess_opponent_field = TextInput(
            self.small_font, (BOARD_SIZE + 10, 148, self.panel_width - 20, 24))
        self.lichess_opponent_field.set(self.lichess_opponent)
        # Masked token field — entered in the UI when no token is preconfigured.
        self.lichess_token_field = TextInput(
            self.small_font, (BOARD_SIZE + 10, 140, self.panel_width - 20, 24),
            max_len=_LICHESS_TOKEN_MAX_LEN,
            valid_chars=_LICHESS_TOKEN_CHARS,
            mask=True,
        )
        # Keep the fields in sync with the current panel width (matters if the
        # window was resized before entering Lichess mode).
        self._layout_lichess_fields()
        token = self._get_lichess_token()
        if token:
            # Prefill the masked field so the user sees a token is set, then
            # connect immediately (preserves the pre-UI env-var workflow).
            self.lichess_token_field.set(token)
            self._connect_with_token()
        else:
            self.lichess_controller = None
            self.lichess_status = "No token. Enter it below and click Connect."
            self._log_lichess("No token found — enter it in the box and click Connect.")

    def _connect_with_token(self) -> None:
        """Connect to Lichess using the token typed in the UI's token field.

        Sets the ``LICHESS_BOT_TOKEN`` environment variable (so the rest of the
        codebase, which reads that env var, sees it) and starts the controller.
        The token is a secret: it is never logged, never written to a file, and
        the field renders it masked. On an empty field the user is warned and no
        connection is attempted.
        """
        if self.lichess_token_field is None:
            return
        token = self.lichess_token_field.value()
        if not token:
            self.lichess_status = "Enter a Lichess bot token to connect."
            self._log_lichess("Enter a Lichess bot token to connect.")
            return
        # The UI input is the source of truth for this session — export it to the
        # env var so any code path that reads LICHESS_BOT_TOKEN (e.g. a later
        # _start_lichess re-entry) sees the same token. Never log it.
        # Structural fingerprint (NEVER the token itself): logs the token's SHAPE
        # (length + char-class counts + lip_ prefix) so the activity log shows
        # whether the field captured the token whole or mangled it -- a mangled
        # token is what produces HTTP 401 "No such token". The secret is never
        # logged.
        self._log_lichess(f"Token fingerprint: {_token_fingerprint(token)}")
        os.environ["LICHESS_BOT_TOKEN"] = token
        self.lichess_status = "Connecting..."
        self._log_lichess("Connecting to Lichess...")
        self._build_and_start_lichess_controller(token)

    def _build_and_start_lichess_controller(self, token: str) -> None:
        """Create and start the LichessController with the current GUI settings.

        Shared by the env-var auto-connect path and the UI Connect button. Stops
        any previously running controller first so reconnecting never leaves two
        event-stream threads alive on this account (a duplicate connection is
        itself an instant-abort cause).
        """
        self._stop_lichess_if_running()
        opponents = (self.lichess_opponent,) if self.lichess_opponent else ()
        self.lichess_controller = LichessController(
            token,
            opponents=opponents,
            auto_accept=self.lichess_auto,
            auto_challenge=self.lichess_auto,
            clock_limit_s=self.lichess_clock_limit_s,
            clock_increment_s=self.lichess_clock_increment_s,
            rated=self.lichess_rated,
        )
        self.lichess_controller.start()

    def _stop_lichess_if_running(self) -> None:
        if self.lichess_controller is not None:
            self.lichess_controller.stop()
            self.lichess_controller = None

    def _handle_resize(self, w: int, h: int) -> None:
        """Grow the panel when the window is resized.

        The board is fixed at 640x640 in the top-left; the height is held at
        ``BOARD_SIZE`` so the board is always fully visible. Only the width is
        flexible, and extra width flows into the side panel so the user can
        enlarge it to read the full (wrapped) panel text. Called from the
        ``VIDEORESIZE`` handler in :meth:`run`.
        """
        new_w = max(BOARD_SIZE + MIN_PANEL_WIDTH, w)
        if new_w == self.window_width and h == WINDOW_HEIGHT:
            return
        self.window_width = new_w
        self.panel_width = new_w - BOARD_SIZE
        self.screen = pygame.display.set_mode(
            (self.window_width, WINDOW_HEIGHT), pygame.RESIZABLE)
        self._layout_lichess_fields()

    def _layout_lichess_fields(self) -> None:
        """Reposition the Lichess text fields to the current panel width.

        The fields are created in :meth:`_start_lichess` at the current width;
        this keeps them tracking the panel when the window is resized while
        Lichess mode is active. No-op before the fields exist.
        """
        fw = max(120, self.panel_width - 20)
        fx = BOARD_SIZE + 10
        for field in (self.lichess_opponent_field, self.lichess_token_field):
            if field is not None:
                field.rect.x = fx
                field.rect.w = fw

    def _drain_lichess_events(self) -> None:
        """Move queued controller events into GUI state (main thread only)."""
        if self.lichess_controller is None:
            return
        while True:
            try:
                event = self.lichess_controller.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_lichess_event(event)
        # Proactively flag a no-first-move stall before the abort lands (the
        # abort message alone arrives too late to be actionable).
        self._warn_if_opponent_stalled()

    def _handle_lichess_event(self, event) -> None:
        # Any event proves the stream is alive — record it for the indicator.
        self.lichess_last_event_ticks = pygame.time.get_ticks()
        if isinstance(event, ChallengeReceived):
            self.pending_challenge = event
            self.lichess_status = f"Challenge from {event.opponent} ({event.speed})"
            self._log_lichess(f"Challenge: {event.opponent} ({event.speed})")
            if self.lichess_opponent_field is not None:
                self.lichess_opponent_field.active = False
        elif isinstance(event, ChallengeSent):
            self.lichess_status = f"Challenged {event.opponent} — waiting for accept"
            # Remember the rated/casual mode of our last outgoing challenge so the
            # GameStarted line and the abort headline can show it (the mode is the
            # crux of the abort debate — game ENTGYOFG was rated yet aborted).
            self.lichess_last_rated = bool(event.rated)
            # Include Lichess's challenge id — proof we sent exactly ONE challenge
            # (one unique id logged once). If the opponent then logs receiving it
            # twice, the duplication is on THEIR side (two event streams), not ours.
            cid = f", id={event.challenge_id}" if event.challenge_id else ""
            # Log the rated/casual mode explicitly — the opponent asked us to
            # confirm which we send, and the mode is relevant to the abort
            # investigation (casual vs rated is one live candidate, UNVERIFIED).
            mode = "rated" if event.rated else "casual"
            # Log the speed Lichess actually assigned. If we asked for a clock
            # but Lichess reports "correspondence", the clock params didn't land
            # (Lichess reads them from the POST body, not the query string) —
            # warn loudly so it's not silently a no-clock game.
            if event.speed == "correspondence":
                self._log_lichess(
                    f"Challenged {event.opponent} ({mode}){cid} — WARNING: Lichess "
                    "made this a correspondence (no-clock) game; the clock was not "
                    "received. A streaming bot cannot safely play correspondence.")
            elif event.speed and event.clock:
                self._log_lichess(
                    f"Challenged {event.opponent} ({event.speed} {event.clock}, "
                    f"{mode}{cid})")
            elif event.speed:
                self._log_lichess(
                    f"Challenged {event.opponent} ({event.speed}, {mode}{cid})")
            else:
                self._log_lichess(f"Challenged {event.opponent} ({mode}{cid})")
        elif isinstance(event, ChallengeDeclined):
            # Our outgoing challenge was declined — make it visible (the prior
            # build silently dropped this and the log just stopped at
            # "Challenged ..."). If the opponent declined because we sent casual,
            # point the user at the LICHESS_RATED knob (the opponent requires
            # rated; that requirement is THEIR rule, not proof of an abort cause).
            reason = f" (reason: {event.reason})" if event.reason else ""
            self.lichess_status = f"Challenge to {event.opponent} declined{reason}"
            self._log_lichess(
                f"Challenge to {event.opponent} was DECLINED{reason}")
            if event.reason and "casual" in event.reason.lower():
                self._log_lichess(
                    f"{event.opponent} declined a CASUAL challenge — set "
                    "LICHESS_RATED=1 and retry to send a rated challenge.")
        elif isinstance(event, GameStarted):
            self.lichess_game = {
                "game_id": event.game_id,
                "bot_is_white": event.bot_is_white,
                "opponent_name": event.opponent_name,
                "initial_fen": event.initial_fen,
                "moves": list(event.moves),
                "wtime": event.wtime,
                "btime": event.btime,
                "status": "started",
                "winner": None,
                "over": False,
                # ms-since-pygame-init when the game started, used to detect a
                # no-first-move stall and time the abort-diagnostic message.
                "started_ticks": pygame.time.get_ticks(),
                "no_first_move_warned": False,
                # True once we begin thinking for this game ("Engine thinking..."
                # Status or an EngineMoved). Lets _log_abort_diagnostic tell an
                # instant-at-creation abort apart from an abort during our think.
                "engine_started": False,
            }
            self.pending_challenge = None
            self.flipped = not event.bot_is_white  # show the bot's perspective
            self.review_mode = False
            if self.lichess_opponent_field is not None:
                self.lichess_opponent_field.active = False
            self.review_index = 0
            self.lichess_connected = True
            self.lichess_last_speed = event.speed  # header shows this speed's rating
            self.lichess_last_move_text = ""
            self.lichess_status = f"Playing {event.opponent_name}"
            color = "White" if event.bot_is_white else "Black"
            opp_color = "Black" if event.bot_is_white else "White"
            mode_tag = ""
            if self.lichess_last_rated is True:
                mode_tag = "; rated"
            elif self.lichess_last_rated is False:
                mode_tag = "; casual"
            self._log_lichess(
                f"Game started vs {event.opponent_name} "
                f"(you are {color}; {event.opponent_name} is {opp_color}{mode_tag}; "
                f"id={event.game_id})")
            if not event.bot_is_white:
                # We're Black — we cannot move until the opponent (White) does.
                # Logged so an abort here is clearly the opponent's doing, not a
                # freeze on our side.
                self._log_lichess(f"Waiting for {event.opponent_name} (White) to move first")
            self._set_lichess_position(self.lichess_game["moves"])
        elif isinstance(event, GameUpdated):
            if self.lichess_game and self.lichess_game["game_id"] == event.game_id:
                self.lichess_game["moves"] = list(event.moves)
                self.lichess_game["wtime"] = event.wtime
                self.lichess_game["btime"] = event.btime
                self.lichess_game["status"] = event.status
                self.lichess_game["winner"] = event.winner
                over = self._controller_is_over(event.status)
                self.lichess_game["over"] = over
                if over:
                    self.lichess_status = self._lichess_result_text(event.status, event.winner)
                    self._log_lichess(f"Game over: {self.lichess_status}")
                    if event.status in ABORT_LIKE_STATUSES:
                        self._log_abort_diagnostic()
                    self._enter_review()
                else:
                    self.lichess_status = f"Playing {self.lichess_game['opponent_name']}"
                    moves = self.lichess_game["moves"]
                    if moves:
                        self.lichess_last_move_text = f"Move {len(moves)}: {moves[-1]}"
                    self._set_lichess_position(self.lichess_game["moves"])
        elif isinstance(event, EngineMoved):
            if self.lichess_game and self.lichess_game["game_id"] == event.game_id \
                    and not self.lichess_game["over"]:
                # Optimistically show the engine's move; the next GameUpdated
                # reconciles from the authoritative server move list.
                self.lichess_game["moves"].append(event.uci)
                self.lichess_game["engine_started"] = True
                self._set_lichess_position(self.lichess_game["moves"])
                self.lichess_status = f"Engine played {event.uci}"
                self.lichess_last_move_text = f"Move {len(self.lichess_game['moves'])}: {event.uci}"
                # Logged so the activity log proves WE moved (an abort after this
                # is the opponent's doing, not ours).
                self._log_lichess(f"Engine played {event.uci}")
        elif isinstance(event, GameFinished):
            if self.lichess_game and self.lichess_game["game_id"] == event.game_id:
                # A second GameFinished can arrive for the same game (a controller
                # double-push, or a GameUpdated that finalized it followed by a
                # GameFinished). Reconcile the final state either way, but log
                # "Game over" + run the abort diagnostic exactly ONCE (game
                # ENTGYOFG, 2026-06-28 logged both twice).
                already_over = bool(self.lichess_game.get("over"))
                self.lichess_game["moves"] = list(event.moves)
                self.lichess_game["status"] = event.status
                self.lichess_game["winner"] = event.winner
                self.lichess_game["over"] = True
                self.lichess_status = self._lichess_result_text(event.status, event.winner)
                if not already_over:
                    self._log_lichess(f"Game over: {self.lichess_status}")
                    if event.status in ABORT_LIKE_STATUSES:
                        self._log_abort_diagnostic()
                self._enter_review()
            elif self.lichess_game is None:
                self.lichess_status = f"Game {event.game_id} finished"
                self._log_lichess(f"Game {event.game_id} finished")
        elif isinstance(event, AccountInfo):
            # The connected account's name + per-speed ratings ("the Lichess
            # score"). Pushed on connect and after each finished game, so the
            # header shows the account name and a rating that refreshes in real
            # time as games complete. Marks us connected (belt-and-suspenders
            # alongside the "Connected as ..." Status).
            prev_ratings = self.lichess_ratings
            self.lichess_account_name = event.username
            self.lichess_ratings = dict(event.ratings)
            self.lichess_connected = True
            # Log a rating CHANGE after a game (not the initial connect), so the
            # activity log shows the score updating in real time.
            if prev_ratings and event.ratings:
                changed = [(s, prev_ratings.get(s), event.ratings.get(s))
                           for s in event.ratings
                           if s in prev_ratings and prev_ratings.get(s) != event.ratings.get(s)]
                if changed:
                    parts = [f"{s}: {old}→{new}" for s, old, new in changed]
                    self._log_lichess("Rating update: " + ", ".join(parts))
        elif isinstance(event, Status):
            # "Connected as ..." marks the connection; "Engine thinking..." (a
            # real think) and "Playing opening book ..." (an instant book move 1)
            # mark the start of each of our moves. All are logged so the activity
            # log proves we were working — e.g. an abort that arrives while one of
            # those is the last entry (with no "Engine played" after it) is an
            # opponent-side abort during our move, not a freeze.
            if event.message.startswith("Connected as"):
                self.lichess_connected = True
                self.lichess_status = f"{event.message} — waiting for challenges"
            else:
                self.lichess_status = event.message
            # "Engine thinking..." (a real think) and "Playing opening book ..."
            # (an instant book move 1 — Experiment A, no engine think) both prove
            # the game thread reached _maybe_move — i.e. the gameFull was LIVE
            # (not already aborted). Its absence is the signature of an instant
            # abort at game creation.
            if self.lichess_game and event.message.startswith(
                    ("Engine thinking", "Playing opening book")):
                self.lichess_game["engine_started"] = True
            self._log_lichess(event.message)
        elif isinstance(event, Error):
            self.lichess_status = f"Error: {event.message}"
            self._log_lichess(f"Error: {event.message}")
            logger.warning("lichess error: %s", event.message)

    @staticmethod
    def _controller_is_over(status: str) -> bool:
        return LichessController.is_game_over_status(status)

    def _enter_review(self) -> None:
        """Switch to review mode at the end of the live game."""
        self.review_mode = True
        self.review_index = len(self.lichess_game["moves"]) if self.lichess_game else 0
        self._set_lichess_position(
            self.lichess_game["moves"][:self.review_index] if self.lichess_game else []
        )

    def _set_lichess_position(self, moves_to_apply: list[str]) -> None:
        """Rebuild ``self.game`` from the Lichess initial FEN + a UCI move list.

        Full rebuild each call (matches the controller) — robust to missed
        events and to optimistic EngineMoved appends. Also refreshes
        move_history, last_move, and captured pieces for the panel/board.
        """
        if self.lichess_game is None:
            return
        game = GameState(Board.from_fen(self.lichess_game["initial_fen"]))
        history: list[str] = []
        last_move: Move | None = None
        captured_white: list[str] = []
        captured_black: list[str] = []
        for uci in moves_to_apply:
            move = uci_to_move(uci)
            from_r, from_c, to_r, to_c, _ = move
            notation = move_to_algebraic(game.board, move)
            moving = game.board.get_piece(from_r, from_c)
            captured = game.board.get_piece(to_r, to_c)
            if moving is not None and moving[1] == "P" and from_c != to_c and captured is None:
                ep = game.board.get_piece(from_r, to_c)
                if ep is not None:
                    captured = ep
            game.make_move(move)
            history.append(notation)
            last_move = move
            if captured is not None:
                (captured_white if captured[0] == "w" else captured_black).append(captured)
        self.game = game
        self.move_history = history
        self.last_move = last_move
        self.captured_white = captured_white
        self.captured_black = captured_black
        self.selected_square = None
        self.legal_moves_for_selected = []

    # --- Lichess button actions (called from the main thread) ------------

    def _accept_challenge(self) -> None:
        if self.pending_challenge and self.lichess_controller:
            self.lichess_controller.accept_challenge(self.pending_challenge.challenge_id)
            self.lichess_status = "Challenge accepted — waiting for game..."
            self._log_lichess(f"Accepted challenge from {self.pending_challenge.opponent}")
            self.pending_challenge = None

    def _decline_challenge(self) -> None:
        if self.pending_challenge and self.lichess_controller:
            self.lichess_controller.decline_challenge(self.pending_challenge.challenge_id)
            self.lichess_status = "Challenge declined"
            self._log_lichess(f"Declined challenge from {self.pending_challenge.opponent}")
            self.pending_challenge = None

    def _resign_lichess(self) -> None:
        if self.lichess_game and self.lichess_controller and not self.lichess_game["over"]:
            self.lichess_controller.resign(self.lichess_game["game_id"])

    def _abort_lichess(self) -> None:
        if self.lichess_game and self.lichess_controller and not self.lichess_game["over"]:
            # Log the click so a manual abort is visible in the activity log —
            # otherwise it's indistinguishable from a server-side conflict abort
            # (the controller's abort() pushes no Status), and the abort
            # diagnostic would misattribute it to a duplicate connection.
            gid = self.lichess_game["game_id"]
            self._log_lichess(f"Abort requested for game {gid} (manual button click)")
            self.lichess_controller.abort(gid)

    def _commit_opponent(self) -> None:
        """Sync the text field into GUI + controller opponent state."""
        if self.lichess_opponent_field is not None:
            self.lichess_opponent = self.lichess_opponent_field.value()
        if self.lichess_controller is not None:
            opponents = (self.lichess_opponent,) if self.lichess_opponent else ()
            self.lichess_controller.set_opponents(opponents)

    def _challenge_opponent(self) -> None:
        """Manually challenge the username in the opponent field (one-shot)."""
        self._commit_opponent()
        if not self.lichess_opponent:
            self.lichess_status = "Enter an opponent username first"
            self._log_lichess("Enter an opponent username first")
            return
        if self.lichess_controller is None:
            self.lichess_status = "Not connected — set LICHESS_BOT_TOKEN and retry"
            self._log_lichess("Not connected; cannot challenge yet")
            return
        self.lichess_controller.challenge(self.lichess_opponent)
        self.lichess_status = f"Challenged {self.lichess_opponent}..."
        # The "Challenged <opp>" log line comes from the ChallengeSent event
        # (drained from the controller queue in _handle_lichess_event), which is
        # the single source — so we do NOT log here, to avoid a duplicate line.

    def _toggle_lichess_auto(self) -> None:
        """Toggle auto-match: auto-accept peers + periodic auto-challenge."""
        self.lichess_auto = not self.lichess_auto
        self._commit_opponent()
        if self.lichess_controller is not None:
            self.lichess_controller.set_auto(self.lichess_auto)
        state = "ON" if self.lichess_auto else "OFF"
        self.lichess_status = f"Auto-match {state}"
        self._log_lichess(f"Auto-match {state}")

    def _toggle_lichess_rated(self) -> None:
        """Toggle rated/casual mode for the NEXT challenge.

        Default is casual (a supported bot mode). Rated is opt-in because the
        opponent (xmiao_ds) declines casual challenges — NOT because rated is a
        proven fix for the creation-abort (see CLAUDE.md). The next manual
        challenge or auto-challenge sends the new mode.
        """
        self.lichess_rated = not self.lichess_rated
        if self.lichess_controller is not None:
            self.lichess_controller.set_rated(self.lichess_rated)
        mode = "rated" if self.lichess_rated else "casual"
        self.lichess_status = f"Challenges: {mode}"
        self._log_lichess(f"Challenge mode: {mode}")

    def _upgrade_lichess_bot(self) -> None:
        """Upgrade the linked Lichess account to a Bot account (irreversible).

        The bot-only game stream endpoint 400s for a normal account; this calls
        the controller's one-shot upgrade, which re-fetches the profile so the
        GUI can resume normal operation without a restart.
        """
        if self.lichess_controller is None:
            self.lichess_status = "Not connected — cannot upgrade"
            self._log_lichess("Not connected; cannot upgrade")
            return
        self.lichess_status = "Upgrading to Bot account..."
        self._log_lichess("Upgrading to Bot account (irreversible)...")
        self.lichess_controller.upgrade_account()

    def _copy_lichess_log(self) -> None:
        """Copy the full activity log to the clipboard for troubleshooting.

        The on-screen log is truncated to 30 chars/line and shows only the last
        6 entries; this copies the complete source list so the output is useful
        when reporting an error. Falls back to writing a file if no clipboard
        backend is available.
        """
        from src.clipboard_util import copy_to_clipboard

        username = self.lichess_controller.username if self.lichess_controller else ""
        header = f"@{username} — {self.lichess_status}" if username else self.lichess_status
        body = list(self.lichess_log) if self.lichess_log else ["(no activity yet)"]
        text = "\n".join([header, *body])

        if copy_to_clipboard(text):
            self.lichess_status = "Activity log copied to clipboard"
            self._log_lichess("Copied activity log to clipboard")
            return
        # Clipboard unavailable — write a file the user can open and share.
        try:
            path = os.path.abspath("lichess_activity_log.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.lichess_status = f"Log saved to {path}"
            self._log_lichess(f"Saved log to {path}")
        except OSError as exc:
            self.lichess_status = f"Could not copy log: {exc}"
            self._log_lichess(f"Could not copy log: {exc}")

    def _handle_lichess_field_click(self, pos: tuple[int, int]) -> None:
        """Focus the visible text field on click-in; defocus on click-out.

        Two fields, shown at different times: the masked **token** field is
        shown only while not connected (the connect form); the **opponent**
        field is shown once connected with no active game and no pending incoming
        challenge. Reviewing a finished game and typing a new opponent to
        challenge again are independent actions, so review mode must not lock
        the opponent field (otherwise, after a game ends, the only way to
        challenge again was to leave Lichess via Menu).
        """
        # Token field — only interactive before the connection is established.
        if self.lichess_token_field is not None:
            if not self.lichess_connected:
                self.lichess_token_field.handle_click(pos)
            else:
                self.lichess_token_field.active = False
        if self.lichess_opponent_field is None:
            return
        field_open = (self.lichess_connected
                      and (self.lichess_game is None or self.lichess_game.get("over"))
                      and self.pending_challenge is None)
        if field_open:
            self.lichess_opponent_field.handle_click(pos)
        else:
            self.lichess_opponent_field.active = False

    def _handle_lichess_keydown(self, event: pygame.event.Event) -> bool:
        """Process a KEYDOWN in Lichess mode. Returns True if consumed.

        - Ctrl+V pastes the clipboard into the focused field (token or
          opponent); the field filters to its valid chars, so a trailing
          newline/space from copying does not corrupt the token.
        - Enter in the token field connects (so the user can type the token and
          press Enter instead of clicking Connect).
        - Otherwise the event is delegated to the focused field's ``handle_key``,
          which consumes keys while focused so global shortcuts (e.g. ``N`` for
          new game) do not fire while the user is typing.
        """
        if getattr(event, "mod", 0) & pygame.KMOD_CTRL and event.key == pygame.K_v:
            clip = paste_from_clipboard()
            if clip:
                if self.lichess_token_field is not None and self.lichess_token_field.active:
                    self.lichess_token_field.insert_text(clip)
                elif self.lichess_opponent_field is not None and self.lichess_opponent_field.active:
                    self.lichess_opponent_field.insert_text(clip)
            return True
        # Enter on the token field connects — do this BEFORE delegating to
        # handle_key, which would defocus the field on Enter.
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            if self.lichess_token_field is not None and self.lichess_token_field.active:
                self._connect_with_token()
                return True
        if self.lichess_token_field is not None and self.lichess_token_field.active:
            return self.lichess_token_field.handle_key(event)
        if self.lichess_opponent_field is not None and self.lichess_opponent_field.active:
            return self.lichess_opponent_field.handle_key(event)
        return False

    def _handle_lichess_textinput(self, event: pygame.event.Event) -> None:
        """Route a pygame TEXTINPUT event to the focused Lichess field.

        ``TEXTINPUT.text`` is the OS-composed text -- IME- and shift-correct --
        so this is the path that captures characters an IME delivers with an
        empty ``KEYDOWN.unicode``. In particular it maps the underscore key to
        ``_`` (not the ``-`` that ``pygame.key.name`` yields), fixing the bug
        where a ``lip_...`` token was mangled to ``lip...`` -> Lichess HTTP 401
        "No such token" -> "profile fetch failed". :meth:`insert_text` filters
        to the field's valid chars, so a stray char still won't corrupt the
        token.

        Skips a TEXTINPUT whose character ``handle_key`` already inserted from
        ``KEYDOWN.unicode`` (the non-IME path) so the same keypress is not
        inserted twice.
        """
        text = getattr(event, "text", "") or ""
        if not text:
            return
        if self.lichess_token_field is not None and self.lichess_token_field.active:
            field = self.lichess_token_field
        elif (self.lichess_opponent_field is not None
              and self.lichess_opponent_field.active):
            field = self.lichess_opponent_field
        else:
            return
        if getattr(field, "_keydown_handled_char", False):
            # KEYDOWN already inserted this char (non-IME path) -- don't double.
            field._keydown_handled_char = False
            return
        field.insert_text(text)

    def _menu_from_lichess(self) -> None:
        self._stop_lichess_if_running()
        self.lichess_game = None
        self.pending_challenge = None
        self.review_mode = False
        self.lichess_auto = False
        if self.lichess_opponent_field is not None:
            self.lichess_opponent_field.active = False
        if self.lichess_token_field is not None:
            self.lichess_token_field.active = False
        self.mode = "menu"

    # --- Review mode navigation ------------------------------------------

    def _apply_review(self) -> None:
        if self.lichess_game is None:
            return
        self._set_lichess_position(self.lichess_game["moves"][:self.review_index])

    def _review_home(self) -> None:
        self.review_index = 0
        self._apply_review()

    def _review_prev(self) -> None:
        if self.lichess_game:
            self.review_index = max(0, self.review_index - 1)
            self._apply_review()

    def _review_next(self) -> None:
        if self.lichess_game:
            self.review_index = min(len(self.lichess_game["moves"]), self.review_index + 1)
            self._apply_review()

    def _review_end(self) -> None:
        if self.lichess_game:
            self.review_index = len(self.lichess_game["moves"])
            self._apply_review()

    def square_from_pos(self, pos: tuple[int, int]) -> tuple[int, int]:
        """Convert screen position to board (row, col)."""
        x, y = pos
        if x >= BOARD_SIZE:
            return (-1, -1)  # Click on panel
        if self.flipped:
            col = 7 - x // SQUARE_SIZE
            row = 7 - y // SQUARE_SIZE
        else:
            col = x // SQUARE_SIZE
            row = y // SQUARE_SIZE
        return (row, col)

    def handle_click(self, pos: tuple[int, int]) -> None:
        """Handle a mouse click on the board."""
        if self.game_over or self.is_engine_thinking:
            return
        if self.game.board.active_color != self.player_color:
            return

        row, col = self.square_from_pos(pos)
        if row < 0 or row > 7 or col < 0 or col > 7:
            return

        # If promotion dialog is active, handle it separately
        if self.promotion_pending is not None:
            return

        piece = self.game.board.get_piece(row, col)

        # If a square is already selected
        if self.selected_square is not None:
            # Check if the clicked square is a legal move destination
            matching_moves = [m for m in self.legal_moves_for_selected
                              if m[2] == row and m[3] == col]
            if matching_moves:
                # Check for promotion
                if matching_moves[0][4] is not None:
                    # Show promotion dialog
                    self.promotion_pending = matching_moves[0]
                    self.promotion_moves = matching_moves
                    self.selected_square = None
                    self.legal_moves_for_selected = []
                    return
                self.execute_move(matching_moves[0])
                self.selected_square = None
                self.legal_moves_for_selected = []
                return

            # Clicking on own piece changes selection
            if piece is not None and piece[0] == self.player_color:
                self.selected_square = (row, col)
                all_legal = generate_legal_moves(self.game.board, self.player_color)
                self.legal_moves_for_selected = [m for m in all_legal if m[0] == row and m[1] == col]
                return

            # Clicking elsewhere deselects
            self.selected_square = None
            self.legal_moves_for_selected = []
            return

        # No square selected yet — select a piece
        if piece is not None and piece[0] == self.player_color:
            self.selected_square = (row, col)
            all_legal = generate_legal_moves(self.game.board, self.player_color)
            self.legal_moves_for_selected = [m for m in all_legal if m[0] == row and m[1] == col]

    def execute_move(self, move: Move) -> None:
        """Execute a move on the board and update state."""
        from_r, from_c, to_r, to_c, promo = move
        captured = self.game.board.get_piece(to_r, to_c)
        # Check en passant capture
        moving_piece = self.game.board.get_piece(from_r, from_c)
        if moving_piece is not None and moving_piece[1] == "P" and from_c != to_c and captured is None:
            ep_captured = self.game.board.get_piece(from_r, to_c)
            if ep_captured is not None:
                captured = ep_captured

        notation = move_to_algebraic(self.game.board, move)
        result = self.game.make_move(move)

        # Track captured pieces
        if captured is not None:
            if captured[0] == "w":
                self.captured_white.append(captured)
            else:
                self.captured_black.append(captured)

        self.last_move = move
        self.move_history.append(notation)

        # Check for game over
        is_over, reason = self.game.is_game_over()
        if is_over:
            self.game_over = True
            self.game_result = reason

    def engine_move(self) -> None:
        """Let the engine make a move."""
        if self.game_over:
            return
        self.is_engine_thinking = True
        move = self.engine.get_best_move(self.game.board)
        self.is_engine_thinking = False
        if move is not None:
            self.execute_move(move)

    def _board_to_screen(self, row: int, col: int) -> tuple[int, int]:
        """Convert board (row, col) to screen (x, y) position."""
        if self.flipped:
            return ((7 - col) * SQUARE_SIZE, (7 - row) * SQUARE_SIZE)
        else:
            return (col * SQUARE_SIZE, row * SQUARE_SIZE)

    def draw_board(self) -> None:
        """Draw the chess board and pieces."""
        # Draw squares
        for row in range(8):
            for col in range(8):
                x, y = self._board_to_screen(row, col)
                color = LIGHT_SQUARE if (row + col) % 2 == 0 else DARK_SQUARE
                pygame.draw.rect(self.screen, color, (x, y, SQUARE_SIZE, SQUARE_SIZE))

        # Highlight last move
        if self.last_move is not None:
            from_r, from_c, to_r, to_c, _ = self.last_move
            surf = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
            surf.fill(LAST_MOVE_COLOR)
            self.screen.blit(surf, self._board_to_screen(from_r, from_c))
            self.screen.blit(surf, self._board_to_screen(to_r, to_c))

        # Highlight selected square
        if self.selected_square is not None:
            row, col = self.selected_square
            surf = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
            surf.fill(SELECTED_COLOR)
            self.screen.blit(surf, self._board_to_screen(row, col))

        # Highlight legal move destinations
        for move in self.legal_moves_for_selected:
            to_r, to_c = move[2], move[3]
            surf = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
            surf.fill(LEGAL_MOVE_COLOR)
            self.screen.blit(surf, self._board_to_screen(to_r, to_c))

        # Highlight king in check
        from src.moves import is_in_check
        active_color = self.game.board.active_color
        if is_in_check(self.game.board, active_color):
            king_pos = self.game.board.find_king(active_color)
            if king_pos:
                kr, kc = king_pos
                surf = pygame.Surface((SQUARE_SIZE, SQUARE_SIZE), pygame.SRCALPHA)
                surf.fill(CHECK_COLOR)
                self.screen.blit(surf, self._board_to_screen(kr, kc))

        # Draw rank and file labels
        for i in range(8):
            # File labels (a-h)
            if self.flipped:
                file_char = chr(ord("a") + 7 - i)
                rank_num = i + 1
            else:
                file_char = chr(ord("a") + i)
                rank_num = 8 - i
            label = self.small_font.render(file_char, True, (120, 120, 120))
            x = i * SQUARE_SIZE + SQUARE_SIZE - 12
            y = BOARD_SIZE - 16
            self.screen.blit(label, (x, y))
            # Rank labels (1-8)
            label = self.small_font.render(str(rank_num), True, (120, 120, 120))
            self.screen.blit(label, (2, i * SQUARE_SIZE + 2))

        # Draw pieces
        for row in range(8):
            for col in range(8):
                piece = self.game.board.get_piece(row, col)
                if piece is not None and piece in self.piece_surfaces:
                    x, y = self._board_to_screen(row, col)
                    self.screen.blit(self.piece_surfaces[piece], (x, y))

        # Draw promotion dialog
        if self.promotion_pending is not None:
            self._draw_promotion_dialog()

    def draw_panel(self) -> None:
        """Draw the side panel with game info."""
        if self.mode == "lichess":
            self._draw_lichess_panel()
            return
        panel_x = BOARD_SIZE
        pygame.draw.rect(self.screen, PANEL_COLOR, (panel_x, 0, self.panel_width, WINDOW_HEIGHT))

        # Title
        title = self.large_font.render("GLM CC Chess", True, (255, 255, 255))
        self.screen.blit(title, (panel_x + 10, 10))

        # Turn indicator
        y_offset = 50
        if not self.game_over:
            turn_text = "White to move" if self.game.board.active_color == "w" else "Black to move"
            if self.is_engine_thinking:
                turn_text = "Engine thinking..."
            turn_color = (255, 255, 255) if self.game.board.active_color == "w" else (180, 180, 180)
            turn_surf = self.medium_font.render(turn_text, True, turn_color)
            self.screen.blit(turn_surf, (panel_x + 10, y_offset))
        else:
            result_surf = self.medium_font.render(self.game_result, True, (255, 100, 100))
            self.screen.blit(result_surf, (panel_x + 10, y_offset))

        # Captured pieces
        y_offset = 80
        if self.captured_black:
            captured_str = " ".join(PIECE_SYMBOLS.get(p, "?") for p in sorted(self.captured_black))
            cap_surf = self.small_font.render(f"Taken: {captured_str}", True, (200, 200, 200))
            self.screen.blit(cap_surf, (panel_x + 10, y_offset))
        y_offset = 100
        if self.captured_white:
            captured_str = " ".join(PIECE_SYMBOLS.get(p, "?") for p in sorted(self.captured_white))
            cap_surf = self.small_font.render(f"Taken: {captured_str}", True, (200, 200, 200))
            self.screen.blit(cap_surf, (panel_x + 10, y_offset))

        # Move history
        y_offset = 130
        history_label = self.medium_font.render("Moves:", True, TEXT_COLOR)
        self.screen.blit(history_label, (panel_x + 10, y_offset))
        y_offset = 155

        # Show last 20 moves in two-column format
        moves_to_show = self.move_history[-40:]
        start_idx = max(0, len(self.move_history) - 40)
        for i in range(0, len(moves_to_show), 2):
            if y_offset > WINDOW_HEIGHT - 80:
                break
            move_num = (start_idx + i) // 2 + 1
            white_move = moves_to_show[i] if i < len(moves_to_show) else ""
            black_move = moves_to_show[i + 1] if i + 1 < len(moves_to_show) else ""
            line = f"{move_num}. {white_move}"
            if black_move:
                line += f" {black_move}"
            move_surf = self.small_font.render(line, True, (180, 180, 180))
            self.screen.blit(move_surf, (panel_x + 10, y_offset))
            y_offset += 18

        # Buttons
        self._draw_button("New Game", panel_x + 10, WINDOW_HEIGHT - 70, 95, 28, self._new_game_action)
        self._draw_button("Flip", panel_x + 115, WINDOW_HEIGHT - 70, 95, 28, self._flip_color_action)
        self._draw_button("Undo", panel_x + 10, WINDOW_HEIGHT - 36, 95, 28, self._undo_action)

    def _draw_lichess_panel(self) -> None:
        """Side panel for AI vs Lichess mode.

        Shows live state so the user can see what the bot is doing even while
        idle: current status, a last-activity indicator (proves the stream is
        alive), a recent activity log, idle instructions with the bot's URL,
        and during a game the opponent, clocks, last move, and move history.
        """
        panel_x = BOARD_SIZE
        panel_w = self.panel_width
        pygame.draw.rect(self.screen, PANEL_COLOR,
                         (panel_x, 0, panel_w, WINDOW_HEIGHT))
        bx = panel_x + 10
        inner_w = panel_w - 20             # text/content width
        btn_full = inner_w                 # a full-width button
        btn_half = (inner_w - 5) // 2      # two buttons per row with a 5px gap

        title = self.large_font.render("vs Lichess", True, (255, 255, 255))
        self.screen.blit(title, (bx, 10))

        y = 44
        # Account name + ratings header ("the Lichess score"). Always visible
        # once connected (incl. during a game and during review). "@name" is the
        # connected account; the per-speed ratings follow. The controller pushes
        # an AccountInfo on connect and re-pushes after each finished game, so
        # the rating refreshes in real time as games complete. The current/last
        # game's speed is shown first and bright (the one that refreshes); the
        # other speeds follow dimmed, wrapped across lines so a wider panel
        # shows them all and a narrow panel still shows them (wrapped, never
        # clipped — the fixed 220px panel used to clip the rating off the edge).
        if self.lichess_connected:
            name = (self.lichess_account_name
                    or (self.lichess_controller.username
                        if self.lichess_controller else ""))
            self.screen.blit(self.small_font.render(f"@{name}", True, (120, 200, 255)),
                             (bx, y))
            y += 18
            ratings = self.lichess_ratings or {}
            if ratings:
                cur = (self.lichess_last_speed
                       or self._lichess_speed_for_clock() or "")
                order: list[str] = []
                for s in (cur, "rapid", "blitz", "classical", "bullet"):
                    if s and s in ratings and s not in order:
                        order.append(s)
                seg_x = bx
                for i, s in enumerate(order):
                    label = f"{s} {ratings[s]}"
                    col = (170, 235, 170) if s == cur else (110, 150, 110)
                    surf = self.small_font.render(label, True, col)
                    if i > 0:
                        sep_surf = self.small_font.render("·", True, (90, 90, 90))
                        if seg_x + sep_surf.get_width() + surf.get_width() > bx + inner_w:
                            seg_x = bx
                            y += 16
                        else:
                            self.screen.blit(sep_surf, (seg_x, y))
                            seg_x += sep_surf.get_width() + 4
                    self.screen.blit(surf, (seg_x, y))
                    seg_x += surf.get_width() + 4
                y += 22
            else:
                y += 4
        else:
            y += 4

        # Status (wrapped to the panel width, as many lines as needed -- the
        # fixed-panel build truncated it to 2 lines of 30 chars).
        for line in self._wrap_text_px(self.lichess_status, self.small_font, inner_w):
            self.screen.blit(self.small_font.render(line, True, TEXT_COLOR), (bx, y))
            y += 17
        y += 3

        # Last-activity indicator — proves the connection is live. Updates on
        # every received event (including the ~7s Lichess keep-alive pulses).
        if self.lichess_controller is not None:
            elapsed = (pygame.time.get_ticks() - self.lichess_last_event_ticks) / 1000.0
            activity = (f"Last activity: {elapsed/60:.0f}m ago"
                        if elapsed >= 60 else f"Last activity: {elapsed:.0f}s ago")
            self.screen.blit(self.small_font.render(activity, True, (130, 130, 130)), (bx, y))
        y += 22

        if not self.lichess_connected:
            # Connect form — enter the bot token in the UI (masked) and click
            # Connect (or press Enter). Shown instead of the opponent controls
            # until the connection is established; the token is never rendered
            # unmasked (see TextInput.display_text). Ctrl+V pastes from the
            # clipboard, filtered to token-legal chars.
            self.screen.blit(self.small_font.render("Lichess bot token:",
                                                    True, (200, 180, 120)), (bx, 100))
            if self.lichess_token_field is not None:
                self.lichess_token_field.draw(self.screen, hint="paste token here")
            self._draw_button("Connect", bx, 172, btn_full, 26, self._connect_with_token)
            self.screen.blit(self.small_font.render("Ctrl+V paste, Enter to connect",
                                                    True, (110, 110, 110)), (bx, 202))
            y = 224
        else:
            # Opponent field + Challenge/Auto controls whenever there is no
            # *active* game (none yet, or the last game finished) and no pending
            # incoming challenge. The field is editable during review of a
            # finished game, so the user can always type the opponent name — the
            # box used to be hidden until ``Connected as`` arrived and locked
            # during review, which made challenging again after a game
            # impossible without leaving Lichess via Menu.
            field_open = ((self.lichess_game is None or self.lichess_game.get("over"))
                          and self.pending_challenge is None)
            if field_open:
                username = self.lichess_controller.username if self.lichess_controller else ""
                is_bot = self.lichess_controller.is_bot if self.lichess_controller else True
                if not is_bot and username:
                    # Normal account: bot-only endpoints will 400. Show a clear,
                    # actionable warning (the account name itself is in the header
                    # above) and an Upgrade button instead of Challenge/Auto
                    # (challenging is blocked until the account is upgraded).
                    self.screen.blit(self.small_font.render(
                        f"@{username} is not a Bot account", True, (220, 150, 120)),
                        (bx, 110))
                self.screen.blit(self.small_font.render("Opponent:", True, (200, 180, 120)),
                                 (bx, 130))
                if self.lichess_opponent_field is not None:
                    self.lichess_opponent_field.draw(self.screen)
                if not is_bot:
                    self._draw_button("Upgrade to Bot", bx, 176, btn_full, 26,
                                      self._upgrade_lichess_bot)
                else:
                    self._draw_button("Challenge", bx, 176, btn_half, 26,
                                      self._challenge_opponent)
                    auto_label = "Auto: ON" if self.lichess_auto else "Auto: OFF"
                    self._draw_button(auto_label, bx + btn_half + 5, 176, btn_half, 26,
                                      self._toggle_lichess_auto)
                tc = f"{self.lichess_clock_limit_s // 60}+{self.lichess_clock_increment_s}"
                rated_label = "Rated: ON" if self.lichess_rated else "Rated: OFF"
                self._draw_button(rated_label, bx, 206, btn_half, 22, self._toggle_lichess_rated)
                self.screen.blit(self.small_font.render(f"Time: {tc}",
                                                        True, (130, 130, 130)),
                                 (bx + btn_half + 9, 210))
                y = 228
            else:
                y += 4

        # Activity log — what's going on (connected / challenge / game / over).
        self.screen.blit(self.medium_font.render("Activity:", True, TEXT_COLOR), (bx, y))
        # Copy control next to the header. The on-screen lines wrap to the panel
        # width (no longer truncated to 30 chars), so this still copies the full
        # source log to the clipboard (with a file fallback) for a bug report.
        self._draw_button("Copy Log", bx + 78, y - 2, 86, 20, self._copy_lichess_log)
        y += 20
        # How far down the log may flow before yielding to the block below it
        # (the game block, or the Accept/Decline / Menu buttons).
        if self.lichess_game:
            log_bottom = WINDOW_HEIGHT - 196
        elif self.pending_challenge is not None:
            log_bottom = WINDOW_HEIGHT - 112
        else:
            log_bottom = WINDOW_HEIGHT - 44
        line_h = 16
        max_lines = max(0, (log_bottom - y) // line_h)
        # Gather wrapped lines from the most recent entries until they fill the
        # space (newest ends at the bottom, as before). Wrapping means a wider
        # panel shows more entries -- each on fewer lines -- so the user widens
        # the window to see more of the message that used to be clipped.
        chosen: list[list[str]] = []
        count = 0
        for entry in reversed(self.lichess_log):
            wrapped = self._wrap_text_px(entry, self.small_font, inner_w)
            if chosen and count + len(wrapped) > max_lines:
                break
            chosen.append(wrapped)
            count += len(wrapped)
        chosen.reverse()  # oldest-of-the-shown first; newest at the bottom
        for grp in chosen:
            for ln in grp:
                self.screen.blit(self.small_font.render(ln, True, (170, 170, 170)),
                                 (bx, y))
                y += line_h

        # Game block (active game or review): opponent, clocks, last move, moves.
        if self.lichess_game:
            y = WINDOW_HEIGHT - 190
            opp = str(self.lichess_game.get("opponent_name", "?"))
            opp_y = y
            for line in self._wrap_text_px(f"Opp: {opp}", self.small_font, inner_w)[:2]:
                self.screen.blit(self.small_font.render(line, True, TEXT_COLOR), (bx, opp_y))
                opp_y += 16
            y += 20
            wclk = self._fmt_clock(self.lichess_game.get("wtime"))
            bclk = self._fmt_clock(self.lichess_game.get("btime"))
            self.screen.blit(self.small_font.render(f"W clock: {wclk}", True, (255, 255, 255)),
                             (bx, y))
            y += 18
            self.screen.blit(self.small_font.render(f"B clock: {bclk}", True, (180, 180, 180)),
                             (bx, y))
            y += 18
            if self.lichess_last_move_text:
                for line in self._wrap_text_px(self.lichess_last_move_text,
                                               self.small_font, inner_w)[:2]:
                    self.screen.blit(self.small_font.render(line, True, (180, 180, 180)),
                                     (bx, y))
                    y += 16
            self.screen.blit(self.small_font.render("Moves:", True, TEXT_COLOR), (bx, y))
            y += 18
            for notation in self.move_history[-6:]:
                if y > WINDOW_HEIGHT - 116:
                    break
                self.screen.blit(self.small_font.render(notation, True, (170, 170, 170)),
                                 (bx, y))
                y += 16

        # Buttons — re-registered every frame (GUI_BUTTON_PATTERN)
        if self.pending_challenge is not None:
            self._draw_button("Accept", bx, WINDOW_HEIGHT - 104, btn_half, 28,
                              self._accept_challenge)
            self._draw_button("Decline", bx + btn_half + 5, WINDOW_HEIGHT - 104, btn_half, 28,
                              self._decline_challenge)
        elif self.lichess_game and not self.lichess_game.get("over") and not self.review_mode:
            self._draw_button("Resign", bx, WINDOW_HEIGHT - 104, btn_half, 28,
                              self._resign_lichess)
            self._draw_button("Abort", bx + btn_half + 5, WINDOW_HEIGHT - 104, btn_half, 28,
                              self._abort_lichess)

        if self.review_mode:
            self._draw_button("<<", bx, WINDOW_HEIGHT - 70, 45, 28, self._review_home)
            self._draw_button("<", bx + 50, WINDOW_HEIGHT - 70, 45, 28, self._review_prev)
            self._draw_button(">", bx + 100, WINDOW_HEIGHT - 70, 45, 28, self._review_next)
            self._draw_button(">>", bx + 150, WINDOW_HEIGHT - 70, 45, 28, self._review_end)

        self._draw_button("Menu", bx, WINDOW_HEIGHT - 36, btn_full, 28, self._menu_from_lichess)

    def _draw_promotion_dialog(self) -> None:
        """Draw promotion piece selection dialog."""
        if not self.promotion_moves:
            return
        # Determine color and position
        promo_color = self.game.board.active_color
        # Use the destination square for positioning
        dest_row = self.promotion_moves[0][2]
        dest_col = self.promotion_moves[0][3]
        dest_x, dest_y = self._board_to_screen(dest_row, dest_col)
        # For white promoting, dialog goes up; for black, goes down
        if promo_color == "w":
            dialog_y = dest_y - 3 * SQUARE_SIZE
        else:
            dialog_y = dest_y
        dialog_y = max(0, min(dialog_y, WINDOW_HEIGHT - SQUARE_SIZE * 4))
        dialog_x = dest_x
        piece_types = ["Q", "R", "B", "N"]

        # Draw semi-transparent overlay
        overlay = pygame.Surface((self.window_width, WINDOW_HEIGHT), pygame.SRCALPHA)
        overlay.fill((0, 0, 0, 128))
        self.screen.blit(overlay, (0, 0))

        # Draw dialog box
        box_w = SQUARE_SIZE
        box_h = SQUARE_SIZE * 4
        box_x = dialog_x
        box_y = dialog_y

        pygame.draw.rect(self.screen, (50, 50, 50), (box_x, box_y, box_w, box_h))
        pygame.draw.rect(self.screen, (200, 200, 200), (box_x, box_y, box_w, box_h), 2)

        # Store promotion button rects for click handling
        self._promo_rects = []
        for i, pt in enumerate(piece_types):
            piece_code = promo_color + pt
            rect_y = box_y + i * SQUARE_SIZE
            rect = pygame.Rect(box_x, rect_y, SQUARE_SIZE, SQUARE_SIZE)
            self._promo_rects.append((rect, piece_code))

            # Highlight on hover
            mouse_pos = pygame.mouse.get_pos()
            if rect.collidepoint(mouse_pos):
                pygame.draw.rect(self.screen, (100, 140, 100), rect)

            # Draw piece symbol
            if piece_code in self.piece_surfaces:
                self.screen.blit(self.piece_surfaces[piece_code], (box_x, rect_y))

    def _handle_promotion_click(self, pos: tuple[int, int]) -> None:
        """Handle click during promotion dialog."""
        if not hasattr(self, '_promo_rects'):
            return
        for rect, piece_code in self._promo_rects:
            if rect.collidepoint(pos):
                # Find the promotion move matching this piece type
                promo_type = piece_code[1]  # "Q", "R", "B", or "N"
                for move in self.promotion_moves:
                    if move[4] == promo_type:
                        self.execute_move(move)
                        self.promotion_pending = None
                        self.promotion_moves = []
                        self.selected_square = None
                        self.legal_moves_for_selected = []
                        return

    def _draw_button(self, text: str, x: int, y: int, w: int, h: int, action) -> None:
        """Draw a button and register its action."""
        mouse_pos = pygame.mouse.get_pos()
        is_hover = x <= mouse_pos[0] <= x + w and y <= mouse_pos[1] <= y + h
        color = BUTTON_HOVER_COLOR if is_hover else BUTTON_COLOR
        pygame.draw.rect(self.screen, color, (x, y, w, h), border_radius=4)
        text_surf = self.small_font.render(text, True, BUTTON_TEXT_COLOR)
        text_rect = text_surf.get_rect(center=(x + w // 2, y + h // 2))
        self.screen.blit(text_surf, text_rect)
        # Store button for click handling
        if not hasattr(self, '_buttons'):
            self._buttons = []
        self._buttons.append((x, y, w, h, action))

    def _new_game_action(self) -> None:
        self.reset_game()
        self.mode = "play"

    def _flip_color_action(self) -> None:
        self.flipped = not self.flipped

    def _undo_action(self) -> None:
        if len(self.game.move_history) >= 2 and not self.game_over:
            # Undo both player and engine move
            self.game.unmake_move()
            self.game.unmake_move()
            self.move_history.pop()
            self.move_history.pop()
            self.last_move = None
            self.selected_square = None
            self.legal_moves_for_selected = []

    def handle_button_click(self, pos: tuple[int, int]) -> None:
        """Check if a button was clicked."""
        if hasattr(self, '_buttons'):
            for x, y, w, h, action in self._buttons:
                if x <= pos[0] <= x + w and y <= pos[1] <= y + h:
                    action()
                    return

    def draw_menu(self) -> None:
        """Draw the main menu."""
        self.screen.fill(BG_COLOR)

        title = self.large_font.render("GLM CC Chess", True, (255, 255, 255))
        title_rect = title.get_rect(center=(self.window_width // 2, 100))
        self.screen.blit(title, title_rect)

        subtitle = self.medium_font.render("A Chess Game with Built-in Engine", True, (180, 180, 180))
        sub_rect = subtitle.get_rect(center=(self.window_width // 2, 140))
        self.screen.blit(subtitle, sub_rect)

        self._buttons = []
        self._draw_button("Play as White", self.window_width // 2 - 100, 200, 200, 40,
                         lambda: self._start_game("w"))
        self._draw_button("Play as Black", self.window_width // 2 - 100, 260, 200, 40,
                         lambda: self._start_game("b"))
        self._draw_button("Engine vs Engine", self.window_width // 2 - 100, 320, 200, 40,
                         lambda: self._start_game("engine_vs_engine"))
        self._draw_button("AI vs Lichess", self.window_width // 2 - 100, 380, 200, 40,
                         self._start_lichess)

    def _start_game(self, mode: str) -> None:
        if mode == "engine_vs_engine":
            self.mode = "engine_vs_engine"
            self.player_color = "w"  # Doesn't matter
        else:
            self.mode = "play"
            self.player_color = mode
        self.flipped = False
        self.reset_game()

    def run(self) -> None:
        """Main game loop."""
        running = True
        while running:
            # Process events using previous frame's button positions
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self._stop_lichess_if_running()
                    running = False
                elif event.type == pygame.VIDEORESIZE:
                    # Widen the panel (board stays 640x640; height held at
                    # BOARD_SIZE). Lets the user enlarge the right side to read
                    # the full panel text.
                    self._handle_resize(event.w, event.h)
                elif event.type == pygame.MOUSEBUTTONDOWN:
                    if event.button == 1:
                        if self.promotion_pending is not None:
                            # Handle promotion dialog click
                            self._handle_promotion_click(event.pos)
                        elif self.mode == "menu":
                            self.handle_button_click(event.pos)
                        else:
                            # Check buttons first (covers lichess panel buttons)
                            self.handle_button_click(event.pos)
                            if self.mode == "lichess":
                                self._handle_lichess_field_click(event.pos)
                            elif self.mode == "play":
                                self.handle_click(event.pos)
                elif event.type == pygame.KEYDOWN:
                    if self.mode == "lichess" and self._handle_lichess_keydown(event):
                        # Consumed by a Lichess text field (typing), Ctrl+V paste,
                        # or Enter-on-token-field to connect.
                        pass
                    elif event.key == pygame.K_n:
                        self._stop_lichess_if_running()
                        self.reset_game()
                        self.mode = "menu"
                    elif event.key == pygame.K_u and self.mode != "lichess":
                        self._undo_action()
                    elif event.key == pygame.K_LEFT and self.mode == "lichess" \
                            and self.review_mode:
                        self._review_prev()
                    elif event.key == pygame.K_RIGHT and self.mode == "lichess" \
                            and self.review_mode:
                        self._review_next()
                elif event.type == pygame.TEXTINPUT:
                    # OS-composed text (IME/shift-correct) -> focused Lichess
                    # field. This is the path that captures '_' in lip_...
                    # tokens when KEYDOWN.unicode is empty under an IME.
                    if self.mode == "lichess":
                        self._handle_lichess_textinput(event)

            # Clear button list before redrawing
            self._buttons = []

            # Drawing
            if self.mode == "menu":
                self.draw_menu()
            else:
                # Lichess mode: drain background events into GUI state first
                if self.mode == "lichess":
                    self._drain_lichess_events()

                self.draw_board()
                self.draw_panel()

                # Local engine move scheduling — never in lichess mode
                # (the controller's threads handle engine moves there).
                if self.mode != "lichess" and not self.game_over \
                        and not self.is_engine_thinking:
                    if self.mode == "engine_vs_engine":
                        current_time = pygame.time.get_ticks()
                        if current_time - self.last_engine_move_time > self.engine_vs_engine_delay:
                            self.engine_move()
                            self.last_engine_move_time = current_time
                    elif self.game.board.active_color != self.player_color:
                        self.engine_move()

            pygame.display.flip()
            self.clock.tick(FPS)

        pygame.quit()
        sys.exit()