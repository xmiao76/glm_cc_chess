# Plan: AI vs Lichess — In-GUI BOT Integration with Game Review

## Summary
Extend the existing `glm_cc_chess` Windows Pygame app so the built-in AI can play games on Lichess via a **dedicated Lichess BOT account**, integrated directly into the existing GUI exe as a first-class mode. The Lichess game is streamed live into the Pygame board, and finished games can be **reviewed move-by-move through the GUI**. A UCI protocol entry point (`python -m src.uci`) is also added so the same engine can be driven by `lichess-bot` as an optional headless bridge. No browser automation; no hardcoded tokens; existing Human-vs-AI and Engine-vs-Engine modes are preserved.

## User Story
As a chess developer/enthusiast, I want my built-in AI to play against real opponents on Lichess from inside the same desktop GUI (and be able to review those games afterward), so that I can test and showcase the engine against human/bot opposition without running a separate headless process — while still keeping the optional `lichess-bot` UCI bridge for unattended 24/7 play.

## Problem → Solution
**Current state:** The GUI offers "Play as White", "Play as Black", and a local "Engine vs Engine" mode where both sides are the local engine. There is no network play and no clean headless engine interface.

**Desired state:** A new "AI vs Lichess" GUI mode connects to Lichess with a BOT token, streams a real game, shows the opponent's moves and the engine's moves on the board with clocks/status, lets the user accept/decline challenges and resign/abort, and review finished games. The engine is also exposed via a UCI stdin/stdout loop so `lichess-bot` can run it headlessly. All three contexts (GUI human-vs-AI, local engine-vs-engine, Lichess/UCI) share one clean `choose_move(board, time_limit_ms=None)` interface.

## Metadata
- **Complexity**: Large
- **Source PRD**: `prompt_lichess_bot.md` (free-form spec; will be updated in Task 1 to match the agreed in-GUI design)
- **PRD Phase**: standalone (no Implementation Phases section)
- **Estimated Files**: ~9 new + ~7 updated

---

## UX Design

### Before
```
┌──────────────────────────────────────────────┐
│            GLM CC Chess (menu)                │
│   [ Play as White ]                           │
│   [ Play as Black ]                           │
│   [ Engine vs Engine ]   <- local only        │
│                                               │
│   (No network play. No Lichess. No review.)   │
└──────────────────────────────────────────────┘
```

### After
```
┌──────────────────────────────────────────────┐
│            GLM CC Chess (menu)                │
│   [ Play as White ]                           │
│   [ Play as Black ]                           │
│   [ Engine vs Engine ]   <- kept (debugging)  │
│   [ AI vs Lichess ]      <- NEW               │
└──────────────────────────────────────────────┘

  Selecting "AI vs Lichess" (token configured):
┌────────────────────────────────┬──────────────┐
│  live board (bot's POV)         │ GLM CC Chess │
│  shows engine + opponent moves  │ vs Lichess   │
│  as they arrive                 │ Opp: user123 │
│                                 │ ⏱ W 1:23     │
│                                 │ ⏱ B 0:58     │
│                                 │ Status: …    │
│                                 │ [Resign]     │
│                                 │ [Abort]      │
│                                 │ [Menu]       │
└────────────────────────────────┴──────────────┘

  Incoming challenge (token configured, idle):
  "Challenge from user123 (blitz 5+3, casual)"
  [Accept]  [Decline]

  After game ends -> Review mode:
  move list shown; [<<] [<] [>] [>>] step through
  board replays the chosen position.
```

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| Main menu | 3 buttons | 4 buttons (add "AI vs Lichess") | Keep all existing buttons |
| Board input | Click-to-move in "play"/"engine_vs_engine" | Watch-only in "lichess" mode (engine plays automatically) | No click-to-move during a live Lichess game |
| Side panel | Turn/captured/moves + New Game/Flip/Undo | Adds opponent name, clocks, status, Resign/Abort/Menu; in review adds step buttons | Reuse existing `_draw_button` pattern |
| Challenge handling | N/A | Accept/Decline buttons appear when a challenge event arrives | Driven by background event stream |
| Game review | N/A | Prev/Next/Home/End step through stored UCI move list | Replays board from initial FEN + moves[:idx] |
| CLI | `python -m src.main` only | Add `python -m src.uci` (UCI engine) | Used by optional lichess-bot bridge |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 (critical) | `src/engine.py` | 161-206 | `ChessEngine.get_best_move(board, time_limit)` — the logic to wrap in `choose_move` |
| P0 (critical) | `src/game.py` | 40-143 | `GameState.make_move(move)` — used to apply UCI move lists (handles castle/EP/promo) |
| P0 (critical) | `src/gui.py` | 60-95, 494-575 | `ChessGUI.__init__`, `draw_menu`, `_start_game`, `run` loop — integration points |
| P0 (critical) | `src/gui.py` | 326-385, 454-492 | `draw_panel`, `_draw_button`, `handle_button_click` — panel/button patterns to mirror |
| P1 (important) | `src/board.py` | 43-80, 158-168 | `Board.from_fen`, `square_to_algebraic`, `algebraic_to_square` — FEN + UCI square conversion |
| P1 (important) | `src/moves.py` | 11-12, 301-338 | `Move` type, `move_to_algebraic` — where `move_to_uci`/`uci_to_move` belong |
| P1 (important) | `src/gui.py` | 238-246, 524-572 | `engine_move`, `run` engine-move scheduling — model for non-blocking engine use |
| P2 (reference) | `tests/test_engine.py` | 1-118 | Test patterns (pytest classes, FEN setup, legality assertions) |
| P2 (reference) | `tests/test_game.py` | 174-188 | `test_engine_vs_engine_completes` — game-completion test pattern |
| P2 (reference) | `GLM_CC_Chess.spec` | 1-45 | PyInstaller spec — `hiddenimports` to extend |
| P2 (reference) | `package.py` | 40-54 | Build/release flow — must still pass |
| P2 (reference) | `prompt_lichess_bot.md` | all | The spec; updated in Task 1 |

## External Documentation

