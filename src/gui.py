"""Pygame-based chess GUI for the chess application.

Provides a visual chessboard with click-to-move interaction,
legal move highlighting, and game status display.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import pygame
from src.board import Board, STARTING_FEN
from src.game import GameState
from src.moves import generate_legal_moves, move_to_algebraic, uci_to_move, Move
from src.engine import ChessEngine, choose_move
from src.lichess_controller import (
    LichessController, ChallengeReceived, GameStarted, GameUpdated,
    EngineMoved, GameFinished, Status, Error,
)

logger = logging.getLogger(__name__)


def _resource_path(relative_path: str) -> str:
    """Return the absolute path to a bundled resource, works in dev and PyInstaller."""
    if getattr(sys, 'frozen', False):
        # Running as PyInstaller bundle
        base = sys._MEIPASS
    else:
        # Running from source
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)

# Constants
SQUARE_SIZE = 80
BOARD_SIZE = SQUARE_SIZE * 8
PANEL_WIDTH = 220
WINDOW_WIDTH = BOARD_SIZE + PANEL_WIDTH
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
        self.screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
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
        copy is never used as a real token. The token is never stored on ``self``
        or rendered to the UI.
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

    def _lichess_result_text(self, status: str, winner) -> str:
        """Map a Lichess game-end status to a human-readable result."""
        if status in ("aborted", "noStart"):
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
        """Append a human-readable event to the on-screen activity log (capped)."""
        self.lichess_log.append(message)
        if len(self.lichess_log) > 8:
            self.lichess_log = self.lichess_log[-8:]
        self.lichess_last_event_ticks = pygame.time.get_ticks()

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

    def _start_lichess(self) -> None:
        """Enter Lichess mode: validate token, start the controller, clear state."""
        self.reset_game()
        self.lichess_game = None
        self.pending_challenge = None
        self.review_mode = False
        self.review_index = 0
        self.lichess_log = []
        self.lichess_last_move_text = ""
        self.lichess_connected = False
        self.lichess_last_event_ticks = pygame.time.get_ticks()
        self.mode = "lichess"
        token = self._get_lichess_token()
        if not token:
            self.lichess_controller = None
            self.lichess_status = "No token. Set LICHESS_BOT_TOKEN env var."
            self._log_lichess("No token found — set LICHESS_BOT_TOKEN.")
            return
        self.lichess_status = "Connecting..."
        self._log_lichess("Connecting to Lichess...")
        self.lichess_controller = LichessController(token)
        self.lichess_controller.start()

    def _stop_lichess_if_running(self) -> None:
        if self.lichess_controller is not None:
            self.lichess_controller.stop()
            self.lichess_controller = None

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

    def _handle_lichess_event(self, event) -> None:
        # Any event proves the stream is alive — record it for the indicator.
        self.lichess_last_event_ticks = pygame.time.get_ticks()
        if isinstance(event, ChallengeReceived):
            self.pending_challenge = event
            self.lichess_status = f"Challenge from {event.opponent} ({event.speed})"
            self._log_lichess(f"Challenge: {event.opponent} ({event.speed})")
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
            }
            self.pending_challenge = None
            self.flipped = not event.bot_is_white  # show the bot's perspective
            self.review_mode = False
            self.review_index = 0
            self.lichess_connected = True
            self.lichess_last_move_text = ""
            self.lichess_status = f"Playing {event.opponent_name}"
            self._log_lichess(f"Game started vs {event.opponent_name}")
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
                self._set_lichess_position(self.lichess_game["moves"])
                self.lichess_status = f"Engine played {event.uci}"
                self.lichess_last_move_text = f"Move {len(self.lichess_game['moves'])}: {event.uci}"
        elif isinstance(event, GameFinished):
            if self.lichess_game and self.lichess_game["game_id"] == event.game_id:
                self.lichess_game["moves"] = list(event.moves)
                self.lichess_game["status"] = event.status
                self.lichess_game["winner"] = event.winner
                self.lichess_game["over"] = True
                self.lichess_status = self._lichess_result_text(event.status, event.winner)
                self._log_lichess(f"Game over: {self.lichess_status}")
                self._enter_review()
            elif self.lichess_game is None:
                self.lichess_status = f"Game {event.game_id} finished"
                self._log_lichess(f"Game {event.game_id} finished")
        elif isinstance(event, Status):
            # "Connected as ..." is meaningful (log it + show instructions);
            # "Engine thinking..." is transient (just display it, don't log).
            if event.message.startswith("Connected as"):
                self.lichess_connected = True
                self.lichess_status = f"{event.message} — waiting for challenges"
                self._log_lichess(event.message)
            else:
                self.lichess_status = event.message
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
            self.lichess_controller.abort(self.lichess_game["game_id"])

    def _menu_from_lichess(self) -> None:
        self._stop_lichess_if_running()
        self.lichess_game = None
        self.pending_challenge = None
        self.review_mode = False
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
        pygame.draw.rect(self.screen, PANEL_COLOR, (panel_x, 0, PANEL_WIDTH, WINDOW_HEIGHT))

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
        pygame.draw.rect(self.screen, PANEL_COLOR, (panel_x, 0, PANEL_WIDTH, WINDOW_HEIGHT))
        bx = panel_x + 10

        title = self.large_font.render("vs Lichess", True, (255, 255, 255))
        self.screen.blit(title, (bx, 10))

        y = 44
        # Status (wrapped to up to 2 lines)
        for line in self._wrap_text(self.lichess_status, 30)[:2]:
            self.screen.blit(self.small_font.render(line, True, TEXT_COLOR), (bx, y))
            y += 18

        # Last-activity indicator — proves the connection is live. Updates on
        # every received event (including the ~7s Lichess keep-alive pulses).
        if self.lichess_controller is not None:
            elapsed = (pygame.time.get_ticks() - self.lichess_last_event_ticks) / 1000.0
            activity = (f"Last activity: {elapsed/60:.0f}m ago"
                        if elapsed >= 60 else f"Last activity: {elapsed:.0f}s ago")
            self.screen.blit(self.small_font.render(activity, True, (130, 130, 130)), (bx, y))
        y += 22

        # Idle instructions: tell the user how to get a game.
        if self.lichess_connected and self.lichess_game is None \
                and self.pending_challenge is None and not self.review_mode:
            username = self.lichess_controller.username if self.lichess_controller else ""
            for line in self._wrap_text(
                    "Waiting for a challenge. From another Lichess account,"
                    " challenge this bot:", 30):
                self.screen.blit(self.small_font.render(line, True, (200, 180, 120)), (bx, y))
                y += 18
            if username:
                self.screen.blit(self.small_font.render(f"lichess.org/@/{username}",
                                                        True, (120, 200, 255)), (bx, y))
                y += 18
            y += 4

        # Activity log — what's going on (connected / challenge / game / over).
        self.screen.blit(self.medium_font.render("Activity:", True, TEXT_COLOR), (bx, y))
        y += 20
        for line in self.lichess_log[-6:]:
            if y > WINDOW_HEIGHT - 200:
                break
            self.screen.blit(self.small_font.render(line[:30], True, (170, 170, 170)), (bx, y))
            y += 17

        # Game block (active game or review): opponent, clocks, last move, moves.
        if self.lichess_game:
            y = WINDOW_HEIGHT - 190
            opp = str(self.lichess_game.get("opponent_name", "?"))
            self.screen.blit(self.small_font.render(f"Opp: {opp[:20]}", True, TEXT_COLOR), (bx, y))
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
                self.screen.blit(self.small_font.render(self.lichess_last_move_text[:28],
                                                        True, (180, 180, 180)), (bx, y))
                y += 18
            self.screen.blit(self.small_font.render("Moves:", True, TEXT_COLOR), (bx, y))
            y += 18
            for notation in self.move_history[-6:]:
                if y > WINDOW_HEIGHT - 116:
                    break
                self.screen.blit(self.small_font.render(notation[:24], True, (170, 170, 170)),
                                 (bx, y))
                y += 16

        # Buttons — re-registered every frame (GUI_BUTTON_PATTERN)
        if self.pending_challenge is not None:
            self._draw_button("Accept", bx, WINDOW_HEIGHT - 104, 95, 28, self._accept_challenge)
            self._draw_button("Decline", bx + 100, WINDOW_HEIGHT - 104, 95, 28,
                              self._decline_challenge)
        elif self.lichess_game and not self.lichess_game.get("over") and not self.review_mode:
            self._draw_button("Resign", bx, WINDOW_HEIGHT - 104, 95, 28, self._resign_lichess)
            self._draw_button("Abort", bx + 100, WINDOW_HEIGHT - 104, 95, 28, self._abort_lichess)

        if self.review_mode:
            self._draw_button("<<", bx, WINDOW_HEIGHT - 70, 45, 28, self._review_home)
            self._draw_button("<", bx + 50, WINDOW_HEIGHT - 70, 45, 28, self._review_prev)
            self._draw_button(">", bx + 100, WINDOW_HEIGHT - 70, 45, 28, self._review_next)
            self._draw_button(">>", bx + 150, WINDOW_HEIGHT - 70, 45, 28, self._review_end)

        self._draw_button("Menu", bx, WINDOW_HEIGHT - 36, 200, 28, self._menu_from_lichess)

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
        overlay = pygame.Surface((WINDOW_WIDTH, WINDOW_HEIGHT), pygame.SRCALPHA)
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
        title_rect = title.get_rect(center=(WINDOW_WIDTH // 2, 100))
        self.screen.blit(title, title_rect)

        subtitle = self.medium_font.render("A Chess Game with Built-in Engine", True, (180, 180, 180))
        sub_rect = subtitle.get_rect(center=(WINDOW_WIDTH // 2, 140))
        self.screen.blit(subtitle, sub_rect)

        self._buttons = []
        self._draw_button("Play as White", WINDOW_WIDTH // 2 - 100, 200, 200, 40,
                         lambda: self._start_game("w"))
        self._draw_button("Play as Black", WINDOW_WIDTH // 2 - 100, 260, 200, 40,
                         lambda: self._start_game("b"))
        self._draw_button("Engine vs Engine", WINDOW_WIDTH // 2 - 100, 320, 200, 40,
                         lambda: self._start_game("engine_vs_engine"))
        self._draw_button("AI vs Lichess", WINDOW_WIDTH // 2 - 100, 380, 200, 40,
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
                            if self.mode == "play":
                                self.handle_click(event.pos)
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_n:
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