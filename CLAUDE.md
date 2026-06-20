# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`glm_cc_chess` is a Windows desktop chess application with a Pygame GUI and a built-in chess engine. A human can play against the engine, watch engine-vs-engine games, or let the engine play against opponents on Lichess via a dedicated BOT account (live board display + game review). The engine is also exposed through a UCI entry point.

## Tech Stack

- **Language**: Python 3.12+
- **GUI**: Pygame
- **Testing**: pytest + pytest-cov
- **Engine**: Custom minimax with alpha-beta pruning, iterative deepening, quiescence search

## Project Structure

```
src/
  board.py             — Board representation, FEN parsing, piece operations
  moves.py             — Move generation, legality, SAN + UCI move conversion
  game.py              — Game state controller, make/unmake, draw detection
  engine.py            — Chess engine (minimax + alpha-beta + quiescence) + choose_move()
  gui.py               — Pygame GUI (board, panel, Lichess mode, review)
  uci.py               — UCI protocol entry point (python -m src.uci)
  lichess_client.py    — Lichess BOT API HTTP client (stdlib urllib)
  lichess_controller.py— Threading + queue bridge between API streams and GUI
  main.py              — GUI entry point
lichess/
  config.yml.example   — lichess-bot config template (optional headless bridge)
  run_engine.bat       — Windows wrapper that runs `python -m src.uci`
  README_LICHESS.md    — Lichess integration guide
tests/
  test_board.py
  test_moves.py
  test_game.py
  test_engine.py
  test_uci.py
  test_lichess.py
```

## Commands

```bash
# Setup
python -m venv venv
source venv/Scripts/activate    # Windows (Git Bash)
pip install -e ".[dev]"

# Run the application
python -m src.main

# Run the UCI engine (for lichess-bot / any UCI host)
python -m src.uci

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=src --cov-report=term-missing

# Package the Windows release
python package.py
```

## Architecture

- **Board** (`src/board.py`): 8x8 array with piece codes (`wP`, `bK`, etc.), FEN parsing/generation
- **Moves** (`src/moves.py`): Pseudo-legal and legal move generation, check/checkmate/stalemate detection, SAN (`move_to_algebraic`) and UCI (`move_to_uci`/`uci_to_move`) conversion
- **Game** (`src/game.py`): Full game state with make/unmake, castling rights, en passant, draw rules
- **Engine** (`src/engine.py`): Evaluation with piece-square tables, search with alpha-beta + quiescence; `choose_move(board, time_limit_ms=None, max_depth=None)` is the clean interface shared by GUI/UCI/Lichess
- **GUI** (`src/gui.py`): Pygame board rendering, click-to-move, legal move highlighting, side panel; "AI vs Lichess" mode + game review
- **UCI** (`src/uci.py`): UCI protocol stdin/stdout loop (`python -m src.uci`); stdout is pure UCI, debug to stderr
- **LichessClient** (`src/lichess_client.py`): Lichess BOT API over stdlib `urllib`; NDJSON streaming; token via `Authorization: Bearer` header (never in URLs/logs)
- **LichessController** (`src/lichess_controller.py`): Background daemon threads for event/game streams; pushes typed events onto a `queue.Queue`; the pygame main loop drains it. Background threads never call pygame.

## Lichess Integration

- Primary: in-GUI "AI vs Lichess" mode (live board + move-by-move review).
- Optional: `lichess-bot` headless bridge via the UCI entry point (`lichess/config.yml.example`, `lichess/run_engine.bat`).
- Token: `LICHESS_BOT_TOKEN` env var or gitignored `lichess/config.yml`. Never hardcoded/committed.
- Dedicated BOT account only; no browser automation; no normal-account automation. See `lichess/README_LICHESS.md`.

## Key Design Decisions

- Board uses row 0 = rank 8 (black side), row 7 = rank 1 (white side)
- Moves are tuples: `(from_row, from_col, to_row, to_col, promotion_piece_or_None)`
- Engine evaluates from white's perspective (positive = white advantage)
- Threefold repetition compares position-only FEN (excludes move counters)
- UCI castling is the king's from-to square (non-Chess960); en passant is plain from-to; `GameState.make_move` infers castling/EP/promotion from piece type + squares
- Lichess turn parity: bot (white) moves on even move count, bot (black) on odd; the board is always rebuilt from `initialFen` + the full UCI move list (each `gameState.moves` is authoritative and complete)