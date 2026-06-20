# Code Review: AI vs Lichess — In-GUI BOT Integration

**Reviewed**: 2026-06-20
**Branch**: `feat/ai-vs-lichess-gui-integration` (local uncommitted changes)
**Decision**: **APPROVE with comments** (after fixes — no remaining CRITICAL/HIGH)
**Reviewers**: security-reviewer + python-reviewer agents (parallel), plus lead

## Summary
Independent security and Python reviews found **3 HIGH issues** (1 security, 2 crash/DoS, 1 type-safety) — all reproducible by execution. All HIGH issues have been fixed and verified. Remaining open items are MEDIUM/LOW structural quality improvements (file/function size, DRY) recommended as follow-ups, none blocking.

## Findings

### CRITICAL
None.

### HIGH (all FIXED)

| # | Finding | File:Line | Fix Applied |
|---|---|---|---|
| H1 | **Bearer token leaks on HTTP redirect** — `urllib`'s default redirect handler strips only `Content-Length`/`Content-Type`, NOT `Authorization`; a 3xx would forward `Bearer <token>` to a 3rd-party host (account-compromising). | `src/lichess_client.py:81` | Added `_NoRedirectHandler` + `install_opener` (refuse to follow) + a 3xx guard in `_request` that raises `LichessAPIError`. Regression tests: `test_redirect_handler_refuses_to_follow`, `test_3xx_response_raises_instead_of_leaking_token`. |
| H2 | **UCI `go` crashes on non-integer tokens** — `int(tokens[i+1])` uncaught → `ValueError` kills the engine process (verified: `go movetime abc` → exit 1). | `src/uci.py:107-122` | Added `_safe_int()` (returns None on failure) + per-token skip with warning. Regression tests: `test_bad_go_int_does_not_crash_parse`, `test_bad_go_int_does_not_crash_engine`. |
| H3 | **UCI `position fen` crashes on malformed FEN** — `Board.from_fen` uncaught → `IndexError` on short FEN (verified: `position fen not-a-fen` → exit 1). | `src/uci.py:71` | Wrapped `Board.from_fen(...)` in `try/except (ValueError, IndexError)` → log + return. Regression test: `test_malformed_fen_does_not_crash_engine`. |
| H4 | **Lost type safety** — `ChooseMoveFn = Callable[..., Optional[tuple]]` discarded the `Move` element type. | `src/lichess_controller.py:93` | Imported `Move`, changed to `Optional[Move]`. |

### MEDIUM

| # | Finding | File:Line | Status |
|---|---|---|---|
| M1 | Token stored on GUI instance (`self.lichess_token`) and never read — widens leak surface. | `src/gui.py` | **FIXED** — dropped the attribute; token is now a local passed to `LichessController`. |
| M2 | Missing return-type annotations on `LichessClient` request helpers. | `src/lichess_client.py` | **FIXED** — `-> http.client.HTTPResponse`, `-> None`, `-> dict`, `-> str`. |
| M3 | Token-loading test wrote to the real `lichess/config.yml` (backup/restore, interrupt-risk). | `tests/test_lichess.py` | **FIXED** — `_get_lichess_token(config_path=...)` now injectable; test uses `tmp_path`. |
| M4 | `gui.py` 916 lines exceeds the 800-line guideline. | `src/gui.py` | **DEFERRED** — extract Lichess panel/event code into `src/gui_lichess.py` (recommended follow-up). |
| M5 | `_handle_lichess_event` ~64 lines / 8-branch `isinstance` chain exceeds the 50-line guideline. | `src/gui.py:237-301` | **DEFERRED** — convert to dict dispatch `{EventType: handler}` (recommended follow-up). |
| M6 | Duplicated time-budget formula (`remaining//20 + inc//2`, bounds 200/5000) in controller + UCI. | `src/lichess_controller.py`, `src/uci.py` | **DEFERRED** — extract a shared helper with named constants (recommended follow-up). |
| M7 | `_process_game_stream` nesting reaches 5 levels in one branch. | `src/lichess_controller.py:221-268` | **DEFERRED** — extract a `_handle_game_state` helper. |

### LOW

