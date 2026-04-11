"""Board representation for the chess engine.

Uses an 8x8 array with piece codes like 'wP' (white pawn), 'bK' (black king).
Square indexing: board[row][col] where row 0 = rank 8 (black side), row 7 = rank 1 (white side).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

PIECE_TYPES = ["P", "N", "B", "R", "Q", "K"]
COLORS = ["w", "b"]
PIECE_VALUES = {"P": 100, "N": 320, "B": 330, "R": 500, "Q": 900, "K": 20000}

STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class Board:
    """Chess board with piece positions and game state."""

    STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    def __init__(self) -> None:
        self.squares: list[list[Optional[str]]] = [[None] * 8 for _ in range(8)]
        self.active_color: str = "w"
        self.castling_rights: str = "KQkq"
        self.en_passant_square: Optional[tuple[int, int]] = None
        self.halfmove_clock: int = 0
        self.fullmove_number: int = 1

    def copy(self) -> Board:
        """Create a deep copy of the board."""
        new = Board()
        new.squares = [row[:] for row in self.squares]
        new.active_color = self.active_color
        new.castling_rights = self.castling_rights
        new.en_passant_square = self.en_passant_square
        new.halfmove_clock = self.halfmove_clock
        new.fullmove_number = self.fullmove_number
        return new

    @staticmethod
    def from_fen(fen: str) -> Board:
        """Parse a FEN string and return a Board."""
        board = Board()
        parts = fen.split()

        # Piece placement
        rows = parts[0].split("/")
        for row_idx, row_str in enumerate(rows):
            col = 0
            for ch in row_str:
                if ch.isdigit():
                    col += int(ch)
                else:
                    color = "w" if ch.isupper() else "b"
                    piece_type = ch.upper()
                    board.squares[row_idx][col] = color + piece_type
                    col += 1

        # Active color
        board.active_color = parts[1]

        # Castling rights
        board.castling_rights = parts[2] if parts[2] != "-" else ""

        # En passant square
        if parts[3] != "-":
            ep_col = ord(parts[3][0]) - ord("a")
            ep_row = 8 - int(parts[3][1])
            board.en_passant_square = (ep_row, ep_col)
        else:
            board.en_passant_square = None

        # Halfmove clock and fullmove number
        board.halfmove_clock = int(parts[4])
        board.fullmove_number = int(parts[5])

        return board

    def to_fen(self) -> str:
        """Generate FEN string from the current board state."""
        rows = []
        for row in self.squares:
            fen_row = ""
            empty = 0
            for piece in row:
                if piece is None:
                    empty += 1
                else:
                    if empty > 0:
                        fen_row += str(empty)
                        empty = 0
                    ch = piece[1]
                    fen_row += ch if piece[0] == "w" else ch.lower()
            if empty > 0:
                fen_row += str(empty)
            rows.append(fen_row)

        placement = "/".join(rows)
        color = self.active_color
        castling = self.castling_rights if self.castling_rights else "-"
        ep = "-"
        if self.en_passant_square is not None:
            r, c = self.en_passant_square
            ep = chr(ord("a") + c) + str(8 - r)

        return f"{placement} {color} {castling} {ep} {self.halfmove_clock} {self.fullmove_number}"

    def get_piece(self, row: int, col: int) -> Optional[str]:
        """Get the piece at a given square."""
        if 0 <= row < 8 and 0 <= col < 8:
            return self.squares[row][col]
        return None

    def set_piece(self, row: int, col: int, piece: Optional[str]) -> None:
        """Set a piece on a given square."""
        self.squares[row][col] = piece

    def find_king(self, color: str) -> Optional[tuple[int, int]]:
        """Find the position of the king for a given color."""
        king = color + "K"
        for r in range(8):
            for c in range(8):
                if self.squares[r][c] == king:
                    return (r, c)
        return None

    def get_all_pieces(self, color: str) -> list[tuple[int, int, str]]:
        """Get all pieces of a given color as (row, col, piece) tuples."""
        result = []
        for r in range(8):
            for c in range(8):
                piece = self.squares[r][c]
                if piece is not None and piece[0] == color:
                    result.append((r, c, piece))
        return result

    def is_on_board(self, row: int, col: int) -> bool:
        """Check if a position is on the board."""
        return 0 <= row < 8 and 0 <= col < 8

    def is_enemy(self, row: int, col: int, color: str) -> bool:
        """Check if a square has an enemy piece."""
        piece = self.get_piece(row, col)
        return piece is not None and piece[0] != color

    def is_empty(self, row: int, col: int) -> bool:
        """Check if a square is empty."""
        return self.get_piece(row, col) is None

    def is_friendly(self, row: int, col: int, color: str) -> bool:
        """Check if a square has a friendly piece."""
        piece = self.get_piece(row, col)
        return piece is not None and piece[0] == color

    @staticmethod
    def square_to_algebraic(row: int, col: int) -> str:
        """Convert (row, col) to algebraic notation like 'e4'."""
        return chr(ord("a") + col) + str(8 - row)

    @staticmethod
    def algebraic_to_square(sq: str) -> tuple[int, int]:
        """Convert algebraic notation like 'e4' to (row, col)."""
        col = ord(sq[0]) - ord("a")
        row = 8 - int(sq[1])
        return (row, col)

    def material_count(self, color: str) -> int:
        """Total material value for a color."""
        return sum(PIECE_VALUES[p[1]] for _, _, p in self.get_all_pieces(color))

    def __repr__(self) -> str:
        lines = []
        for r in range(8):
            row_str = ""
            for c in range(8):
                piece = self.squares[r][c]
                if piece is None:
                    row_str += ". "
                else:
                    row_str += piece + " "
            lines.append(f"{8 - r} {row_str}")
        lines.append("  a b c d e f g h")
        return "\n".join(lines)