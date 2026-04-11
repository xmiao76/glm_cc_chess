"""Tests for board.py — board representation, FEN, piece operations."""

import pytest
from src.board import Board, STARTING_FEN, PIECE_VALUES


class TestBoardCreation:
    def test_empty_board(self):
        board = Board()
        for r in range(8):
            for c in range(8):
                assert board.squares[r][c] is None

    def test_starting_position_from_fen(self):
        board = Board.from_fen(STARTING_FEN)
        # White pieces on rank 1 (row 7)
        assert board.get_piece(7, 0) == "wR"
        assert board.get_piece(7, 1) == "wN"
        assert board.get_piece(7, 2) == "wB"
        assert board.get_piece(7, 3) == "wQ"
        assert board.get_piece(7, 4) == "wK"
        assert board.get_piece(7, 5) == "wB"
        assert board.get_piece(7, 6) == "wN"
        assert board.get_piece(7, 7) == "wR"
        # White pawns on rank 2 (row 6)
        for c in range(8):
            assert board.get_piece(6, c) == "wP"
        # Black pieces on rank 8 (row 0)
        assert board.get_piece(0, 0) == "bR"
        assert board.get_piece(0, 4) == "bK"
        # Black pawns on rank 7 (row 1)
        for c in range(8):
            assert board.get_piece(1, c) == "bP"
        # Empty in middle
        for r in range(2, 6):
            for c in range(8):
                assert board.get_piece(r, c) is None

    def test_starting_position_state(self):
        board = Board.from_fen(STARTING_FEN)
        assert board.active_color == "w"
        assert board.castling_rights == "KQkq"
        assert board.en_passant_square is None
        assert board.halfmove_clock == 0
        assert board.fullmove_number == 1


class TestFenRoundtrip:
    def test_starting_position_roundtrip(self):
        board = Board.from_fen(STARTING_FEN)
        assert board.to_fen() == STARTING_FEN

    def test_custom_position_roundtrip(self):
        fen = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
        board = Board.from_fen(fen)
        assert board.to_fen() == fen

    def test_en_passant_roundtrip(self):
        fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 2"
        board = Board.from_fen(fen)
        assert board.to_fen() == fen

    def test_reduced_castling_roundtrip(self):
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w Kq - 0 1"
        board = Board.from_fen(fen)
        assert board.to_fen() == fen

    def test_no_castling_roundtrip(self):
        fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w - - 0 1"
        board = Board.from_fen(fen)
        assert board.to_fen() == fen


class TestBoardOperations:
    def test_get_piece_out_of_bounds(self):
        board = Board()
        assert board.get_piece(-1, 0) is None
        assert board.get_piece(0, 8) is None

    def test_set_and_get_piece(self):
        board = Board()
        board.set_piece(4, 4, "wK")
        assert board.get_piece(4, 4) == "wK"
        board.set_piece(4, 4, None)
        assert board.get_piece(4, 4) is None

    def test_find_king(self):
        board = Board.from_fen(STARTING_FEN)
        assert board.find_king("w") == (7, 4)
        assert board.find_king("b") == (0, 4)

    def test_find_king_missing(self):
        board = Board()
        assert board.find_king("w") is None

    def test_get_all_pieces(self):
        board = Board.from_fen(STARTING_FEN)
        white_pieces = board.get_all_pieces("w")
        assert len(white_pieces) == 16
        black_pieces = board.get_all_pieces("b")
        assert len(black_pieces) == 16

    def test_is_on_board(self):
        board = Board()
        assert board.is_on_board(0, 0)
        assert board.is_on_board(7, 7)
        assert not board.is_on_board(-1, 0)
        assert not board.is_on_board(8, 0)

    def test_is_empty(self):
        board = Board.from_fen(STARTING_FEN)
        assert not board.is_empty(0, 0)  # Black rook
        assert board.is_empty(4, 4)  # Empty square

    def test_is_friendly(self):
        board = Board.from_fen(STARTING_FEN)
        assert board.is_friendly(7, 0, "w")  # White rook
        assert not board.is_friendly(0, 0, "w")  # Black rook

    def test_is_enemy(self):
        board = Board.from_fen(STARTING_FEN)
        assert board.is_enemy(0, 0, "w")  # Black rook from white perspective
        assert not board.is_enemy(7, 0, "w")  # White rook from white perspective

    def test_copy_independence(self):
        board = Board.from_fen(STARTING_FEN)
        copy = board.copy()
        copy.set_piece(4, 4, "wQ")
        assert board.get_piece(4, 4) is None  # Original unchanged
        assert copy.get_piece(4, 4) == "wQ"

    def test_square_to_algebraic(self):
        assert Board.square_to_algebraic(7, 0) == "a1"
        assert Board.square_to_algebraic(0, 7) == "h8"
        assert Board.square_to_algebraic(4, 4) == "e4"

    def test_algebraic_to_square(self):
        assert Board.algebraic_to_square("a1") == (7, 0)
        assert Board.algebraic_to_square("h8") == (0, 7)
        assert Board.algebraic_to_square("e4") == (4, 4)

    def test_material_count(self):
        board = Board.from_fen(STARTING_FEN)
        # Starting position: each side has 8 pawns (800), 2 rooks (1000),
        # 2 knights (640), 2 bishops (660), 1 queen (900), 1 king (20000)
        white_material = board.material_count("w")
        expected = 8 * 100 + 2 * 500 + 2 * 320 + 2 * 330 + 900 + 20000
        assert white_material == expected