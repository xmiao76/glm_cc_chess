# GLM CC Chess

A Windows desktop chess application with a Pygame GUI and a built-in chess
engine (minimax + alpha-beta + iterative deepening + quiescence search). A human
can play against the engine, watch engine-vs-engine games, and — new — let the
engine play against opponents on **Lichess** via a dedicated BOT account, with
live board display and move-by-move game review.

## Features / Play Modes

| Mode | Description |
|---|---|
| **Play as White / Black** | Human vs the built-in engine on the desktop GUI. |
| **Engine vs Engine** | The engine plays both sides (debugging / regression). |
| **AI vs Lichess** | The engine plays real games on Lichess via the BOT API; the game is streamed live onto the GUI board and finished games can be reviewed move-by-move. |

The same engine is also exposed through a **UCI** command-line entry point, so
[`lichess-bot`](https://github.com/lichess-bot-devs/lichess-bot) can run it
headlessly as an optional unattended bridge.

## Tech Stack

- **Language:** Python 3.12+
- **GUI:** Pygame
- **Engine:** Custom minimax with alpha-beta pruning, iterative deepening, quiescence search
- **Testing:** pytest + pytest-cov
- **Lichess:** Official Lichess BOT API over stdlib `urllib` (no extra dependencies)

## Project Structure

```
src/
  board.py             — Board representation, FEN parsing, piece operations
  moves.py             — Move generation, legality, SAN + UCI move conversion
  game.py              — Game state controller, make/unmake, draw detection
  engine.py            — Chess engine + choose_move() clean interface
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
  test_board.py, test_moves.py, test_game.py, test_engine.py,
  test_uci.py, test_lichess.py
```

## Setup

```bash
python -m venv venv
source venv/Scripts/activate    # Windows (Git Bash)
pip install -e ".[dev]"
```

## Run

```bash
# GUI (Human vs AI / Engine vs Engine / AI vs Lichess)
python -m src.main

# UCI engine (for lichess-bot or any UCI host)
python -m src.uci
```

## Test

```bash
pytest tests/ -v
pytest tests/ --cov=src --cov-report=term-missing
```

## Package the Windows Release

```bash
python package.py        # builds release/GLM_CC_Chess.exe + zip via PyInstaller
```

## Lichess Setup (quick)

1. Create a **dedicated Lichess BOT account** (not your personal account) and
   upgrade it to a BOT.
2. Generate an API token with the `bot:play` scope at
   <https://lichess.org/account/oauth/token>.
3. Set it as an environment variable:
   ```bash
   export LICHESS_BOT_TOKEN="your_real_token"   # Git Bash
   ```
   (or put it in a gitignored `lichess/config.yml` — never commit it).
4. Run `python -m src.main`, click **AI vs Lichess**, accept a challenge, and
   watch the engine play. Review finished games with `<<` `<` `>` `>>`.

See [`lichess/README_LICHESS.md`](lichess/README_LICHESS.md) for the full guide,
including the optional headless `lichess-bot` bridge and PGN collection.

## Security Notes

- The Lichess token is **never** hardcoded or committed. Read it from the
  `LICHESS_BOT_TOKEN` env var or a gitignored `lichess/config.yml`.
- `.gitignore` excludes `lichess/config.yml`, `.lichess_token`, and `*.token`.
- No browser automation, Selenium, or screen scraping is used — only the
  official Lichess BOT API.
- Automated play uses a **dedicated BOT account only**. Never automate a normal
  Lichess account (that violates the Lichess Terms of Service).

## Architecture Notes

- Board: row 0 = rank 8 (black side), row 7 = rank 1 (white side).
- Moves are tuples: `(from_row, from_col, to_row, to_col, promotion_piece_or_None)`.
- The engine evaluates from white's perspective (positive = white advantage).
- `choose_move(board, time_limit_ms=None, max_depth=None)` is the clean move
  interface shared by the GUI, UCI, and Lichess controller.
- The Lichess controller runs API streams on background daemon threads and
  pushes typed events onto a `queue.Queue`; the pygame main loop drains it.
  Background threads never call pygame (not thread-safe).