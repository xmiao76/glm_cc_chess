"""Tests for moves.py — move generation, legality, check, checkmate, stalemate."""

import pytest
from src.board import Board, STARTING_FEN
from src.moves import (
    generate_pseudo_legal_moves,
    generate_legal_moves,
    is_in_check,
    is_checkmate,
    is_stalemate,
    move_to_algebraic,
    move_to_uci,
    uci_to_move,
)


class TestPseudoLegalMovesStarting:
    def test_starting_position_white(self):
        board = Board.from_fen(STARTING_FEN)
        moves = generate_pseudo_legal_moves(board, "w")
        # White has 20 moves in starting position: 16 pawn moves + 4 knight moves
        assert len(moves) == 20

    def test_starting_position_black(self):
        board = Board.from_fen(STARTING_FEN)
        moves = generate_pseudo_legal_moves(board, "b")
        assert len(moves) == 20


class TestPawnMoves:
    def test_white_pawn_single_push(self):
        board = Board.from_fen("8/8/8/8/8/8/P7/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        pawn_moves = [m for m in moves if board.get_piece(m[0], m[1]) == "wP"]
        assert (6, 0, 5, 0, None) in pawn_moves

    def test_white_pawn_double_push(self):
        board = Board.from_fen("8/8/8/8/8/8/P7/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        pawn_moves = [m for m in moves if board.get_piece(m[0], m[1]) == "wP"]
        assert (6, 0, 4, 0, None) in pawn_moves

    def test_pawn_single_push_only_when_blocked(self):
        # White pawn on a2, black pawn on a4 — single push to a3 still possible
        board = Board.from_fen("8/8/8/8/p7/8/P7/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        pawn_moves = [m for m in moves if m[0] == 6 and m[1] == 0]
        # Can push to a3 (single push), but not a4 (blocked by black pawn)
        assert (6, 0, 5, 0, None) in pawn_moves
        assert (6, 0, 4, 0, None) not in pawn_moves

    def test_pawn_fully_blocked(self):
        # White pawn on a2, black pawn on a3 — completely blocked
        board = Board.from_fen("8/8/8/8/8/p7/P7/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        pawn_moves = [m for m in moves if m[0] == 6 and m[1] == 0]
        assert len(pawn_moves) == 0

    def test_pawn_capture(self):
        board = Board.from_fen("8/8/8/8/8/1p6/P7/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        assert (6, 0, 5, 1, None) in moves

    def test_pawn_promotion(self):
        board = Board.from_fen("8/4P3/8/8/8/8/8/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        # Pawn on e7 promotes when pushing to e8
        promo_moves = [m for m in moves if m[0] == 1 and m[1] == 4 and m[2] == 0]
        assert len(promo_moves) == 4  # Q, R, B, N
        promo_types = {m[4] for m in promo_moves}
        assert promo_types == {"Q", "R", "B", "N"}

    def test_en_passant(self):
        board = Board.from_fen("8/8/8/4pP2/8/8/8/8 w - e6 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        assert (3, 5, 2, 4, None) in moves

    def test_black_pawn_direction(self):
        board = Board.from_fen("8/p7/8/8/8/8/8/8 b - - 0 1")
        moves = generate_pseudo_legal_moves(board, "b")
        assert (1, 0, 2, 0, None) in moves
        assert (1, 0, 3, 0, None) in moves


class TestKnightMoves:
    def test_knight_from_center(self):
        # Knight on e5 (row 3, col 4)
        board = Board.from_fen("8/8/8/4N3/8/8/8/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        knight_moves = [(m[2], m[3]) for m in moves if m[0] == 3 and m[1] == 4]
        assert len(knight_moves) == 8

    def test_knight_in_corner(self):
        board = Board.from_fen("8/8/8/8/8/8/8/N7 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        knight_moves = [(m[2], m[3]) for m in moves]
        assert len(knight_moves) == 2

    def test_knight_cannot_move_to_friendly(self):
        board = Board.from_fen("8/8/8/8/8/8/8/N6P w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        knight_destinations = [(m[2], m[3]) for m in moves if m[0] == 7 and m[1] == 0]
        # Knight at a1 can go to b3 (5,1) and c2 (6,2) — NOT to h1 where white pawn is
        assert (5, 1) in knight_destinations  # b3
        assert (6, 2) in knight_destinations  # c2


class TestSlidingPieces:
    def test_rook_from_corner(self):
        board = Board.from_fen("8/8/8/8/8/8/8/R7 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        rook_moves = [(m[2], m[3]) for m in moves if m[0] == 7 and m[1] == 0]
        assert len(rook_moves) == 14  # 7 along rank + 7 along file

    def test_bishop_blocked_by_own_piece(self):
        board = Board.from_fen("8/8/8/8/8/8/8/B6P w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        bishop_moves = [(m[2], m[3]) for m in moves if m[0] == 7 and m[1] == 0]
        assert len(bishop_moves) > 0

    def test_queen_combines_rook_and_bishop(self):
        board = Board.from_fen("8/8/8/8/8/8/8/Q7 w - - 0 1")
        queen_moves = generate_pseudo_legal_moves(board, "w")
        assert len(queen_moves) == 21

    def test_rook_captures_enemy(self):
        board = Board.from_fen("8/8/8/8/8/8/8/Rp6 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        rook_captures = [m for m in moves if m[0] == 7 and m[1] == 0 and m[2] == 7 and m[3] == 1]
        assert len(rook_captures) == 1


class TestKingMoves:
    def test_king_from_center(self):
        # King on e4 (row 4, col 4)
        board = Board.from_fen("8/8/8/8/4K3/8/8/8 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        king_moves = [(m[2], m[3]) for m in moves if m[0] == 4 and m[1] == 4]
        assert len(king_moves) == 8

    def test_king_at_edge(self):
        board = Board.from_fen("8/8/8/8/8/8/8/K7 w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        king_moves = [(m[2], m[3]) for m in moves if m[0] == 7 and m[1] == 0]
        assert len(king_moves) == 3  # b1, b2, a2


class TestCastling:
    def test_white_kingside_castling(self):
        board = Board.from_fen("8/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        king_moves = [m for m in moves if m[0] == 7 and m[1] == 4]
        assert (7, 4, 7, 6, None) in king_moves

    def test_white_queenside_castling(self):
        board = Board.from_fen("8/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        king_moves = [m for m in moves if m[0] == 7 and m[1] == 4]
        assert (7, 4, 7, 2, None) in king_moves

    def test_castling_blocked_by_piece(self):
        board = Board.from_fen("8/8/8/8/8/8/8/RNKCK2R w KQ - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        king_moves = [m for m in moves if m[0] == 7 and m[1] == 3]
        castling_moves = [m for m in king_moves if abs(m[3] - m[1]) == 2]
        assert len(castling_moves) == 0

    def test_castling_through_check(self):
        # Black rook on f8 attacks f1 — no kingside castling through f1
        board = Board.from_fen("5r2/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        moves = generate_legal_moves(board, "w")
        king_moves = [m for m in moves if m[0] == 7 and m[1] == 4 and abs(m[3] - 4) == 2]
        # King shouldn't castle through f1 which is attacked by rook on f8
        assert (7, 4, 7, 6, None) not in king_moves

    def test_no_castling_when_lost_rights(self):
        board = Board.from_fen("8/8/8/8/8/8/8/R3K2R w - - 0 1")
        moves = generate_pseudo_legal_moves(board, "w")
        king_moves = [m for m in moves if m[0] == 7 and m[1] == 4 and abs(m[3] - 4) == 2]
        assert len(king_moves) == 0


class TestLegalMoves:
    def test_legal_moves_filter_check(self):
        # White king on e1, black rook on e8 — king can't move to e2 (still in check)
        board = Board.from_fen("4r3/8/8/8/8/8/8/4K3 w - - 0 1")
        legal = generate_legal_moves(board, "w")
        e2_moves = [m for m in legal if m[2] == 6 and m[3] == 4]  # e2
        assert len(e2_moves) == 0

    def test_pinned_piece_cannot_leave_pin(self):
        # King on e1, Rook on e2 pinned by black rook on e8
        board = Board.from_fen("4r3/8/8/8/8/8/4R3/4K3 w - - 0 1")
        legal = generate_legal_moves(board, "w")
        rook_moves = [m for m in legal if m[0] == 6 and m[1] == 4]
        # Rook can only move on e-file (staying on the pin line)
        for m in rook_moves:
            assert m[3] == 4


class TestCheck:
    def test_king_in_check(self):
        board = Board.from_fen("4k3/8/8/8/8/8/8/4R3 w - - 0 1")
        assert is_in_check(board, "b")

    def test_king_not_in_check(self):
        board = Board.from_fen(STARTING_FEN)
        assert not is_in_check(board, "w")
        assert not is_in_check(board, "b")


class TestCheckmate:
    def test_scholars_mate(self):
        board = Board.from_fen("r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4")
        assert is_checkmate(board, "b")

    def test_not_checkmate_if_can_escape(self):
        board = Board.from_fen(STARTING_FEN)
        assert not is_checkmate(board, "w")

    def test_back_rank_mate(self):
        # Rook on d8 gives check to king on g8, pawns block escape
        board = Board.from_fen("3R2k1/5ppp/8/8/8/8/8/4K3 b - - 0 1")
        assert is_checkmate(board, "b")


class TestStalemate:
    def test_stalemate_position(self):
        board = Board.from_fen("k7/2Q5/8/8/8/8/8/4K3 b - - 0 1")
        assert is_stalemate(board, "b")

    def test_not_stalemate_with_legal_moves(self):
        board = Board.from_fen(STARTING_FEN)
        assert not is_stalemate(board, "w")


class TestMoveNotation:
    def test_pawn_move(self):
        board = Board.from_fen(STARTING_FEN)
        move = (6, 4, 4, 4, None)
        notation = move_to_algebraic(board, move)
        assert notation == "e4"

    def test_knight_move(self):
        board = Board.from_fen(STARTING_FEN)
        move = (7, 6, 5, 5, None)
        notation = move_to_algebraic(board, move)
        assert notation == "Nf3"

    def test_castling_kingside(self):
        board = Board.from_fen("8/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        move = (7, 4, 7, 6, None)
        notation = move_to_algebraic(board, move)
        assert notation == "O-O"

    def test_castling_queenside(self):
        board = Board.from_fen("8/8/8/8/8/8/8/R3K2R w KQ - 0 1")
        move = (7, 4, 7, 2, None)
        notation = move_to_algebraic(board, move)
        assert notation == "O-O-O"


class TestUciConversion:
    """Round-trip and edge-case tests for move_to_uci / uci_to_move."""

    def test_pawn_move(self):
        assert move_to_uci((6, 4, 4, 4, None)) == "e2e4"
        assert uci_to_move("e2e4") == (6, 4, 4, 4, None)

    def test_knight_move(self):
        # Ng1-f3 = (7,6) -> (5,5)
        assert move_to_uci((7, 6, 5, 5, None)) == "g1f3"
        assert uci_to_move("g1f3") == (7, 6, 5, 5, None)

    def test_castling_kingside(self):
        # White O-O: e1g1 (king from e1 to g1)
        assert move_to_uci((7, 4, 7, 6, None)) == "e1g1"
        assert uci_to_move("e1g1") == (7, 4, 7, 6, None)

    def test_castling_queenside(self):
        # White O-O-O: e1c1
        assert move_to_uci((7, 4, 7, 2, None)) == "e1c1"
        assert uci_to_move("e1c1") == (7, 4, 7, 2, None)

    def test_promotion_lowercase_in_uci(self):
        # Internal promo is uppercase "Q"; UCI is lowercase "q"
        assert move_to_uci((1, 4, 0, 4, "Q")) == "e7e8q"
        assert move_to_uci((1, 4, 0, 4, "R")) == "e7e8r"
        assert uci_to_move("e7e8q") == (1, 4, 0, 4, "Q")
        assert uci_to_move("a7a8n") == (1, 0, 0, 0, "N")

    def test_promotion_capture(self):
        # Pawn on b7 captures a8 promoting: b7a8q
        assert uci_to_move("b7a8q") == (1, 1, 0, 0, "Q")

    def test_en_passant_is_plain_from_to(self):
        # En passant in UCI is just the pawn's from-to (e5d6); make_move infers it.
        assert uci_to_move("e5d6") == (3, 4, 2, 3, None)
        assert move_to_uci((3, 4, 2, 3, None)) == "e5d6"

    def test_roundtrip_random_legal_moves(self):
        """Every legal move in several positions round-trips through UCI."""
        positions = [
            STARTING_FEN,
            "r1bqkbnr/pppppppp/2n5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 1 2",
            "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1",
        ]
        for fen in positions:
            board = Board.from_fen(fen)
            for move in generate_legal_moves(board, board.active_color):
                uci = move_to_uci(move)
                # UCI length is 4 (quiet/castle/ep) or 5 (promotion)
                assert len(uci) in (4, 5)
                assert uci_to_move(uci) == move

    def test_uci_applied_matches_internal_move(self):
        """Applying a UCI move via uci_to_move + GameState matches the move."""
        from src.game import GameState
        for fen in [STARTING_FEN, "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"]:
            board = Board.from_fen(fen)
            for move in generate_legal_moves(board, board.active_color):
                g_direct = GameState(Board.from_fen(fen))
                g_uci = GameState(Board.from_fen(fen))
                g_direct.make_move(move)
                g_uci.make_move(uci_to_move(move_to_uci(move)))
                assert g_direct.board.to_fen() == g_uci.board.to_fen()

    def test_null_move_rejected(self):
        with pytest.raises(ValueError):
            uci_to_move("0000")
        with pytest.raises(ValueError):
            uci_to_move("")