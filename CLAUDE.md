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
  gui_textinput.py     — Reusable single-line text input field (Lichess opponent entry)
  clipboard_util.py    — System clipboard copy (Copy Log feature; stdlib backends)
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
- The bot can also **initiate** games: a manual **Challenge** (target username)
  and an **Auto** toggle (auto-accept peers + periodic auto-challenge). Two
  programs with Auto on auto-match via a leader/follower rule (the
  alphabetically-earlier bot challenges) so exactly one game starts.
- Optional: `lichess-bot` headless bridge via the UCI entry point (`lichess/config.yml.example`, `lichess/run_engine.bat`).
- Token: `LICHESS_BOT_TOKEN` env var or gitignored `lichess/config.yml`. Never hardcoded/committed.
- Auto-match env vars: `LICHESS_OPPONENT`, `LICHESS_AUTO_MATCH`, `LICHESS_CLOCK_LIMIT`, `LICHESS_CLOCK_INCREMENT`.
- Dedicated BOT account only; no browser automation; no normal-account automation. See `lichess/README_LICHESS.md`.
- Bot-account requirement: the bot-only endpoints (`/api/bot/game/stream/*`, make-move) return HTTP 400 `"This endpoint can only be used with a Bot account"` for a normal account. The controller detects this at startup via the profile `title == "BOT"` (`is_bot`), blocks challenges and auto-match until True, and exposes `upgrade_account()` (POST `/api/bot/account/upgrade`, irreversible); the GUI shows an "Upgrade to Bot" button when `is_bot` is False.

## Key Design Decisions

