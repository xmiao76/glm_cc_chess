# Chess Application — Full Project Prompt

Act as a senior software architect and technical lead.
I want to build a Windows desktop chess application with a GUI and a built-in chess engine. A human user must be able to play against the computer on a visual chessboard.

Your responsibility is to choose the best implementation approach and create a practical development plan that can be executed step by step until the application is fully working, fully tested, and packaged for release.

## Requirements

Select the best programming language, architecture, and Windows GUI framework for this project
Include all required modules for a full chess application
You may study existing chess engine designs, but all source code must be newly written for this application
The first completed phase must deliver a working GUI version where a human can play against the engine
The engine must calculate quickly enough to provide a smooth and responsive user experience
The application must be easy to build, run, and test locally
Testing must be integrated throughout development, not postponed until the end
Automated tests must be created and maintained during development
Defects found during testing or real gameplay must be fixed and retested until stable
The final application must be able to complete full chess games correctly
Engine-vs-engine mode is desirable if practical
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
- the Windows executable is successfully packaged into the release folder
- the packaged release .exe is tested directly from the release folder
- the tested release executable can launch and play a real game successfully
- the release folder contains both the runnable .exe and a clear README.txt for end users