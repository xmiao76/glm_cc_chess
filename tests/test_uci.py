"""Tests for src/uci.py — the UCI protocol entry point.

Driven via subprocess (`python -m src.uci`) to exercise the real stdin/stdout
loop. Asserts that stdout contains ONLY valid UCI tokens and that `bestmove`
results are legal moves (verified by replaying through the board/moves modules).
"""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from src.board import Board, STARTING_FEN
from src.moves import generate_legal_moves, uci_to_move
from src.uci import UCIEngine


def _run_uci(commands: list[str], timeout: int = 30) -> str:
    """Spawn `python -m src.uci`, feed commands, return stdout text."""
    proc = subprocess.run(
        [sys.executable, "-m", "src.uci"],
        input="\n".join(commands) + "\n",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.stdout


def _bestmove(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        if line.startswith("bestmove"):
            return line.split()[1]
    pytest.fail(f"no bestmove in output:\n{stdout}")


def _assert_pure_uci(stdout: str) -> None:
    """Every non-empty stdout line must start with a known UCI token."""
    allowed = {"id", "uciok", "readyok", "bestmove", "info", "registration"}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        assert line.split()[0] in allowed, f"non-UCI output on stdout: {line!r}"


class TestUCIHandshake:
    def test_uci_returns_id_and_uciok(self):
        out = _run_uci(["uci", "quit"])
        assert "id name GLMCCChessEngine" in out
        assert "id author Joe" in out
        assert "uciok" in out
        _assert_pure_uci(out)

    def test_isready_returns_readyok(self):
        out = _run_uci(["isready", "quit"])
        assert "readyok" in out
        _assert_pure_uci(out)

    def test_ucinewgame_is_accepted(self):
        out = _run_uci(["ucinewgame", "isready", "quit"])
        assert "readyok" in out


class TestUCIPosition:
    def test_position_startpos_moves_e2e4(self):
        # After 1.e4 the engine (black to move) must reply with a legal black move.
        out = _run_uci(["position startpos moves e2e4", "go movetime 200", "quit"])
        bm = _bestmove(out)
        assert bm != "0000"
        board = Board.from_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")
        assert uci_to_move(bm) in generate_legal_moves(board, "b")

    def test_position_fen_then_moves(self):
        out = _run_uci([
            "position fen rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1 moves e7e5",
            "go movetime 200",
            "quit",
        ])
        bm = _bestmove(out)
        assert bm != "0000"
        # White to move after 1.e4 e5
        board = Board.from_fen("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2")
        assert uci_to_move(bm) in generate_legal_moves(board, "w")


class TestUCISearch:
    def test_go_movetime_1000_returns_legal_bestmove(self):
        out = _run_uci(["position startpos", "go movetime 1000", "quit"])
        bm = _bestmove(out)
        assert bm != "0000"
        board = Board.from_fen(STARTING_FEN)
        assert uci_to_move(bm) in generate_legal_moves(board, "w")
        _assert_pure_uci(out)

    def test_go_depth_1_returns_legal_bestmove(self):
        out = _run_uci(["position startpos", "go depth 1", "quit"])
        bm = _bestmove(out)
        assert bm != "0000"
        board = Board.from_fen(STARTING_FEN)
        assert uci_to_move(bm) in generate_legal_moves(board, "w")

    def test_go_with_clock_returns_legal_bestmove(self):
        out = _run_uci([
            "position startpos",
            "go wtime 60000 btime 60000 winc 2000 binc 2000",
            "quit",
        ])
        bm = _bestmove(out)
        assert bm != "0000"
        board = Board.from_fen(STARTING_FEN)
        assert uci_to_move(bm) in generate_legal_moves(board, "w")


class TestUCIPromotion:
    def test_promotion_position_returns_promotion_move(self):
        # Forced-promotion position: every legal move is a pawn promotion, so
        # bestmove must be a 5-char promotion regardless of search depth.
        out = _run_uci([
            "position fen 8/P7/8/8/8/1b6/2k5/K7 w - - 0 1",
            "go movetime 300",
            "quit",
        ])
        bm = _bestmove(out)
        assert bm != "0000"
        assert len(bm) == 5 and bm[4] in "qrbn", bm


class TestUCITerminalPositions:
    def test_checkmate_returns_null_bestmove(self):
        # Black is checkmated (Scholar's mate); no legal move -> bestmove 0000.
        out = _run_uci([
            "position fen r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4",
            "go movetime 300",
            "quit",
        ])
        assert _bestmove(out) == "0000"

    def test_stalemate_returns_null_bestmove(self):
        out = _run_uci([
            "position fen k7/2Q5/8/8/8/8/8/4K3 b - - 0 1",
            "go movetime 300",
            "quit",
        ])
        assert _bestmove(out) == "0000"


class TestUCIPurity:
    def test_stdout_contains_only_uci_tokens(self):
        out = _run_uci([
            "uci", "isready", "ucinewgame",
            "position startpos moves e2e4 e7e5",
            "go movetime 200",
            "quit",
        ])
        _assert_pure_uci(out)


# ---------------------------------------------------------------------------
# In-process tests (also provide coverage for src/uci.py)
# ---------------------------------------------------------------------------


def _run_inprocess(commands: list[str]) -> list[str]:
    """Drive UCIEngine directly with StringIO streams (no subprocess)."""
    engine = UCIEngine(outstream=io.StringIO())
    engine.loop(instream=io.StringIO("\n".join(commands) + "\n"))
    return engine.out.getvalue().splitlines()


class TestUCIEngineInProcess:
    def test_uci_handshake(self):
        out = _run_inprocess(["uci", "quit"])
        assert "id name GLMCCChessEngine" in out
        assert "id author Joe" in out
        assert "uciok" in out

    def test_isready(self):
        assert "readyok" in _run_inprocess(["isready", "quit"])

    def test_ucinewgame_resets(self):
        # After ucinewgame, a go from startpos returns a legal white move.
        out = _run_inprocess(["ucinewgame", "go movetime 100", "quit"])
        bm = _bestmove("\n".join(out))
        assert uci_to_move(bm) in generate_legal_moves(Board.from_fen(STARTING_FEN), "w")

    def test_position_startpos_moves(self):
        out = _run_inprocess(["position startpos moves e2e4", "go movetime 100", "quit"])
        bm = _bestmove("\n".join(out))
        board = Board.from_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")
        assert uci_to_move(bm) in generate_legal_moves(board, "b")

    def test_position_fen(self):
        out = _run_inprocess([
            "position fen rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2",
            "go movetime 100", "quit",
        ])
        bm = _bestmove("\n".join(out))
        board = Board.from_fen("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2")
        assert uci_to_move(bm) in generate_legal_moves(board, "w")

    def test_go_depth(self):
        out = _run_inprocess(["position startpos", "go depth 1", "quit"])
        bm = _bestmove("\n".join(out))
        assert uci_to_move(bm) in generate_legal_moves(Board.from_fen(STARTING_FEN), "w")

    def test_go_clock_budget(self):
        out = _run_inprocess([
            "position startpos",
            "go wtime 60000 btime 60000 winc 2000 binc 2000", "quit",
        ])
        bm = _bestmove("\n".join(out))
        assert uci_to_move(bm) in generate_legal_moves(Board.from_fen(STARTING_FEN), "w")

    def test_go_infinite(self):
        out = _run_inprocess(["position startpos", "go infinite", "quit"])
        bm = _bestmove("\n".join(out))
        assert bm != "0000"

    def test_promotion_forced(self):
        out = _run_inprocess([
            "position fen 8/P7/8/8/8/1b6/2k5/K7 w - - 0 1",
            "go movetime 100", "quit",
        ])
        bm = _bestmove("\n".join(out))
        assert len(bm) == 5 and bm[4] in "qrbn"

    def test_checkmate_returns_null(self):
        out = _run_inprocess([
            "position fen r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4",
            "go movetime 100", "quit",
        ])
        assert _bestmove("\n".join(out)) == "0000"

    def test_stalemate_returns_null(self):
        out = _run_inprocess([
            "position fen k7/2Q5/8/8/8/8/8/4K3 b - - 0 1",
            "go movetime 100", "quit",
        ])
        assert _bestmove("\n".join(out)) == "0000"

    def test_setoption_debug_stop_register_accepted(self):
        # These commands must not crash and must not pollute stdout.
        out = _run_inprocess([
            "setoption name Foo value Bar", "debug on", "stop",
            "register", "isready", "quit",
        ])
        assert "readyok" in out
        _assert_pure_uci("\n".join(out))

    def test_unknown_command_ignored(self):
        out = _run_inprocess(["frobnicate", "isready", "quit"])
        assert "readyok" in out

    def test_parse_go_movetime(self):
        engine = UCIEngine()
        engine.set_position(["startpos"])
        assert engine.parse_go(["movetime", "1234"]) == (1234, None)

    def test_parse_go_depth(self):
        engine = UCIEngine()
        engine.set_position(["startpos"])
        assert engine.parse_go(["depth", "3"]) == (None, 3)

    def test_parse_go_clock(self):
        engine = UCIEngine()
        engine.set_position(["startpos"])
        time_ms, depth = engine.parse_go(["wtime", "60000", "btime", "60000",
                                          "winc", "0", "binc", "0"])
        # White to move: budget = 60000//20 = 3000
        assert time_ms == 3000 and depth is None

    def test_malformed_fen_does_not_crash_engine(self):
        # A short/malformed FEN must not kill the process; isready still works.
        out = _run_inprocess([
            "position fen not-a-fen",
            "isready",
            "quit",
        ])
        assert "readyok" in out

    def test_bad_go_int_does_not_crash_parse(self):
        engine = UCIEngine()
        engine.set_position(["startpos"])
        # Non-integer tokens are skipped (warned), not raised as ValueError.
        result = engine.parse_go(["movetime", "abc", "wtime", "xyz", "binc", "nope"])
        assert isinstance(result, tuple)
        # No valid movetime/depth/clock -> falls back to the default movetime.
        assert result[0] == 1000 and result[1] is None

    def test_bad_go_int_does_not_crash_engine(self):
        # Bad btime token is skipped; the valid movetime drives a fast search
        # and the engine keeps responding.
        out = _run_inprocess(["go movetime 50 btime xyz", "isready", "quit"])
        assert "readyok" in out
        assert any(l.startswith("bestmove") for l in out)