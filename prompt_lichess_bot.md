# Chess Application — Lichess BOT Integration Prompt

Act as a senior software architect and technical lead.

I already have a working Windows desktop chess application where a human player can play against the built-in AI engine. The existing project was created from the original prompt.md. Now I want to extend the existing application so that the built-in AI can also play games on Lichess using a dedicated Lichess BOT account.

This is an incremental upgrade project. Do not rewrite the existing chess application from scratch.

## Main Goal

Add Lichess BOT support to the existing chess application so that the current AI engine can play games against opponents on Lichess.

The preferred design is:

* Keep the existing Windows GUI human-vs-AI mode working.
* Keep the existing local Engine-vs-Engine mode (useful for debugging and regression).
* Extract or reuse the existing AI move-selection logic.
* Add a UCI-compatible engine entry point.
* Integrate Lichess **directly into the existing GUI exe** as a first-class mode using the official Lichess BOT API (the game is streamed live onto the GUI board and finished games are reviewable move-by-move through the GUI).
* Provide `lichess-bot` as an **optional headless bridge** (via the UCI entry point) for unattended 24/7 play.
* Do not use browser automation, Selenium, screen scraping, or normal-account automation.

## Important Constraints

* Preserve the existing GUI chess application.
* Preserve existing local human-vs-AI gameplay.
* Do not rewrite the project unless a refactor is clearly necessary.
* Do not hardcode the Lichess OAuth token.
* Use environment variables or a local config file for secrets.
* The real token must be excluded from git.
* Automated Lichess play must use a dedicated Lichess BOT account.
* Do not design anything that logs into a normal Lichess account and plays automatically.

## First Task: Inspect the Existing Project

Before editing code, inspect the current project and explain:

* current project structure
* GUI framework
* board representation
* move validation logic
* game loop
* AI engine / move-selection logic
* engine-vs-engine mode, if already present
* test structure
* build and packaging workflow

Then provide a concise migration plan.

## Desired Architecture

Refactor or adapt the project so that the AI engine can be used in three contexts:

1. Existing GUI mode:

   * Human vs AI on the Windows desktop app

2. Optional local AI-vs-AI mode:

   * Useful for debugging and regression testing

3. Lichess/UCI mode (PRIMARY):

   * A Lichess BOT API client integrated into the existing GUI exe that streams
     live games onto the GUI board and supports move-by-move review of finished
     games through the GUI interface.
   * A command-line UCI engine entry point that lichess-bot can call as an
     OPTIONAL headless bridge for unattended play.

The AI engine should expose a clean interface similar to:

```python
choose_move(board, time_limit_ms=None) -> move
```

The exact types may depend on the current project, but the interface must guarantee:

* input is a valid chess position
* output is a legal move
* it respects a time limit when provided
* it handles checkmate, stalemate, promotion, castling, en passant, and draw states correctly

## UCI Engine Entry Point

Add a UCI-compatible entry point, for example:

```bash
python -m chess_app.uci
```

or another command that fits the current project structure.

The UCI mode must support at least:

```text
uci
isready
ucinewgame
position startpos
position startpos moves ...
position fen ... moves ...
go movetime N
go depth N
stop
quit
```

Required UCI output examples:

```text
id name MyChessAppEngine
id author Joe
uciok
readyok
bestmove e2e4
```

Important:

* In UCI mode, stdout must only contain valid UCI protocol output.
* Debug logs must go to stderr or a log file.
* `go movetime 1000` must always return `bestmove <legal_move>` when the game is not over.

## Lichess BOT Integration

The GUI integrates a dedicated Lichess BOT API client (official API, no browser
automation) so the engine can play live games that are visible on the GUI board
and reviewable afterward. `lichess-bot` is supported as an optional headless
alternative via the UCI entry point.

Add a Lichess integration folder such as:

```text
lichess/
  config.yml.example
  README_LICHESS.md
```

The example config should show how to connect lichess-bot to the local UCI engine.

Do not commit the real token. Use:

```text
LICHESS_BOT_TOKEN
```

The documentation must explain:

* how to use a dedicated Lichess BOT account
* how to set the token safely
* how to test the UCI engine locally before connecting to Lichess
* how to run lichess-bot with this engine
* how to collect PGN/game results for rating evaluation

If lichess-bot config fields differ from examples, check the current official lichess-bot documentation and follow the current format.

## Testing Requirements

Add or update automated tests for:

* existing GUI/local chess logic still works
* AI move selection always returns legal moves
* UCI `uci` returns `uciok`
* UCI `isready` returns `readyok`
* UCI `position startpos moves e2e4` sets the board correctly
* UCI `go movetime 1000` returns a legal bestmove
* promotion moves are handled correctly
* checkmate/stalemate positions do not crash the engine
* existing packaging tests still pass

## Regression Requirement

After adding Lichess support, verify that the original application still satisfies the original prompt.md goals:

* human can still play against the AI in the Windows GUI
* full chess games can complete correctly
* automated tests pass
* Windows release build still works
* packaged .exe can still launch from the release folder

## Deliverables

Please implement:

* minimal refactor needed to expose the AI engine cleanly
* UCI protocol entry point
* in-GUI "AI vs Lichess" mode using the official Lichess BOT API, with live board display and move-by-move game review
* lichess-bot example config (optional headless bridge)
* Lichess integration README
* tests for UCI and AI move legality
* updates to the main README
* no hardcoded tokens
* no browser automation

## Completion Criteria

This upgrade is complete only when:

* existing GUI gameplay still works
* the AI can still play local games
* the existing Engine-vs-Engine mode still works
* UCI mode works from the command line
* lichess-bot can call the engine command
* `go movetime 1000` returns a legal bestmove
* the GUI "AI vs Lichess" mode plays a live game that is visible on the board and reviewable move-by-move through the GUI
* Lichess token is not hardcoded or committed
* tests pass
* documentation explains how to connect the engine to Lichess safely
* the Windows packaged release still works after the integration

Start by inspecting the existing project and producing a concise migration plan before making code changes.
