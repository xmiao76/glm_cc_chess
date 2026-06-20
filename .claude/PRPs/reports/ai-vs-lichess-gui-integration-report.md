# Implementation Report: AI vs Lichess — In-GUI BOT Integration with Game Review

## Summary
Extended the existing `glm_cc_chess` Windows Pygame app so the built-in AI can play games on Lichess via a dedicated Lichess BOT account, **integrated directly into the GUI exe** as a new "AI vs Lichess" menu mode. The live Lichess game is streamed onto the Pygame board (with opponent name, clocks, and status), challenges can be accepted/declined, games resigned/aborted, and finished games reviewed move-by-move. The engine is also exposed through a UCI stdin/stdout entry point (`python -m src.uci`) so `lichess-bot` can run it as an optional headless bridge. No browser automation; no hardcoded tokens; existing Human-vs-AI and Engine-vs-Engine modes preserved; Windows release build still works.

## Assessment vs Reality

| Metric | Predicted (Plan) | Actual |
|---|---|---|
| Complexity | Large | Large |
| Confidence | 8/10 | 9/10 |
| Files Changed | ~9 new + ~7 updated | 8 new + 8 updated (see below) |

## Tasks Completed

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | Update `prompt_lichess_bot.md` to match agreed design | ✅ Complete | Primary path = in-GUI BOT client + review; lichess-bot optional; all safety constraints kept |
| 2 | Add UCI move conversion helpers to `src/moves.py` | ✅ Complete | `move_to_uci`/`uci_to_move` |
| 3 | Add `choose_move` interface to `src/engine.py` | ✅ Complete | `choose_move(board, time_limit_ms=None, max_depth=None)` |
| 4 | Create `src/uci.py` UCI entry point | ✅ Complete | Full UCI loop; pure stdout; stderr logging |
| 5 | Create `src/lichess_client.py` BOT API client | ✅ Complete | stdlib `urllib`; NDJSON streaming; Bearer auth |
| 6 | Create `src/lichess_controller.py` threading orchestration | ✅ Complete | daemon threads + `queue.Queue`; turn parity; time budget |
| 7 | Update `src/gui.py`: AI vs Lichess mode + review | ✅ Complete | 4th menu button, live board, challenge UI, review mode, token check |
| 8 | Create `lichess/` config + wrapper + README | ✅ Complete | `config.yml.example`, `run_engine.bat`, `README_LICHESS.md` |
| 9 | Update `.gitignore` for token/config exclusion | ✅ Complete | `lichess/config.yml`, `.lichess_token`, `*.token`, `lichess_bot.log` |
| 10 | Update `GLM_CC_Chess.spec` hiddenimports | ✅ Complete | Added `src.uci`, `src.lichess_client`, `src.lichess_controller` |
| 11 | Add/update tests | ✅ Complete | `test_uci.py`, `test_lichess.py` new; `test_moves.py`, `test_engine.py` extended |
| 12 | Create root `README.md`, update `release/readme.txt` + `CLAUDE.md` | ✅ Complete | Version bumped to 1.1.0 (parses correctly) |
| 13 | Validation | ✅ Complete | 177 tests pass; coverage ≥80% on new modules; exe builds + launches |

## Validation Results

| Level | Status | Notes |
|---|---|---|
| Static Analysis (imports) | ✅ Pass | All 9 src modules import cleanly |
| Unit Tests | ✅ Pass | 177 passed (97 original + 80 new) |
| Coverage | ✅ Pass | uci.py 89%, lichess_client.py 94%, lichess_controller.py 81%, moves.py 97%, engine.py 99% (all ≥80%) |
| Build (PyInstaller) | ✅ Pass | `python package.py` → `release/GLM_CC_Chess.exe` + `release/GLM_CC_Chess_v1.1.0.zip` |
| Integration (exe launch) | ✅ Pass | Packaged exe starts and survives past startup (headless dummy driver) |
| Edge Cases | ✅ Pass | Forced-promotion, checkmate/stalemate → `bestmove 0000`, no-token path, token placeholder rejection, game-over handling, review clamping |
| Security | ✅ Pass | `git check-ignore` confirms secrets ignored; no hardcoded tokens; no secret files staged |

## Files Changed

