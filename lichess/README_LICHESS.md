# Lichess BOT Integration

This folder documents how to make the built-in GLM CC Chess engine play games
on Lichess using a **dedicated Lichess BOT account**. There are two ways to do
this, both using the same engine and the official Lichess BOT API (no browser
automation, no Selenium, no normal-account automation):

1. **In-GUI mode (primary)** — click **"AI vs Lichess"** in the desktop app.
   The game is streamed live onto the GUI board and finished games can be
   reviewed move-by-move. This is the recommended way to watch the engine play.
2. **Headless bridge (optional)** — run [`lichess-bot`](https://github.com/lichess-bot-devs/lichess-bot)
   with this repo's UCI engine (`python -m src.uci`) for unattended 24/7 play.

> ⚠️ **Use a dedicated BOT account only.** Never automate play on a normal
> Lichess account — that violates the Lichess Terms of Service. Bots must be
> upgraded BOT accounts and can only play challenge games (no pools/tournaments).

---

## 1. Create a dedicated Lichess BOT account

1. Sign up for a **new** Lichess account that will act only as a bot
   (e.g. `my_chess_bot`). Do **not** use your personal account.
2. Upgrade the account to a BOT. Open the Lichess API token page or run:
   ```
   curl -X POST https://lichess.org/api/bot/account/upgrade \
        -H "Authorization: Bearer <YOUR_TOKEN>"
   ```
   (You can also upgrade from the Lichess account settings.) Once upgraded,
   the account can no longer play as a human — it can only play via the BOT API.

## 2. Generate an API token

1. Go to <https://lichess.org/account/oauth/token> while logged into the bot
   account.
2. Create a token with the **`bot:play`** scope (this is the only scope needed).
3. **Keep the token secret.** It is a password for your bot account.

Set the token as an environment variable (preferred):

**Windows (PowerShell):**
```powershell
setx LICHESS_BOT_TOKEN "your_real_token_here"
```
**Windows (Command Prompt, current session only):**
```cmd
set LICHESS_BOT_TOKEN=your_real_token_here
```
**Git Bash:**
```bash
export LICHESS_BOT_TOKEN="your_real_token_here"
```

Alternatively, copy `config.yml.example` to `config.yml` **in this folder** and
put the token on the `token:` line. `config.yml` is gitignored and will never be
committed. The in-GUI mode and `run_engine.bat`-based bridge both read the env
var first, then `lichess/config.yml`.

> **Never commit a real token.** The `.gitignore` excludes `lichess/config.yml`,
> `.lichess_token`, and `*.token`. The placeholder value `LICHESS_BOT_TOKEN`
> (used in `config.yml.example`) is rejected by the app, so an unedited copy is
> never accidentally used as a token.

## 3. Play from the GUI (primary)

```bash
python -m src.main
```
(or double-click the packaged `GLM_CC_Chess.exe`)

1. Click **"AI vs Lichess"** on the main menu.
2. If no token is found, the panel shows instructions — set
   `LICHESS_BOT_TOKEN` and restart.
3. Once connected, the app streams incoming challenges. When a challenge
   appears, click **Accept** or **Decline** in the side panel.
4. The game is shown live on the board (from the bot's perspective) with
   opponent name and clocks. The engine moves automatically.
5. Use **Resign** / **Abort** during a game, and **Menu** to leave Lichess mode.
6. When the game ends, **review mode** activates — use `<<` `<` `>` `>>` (or
   the Left/Right arrow keys) to step through the whole game.

On first run, Windows Firewall may prompt for network access — allow it so the
bot can reach `lichess.org`.

## 4. Test the UCI engine locally first

Before connecting to Lichess, verify the engine speaks UCI correctly:

```bash
python -m src.uci
```
Then type:
```
uci                              -> id name GLMCCChessEngine / uciok
isready                          -> readyok
position startpos moves e2e4 e7e5
go movetime 1000                 -> bestmove <a legal move>
quit
```

stdout contains only UCI protocol output; diagnostics go to stderr. If
`go movetime 1000` returns a `bestmove`, the engine is ready for lichess-bot.

## 5. Run the optional headless bridge with lichess-bot

This lets the bot accept challenges and play unattended, without the GUI.

1. Clone lichess-bot:
   ```bash
   git clone https://github.com/lichess-bot-devs/lichess-bot.git
   cd lichess-bot
   pip install -r requirements.txt
   ```
2. Copy this repo's `lichess/config.yml.example` to `lichess-bot/config.yml`
   and set the token (or export `LICHESS_BOT_TOKEN`). The default config points
   `engine.dir` at `.` and `engine.name` at `run_engine.bat`, so run lichess-bot
   from this repo root (or edit `dir` to an absolute path to this repo).
3. Make sure `run_engine.bat` and `python` are reachable from the lichess-bot
   working directory. `run_engine.bat` simply runs `python -m src.uci`.
4. Start the bot:
   ```bash
   python lichess-bot.py
   ```
   It will connect to Lichess, listen for challenges, and launch the UCI engine
   for each game.

> The batch wrapper requires Python on the lichess-bot host. A self-contained
> engine `.exe` (via a second PyInstaller spec with `console=True`) is a
> possible future enhancement; it is not required for this integration.

## 6. Collect PGN and game results

To download a finished game's PGN (for manual rating/result tracking):

```bash
curl https://lichess.org/api/game/export/<gameId> \
     -H "Authorization: Bearer <YOUR_TOKEN>"
```

The in-code client also exposes `LichessClient.get_game_pgn(game_id)`. Lichess
game IDs are shown in the GUI status and in the lichess-bot logs. There is no
automated rating evaluation in this project — collect results manually.

## 7. Lichess Terms of Service

When running a bot you must follow the Lichess TOS:
- No sandbagging (intentionally losing to lower-rated players).
- No boosting (helping another account gain rating).
- No constant aborting of games.
- One bot account per person; do not automate normal accounts.

This integration is a single bot that plays honestly with the built-in engine.

## Files in this folder

| File | Purpose |
|---|---|
| `config.yml.example` | Template for the optional lichess-bot bridge (commit this). |
| `config.yml` | Real config with your token — **gitignored, never commit**. |
| `run_engine.bat` | Windows wrapper that runs `python -m src.uci` for lichess-bot. |
| `README_LICHESS.md` | This file. |

## Related code

| Module | Role |
|---|---|
| `src/lichess_client.py` | Lichess BOT API HTTP client (stdlib `urllib`). |
| `src/lichess_controller.py` | Threading + queue bridge between API streams and the GUI. |
| `src/uci.py` | UCI protocol entry point used by the headless bridge. |
| `src/engine.py` (`choose_move`) | Clean move-selection interface shared by GUI, UCI, and Lichess. |
| `src/moves.py` (`move_to_uci`/`uci_to_move`) | UCI ↔ internal move conversion. |