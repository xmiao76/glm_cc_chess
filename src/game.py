"""Game state controller for chess.

Manages the full game state: board position, castling rights, en passant,
halfmove clock, move history. Provides make_move/unmake_move and
draw/checkmate detection.
"""

from __future__ import annotations

from src.board import Board
from src.moves import (
    Move,
    generate_legal_moves,
    is_in_check,
    is_checkmate,
    is_stalemate,
    _opponent,
)


class GameResult(Exception):
    """Raised when the game ends."""

    def __init__(self, result: str, reason: str) -> None:
        self.result = result  # "1-0", "0-1", "1/2-1/2"
        self.reason = reason


class GameState:
    """Full chess game state with move history and draw detection."""

    def __init__(self, board: Board | None = None) -> None:
        self.board = board if board is not None else Board.from_fen(Board.STARTING_FEN)
        self.move_history: list[Move] = []
        self.position_history: list[str] = [self.board.to_fen()]
        # Stack of states for unmake: (board_copy, castling_rights, ep_square,
        #   halfmove_clock, fullmove_number, captured_piece)
        self._undo_stack: list[tuple] = []

    def make_move(self, move: Move) -> str | None:
        """Execute a move on the board. Returns captured piece or None.

        Raises GameResult if the game ends.
        """
        from_r, from_c, to_r, to_c, promo = move
        piece = self.board.get_piece(from_r, from_c)
        if piece is None:
            raise ValueError(f"No piece at ({from_r},{from_c})")

        color = piece[0]
        piece_type = piece[1]

        # Save state for unmake
        captured = self.board.get_piece(to_r, to_c)
        old_castling = self.board.castling_rights
        old_ep = self.board.en_passant_square
        old_halfmove = self.board.halfmove_clock
        old_fullmove = self.board.fullmove_number

        # En passant capture
        en_passant_capture: str | None = None
        if piece_type == "P" and from_c != to_c and captured is None:
            # En passant
            en_passant_capture = self.board.get_piece(from_r, to_c)
            self.board.set_piece(from_r, to_c, None)

        # Actually capture
        if captured is not None:
            pass  # already on destination square

        # Move the piece
        self.board.set_piece(from_r, from_c, None)
        if promo is not None:
            self.board.set_piece(to_r, to_c, color + promo)
        else:
            self.board.set_piece(to_r, to_c, piece)

        # Castling: move the rook
        if piece_type == "K":
            if to_c - from_c == 2:  # Kingside
                rook = self.board.get_piece(from_r, 7)
                self.board.set_piece(from_r, 7, None)
                self.board.set_piece(from_r, 5, color + "R")
            elif from_c - to_c == 2:  # Queenside
                rook = self.board.get_piece(from_r, 0)
                self.board.set_piece(from_r, 0, None)
                self.board.set_piece(from_r, 3, color + "R")

        # Update castling rights
        new_castling = self.board.castling_rights
        if piece_type == "K":
            if color == "w":
                new_castling = new_castling.replace("K", "").replace("Q", "")
            else:
                new_castling = new_castling.replace("k", "").replace("q", "")
        # Rook moved or captured
        if piece_type == "R" or captured is not None:
            # Check rook positions
            if from_r == 7 and from_c == 7 or (to_r == 7 and to_c == 7):
                new_castling = new_castling.replace("K", "")
            if from_r == 7 and from_c == 0 or (to_r == 7 and to_c == 0):
                new_castling = new_castling.replace("Q", "")
            if from_r == 0 and from_c == 7 or (to_r == 0 and to_c == 7):
                new_castling = new_castling.replace("k", "")
            if from_r == 0 and from_c == 0 or (to_r == 0 and to_c == 0):
                new_castling = new_castling.replace("q", "")
        self.board.castling_rights = new_castling

        # Update en passant square
        if piece_type == "P" and abs(to_r - from_r) == 2:
            ep_row = (from_r + to_r) // 2
            self.board.en_passant_square = (ep_row, from_c)
        else:
            self.board.en_passant_square = None

        # Update halfmove clock
        if piece_type == "P" or captured is not None or en_passant_capture is not None:
            self.board.halfmove_clock = 0
        else:
            self.board.halfmove_clock += 1

        # Update fullmove number
        if color == "b":
            self.board.fullmove_number += 1

        # Switch active color
        self.board.active_color = _opponent(color)

        # Save to undo stack
        # is_en_passant flag helps unmake distinguish en passant from regular capture
        is_ep = en_passant_capture is not None
        saved_capture = captured if captured is not None else en_passant_capture
        self._undo_stack.append(
            (old_castling, old_ep, old_halfmove, old_fullmove,
             from_r, from_c, to_r, to_c, promo, piece, saved_capture, is_ep)
        )

        # Record position for repetition detection
        self.move_history.append(move)
        self.position_history.append(self.board.to_fen())

        # Check for game end
        return saved_capture

    def unmake_move(self) -> None:
        """Undo the last move."""
        if not self._undo_stack:
            raise ValueError("No moves to unmake")

        (old_castling, old_ep, old_halfmove, old_fullmove,
         from_r, from_c, to_r, to_c, promo, piece, saved_capture, is_ep) = self._undo_stack.pop()

        color = piece[0]

        # Undo castling rook movement (before moving the king back)
        if piece[1] == "K":
            if to_c - from_c == 2:  # Kingside
                self.board.set_piece(from_r, 5, None)
                self.board.set_piece(from_r, 7, color + "R")
            elif from_c - to_c == 2:  # Queenside
                self.board.set_piece(from_r, 3, None)
                self.board.set_piece(from_r, 0, color + "R")

        # Move the piece back to its original square
        self.board.set_piece(from_r, from_c, piece)

        if is_ep:
            # En passant: the captured pawn was on (from_r, to_c), not (to_r, to_c)
            # The destination square was empty before the move
            self.board.set_piece(to_r, to_c, None)
            self.board.set_piece(from_r, to_c, saved_capture)
        elif promo is not None:
            # Promotion: put the captured piece back (or None) on the destination square
            self.board.set_piece(to_r, to_c, saved_capture)
        else:
            # Normal move or regular capture: restore destination square
            self.board.set_piece(to_r, to_c, saved_capture)

        # Restore state
        self.board.castling_rights = old_castling
        self.board.en_passant_square = old_ep
        self.board.halfmove_clock = old_halfmove
        self.board.fullmove_number = old_fullmove
        self.board.active_color = _opponent(self.board.active_color)

        # Remove from history
        self.move_history.pop()
        self.position_history.pop()

    def get_legal_moves(self) -> list[Move]:
        """Get all legal moves for the current side."""
        return generate_legal_moves(self.board, self.board.active_color)

    def is_game_over(self) -> tuple[bool, str]:
        """Check if the game is over. Returns (is_over, reason)."""
        color = self.board.active_color
        legal_moves = self.get_legal_moves()

        if len(legal_moves) == 0:
            if is_in_check(self.board, color):
                winner = "White" if color == "b" else "Black"
                return True, f"Checkmate — {winner} wins"
            else:
                return True, "Stalemate"

        # 50-move rule
        if self.board.halfmove_clock >= 100:
            return True, "50-move rule — Draw"

        # Threefold repetition
        if self._is_threefold_repetition():
            return True, "Threefold repetition — Draw"

        # Insufficient material
        if self._is_insufficient_material():
            return True, "Insufficient material — Draw"

        return False, ""

    def get_game_result(self) -> str:
        """Get the result string if the game is over, else empty string."""
        is_over, reason = self.is_game_over()
        if not is_over:
            return ""
        color = self.board.active_color
        legal_moves = self.get_legal_moves()
        if len(legal_moves) == 0:
            if is_in_check(self.board, color):
                return "1-0" if color == "b" else "0-1"
            else:
                return "1/2-1/2"
        return "1/2-1/2"

    def _is_threefold_repetition(self) -> bool:
        """Check for threefold repetition.

        Compares only the position part of FEN (placement, active color,
        castling, en passant) — not the move counters.
        """
        current = self.position_history[-1]
        # Extract position-only key (first 4 fields of FEN)
        current_pos = " ".join(current.split()[:4])
        count = 0
        for fen in self.position_history:
            pos = " ".join(fen.split()[:4])
            if pos == current_pos:
                count += 1
        return count >= 3

    def _is_insufficient_material(self) -> bool:
        """Check for insufficient material to checkmate."""
        white_pieces = self.board.get_all_pieces("w")
        black_pieces = self.board.get_all_pieces("b")

        # Remove kings
        white_non_king = [(r, c, p) for r, c, p in white_pieces if p[1] != "K"]
        black_non_king = [(r, c, p) for r, c, p in black_pieces if p[1] != "K"]

        # K vs K
        if not white_non_king and not black_non_king:
            return True

        # K+B vs K or K+N vs K
        if len(white_non_king) == 1 and not black_non_king:
            if white_non_king[0][2][1] in ("B", "N"):
                return True
        if len(black_non_king) == 1 and not white_non_king:
            if black_non_king[0][2][1] in ("B", "N"):
                return True

        # K+B vs K+B (same colored bishops)
        if len(white_non_king) == 1 and len(black_non_king) == 1:
            wp = white_non_king[0][2]
            bp = black_non_king[0][2]
            if wp[1] == "B" and bp[1] == "B":
                # Check if bishops are on same color
                wr, wc = white_non_king[0][0], white_non_king[0][1]
                br, bc = black_non_king[0][0], black_non_king[0][1]
                if (wr + wc) % 2 == (br + bc) % 2:
                    return True

        return False