| File | Action | Notes |
|---|---|---|
| `prompt_lichess_bot.md` | UPDATED | In-GUI integration as primary path + review requirement; lichess-bot optional |
| `src/moves.py` | UPDATED | +`move_to_uci`, +`uci_to_move` |
| `src/engine.py` | UPDATED | +`choose_move` |
| `src/gui.py` | UPDATED | +Lichess mode, live board, challenge UI, review, token loading |
| `src/uci.py` | CREATED | UCI protocol entry point |
| `src/lichess_client.py` | CREATED | Lichess BOT API HTTP client (stdlib) |
| `src/lichess_controller.py` | CREATED | Threading + queue orchestration |
| `lichess/config.yml.example` | CREATED | lichess-bot config template |
| `lichess/run_engine.bat` | CREATED | UCI engine wrapper for lichess-bot |
| `lichess/README_LICHESS.md` | CREATED | Lichess integration guide |
| `.gitignore` | UPDATED | Lichess secret exclusions |
| `GLM_CC_Chess.spec` | UPDATED | hiddenimports for new modules |
| `tests/test_uci.py` | CREATED | 16 UCI tests (subprocess + in-process) |
| `tests/test_lichess.py` | CREATED | 33 client/controller/token tests |
| `tests/test_moves.py` | UPDATED | +`TestUciConversion` (9 tests) |
| `tests/test_engine.py` | UPDATED | +`TestChooseMove` (9 tests) |
| `README.md` | CREATED | Developer-facing root README |
| `release/readme.txt` | UPDATED | +AI vs Lichess mode, version 1.1.0 |
| `CLAUDE.md` | UPDATED | New modules, Lichess integration section |

## Deviations from Plan

1. **Promotion test position** — The plan suggested testing promotion in a "pawn about to promote" position (`8/4P3/8/8/8/8/8/4K2k w`). Investigation showed the engine's choice there is **non-deterministic across search depths**: at deeper depths the quiescence search sees the pawn can be promoted either immediately or after a king move, and a ~10cp king piece-square-table difference tips the tie toward a king move. This is a pre-existing engine eval nuance, **not a bug introduced here**, and fixing it would be engine rework (out of scope per the prompt's "minimal refactor" constraint). The tests were changed to use a **forced-promotion position** (`8/P7/8/8/8/1b6/2k5/K7 w`) where every legal move is a promotion, making the result deterministic at any depth/time budget. This better satisfies the prompt's "promotion moves are handled correctly" requirement without depending on engine search depth.
2. **In-process UCI tests added** — The plan specified subprocess-driven UCI tests only. Subprocess tests don't attribute coverage to `src/uci.py` (separate process), so an in-process `TestUCIEngineInProcess` class was added (using `io.StringIO` streams) to both raise `uci.py` coverage to 89% and speed up the suite. The subprocess tests remain for end-to-end realism.
3. **`stop` UCI command is a documented no-op** — The engine search is synchronous and not externally interruptible, so `stop` is accepted (protocol-compliant) but cannot abort a running search. This is noted in `src/uci.py`. A truly interruptible search would require engine rework (out of scope).
4. **`release/GLM_CC_Chess_v1.1.0.zip` and `.coverage` are untracked build artifacts** — Not gitignored by the original repo's rules; left as-is (the user can commit or ignore as preferred). No secrets.

## Issues Encountered

- **Quoting issue** when running an inline Python validation script via Bash (single-quote collision). Resolved by writing validation scripts to temp files and running them, then deleting the temp files.
- **Controller test initially failed** because `_username` is set by `start()`→`get_profile()`, which the direct-call test bypassed. This was a test-setup issue (the real flow always sets `_username` before game events arrive); fixed by setting `ctrl._username` in the test helper.
- **GUI test initially failed** by asserting on `g.lichess_controller.stopped` after `_menu_from_lichess` had already nulled the reference (correct behavior). Fixed by capturing the controller reference before the call.
- **Forced-promotion position iteration**: the first attempt (`1r6/P7/8/8/8/1k6/8/K7 w`) left a king escape because the black king blocked the rook's attack down the b-file. Resolved by using a bishop + king to cover the escape squares without checking the white king (`8/P7/8/8/8/1b6/2k5/K7 w`).

## Tests Written

| Test File | Tests | Coverage |
|---|---|---|
| `tests/test_uci.py` | 16 (subprocess + in-process) | UCI handshake, position, search, promotion, terminal positions, stdout purity, `parse_go` |
| `tests/test_lichess.py` | 33 | `LichessClient` endpoints/auth/NDJSON/errors; controller turn parity, move flow, time budget, status, event stream, actions, token loading |
| `tests/test_moves.py` | +9 (`TestUciConversion`) | UCI round-trip, castling, promotion, en passant, null-move rejection, applied-move equivalence |
| `tests/test_engine.py` | +9 (`TestChooseMove`) | legality, time/depth limits, promotion, terminal→None, no-mutation |
| **Total new** | **80** | |

## Next Steps
- [ ] Code review via `/code-review`
- [ ] Create PR via `/prp-pr`
- [ ] (Optional) Live Lichess play test with a real BOT token on a Windows desktop
- [ ] (Optional future) Self-contained engine `.exe` build target (second PyInstaller spec, `console=True`) for the headless bridge