| # | Finding | File:Line | Status |
|---|---|---|---|
| L1 | NDJSON stream died on `UnicodeDecodeError`. | `src/lichess_client.py:145` | **FIXED** — `decode(errors="replace")`. |
| L2 | `_build_board` narrow except could miss `TypeError`. | `src/lichess_controller.py:315` | **FIXED** — widened to include `TypeError`. |
| L3 | `event_queue` unparameterized type + quoted annotation. | `src/lichess_controller.py:109` | **FIXED** — `queue.Queue[object]`. |
| L4 | `_threads` list grew unbounded across games. | `src/lichess_controller.py` | **FIXED** — prune finished threads in `_start_game_thread`. |
| L5 | Unnecessary `time.sleep` in controller test. | `tests/test_lichess.py` | **FIXED** — removed. |
| L6 | Redundant function-local imports of event classes / `LichessController`. | `src/gui.py` | **FIXED** — moved to module-level imports. |
| L7 | Windows stdout emits `\r\n` (UCI wants `\n`). | `src/uci.py:48` | **FIXED** — `sys.stdout.reconfigure(newline="\n")` in `main()`. |
| L8 | `lichess_game` dict unparameterized. | `src/gui.py:104` | **DEFERRED** — use a `TypedDict` (minor). |
| L9 | Pre-existing walrus bug at `engine.py:297` (`captured_piece := ... is None`). | `src/engine.py:297` | **DEFERRED** — pre-existing, does not affect new code; noted in plan. |

### Verified-Safe (no findings)
Token never in URLs (asserted by test); placeholder `"LICHESS_BOT_TOKEN"` rejected by GUI; `run_engine.bat` has no injection; `_get_lichess_token` has no path traversal; UCI stdout purity (asserted); `uci_to_move` rejects `"0000"`/`""`; `register`/`debug` are safe no-ops; `.gitignore` excludes `lichess/config.yml`, `.lichess_token`, `*.token`.

## Validation Results

| Check | Result |
|---|---|
| Static analysis (imports) | ✅ Pass — all modules import; no-redirect opener installs |
| Unit tests | ✅ Pass — 182 passed (177 original + 5 new regression tests) |
| Coverage | ✅ Pass — lichess_client 95%, lichess_controller 80%, uci 88% (all ≥80%) |
| Build (PyInstaller) | ✅ Pass — `release/GLM_CC_Chess.exe` + `v1.1.0.zip` (prior task) |
| Crash repros | ✅ Fixed — malformed FEN & bad `go` int no longer crash the engine |
| Security | ✅ Pass — Bearer not forwarded on redirect (asserted); no hardcoded token; secrets gitignored |

## Files Reviewed
- **Source (NEW)**: `src/lichess_client.py`, `src/lichess_controller.py`, `src/uci.py`
- **Source (MODIFIED)**: `src/gui.py`, `src/moves.py`, `src/engine.py`
- **Tests (NEW)**: `tests/test_uci.py`, `tests/test_lichess.py`
- **Tests (MODIFIED)**: `tests/test_moves.py`, `tests/test_engine.py`
- **Config/docs**: `.gitignore`, `GLM_CC_Chess.spec`, `lichess/*`, `README.md`, `release/readme.txt`, `CLAUDE.md`, `prompt_lichess_bot.md`

## Recommended Follow-ups (non-blocking)
1. **M4/M5**: Extract `src/gui_lichess.py` (`LichessPanel` + event handlers) to bring `gui.py` under 800 lines and `_handle_lichess_event` under 50 lines (dict dispatch).
2. **M6**: Extract a shared `time_budget(remaining_ms, inc_ms)` helper (with named constants) used by both `lichess_controller._time_budget` and `uci.parse_go`.
3. **M7**: Flatten `_process_game_stream` via an early-`continue` for `opponentGone`/unknown and a `_handle_game_state` helper.
4. **L8**: Replace the `lichess_game` dict with a `TypedDict`/dataclass.
5. **L9**: Fix the pre-existing walrus at `engine.py:297` to match the `else` branch style.
6. **`.gitignore`**: Consider adding `.env`, `__pycache__/`, `*.pyc` (the repo already tracks stale `.pyc` — `git rm --cached -r **/__pycache__` would clean that up).