# Chess Application — Full Project Prompt

Act as a senior software architect and technical lead.
I want to build a Windows desktop chess application with a GUI and a built-in chess engine. A human user must be able to play against the computer on a visual chessboard, watch engine-versus-engine games, and let the engine play real games on Lichess.org.

Your responsibility is to choose the best implementation approach and create a practical development plan that can be executed step by step until the application is fully working, fully tested, and packaged for release.

## Requirements

Select the best programming language, architecture, and Windows GUI framework for this project
Include all required modules for a full chess application
You may study existing chess engine designs, but all source code must be newly written for this application
The first completed phase must deliver a working GUI version where a human can play against the engine as White or as Black
Subsequent phases add engine-versus-engine play, the standalone UCI interface, and the AI vs Lichess mode
The engine must calculate quickly enough to provide a smooth and responsive user experience
The application must be easy to build, run, and test locally
Testing must be integrated throughout development, not postponed until the end
Automated tests must be created and maintained during development
Defects found during testing or real gameplay must be fixed and retested until stable
The final application must be able to complete full chess games correctly
The application must support an engine-versus-engine mode where the user can watch two engines play a full game against each other
The application must support an "AI vs Lichess" mode in which the built-in engine plays real, timed games on Lichess.org against human or bot opponents
In AI vs Lichess mode, the active game must be shown live on the board from the engine's perspective, with the opponent name and clocks, and the engine must move automatically on its turn
The GUI side panel must be resizable: the user must be able to widen the window to enlarge the panel (the chessboard stays a fixed size and the extra width flows into the panel), and panel text — status, activity log, opponent, and move list — must wrap to the panel width instead of being truncated or clipped, so the whole message is visible at any panel width
In AI vs Lichess mode, once connected, the UI must display both the connected account's name and its Lichess rating(s) (the Lichess score)
The displayed Lichess rating must refresh in real time after each completed game by re-reading the account profile, without requiring a restart or a manual refresh
After a Lichess game finishes, the user must be able to step through and review the entire game move by move
The user must be able to manually challenge a specific opponent by username and to accept or decline incoming challenges shown in the UI
An auto-match option must let the bot automatically play a chosen opponent, auto-accepting their challenges and re-issuing a challenge while idle, while keeping at most one game in progress at a time
Lichess games must use real clocks with an initial time and a per-move increment, and the user must be able to choose casual or rated games
The built-in engine must also be usable on its own through a standard UCI (Universal Chess Interface) command-line interface, so external UCI-compatible hosts can drive the engine for games without the GUI
Lichess integration must use a dedicated Lichess BOT account only; the application must never automate play on a normal Lichess account and must never rely on browser automation
The application must detect when the connected Lichess account is not yet a BOT, offer to upgrade it (a permanent change), and must not attempt bot-only actions until the account is a BOT
The Lichess API token is a secret: the user must be able to enter it in the GUI (masked on screen) or supply it via an environment variable; it must never be hardcoded in the source, never committed to the repository, never written to a file, and never shown in logs
The final build must produce a packaged Windows .exe
The packaged .exe must be output to a release folder
The release folder must also contain a README.txt
The README.txt must explain how to launch the application, how to play, the basic controls, and any important notes for the user
The packaged release .exe must itself be tested after packaging, not only the development build
Release validation must confirm that the .exe in the release folder can start successfully and can be used to play a real chess game
Any defects found in the packaged release version must be fixed, rebuilt, and retested until the release executable passes all required checks

## Chess Rules

The chess program must fully support standard chess rules, including:

- legal move validation
- check, checkmate, and stalemate
- castling
- en passant
- pawn promotion
- draw rules such as threefold repetition, fifty-move rule, and insufficient material

## Deliverables

Please provide:

- recommended tech stack and justification
- architecture design
- module breakdown and responsibilities
- phased implementation roadmap
- test plan for every phase
- automated testing approach
- performance optimization approach
- local build, run, and test workflow
- defect-fix and regression-test workflow
- packaging plan for the Windows executable
- release validation plan for the packaged .exe
- expected contents of the release folder
- suggested contents of the README.txt
- final completion criteria

## Completion Criteria

Define completion so that the task is only finished when:

- the application is playable and stable
- it can complete full chess games without crashes or rule errors
- all required automated tests pass
- every game mode works: human-versus-engine, engine-versus-engine, and AI vs Lichess
- the engine is drivable on its own through its UCI interface
- Lichess games connect with a dedicated BOT account, display live on the board, and can be reviewed move by move when finished
- the GUI side panel is resizable and its text wraps to the panel width, so the full message is visible at any panel size rather than truncated or clipped
- in AI vs Lichess mode the connected account name and Lichess rating are shown together, and the rating refreshes in real time after each completed game
- the Lichess token is handled securely — entered masked in the GUI or supplied via an environment variable, and never hardcoded, committed, logged, or written to a file
- the Windows executable is successfully packaged into the release folder
- the packaged release .exe is tested directly from the release folder
- the tested release executable can launch and play a real game successfully
- the release folder contains both the runnable .exe and a clear README.txt for end users