- Board uses row 0 = rank 8 (black side), row 7 = rank 1 (white side)
- Moves are tuples: `(from_row, from_col, to_row, to_col, promotion_piece_or_None)`
- Engine evaluates from white's perspective (positive = white advantage)
- Threefold repetition compares position-only FEN (excludes move counters)
- UCI castling is the king's from-to square (non-Chess960); en passant is plain from-to; `GameState.make_move` infers castling/EP/promotion from piece type + squares
- Lichess turn parity: bot (white) moves on even move count, bot (black) on odd; the board is always rebuilt from `initialFen` + the full UCI move list (each `gameState.moves` is authoritative and complete)
- Lichess challenge events carry a `direction` (`in`/`out`): incoming may be auto-accepted (only from configured peers, only when idle); outgoing are tracked in `_pending_outgoing` and canceled on `gameStart`/before reissue
- Lichess auto-match uses a leader/follower rule: the bot challenges only peers whose username sorts after its own, so two identical auto-matching bots create exactly one game (no double games); concurrency is one game at a time (`_active_games`)
- Lichess auto-accept declines `correspondence` challenges from configured peers with reason `tooSlow` (the streaming bot can't safely resume a multi-day game after a restart — the event stream only delivers NEW gameStarts, not in-progress games); real-time speeds (blitz/bullet/rapid/classical) are auto-accepted. Non-peer or Auto-off correspondence still surfaces as `ChallengeReceived` for a manual choice. The GUI logs "Challenged <opp>" once, from the `ChallengeSent` event (single source — `_challenge_opponent` does not log it directly), and includes Lichess's challenge `id=` so one-unique-id-logged-once proves we sent exactly ONE challenge. NOTE: the opponent receiving it twice is SUFFICIENT but NOT NECESSARY evidence of two LIVE event streams on the opponent's account (Lichess delivers each event to every live stream) — the gameStart (not the challenge) is what double-connects the game stream, so a duplicate aborts at creation even with SINGLE challenge receipt (the 2nd stream came up after the challenge but caught the gameStart). Two LIVE streams can be one process with a stale/lingering reconnect stream, not necessarily two processes. Two `Connected as <opp>` lines do NOT prove a duplicate — lichess-bot logs 'Connected' on each reconnect (`start()` re-runs when the event stream drops), so two lines can be ONE process. The decisive check is the bot PROCESS count on BOTH machines (Task Manager), not the `Connected` line count and not the receipt count
- Lichess activity log format: every line is prefixed with a wall-clock `HH:MM:SS` timestamp (`time.strftime`, matching the opponent lichess-bot's format) so the two logs can be correlated and the same-second "instant abort at creation" is visible; the rolling history is 100 lines (Copy Log captures the full game sequence — the challenge line survives; the on-screen panel renders only the last ~6, truncated to 30 chars). The timestamp is the evidence that distinguishes an instant abort (sub-second) from the ~15-30s no-first-move timeout
- Lichess challenge/decline params MUST be in the POST body (`application/x-www-form-urlencoded`), NOT the query string — Lichess ignores query-string params and silently makes a no-clock `correspondence` game (lichess-org/api issue #142). `LichessClient._request` form-encodes `form_params` into the body; `create_challenge` uses dotted keys `clock.limit`/`clock.increment`. The controller surfaces Lichess's assigned `speed` on `ChallengeSent`; the GUI logs it (e.g. "Challenged <opp> (rapid 300+3)") and warns loudly if a challenge came back `correspondence` (clock didn't land). `GameStarted` is logged with both colors + the game id for log correlation
- Lichess bidirectional double-game guard: if we have a pending outgoing challenge to a peer, their near-simultaneous reverse challenge is auto-declined with reason `later` (accepting would start a second game where each bot can latch a different one and both appear to wait on the other). Without a pending outgoing, normal peer challenges auto-accept as usual
- Lichess first-move budget: the bot's first move of a game is capped at `FIRST_MOVE_BUDGET_MS` (1000ms) so it lands before the opponent can abort (Lichess disallows a single-player abort once a move is on the board); later moves use the normal clock-derived budget (`remaining//20 + inc//2`, capped 200–5000ms). The GUI logs `Engine thinking...` (from the `Status` event) before each move so an abort during the think is visibly our-side-trying, not a freeze
- Lichess instant opening-book move 1 (Experiment A): when the bot is WHITE and it is move 1 from `STARTING_FEN`, `_maybe_move` posts an instant book move from `_OPENING_BOOK_WHITE_MOVE1` (`("e2e4",)`) with NO engine think (`_instant_first_move` verifies legality via `generate_legal_moves` first), so move 1 lands at ~POST-RTT — ahead of a same-owner creation abort (~0.5–1s, faster than any engine think) IF the abort respects "a move is on the board". The `Status` is `"Playing opening book (e2e4)..."` (the GUI sets `engine_started` for any Status starting with `"Engine thinking"` OR `"Playing opening book"`). This is a diagnostic + possible workaround, NOT a guaranteed fix, and applies only when we are White (when we are Black the opponent, White, must move first). Set `_OPENING_BOOK_WHITE_MOVE1 = ()` to disable. If an instant move 1 SURVIVES → the abort was a short no-first-move window (ship the book move); if it STILL aborts → confirms a server-side same-owner/IP abort that ignores moves-on-board (no code fix; the decisive test remains the third-party-bot experiment)
- Lichess instant-abort-at-creation diagnosis: when the gameFull arrives already carrying an `aborted`/`noStart` status, `_process_game_stream` skips `_maybe_move` (so no `Engine thinking...`/`Playing opening book...` Status is ever pushed) and the GUI logs `GameStarted` then `GameFinished` with 0 moves (in EITHER color — we move first as White, or wait on White as Black). `noStart` (aborted BEFORE it started — a creation-time conflict) is distinct from `aborted` (a player/no-first-move abort); both render as "Game aborted", so the controller surfaces the ACTUAL status + the gameFull's `source`/`speed`/`variant`/titles in a summary `Status` (`_log_already_over_game_full`) and logs the full gameFull JSON (`logger.warning`) — Lichess gives no explicit "who aborted" field, so the status + context is all we have. The GUI tracks `engine_started` on the live game (set True by an `Engine thinking...`/`Playing opening book...` Status or `EngineMoved`); `_log_abort_diagnostic` (fired for both `aborted` and `noStart` via `ABORT_LIKE_STATUSES`) tells the regimes apart by COLOR: **White** uses `engine_started` (never started thinking ⇒ already over when we connected; started thinking ⇒ opponent aborted during our think); **Black** uses elapsed in THREE regimes (we never start thinking while Black, so `engine_started` is always False and useless — only elapsed distinguishes them): `< INSTANT_ABORT_THRESHOLD_S` (5s) ⇒ instant abort at creation (duplicate event-stream conflict); `5s–NO_FIRST_MOVE_WARN_S` (20s) ⇒ a MID-THINK abort — the game was LIVE then aborted before White's first move, too soon for the ~15-30s timeout (the 20s stall warning never fired), so it's one account connecting to the game stream twice (a second GUI window / lichess-bot for our account alongside the GUI / opponent running two bot processes) or a manual Abort click — a single instance can't cause this (`gameStart` is deduped, `abort()` is only the manual button); `≥ 20s` ⇒ the genuine no-first-move timeout ("opponent not playing"). The instant and mid-think messages explicitly note the opponent may have been trying to move (its move landed on an already-dead game → `HTTP 400: game already over`), so neither says "opponent not playing" (reserved for the genuine timeout). The event-stream dedup branch logs a `Status` note on a duplicate `gameStart` (a second event-stream connection for this account — the same signature). The CANDIDATE cause of this instant-at-creation abort is a DUPLICATE event-stream connection on one of the accounts: two LIVE streams on one account (two bot processes, OR one process with a stale/lingering reconnect stream Lichess hasn't closed) both receive the gameStart (Lichess pushes it to EVERY live stream on the account) and both connect to the same game's `/api/bot/game/stream/{id}`, and Lichess aborts the conflict at creation — this happens whether our one challenge was received once or twice (the gameStart, NOT the challenge, is what double-connects the game stream). This is UNPROVEN for single-receipt games — do NOT assert it as the cause. RETRACTED: the earlier claim that 'the reliable proof of which side has the duplicate is the `Connected as <user>` line COUNT in each log' is FALSE — lichess-bot logs 'Connected' on each reconnect (`start()` re-runs when the event stream drops), so TWO `Connected as <user>` lines can be ONE process, not two. Game NSyfD5g5 (2026-06-27): our side clean (one `Connected as xmiao_glm`, one challenge id, gameFull already aborted, never started thinking, we were White), opponent xmiao_ds logged the challenge ONCE + accepted ONCE yet STILL aborted at creation (turn=W, 0 moves, 0.0s); xmiao_ds's TWO `Connected as xmiao_ds` lines are reconnect logging, NOT proof of a duplicate on xmiao_ds. The decisive check is the bot PROCESS count on BOTH machines (Task Manager), not the `Connected` line count and not the receipt count; double challenge receipt (opponent receiving it twice) corroborates two LIVE streams but not two processes. The fix is to run only ONE bot process per account (a second process for EITHER account double-connects and aborts every game); if a side has only one process yet aborts recur, its stream may be reconnecting/stale (network/proxy idle-timeout) — run that bot in this GUI too (one clean connection, `gameStart` deduped). Decisive experiment: run BOTH bots in this GUI (one event stream each); if aborts stop -> lichess-bot/stale stream was it; if they continue -> it's the Lichess matchup (same owner/IP — unverified abort policy) or a Lichess-side abort, not our code. `_start_lichess` stops any running controller before creating a new one, so re-entering Lichess mode does not leak a second live event-stream thread on our side