| Topic | Source | Key Takeaway |
|---|---|---|
| Lichess BOT API endpoints | [lichess-org/api OpenAPI](https://github.com/lichess-org/api/blob/master/doc/specs/lichess-api.yaml) | `GET /api/stream/event`, `GET /api/bot/game/stream/{id}`, `POST /api/bot/game/{id}/move/{move}`, `POST /api/challenge/{id}/accept\|decline`, `POST /api/bot/game/{id}/abort\|resign`; auth = `Authorization: Bearer <token>`; scope `bot:play`; BOT accounts only |
| Bot game stream event fields | [lichess-bot lib/model.py](https://github.com/lichess-bot-devs/lichess-bot/blob/master/lib/model.py) | `gameFull`: `{id, white{name,id,...}, black{...}, initialFen, state{moves,wtime,btime,winc,binc,status,winner}, clock{initial,increment}, speed, variant, rated}`; `gameState`: `{moves,wtime,btime,winc,binc,status,winner}`; bot color = `white.name.lower()==my_username`; `moves` is the FULL UCI move list (space-separated) from `initialFen` |
| Event stream event types | lichess-bot `lib/lichess.py` | `type` ∈ `challenge`, `gameStart` (has `game` field), `gameFinish`; NDJSON; empty keep-alive lines every ~7s |
| Lichess game status values | lichess-org/api spec | `created`, `started`, `aborted`, `mate`, `resign`, `stalemate`, `timeout`, `draw`, `outoftime`, `cheat`, `noStart`, `variantEnd`, `unknown` — game over when status ∉ {created, started} |
| lichess-bot config format | [lichess-bot config.yml.default](https://github.com/lichess-bot-devs/lichess-bot/blob/master/config.yml.default) | `token`, `url`, `engine:{dir,name,protocol:uci,uci_options}` — used for the optional headless bridge |
| UCI protocol | [chessprogramming/UCI](https://www.chessprogramming.org/UCI) | `uci`, `isready`, `ucinewgame`, `position [startpos\|fen ...] moves ...`, `go movetime N\|depth N`, `stop`, `quit`; output `id name`, `id author`, `uciok`, `readyok`, `bestmove <move>`; `bestmove 0000` when no legal move |

---

## Patterns to Mirror

Code patterns discovered in the codebase. Follow these exactly.

### NAMING_CONVENTION
// SOURCE: src/engine.py:161, src/game.py:29, src/moves.py:12
```python
class ChessEngine:
    """Chess engine using minimax with alpha-beta pruning and iterative deepening."""
    def __init__(self, max_depth: int = 4, time_limit: float = 2.0) -> None: ...
    def get_best_move(self, board: Board, time_limit: float | None = None) -> Move | None: ...

class GameState:
    """Full chess game state with move history and draw detection."""

Move = tuple[int, int, int, int, str | None]  # (from_row, from_col, to_row, to_col, promotion_piece_or_None)
```
- Module-level docstring at top of every file. `from __future__ import annotations` first code line.
- Classes PascalCase with a triple-quoted docstring; methods snake_case with type hints and `-> None` where applicable.
- Module-level constants UPPER_SNAKE (`STARTING_FEN`, `PIECE_VALUES`, `CHECKMATE_SCORE`).

### ERROR_HANDLING
// SOURCE: src/game.py:47-48, src/game.py:21-26, src/engine.py:180-184
```python
piece = self.board.get_piece(from_r, from_c)
if piece is None:
    raise ValueError(f"No piece at ({from_r},{from_c})")

class GameResult(Exception):
    """Raised when the game ends."""
    def __init__(self, result: str, reason: str) -> None: ...

# Engine returns None when there are no legal moves (game over):
legal_moves = generate_legal_moves(board, board.active_color)
if not legal_moves:
    return None
```
- Raise `ValueError` with a descriptive f-string for programmer errors.
- Sentinel returns (`None`) for "no result" cases; never silently swallow.
- Network/protocol layers (UCI, Lichess) MUST handle errors explicitly and never crash the GUI thread — catch per-line, log to stderr, continue.

### LOGGING_PATTERN
// SOURCE: codebase has NO logging precedent. Established here for UCI + Lichess.
```python
import logging
logger = logging.getLogger(__name__)  # uci.py / lichess_client.py / lichess_controller.py
```
- The codebase currently uses no `logging`. UCI requires debug output on stderr only (stdout must be pure UCI). Lichess controller logs to stderr too.
- Configure once at module import: `logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")` — but ONLY in entry-point modules (`src/uci.py`'s `main()`, and `src/main.py` if needed), never in library modules called by tests. Library modules just create a `logger` and log; the entry point wires the handler.
- NEVER log the token. Redact in any debug output.

### BOARD_SQUARE_CONVERSION
// SOURCE: src/board.py:158-168
```python
@staticmethod
def square_to_algebraic(row: int, col: int) -> str:
    """Convert (row, col) to algebraic notation like 'e4'."""
    return chr(ord("a") + col) + str(8 - row)

@staticmethod
def algebraic_to_square(sq: str) -> tuple[int, int]:
    """Convert algebraic notation like 'e4' to (row, col)."""
    col = ord(sq[0]) - ord("a")
    row = 8 - int(sq[1])
    return (row, col)
```
- row 0 = rank 8 (black side), row 7 = rank 1 (white side). `algebraic_to_square("e2")` → (6,4); `algebraic_to_square("e4")` → (4,4). UCI "e2e4" → internal move (6,4,4,4,None). Verified against existing tests (`test_board.py:132-140`).

### GUI_BUTTON_PATTERN
// SOURCE: src/gui.py:454-492
```python
def _draw_button(self, text: str, x: int, y: int, w: int, h: int, action) -> None:
    """Draw a button and register its action."""
    mouse_pos = pygame.mouse.get_pos()
    is_hover = x <= mouse_pos[0] <= x + w and y <= mouse_pos[1] <= y + h
    color = BUTTON_HOVER_COLOR if is_hover else BUTTON_COLOR
    pygame.draw.rect(self.screen, color, (x, y, w, h), border_radius=4)
    text_surf = self.small_font.render(text, True, BUTTON_TEXT_COLOR)
    text_rect = text_surf.get_rect(center=(x + w // 2, y + h // 2))
    self.screen.blit(text_surf, text_rect)
    if not hasattr(self, '_buttons'):
        self._buttons = []
    self._buttons.append((x, y, w, h, action))

def handle_button_click(self, pos: tuple[int, int]) -> None:
    if hasattr(self, '_buttons'):
        for x, y, w, h, action in self._buttons:
            if x <= pos[0] <= x + w and y <= pos[1] <= y + h:
                action()
                return
```
- `self._buttons` is REBUILT every frame in `run()` (cleared at `gui.py:552` before redraw). Any new buttons must be re-registered each frame by the draw method. Actions are plain callables (often lambdas).
- New "lichess" mode must follow this exact rebuild-each-frame contract.

### GUI_RUN_LOOP_PATTERN
// SOURCE: src/gui.py:524-572
```python
def run(self) -> None:
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:
                    ...  # dispatch by mode
            elif event.type == pygame.KEYDOWN:
                ...
        self._buttons = []          # clear before redraw
        if self.mode == "menu":
            self.draw_menu()
        else:
            self.draw_board()
            self.draw_panel()
            # engine move scheduling (currently BLOCKING):
            if not self.game_over and not self.is_engine_thinking:
                if self.mode == "engine_vs_engine": ...
                elif self.game.board.active_color != self.player_color:
                    self.engine_move()
        pygame.display.flip()
        self.clock.tick(FPS)
    pygame.quit()
    sys.exit()
```
- GOTCHA: pygame is NOT thread-safe. Only the main loop thread may call pygame functions. Background Lichess/engine threads must communicate via `queue.Queue`; the main loop drains it and updates pygame state. (The existing `engine_move()` blocks the loop — acceptable locally, but the Lichess engine search MUST run off the main thread so the UI stays responsive and clocks update.)

### TEST_STRUCTURE
// SOURCE: tests/test_engine.py:1-12, tests/test_game.py:174-188
```python
"""Tests for engine.py — evaluation, forced mates, time control."""
import time
import pytest
from src.board import Board, STARTING_FEN
from src.engine import ChessEngine, evaluate, CHECKMATE_SCORE
from src.moves import generate_legal_moves

class TestEngineConsistency:
    def test_always_returns_legal_move(self):
        positions = [STARTING_FEN, "r1bqkbnr/..."]
        engine = ChessEngine(max_depth=3, time_limit=2.0)
        for fen in positions:
            board = Board.from_fen(fen)
            move = engine.get_best_move(board)
            assert move is not None
            legal = generate_legal_moves(board, board.active_color)
            assert move in legal
```
- One `tests/test_<module>.py` per source module. Group tests in `class Test<Area>:` with `def test_<behavior>(self):`.
- Setup via `Board.from_fen(...)`; assert against `generate_legal_moves` for legality.
- AAA layout (Arrange/Act/Assert). Comments explain the position where useful.
- `pyproject.toml` sets `pythonpath = ["."]` and `testpaths = ["tests"]`, so tests import `from src.x import ...`.

### CONFIG_PATTERN
// SOURCE: pyproject.toml:20-25, .gitignore:1-7
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```
```
# .gitignore
/venv
release/*.exe
.claude/settings.local.json
/build
/dist
```
- No env-var usage exists yet. The Lichess token is read from `LICHESS_BOT_TOKEN` (preferred) then a gitignored `lichess/config.yml`; the real config file and any token files MUST be added to `.gitignore`.

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `prompt_lichess_bot.md` | UPDATE | Reconcile spec with agreed in-GUI design (primary: in-GUI BOT client + review; optional: lichess-bot bridge). Keep all safety constraints. |
| `src/moves.py` | UPDATE | Add `move_to_uci` + `uci_to_move` (shared by UCI + Lichess). |
| `src/engine.py` | UPDATE | Add `choose_move(board, time_limit_ms=None, max_depth=None)` clean interface. |
| `src/uci.py` | CREATE | UCI protocol stdin/stdout loop entry point (`python -m src.uci`). |
| `src/lichess_client.py` | CREATE | Lichess BOT API HTTP client (token, endpoints, NDJSON streaming, move posting) — stdlib `urllib` only. |
| `src/lichess_controller.py` | CREATE | Threading + queue orchestration between client and GUI (event stream, game streams, think-and-move, accept/decline/resign/abort). |
| `src/gui.py` | UPDATE | Add "AI vs Lichess" menu button, `lichess` mode, live game display, challenge UI, review mode, status panel, token check. |
| `lichess/config.yml.example` | CREATE | lichess-bot config template (optional headless bridge). |
| `lichess/run_engine.bat` | CREATE | Windows wrapper that runs `python -m src.uci` for lichess-bot. |
| `lichess/README_LICHESS.md` | CREATE | Lichess integration docs (BOT account, token, in-GUI mode, optional lichess-bot bridge, PGN collection). |
| `.gitignore` | UPDATE | Exclude real `lichess/config.yml`, `.lichess_token`, `*.token`. |
| `GLM_CC_Chess.spec` | UPDATE | Add `src.lichess_client`, `src.lichess_controller` to `hiddenimports`. |
| `tests/test_uci.py` | CREATE | UCI protocol tests (subprocess-driven). |
| `tests/test_lichess.py` | CREATE | Lichess client/controller tests (mocked HTTP + event parsing + turn logic). |
| `tests/test_moves.py` | UPDATE | Add `move_to_uci`/`uci_to_move` round-trip tests. |
| `tests/test_engine.py` | UPDATE | Add `choose_move` legality tests (movetime, depth, promotion, terminal positions). |
| `README.md` | CREATE | Developer-facing root README (modes, run, test, package, Lichess setup). |
| `release/readme.txt` | UPDATE | Mention new "AI vs Lichess" mode + token/BOT-account note for end users. |
| `CLAUDE.md` | UPDATE | Add new modules + Lichess feature to project structure/architecture. |

## NOT Building

- **No browser automation, Selenium, or screen scraping** (forbidden by prompt).
- **No normal-account automation** — only a dedicated Lichess BOT account via the official BOT API.
- **No rewriting of the core engine** (minimax/alpha-beta/quiescence stays as-is; we only wrap it).
- **No new third-party runtime dependencies** — Lichess HTTP uses stdlib `urllib.request`/`json`/`threading`/`queue` only (keeps PyInstaller packaging simple).
- **No matchmaking/tournament/pool engine** — accept incoming challenges (and optionally send one challenge from the GUI); bots cannot play pools/tournaments on Lichess.
- **No online opening books or endgame tablebases.**
- **No automated rating evaluation** — document how to collect PGN/results manually.
- **No chat UI** (the chat endpoint exists; a minimal send is optional but not required).
- **No fix to the pre-existing walrus bug at `src/engine.py:297`** (killer-move heuristic only; does not affect best-move correctness) — out of scope; noted in Risks.
- **No standalone engine `.exe` build target** in this pass — the optional lichess-bot bridge uses the `run_engine.bat` wrapper over `python -m src.uci` (requires Python on the lichess-bot host). A self-contained engine binary is a documented future option.

---

## Step-by-Step Tasks

### Task 1: Update `prompt_lichess_bot.md` to match the agreed design
- **ACTION**: Edit the spec so it reflects the in-GUI Lichess BOT integration + game review as the PRIMARY path, with the lichess-bot UCI bridge as an OPTIONAL alternative. Keep every safety constraint.
- **IMPLEMENT**:
  - In "Desired Architecture", make context #3 read: "Lichess mode (PRIMARY): a Lichess BOT API client integrated into the existing GUI exe that streams live games onto the board and supports move-by-move review. The UCI entry point + lichess-bot remain an OPTIONAL headless bridge for unattended play."
  - Add a requirement: "The Lichess game must be visible on the GUI board as it is played, and finished games must be reviewable move-by-move through the GUI."
  - Change "Prefer using lichess-bot as the bridge instead of building a custom Lichess client first" → "The GUI integrates a dedicated Lichess BOT API client (official API, no browser automation). lichess-bot is supported as an optional headless alternative via the UCI entry point."
  - Keep: dedicated BOT account only, no hardcoded token, `LICHESS_BOT_TOKEN`, token excluded from git, no browser automation, preserve GUI/local modes, UCI entry point requirements, testing requirements, regression requirements, completion criteria. Add "Lichess game is reviewable in the GUI" to completion criteria.
- **MIRROR**: N/A (documentation).
- **IMPORTS**: N/A.
- **GOTCHA**: Do not delete the original constraints — the regression requirements (existing GUI/local play still work, Windows release still works) must remain.
- **VALIDATE**: Re-read the file; confirm all original safety constraints are intact and the new primary path + review requirement are present.

### Task 2: Add UCI move conversion helpers to `src/moves.py`
- **ACTION**: Add `move_to_uci(move)` and `uci_to_move(uci)` next to the existing `move_to_algebraic`.
- **IMPLEMENT**:
```python
def move_to_uci(move: Move) -> str:
    """Convert an internal move to UCI notation (e.g. 'e2e4', 'e7e8q', 'e1g1').

    Castling is encoded as the king's from-to square (UCI standard for
    non-Chess960), which matches how generate_legal_moves emits castling.
    """
    from_r, from_c, to_r, to_c, promo = move
    uci = Board.square_to_algebraic(from_r, from_c) + Board.square_to_algebraic(to_r, to_c)
    if promo is not None:
        uci += promo.lower()
    return uci


def uci_to_move(uci: str) -> Move:
    """Convert UCI notation to an internal move.

    Only from/to/promotion are encoded; castling, en passant, and promotion
    are inferred by GameState.make_move from the piece type and squares.
    """
    from_sq, to_sq = uci[0:2], uci[2:4]
    from_r, from_c = Board.algebraic_to_square(from_sq)
    to_r, to_c = Board.algebraic_to_square(to_sq)
    promo = uci[4].upper() if len(uci) > 4 else None
    return (from_r, from_c, to_r, to_c, promo)
```
- **MIRROR**: BOARD_SQUARE_CONVERSION; place beside `move_to_algebraic` (`src/moves.py:301`).
- **IMPORTS**: `Board` is already imported at top of `moves.py`.
- **GOTCHA**: UCI promotion letter is lowercase ("e7e8q"); internal promo is uppercase ("Q"). Castling in UCI is king from-to ("e1g1"), which the existing `_king_moves`/`make_move` already produce/consume — do NOT emit rook from-to.
- **VALIDATE**: `move_to_uci((6,4,4,4,None)) == "e2e4"`; `uci_to_move("e2e4") == (6,4,4,4,None)`; `move_to_uci((7,4,7,6,None)) == "e1g1"`; `uci_to_move("e7e8q") == (1,4,0,4,"Q")`. Covered by new tests in Task 11.

### Task 3: Add clean engine interface `choose_move` to `src/engine.py`
- **ACTION**: Add a module-level function `choose_move(board, time_limit_ms=None, max_depth=None)` that wraps `ChessEngine.get_best_move` and guarantees a legal move (or `None` if the game is over).
- **IMPLEMENT**:
```python
def choose_move(board: Board, time_limit_ms: int | None = None,
                max_depth: int | None = None) -> Move | None:
    """Clean AI move-selection interface for UCI and Lichess integrations.

    Guarantees:
      - input is a valid chess position (caller-supplied Board);
      - the returned move is legal for `board.active_color`, or None when the
        game is already over (no legal moves);
      - when `time_limit_ms` is provided the search respects it (best effort).

    `max_depth` overrides the iterative-deepening ceiling (used by `go depth N`).
    If both are None, engine defaults (depth 4, 2.0s) are used.
    """
    depth = 20 if max_depth is None else max_depth
    time_limit = (time_limit_ms / 1000.0) if time_limit_ms is not None else 2.0
    engine = ChessEngine(max_depth=depth, time_limit=time_limit)
    return engine.get_best_move(board, time_limit=time_limit)
```
- **MIRROR**: NAMING_CONVENTION; reuse `ChessEngine` (`src/engine.py:161`). Construct a fresh `ChessEngine` per call so search scratch state never leaks between moves.
- **IMPORTS**: `Move` is already imported in `engine.py:11`.
- **GOTCHA**: `get_best_move` returns `None` when there are no legal moves (`src/engine.py:180-184`) — preserve that contract so callers can emit `bestmove 0000` / skip posting on Lichess. Do NOT mutate the caller's board.
- **VALIDATE**: `choose_move(Board.from_fen(STARTING_FEN), time_limit_ms=300)` returns a move in `generate_legal_moves(board, "w")`. `choose_move` on a checkmate FEN returns `None`. Covered by Task 11 tests.

### Task 4: Create `src/uci.py` — UCI protocol entry point
- **ACTION**: Implement a stdin/stdout UCI loop runnable via `python -m src.uci`. stdout carries ONLY UCI protocol output; all debug goes to stderr.
- **IMPLEMENT**: A `UCIEngine` class holding a `GameState` (or a `Board` + applied move list) and a `main()` that reads lines from `sys.stdin`.
  - Handle commands: `uci` → print `id name GLMCCChessEngine`, `id author Joe`, then `uciok`; `isready` → `readyok`; `ucinewgame` → reset to startpos; `setoption ...` → accept and ignore (or store); `position [startpos | fen <fen>] [moves <uci...>]` → rebuild board via `Board.from_fen` (or `STARTING_FEN`) then `GameState.make_move(uci_to_move(m))` for each move; `go movetime N` / `go depth N` / `go` (with optional `wtime/btime/winc/binc`) → compute a time/depth budget, call `choose_move`, print `bestmove <uci>` (or `bestmove 0000` if None); `stop` → best-effort (search is synchronous; print current bestmove if mid-search); `quit` → exit.
  - Time budget for `go wtime X btime Y ...`: `budget_ms = max(200, min(own_time // 20 + own_inc // 2, 5000))`; if no clock fields, default 1000ms. For `go movetime N` use N; for `go depth N` use `max_depth=N` with a generous time limit.
  - Output helper: `def _send(line: str) -> None: print(line, flush=True)` — stdout only.
  - Logging: `logger = logging.getLogger("src.uci")`; configure handler in `main()` only.
- **MIRROR**: ERROR_HANDLING (catch parse errors, log to stderr, continue); LOGGING_PATTERN (stderr only, configure in `main()`).
- **IMPORTS**: `import sys, logging`; `from src.board import Board, STARTING_FEN`; `from src.game import GameState`; `from src.moves import uci_to_move, move_to_uci, generate_legal_moves`; `from src.engine import choose_move`.
- **GOTCHA**:
  - stdout purity: NEVER `print` debug to stdout. Use `logger.debug/info` (stderr) for diagnostics.
  - `position fen` may use the 6-field FEN; `Board.from_fen` already parses it (`src/board.py:43-80`).
  - For `bestmove`, if the game is over (`generate_legal_moves` empty) emit `bestmove 0000` so the GUI/lichess-bot knows there is no move.
  - `go` parsing: tokenize; pick known tokens; ignore unknown options gracefully.
- **VALIDATE**: Manual + automated (Task 11). Required: `uci`→`uciok`; `isready`→`readyok`; `position startpos moves e2e4` then internal board has `wP` on e4 (verified via a debug option or via the `go` bestmove being a legal black reply); `go movetime 1000`→`bestmove <legal>`; promotion position returns a `bestmove` ending in a promotion letter; checkmate/stalemate positions return `bestmove 0000` without crashing.

### Task 5: Create `src/lichess_client.py` — Lichess BOT API HTTP client
- **ACTION**: Implement a thin, stdlib-only HTTP client for the Lichess BOT API.
- **IMPLEMENT**: A `LichessClient` class:
  - `__init__(self, token: str, base_url: str = "https://lichess.org", user_agent: str = "glm_cc_chess")` — store token (never log it), build opener.
  - `_request(self, method, path, data=None, stream=False, timeout=...)`: build `urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Bearer {self.token}", "User-Agent": self.user_agent})`; return `urllib.request.urlopen(req, timeout=...)`.
  - `get_profile() -> dict`: `GET /api/account` (validates token).
  - `stream_events() -> iterator[dict]`: `GET /api/stream/event`; iterate response lines, `json.loads` non-empty lines, yield parsed events. (Caller runs this in a thread.)
  - `stream_game(game_id) -> iterator[dict]`: `GET /api/bot/game/stream/{game_id}`; same NDJSON handling. First yielded object is the `gameFull` object (has `state`); subsequent are `gameState`/`opponentGone`.
  - `accept_challenge(challenge_id)`, `decline_challenge(challenge_id, reason="generic")`: `POST /api/challenge/{id}/accept` | `/decline`.
  - `make_move(game_id, uci: str, draw: bool = False)`: `POST /api/bot/game/{game_id}/move/{uci}` (+ `?draw=1` if draw).
  - `abort(game_id)`, `resign(game_id)`: `POST /api/bot/game/{id}/abort` | `/resign`.
  - `get_game_pgn(game_id) -> str`: `GET /api/game/export/{game_id}` (for PGN collection).
- **MIRROR**: ERROR_HANDLING (raise a custom `LichessAPIError` on non-2xx; let callers catch); LOGGING_PATTERN (logger, never log token).
- **IMPORTS**: `import json, urllib.request, urllib.error, logging`; `from typing import Iterator`.
- **GOTCHA**:
  - NDJSON streaming: iterate `for raw in response:` (HTTPResponse yields bytes lines); `line = raw.decode().strip()`; skip empty keep-alive lines; `json.loads(line)`.
  - Read timeout: Lichess sends keep-alive empty lines ~every 7s, so a 60-300s socket timeout is safe and avoids premature TimeoutError. Set `timeout=300` for stream endpoints.
  - `urllib` POST with no body: pass `data=b""` (or `None`) and `method="POST"`. Some endpoints return 200 with empty body — ignore body.
  - 429 rate limit: catch `urllib.error.HTTPError` with code 429, log, and back off (sleep via `time.sleep` in the controller, not the client).
  - Never put the token in a URL query or log line.
- **VALIDATE**: Unit tests with `unittest.mock.patch("urllib.request.urlopen")` returning a fake response yielding NDJSON lines (Task 11). Verify endpoint paths and headers are constructed correctly; verify token is in the `Authorization` header only.

### Task 6: Create `src/lichess_controller.py` — threading + queue orchestration
- **ACTION**: Build the bridge between the background Lichess streams and the pygame main thread via a `queue.Queue` of typed events.
- **IMPLEMENT**: A `LichessController` class:
  - Events (use `dataclass` or simple namedtuples in this module): `ChallengeReceived(challenge_id, opponent, speed, variant, color, mode)`, `GameStarted(game_id, bot_is_white, opponent_name, initial_fen, moves, wtime, btime)`, `GameUpdated(game_id, moves, wtime, btime, status, winner)`, `EngineMoved(game_id, uci)`, `GameFinished(game_id, status, winner, moves, initial_fen)`, `Status(message)`, `Error(message)`.
  - `__init__(self, token, engine_choose=choose_move, default_movetime_ms=1000)`: create `LichessClient`, `self.event_queue: queue.Queue = queue.Queue()`, `self._stop = threading.Event()`, `self._username` (filled after profile fetch).
  - `start(self)`: fetch profile (`get_profile`) to get the bot username (needed to determine color); start daemon `event_stream_thread` targeting `_event_loop`.
  - `_event_loop(self)`: iterate `client.stream_events()`; on `type=="challenge"` → push `ChallengeReceived`; on `type=="gameStart"` → start a daemon `game_thread` for `event["game"]["id"]`; on `type=="gameFinish"` → push `GameFinished`. Check `self._stop` each iteration; catch exceptions → push `Error`.
  - `_game_loop(self, game_id)`: iterate `client.stream_game(game_id)`; first object is `gameFull` → determine `bot_is_white = (white.name.lower() == self._username.lower())`, `initial_fen = obj.get("initialFen") or STARTING_FEN` (treat `"startpos"` as `STARTING_FEN`), build state from `obj["state"]`, push `GameStarted`, then `_maybe_move(game_id, initial_fen, state, bot_is_white)`. For each `gameState` object → push `GameUpdated`, then if not game-over `_maybe_move(...)`. On game-over status → push `GameFinished`.
  - `_maybe_move(self, game_id, initial_fen, state, bot_is_white)`: determine bot-to-move = `(len(state["moves"].split()) % 2 == 0) if bot_is_white else (== 1)`; if not bot's turn, return; rebuild board from `initial_fen` + moves via `GameState.make_move(uci_to_move(m))`; compute `time_limit_ms = self._time_budget(state, bot_is_white)`; `move = self.engine_choose(board, time_limit_ms=time_limit_ms)`; if move is None → return (game over); `uci = move_to_uci(move)`; `client.make_move(game_id, uci)`; push `EngineMoved(game_id, uci)`.
  - `_time_budget(self, state, bot_is_white) -> int`: `remaining = state["wtime"] if bot_is_white else state["btime"]`; `inc = state["winc"] if bot_is_white else state["binc"]`; if `remaining <= 0` (correspondence/no clock) → return `self.default_movetime_ms`; else `return max(200, min(remaining // 20 + inc // 2, 5000))`.
  - `accept_challenge(self, id)` / `decline_challenge(self, id)` / `resign(self, game_id)` / `abort(self, game_id)`: call client (thread-safe enough; urllib is okay from any thread). Wrap in try/except → push `Error` on failure.
  - `stop(self)`: set `self._stop`; daemon threads die on process exit; best-effort close any held responses.
  - `is_game_over_status(status) -> bool` helper: `status not in ("created", "started")`.
- **MIRROR**: ERROR_HANDLING (per-line try/except, push `Error` events, never crash threads); LOGGING_PATTERN (logger in this module; no token).
- **IMPORTS**: `import threading, queue, logging`; `from src.board import Board, STARTING_FEN`; `from src.game import GameState`; `from src.moves import uci_to_move, move_to_uci`; `from src.engine import choose_move`; `from src.lichess_client import LichessClient`.
- **GOTCHA**:
  - **Thread safety**: background threads must NOT call any pygame function. They only push events to `self.event_queue`. The GUI main loop drains it.
  - **Color detection**: use `white.name`/`black.name` vs the bot username from `get_profile` (`/api/account`), exactly as lichess-bot does (`model.py:209`). Do not rely on a `color` field in `gameFull`.
  - **Rebuild every update**: each `gameState.moves` is the FULL move list — always rebuild from `initial_fen` rather than incrementally applying. This is robust to missed events.
  - `initialFen` may be `"startpos"` (string) or a real FEN or `null`; normalize `"startpos"`/`None` → `STARTING_FEN`.
  - Daemon threads: set `thread.daemon = True` so they don't block process exit; `stop()` sets the flag and the read loops exit after the next keep-alive line.
  - Don't post a move if the game is already over (check `is_game_over_status`).
- **VALIDATE**: Unit tests (Task 11) with a mocked `LichessClient` whose `stream_game` yields a scripted `gameFull` + `gameState` sequence; assert the controller emits `GameStarted`/`GameUpdated`/`EngineMoved` in the right order, picks the right turn, and rebuilds the board correctly. Assert `_time_budget` boundaries.

### Task 7: Update `src/gui.py` — add "AI vs Lichess" mode + review
- **ACTION**: Add a 4th menu button, a `lichess` mode, live game rendering, challenge accept/decline, resign/abort, and a review mode.
- **IMPLEMENT** (extend `ChessGUI`):
  - `__init__`: add fields: `self.mode` now also `"lichess"`; `self.lichess_controller = None`; `self.lichess_token = None`; `self.pending_challenge = None`; `self.lichess_game = None` (dict with `game_id`, `bot_is_white`, `opponent_name`, `initial_fen`, `moves` (list[str]), `wtime`, `btime`, `status`, `winner`); `self.review_mode = False`; `self.review_index = 0`; `self.lichess_status = "Idle"`.
  - `_get_lichess_token(self) -> str | None`: read `os.environ.get("LICHESS_BOT_TOKEN")`; if absent, read `lichess/config.yml` (simple parse for a `token:` line) — but only the gitignored real file, never the example. Return None if missing.
  - `draw_menu`: add `self._draw_button("AI vs Lichess", WINDOW_WIDTH // 2 - 100, 380, 200, 40, lambda: self._start_lichess())` (after the Engine vs Engine button at `gui.py:511-512`).
  - `_start_lichess(self)`: token = `_get_lichess_token()`; if None → set `self.mode = "lichess_no_token"` (or a sub-state) and show instructions in the panel with a Back button; else create `LichessController(token)`, `self.lichess_controller = controller`, `controller.start()`, `self.mode = "lichess"`, `self.lichess_status = "Connecting…"`.
  - `_drain_lichess_events(self)`: called each frame in `run()` when `mode == "lichess"`; `while True: try: ev = self.lichess_controller.event_queue.get_nowait(); except queue.Empty: break; self._handle_lichess_event(ev)`.
  - `_handle_lichess_event(self, ev)`: dispatch by type:
    - `ChallengeReceived` → `self.pending_challenge = ev`; `self.lichess_status = "Challenge from {opponent}"`.
    - `GameStarted` → `self.lichess_game = {...}`; set `self.game = GameState(Board.from_fen(ev.initial_fen))` for rendering; `self.flipped = not ev.bot_is_white` (show bot's perspective); `self.review_mode = False`; `self.lichess_status = "Playing {opponent}"`.
    - `GameUpdated` → update `lichess_game` moves/clocks/status; `_apply_lichess_moves()` to refresh `self.game` board for display.
    - `EngineMoved` → (optional) immediately reflect; the next `GameUpdated` will reconcile. Avoid double-application: rely on full rebuild from `GameUpdated`.
    - `GameFinished` → `self.lichess_status = _result_text(status, winner)`; `self.game_over = True`; switch to review: `self.review_mode = True`; `self.review_index = len(self.lichess_game["moves"])`.
    - `Status` → update `self.lichess_status`.
    - `Error` → `self.lichess_status = "Error: {message}"`.
  - `_apply_lichess_moves(self)`: rebuild `self.game = GameState(Board.from_fen(initial_fen))`; for each uci in `lichess_game["moves"]`: `self.game.make_move(uci_to_move(m))`. (Full rebuild each update — matches controller.)
  - `draw_panel` (extend): when `mode == "lichess"`, draw title "vs Lichess", opponent name, clocks (W/B from `wtime`/`btime` formatted mm:ss), status, and buttons: `[Accept]`/`[Decline]` (only if `pending_challenge`), `[Resign]`/`[Abort]` (only if game active and not over), `[Menu]` (stops controller). In review mode draw `[<<] [\<] [>] [>>]` and the move list with the current `review_index` highlighted.
  - Review rendering: when `self.review_mode`, the board is rebuilt from `initial_fen` + `moves[:self.review_index]` (NOT the full list). Prev/Next/Home/End adjust `review_index` clamped to `[0, len(moves)]`.
  - Button actions: `_accept_challenge()` → `controller.accept_challenge(id)`; clear pending. `_decline_challenge()` → `controller.decline_challenge(id)`. `_resign_lichess()` → `controller.resign(game_id)`. `_abort_lichess()` → `controller.abort(game_id)`. `_menu_from_lichess()` → `controller.stop()`; `self.mode = "menu"`.
  - `run` loop: in the `else` branch (non-menu), when `mode == "lichess"`: call `self._drain_lichess_events()`; draw board + lichess panel; do NOT call `engine_move()` (the controller's threads handle engine moves). Handle clicks via `handle_button_click` only (no click-to-move). Add keyboard: `N` → back to menu (stop controller); Left/Right → review step when in review mode.
- **MIRROR**: GUI_BUTTON_PATTERN (rebuild `_buttons` each frame); GUI_RUN_LOOP_PATTERN (no pygame from threads; drain queue in main loop). Use existing `_draw_button`, `draw_board` (reuse as-is — it reads `self.game.board`), `_board_to_screen`, `PIECE_SYMBOLS`.
- **IMPORTS**: add `import os, queue, logging`; `from src.lichess_controller import LichessController`; `from src.moves import uci_to_move` (already imports `generate_legal_moves, move_to_algebraic, Move` — extend).
- **GOTCHA**:
  - **Reuse `draw_board` as-is**: it renders `self.game.board`, last_move, check highlight, etc. As long as `_apply_lichess_moves()`/review keeps `self.game` correct, the board renders correctly with no new drawing code.
  - **Do not double-move**: `EngineMoved` should NOT re-apply a move to `self.game` (the server's `GameUpdated` with the full move list is the source of truth). If you apply on `EngineMoved`, skip on the next `GameUpdated` for the same move count. Simplest: ignore `EngineMoved` for board state; use it only to update status text ("Engine played e2e4").
  - **Flip perspective**: `self.flipped = not bot_is_white` so the bot's pieces are at the bottom. Existing flip logic (`_board_to_screen`, `square_from_pos`) handles this.
  - **Stop controller on menu/quit**: in `_menu_from_lichess` and on `pygame.QUIT` while in lichess mode, call `controller.stop()`.
  - **No-token state**: keep it inside the `lichess` mode family but render an instructions panel + Back button; do not start the controller.
- **VALIDATE**: Manual run (`python -m src.main`): menu shows 4 buttons; clicking "AI vs Lichess" with no token shows instructions; with a valid token shows "Connecting…" then accepts a challenge and plays a move on the board; clocks/status update; after game ends, review buttons step through moves. Existing "Play as White/Black" and "Engine vs Engine" still work unchanged.

### Task 8: Create `lichess/config.yml.example`, `lichess/run_engine.bat`, `lichess/README_LICHESS.md`
- **ACTION**: Provide the optional lichess-bot bridge assets + full integration docs.
- **IMPLEMENT**:
  - `lichess/config.yml.example` — current lichess-bot format (verified against upstream `config.yml.default`):
    ```yaml
    token: "LICHESS_BOT_TOKEN"      # set the real token via env var or a gitignored copy
    url: "https://lichess.org/"
    engine:
      dir: "."                      # repo root (where run_engine.bat lives)
      name: "run_engine.bat"        # wrapper that runs `python -m src.uci`
      protocol: "uci"
      ponder: false
      uci_options:
        Move Overhead: 100
        Threads: 1
        Hash: 64
    abort_time: 30
    move_overhead: 2000
    challenge:
      concurrency: 1
      variants:
        - standard
      time_controls:
        - bullet
        - blitz
        - rapid
        - classical
      modes:
        - casual
        - rated
    ```
  - `lichess/run_engine.bat`:
    ```bat
    @echo off
    REM Wrapper so lichess-bot can launch the built-in UCI engine.
    REM Requires Python on PATH; run from the repo root.
    python -m src.uci
    ```
  - `lichess/README_LICHESS.md` — document:
    1. **Dedicated BOT account**: create a new Lichess account, then upgrade it to a BOT at `https://lichess.org/api/bot/account/upgrade` (or the account settings). Do NOT use a normal account.
    2. **Token**: generate an API token at `https://lichess.org/account/oauth/token` with the `bot:play` scope. Set it as `LICHESS_BOT_TOKEN` env var, OR put it in a gitignored `lichess/config.yml` copy (never `config.yml.example`). Never commit it.
    3. **In-GUI mode (PRIMARY)**: run `python -m src.main` (or the packaged exe), click "AI vs Lichess". The app streams incoming challenges; accept one and watch the engine play on the board; review the game afterward.
    4. **Test the UCI engine locally first**: `python -m src.uci` then type `uci`, `isready`, `position startpos moves e2e4 e7e5`, `go movetime 1000` → expect `bestmove ...`.
    5. **Optional lichess-bot headless bridge**: clone `lichess-bot-devs/lichess-bot`, copy `lichess/config.yml.example` to `lichess-bot/config.yml`, set the token, set `engine.dir`/`engine.name` to this repo's `run_engine.bat`, run `python lichess-bot.py`.
    6. **Collect PGN / game results**: after a game, `GET /api/game/export/{gameId}` (client `get_game_pgn`) or download from the game page; track results manually for rating evaluation.
    7. **Lichess TOS**: no sandbagging, no boosting, no constant aborting.
- **MIRROR**: N/A (docs + config).
- **IMPORTS**: N/A.
- **GOTCHA**: The real `lichess/config.yml` (with the token) must be gitignored (Task 9); only `config.yml.example` is committed. `run_engine.bat` requires Python on the lichess-bot host (documented limitation; a self-contained engine exe is a future option).
- **VALIDATE**: `lichess/config.yml.example` parses as valid YAML ( eyeball / optional `python -c "import yaml; yaml.safe_load(open('lichess/config.yml.example'))"` if PyYAML is available — but do NOT add PyYAML as a dependency; just keep the file hand-valid). README links and instructions are accurate per the verified endpoints.

### Task 9: Update `.gitignore` for token/config exclusion
- **ACTION**: Ensure no real token or real config is ever committed.
- **IMPLEMENT**: append:
  ```
  # Lichess secrets — never commit real tokens or real config
  lichess/config.yml
  .lichess_token
  *.token
  lichess_bot.log
  ```
- **MIRROR**: CONFIG_PATTERN (extend `.gitignore`).
- **GOTCHA**: Do NOT ignore `lichess/config.yml.example` or `lichess/README_LICHESS.md` — those are committed templates.
- **VALIDATE**: `git check-ignore -v lichess/config.yml` lists it as ignored; `git check-ignore -v lichess/config.yml.example` returns nothing (not ignored).

### Task 10: Update `GLM_CC_Chess.spec` + verify packaging
- **ACTION**: Make sure the GUI exe bundles the new modules and still builds.
- **IMPLEMENT**: in `GLM_CC_Chess.spec` change `hiddenimports` to include the new modules:
  ```python
  hiddenimports=['src.board', 'src.moves', 'src.game', 'src.engine',
                 'src.gui', 'src.uci', 'src.lichess_client', 'src.lichess_controller'],
  ```
  (`src.uci` is not imported by the GUI but is harmless to list; the lichess modules are imported by `gui` so PyInstaller finds them, but listing is belt-and-suspenders.) The windowed GUI exe does NOT need `console=True` (UCI runs from source via `python -m src.uci`, not from the windowed exe).
- **MIRROR**: existing spec style (`GLM_CC_Chess.spec:13`).
- **IMPORTS**: N/A (spec).
- **GOTCHA**: stdlib `urllib`, `ssl`, `json`, `threading`, `queue`, `logging` are bundled automatically by PyInstaller — no extra `hiddenimports` needed for them. Network access at runtime is required for Lichess mode (Windows firewall may prompt on first run — document in release/readme.txt).
- **VALIDATE**: `python package.py` (or `python -m PyInstaller --clean --noconfirm GLM_CC_Chess.spec`) builds; `release/GLM_CC_Chess.exe` launches; the "AI vs Lichess" button appears. (Full release validation in Task 13.)

### Task 11: Add/update tests
- **ACTION**: Cover UCI, Lichess client/controller, UCI move conversion, and the `choose_move` interface.
- **IMPLEMENT**:
  - `tests/test_moves.py` (extend `class TestMoveNotation` or add `class TestUciConversion`): round-trip `move_to_uci`/`uci_to_move` for normal, capture, castling ("e1g1"/"e1c1"), en passant, promotion ("e7e8q"); assert `uci_to_move("e2e4") == (6,4,4,4,None)`.
  - `tests/test_engine.py` (add `class TestChooseMove`): `choose_move(Board.from_fen(STARTING_FEN), time_limit_ms=300)` is legal; `choose_move(..., max_depth=2)` is legal; promotion FEN returns a move whose UCI ends in a promo letter; a checkmate FEN returns `None`; a stalemate FEN returns `None` without crashing.
  - `tests/test_uci.py` (new, subprocess-driven integration):
    - Helper `_run_uci(commands: list[str]) -> str` that spawns `python -m src.uci` via `subprocess.run`, feeds commands joined by newlines to stdin, returns stdout.
    - `test_uci_returns_uciok`: send `uci`+`quit` → assert `uciok` and `id name` in output.
    - `test_isready_returns_readyok`: assert `readyok`.
    - `test_position_startpos_moves_e2e4`: send `position startpos moves e2e4`, `go movetime 200` → `bestmove` is a legal black reply (parse UCI, apply to a board, assert in `generate_legal_moves`).
    - `test_go_movetime_1000_returns_legal_bestmove`: from startpos, `go movetime 1000` → legal move.
    - `test_go_depth_1_returns_legal_bestmove`.
    - `test_promotion_handled`: from a FEN with a pawn about to promote, `go movetime 300` → `bestmove` length 5 with a promo letter.
    - `test_checkmate_no_crash`: from a checkmate FEN, `go movetime 300` → `bestmove 0000` (or no crash).
    - `test_stalemate_no_crash`: from a stalemate FEN, `go movetime 300` → `bestmove 0000` (or no crash).
    - `test_ucinewgame_resets`.
    - Assert stdout contains ONLY UCI tokens (no debug prose) — e.g., split output lines all start with known UCI tokens (`id`, `uciok`, `readyok`, `bestmove`, `info`).
  - `tests/test_lichess.py` (new):
    - `class TestLichessClient`: with `unittest.mock.patch("urllib.request.urlopen")`, assert `stream_events()`/`stream_game()` parse NDJSON lines; `make_move` posts to `/api/bot/game/{id}/move/{uci}` with `Authorization: Bearer <token>`; `accept_challenge` posts to `/api/challenge/{id}/accept`; the token never appears in any logged/asserted URL query.
    - `class TestLichessController`: use a fake `LichessClient` whose `stream_game` yields a scripted `gameFull` (white.name == bot username) then a `gameState` with one extra move; assert the controller pushes `GameStarted`, determines `bot_is_white` correctly, rebuilds the board from `initial_fen` + moves, and calls `engine_choose` + `make_move` only on the bot's turn. Test `_time_budget` boundaries (correspondence → default; normal → clamped). Test `is_game_over_status`.
    - `class TestTurnLogic`: assert bot-to-move parity (white: even move count; black: odd) via a small helper or the controller.
    - `class TestTokenLoading`: `LichessController`/GUI `_get_lichess_token` reads `LICHESS_BOT_TOKEN` env var (use `monkeypatch.setenv`); falls back to `lichess/config.yml`; returns None when absent.
- **MIRROR**: TEST_STRUCTURE (pytest classes, `Board.from_fen` setup, AAA, `generate_legal_moves` legality). For subprocess UCI tests, mirror the engine-completion test pattern (`test_game.py:174-188`).
- **IMPORTS**: `import subprocess, unittest.mock, pytest`; `from src.uci import ...` (if testable functions) or drive via subprocess; `from src.lichess_client import LichessClient`; `from src.lichess_controller import LichessController`.
- **GOTCHA**:
  - UCI subprocess tests: send `quit` at the end so the process exits; use `timeout=30` on `subprocess.run`; on Windows use `text=True` and proper newline handling.
  - Mocking `urllib.request.urlopen`: return a context-manager-like fake whose iteration yields bytes lines (`b'{"type":"gameStart","game":{...}}\n'`, `b"\n"` keep-alive). Match the real `HTTPResponse` interface enough for the client's `for raw in resp:` loop.
  - Don't hit the real Lichess API in tests — always mock.
  - `choose_move` tests: keep `time_limit_ms` small (200-400) so the suite stays fast.
- **VALIDATE**: `pytest tests/ -v` all green; `pytest tests/ --cov=src --cov-report=term-missing` shows new modules covered (target ≥80% on `uci.py`, `lichess_client.py`, `lichess_controller.py`, and the new helpers).

### Task 12: Create root `README.md`, update `release/readme.txt` + `CLAUDE.md`
- **ACTION**: Add developer-facing docs and update user-facing docs + the Claude guide.
- **IMPLEMENT**:
  - `README.md` (new, root): project overview, tech stack, the three play contexts (Human vs AI, Engine vs Engine, AI vs Lichess), how to run (`python -m src.main`, `python -m src.uci`), how to test (`pytest tests/ -v`, coverage), how to package (`python package.py`), Lichess setup summary with a link to `lichess/README_LICHESS.md`, token security note, and the no-browser-automation constraint.
  - `release/readme.txt` (update): add "AI vs Lichess" to GAME MODES; add a note that it requires a dedicated Lichess BOT account + token (set `LICHESS_BOT_TOKEN`) and an internet connection; mention that on first run Windows Firewall may prompt for network access. Keep VERSION at 1.0.0 (or bump to 1.1.0 if desired).
  - `CLAUDE.md` (update): add `src/uci.py`, `src/lichess_client.py`, `src/lichess_controller.py` to the structure list; add a short "Lichess Integration" note under Architecture (in-GUI BOT API client + optional lichess-bot UCI bridge; token via `LICHESS_BOT_TOKEN`).
- **MIRROR**: existing `release/readme.txt` section style (uppercase headers with `---`); `CLAUDE.md` structure table style.
- **GOTCHA**: `release/readme.txt` version line is parsed by `package.py:read_version_from_readme` (regex `^VERSION\n-=+\n([\d.]+)`) — keep that exact format if you bump the version.
- **VALIDATE**: `README.md` renders; `release/readme.txt` VERSION section still parses (run `python -c "from package import read_version_from_readme; from pathlib import Path; print(read_version_from_readme(Path('release/readme.txt')))"`).

### Task 13: Validation — tests, UCI, GUI, packaging, release
- **ACTION**: Run the full validation gauntlet and confirm completion criteria.
- **IMPLEMENT** (run each, expect pass):
  - `pytest tests/ -v` — all tests pass (existing + new).
  - `pytest tests/ --cov=src --cov-report=term-missing` — no regressions; new modules ≥80%.
  - UCI manual: `python -m src.uci` then `uci` / `isready` / `position startpos moves e2e4 e7e5` / `go movetime 1000` → `bestmove <legal>`; `quit`.
  - GUI manual: `python -m src.main` → menu has 4 buttons; Play as White/Black and Engine vs Engine unchanged; AI vs Lichess with a valid BOT token connects and plays a live game; review works after game end.
  - Packaging: `python package.py` → builds `release/GLM_CC_Chess.exe` + zip; launch the exe from `release/` → menu works, AI vs Lichess button present.
  - Security: `git status` shows no token files staged; `git check-ignore lichess/config.yml` confirms ignore; grep repo for any hardcoded token (none).
- **MIRROR**: N/A.
- **GOTCHA**: If the GUI network test is blocked by firewall/sandbox, document it as a manual step rather than failing CI. The automated tests cover the client/controller via mocks, so network is not required for `pytest`.
- **VALIDATE**: Every checkbox in Acceptance Criteria below is satisfied.

---

## Testing Strategy

### Unit Tests

| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| `move_to_uci` normal | `(6,4,4,4,None)` | `"e2e4"` | no |
| `move_to_uci` castle | `(7,4,7,6,None)` | `"e1g1"` | castling encoding |
| `move_to_uci` promo | `(1,4,0,4,"Q")` | `"e7e8q"` | lowercase promo |
| `uci_to_move` promo | `"e7e8q"` | `(1,4,0,4,"Q")` | uppercase restore |
| `choose_move` legal | startpos, `time_limit_ms=300` | move in `generate_legal_moves` | time limit |
| `choose_move` depth | startpos, `max_depth=2` | legal move | depth limit |
| `choose_move` terminal | checkmate FEN | `None` | no legal move |
| UCI `uci` | `uci\nquit` | contains `uciok`, `id name` | protocol |
| UCI `isready` | `isready\nquit` | contains `readyok` | protocol |
| UCI `position ... moves e2e4` + `go movetime 200` | stdin sequence | `bestmove <legal black reply>` | move apply |
| UCI promotion | promo FEN + `go movetime 300` | `bestmove` len 5, promo letter | promotion |
| UCI checkmate | mate FEN + `go movetime 300` | `bestmove 0000` (no crash) | terminal |
| Lichess `make_move` endpoint | mocked urlopen | POST to `/api/bot/game/{id}/move/{uci}`, Bearer header | auth |
| Controller turn parity | bot=white, 0 moves | bot to move (even) | color logic |
| Controller turn parity | bot=black, 1 move | bot to move (odd) | color logic |
| Controller `_time_budget` correspondence | wtime=0 | default movetime | no clock |
| Token loading | env var set | returns token | env |
| Token loading | absent | `None` | missing secret |

### Edge Cases Checklist
- [x] Empty move list (startpos, bot=white → bot to move immediately)
- [x] Promotion (UCI + Lichess + engine)
- [x] Castling (UCI king from-to; Lichess rebuild)
- [x] En passant (UCI from-to only; make_move infers)
- [x] Checkmate / stalemate (engine returns None; UCI emits `0000`; Lichess skips posting)
- [x] No token configured (GUI shows instructions, does not start controller)
- [x] Network failure / 429 rate limit (logged as `Error` event, UI shows error, no crash)
- [x] Opponent disconnects / `opponentGone` (ignored gracefully)
- [x] Multiple concurrent games (one game thread per `gameStart`; GUI shows the current/first)
- [x] Review at start (index 0 = initial position) and at end (full game)
- [x] Process exit while streams open (daemon threads; `stop()` sets flag)

---

## Validation Commands

### Static Analysis
```bash
# Python syntax / import check (no type checker in project)
python -c "import src.uci, src.lichess_client, src.lichess_controller, src.gui, src.engine, src.moves"
```
EXPECT: no ImportError / SyntaxError.

### Unit Tests
```bash
pytest tests/ -v
```
EXPECT: All tests pass (existing + new UCI/Lichess/conversion/choose_move tests).

### Coverage
```bash
pytest tests/ --cov=src --cov-report=term-missing
```
EXPECT: No regressions; new modules (`uci`, `lichess_client`, `lichess_controller`) ≥80%.

### UCI Manual
```bash
python -m src.uci
# then type:  uci  ->  uciok
#            isready  ->  readyok
#            position startpos moves e2e4 e7e5
#            go movetime 1000  ->  bestmove <legal>
#            quit
```
EXPECT: Pure UCI output on stdout; `bestmove` is a legal move.

### GUI Manual
```bash
python -m src.main
```
EXPECT: Menu shows 4 buttons; existing modes work; "AI vs Lichess" connects with a valid token and plays a live, reviewable game.

### Packaging
```bash
python package.py
```
EXPECT: `release/GLM_CC_Chess.exe` (+ zip) produced; exe launches and shows the AI vs Lichess button.

### Release Validation
```bash
# Launch the packaged exe directly from release/
release/GLM_CC_Chess.exe
```
EXPECT: App starts; user can start a game; AI vs Lichess menu entry present.

### Security Validation
```bash
git check-ignore -v lichess/config.yml
git status --porcelain   # no token files staged
```
EXPECT: `lichess/config.yml` ignored; no real token in git.

### Manual Validation
- [ ] Human-vs-AI still works (Play as White / Black)
- [ ] Engine-vs-Engine still runs to completion
- [ ] AI vs Lichess: no-token state shows instructions
- [ ] AI vs Lichess: with token, accepts a challenge and plays a legal move on the board
- [ ] Live game board updates with opponent + engine moves; clocks/status shown
- [ ] Finished game enters review; Prev/Next/Home/End step through correctly
- [ ] UCI `go movetime 1000` returns a legal bestmove
- [ ] UCI checkmate/stalemate positions return `bestmove 0000` without crashing
- [ ] No hardcoded token anywhere in the repo
- [ ] Packaged exe launches from `release/`

---

## Acceptance Criteria
- [ ] All 13 tasks completed
- [ ] `pytest tests/ -v` passes (existing + new)
- [ ] Coverage ≥80% on new modules; no regressions
- [ ] UCI mode works from the command line (`uciok`, `readyok`, `bestmove <legal>`)
- [ ] `go movetime 1000` returns a legal bestmove; terminal positions return `0000` without crashing
- [ ] AI vs Lichess GUI mode connects with a BOT token, plays a live game on the board
- [ ] Finished Lichess games are reviewable move-by-move in the GUI
- [ ] Existing Human-vs-AI and Engine-vs-Engine modes unchanged
- [ ] Lichess token is never hardcoded or committed (env var / gitignored config)
- [ ] No browser automation; dedicated BOT account only
- [ ] Optional lichess-bot bridge documented + `config.yml.example` + `run_engine.bat` provided
- [ ] `prompt_lichess_bot.md` updated to match the agreed design
- [ ] Windows packaged release still builds and launches from `release/`
- [ ] `README.md`, `release/readme.txt`, `CLAUDE.md` updated

## Completion Checklist
- [ ] Code follows discovered patterns (module docstrings, `from __future__ import annotations`, PascalCase classes, snake_case methods, UPPER_SNAKE constants)
- [ ] Error handling matches codebase style (`ValueError`/sentinel `None`; network layers catch + log + continue, never crash GUI)
- [ ] Logging on stderr only for UCI; never logs the token
- [ ] Tests follow pytest class patterns with FEN setup + legality assertions
- [ ] No hardcoded values (endpoints in a constants dict; token from env/config; budgets via named helpers)
- [ ] Documentation updated (root README, release readme, CLAUDE.md, lichess/README_LICHESS.md, prompt)
- [ ] No unnecessary scope additions (no new deps, no engine rewrite, no matchmaking)
- [ ] Self-contained — no questions needed during implementation

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Background threads call pygame and crash/hang | Medium | High | Strict rule: threads only push to `queue.Queue`; main loop drains + updates pygame. No exceptions. |
| `urllib` NDJSON streaming quirks on Windows (timeouts, buffering) | Medium | Medium | Long socket timeout (300s) relying on ~7s keep-alive lines; daemon threads; `stop()` flag; per-line try/except. |
| Engine flags (loses on time) in fast Lichess games | Medium | Medium | Simple time budget `min(own_time//20 + inc//2, 5000)` with `move_overhead`; document that very fast time controls are risky for a depth-4 engine. |
| Token leaked to git/logs | Low | High | `.gitignore` rules; env var preferred; never log token; `git check-ignore` validation in Task 13. |
| PyInstaller misses a stdlib network module | Low | Medium | `urllib`/`ssl`/`json`/`threading` auto-bundled; added `hiddenimports` for `src.*`; verify exe launches. |
| Color/turn detection wrong → engine plays opponent's move | Low | High | Mirror lichess-bot's `white.name == username` logic; full-board rebuild from move list each update; unit tests for parity. |
| Review mode double-applies moves / desyncs | Medium | Medium | Review rebuilds from `initial_fen + moves[:index]` each step; never mutate the live game state. |
| Lichess TOS violation (sandbagging/boosting) | Low | High | Document TOS in README_LICHESS.md; this is a single BOT playing honestly, not a boosting tool. |
| Pre-existing `engine.py:297` walrus bug (killer moves) | Low | Low | Out of scope; does not affect best-move legality. Note only; do not fix here. |
| BOT account upgrade is a manual Lichess step | High (by design) | Low | Document clearly; cannot be automated per Lichess policy. |

## Notes
- **Why stdlib `urllib` instead of `requests`**: keeps the dependency list unchanged (`pygame` only) and PyInstaller packaging simple; NDJSON streaming over `urllib` is reliable for Lichess's keep-alive pattern. `lichess-bot` itself uses `requests`, but that's the optional external bridge, not our in-GUI client.
- **Why a fresh `ChessEngine` per `choose_move`**: the engine stores search scratch (`_killer_moves`, `best_move`, timers) that is reset at the top of `get_best_move`, but constructing fresh avoids any cross-call leakage and makes the UCI/Lichess callers stateless and thread-safe.
- **Two Lichess paths by design**: the in-GUI BOT API client (primary, user-facing, reviewable) and the lichess-bot UCI bridge (optional, headless 24/7). Both share `src/uci.py` (the bridge) and the core engine + `src/moves.py` UCI helpers. The in-GUI client does NOT go through UCI — it calls `choose_move` directly and posts UCI moves to the Lichess API.
- **PGN/rating collection**: `LichessClient.get_game_pgn(game_id)` wraps `GET /api/game/export/{id}`; results are tracked manually (no automated rating evaluation, per NOT Building).
- **`run_engine.bat` requires Python on the host**: acceptable for the optional bridge; a self-contained engine `.exe` (second PyInstaller spec, `console=True`) is a documented future enhancement, not in this pass.