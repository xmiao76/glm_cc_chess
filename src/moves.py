"""Move generation and legality checking for chess.

Generates pseudo-legal moves, then filters for legality (no leaving king in check).
Handles all special moves: castling, en passant, pawn promotion.
"""

from __future__ import annotations

from src.board import Board

# Move representation: (from_row, from_col, to_row, to_col, promotion_piece_or_None)
Move = tuple[int, int, int, int, str | None]


def generate_pseudo_legal_moves(board: Board, color: str) -> list[Move]:
    """Generate all pseudo-legal moves for a color (may leave king in check)."""
    moves: list[Move] = []
    for row, col, piece in board.get_all_pieces(color):
        piece_type = piece[1]
        if piece_type == "P":
            moves.extend(_pawn_moves(board, row, col, color))
        elif piece_type == "N":
            moves.extend(_knight_moves(board, row, col, color))
        elif piece_type == "B":
            moves.extend(_bishop_moves(board, row, col, color))
        elif piece_type == "R":
            moves.extend(_rook_moves(board, row, col, color))
        elif piece_type == "Q":
            moves.extend(_queen_moves(board, row, col, color))
        elif piece_type == "K":
            moves.extend(_king_moves(board, row, col, color))
    return moves


def generate_legal_moves(board: Board, color: str) -> list[Move]:
    """Generate all legal moves for a color (never leaves own king in check)."""
    pseudo = generate_pseudo_legal_moves(board, color)
    legal: list[Move] = []
    for move in pseudo:
        if _is_legal_move(board, move, color):
            legal.append(move)
    return legal


def is_in_check(board: Board, color: str) -> bool:
    """Check if the given color's king is in check."""
    king_pos = board.find_king(color)
    if king_pos is None:
        return False
    return _is_square_attacked(board, king_pos[0], king_pos[1], _opponent(color))


def is_checkmate(board: Board, color: str) -> bool:
    """Check if the given color is in checkmate."""
    if not is_in_check(board, color):
        return False
    return len(generate_legal_moves(board, color)) == 0


def is_stalemate(board: Board, color: str) -> bool:
    """Check if the given color is in stalemate."""
    if is_in_check(board, color):
        return False
    return len(generate_legal_moves(board, color)) == 0


# --- Internal helpers ---


def _opponent(color: str) -> str:
    return "b" if color == "w" else "w"


def _is_square_attacked(board: Board, row: int, col: int, by_color: str) -> bool:
    """Check if a square is attacked by any piece of by_color."""
    # Pawn attacks
    if by_color == "w":
        # White pawns attack diagonally upward (decreasing row)
        for dc in (-1, 1):
            r, c = row + 1, col + dc
            if board.is_on_board(r, c) and board.get_piece(r, c) == "wP":
                return True
    else:
        # Black pawns attack diagonally downward (increasing row)
        for dc in (-1, 1):
            r, c = row - 1, col + dc
            if board.is_on_board(r, c) and board.get_piece(r, c) == "bP":
                return True

    # Knight attacks
    knight = by_color + "N"
    for dr, dc in [(-2, -1), (-2, 1), (-1, -2), (-1, 2),
                   (1, -2), (1, 2), (2, -1), (2, 1)]:
        r, c = row + dr, col + dc
        if board.is_on_board(r, c) and board.get_piece(r, c) == knight:
            return True

    # King attacks
    king = by_color + "K"
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            r, c = row + dr, col + dc
            if board.is_on_board(r, c) and board.get_piece(r, c) == king:
                return True

    # Sliding pieces: bishop/queen (diagonals)
    bishop = by_color + "B"
    queen = by_color + "Q"
    for dr, dc in [(-1, -1), (-1, 1), (1, -1), (1, 1)]:
        r, c = row + dr, col + dc
        while board.is_on_board(r, c):
            piece = board.get_piece(r, c)
            if piece is not None:
                if piece == bishop or piece == queen:
                    return True
                break
            r += dr
            c += dc

    # Sliding pieces: rook/queen (straights)
    rook = by_color + "R"
    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        r, c = row + dr, col + dc
        while board.is_on_board(r, c):
            piece = board.get_piece(r, c)
            if piece is not None:
                if piece == rook or piece == queen:
                    return True
                break
            r += dr
            c += dc

    return False


