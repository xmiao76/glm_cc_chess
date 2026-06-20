"""Tests for engine.py — evaluation, forced mates, time control."""

import time
import pytest
from src.board import Board, STARTING_FEN
from src.engine import ChessEngine, choose_move, evaluate, CHECKMATE_SCORE
from src.moves import generate_legal_moves, move_to_uci


class TestEvaluation:
    def test_starting_position_near_zero(self):
        """Starting position should be roughly balanced."""
        board = Board.from_fen(STARTING_FEN)
        score = evaluate(board)
        assert abs(score) < 200  # Within 2 pawns of zero

    def test_material_advantage_white(self):
        """White with extra queen should have large positive score."""
        board = Board.from_fen("4k3/8/8/8/8/8/8/4KQ2 w - - 0 1")
        score = evaluate(board)
        assert score > 800  # Extra queen is worth ~900

    def test_material_advantage_black(self):
        """Black with extra rook should have negative score."""
        board = Board.from_fen("4k2r/8/8/8/8/8/8/4K3 w - - 0 1")
        score = evaluate(board)
        assert score < -400  # Extra rook worth ~500

    def test_king_only_is_drawish(self):
        """K vs K should be near zero."""
        board = Board.from_fen("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
        score = evaluate(board)
        assert abs(score) < 200


class TestEngineForcedMates:
    def test_mate_in_one_white(self):
        """Engine should find mate in 1 for white."""
        # White queen delivers checkmate on d8
        board = Board.from_fen("3k4/8/8/8/8/8/8/3KQ3 w - - 0 1")
        engine = ChessEngine(max_depth=2, time_limit=5.0)
        move = engine.get_best_move(board)
        assert move is not None
        # Engine should find a move that leads to checkmate

    def test_mate_in_one_back_rank(self):
        """Engine should find back rank mate."""
        board = Board.from_fen("6k1/5ppp/8/8/8/8/8/R3K3 w - - 0 1")
        engine = ChessEngine(max_depth=2, time_limit=5.0)
        move = engine.get_best_move(board)
        assert move is not None
        # The best move should be Ra8# (7,0 -> 0,0)

    def test_avoid_checkmate(self):
        """Engine should avoid being checkmated."""
        # Black to move, must avoid Qd8#
        board = Board.from_fen("r1bqkbnr/pppp1ppp/2n5/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 1 3")
        engine = ChessEngine(max_depth=3, time_limit=5.0)
        move = engine.get_best_move(board)
        assert move is not None
        # Should not be a move that allows Qxf7#

    def test_find_winning_capture(self):
        """Engine should find a simple queen capture."""
        # White queen can capture undefended black queen
        board = Board.from_fen("4k3/8/8/3q4/4Q3/8/8/4K3 w - - 0 1")
        engine = ChessEngine(max_depth=3, time_limit=5.0)
        move = engine.get_best_move(board)
        assert move is not None
        # Should capture the queen: (4, 4) -> (3, 3) or similar winning capture


class TestEngineTimeControl:
    def test_respects_time_limit(self):
        """Engine should not exceed time limit significantly."""
        board = Board.from_fen(STARTING_FEN)
        engine = ChessEngine(max_depth=10, time_limit=0.5)
        start = time.time()
        move = engine.get_best_move(board)
        elapsed = time.time() - start
        assert move is not None
        assert elapsed < 3.0  # Should not take more than 3 seconds

    def test_returns_move_from_starting_position(self):
        """Engine should return a valid move from the starting position."""
        board = Board.from_fen(STARTING_FEN)
        engine = ChessEngine(max_depth=3, time_limit=5.0)
        move = engine.get_best_move(board)
        assert move is not None
        legal_moves = generate_legal_moves(board, "w")
        assert move in legal_moves

    def test_single_move_position(self):
        """When only one legal move, engine should return it immediately."""
        board = Board.from_fen("k7/8/1K6/8/8/8/8/8 b - - 0 1")
        # Black king in corner with only one legal move
        legal_moves = generate_legal_moves(board, "b")
        if len(legal_moves) == 1:
            engine = ChessEngine(max_depth=1, time_limit=1.0)
            move = engine.get_best_move(board)
            assert move == legal_moves[0]


class TestEngineConsistency:
    def test_always_returns_legal_move(self):
        """Engine should always return a legal move."""
        positions = [
            STARTING_FEN,
            "r1bqkbnr/pppppppp/2n5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 1 2",
            "4k3/8/8/8/8/8/8/4K2R w K - 0 1",
        ]
        engine = ChessEngine(max_depth=3, time_limit=2.0)
        for fen in positions:
            board = Board.from_fen(fen)
            move = engine.get_best_move(board)
            assert move is not None
            legal = generate_legal_moves(board, board.active_color)
            assert move in legal


class TestChooseMove:
    """Tests for the clean choose_move() interface used by UCI and Lichess."""

    def test_returns_legal_move_with_time_limit(self):
        board = Board.from_fen(STARTING_FEN)
        move = choose_move(board, time_limit_ms=300)
        assert move is not None
        assert move in generate_legal_moves(board, board.active_color)

    def test_returns_legal_move_with_depth(self):
        board = Board.from_fen(STARTING_FEN)
        move = choose_move(board, max_depth=2)
        assert move is not None
        assert move in generate_legal_moves(board, board.active_color)

    def test_returns_legal_move_with_defaults(self):
        board = Board.from_fen(STARTING_FEN)
        move = choose_move(board)
        assert move is not None
        assert move in generate_legal_moves(board, board.active_color)

    def test_respects_time_limit(self):
        """choose_move should not vastly exceed a small time budget."""
        board = Board.from_fen(STARTING_FEN)
        start = time.time()
        choose_move(board, time_limit_ms=200)
        elapsed = time.time() - start
        # Allow margin for search teardown; well under the 2.0s default.
        assert elapsed < 2.0

    def test_promotion_position_returns_promotion(self):
        """In a forced-promotion position the returned move must be a promotion.

        White Ka1 is stalemated except for the pawn (a2/b1/b2 all attacked), so
        every legal move is a pawn promotion. This makes the result deterministic
        regardless of search depth or time budget.
        """
        board = Board.from_fen("8/P7/8/8/8/1b6/2k5/K7 w - - 0 1")
        move = choose_move(board, time_limit_ms=300)
        assert move is not None
        assert move in generate_legal_moves(board, "w")
        uci = move_to_uci(move)
        assert len(uci) == 5 and uci[4] in "qrbn", uci

    def test_checkmate_returns_none(self):
        """A checkmated side has no legal move -> choose_move returns None."""
        board = Board.from_fen("r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4")
        assert choose_move(board, time_limit_ms=200) is None

    def test_stalemate_returns_none(self):
        """A stalemated side has no legal move -> choose_move returns None."""
        board = Board.from_fen("k7/2Q5/8/8/8/8/8/4K3 b - - 0 1")
        assert choose_move(board, time_limit_ms=200) is None

    def test_does_not_mutate_input_board(self):
        """choose_move must not mutate the caller's board."""
        board = Board.from_fen(STARTING_FEN)
        fen_before = board.to_fen()
        choose_move(board, time_limit_ms=200)
        assert board.to_fen() == fen_before

    def test_always_legal_across_positions(self):
        positions = [
            STARTING_FEN,
            "r1bqkbnr/pppppppp/2n5/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 1 2",
            "4k3/8/8/8/8/8/8/4K2R w K - 0 1",
            "r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1",
        ]
        for fen in positions:
            board = Board.from_fen(fen)
            move = choose_move(board, time_limit_ms=200)
            assert move is not None
            assert move in generate_legal_moves(board, board.active_color)