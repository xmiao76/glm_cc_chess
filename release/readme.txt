GLM CC Chess - Readme
======================

A Windows desktop chess game with a built-in AI engine.
Play human vs engine, watch engine vs engine, or let the engine
play against opponents on Lichess.

HOW TO START
------------
Double-click GLM_CC_Chess.exe to launch the game.

No installation required. No Python or other dependencies needed.

GAME MODES
----------
From the main menu, choose one of four modes:

  1. Play as White    - You play White, the engine plays Black
  2. Play as Black    - The engine plays White, you play Black
  3. Engine vs Engine - Watch the engine play both sides
  4. AI vs Lichess    - The engine plays real games on Lichess

AI VS LICHESS
-------------
Lets the built-in engine play against opponents on Lichess using a
dedicated Lichess BOT account. The game is shown live on the board and
finished games can be reviewed move-by-step with the <<  <  >  >> buttons
(or the Left/Right arrow keys).

Requirements:
  - A dedicated Lichess BOT account (NOT your personal account).
  - A Lichess API token with the "bot:play" scope.
  - The token, set in EITHER of these ways:
      * Enter it in the app: open "AI vs Lichess", paste/type the token in
        the masked input box, and click Connect (Ctrl+V pastes; Enter also
        connects). The token is masked, never logged, and never written to
        a file.
      * Or set the LICHESS_BOT_TOKEN environment variable before launching,
        e.g. (Command Prompt):
            set LICHESS_BOT_TOKEN=your_token_here
  - An internet connection. On first run, Windows Firewall may prompt
    for network access - allow it so the bot can reach lichess.org.

If no token is found, the panel shows the token input box. See the developer
README and lichess/README_LICHESS.md (in the source repo) for full setup.

HOW TO PLAY
-----------
Click a piece to select it. Legal moves are highlighted in green.
Click a highlighted square to move there. Click elsewhere to deselect.

Pawns promote to Queens automatically.

KEYBOARD SHORTCUTS
------------------
  N - Return to the main menu (new game)
  U - Undo last move (undoes both your move and the engine's response)

GAME RULES
----------
The game enforces all standard chess rules:
  - Legal move generation for all pieces
  - Check, checkmate, and stalemate detection
  - Castling (kingside and queenside)
  - En passant captures
  - Pawn promotion
  - Draw by 50-move rule
  - Draw by threefold repetition
  - Draw by insufficient material

ENGINE
------
The built-in engine uses minimax search with alpha-beta pruning,
iterative deepening, and quiescence search. It searches to depth 4
by default and typically responds within 1-2 seconds.

SYSTEM REQUIREMENTS
------------------
  - Windows 10 or later
  - No internet connection required (except for AI vs Lichess mode)
  - No additional software required

TROUBLESHOOTING
---------------
If the game does not start:
  - Make sure you are running on Windows
  - Try running from a command prompt to see any error messages:
    GLM_CC_Chess.exe

If pieces appear as squares or missing symbols:
  - The game uses Unicode chess symbols. Ensure the "Segoe UI Symbol"
    font is installed (it ships with Windows 10+).

UNINSTALLING
-----------
Delete GLM_CC_Chess.exe. No other files are created on your system.

VERSION
-------
1.2.0