def _is_legal_move(board: Board, move: Move, color: str) -> bool:
    """Check if making the move doesn't leave own king in check."""
    from_r, from_c, to_r, to_c, promo = move
    # Make the move on a copy
    new_board = board.copy()
    piece = new_board.squares[from_r][from_c]
    new_board.squares[from_r][from_c] = None

    if promo is not None:
        new_board.squares[to_r][to_c] = color + promo
    else:
        new_board.squares[to_r][to_c] = piece

    # En passant capture
    if piece is not None and piece[1] == "P" and from_c != to_c and board.is_empty(to_r, to_c):
        # This is an en passant capture
        new_board.squares[from_r][to_c] = None

    # Castling - move the rook too
    if piece is not None and piece[1] == "K":
        if abs(to_c - from_c) == 2:
            # Castling
            if to_c == 6:  # Kingside
                new_board.squares[from_r][7] = None
                new_board.squares[from_r][5] = color + "R"
            elif to_c == 2:  # Queenside
                new_board.squares[from_r][0] = None
                new_board.squares[from_r][3] = color + "R"

    return not is_in_check(new_board, color)


# --- Piece-specific move generators ---


def _pawn_moves(board: Board, row: int, col: int, color: str) -> list[Move]:
    moves: list[Move] = []
    direction = -1 if color == "w" else 1
    start_row = 6 if color == "w" else 1
    promo_row = 0 if color == "w" else 7

    # Single push
    to_r = row + direction
    if board.is_on_board(to_r, col) and board.is_empty(to_r, col):
        if to_r == promo_row:
            for p in ["Q", "R", "B", "N"]:
                moves.append((row, col, to_r, col, p))
        else:
            moves.append((row, col, to_r, col, None))

            # Double push from starting position
            to_r2 = row + 2 * direction
            if row == start_row and board.is_empty(to_r2, col):
                moves.append((row, col, to_r2, col, None))

    # Captures
    for dc in (-1, 1):
        to_c = col + dc
        if not board.is_on_board(to_r, to_c):
            continue
        if board.is_enemy(to_r, to_c, color):
            if to_r == promo_row:
                for p in ["Q", "R", "B", "N"]:
                    moves.append((row, col, to_r, to_c, p))
            else:
                moves.append((row, col, to_r, to_c, None))
        # En passant
        elif board.en_passant_square == (to_r, to_c):
            moves.append((row, col, to_r, to_c, None))

    return moves


def _knight_moves(board: Board, row: int, col: int, color: str) -> list[Move]:
    moves: list[Move] = []
    offsets = [(-2, -1), (-2, 1), (-1, -2), (-1, 2),
               (1, -2), (1, 2), (2, -1), (2, 1)]
    for dr, dc in offsets:
        to_r, to_c = row + dr, col + dc
        if board.is_on_board(to_r, to_c) and not board.is_friendly(to_r, to_c, color):
            moves.append((row, col, to_r, to_c, None))
    return moves


def _sliding_moves(board: Board, row: int, col: int, color: str,
                   directions: list[tuple[int, int]]) -> list[Move]:
    moves: list[Move] = []
    for dr, dc in directions:
        to_r, to_c = row + dr, col + dc
        while board.is_on_board(to_r, to_c):
            if board.is_empty(to_r, to_c):
                moves.append((row, col, to_r, to_c, None))
            elif board.is_enemy(to_r, to_c, color):
                moves.append((row, col, to_r, to_c, None))
                break
            else:  # friendly piece
                break
            to_r += dr
            to_c += dc
    return moves


def _bishop_moves(board: Board, row: int, col: int, color: str) -> list[Move]:
    return _sliding_moves(board, row, col, color,
                         [(-1, -1), (-1, 1), (1, -1), (1, 1)])


def _rook_moves(board: Board, row: int, col: int, color: str) -> list[Move]:
    return _sliding_moves(board, row, col, color,
                         [(-1, 0), (1, 0), (0, -1), (0, 1)])


def _queen_moves(board: Board, row: int, col: int, color: str) -> list[Move]:
    return _sliding_moves(board, row, col, color,
                         [(-1, -1), (-1, 1), (1, -1), (1, 1),
                          (-1, 0), (1, 0), (0, -1), (0, 1)])


