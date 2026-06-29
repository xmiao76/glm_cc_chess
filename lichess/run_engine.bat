@echo off
REM ---------------------------------------------------------------------------
REM Wrapper so lichess-bot can launch the built-in UCI engine on Windows.
REM
REM lichess-bot spawns the binary named in config.yml `engine.name` and talks
REM UCI to it over stdin/stdout. This wrapper simply runs the Python UCI entry
REM point so the same engine used by the GUI can be driven headlessly.
REM
REM Requirements:
REM   * Python 3.12+ must be on PATH on the machine running lichess-bot.
REM   * Run lichess-bot with this repo as the engine `dir` (or an absolute path).
REM
REM Test it standalone first:
REM   run_engine.bat
REM   uci         -> uciok
REM   isready     -> readyok
REM   position startpos moves e2e4 e7e5
REM   go movetime 1000   -> bestmove <legal>
REM   quit
REM ---------------------------------------------------------------------------
python -m src.uci