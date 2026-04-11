# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`glm_cc_chess` is a Windows desktop chess application with a Pygame GUI and a built-in chess engine. A human can play against the engine or watch engine-vs-engine games.

## Tech Stack

- **Language**: Python 3.12+
- **GUI**: Pygame
- **Testing**: pytest + pytest-cov
- **Engine**: Custom minimax with alpha-beta pruning, iterative deepening, quiescence search

## Project Structure

```
src/
  board.py      — Board representation, FEN parsing, piece operations
  moves.py      — Move generation, legality, check/checkmate detection
  game.py       — Game state controller, make/unmake, draw detection
  engine.py     — Chess engine (minimax + alpha-beta + quiescence)
  gui.py        — Pygame GUI (board rendering, click interaction)
  main.py       — Entry point
tests/
  test_board.py
  test_moves.py
  test_game.py
  test_engine.py
```

## Commands

```bash
# Setup
python -m venv venv
source venv/Scripts/activate    # Windows (Git Bash)
pip install -e ".[dev]"

# Run the application
python -m src.main

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=src --cov-report=term-missing
```

## Architecture

- **Board** (`src/board.py`): 8x8 array with piece codes (`wP`, `bK`, etc.), FEN parsing/generation
- **Moves** (`src/moves.py`): Pseudo-legal and legal move generation, check/checkmate/stalemate detection
- **Game** (`src/game.py`): Full game state with make/unmake, castling rights, en passant, draw rules
- **Engine** (`src/engine.py`): Evaluation with piece-square tables, search with alpha-beta + quiescence
- **GUI** (`src/gui.py`): Pygame board rendering, click-to-move, legal move highlighting, side panel

## Key Design Decisions

- Board uses row 0 = rank 8 (black side), row 7 = rank 1 (white side)
- Moves are tuples: `(from_row, from_col, to_row, to_col, promotion_piece_or_None)`
- Engine evaluates from white's perspective (positive = white advantage)
- Threefold repetition compares position-only FEN (excludes move counters)