def _king_moves(board: Board, row: int, col: int, color: str) -> list[Move]:
    moves: list[Move] = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            to_r, to_c = row + dr, col + dc
            if board.is_on_board(to_r, to_c) and not board.is_friendly(to_r, to_c, color):
                moves.append((row, col, to_r, to_c, None))

    # Castling
    if color == "w" and row == 7:
        if "K" in board.castling_rights:
            # White kingside castling
            if (board.is_empty(7, 5) and board.is_empty(7, 6)
                    and not _is_square_attacked(board, 7, 4, "b")
                    and not _is_square_attacked(board, 7, 5, "b")
                    and not _is_square_attacked(board, 7, 6, "b")):
                moves.append((7, 4, 7, 6, None))
        if "Q" in board.castling_rights:
            # White queenside castling
            if (board.is_empty(7, 3) and board.is_empty(7, 2) and board.is_empty(7, 1)
                    and not _is_square_attacked(board, 7, 4, "b")
                    and not _is_square_attacked(board, 7, 3, "b")
                    and not _is_square_attacked(board, 7, 2, "b")):
                moves.append((7, 4, 7, 2, None))
    elif color == "b" and row == 0:
        if "k" in board.castling_rights:
            # Black kingside castling
            if (board.is_empty(0, 5) and board.is_empty(0, 6)
                    and not _is_square_attacked(board, 0, 4, "w")
                    and not _is_square_attacked(board, 0, 5, "w")
                    and not _is_square_attacked(board, 0, 6, "w")):
                moves.append((0, 4, 0, 6, None))
        if "q" in board.castling_rights:
            # Black queenside castling
            if (board.is_empty(0, 3) and board.is_empty(0, 2) and board.is_empty(0, 1)
                    and not _is_square_attacked(board, 0, 4, "w")
                    and not _is_square_attacked(board, 0, 3, "w")
                    and not _is_square_attacked(board, 0, 2, "w")):
                moves.append((0, 4, 0, 2, None))

    return moves


def move_to_algebraic(board: Board, move: Move) -> str:
    """Convert a move to algebraic notation (e.g., 'Nf3', 'e4', 'O-O').

    This is a simplified version for display purposes.
    """
    from_r, from_c, to_r, to_c, promo = move
    piece = board.get_piece(from_r, from_c)

    if piece is None:
        return "????"

    piece_type = piece[1]

    # Castling
    if piece_type == "K" and abs(to_c - from_c) == 2:
        return "O-O" if to_c == 6 else "O-O-O"

    dest = Board.square_to_algebraic(to_r, to_c)

    # Pawn moves
    if piece_type == "P":
        notation = ""
        if from_c != to_c:
            # Capture
            notation += chr(ord("a") + from_c) + "x"
        notation += dest
        if promo:
            notation += "=" + promo
        return notation

    # Piece moves
    notation = piece_type
    # TODO: add disambiguation for multiple pieces of same type
    capture = board.get_piece(to_r, to_c)
    if capture is not None:
        notation += "x"
    notation += dest
    return notation


def move_to_uci(move: Move) -> str:
    """Convert an internal move to UCI notation (e.g. 'e2e4', 'e7e8q', 'e1g1').

    Castling is encoded as the king's from-to square (the UCI standard for
    non-Chess960), which matches how `_king_moves` emits castling and how
    `GameState.make_move` detects it (king moving two files).
    """
    from_r, from_c, to_r, to_c, promo = move
    uci = Board.square_to_algebraic(from_r, from_c) + Board.square_to_algebraic(to_r, to_c)
    if promo is not None:
        uci += promo.lower()
    return uci


def uci_to_move(uci: str) -> Move:
    """Convert UCI notation to an internal move.

    Only from/to/promotion are encoded here; castling, en passant, and
    promotion are inferred by `GameState.make_move` from the piece type and
    the from/to squares. The special UCI move '0000' (no move) is rejected.
    """
    uci = uci.strip()
    if uci in ("0000", ""):
        raise ValueError(f"Cannot convert null/empty UCI move: {uci!r}")
    from_sq, to_sq = uci[0:2], uci[2:4]
    from_r, from_c = Board.algebraic_to_square(from_sq)
    to_r, to_c = Board.algebraic_to_square(to_sq)
    promo = uci[4].upper() if len(uci) > 4 else None
    return (from_r, from_c, to_r, to_c, promo)