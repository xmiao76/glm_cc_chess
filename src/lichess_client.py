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
import threading
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
    "challenge": "/api/challenge/{}",
    "cancel": "/api/challenge/{}/cancel",
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
    """Raised when a Lichess API request fails (non-2xx or network error).

    ``status`` is the HTTP status code when known (``None`` for network errors),
    so callers can branch on e.g. a permanent 400/404 vs a transient failure and
    decide whether reconnecting would just spam the same error.
    """

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class LichessClient:
    """Thin Lichess BOT API client over ``urllib``."""

    def __init__(self, token: str, base_url: str = BASE_URL,
                 user_agent: str = USER_AGENT) -> None:
        if not token:
            raise ValueError("Lichess token must not be empty")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        # Open streaming HTTP responses (event stream + each game stream). Tracked
        # so :meth:`close_streams` can tear them down promptly on shutdown — see the
        # note on ``stop()`` in :class:`LichessController`. Without this, a
        # controller restart on Windows could leave the old event stream socket
        # alive for up to ~7s (until the next keep-alive line), overlapping the
        # new stream and looking like a second bot instance to Lichess.
        self._stream_lock = threading.Lock()
        self._open_streams: list[http.client.HTTPResponse] = []

    # --- request plumbing ---------------------------------------------------

    def _url(self, endpoint: str, *args: str) -> str:
        path = ENDPOINTS[endpoint].format(*args)
        return f"{self.base_url}{path}"

    def _request(self, method: str, url: str, *, data: Optional[bytes] = None,
                 form_params: Optional[dict] = None,
                 timeout: float = REQUEST_TIMEOUT) -> http.client.HTTPResponse:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self.user_agent,
        }
        if form_params:
            # Lichess's POST endpoints (challenge, decline) read parameters from
            # the request BODY (application/x-www-form-urlencoded), NOT the query
            # string. Sending them as query params silently drops them — a
            # challenge whose clock is in the query becomes a no-clock
            # "correspondence" game (lichess-org/api issue #142). So form-encode
            # into the body, using the documented dotted keys (``clock.limit``).
            data = urllib.parse.urlencode(form_params).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        logger.debug("HTTP %s %s", method, url)  # url never contains the token
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # Read the body so callers see *why* Lichess rejected the request
            # (e.g. the exact reason a game stream returned 400) instead of a
            # bare status code. The body never contains our token.
            body = ""
            try:
                body = exc.read().decode(errors="replace").strip()
            except Exception:  # noqa: BLE001 - body is best-effort, not load-bearing
                body = ""
            if body:
                body = body[:300]
            msg = f"{method} {url} -> HTTP {exc.code}"
            if body:
                msg = f"{msg}: {body}"
            raise LichessAPIError(msg, status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise LichessAPIError(f"{method} {url} -> {exc.reason}") from exc
        # Defense-in-depth: even though the installed opener refuses to follow
        # redirects, also reject any 3xx that slips through so the token can
        # never be forwarded to a different host.
        if 300 <= getattr(resp, "status", 200) < 400:
            resp.close()
            raise LichessAPIError(
                f"{method} {url} -> HTTP {resp.status} (redirect not followed)",
                status=getattr(resp, "status", None))
        return resp

    def _post(self, endpoint: str, *args: str,
              params: Optional[dict] = None) -> None:
        url = self._url(endpoint, *args)
        resp = self._request("POST", url, form_params=params)
        try:
            resp.read()
        finally:
            resp.close()

    def _post_json(self, endpoint: str, *args: str,
                   params: Optional[dict] = None) -> dict:
        """POST and return the parsed JSON body (``{}`` if empty/unparseable).

        ``params`` are sent as a form-encoded POST body (see :meth:`_request`).
        """
        url = self._url(endpoint, *args)
        resp = self._request("POST", url, form_params=params)
        try:
            body = resp.read().decode(errors="replace")
        finally:
            resp.close()
        if not body.strip():
            return {}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("unparseable response from %s: %r", endpoint, body[:200])
            return {}
        return parsed if isinstance(parsed, dict) else {"data": parsed}

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

    def create_challenge(self, username: str, *, rated: bool = False,
                         clock_limit_s: Optional[int] = None,
                         clock_increment_s: int = 0,
                         color: str = "random",
                         variant: str = "standard") -> dict:
        """Challenge ``username`` to a game; return Lichess's challenge JSON.

        ``clock_limit_s`` is the initial clock in seconds (``None`` => no clock,
        i.e. correspondence). ``clock_increment_s`` is the increment per move.
        ``color`` is ``"white"`` / ``"black"`` / ``"random"``. The token is sent
        only in the ``Authorization`` header — never in the URL or params.
        """
        params: dict = {
            "rated": "true" if rated else "false",
            "color": color,
            "variant": variant,
        }
        if clock_limit_s is not None:
            # Dotted keys (``clock.limit``) are the documented form; Lichess
            # reads them from the POST body. Omitting both clock and ``days``
            # creates a correspondence game, so a clock MUST be present for the
            # real-time game we intend.
            params["clock.limit"] = str(int(clock_limit_s))
            params["clock.increment"] = str(int(clock_increment_s))
        return self._post_json("challenge", username, params=params)

    def cancel_challenge(self, challenge_id: str) -> None:
        """Cancel an outgoing challenge (``POST /api/challenge/{id}/cancel``).

        Best-effort: Lichess returns 4xx if the challenge was already accepted,
        declined, or expired. Callers that treat cancel as advisory should catch
        :class:`LichessAPIError`.
        """
        self._post("cancel", challenge_id)

    def make_move(self, game_id: str, uci: str, draw: bool = False) -> None:
        """Play ``uci`` (e.g. 'e2e4') in the given game via the BOT API."""
        params = {"draw": "1"} if draw else None
        self._post("move", game_id, uci, params=params)

    def abort(self, game_id: str) -> None:
        self._post("abort", game_id)

    def resign(self, game_id: str) -> None:
        self._post("resign", game_id)

    def upgrade_to_bot(self) -> None:
        """Upgrade the linked account to a Bot account (IRREVERSIBLE).

        ``POST /api/bot/account/upgrade``. Required once before the bot-only
        endpoints (game stream, make move) will work — a normal account gets
        HTTP 400 ``"This endpoint can only be used with a Bot account"`` from
        ``/api/bot/game/stream/{id}`` until this is done.
        """
        self._post("upgrade")

    def get_game_pgn(self, game_id: str) -> str:
        """Download a finished game's PGN (for result/rating collection)."""
        return self._get_text("pgn", game_id)

    # --- streaming ----------------------------------------------------------

    def _register_stream(self, resp: http.client.HTTPResponse) -> None:
        """Track an open streaming response so :meth:`close_streams` can close it."""
        with self._stream_lock:
            self._open_streams.append(resp)

    def _unregister_stream(self, resp: http.client.HTTPResponse) -> None:
        with self._stream_lock:
            try:
                self._open_streams.remove(resp)
            except ValueError:
                pass  # already removed (e.g. close_streams cleared the list)

    def close_streams(self) -> None:
        """Close every open streaming HTTP response (event stream + game streams).

        Called by :meth:`LichessController.stop` so a controller restart tears
        down the old streams promptly. Without this the old event-stream socket
        can linger ~7s on Windows, overlapping the new stream and looking like a
        second bot instance to Lichess — exactly the duplicate-event-stream
        conflict that aborts games at creation. Safe when no streams are open
        (no-op) and idempotent (``HTTPResponse.close`` is safe to call twice).
        """
        with self._stream_lock:
            streams = list(self._open_streams)
            self._open_streams.clear()
        for resp in streams:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                logger.debug("error closing a stream response", exc_info=True)

    def _iter_ndjson(self, url: str,
                     stop: Optional[threading.Event] = None) -> Iterator[dict]:
        resp = self._request("GET", url, timeout=STREAM_TIMEOUT)
        self._register_stream(resp)
        if stop is not None and stop.is_set():
            # We were stopped while the (re)connect handshake was in flight: this
            # response was registered AFTER ``close_streams()`` already ran and
            # cleared the list, so ``close_streams`` missed it. Close it here and
            # yield nothing, so a controller restart never leaves a lingering
            # second event/game stream for this account (the exact condition that
            # Lichess reads as a duplicate bot instance and aborts games for).
            self._unregister_stream(resp)
            resp.close()
            return
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
            self._unregister_stream(resp)
            resp.close()

    def stream_events(self, stop: Optional[threading.Event] = None) -> Iterator[dict]:
        """Yield events from ``GET /api/stream/event``.

        Event types: ``challenge``, ``gameStart``, ``gameFinish``. ``stop`` (the
        controller's stop event) is re-checked right after the (re)connect
        handshake so a shutdown during a reconnect closes the fresh response
        instead of leaving it lingering as a second stream.
        """
        yield from self._iter_ndjson(self._url("stream_event"), stop=stop)

    def stream_game(self, game_id: str,
                    stop: Optional[threading.Event] = None) -> Iterator[dict]:
        """Yield game events from ``GET /api/bot/game/stream/{game_id}``.

        The first object is a ``gameFull`` event (contains ``state``);
        subsequent objects are ``gameState`` (move updates) or ``opponentGone``.
        ``stop`` is re-checked after the (re)connect handshake (see
        :meth:`stream_events`).
        """
        yield from self._iter_ndjson(self._url("stream", game_id), stop=stop)