"""UCI (Universal Chess Interface) protocol entry point for the built-in engine.

Run with::

    python -m src.uci

stdout carries ONLY valid UCI protocol output (``id``, ``uciok``, ``readyok``,
``bestmove``). All diagnostics go to stderr via the ``logging`` module, so a UCI
host such as lichess-bot can parse stdout cleanly.

Supported commands: ``uci``, ``isready``, ``ucinewgame``, ``setoption``,
``debug``, ``position [startpos | fen ...] [moves ...]``, ``go movetime N``,
``go depth N``, ``go wtime ... btime ...``, ``go infinite``, ``stop``, ``quit``.
"""

from __future__ import annotations

import logging
import sys
from typing import IO, Optional

from src.board import Board, STARTING_FEN
from src.game import GameState
from src.moves import uci_to_move, move_to_uci, generate_legal_moves
from src.engine import choose_move

logger = logging.getLogger(__name__)

ENGINE_NAME = "GLMCCChessEngine"
ENGINE_AUTHOR = "Joe"
DEFAULT_MOVETIME_MS = 1000
DEFAULT_MAX_DEPTH = 20
# Null move sentinel per the UCI convention (no legal move available).
NULL_MOVE = "0000"


class UCIEngine:
    """A minimal UCI-protocol engine driven over stdin/stdout."""

    def __init__(self, outstream: IO[str] | None = None) -> None:
        self.game = GameState()
        self.out: IO[str] = outstream if outstream is not None else sys.stdout

    # --- output ---------------------------------------------------------

    def send(self, line: str) -> None:
        """Write one UCI line to stdout (or the configured outstream)."""
        print(line, file=self.out, flush=True)

    # --- state ----------------------------------------------------------

    def reset(self) -> None:
        """Reset to the starting position (``ucinewgame``)."""
        self.game = GameState(Board.from_fen(STARTING_FEN))

    def set_position(self, tokens: list[str]) -> None:
        """Handle ``position [startpos | fen <fen>] [moves <uci>...]``."""
        if not tokens:
            return
        if tokens[0] == "startpos":
            board = Board.from_fen(STARTING_FEN)
            move_start = 1
        elif tokens[0] == "fen":
            fen_fields: list[str] = []
            j = 1
            while j < len(tokens) and len(fen_fields) < 6 and tokens[j] != "moves":
                fen_fields.append(tokens[j])
                j += 1
            try:
                board = Board.from_fen(" ".join(fen_fields))
            except (ValueError, IndexError) as exc:
                logger.warning("bad FEN in position command: %r (%s)", fen_fields, exc)
                return
            move_start = j
        else:
            logger.debug("unknown position variant: %s", tokens[0])
            return

        if move_start < len(tokens) and tokens[move_start] == "moves":
            move_start += 1

        self.game = GameState(board)
        for uci in tokens[move_start:]:
            if not uci:
                continue
            try:
                self.game.make_move(uci_to_move(uci))
            except (ValueError, IndexError):
                logger.warning("could not apply UCI move %r", uci)
                break

    # --- search ---------------------------------------------------------

    @staticmethod
    def _safe_int(value: str) -> Optional[int]:
        """Parse an int, returning ``None`` on failure (never raises)."""
        try:
            return int(value)
        except ValueError:
            return None

    def parse_go(self, tokens: list[str]) -> tuple[Optional[int], Optional[int]]:
        """Parse a ``go`` command into ``(time_limit_ms, max_depth)``.

        Returns ``(None, None)`` semantics: if neither movetime nor depth is
        given, a clock-based budget (or a default) is computed for our side.
        Malformed numeric tokens are skipped with a warning rather than raising
        ``ValueError`` and killing the engine process.
        """
        time_limit_ms: Optional[int] = None
        max_depth: Optional[int] = None
        wtime = btime = winc = binc = None
        infinite = False

        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in ("movetime", "depth", "wtime", "btime", "winc", "binc") \
                    and i + 1 < len(tokens):
                value = self._safe_int(tokens[i + 1])
                if value is None:
                    logger.warning("bad %s value %r in go", t, tokens[i + 1])
                    i += 2
                    continue
                if t == "movetime":
                    time_limit_ms = value
                elif t == "depth":
                    max_depth = value
                elif t == "wtime":
                    wtime = value
                elif t == "btime":
                    btime = value
                elif t == "winc":
                    winc = value
                elif t == "binc":
                    binc = value
                i += 2
            elif t == "infinite":
                infinite = True
                i += 1
            else:
                i += 1

        if time_limit_ms is None and max_depth is None:
            color = self.game.board.active_color
            remaining = wtime if color == "w" else btime
            inc = winc if color == "w" else binc
            if remaining is not None and remaining > 0:
                time_limit_ms = max(200, min(remaining // 20 + (inc or 0) // 2, 5000))
            else:
                time_limit_ms = DEFAULT_MOVETIME_MS

        if infinite:
            # No hard time limit; rely on the default depth ceiling.
            time_limit_ms = None
            max_depth = DEFAULT_MAX_DEPTH

        return time_limit_ms, max_depth

    def go(self, tokens: list[str]) -> None:
        """Handle ``go ...`` -> emit ``bestmove <uci>`` (or ``bestmove 0000``)."""
        legal = generate_legal_moves(self.game.board, self.game.board.active_color)
        if not legal:
            self.send(f"bestmove {NULL_MOVE}")
            return
        time_limit_ms, max_depth = self.parse_go(tokens)
        move = choose_move(self.game.board, time_limit_ms=time_limit_ms, max_depth=max_depth)
        if move is None:
            self.send(f"bestmove {NULL_MOVE}")
            return
        self.send(f"bestmove {move_to_uci(move)}")

    # --- main loop ------------------------------------------------------

    def loop(self, instream: IO[str] | None = None) -> None:
        """Read and dispatch UCI commands until ``quit`` or EOF."""
        instream = instream if instream is not None else sys.stdin
        for raw in instream:
            line = raw.strip()
            if not line:
                continue
            logger.debug("uci-in: %s", line)
            tokens = line.split()
            cmd = tokens[0]

            if cmd == "uci":
                self.send(f"id name {ENGINE_NAME}")
                self.send(f"id author {ENGINE_AUTHOR}")
                self.send("uciok")
            elif cmd == "isready":
                self.send("readyok")
            elif cmd == "ucinewgame":
                self.reset()
            elif cmd == "setoption":
                pass  # accepted and ignored — no tunable options exposed
            elif cmd == "debug":
                pass  # debug state is a no-op; logs always go to stderr
            elif cmd == "position":
                self.set_position(tokens[1:])
            elif cmd == "go":
                self.go(tokens[1:])
            elif cmd == "stop":
                # The search is synchronous, so nothing is in flight to abort.
                # The command is accepted per the UCI protocol; a real stop
                # would require an interruptible search, out of scope here.
                pass
            elif cmd == "quit":
                return
            elif cmd == "register":
                self.send("registration checking")  # no registration required
            else:
                logger.debug("unknown uci command: %s", cmd)


def main() -> None:
    """Entry point for ``python -m src.uci``."""
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # UCI lines must be '\n'-terminated; Windows text stdout would emit '\r\n'.
    try:
        sys.stdout.reconfigure(newline="\n")
    except (AttributeError, ValueError):
        pass
    UCIEngine().loop()


if __name__ == "__main__":
    main()