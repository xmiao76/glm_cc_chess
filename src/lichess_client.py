"""Lichess BOT API HTTP client (Python standard library only).

Talks to the official Lichess BOT API using a dedicated BOT account token.
No browser automation, no Selenium, no normal-account automation. Uses only
``urllib`` so the GUI exe stays dependency-free and PyInstaller-friendly.

Authentication: ``Authorization: Bearer <token>`` header; the token must have
the ``bot:play`` scope and belong to a BOT account (upgraded via
``POST /api/bot/account/upgrade``).

Streaming endpoints (``/api/stream/event``, ``/api/bot/game/stream/{id}``) return
NDJSON — one JSON object per line, with empty keep-alive lines roughly every
7 seconds. The token is NEVER placed in a URL query string or written to logs.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://lichess.org"
USER_AGENT = "glm_cc_chess"
# Long read timeout is safe because Lichess sends keep-alive empty lines ~7s,
# which resets the per-read timer. 300s guards against genuine stalls.
STREAM_TIMEOUT = 300
REQUEST_TIMEOUT = 30

ENDPOINTS = {
    "profile": "/api/account",
    "playing": "/api/account/playing",
    "stream_event": "/api/stream/event",
    "stream": "/api/bot/game/stream/{}",
    "move": "/api/bot/game/{}/move/{}",
    "abort": "/api/bot/game/{}/abort",
    "resign": "/api/bot/game/{}/resign",
    "accept": "/api/challenge/{}/accept",
    "decline": "/api/challenge/{}/decline",
    "upgrade": "/api/bot/account/upgrade",
    "pgn": "/api/game/export/{}",
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects so the Bearer token is never forwarded.

    ``urllib``'s default ``HTTPRedirectHandler`` strips only ``Content-Length``
    and ``Content-Type`` on redirect — NOT ``Authorization`` — so a 3xx would
    leak the ``Bearer`` token to whatever host Lichess redirected to. Lichess
    API endpoints do not redirect in practice, so disabling redirects is safe
    and removes this account-compromising leak.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # do not follow; the 3xx response is surfaced as an error


# Install process-wide so every ``urlopen`` call (including streams) refuses
# to follow redirects. Tests that patch ``urllib.request.urlopen`` bypass this
# opener entirely, so this does not affect mocked tests.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)
urllib.request.install_opener(_NO_REDIRECT_OPENER)


class LichessAPIError(Exception):
    """Raised when a Lichess API request fails (non-2xx or network error)."""


class LichessClient:
    """Thin Lichess BOT API client over ``urllib``."""

    def __init__(self, token: str, base_url: str = BASE_URL,
                 user_agent: str = USER_AGENT) -> None:
        if not token:
            raise ValueError("Lichess token must not be empty")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent

    # --- request plumbing ---------------------------------------------------

    def _url(self, endpoint: str, *args: str) -> str:
        path = ENDPOINTS[endpoint].format(*args)
        return f"{self.base_url}{path}"

    def _request(self, method: str, url: str, *, data: Optional[bytes] = None,
                 params: Optional[dict] = None,
                 timeout: float = REQUEST_TIMEOUT) -> http.client.HTTPResponse:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self.user_agent,
        }
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        logger.debug("HTTP %s %s", method, url)  # url never contains the token
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise LichessAPIError(f"{method} {url} -> HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LichessAPIError(f"{method} {url} -> {exc.reason}") from exc
        # Defense-in-depth: even though the installed opener refuses to follow
        # redirects, also reject any 3xx that slips through so the token can
        # never be forwarded to a different host.
        if 300 <= getattr(resp, "status", 200) < 400:
            resp.close()
            raise LichessAPIError(
                f"{method} {url} -> HTTP {resp.status} (redirect not followed)")
        return resp

    def _post(self, endpoint: str, *args: str,
              params: Optional[dict] = None) -> None:
        url = self._url(endpoint, *args)
        resp = self._request("POST", url, data=b"", params=params)
        try:
            resp.read()
        finally:
            resp.close()

    def _get_json(self, endpoint: str, *args: str) -> dict:
        url = self._url(endpoint, *args)
        resp = self._request("GET", url)
        try:
            return json.loads(resp.read().decode())
        finally:
            resp.close()

    def _get_text(self, endpoint: str, *args: str) -> str:
        url = self._url(endpoint, *args)
        resp = self._request("GET", url)
        try:
            return resp.read().decode()
        finally:
            resp.close()

    # --- public API ---------------------------------------------------------

    def get_profile(self) -> dict:
        """Return the bot account profile (validates the token)."""
        return self._get_json("profile")

    def accept_challenge(self, challenge_id: str) -> None:
        self._post("accept", challenge_id)

    def decline_challenge(self, challenge_id: str, reason: str = "generic") -> None:
        self._post("decline", challenge_id, params={"reason": reason})

    def make_move(self, game_id: str, uci: str, draw: bool = False) -> None:
        """Play ``uci`` (e.g. 'e2e4') in the given game via the BOT API."""
        params = {"draw": "1"} if draw else None
        self._post("move", game_id, uci, params=params)

    def abort(self, game_id: str) -> None:
        self._post("abort", game_id)

    def resign(self, game_id: str) -> None:
        self._post("resign", game_id)

    def get_game_pgn(self, game_id: str) -> str:
        """Download a finished game's PGN (for result/rating collection)."""
        return self._get_text("pgn", game_id)

    # --- streaming ----------------------------------------------------------

    def _iter_ndjson(self, url: str) -> Iterator[dict]:
        resp = self._request("GET", url, timeout=STREAM_TIMEOUT)
        try:
            for raw in resp:
                # errors="replace" so a single invalid byte does not kill the
                # stream with UnicodeDecodeError.
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue  # keep-alive empty line
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("unparseable NDJSON line: %r", line)
        finally:
            resp.close()

    def stream_events(self) -> Iterator[dict]:
        """Yield events from ``GET /api/stream/event``.

        Event types: ``challenge``, ``gameStart``, ``gameFinish``.
        """
        yield from self._iter_ndjson(self._url("stream_event"))

    def stream_game(self, game_id: str) -> Iterator[dict]:
        """Yield game events from ``GET /api/bot/game/stream/{game_id}``.

        The first object is a ``gameFull`` event (contains ``state``);
        subsequent objects are ``gameState`` (move updates) or ``opponentGone``.
        """
        yield from self._iter_ndjson(self._url("stream", game_id))