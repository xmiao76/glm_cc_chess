"""Tests for src/lichess_client.py and src/lichess_controller.py.

The Lichess HTTP client is tested with ``unittest.mock`` patches on
``urllib.request.urlopen`` (no real network). The controller is tested with a
fake client feeding scripted NDJSON events. Token loading is tested through the
GUI helper using pygame's dummy video driver (headless).
"""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
from unittest.mock import patch

import pytest

from src.lichess_client import LichessClient, LichessAPIError
from src.lichess_controller import (
    LichessController,
    ChallengeReceived,
    ChallengeSent,
    ChallengeDeclined,
    GameStarted,
    GameUpdated,
    EngineMoved,
    GameFinished,
    Status,
    Error,
    FIRST_MOVE_BUDGET_MS,
    _OPENING_BOOK_WHITE_MOVE1,
)
from src.board import STARTING_FEN, Board
from src.moves import generate_legal_moves, move_to_uci, uci_to_move


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class FakeResp:
    """Minimal stand-in for an ``http.client.HTTPResponse`` over NDJSON."""

    def __init__(self, lines):
        self._lines = [l.encode() for l in lines]

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b""

    def close(self):
        pass


class FakeJsonResp(FakeResp):
    def __init__(self, obj):
        super().__init__([json.dumps(obj)])

    def read(self):
        return self._lines[0]


class FakeTextResp(FakeResp):
    def __init__(self, text):
        super().__init__([text])

    def read(self):
        return self._lines[0]


class FakeClient:
    """In-memory Lichess client for controller tests."""

    def __init__(self, username="MyBot", title=None):
        self.username = username
        self.title = title  # "BOT" for a bot account; None/other for a normal one
        self.posted_moves = []
        self.profile_called = False
        self.profile_calls = 0
        self.last_accept = None
        self.last_decline = None
        self.last_decline_reason = None
        self.last_resign = None
        self.last_abort = None
        self.created_challenges = []  # list of dicts (opponent + kwargs)
        self.cancelled = []           # list of challenge ids cancelled
        self.upgrade_called = False

    def get_profile(self):
        self.profile_called = True
        self.profile_calls += 1
        profile = {"username": self.username, "id": self.username.lower()}
        if self.title:
            profile["title"] = self.title
        return profile

    def upgrade_to_bot(self):
        self.upgrade_called = True
        # Simulate Lichess converting the account to a Bot on success.
        self.title = "BOT"

    def make_move(self, game_id, uci, draw=False):
        self.posted_moves.append((game_id, uci))

    def accept_challenge(self, cid):
        self.last_accept = cid

    def decline_challenge(self, cid, reason="generic"):
        self.last_decline = cid
        self.last_decline_reason = reason

    def resign(self, gid):
        self.last_resign = gid

    def abort(self, gid):
        self.last_abort = gid

    def create_challenge(self, opponent, rated=False, clock_limit_s=None,
                         clock_increment_s=0, color="random", variant="standard"):
        record = {
            "opponent": opponent, "rated": rated,
            "clock_limit_s": clock_limit_s, "clock_increment_s": clock_increment_s,
            "color": color, "variant": variant,
        }
        self.created_challenges.append(record)
        resp = {"id": f"c{len(self.created_challenges)}", "direction": "out",
                "color": color, "rated": rated}
        # Mirror Lichess's challenge JSON so the controller can surface the speed
        # it actually assigned (rapid when a clock is present, correspondence
        # when none — the symptom of clock params not reaching the body).
        if clock_limit_s is not None:
            resp["speed"] = "rapid"
            resp["clock"] = {"limit": clock_limit_s, "increment": clock_increment_s}
        else:
            resp["speed"] = "correspondence"
        return resp

    def cancel_challenge(self, challenge_id):
        self.cancelled.append(challenge_id)

    def stream_events(self, stop=None):
        return iter([])

    def stream_game(self, gid, stop=None):
        return iter([])


def fake_choose(board, time_limit_ms=None):
    """Deterministic engine: play the first legal move (or None)."""
    moves = generate_legal_moves(board, board.active_color)
    return moves[0] if moves else None


def make_controller(client, **kw):
    # Inject a no-op singleton bind so tests that exercise start() never open a
    # real localhost socket (deterministic, no port collisions across tests).
    kw.setdefault("singleton_bind", lambda name: True)
    ctrl = LichessController(token="x", client=client, engine_choose=fake_choose, **kw)
    ctrl._username = "MyBot"  # normally set by start() -> get_profile()
    return ctrl


def drain(ctrl):
    out = []
    while True:
        try:
            out.append(ctrl.event_queue.get_nowait())
        except queue.Empty:
            break
    return out


# ---------------------------------------------------------------------------
# LichessClient
# ---------------------------------------------------------------------------


class TestLichessClient:
    def test_get_profile_uses_bearer_header_and_account_endpoint(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = req.headers
            captured["method"] = req.get_method()
            return FakeJsonResp({"username": "MyBot", "id": "mybot"})

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            profile = c.get_profile()
        assert profile["username"] == "MyBot"
        assert captured["headers"]["Authorization"] == "Bearer SECRET"
        assert captured["method"] == "GET"
        assert "/api/account" in captured["url"]
        assert "token" not in captured["url"]  # token never in URL

    def test_make_move_posts_to_move_endpoint(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.make_move("game123", "e2e4")
        assert captured["method"] == "POST"
        assert "/api/bot/game/game123/move/e2e4" in captured["url"]

    def test_make_move_with_draw_in_body(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.make_move("g1", "a7a8q", draw=True)
        # The move is in the path; the draw flag is a form-encoded POST body
        # param (Lichess reads POST params from the body, not the query string).
        assert "/move/a7a8q" in captured["url"]
        assert "draw=1" in (captured["data"] or b"").decode()

    def test_accept_and_decline_challenge(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.accept_challenge("ch1")
        assert "/api/challenge/ch1/accept" in captured["url"]
        with patch("urllib.request.urlopen", side_effect=fake):
            c.decline_challenge("ch1", "nothanks")
        assert "/api/challenge/ch1/decline" in captured["url"]
        # The decline reason is a form-encoded POST body param (a query-string
        # reason is dropped by Lichess -> a bare generic decline).
        assert "reason=nothanks" in (captured["data"] or b"").decode()

    def test_create_challenge_posts_to_challenge_endpoint(self):
        from urllib.parse import urlsplit
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["data"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return FakeJsonResp({"id": "abc", "direction": "out", "color": "white"})

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            result = c.create_challenge("BotY", clock_limit_s=300,
                                        clock_increment_s=3, color="white")
        assert captured["method"] == "POST"
        assert "/api/challenge/BotY" in captured["url"]
        # Lichess reads challenge params from the POST BODY
        # (application/x-www-form-urlencoded), NOT the query string — sending
        # them as query params silently drops the clock and makes a no-clock
        # correspondence game (lichess-org/api issue #142).
        assert urlsplit(captured["url"]).query == ""
        body = captured["data"].decode()
        assert "clock.limit=300" in body
        assert "clock.increment=3" in body
        assert "rated=false" in body
        assert "color=white" in body
        assert captured["content_type"] == "application/x-www-form-urlencoded"
        assert result["id"] == "abc"
        assert "token" not in captured["url"]  # token never in URL/params/body

    def test_create_challenge_uses_dotted_clock_keys_in_body(self):
        # The documented form is dotted keys (``clock.limit``) in the body — not
        # bracketed (``clock[limit]``) and not in the query string.
        from urllib.parse import urlsplit
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return FakeJsonResp({"id": "abc", "direction": "out"})

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.create_challenge("BotY", clock_limit_s=300, clock_increment_s=3)
        body = captured["data"].decode()
        assert "clock.limit=300" in body
        assert "clock.increment=3" in body
        assert "clock%5B" not in body  # no encoded brackets
        assert "clock[" not in body    # no literal brackets
        assert urlsplit(captured["url"]).query == ""  # nothing in query string

    def test_create_challenge_sends_rated_true_in_body(self):
        # Counterpart to the rated=false body test: when rated=True the body must
        # carry rated=true (Lichess reads it from the body, not the query string).
        # This is the param the opponent asked us to verify.
        from urllib.parse import urlsplit
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            return FakeJsonResp({"id": "abc", "direction": "out"})

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.create_challenge("BotY", rated=True, clock_limit_s=300)
        assert urlsplit(captured["url"]).query == ""
        body = captured["data"].decode()
        assert "rated=true" in body
        assert "rated=false" not in body

    def test_decline_challenge_sends_reason_in_body(self):
        # The decline reason must reach Lichess: it's read from the POST body,
        # not the query string (a bare query-string reason is dropped -> generic).
        from urllib.parse import urlsplit
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["content_type"] = req.get_header("Content-type")
            return FakeJsonResp({})

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.decline_challenge("chX", reason="tooSlow")
        assert urlsplit(captured["url"]).query == ""   # reason not in query
        body = captured["data"].decode()
        assert "reason=tooSlow" in body
        assert captured["content_type"] == "application/x-www-form-urlencoded"

    def test_http_error_body_and_status_captured(self):
        # A 4xx must surface Lichess's reason (so the user can see WHY a game
        # stream returned 400) and carry the status code for branching.
        import urllib.error
        body = b'{"error":"Not your turn"}'

        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request",
                                         {}, io.BytesIO(body))

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            with pytest.raises(LichessAPIError) as ei:
                c.get_profile()
        assert ei.value.status == 400
        assert "HTTP 400" in str(ei.value)
        assert "Not your turn" in str(ei.value)

    def test_cancel_challenge_posts_to_cancel_endpoint(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.cancel_challenge("abc")
        assert captured["method"] == "POST"
        assert "/api/challenge/abc/cancel" in captured["url"]

    def test_upgrade_to_bot_posts_to_upgrade_endpoint(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.upgrade_to_bot()
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/api/bot/account/upgrade")
        assert "token" not in captured["url"]

    def test_abort_and_resign(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.abort("g1")
        assert "/api/bot/game/g1/abort" in captured["url"]
        with patch("urllib.request.urlopen", side_effect=fake):
            c.resign("g1")
        assert "/api/bot/game/g1/resign" in captured["url"]

    def test_stream_game_parses_ndjson_and_skips_keepalive(self):
        ndjson = [
            json.dumps({"id": "g1", "white": {"name": "MyBot"}, "black": {"name": "opp"},
                        "initialFen": "startpos",
                        "state": {"moves": "", "wtime": 10000, "btime": 10000,
                                  "winc": 0, "binc": 0, "status": "started"}}),
            "",  # keep-alive
            json.dumps({"type": "gameState", "moves": "e2e4", "wtime": 9000,
                        "btime": 10000, "winc": 0, "binc": 0, "status": "started"}),
            json.dumps({"type": "gameState", "moves": "e2e4 e7e5", "wtime": 9000,
                        "btime": 9000, "winc": 0, "binc": 0, "status": "started"}),
        ]
        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen",
                   side_effect=lambda req, timeout=None: FakeResp(ndjson)):
            events = list(c.stream_game("g1"))
        assert len(events) == 3  # keep-alive skipped
        assert events[0]["white"]["name"] == "MyBot"
        assert events[2]["moves"] == "e2e4 e7e5"

    def test_http_error_raises_lichess_api_error(self):
        import urllib.error

        def fake(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            with pytest.raises(LichessAPIError):
                c.get_profile()

    def test_empty_token_rejected(self):
        with pytest.raises(ValueError):
            LichessClient(token="")

    def test_get_game_pgn(self):
        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen",
                   side_effect=lambda req, timeout=None: FakeTextResp("[PGN]")):
            pgn = c.get_game_pgn("g1")
        assert pgn == "[PGN]"

    def test_redirect_handler_refuses_to_follow(self):
        # The Bearer token must never be forwarded to a different host on a
        # 3xx. The no-redirect handler returns None (do not follow).
        from src.lichess_client import _NoRedirectHandler
        import urllib.request
        handler = _NoRedirectHandler()
        req = urllib.request.Request("https://lichess.org/api/account",
                                     headers={"Authorization": "Bearer SECRET"})
        result = handler.redirect_request(
            req, fp=None, code=302, msg="Found",
            headers={"location": "https://evil.example.com/steal"},
            newurl="https://evil.example.com/steal")
        assert result is None  # redirect NOT followed

    def test_3xx_response_raises_instead_of_leaking_token(self):
        class RedirectResp(FakeResp):
            status = 302

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen",
                   side_effect=lambda req, timeout=None: RedirectResp([])):
            with pytest.raises(LichessAPIError) as exc:
                c.get_profile()
        assert "redirect not followed" in str(exc.value)


# ---------------------------------------------------------------------------
# LichessController — turn logic and move flow
# ---------------------------------------------------------------------------


class TestControllerTurnAndMove:
    def test_bot_white_moves_on_even_move_count(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc, default_movetime_ms=500)
        game_full = {"id": "g1", "white": {"name": "MyBot"}, "black": {"name": "OppX"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 10000, "btime": 10000,
                               "winc": 0, "binc": 0, "status": "started"}}
        ctrl._process_game_stream("g1", iter([game_full]))
        events = drain(ctrl)
        assert any(isinstance(e, GameStarted) for e in events)
        assert any(isinstance(e, EngineMoved) for e in events)
        assert len(fc.posted_moves) == 1
        # The posted move is a legal white opening move.
        b = Board.from_fen(STARTING_FEN)
        assert uci_to_move(fc.posted_moves[0][1]) in generate_legal_moves(b, "w")

    def test_bot_black_moves_on_odd_move_count(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc, default_movetime_ms=500)
        game_full = {"id": "g2", "white": {"name": "OppX"}, "black": {"name": "MyBot"},
                     "initialFen": "startpos",
                     "state": {"moves": "e2e4", "wtime": 10000, "btime": 10000,
                               "winc": 0, "binc": 0, "status": "started"}}
        ctrl._process_game_stream("g2", iter([game_full]))
        events = drain(ctrl)
        gs = [e for e in events if isinstance(e, GameStarted)][0]
        assert gs.bot_is_white is False
        assert len(fc.posted_moves) == 1
        b = Board.from_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")
        assert uci_to_move(fc.posted_moves[0][1]) in generate_legal_moves(b, "b")

    def test_bot_does_not_move_on_opponent_turn(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc, default_movetime_ms=500)
        game_full = {"id": "g", "white": {"name": "MyBot"}, "black": {"name": "O"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 10000, "btime": 10000,
                               "winc": 0, "binc": 0, "status": "started"}}
        # After gameFull (bot moves once), a gameState with 1 move means it is
        # the opponent's turn — the bot must NOT move again.
        gs1 = {"type": "gameState", "moves": "e2e4", "wtime": 9000, "btime": 10000,
               "winc": 0, "binc": 0, "status": "started"}
        ctrl._process_game_stream("g", iter([game_full, gs1]))
        drain(ctrl)
        assert len(fc.posted_moves) == 1  # only the initial move

    def test_game_over_emits_game_finished_and_no_move(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "MyBot"}, "black": {"name": "O"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 0, "btime": 0,
                               "winc": 0, "binc": 0, "status": "started"}}
        mate = {"type": "gameState", "moves": "f2f3 e7e5 g2g4 d8h4",
                "wtime": 10000, "btime": 10000, "winc": 0, "binc": 0,
                "status": "mate", "winner": "black"}
        ctrl._process_game_stream("g", iter([game_full, mate]))
        events = drain(ctrl)
        finished = [e for e in events if isinstance(e, GameFinished)]
        assert len(finished) == 1
        assert finished[0].status == "mate"
        assert finished[0].winner == "black"

    def test_gamefull_already_aborted_emits_started_then_finished_no_move(self):
        # The instant-abort-at-creation case: the gameFull we receive already
        # carries status "aborted" (the game was aborted at creation, e.g. by the
        # opponent's duplicate event-stream conflict). We must push GameStarted
        # then GameFinished and NEVER call _maybe_move — so no "Engine thinking..."
        # Status and no posted move. The GUI keys off that absence to diagnose it.
        # The controller also surfaces a summary of the gameFull (status/source/
        # speed/titles) so the actual status is visible, not just "Game aborted".
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "MyBot", "title": "BOT"},
                     "black": {"name": "O", "title": "BOT"},
                     "initialFen": "startpos", "source": "friend", "speed": "blitz",
                     "variant": {"key": "standard", "name": "Standard"},
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 0, "binc": 0, "status": "aborted"}}
        ctrl._process_game_stream("g", iter([game_full]))
        events = drain(ctrl)
        types = [type(e) for e in events]
        # GameStarted must precede GameFinished; no EngineMoved / no "Engine
        # thinking..." Status in between (we never started thinking).
        assert GameStarted in types
        assert GameFinished in types
        assert types.index(GameStarted) < types.index(GameFinished)
        assert EngineMoved not in types
        assert not any(isinstance(e, Status) and e.message == "Engine thinking..."
                       for e in events)
        assert fc.posted_moves == []           # we never moved
        finished = [e for e in events if isinstance(e, GameFinished)][0]
        assert finished.status == "aborted"
        # A summary Status surfaces the actual status + gameFull fields.
        summary = [e for e in events if isinstance(e, Status)
                   and "already over" in e.message]
        assert len(summary) == 1
        msg = summary[0].message
        assert "status=aborted" in msg
        assert "source=friend" in msg
        assert "speed=blitz" in msg
        assert "white=MyBot/BOT" in msg
        assert "black=O/BOT" in msg

    def test_gamefull_already_over_nostart_surfaces_actual_status(self):
        # "noStart" (aborted BEFORE it started) is a different cause from
        # "aborted". The summary must report the ACTUAL status so the GUI can
        # distinguish them — both render as "Game aborted" otherwise.
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "MyBot"}, "black": {"name": "O"},
                     "initialFen": "startpos", "source": "friend", "speed": "blitz",
                     "state": {"moves": "", "wtime": 0, "btime": 0,
                               "winc": 0, "binc": 0, "status": "noStart"}}
        ctrl._process_game_stream("g", iter([game_full]))
        events = drain(ctrl)
        finished = [e for e in events if isinstance(e, GameFinished)][0]
        assert finished.status == "noStart"
        summary = [e for e in events if isinstance(e, Status)
                   and "already over" in e.message][0]
        assert "status=noStart" in summary.message

    def test_already_over_gamefull_then_trailing_gamestate_emits_single_finished(self):
        # Game ENTGYOFG (2026-06-28): the game stream sent a gameFull already
        # carrying status "aborted" FOLLOWED BY a trailing gameState that also
        # carried status "aborted". Both the gameFull branch and the gameState
        # branch detect game-over, so a naive loop pushes TWO GameFinished — and
        # the GUI then logged "Game over: Game aborted" + the diagnostic TWICE.
        # The controller must stop reading the stream once the game is over, so
        # exactly ONE GameFinished is emitted for the game.
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "MyBot", "title": "BOT"},
                     "black": {"name": "O", "title": "BOT"},
                     "initialFen": "startpos", "source": "friend", "speed": "blitz",
                     "variant": {"key": "standard", "name": "Standard"},
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 0, "binc": 0, "status": "aborted"}}
        trailing = {"type": "gameState", "moves": "", "wtime": 300000,
                    "btime": 300000, "winc": 0, "binc": 0, "status": "aborted"}
        ctrl._process_game_stream("g", iter([game_full, trailing]))
        events = drain(ctrl)
        finished = [e for e in events if isinstance(e, GameFinished)]
        assert len(finished) == 1
        assert finished[0].status == "aborted"

    def test_game_stream_emits_open_and_close_with_counter(self):
        # Opponent's xmiao_glm.md §1: log a game-stream OPEN/CLOSE pair with a
        # per-game counter so a DUPLICATE stream connection (n>1) is visible --
        # the single most useful diagnostic for the instant-at-creation abort.
        # OPEN fires at the top of _process_game_stream (before GameStarted);
        # CLOSE fires in the finally (after the last event). n=1 for a normal
        # single stream; n>1 means a second stream for the same game.
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "ENTGYOFG", "white": {"name": "MyBot", "title": "BOT"},
                     "black": {"name": "O", "title": "BOT"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 0, "binc": 0, "status": "started"}}
        ctrl._process_game_stream("ENTGYOFG", iter([game_full]))
        events = drain(ctrl)
        statuses = [e.message for e in events if isinstance(e, Status)]
        opens = [s for s in statuses if "Game stream OPEN" in s]
        closes = [s for s in statuses if "Game stream CLOSE" in s]
        assert len(opens) == 1, statuses
        assert len(closes) == 1, statuses
        assert "ENTGYOFG" in opens[0] and "n=1" in opens[0]
        assert "ENTGYOFG" in closes[0] and "n=1" in closes[0]
        # OPEN must precede GameStarted; CLOSE is the final event.
        types = [type(e) for e in events]
        assert types.index(Status) < types.index(GameStarted)
        assert types[-1] is Status  # CLOSE is last

    def test_game_stream_close_fires_on_already_over_early_return(self):
        # The already-over gameFull branch returns early; the try/finally must
        # still emit CLOSE so a stream that never reached the normal loop end is
        # balanced (a missing CLOSE would mask a leaked stream).
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "MyBot", "title": "BOT"},
                     "black": {"name": "O", "title": "BOT"},
                     "initialFen": "startpos", "source": "friend", "speed": "blitz",
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 0, "binc": 0, "status": "aborted"}}
        ctrl._process_game_stream("g", iter([game_full]))
        events = drain(ctrl)
        statuses = [e.message for e in events if isinstance(e, Status)]
        assert any("Game stream OPEN" in s and "n=1" in s for s in statuses), statuses
        assert any("Game stream CLOSE" in s and "n=1" in s for s in statuses), statuses

    def test_gamefull_status_on_arrival_live_game(self):
        # Opponent's xmiao_glm.md §2: log the gameFull's status on arrival so a
        # LIVE game (status=started) is distinguishable from a DEAD one
        # (status=aborted/noStart) in the activity log.
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "xmiao_glm", "title": "BOT"},
                     "black": {"name": "xmiao_ds", "title": "BOT"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 0, "binc": 0, "status": "started"}}
        ctrl._process_game_stream("g", iter([game_full]))
        events = drain(ctrl)
        gf = [e.message for e in events if isinstance(e, Status)
              and "gameFull:" in e.message]
        assert gf, [e.message for e in events if isinstance(e, Status)]
        assert "status=started" in gf[0]
        assert "White=xmiao_glm" in gf[0]
        assert "Black=xmiao_ds" in gf[0]

    def test_gamefull_status_on_arrival_already_over(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "MyBot", "title": "BOT"},
                     "black": {"name": "O", "title": "BOT"},
                     "initialFen": "startpos", "source": "friend", "speed": "blitz",
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 0, "binc": 0, "status": "aborted"}}
        ctrl._process_game_stream("g", iter([game_full]))
        events = drain(ctrl)
        gf = [e.message for e in events if isinstance(e, Status)
              and "gameFull:" in e.message]
        assert gf, [e.message for e in events if isinstance(e, Status)]
        assert "status=aborted" in gf[0]


class TestControllerTimeBudget:
    def test_correspondence_uses_default(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc, default_movetime_ms=700)
        assert ctrl._time_budget({"wtime": 0, "btime": 0, "winc": 0, "binc": 0}, True) == 700

    def test_normal_budget_divides_by_20(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc, default_movetime_ms=500)
        assert ctrl._time_budget({"wtime": 60000, "btime": 60000, "winc": 0, "binc": 0}, True) == 3000

    def test_budget_capped_at_5000(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        assert ctrl._time_budget({"wtime": 100000, "btime": 100000, "winc": 0, "binc": 0}, True) == 5000

    def test_budget_floored_at_200(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        assert ctrl._time_budget({"wtime": 1000, "btime": 1000, "winc": 0, "binc": 0}, True) == 200

    def test_budget_includes_increment(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        assert ctrl._time_budget({"wtime": 60000, "btime": 60000, "winc": 2000, "binc": 2000}, True) == 4000

    def test_uses_black_clock_when_bot_is_black(self):
        fc = FakeClient("MyBot")
        ctrl = make_controller(fc)
        # bot is black: budget based on btime (30000//20 = 1500)
        assert ctrl._time_budget({"wtime": 60000, "btime": 30000, "winc": 0, "binc": 0}, False) == 1500

    def test_first_move_budget_capped_to_lock_game_quickly(self):
        # The bot's first move should think briefly so it lands before the
        # opponent can abort (Lichess disallows single-player abort once a move
        # is on the board). A 5+3 game would normally budget 5000ms; move 1 is
        # capped to FIRST_MOVE_BUDGET_MS. Uses a NON-starting FEN because the
        # standard start position now uses the instant opening-book move (see
        # test_first_move_white_startpos_uses_opening_book_skips_engine); the
        # cap still governs the engine path for any custom start position.
        fc = FakeClient("MyBot")
        captured = {}
        def choosing(board, time_limit_ms=None):
            captured["ms"] = time_limit_ms
            return fake_choose(board, time_limit_ms)
        ctrl = LichessController(token="x", client=fc, engine_choose=choosing)
        ctrl._username = "MyBot"
        # White to move, no moves played, but NOT the standard start position ->
        # the opening book does not apply, so the engine is used and its budget
        # is capped to FIRST_MOVE_BUDGET_MS. (A real middlegame FEN with White to
        # move, used here as a custom start position.)
        custom_fen = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
        game_full = {"id": "g1", "white": {"name": "MyBot"}, "black": {"name": "OppX"},
                     "initialFen": custom_fen,
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 3000, "binc": 3000, "status": "started"}}
        ctrl._process_game_stream("g1", iter([game_full]))
        drain(ctrl)
        assert captured["ms"] == FIRST_MOVE_BUDGET_MS
        assert len(fc.posted_moves) == 1  # a move was still sent

    def test_first_move_black_also_capped(self):
        # The first-move cap (len(moves) <= 1) also covers OUR first move as
        # Black (move 2, after White's move 1): the game already has a move on
        # the board, so this is pre-existing behavior -- the cap is broader than
        # the abort-lock rationale strictly requires. Pinned here so any later
        # narrowing (e.g. to len==0 only) is intentional. The book does not apply
        # (White-only).
        fc = FakeClient("MyBot")
        captured = {}
        def choosing(board, time_limit_ms=None):
            captured["ms"] = time_limit_ms
            return fake_choose(board, time_limit_ms)
        ctrl = LichessController(token="x", client=fc, engine_choose=choosing)
        ctrl._username = "MyBot"
        # We are Black; White has played e2e4 (1 move) -> our first move, len==1.
        game_full = {"id": "g1", "white": {"name": "OppX"}, "black": {"name": "MyBot"},
                     "initialFen": "startpos",
                     "state": {"moves": "e2e4", "wtime": 300000, "btime": 300000,
                               "winc": 3000, "binc": 3000, "status": "started"}}
        ctrl._process_game_stream("g1", iter([game_full]))
        drain(ctrl)
        assert captured["ms"] == FIRST_MOVE_BUDGET_MS
        assert len(fc.posted_moves) == 1

    def test_later_move_uses_normal_budget(self):
        # Only the bot's FIRST move is capped; subsequent moves use the normal
        # budget (5000ms for a 5+3 game with plenty of time), so play stays strong.
        fc = FakeClient("MyBot")
        captured = {}
        def choosing(board, time_limit_ms=None):
            captured["ms"] = time_limit_ms
            return fake_choose(board, time_limit_ms)
        ctrl = LichessController(token="x", client=fc, engine_choose=choosing)
        ctrl._username = "MyBot"
        # Two moves already played (White e2e4, Black e7e5); bot is White to move
        # (len 2, even) — this is its second move, so the first-move cap does NOT
        # apply.
        game_full = {"id": "g1", "white": {"name": "MyBot"}, "black": {"name": "OppX"},
                     "initialFen": "startpos",
                     "state": {"moves": "e2e4 e7e5", "wtime": 300000, "btime": 300000,
                               "winc": 3000, "binc": 3000, "status": "started"}}
        ctrl._process_game_stream("g1", iter([game_full]))
        drain(ctrl)
        assert captured["ms"] == 5000

    def test_first_move_white_startpos_uses_opening_book_skips_engine(self):
        # EXPERIMENT A: as White, move 1 from the standard start position is
        # played from the opening book with NO engine think, so it lands at
        # ~POST-RTT -- before a same-owner creation abort (~0.5-1s, faster than
        # any engine think) can land, IF the abort respects moves-on-board.
        # The engine must NOT be called; a book move must be posted, with a
        # "Playing opening book (...)" Status (so the GUI marks engine_started).
        fc = FakeClient("MyBot")
        engine_called = {"n": 0}
        def choosing(board, time_limit_ms=None):
            engine_called["n"] += 1
            return fake_choose(board, time_limit_ms)
        ctrl = LichessController(token="x", client=fc, engine_choose=choosing)
        ctrl._username = "MyBot"
        game_full = {"id": "g1", "white": {"name": "MyBot"}, "black": {"name": "OppX"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 3000, "binc": 3000, "status": "started"}}
        ctrl._process_game_stream("g1", iter([game_full]))
        events = drain(ctrl)
        assert engine_called["n"] == 0  # engine skipped -- instant book move
        assert len(fc.posted_moves) == 1
        posted_uci = fc.posted_moves[0][1]
        assert posted_uci in _OPENING_BOOK_WHITE_MOVE1  # a book move was played
        status_msgs = [e.message for e in events if isinstance(e, Status)]
        assert any(m.startswith("Playing opening book") for m in status_msgs)
        assert not any(m == "Engine thinking..." for m in status_msgs)  # no think
        moved = [e for e in events if isinstance(e, EngineMoved)]
        assert len(moved) == 1 and moved[0].uci == posted_uci

    def test_first_move_black_startpos_does_not_use_book(self):
        # When we are Black, move 1 is White's (we wait); the book (White-only)
        # does not apply. The engine is not called here only because it is not
        # our turn -- the point is no book move is posted for Black's first move.
        fc = FakeClient("MyBot")
        engine_called = {"n": 0}
        def choosing(board, time_limit_ms=None):
            engine_called["n"] += 1
            return fake_choose(board, time_limit_ms)
        ctrl = LichessController(token="x", client=fc, engine_choose=choosing)
        ctrl._username = "MyBot"
        game_full = {"id": "g1", "white": {"name": "OppX"}, "black": {"name": "MyBot"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 300000, "btime": 300000,
                               "winc": 3000, "binc": 3000, "status": "started"}}
        ctrl._process_game_stream("g1", iter([game_full]))
        drain(ctrl)
        assert engine_called["n"] == 0  # not our turn (Black waits for White)
        assert len(fc.posted_moves) == 0  # no book move posted for Black

    def test_instant_first_move_white_startpos_returns_book_move(self):
        ctrl = LichessController(token="x", client=FakeClient("MyBot"),
                                 engine_choose=fake_choose)
        board = Board.from_fen(STARTING_FEN)
        mv = ctrl._instant_first_move(board, STARTING_FEN, (), bot_is_white=True)
        assert mv is not None
        assert mv in _OPENING_BOOK_WHITE_MOVE1
        # And the returned move is actually legal in the start position.
        legal = {move_to_uci(m) for m in generate_legal_moves(board, board.active_color)}
        assert mv in legal

    def test_instant_first_move_black_returns_none(self):
        # The book is White-only: when we are Black we cannot move first.
        ctrl = LichessController(token="x", client=FakeClient("MyBot"),
                                 engine_choose=fake_choose)
        board = Board.from_fen(STARTING_FEN)
        assert ctrl._instant_first_move(board, STARTING_FEN, (), bot_is_white=False) is None

    def test_instant_first_move_after_moves_returns_none(self):
        # Only move 1 (no moves played) qualifies; move 2+ uses the engine.
        # The method guards on `moves` before inspecting the board, so the board
        # passed here is irrelevant (None is returned on the moves guard).
        ctrl = LichessController(token="x", client=FakeClient("MyBot"),
                                 engine_choose=fake_choose)
        board = Board.from_fen(STARTING_FEN)
        assert ctrl._instant_first_move(board, STARTING_FEN, ("e2e4",),
                                        bot_is_white=True) is None

    def test_instant_first_move_non_standard_fen_returns_none(self):
        # A custom / Chess960 start position is not covered by the book; the
        # engine is used instead (a hardcoded move's legality can't be assumed).
        ctrl = LichessController(token="x", client=FakeClient("MyBot"),
                                 engine_choose=fake_choose)
        custom_fen = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
        board = Board.from_fen(custom_fen)
        assert ctrl._instant_first_move(board, custom_fen, (), bot_is_white=True) is None

    def test_instant_first_move_skips_illegal_book_entry(self):
        # Safety: a book entry that is not legal in the position is skipped
        # (returns None if no entry is legal) -- we never post an illegal move
        # even if the book tuple is misconfigured. Membership is by UCI string,
        # so an off-board entry like "z9z9" is simply absent from the legal set.
        ctrl = LichessController(token="x", client=FakeClient("MyBot"),
                                 engine_choose=fake_choose)
        board = Board.from_fen(STARTING_FEN)
        with patch("src.lichess_controller._OPENING_BOOK_WHITE_MOVE1", ("z9z9",)):
            assert ctrl._instant_first_move(board, STARTING_FEN, (),
                                            bot_is_white=True) is None


class TestControllerStatus:
    @pytest.mark.parametrize("status,expected", [
        ("started", False),
        ("created", False),
        ("mate", True),
        ("resign", True),
        ("outoftime", True),
        ("draw", True),
        ("stalemate", True),
        ("aborted", True),
    ])
    def test_is_game_over_status(self, status, expected):
        assert LichessController.is_game_over_status(status) is expected


class TestControllerEventStream:
    def test_challenge_gamestart_gamefinish(self):
        fc = FakeClient("MyBot")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        started = []
        ctrl._start_game_thread = lambda gid: started.append(gid)
        events = [
            {"type": "challenge", "challenge": {"id": "ch1", "speed": "blitz",
              "variant": {"name": "standard"}, "color": "random", "rated": False,
              "user": {"name": "Challenger1"}}},
            {"type": "gameStart", "game": {"id": "gA"}},
            {"type": "gameFinish", "game": {"id": "gA"}},
        ]
        ctrl._process_event_stream(iter(events))
        out = drain(ctrl)
        challenges = [e for e in out if isinstance(e, ChallengeReceived)]
        assert len(challenges) == 1
        assert challenges[0].challenge_id == "ch1"
        assert challenges[0].opponent == "Challenger1"
        assert challenges[0].speed == "blitz"
        assert started == ["gA"]
        # gameFinish drops the game from the active set (housekeeping). The
        # event stream no longer pushes a "Game <id> finished" Status — the game
        # stream's structured GameFinished is what the GUI logs — so we assert
        # the housekeeping instead.
        assert "gA" not in ctrl._active_games

    def test_duplicate_gamestart_does_not_start_second_thread(self):
        # If the event stream reconnects and re-emits gameStart for a game we
        # are already streaming, we must not start a second, racing thread. We
        # DO log a note (the recurring pattern points at a duplicate event-stream
        # connection on our side).
        fc = FakeClient("MyBot")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        started = []
        ctrl._start_game_thread = lambda gid: started.append(gid)
        events = [{"type": "gameStart", "game": {"id": "gA"}},
                  {"type": "gameStart", "game": {"id": "gA"}}]
        ctrl._process_event_stream(iter(events))
        assert started == ["gA"]  # only one stream thread started
        assert "gA" in ctrl._streaming
        out = drain(ctrl)
        notes = [e for e in out if isinstance(e, Status)
                 and "Duplicate gameStart for gA" in e.message]
        assert len(notes) == 1
        assert "second event-stream connection" in notes[0].message


class TestControllerActions:
    def test_accept_decline_resign_abort_propagate(self):
        fc = FakeClient("MyBot")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.accept_challenge("ch1")
        assert fc.last_accept == "ch1"
        ctrl.decline_challenge("ch1")
        assert fc.last_decline == "ch1"
        ctrl.resign("gA")
        assert fc.last_resign == "gA"
        ctrl.abort("gA")
        assert fc.last_abort == "gA"

    def test_start_fetches_profile_and_emits_status(self):
        fc = FakeClient("MyBot")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        # start() pushes the Status event synchronously before the event
        # thread begins, so no sleep is needed.
        out = drain(ctrl)
        assert fc.profile_called
        assert any(isinstance(e, Status) and "MyBot" in e.message for e in out)
        ctrl.stop()

    def test_api_error_on_action_emits_error_event(self):
        class ErrClient(FakeClient):
            def resign(self, gid):
                raise LichessAPIError("nope")
        fc = ErrClient("MyBot")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.resign("gA")
        out = drain(ctrl)
        assert any(isinstance(e, Error) for e in out)


# ---------------------------------------------------------------------------
# LichessController — Bot-account detection + upgrade
# ---------------------------------------------------------------------------


class TestControllerBotAccount:
    def test_start_detects_non_bot_and_blocks_challenge(self):
        # A normal account (no "BOT" title) cannot use bot-only endpoints.
        # start() must flag it, push an actionable error, and block challenges.
        fc = FakeClient("xmiao_glm", title=None)
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        assert ctrl.is_bot is False
        out = drain(ctrl)
        assert any(isinstance(e, Status) and "Connected as xmiao_glm" in e.message
                   for e in out)
        assert any(isinstance(e, Error) and "Bot account" in str(e) for e in out)
        ctrl.stop()
        # Manual challenge is blocked until upgraded.
        fc.created_challenges = []
        ctrl.challenge("xmiao_ds")
        out = drain(ctrl)
        assert fc.created_challenges == []
        assert any(isinstance(e, Error) and "Upgrade to Bot" in str(e) for e in out)

    def test_start_detects_bot_account(self):
        fc = FakeClient("realbot", title="BOT")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        assert ctrl.is_bot is True
        out = drain(ctrl)
        assert not any(isinstance(e, Error) and "Bot account" in str(e) for e in out)
        ctrl.stop()

    def test_auto_challenge_not_started_for_non_bot(self):
        fc = FakeClient("xmiao_glm", title=None)
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose,
                                 opponents=("xmiao_ds",), auto_challenge=True,
                                 singleton_bind=lambda name: True)
        ctrl.start()
        ctrl.stop()
        # No auto-challenge thread should be running for a non-bot account.
        assert ctrl._challenge_thread is None
        assert fc.created_challenges == []

    def test_upgrade_account_promotes_to_bot(self):
        fc = FakeClient("xmiao_glm", title=None)
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        drain(ctrl)
        assert ctrl.is_bot is False
        ctrl.upgrade_account()
        assert fc.upgrade_called is True
        assert fc.profile_calls == 2  # initial start() + re-fetch after upgrade
        assert ctrl.is_bot is True
        out = drain(ctrl)
        assert any(isinstance(e, Status) and "Upgraded to Bot" in e.message
                   for e in out)
        # After upgrade, challenging works.
        ctrl.challenge("xmiao_ds")
        assert len(fc.created_challenges) == 1
        ctrl.stop()

    def test_upgrade_failure_emits_error_and_stays_non_bot(self):
        class ErrClient(FakeClient):
            def upgrade_to_bot(self):
                raise LichessAPIError("nope", status=400)
        fc = ErrClient("xmiao_glm", title=None)
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        drain(ctrl)
        ctrl.upgrade_account()
        assert ctrl.is_bot is False
        out = drain(ctrl)
        assert any(isinstance(e, Error) and "upgrade failed" in str(e) for e in out)
        ctrl.stop()


# ---------------------------------------------------------------------------
# LichessController — challenge initiation + auto-match
# ---------------------------------------------------------------------------


def _challenge_event(cid, direction="in", name="beta"):
    """Build a challenge event with the right opponent field per direction."""
    if direction == "out":
        return {"type": "challenge", "challenge": {
            "id": cid, "direction": "out", "speed": "blitz",
            "variant": {"name": "standard"}, "color": "random", "rated": False,
            "destUser": {"name": name}}}
    return {"type": "challenge", "challenge": {
        "id": cid, "direction": "in", "speed": "blitz",
        "variant": {"name": "standard"}, "color": "random", "rated": False,
        "user": {"name": name}}}


class TestControllerChallenge:
    def test_manual_challenge_creates_and_emits_challenge_sent(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)
        ctrl.challenge("BotY")
        out = drain(ctrl)
        assert len(fc.created_challenges) == 1
        assert fc.created_challenges[0]["opponent"] == "BotY"
        sent = [e for e in out if isinstance(e, ChallengeSent)]
        assert len(sent) == 1 and sent[0].opponent == "BotY"
        assert ctrl._pending_outgoing.get("BotY")

    def test_challenge_sent_reports_speed_and_clock_from_lichess(self):
        # The speed Lichess actually assigned must be surfaced so the GUI can warn
        # if our clock was dropped (correspondence) vs accepted (rapid). The
        # FakeClient mirrors Lichess: clock_limit_s=300 -> "rapid", clock 300+3.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)  # default clock_limit_s=300, increment=3
        ctrl.challenge("BotY")
        sent = [e for e in drain(ctrl) if isinstance(e, ChallengeSent)]
        assert len(sent) == 1
        assert sent[0].speed == "rapid"
        assert sent[0].clock == "300+3"

    def test_challenge_sent_flags_correspondence_when_no_clock(self):
        # No clock -> Lichess makes it correspondence; ChallengeSent must carry
        # that so the GUI warns (a streaming bot cannot safely play correspondence).
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, clock_limit_s=None)
        ctrl.challenge("BotY")
        sent = [e for e in drain(ctrl) if isinstance(e, ChallengeSent)]
        assert len(sent) == 1
        assert sent[0].speed == "correspondence"
        assert sent[0].clock == ""

    def test_rated_challenge_is_sent_and_echoed_on_challenge_sent(self):
        # The opponent (xmiao_ds) asked: "Are your challenges rated or casual?"
        # The answer is decided by the controller's `rated` flag -> the rated
        # param in the POST body. ChallengeSent must carry the rated flag Lichess
        # confirmed so the GUI can log "rated"/"casual" explicitly.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, rated=True)
        ctrl.challenge("BotY")
        # The controller must pass rated=True to the client (-> POST body).
        assert fc.created_challenges[0]["rated"] is True
        sent = [e for e in drain(ctrl) if isinstance(e, ChallengeSent)]
        assert len(sent) == 1
        assert sent[0].rated is True

    def test_casual_challenge_is_default_and_echoed_on_challenge_sent(self):
        # Default (no LICHESS_RATED) is casual (rated=False) — preserves prior
        # behavior. ChallengeSent.rated must be False so the GUI logs "casual".
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)  # rated defaults to False
        ctrl.challenge("BotY")
        assert fc.created_challenges[0]["rated"] is False
        sent = [e for e in drain(ctrl) if isinstance(e, ChallengeSent)]
        assert len(sent) == 1
        assert sent[0].rated is False

    def test_set_rated_changes_mode_for_next_challenge(self):
        # The GUI Rated/Casual toggle calls set_rated(); it must flip the mode the
        # controller sends on the NEXT challenge (rated is read at issue time).
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)  # default casual
        assert ctrl.rated is False
        ctrl.set_rated(True)
        assert ctrl.rated is True
        ctrl.challenge("BotY")
        assert fc.created_challenges[0]["rated"] is True
        sent = [e for e in drain(ctrl) if isinstance(e, ChallengeSent)]
        assert sent[0].rated is True

    def test_reverse_challenge_declined_while_outgoing_pending(self):
        # We just challenged a peer (pending outgoing) and their near-simultaneous
        # reverse challenge arrives before the game starts. Accepting it would
        # start a SECOND game — the two bots can each latch a different one and
        # both appear to wait on the other (one aborts). Decline to keep one game.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._pending_outgoing["beta"] = "ourOut"  # we just challenged beta
        ctrl._process_event_stream(iter([_challenge_event("ch1", "in", "beta")]))
        assert fc.last_accept is None        # NOT accepted
        assert fc.last_decline == "ch1"     # declined instead
        assert fc.last_decline_reason == "later"
        out = drain(ctrl)
        assert any(isinstance(e, Status) and "reverse" in e.message.lower()
                   for e in out)
        assert not any(isinstance(e, ChallengeReceived) for e in out)

    def test_reverse_challenge_accepted_when_no_pending_outgoing(self):
        # Without a pending outgoing challenge, a peer's normal challenge is
        # auto-accepted as before (the reverse-decline guard must not fire).
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._process_event_stream(iter([_challenge_event("ch1", "in", "beta")]))
        assert fc.last_accept == "ch1"
        assert fc.last_decline is None

    def test_manual_challenge_empty_opponent_emits_error(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)
        ctrl.challenge("   ")
        out = drain(ctrl)
        assert any(isinstance(e, Error) for e in out)
        assert fc.created_challenges == []

    def test_auto_accept_accepts_incoming_from_peer(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._process_event_stream(iter([_challenge_event("ch1", "in", "beta")]))
        assert fc.last_accept == "ch1"
        out = drain(ctrl)
        assert not any(isinstance(e, ChallengeReceived) for e in out)
        assert any(isinstance(e, Status) and "Auto-accepted" in e.message for e in out)

    def test_auto_accept_does_not_accept_non_peer(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._process_event_stream(iter([_challenge_event("ch1", "in", "randomuser")]))
        assert fc.last_accept is None
        out = drain(ctrl)
        assert any(isinstance(e, ChallengeReceived) for e in out)

    def test_auto_accept_off_pushes_challenge_received(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)  # auto_accept defaults to False
        ctrl._process_event_stream(iter([_challenge_event("ch1", "in", "beta")]))
        assert fc.last_accept is None
        out = drain(ctrl)
        assert any(isinstance(e, ChallengeReceived) for e in out)

    def test_outgoing_challenge_tracked_not_accepted(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._process_event_stream(iter([_challenge_event("chZ", "out", "beta")]))
        assert fc.last_accept is None
        assert ctrl._pending_outgoing.get("beta") == "chZ"

    def test_outgoing_challenge_without_direction_classified_by_challenger(self):
        # Our own outgoing challenge sometimes echoes back with no `direction`
        # field. It must be classified as outgoing (by matching the challenger
        # to our own username), NOT auto-accepted/surfaced as "Challenge: <self>".
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("alpha",), auto_accept=True)
        ctrl._username = "alpha"  # simulate the bot seeing its own challenge
        event = {"type": "challenge", "challenge": {
            "id": "chQ", "speed": "blitz", "variant": {"name": "standard"},
            "color": "random", "rated": False,
            "challenger": {"name": "alpha"}, "destUser": {"name": "beta"}}}
        ctrl._process_event_stream(iter([event]))
        assert fc.last_accept is None
        assert not any(isinstance(e, ChallengeReceived) for e in drain(ctrl))
        assert ctrl._pending_outgoing.get("beta") == "chQ"

    def test_outgoing_challenge_declined_surfaces_and_clears_pending(self):
        # When the opponent declines our outgoing challenge, Lichess delivers a
        # challengeDeclined event. We must surface it (so the GUI can log it —
        # the prior build silently dropped it and the log just stopped at
        # "Challenged ...") and clear the pending-outgoing tracking.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._pending_outgoing["beta"] = "chX"  # we just challenged beta
        event = {"type": "challengeDeclined", "challenge": {
            "id": "chX", "status": "declined", "speed": "blitz",
            "challenger": {"name": "alpha"}, "destUser": {"name": "beta"},
            "declineReason": "casual"}}
        ctrl._process_event_stream(iter([event]))
        out = drain(ctrl)
        declined = [e for e in out if isinstance(e, ChallengeDeclined)]
        assert len(declined) == 1
        assert declined[0].opponent == "beta"   # opponent is destUser, not us
        assert declined[0].reason == "casual"
        assert "beta" not in ctrl._pending_outgoing  # cleared

    def test_outgoing_challenge_declined_without_reason(self):
        # Some payloads omit the decline reason — still surface the decline.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",))
        ctrl._pending_outgoing["beta"] = "chX"
        event = {"type": "challengeDeclined", "challenge": {
            "id": "chX", "status": "declined", "speed": "blitz",
            "challenger": {"name": "alpha"}, "destUser": {"name": "beta"}}}
        ctrl._process_event_stream(iter([event]))
        out = drain(ctrl)
        declined = [e for e in out if isinstance(e, ChallengeDeclined)]
        assert len(declined) == 1
        assert declined[0].opponent == "beta"
        assert declined[0].reason == ""
        assert "beta" not in ctrl._pending_outgoing

    def test_outgoing_challenge_canceled_surfaces_and_clears_pending(self):
        # A canceled/expired outgoing challenge must also be surfaced + cleared.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",))
        ctrl._pending_outgoing["beta"] = "chX"
        event = {"type": "challengeCanceled", "challenge": {
            "id": "chX", "status": "canceled", "speed": "blitz",
            "destUser": {"name": "beta"}}}
        ctrl._process_event_stream(iter([event]))
        out = drain(ctrl)
        assert any(isinstance(e, Status) and "cancel" in e.message.lower()
                   and "beta" in e.message for e in out)
        assert "beta" not in ctrl._pending_outgoing

    def test_auto_accept_declines_correspondence_from_peer(self):
        # Correspondence (no clock, days/move) can't be safely resumed by our
        # streaming bot after a restart — the event stream only delivers NEW
        # gameStarts, not in-progress games — so auto-accept declines it with a
        # clear reason instead of accepting. Real-time speeds still auto-accept.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        event = {"type": "challenge", "challenge": {
            "id": "chC", "direction": "in", "speed": "correspondence",
            "variant": {"name": "standard"}, "color": "random", "rated": False,
            "user": {"name": "beta"}}}
        ctrl._process_event_stream(iter([event]))
        out = drain(ctrl)
        assert fc.last_accept is None              # not accepted
        assert fc.last_decline == "chC"            # explicitly declined
        assert fc.last_decline_reason == "tooSlow"
        assert not any(isinstance(e, ChallengeReceived) for e in out)
        assert any(isinstance(e, Status)
                   and "correspondence" in e.message
                   and "blitz" in e.message for e in out)

    def test_correspondence_from_non_peer_surfaces_for_manual(self):
        # Only peer correspondence is auto-declined; a non-peer correspondence
        # challenge still surfaces for a manual accept/decline choice so we
        # don't silently decline arbitrary accounts.
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        event = {"type": "challenge", "challenge": {
            "id": "chC", "direction": "in", "speed": "correspondence",
            "variant": {"name": "standard"}, "color": "random", "rated": False,
            "user": {"name": "randomuser"}}}
        ctrl._process_event_stream(iter([event]))
        out = drain(ctrl)
        assert fc.last_decline is None             # not auto-declined
        assert fc.last_accept is None
        recv = [e for e in out if isinstance(e, ChallengeReceived)]
        assert len(recv) == 1 and recv[0].speed == "correspondence"

    def test_game_stream_400_stops_and_discards_active(self):
        # A 400 means the game is not streamable; reconnecting would spam the
        # same error forever. The thread must stop and free the slot.
        class Stream400Client(FakeClient):
            def stream_game(self, gid, stop=None):
                raise LichessAPIError(
                    "GET .../stream/g1 -> HTTP 400: cannot stream", status=400)
        fc = Stream400Client("alpha")
        ctrl = make_controller(fc)
        ctrl._active_games.add("g1")
        ctrl._streaming.add("g1")
        t = threading.Thread(target=ctrl._game_thread, args=("g1",), daemon=True)
        t.start()
        t.join(5.0)
        assert not t.is_alive(), "game thread must stop on 400, not reconnect"
        assert "g1" not in ctrl._active_games
        assert "g1" not in ctrl._streaming
        out = drain(ctrl)
        assert any(isinstance(e, Error) and "400" in str(e) for e in out)

    def test_leader_follower_challenges_only_greater_usernames(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta", "aaa", "alpha"),
                               auto_challenge=True)
        ctrl._username = "alpha"
        ctrl._auto_challenge_step()
        names = [c["opponent"] for c in fc.created_challenges]
        assert names == ["beta"]  # only "beta" sorts after "alpha"

    def test_auto_challenge_skipped_while_in_game(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_challenge=True)
        ctrl._username = "alpha"
        ctrl._active_games.add("g1")
        ctrl._auto_challenge_step()
        assert fc.created_challenges == []

    def test_auto_challenge_cancels_previous_then_recreates(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_challenge=True)
        ctrl._username = "alpha"
        ctrl._pending_outgoing["beta"] = "oldid"
        ctrl._auto_challenge_step()
        assert "oldid" in fc.cancelled
        assert len(fc.created_challenges) == 1

    def test_gamestart_cancels_pending_and_tracks_active(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_challenge=True)
        ctrl._pending_outgoing["beta"] = "oldid"
        started = []
        ctrl._start_game_thread = lambda gid: started.append(gid)
        ctrl._process_event_stream(iter([{"type": "gameStart", "game": {"id": "g1"}}]))
        assert "oldid" in fc.cancelled
        assert ctrl._pending_outgoing == {}
        assert "g1" in ctrl._active_games
        assert started == ["g1"]

    def test_gamestart_does_not_cancel_challenge_that_became_the_game(self):
        # ROOT CAUSE of the instant-at-creation abort (user report, 2026-06-28):
        # when our outgoing challenge is accepted, Lichess REUSES the challenge
        # id as the game id (lichess-org/api issue #234 confirms the same id
        # fires for challengeCanceled and a later gameStart), so gameStart
        # arrives for that SAME id. _cancel_all_pending() then POSTed
        # /api/challenge/{id}/cancel on the challenge that JUST became the live
        # 0-move game -- which Lichess honors as the challenger withdrawing from
        # their own just-accepted challenge, aborting the game at creation (0
        # moves, <1s). The acceptor's side is clean (it never cancels -- it has
        # no pending outgoing), so the abort comes from the CHALLENGER's side
        # doing this cancel. A human web game between the same two accounts on
        # the same two machines plays fine (user-verified), so the abort is this
        # cancel call, NOT a Lichess/owner/IP policy. The fix: do NOT cancel the
        # pending-outgoing entry whose id == the active game id; only cancel
        # OTHER pending challenges (one-game-at-a-time housekeeping).
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_challenge=True)
        ctrl._username = "alpha"
        # Our outgoing challenge to beta was accepted: its id == the game id.
        ctrl._pending_outgoing["beta"] = "g1"
        started = []
        ctrl._start_game_thread = lambda gid: started.append(gid)
        ctrl._process_event_stream(iter([{"type": "gameStart", "game": {"id": "g1"}}]))
        assert "g1" not in fc.cancelled        # MUST NOT abort our own just-started game
        assert ctrl._pending_outgoing == {}    # entry cleared (forgotten, not canceled)
        assert "g1" in ctrl._active_games
        assert started == ["g1"]               # we still connect to play the game

    def test_gamestart_cancels_other_pending_but_not_the_active_game(self):
        # Multi-peer: a game starts with beta (challenge id == game id g1) while
        # we also hold an UNRELATED pending challenge to gamma. We must cancel
        # gamma's (keep one game at a time -- gamma could otherwise accept and
        # start a second game) but NOT beta's (it became the active game).
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta", "gamma"), auto_challenge=True)
        ctrl._username = "alpha"
        ctrl._pending_outgoing["beta"] = "g1"        # accepted -> became game g1
        ctrl._pending_outgoing["gamma"] = "cGamma"   # unrelated, still pending
        started = []
        ctrl._start_game_thread = lambda gid: started.append(gid)
        ctrl._process_event_stream(iter([{"type": "gameStart", "game": {"id": "g1"}}]))
        assert "g1" not in fc.cancelled        # the active game is NOT canceled
        assert "cGamma" in fc.cancelled         # the unrelated pending challenge is
        assert ctrl._pending_outgoing == {}
        assert "g1" in ctrl._active_games
        assert started == ["g1"]

    def test_gamefinish_clears_active_game(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)
        ctrl._active_games.add("g1")
        ctrl._process_event_stream(iter([{"type": "gameFinish", "game": {"id": "g1"}}]))
        assert ctrl._active_games == set()

    def test_set_auto_and_set_opponents_update_config(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)
        ctrl.set_opponents(("beta",))
        assert ctrl.opponents == ("beta",)
        ctrl.set_auto(True)
        assert ctrl.auto_accept is True and ctrl.auto_challenge is True

    def test_manual_challenge_cancels_prior_pending(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)
        ctrl._pending_outgoing["BotY"] = "oldid"
        ctrl.challenge("BotY")
        assert "oldid" in fc.cancelled
        assert ctrl._pending_outgoing.get("BotY") != "oldid"

    def test_claim_accept_blocked_when_already_in_game(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._active_games.add("g1")
        # busy -> should not accept; challenge surfaces for manual handling
        ctrl._process_event_stream(iter([_challenge_event("ch1", "in", "beta")]))
        assert fc.last_accept is None
        assert any(isinstance(e, ChallengeReceived) for e in drain(ctrl))

    def test_claim_accept_released_on_failure(self):
        class ErrClient(FakeClient):
            def accept_challenge(self, cid):
                raise LichessAPIError("nope")
        fc = ErrClient("alpha")
        ctrl = make_controller(fc, opponents=("beta",), auto_accept=True)
        ctrl._process_event_stream(iter([_challenge_event("ch1", "in", "beta")]))
        assert ctrl._accepting is False  # claim released after the failed accept

    def test_game_stream_returns_ended_on_game_over(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "alpha"}, "black": {"name": "o"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 10000, "btime": 10000,
                               "winc": 0, "binc": 0, "status": "started"}}
        mate = {"type": "gameState", "moves": "f2f3 e7e5 g2g4 d8h4",
                "wtime": 10000, "btime": 10000, "winc": 0, "binc": 0,
                "status": "mate", "winner": "black"}
        ended = ctrl._process_game_stream("g", iter([game_full, mate]))
        assert ended is True

    def test_game_stream_returns_not_ended_while_active(self):
        fc = FakeClient("alpha")
        ctrl = make_controller(fc)
        game_full = {"id": "g", "white": {"name": "alpha"}, "black": {"name": "o"},
                     "initialFen": "startpos",
                     "state": {"moves": "", "wtime": 10000, "btime": 10000,
                               "winc": 0, "binc": 0, "status": "started"}}
        ended = ctrl._process_game_stream("g", iter([game_full]))
        assert ended is False

    def test_next_backoff_resets_after_healthy_run(self):
        assert LichessController._next_backoff(30.0, 15.0) == 1.0
        assert LichessController._next_backoff(1.0, 1.0) == 2.0
        assert LichessController._next_backoff(40.0, 1.0) == 60.0


# ---------------------------------------------------------------------------
# Token loading (GUI helper, headless via dummy display)
# ---------------------------------------------------------------------------


class TestTokenLoading:
    def test_env_var_takes_precedence(self, monkeypatch):
        os = pytest.importorskip("os")
        # headless pygame
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        import pygame
        pygame.init()
        try:
            screen = pygame.display.set_mode((640, 480))
            import src.gui as gui
            g = gui.ChessGUI()
            monkeypatch.setenv("LICHESS_BOT_TOKEN", "envtok")
            assert g._get_lichess_token() == "envtok"
            monkeypatch.delenv("LICHESS_BOT_TOKEN", raising=False)
            assert g._get_lichess_token() is None
        finally:
            pygame.quit()

    def test_config_file_token_and_placeholder_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        # ensure the env var is unset so the config file is the only source
        monkeypatch.delenv("LICHESS_BOT_TOKEN", raising=False)
        import pygame
        pygame.init()
        try:
            pygame.display.set_mode((640, 480))
            import src.gui as gui
            g = gui.ChessGUI()
            # Write to a temp file (never the real repo config) and pass its
            # path explicitly — fully isolated, no backup/restore needed.
            cfg = tmp_path / "config.yml"
            cfg.write_text('token: "filetok"\n', encoding="utf-8")
            assert g._get_lichess_token(config_path=str(cfg)) == "filetok"
            cfg.write_text('token: "LICHESS_BOT_TOKEN"\n', encoding="utf-8")
            assert g._get_lichess_token(config_path=str(cfg)) is None  # placeholder rejected
        finally:
            pygame.quit()


class TestRatedEnv:
    """LICHESS_RATED selects rated vs casual challenges (default casual).

    The opponent (xmiao_ds) asked us to confirm whether we send rated or casual.
    Casual (rated=false) is the default to preserve prior behavior; ``LICHESS_RATED``
    lets the user opt into rated, which is also the decisive experiment (a rated
    own-bot game that plays would falsify the same-IP/casual candidates). NOTE:
    whether rated *fixes* the abort is UNVERIFIED — casual is a supported bot
    mode, so this is a configurable knob + a logged signal, not an asserted fix.
    """

    def _make_gui(self, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        # No token -> _start_lichess still parses the env vars, then returns
        # early (controller=None), so no real controller/threads are spawned.
        monkeypatch.delenv("LICHESS_BOT_TOKEN", raising=False)
        import pygame
        pygame.init()
        try:
            pygame.display.set_mode((640, 480))
            import src.gui as gui
            g = gui.ChessGUI()
            # Default mode is "menu"; entering Lichess mode parses the LICHESS_*
            # env vars (then bails on the missing token — no controller spawned).
            g._start_lichess()
            return g, pygame
        except Exception:
            pygame.quit()
            raise

    def test_lichess_rated_defaults_to_casual(self, monkeypatch):
        monkeypatch.delenv("LICHESS_RATED", raising=False)
        g, pygame = self._make_gui(monkeypatch)
        try:
            assert g.lichess_rated is False
        finally:
            pygame.quit()

    def test_lichess_rated_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("LICHESS_RATED", "1")
        g, pygame = self._make_gui(monkeypatch)
        try:
            assert g.lichess_rated is True
        finally:
            pygame.quit()


# ---------------------------------------------------------------------------
# GUI wiring for challenge + auto-match (headless pygame)
# ---------------------------------------------------------------------------


class _FakeLichessController:
    """Records the calls the GUI makes for challenge/auto-match."""

    def __init__(self):
        self.calls = []
        self.username = "fakebot"
        self.is_bot = True

    def challenge(self, opponent):
        self.calls.append(("challenge", opponent))

    def set_opponents(self, opponents):
        self.calls.append(("opponents", tuple(opponents)))

    def set_auto(self, enabled):
        self.calls.append(("auto", bool(enabled)))

    def set_rated(self, enabled):
        self.calls.append(("set_rated", bool(enabled)))

    def upgrade_account(self):
        self.calls.append(("upgrade",))
        self.is_bot = True


class TestLichessGuiChallenge:
    def _make_gui(self, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        import pygame
        pygame.init()
        try:
            pygame.display.set_mode((640, 480))
            import src.gui as gui
            g = gui.ChessGUI()
            g.lichess_opponent_field = gui.TextInput(g.small_font, (650, 148, 200, 24))
            g.lichess_controller = _FakeLichessController()
            return g, pygame
        except Exception:
            pygame.quit()
            raise

    def test_challenge_opponent_commits_and_calls_controller(self, monkeypatch):
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_opponent_field.set("BotY")
            g._challenge_opponent()
            assert ("opponents", ("BotY",)) in g.lichess_controller.calls
            assert ("challenge", "BotY") in g.lichess_controller.calls
        finally:
            pygame.quit()

    def test_toggle_rated_flips_and_notifies_controller(self, monkeypatch):
        # The GUI Rated/Casual toggle must flip the flag AND tell the controller,
        # so the next challenge (manual or auto) sends the new mode. Default is
        # casual (False) — preserves behavior; the user opts into rated because
        # the opponent (xmiao_ds) declines casual, NOT because rated is a proven
        # abort fix.
        g, pygame = self._make_gui(monkeypatch)
        try:
            assert g.lichess_rated is False
            g._toggle_lichess_rated()
            assert g.lichess_rated is True
            assert ("set_rated", True) in g.lichess_controller.calls
            assert any("rated" in line.lower() for line in g.lichess_log)
            g._toggle_lichess_rated()
            assert g.lichess_rated is False
            assert ("set_rated", False) in g.lichess_controller.calls
        finally:
            pygame.quit()

    def test_challenge_requires_an_opponent(self, monkeypatch):
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_opponent_field.set("")
            g._challenge_opponent()
            assert not any(c[0] == "challenge" for c in g.lichess_controller.calls)
            assert "Enter an opponent" in g.lichess_status
        finally:
            pygame.quit()

    def test_challenge_logs_challenged_once_via_event(self, monkeypatch):
        # The GUI must not log "Challenged" twice: _challenge_opponent no longer
        # logs it directly; the ChallengeSent event (drained from the controller
        # queue) is the single source. (The double "Challenged" in the user's
        # earlier log was this redundant line + the event, not a stream duplicate.)
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_opponent_field.set("BotY")
            g.lichess_log = []
            g._challenge_opponent()
            assert not any("Challenged" in line for line in g.lichess_log)
            # Simulate the ChallengeSent event arriving from the controller queue:
            g._handle_lichess_event(ChallengeSent(opponent="BotY", challenge_id="c1"))
            challenged = [l for l in g.lichess_log if "Challenged BotY" in l]
            assert len(challenged) == 1
        finally:
            pygame.quit()

    def test_challenge_sent_logs_speed_and_clock(self, monkeypatch):
        # When Lichess accepts our clock, the log must show the real speed/time
        # control (rapid 300+3) — proof the clock reached the POST body. This is
        # the positive counterpart to the correspondence warning below.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_log = []
            g._handle_lichess_event(ChallengeSent(
                opponent="BotY", challenge_id="c1", speed="rapid", clock="300+3"))
            log = "\n".join(g.lichess_log)
            assert "Challenged BotY" in log
            assert "rapid" in log
            assert "300+3" in log
            assert "correspondence" not in log.lower()
        finally:
            pygame.quit()

    def test_challenge_sent_warns_on_correspondence(self, monkeypatch):
        # If Lichess reports "correspondence" for our challenge, the clock did not
        # land (params were in the query string, not the body — issue #142). The
        # log must WARN loudly instead of silently playing a no-clock game.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_log = []
            g._handle_lichess_event(ChallengeSent(
                opponent="BotY", challenge_id="c1", speed="correspondence"))
            log = "\n".join(g.lichess_log)
            assert "Challenged BotY" in log
            assert "WARNING" in log
            assert "correspondence" in log.lower()
        finally:
            pygame.quit()

    def test_challenge_sent_logs_casual_mode(self, monkeypatch):
        # The activity log must say "casual" when rated=False — directly answering
        # the opponent's "are your challenges rated or casual?".
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_log = []
            g._handle_lichess_event(ChallengeSent(
                opponent="BotY", challenge_id="c1", speed="rapid",
                clock="300+3", rated=False))
            log = "\n".join(g.lichess_log)
            assert "Challenged BotY" in log
            assert "casual" in log.lower()
            assert "rated" not in log.lower() or "casual" in log.lower()
        finally:
            pygame.quit()

    def test_challenge_sent_logs_rated_mode(self, monkeypatch):
        # When rated=True the log must say "rated" so the user can SEE we are
        # sending rated challenges (the decisive-test configuration).
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_log = []
            g._handle_lichess_event(ChallengeSent(
                opponent="BotY", challenge_id="c1", speed="rapid",
                clock="300+3", rated=True))
            log = "\n".join(g.lichess_log)
            assert "Challenged BotY" in log
            assert "rated" in log.lower()
        finally:
            pygame.quit()

    def test_challenge_declined_is_logged(self, monkeypatch):
        # When the opponent declines our challenge, the activity log MUST show it
        # (with the reason if Lichess gave one) — the prior build silently dropped
        # the decline and the log just stopped at "Challenged ...".
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_log = []
            g._handle_lichess_event(ChallengeDeclined(
                opponent="BotY", reason="casual"))
            log = "\n".join(g.lichess_log)
            assert "BotY" in log
            assert "declin" in log.lower()
            assert "casual" in log.lower()
        finally:
            pygame.quit()

    def test_engine_thinking_is_logged(self, monkeypatch):
        # "Engine thinking..." must appear in the activity log so an abort that
        # arrives during our think is visibly "we were trying", not a silent
        # freeze (the user's abort log showed no Engine played and no error —
        # logging this proves we had started thinking when the abort hit).
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_log = []
            g._handle_lichess_event(Status("Engine thinking..."))
            assert any("Engine thinking" in line for line in g.lichess_log)
        finally:
            pygame.quit()

    def test_toggle_auto_commits_and_enables(self, monkeypatch):
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_opponent_field.set("BotY")
            assert g.lichess_auto is False
            g._toggle_lichess_auto()
            assert g.lichess_auto is True
            assert ("auto", True) in g.lichess_controller.calls
            assert ("opponents", ("BotY",)) in g.lichess_controller.calls
        finally:
            pygame.quit()

    def test_opponent_field_not_focusable_when_not_connected(self, monkeypatch):
        # With the in-GUI token input, the opponent field is hidden until the
        # connection is established (you can't challenge anyone without a
        # connection), so a click must NOT grab opponent-field focus
        # pre-connection. The token field is the pre-connection input instead
        # (covered by TestLichessTokenInput). This supersedes the old regression
        # where the opponent field was the only field and had to be editable
        # pre-connection.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_controller = None          # not connected
            g.lichess_connected = False
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            field = g.lichess_opponent_field
            center = (field.rect.centerx, field.rect.centery)
            g._handle_lichess_field_click(center)
            assert field.active is False
        finally:
            pygame.quit()

    def test_field_focusable_after_game_ends_in_review(self, monkeypatch):
        # Regression: after a game finishes the GUI enters review mode and
        # ``lichess_game`` stays set (over=True). The opponent field used to be
        # locked by the ``not review_mode`` clause, so the user could not type a
        # new opponent to challenge again without leaving Lichess via Menu. The
        # field must be editable once the game is over, even while reviewing.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_connected = True
            g.lichess_game = {"game_id": "g1", "over": True, "moves": [],
                              "opponent_name": "BotZ"}
            g.review_mode = True          # entered by _enter_review() at game end
            g.pending_challenge = None
            field = g.lichess_opponent_field
            center = (field.rect.centerx, field.rect.centery)
            g.handle_button_click(center)
            g._handle_lichess_field_click(center)
            assert field.active is True
            for ch in "BotW":
                field.handle_key(pygame.event.Event(
                    pygame.KEYDOWN, key=pygame.K_a, unicode=ch))
            assert field.text == "BotW"
        finally:
            pygame.quit()

    def test_field_locked_while_game_in_progress(self, monkeypatch):
        # While a game is actually in progress (over=False), the field must stay
        # locked so the user cannot start a second challenge mid-game.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_connected = True
            g.lichess_game = {"game_id": "g1", "over": False, "moves": [],
                              "opponent_name": "BotZ"}
            g.review_mode = False
            g.pending_challenge = None
            field = g.lichess_opponent_field
            center = (field.rect.centerx, field.rect.centery)
            g._handle_lichess_field_click(center)
            assert field.active is False
        finally:
            pygame.quit()

    def test_challenge_reports_when_not_connected(self, monkeypatch):
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_controller = None          # not connected
            g.lichess_connected = False
            g.lichess_opponent_field.set("BotX")
            g._challenge_opponent()
            assert "Not connected" in g.lichess_status
        finally:
            pygame.quit()

    def test_upgrade_button_calls_controller_when_not_bot(self, monkeypatch):
        # When the linked account is not a Bot account, the GUI exposes an
        # Upgrade button that calls controller.upgrade_account().
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_connected = True
            g.lichess_controller.is_bot = False
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            g._upgrade_lichess_bot()
            assert ("upgrade",) in g.lichess_controller.calls
            assert "Upgrading to Bot" in g.lichess_status
        finally:
            pygame.quit()

    def test_game_events_log_color_move_and_abort_hint(self, monkeypatch):
        # The activity log must record whose turn it is, our own moves, and an
        # actionable hint on abort — otherwise an opponent-side abort looks
        # indistinguishable from our bot freezing.
        from src.lichess_controller import (GameStarted, EngineMoved,
                                            GameFinished)
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            # Bot is White -> should move first.
            g._handle_lichess_event(GameStarted(
                game_id="g1", bot_is_white=True, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            g._handle_lichess_event(EngineMoved(game_id="g1", uci="e2e4"))
            g._handle_lichess_event(GameFinished(
                game_id="g1", status="aborted", winner=None,
                moves=("e2e4",), initial_fen=STARTING_FEN))
            log = "\n".join(g.lichess_log)
            assert "Game started vs xmiao_ds (you are White" in log
            assert "xmiao_ds is Black" in log   # opponent color is logged too
            assert "id=g1" in log               # game id for log correlation
            assert "Engine played e2e4" in log
            # 1 move on the board -> mid-game abort diagnostic (a player aborted).
            assert "1 move(s) played" in log
            assert "a player aborted the game mid-way" in log
        finally:
            pygame.quit()

    def test_game_started_black_logs_waiting_for_white(self, monkeypatch):
        from src.lichess_controller import GameStarted
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            g._handle_lichess_event(GameStarted(
                game_id="g2", bot_is_white=False, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            log = "\n".join(g.lichess_log)
            assert "Game started vs xmiao_ds (you are Black" in log
            assert "xmiao_ds is White" in log   # we're Black -> opponent is White
            assert "id=g2" in log
            assert "Waiting for xmiao_ds (White) to move first" in log
        finally:
            pygame.quit()

    def test_abort_diagnostic_no_moves_black_blames_white_no_first_move(self, monkeypatch):
        # The genuine no-first-move timeout: bot is Black, White (xmiao_ds) never
        # moved, ~15-30s elapsed, Lichess aborted on the timeout. The diagnostic
        # must say so explicitly — not the old generic "Aborted by a player" hint.
        # A real timeout is elapsed >= INSTANT_ABORT_THRESHOLD_S, so start the
        # clock 25s in the past (NOT 0s, which is the instant-abort-at-creation
        # case — a different cause, covered by its own test).
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g3", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks() - 25000, "over": True,
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            log = "\n".join(g.lichess_log)
            assert "White (xmiao_ds) never made the first move" in log
            assert "Lichess aborts a game automatically" in log
            assert "Aborted by a player" not in log
            assert "instant abort at creation" not in log
        finally:
            pygame.quit()

    def test_abort_diagnostic_black_already_over_is_honest_about_uncertainty(self, monkeypatch):
        # LtnFaUxZ (2026-06-28): bot is Black, the gameFull arrived ALREADY
        # aborted (elapsed ~0s, 0 moves). The opponent (xmiao_ds) WAS playing
        # (it logged "Engine thinking" + status "started", then tried g1f3 and
        # got "game already over") — so this was actually a MID-THINK abort we
        # connected slightly late to, NOT provably an instant-at-creation abort.
        # Our elapsed clock starts when WE connect, so the two regimes are
        # indistinguishable on our side. The diagnostic must NOT assert "instant
        # abort at creation" as fact; it must (a) state the limitation, (b) name
        # the duplicate-stream candidate as now WEAKENED (both sides
        # single-instance in recent cycles), (c) add the Lichess-side
        # same-owner/same-IP candidate, and (d) point at the decisive
        # experiments — not the timeout hint.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g3b", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks(), "over": True,
                "status": "aborted",
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            log = "\n".join(g.lichess_log)
            assert "we were Black" in log
            assert "already over when we connected" in log
            assert "'aborted' status" in log
            # Must NOT assert instant-at-creation as fact: elapsed is measured
            # from OUR connection, so a mid-think abort we arrived late to looks
            # identical to an instant-at-creation abort on our side.
            assert "instant abort at creation" not in log
            assert "does NOT prove" in log          # the honest limitation
            assert "mid-think" in log               # names the alternative regime
            # The duplicate-stream candidate is still named, but now WEAKENED.
            assert "DUPLICATE event-stream connection" in log
            assert "WEAKENED" in log
            # The new leading (unverified) candidate + the decisive experiments.
            assert "Lichess-side abort" in log
            assert "THIRD-PARTY bot" in log
            assert "DIFFERENT networks" in log
            assert "curl ifconfig.me" in log
            # Process-count guidance still present.
            assert "run only ONE bot process per account" in log
            assert "Connected as" in log
            assert "does NOT prove a duplicate" in log
            # Must NOT misdiagnose as the no-first-move timeout.
            assert "White (xmiao_ds) never made the first move" not in log
            assert "Lichess aborts a game automatically" not in log
        finally:
            pygame.quit()

    def test_abort_diagnostic_black_mid_think_names_conflict_or_abort(self, monkeypatch):
        # The user's abort (2026-06-27, game J9aiog88): bot is Black, xmiao_ds is
        # White, the game was LIVE for ~5s (xmiao_ds logged "Engine thinking"),
        # then aborted BEFORE White's first move (g1f3) landed -> 400 "game already
        # over". Elapsed ~5s is too short for the ~15-30s no-first-move timeout and
        # too long for an instant-at-creation abort — a THIRD regime: a mid-think
        # abort from one account double-connecting the game stream, or a manual
        # Abort. The diagnostic must NOT say "opponent not playing" (the opponent
        # WAS playing); it must name the conflict/manual-Abort causes.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g3c", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks() - 8000, "over": True,
                "status": "aborted",
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            log = "\n".join(g.lichess_log)
            assert "we were Black" in log
            assert "aborted mid-way" in log
            assert "NOT a timeout" in log
            assert "connected to the game stream TWO times" in log
            assert "Abort button was clicked" in log
            assert "extra python/lichess-bot processes on EITHER account" in log
            # Must NOT misdiagnose as the no-first-move timeout.
            assert "White (xmiao_ds) never made the first move" not in log
            assert "Lichess aborts a game automatically" not in log
            # Must NOT misdiagnose as instant-at-creation (the game lived ~8s).
            assert "instant abort at creation" not in log
            assert "already over when we connected" not in log
        finally:
            pygame.quit()

    def test_abort_diagnostic_white_no_thinking_names_same_ip_owner_candidates(self, monkeypatch):
        # The user's recurring abort: bot is White, 0 moves, and we NEVER started
        # thinking — the gameFull arrived already aborted (a creation/early abort,
        # NOT the ~15-30s no-first-move timeout). The duplicate-stream candidate is
        # now WEAKENED (both sides confirmed single-instance across NSyfD5g5,
        # CTteTHAU, ls5G7MOe, pJiIjxtm — 4 games), so the diagnostic must NOT lead
        # with it; it must name NO proven cause, demote the duplicate candidate,
        # lead with the live unverified server-side candidates (same-IP /
        # same-owner), and point at the decisive checks (curl ifconfig.me +
        # third-party bot + different public IPs) — not the old PROCESS-count hint.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g4", "bot_is_white": True,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks(), "over": True,
                "engine_started": False,
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            log = "\n".join(g.lichess_log)
            assert "we were White" in log
            assert "never started thinking" in log
            assert "already over when we connected" in log
            assert "'aborted' status" in log          # the ACTUAL status is shown
            # Honest framing: NO proven cause; duplicate demoted to WEAKENED.
            assert "NONE proven" in log
            assert "DUPLICATE event-stream connection" in log
            assert "WEAKENED" in log
            # Leading live unverified candidates: same-IP / same-owner.
            assert "Lichess-side abort" in log
            assert "same public IP" in log
            # The decisive checks (the actual next step), not the PROCESS-count hint.
            assert "curl ifconfig.me" in log
            assert "THIRD-PARTY bot" in log
            assert "DIFFERENT networks" in log
            assert "PROCESS count" not in log
            # Honest wording (game rhftKWG3): the gameStart — not the challenge —
            # is what double-connects the game stream, so this happens with SINGLE
            # challenge receipt too. But 'Connected as' line counts do NOT prove a
            # duplicate (lichess-bot logs 'Connected' on each reconnect, so two
            # 'Connected as <opp>' lines can be ONE process). The old inaccurate
            # "both accepted"/"delivered to two streams" claim is gone.
            assert "Connected as xmiao_ds" in log
            assert "whether our one challenge was received once or twice" in log
            assert "does NOT prove a duplicate" in log
            # Hygiene still present, demoted from the lead.
            assert "run only ONE bot process per account" in log
            assert "double-connects and aborts every game" in log
            # Must NOT assert instant-at-creation as proven fact (elapsed is from
            # our connection) and must NOT misdiagnose as a mid-game player abort.
            assert "instant abort at creation" not in log
            assert "both accepted" not in log
            assert "delivered to two" not in log
            assert "a player aborted the game mid-way" not in log
        finally:
            pygame.quit()

    def test_abort_diagnostic_white_was_thinking_blames_opponent_abort(self, monkeypatch):
        # Bot is White, 0 moves, but we DID start thinking (the gameFull was
        # live; the game aborted during our think before the move landed). The
        # causes are a manual opponent abort, a duplicate-stream conflict, OR a
        # Lichess-side same-owner/same-IP abort. The diagnostic must keep the
        # "had started thinking" clause (so it isn't mistaken for the
        # already-over-on-connect case) and also point at the decisive checks.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g4b", "bot_is_white": True,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks(), "over": True,
                "engine_started": True,
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            log = "\n".join(g.lichess_log)
            assert "we were White" in log
            assert "had started thinking" in log
            assert "aborted before our first move landed" in log
            assert "never started thinking" not in log
            # The live unverified server-side candidate + decisive checks too.
            assert "Lichess-side abort" in log
            assert "curl ifconfig.me" in log
            assert "THIRD-PARTY bot" in log
        finally:
            pygame.quit()

    def test_engine_thinking_status_sets_engine_started(self, monkeypatch):
        # "Engine thinking..." proves the game thread reached _maybe_move, i.e.
        # the gameFull was live. The GUI must record that so a subsequent abort
        # is diagnosed as "during our think", not "already aborted when connected".
        from src.lichess_controller import GameStarted, Status
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g._handle_lichess_event(GameStarted(
                game_id="g9", bot_is_white=True, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            assert g.lichess_game["engine_started"] is False
            g._handle_lichess_event(Status("Engine thinking..."))
            assert g.lichess_game["engine_started"] is True
        finally:
            pygame.quit()

    def test_opening_book_status_sets_engine_started(self, monkeypatch):
        # An instant opening-book first move (EXPERIMENT A) emits
        # "Playing opening book (...)" instead of "Engine thinking..." (there is
        # no think). It must STILL mark engine_started=True so a subsequent abort
        # is diagnosed as "during our move" (we reached _maybe_move on a live
        # gameFull and posted), not "already aborted when connected" -- otherwise
        # an abort that beats the POST would be mislabeled.
        from src.lichess_controller import GameStarted, Status
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g._handle_lichess_event(GameStarted(
                game_id="g9b", bot_is_white=True, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            assert g.lichess_game["engine_started"] is False
            g._handle_lichess_event(Status("Playing opening book (e2e4)..."))
            assert g.lichess_game["engine_started"] is True
        finally:
            pygame.quit()

    def test_abort_diagnostic_nostart_names_creation_time_conflict(self, monkeypatch):
        # status "noStart" = Lichess aborted the game BEFORE it started (a
        # creation-time conflict), distinct from the ~15-30s no-first-move
        # timeout. The diagnostic must say so explicitly and, like the other
        # instant-at-creation branches, name NO proven cause, demote the
        # duplicate-stream candidate (WEAKENED — both sides single-instance in
        # recent cycles), lead with the same-IP/same-owner server-side candidates,
        # and point at the decisive checks (curl ifconfig.me + third-party bot +
        # different public IPs) — regardless of our color.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "gNs", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks(), "over": True,
                "engine_started": False, "status": "noStart",
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            log = "\n".join(g.lichess_log)
            assert "status 'noStart'" in log
            assert "aborted BEFORE it started" in log
            assert "NOT a no-first-move timeout" in log
            assert "creation-time conflict" in log
            # Honest framing: NO proven cause; duplicate demoted to WEAKENED.
            assert "NONE proven" in log
            assert "DUPLICATE event-stream connection" in log
            assert "WEAKENED" in log
            # Leading live unverified candidates + decisive checks.
            assert "Lichess-side abort" in log
            assert "same public IP" in log
            assert "curl ifconfig.me" in log
            assert "THIRD-PARTY bot" in log
            assert "DIFFERENT networks" in log
            assert "PROCESS count" not in log
            # Honest wording: the gameStart double-connects the game stream
            # whether the challenge was received once or twice, but 'Connected as'
            # line counts do NOT prove a duplicate (lichess-bot logs 'Connected' on
            # reconnect).
            assert "Connected as xmiao_ds" in log
            assert "whether our one challenge was received once or twice" in log
            assert "does NOT prove a duplicate" in log
            # Hygiene still present, demoted from the lead.
            assert "run only ONE bot process per account" in log
            assert "double-connects and aborts every game" in log
            # Must NOT misdiagnose as the Black no-first-move timeout.
            assert "never made the first move" not in log
            assert "Lichess aborts a game automatically" not in log
            assert "both accepted" not in log
            assert "delivered to two" not in log
        finally:
            pygame.quit()

    def test_gamefinished_nostart_runs_diagnostic(self, monkeypatch):
        # The GameFinished handler must run the diagnostic for "noStart" too,
        # not only "aborted" — otherwise a creation-time abort is silently logged
        # as just "Game over: Game aborted" with no explanation.
        from src.lichess_controller import GameStarted, GameFinished
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            g._handle_lichess_event(GameStarted(
                game_id="gN", bot_is_white=True, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            g._handle_lichess_event(GameFinished(
                game_id="gN", status="noStart", winner=None,
                moves=(), initial_fen=STARTING_FEN))
            log = "\n".join(g.lichess_log)
            assert "Game over: Game aborted" in log   # noStart renders as aborted
            assert "status 'noStart'" in log          # ...but the diagnostic names it
            assert "creation-time conflict" in log
        finally:
            pygame.quit()

    def test_abort_diagnostic_mid_game_blames_player_abort(self, monkeypatch):
        # Moves on the board -> a player aborted mid-game (single-player abort is
        # disabled once a move is played, so this is a server / both-agreed abort).
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g5", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": ["e2e4", "e7e5", "g1f3"],
                "started_ticks": pygame.time.get_ticks(), "over": True,
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            log = "\n".join(g.lichess_log)
            assert "3 move(s) played" in log
            assert "a player aborted the game mid-way" in log
        finally:
            pygame.quit()

    def test_gamefinished_abort_black_no_moves_diagnoses_white_never_moved(self, monkeypatch):
        # End-to-end through the GameFinished handler, mirroring a genuine
        # no-first-move timeout: bot Black, White never moved for ~25s, then
        # GameFinished with status "aborted". The handler records started_ticks on
        # GameStarted; here we move that start 25s into the past so elapsed is
        # past INSTANT_ABORT_THRESHOLD_S (a back-to-back GameStarted/GameFinished
        # pair would be ~0s elapsed — the instant-abort-at-creation case, with a
        # different message covered by its own test).
        from src.lichess_controller import GameStarted, GameFinished
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            g._handle_lichess_event(GameStarted(
                game_id="g6", bot_is_white=False, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            g.lichess_game["started_ticks"] = pygame.time.get_ticks() - 25000
            g._handle_lichess_event(GameFinished(
                game_id="g6", status="aborted", winner=None,
                moves=(), initial_fen=STARTING_FEN))
            log = "\n".join(g.lichess_log)
            assert "Waiting for xmiao_ds (White) to move first" in log
            assert "White (xmiao_ds) never made the first move" in log
            assert "instant abort at creation" not in log
        finally:
            pygame.quit()

    def test_gamefinished_abort_black_already_over_is_honest_about_uncertainty(self, monkeypatch):
        # End-to-end: gameFull arrives already aborted while we're Black
        # (GameStarted then GameFinished back-to-back, ~0s elapsed). The opponent
        # may well have been playing (a mid-think abort we connected late to),
        # so the message must NOT assert "instant abort at creation" as fact;
        # it must state the limitation, demote the duplicate candidate, add the
        # Lichess-side same-owner/same-IP candidate, and point at the decisive
        # experiments — not "opponent not playing".
        from src.lichess_controller import GameStarted, GameFinished
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            g._handle_lichess_event(GameStarted(
                game_id="g6b", bot_is_white=False, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            g._handle_lichess_event(GameFinished(
                game_id="g6b", status="aborted", winner=None,
                moves=(), initial_fen=STARTING_FEN))
            log = "\n".join(g.lichess_log)
            assert "we were Black" in log
            assert "already over when we connected" in log
            assert "instant abort at creation" not in log
            assert "does NOT prove" in log
            assert "mid-think" in log
            assert "DUPLICATE event-stream connection" in log
            assert "WEAKENED" in log
            assert "Lichess-side abort" in log
            assert "THIRD-PARTY bot" in log
            assert "DIFFERENT networks" in log
            assert "curl ifconfig.me" in log
            assert "run only ONE bot process per account" in log
            assert "White (xmiao_ds) never made the first move" not in log
        finally:
            pygame.quit()

    def test_gamefinished_not_double_logged_when_already_over(self, monkeypatch):
        # Defense-in-depth: if two GameFinished events arrive for the SAME
        # already-over game (a controller double-push from a trailing gameState,
        # OR a GameUpdated that finalized it followed by a GameFinished), the
        # GUI must log "Game over: ..." and run the abort diagnostic exactly
        # ONCE. Game ENTGYOFG (2026-06-28) showed "Game over: Game aborted" +
        # the diagnostic TWICE in our log.
        from src.lichess_controller import GameStarted, GameFinished
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            g._handle_lichess_event(GameStarted(
                game_id="gD", bot_is_white=False, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            g._handle_lichess_event(GameFinished(
                game_id="gD", status="aborted", winner=None,
                moves=(), initial_fen=STARTING_FEN))
            g._handle_lichess_event(GameFinished(
                game_id="gD", status="aborted", winner=None,
                moves=(), initial_fen=STARTING_FEN))
            # Log lines are timestamp-prefixed ("HH:MM:SS Game over: ..."), so
            # count substring occurrences, not startswith.
            over_count = sum(1 for ln in g.lichess_log if "Game over: Game aborted" in ln)
            diag_count = sum(1 for ln in g.lichess_log if "Aborted within" in ln
                             or "Aborted after" in ln)
            assert over_count == 1
            assert diag_count == 1
        finally:
            pygame.quit()

    def test_abort_diagnostic_emits_concise_headline_with_mode(self, monkeypatch):
        # The detailed abort diagnostic is a wall of text unreadable in the
        # panel (truncated to 30 chars, 6 lines). The FIRST log line must be a
        # concise, factual headline — status, 0 moves, our color, and the
        # rated/casual mode (the mode is the crux of the debate; game
        # ENTGYOFG was RATED yet still aborted). The detailed cause analysis
        # follows on subsequent lines.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_last_rated = True   # we sent a RATED challenge
            g.lichess_game = {
                "game_id": "gH", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks(), "over": True,
                "status": "aborted",
            }
            g.lichess_log = []
            g._log_abort_diagnostic()
            assert g.lichess_log, "expected at least one log line"
            headline = g.lichess_log[0]
            assert "0 move" in headline          # factual: 0 moves played
            assert "[rated]" in headline         # surfaces the mode (key info)
            assert "Black" in headline           # our color
            assert len(headline) < 115           # concise (incl. timestamp prefix)
            # The detailed analysis still follows after the headline.
            detail = "\n".join(g.lichess_log[1:])
            assert "already over when we connected" in detail
        finally:
            pygame.quit()

    def test_challenge_sent_records_rated_mode(self, monkeypatch):
        # The rated/casual mode of our last OUTGOING challenge is remembered so
        # the GameStarted line and the abort headline can show it. The mode is
        # the crux of the abort investigation (game ENTGYOFG was rated yet still
        # aborted — falsifying the "casual is auto-aborted" claim).
        from src.lichess_controller import ChallengeSent
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_log = []
            g.lichess_last_rated = None
            g._handle_lichess_event(ChallengeSent(
                opponent="xmiao_ds", challenge_id="abc", rated=True))
            assert g.lichess_last_rated is True
        finally:
            pygame.quit()

    def test_game_started_line_includes_rated_mode(self, monkeypatch):
        # The GameStarted log line shows the rated/casual mode when known, so a
        # glance at the log confirms which mode the (possibly aborting) game was.
        from src.lichess_controller import GameStarted
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = None
            g.pending_challenge = None
            g.review_mode = False
            g.lichess_last_rated = True
            g.lichess_log = []
            g._handle_lichess_event(GameStarted(
                game_id="gM", bot_is_white=False, opponent_name="xmiao_ds",
                initial_fen=STARTING_FEN, moves=(), wtime=300000, btime=300000))
            started_line = [ln for ln in g.lichess_log if "Game started" in ln][0]
            assert "rated" in started_line
        finally:
            pygame.quit()

    def test_warn_if_opponent_stalled_fires_after_threshold(self, monkeypatch):
        # Proactive warning: 0 moves and >NO_FIRST_MOVE_WARN_S elapsed while the
        # game is still live (over=False) -> warn once that White isn't moving,
        # before the abort lands.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g7", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks() - 21000,
                "no_first_move_warned": False, "over": False,
            }
            g.lichess_log = []
            g._warn_if_opponent_stalled()
            log = "\n".join(g.lichess_log)
            assert "No first move after" in log
            assert "White (xmiao_ds) hasn't moved" in log
            assert g.lichess_game["no_first_move_warned"] is True
        finally:
            pygame.quit()

    def test_warn_if_opponent_stalled_skips_when_moves_played(self, monkeypatch):
        # Once a move is on the board the game is progressing -> no warning.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g8", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": ["e2e4"],
                "started_ticks": pygame.time.get_ticks() - 21000,
                "no_first_move_warned": False, "over": False,
            }
            g.lichess_log = []
            g._warn_if_opponent_stalled()
            assert not any("No first move" in l for l in g.lichess_log)
        finally:
            pygame.quit()

    def test_warn_if_opponent_stalled_skips_when_game_over(self, monkeypatch):
        # Over game (abort already arrived) -> no proactive warning.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g9", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks() - 21000,
                "no_first_move_warned": False, "over": True,
            }
            g.lichess_log = []
            g._warn_if_opponent_stalled()
            assert not any("No first move" in l for l in g.lichess_log)
        finally:
            pygame.quit()

    def test_warn_if_opponent_stalled_fires_only_once(self, monkeypatch):
        # The one-shot flag prevents log spam across frames.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.mode = "lichess"
            g.lichess_game = {
                "game_id": "g10", "bot_is_white": False,
                "opponent_name": "xmiao_ds", "moves": [],
                "started_ticks": pygame.time.get_ticks() - 21000,
                "no_first_move_warned": False, "over": False,
            }
            g.lichess_log = []
            g._warn_if_opponent_stalled()
            g._warn_if_opponent_stalled()
            warned = [l for l in g.lichess_log if "No first move after" in l]
            assert len(warned) == 1
        finally:
            pygame.quit()


# ---------------------------------------------------------------------------
# In-GUI Lichess token input (headless pygame)
# ---------------------------------------------------------------------------
# The user can paste/type the Lichess BOT token directly in the UI and click
# Connect, instead of setting LICHESS_BOT_TOKEN in the environment beforehand.
# On connect the GUI sets the LICHESS_BOT_TOKEN env var (so the rest of the
# codebase, which reads the env var, sees it) and starts the controller. The
# token is a secret: it must NEVER be written to a log, a file, or rendered
# unmasked on screen.


class _FakeTokenController:
    """Stand-in for LichessController used by the token-connect tests.

    Records that start() was called and captures the token kwarg, without
    spawning any threads or opening sockets. The GUI only needs ``username``,
    ``is_bot``, and ``start``/``stop`` to treat it as a controller.
    """

    def __init__(self, token=None, **kwargs):
        self.kwargs = {"token": token, **kwargs}
        self.username = "fakebot"
        self.is_bot = True
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        pass


class TestLichessTokenInput:
    def _make_gui(self, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        # No token in the environment — the UI must show the token input form.
        monkeypatch.delenv("LICHESS_BOT_TOKEN", raising=False)
        import pygame
        pygame.init()
        try:
            pygame.display.set_mode((640, 480))
            import src.gui as gui
            monkeypatch.setattr(gui, "LichessController", _FakeTokenController)
            g = gui.ChessGUI()
            # Entering Lichess mode with no token must NOT spawn a controller;
            # it shows the token field so the user can enter it in the UI.
            g._start_lichess()
            assert g.lichess_token_field is not None
            assert g.lichess_controller is None
            return g, pygame
        except Exception:
            pygame.quit()
            raise

    def test_connect_sets_env_var_and_starts_controller(self, monkeypatch):
        os = pytest.importorskip("os")
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_token_field.set("tok_abc123")
            g._connect_with_token()
            # The UI input sets the env var so the rest of the codebase sees it.
            assert os.environ.get("LICHESS_BOT_TOKEN") == "tok_abc123"
            # A controller was created from that token and started.
            assert isinstance(g.lichess_controller, _FakeTokenController)
            assert g.lichess_controller.started is True
            assert g.lichess_controller.kwargs.get("token") == "tok_abc123"
        finally:
            os.environ.pop("LICHESS_BOT_TOKEN", None)
            pygame.quit()

    def test_connect_never_logs_or_renders_the_token(self, monkeypatch):
        os = pytest.importorskip("os")
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_token_field.set("SECRET_token_value")
            g._connect_with_token()
            # The token is a secret — it must never appear in the activity log.
            assert "SECRET_token_value" not in "\n".join(g.lichess_log)
            # And the masked field never holds the raw text for rendering.
            assert g.lichess_token_field.display_text() == "*" * 18
        finally:
            os.environ.pop("LICHESS_BOT_TOKEN", None)
            pygame.quit()

    def test_connect_with_empty_field_warns_without_connecting(self, monkeypatch):
        os = pytest.importorskip("os")
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_token_field.set("   ")
            g._connect_with_token()
            assert g.lichess_controller is None            # did not connect
            assert os.environ.get("LICHESS_BOT_TOKEN") is None  # did not set env
            assert "token" in g.lichess_status.lower()      # warns the user
        finally:
            pygame.quit()

    def test_paste_into_token_field_filters_to_token_chars(self, monkeypatch):
        # Ctrl+V pastes the clipboard into the masked token field, keeping only
        # token-legal chars so a trailing newline/space from copying does not
        # corrupt the token sent to Lichess.
        g, pygame = self._make_gui(monkeypatch)
        try:
            monkeypatch.setattr("src.gui.paste_from_clipboard",
                                lambda: "tok_ABC123 \n")
            g.lichess_token_field.active = True
            handled = g._handle_lichess_keydown(_ctrl_v(pygame))
            assert handled is True
            assert g.lichess_token_field.value() == "tok_ABC123"
        finally:
            pygame.quit()

    def test_token_field_focusable_when_not_connected(self, monkeypatch):
        # Before connecting, the token input is the active form — a click in it
        # grabs focus and typing works. The opponent field is hidden until
        # connected, so it must not grab focus from this click either.
        g, pygame = self._make_gui(monkeypatch)
        try:
            assert g.lichess_connected is False
            tfield = g.lichess_token_field
            center = (tfield.rect.centerx, tfield.rect.centery)
            g._handle_lichess_field_click(center)
            assert tfield.active is True
            assert g.lichess_opponent_field.active is False
            for ch in "tok_ABC123":
                tfield.handle_key(pygame.event.Event(
                    pygame.KEYDOWN, key=pygame.K_a, unicode=ch))
            assert tfield.text == "tok_ABC123"
            # Masked: the rendered text is asterisks, never the raw token.
            assert tfield.display_text() == "*" * 10
        finally:
            pygame.quit()

    def test_enter_in_token_field_connects(self, monkeypatch):
        os = pytest.importorskip("os")
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_token_field.set("tok_enter123")
            g.lichess_token_field.active = True
            enter = pygame.event.Event(pygame.KEYDOWN,
                                        {"key": pygame.K_RETURN, "unicode": "\r",
                                         "mod": 0})
            handled = g._handle_lichess_keydown(enter)
            assert handled is True
            assert os.environ.get("LICHESS_BOT_TOKEN") == "tok_enter123"
            assert isinstance(g.lichess_controller, _FakeTokenController)
        finally:
            os.environ.pop("LICHESS_BOT_TOKEN", None)
            pygame.quit()

    def test_connect_form_renders_without_error_when_not_connected(self, monkeypatch):
        # The panel must render the connect form (token label, masked field,
        # Connect button, paste hint) without error when not connected, and must
        # register the Connect button so a click triggers _connect_with_token.
        g, pygame = self._make_gui(monkeypatch)
        try:
            g._buttons = []
            g._draw_lichess_panel()
            assert any(b[4] == g._connect_with_token for b in g._buttons)
        finally:
            pygame.quit()

    def test_connect_preserves_a_long_token_not_truncated(self, monkeypatch):
        # Regression: Lichess tokens can be >= 512 chars. The lichess-org/api
        # OpenAPI spec says "Make sure your application can handle at least 512
        # characters". The masked token field MUST NOT truncate a long token --
        # if it does, the controller sends a PARTIAL token, Lichess returns
        # HTTP 401 "No such token", and every Connect shows "profile fetch
        # failed" (the user's "Profile Fetch Error"). This pinned when the
        # field's max_len was set below the spec minimum (128).
        os = pytest.importorskip("os")
        g, pygame = self._make_gui(monkeypatch)
        try:
            long_token = "a" * 512   # all token-legal chars, spec-min length
            g.lichess_token_field.set(long_token)
            g._connect_with_token()
            sent = g.lichess_controller.kwargs.get("token")
            # The FULL token reaches the controller -- no truncation.
            assert sent == long_token
            assert len(sent) == 512
            # And the env var (the rest of the codebase reads it) is the full token.
            assert os.environ.get("LICHESS_BOT_TOKEN") == long_token
        finally:
            os.environ.pop("LICHESS_BOT_TOKEN", None)
            pygame.quit()

    def test_paste_into_token_field_keeps_a_long_token(self, monkeypatch):
        # Pasting (Ctrl+V) a long token must keep the WHOLE token, not truncate
        # it at the old 128-char cap (a truncated token -> HTTP 401 "No such
        # token" -> "profile fetch failed"). Users paste tokens from lichess.org.
        g, pygame = self._make_gui(monkeypatch)
        try:
            long_token = "b" * 512
            monkeypatch.setattr("src.gui.paste_from_clipboard",
                                lambda: long_token)
            g.lichess_token_field.active = True
            handled = g._handle_lichess_keydown(_ctrl_v(pygame))
            assert handled is True
            assert g.lichess_token_field.value() == long_token
            assert len(g.lichess_token_field.value()) == 512
        finally:
            pygame.quit()

    def test_typing_ime_token_lands_exact_and_connects(self, monkeypatch):
        # Regression (the user's "Profile Fetch Error" with a VALID lip_... token):
        # on a Windows/IME setup KEYDOWN.unicode arrives EMPTY and the OS-composed
        # character arrives separately as a TEXTINPUT event. The old field
        # derived the char from the key name -- pygame.key.name(K_MINUS) is "-"
        # (the unshifted symbol), which is NOT a token-legal char, so "_" was
        # DROPPED and lip_BXsJDkm4DGXnoDrzrVEl was mangled -> Lichess HTTP 401
        # "No such token" -> "profile fetch failed". Routing TEXTINPUT to
        # insert_text must capture the EXACT token, and Connect must send that
        # exact token to the controller.
        os = pytest.importorskip("os")
        g, pygame = self._make_gui(monkeypatch)
        try:
            tfield = g.lichess_token_field
            tfield.active = True
            token = "lip_BXsJDkm4DGXnoDrzrVEl"

            def key_mod(c):
                if c == "_":
                    return pygame.K_MINUS, pygame.KMOD_SHIFT
                if c.isdigit():
                    return getattr(pygame, "K_" + c), 0
                if c.isupper():
                    return getattr(pygame, "K_" + c.lower()), pygame.KMOD_SHIFT
                return getattr(pygame, "K_" + c), 0

            for c in token:
                k, m = key_mod(c)
                # Real pygame fires KEYDOWN (empty unicode under IME) then
                # TEXTINPUT (the OS-composed char) for each keypress.
                g._handle_lichess_keydown(pygame.event.Event(
                    pygame.KEYDOWN, {"key": k, "unicode": "", "mod": m}))
                g._handle_lichess_textinput(pygame.event.Event(
                    pygame.TEXTINPUT, {"text": c}))
            assert tfield.value() == token
            g._connect_with_token()
            assert g.lichess_controller is not None
            assert g.lichess_controller.kwargs.get("token") == token
        finally:
            os.environ.pop("LICHESS_BOT_TOKEN", None)
            pygame.quit()

    def test_non_ime_keydown_and_textinput_do_not_double_insert(self, monkeypatch):
        # Non-IME: both KEYDOWN(unicode) and TEXTINPUT fire for a keypress. The
        # field must hold each char ONCE -- KEYDOWN.unicode inserts it and the
        # matching TEXTINPUT is skipped (the _keydown_handled_char dedup), so
        # "bot" is not doubled to "bbboott".
        g, pygame = self._make_gui(monkeypatch)
        try:
            tfield = g.lichess_token_field
            tfield.active = True
            for c in "bot":
                k = getattr(pygame, "K_" + c)
                g._handle_lichess_keydown(pygame.event.Event(
                    pygame.KEYDOWN, {"key": k, "unicode": c, "mod": 0}))
                g._handle_lichess_textinput(pygame.event.Event(
                    pygame.TEXTINPUT, {"text": c}))
            assert tfield.value() == "bot"
        finally:
            pygame.quit()

    def test_underscore_captured_when_ime_skips_textinput_for_it(self, monkeypatch):
        # The user's RECURRING "Profile Fetch Error" with a VALID lip_... token:
        # their IME fires TEXTINPUT for letters/digits but NOT for the underscore
        # (Shift+hyphen), so the TEXTINPUT-only path drops '_' and the token is
        # mangled (lip_... -> lip... -> HTTP 401 "No such token"). The KEYDOWN
        # fallback must capture '_' from K_MINUS+Shift even when no TEXTINPUT
        # event fires for it.
        g, pygame = self._make_gui(monkeypatch)
        try:
            tfield = g.lichess_token_field
            tfield.active = True
            token = "lip_BXsJDkm4DGXnoDrzrVEl"

            def key_mod(c):
                if c == "_":
                    return pygame.K_MINUS, pygame.KMOD_SHIFT
                if c.isdigit():
                    return getattr(pygame, "K_" + c), 0
                if c.isupper():
                    return getattr(pygame, "K_" + c.lower()), pygame.KMOD_SHIFT
                return getattr(pygame, "K_" + c), 0

            for c in token:
                k, m = key_mod(c)
                g._handle_lichess_keydown(pygame.event.Event(
                    pygame.KEYDOWN, {"key": k, "unicode": "", "mod": m}))
                if c == "_":
                    # This IME does NOT deliver a TEXTINPUT for the underscore.
                    continue
                g._handle_lichess_textinput(pygame.event.Event(
                    pygame.TEXTINPUT, {"text": c}))
            assert tfield.value() == token   # underscore preserved, not dropped
        finally:
            pygame.quit()

    def test_typing_token_survives_when_ime_skips_all_textinput(self, monkeypatch):
        # PROVEN root cause of the recurring "Profile Fetch Error": the field had
        # a KEYDOWN fallback for '_' and digits but NOT for letters, so an IME
        # that skips TEXTINPUT (for uppercase letters, or for every key) DROPPED
        # every letter -> lip_BXsJDkm4DGXnoDrzrVEl was mangled (repro showed
        # 'lip_skm4norzrl' under the no-TEXTINPUT-for-shifted model, and '_4'
        # under the no-TEXTINPUT-at-all model) -> HTTP 401 "No such token". The
        # fallback must capture LETTERS from the key too. This is the harshest
        # model: NO TEXTINPUT event for ANY character (pure KEYDOWN, empty
        # unicode) -- if letters survive this, they survive every IME model.
        g, pygame = self._make_gui(monkeypatch)
        try:
            tfield = g.lichess_token_field
            tfield.active = True
            token = "lip_BXsJDkm4DGXnoDrzrVEl"

            def key_mod(c):
                if c == "_":
                    return pygame.K_MINUS, pygame.KMOD_SHIFT
                if c.isdigit():
                    return getattr(pygame, "K_" + c), 0
                if c.isupper():
                    return getattr(pygame, "K_" + c.lower()), pygame.KMOD_SHIFT
                return getattr(pygame, "K_" + c), 0

            for c in token:
                k, m = key_mod(c)
                g._handle_lichess_keydown(pygame.event.Event(
                    pygame.KEYDOWN, {"key": k, "unicode": "", "mod": m}))
                # NO TEXTINPUT for any char -- the IME-skips-everything case.
            assert tfield.value() == token   # every letter captured via fallback
        finally:
            pygame.quit()


# Construct a pygame KEYDOWN event for Ctrl+V (paste). pygame.event.Event is
# available once pygame is initialized; tests call this inside _make_gui's
# active pygame session.
def _ctrl_v(pygame):
    return pygame.event.Event(pygame.KEYDOWN,
                              {"key": pygame.K_v,
                               "unicode": "v",
                               "mod": pygame.KMOD_CTRL})


class TestLichessCopyLog:
    def _make_gui(self, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        import pygame
        pygame.init()
        try:
            pygame.display.set_mode((860, 640))
            import src.gui as gui
            g = gui.ChessGUI()
            g.mode = "lichess"
            g.lichess_opponent_field = gui.TextInput(g.small_font, (650, 148, 200, 24))
            g.lichess_controller = _FakeLichessController()  # username == "fakebot"
            return g, pygame
        except Exception:
            pygame.quit()
            raise

    @staticmethod
    def _capture(box):
        def fake(text):
            box["text"] = text
            return True
        return fake

    def test_copy_log_copies_full_untruncated_text(self, monkeypatch):
        g, pygame = self._make_gui(monkeypatch)
        captured: dict = {}
        import src.clipboard_util as cb
        monkeypatch.setattr(cb, "copy_to_clipboard", self._capture(captured))
        try:
            long_line = "Engine error: something went very wrong " * 3  # > 30 chars
            g.lichess_log = ["connected as fakebot", long_line]
            g.lichess_status = "Playing X"
            g._copy_lichess_log()
            assert "copied" in g.lichess_status.lower()
            # The on-screen log truncates to 30 chars; the copy must include the
            # full untruncated line so it is useful for troubleshooting.
            assert long_line in captured["text"]
            assert "connected as fakebot" in captured["text"]
        finally:
            pygame.quit()

    def test_copy_log_includes_username_header(self, monkeypatch):
        g, pygame = self._make_gui(monkeypatch)
        captured: dict = {}
        import src.clipboard_util as cb
        monkeypatch.setattr(cb, "copy_to_clipboard", self._capture(captured))
        try:
            g.lichess_log = ["challenge from Y"]
            g.lichess_status = "Waiting"
            g._copy_lichess_log()
            assert captured["text"].startswith("@fakebot")
            assert "Waiting" in captured["text"]
        finally:
            pygame.quit()

    def test_copy_log_falls_back_to_file(self, monkeypatch, tmp_path):
        g, pygame = self._make_gui(monkeypatch)
        monkeypatch.chdir(tmp_path)
        import src.clipboard_util as cb
        monkeypatch.setattr(cb, "copy_to_clipboard", lambda t: False)
        try:
            g.lichess_log = ["err: boom", "disconnected"]
            g.lichess_status = "Error: boom"
            g._copy_lichess_log()
            saved = tmp_path / "lichess_activity_log.txt"
            assert saved.exists()
            content = saved.read_text(encoding="utf-8")
            assert "err: boom" in content
            assert "disconnected" in content
            assert "Error: boom" in content
            assert "saved" in g.lichess_status.lower()
        finally:
            pygame.quit()

    def test_panel_registers_copy_log_button(self, monkeypatch):
        g, pygame = self._make_gui(monkeypatch)
        try:
            g.lichess_connected = True
            g._buttons = []
            g._draw_lichess_panel()
            # The Copy Log button must be registered so a click triggers it.
            assert any(button[4] == g._copy_lichess_log for button in g._buttons)
        finally:
            pygame.quit()


# ---------------------------------------------------------------------------
# TextInput helper (headless pygame)
# ---------------------------------------------------------------------------


class TestTextInput:
    def _make(self, monkeypatch):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        import pygame
        pygame.init()
        pygame.display.set_mode((640, 480))
        from src.gui_textinput import TextInput
        font = pygame.font.Font(None, 22)
        return TextInput(font, (10, 10, 100, 24)), pygame

    def test_click_toggles_focus(self, monkeypatch):
        ti, pygame = self._make(monkeypatch)
        try:
            ti.handle_click((50, 20))   # inside the rect
            assert ti.active is True
            ti.handle_click((5, 5))     # outside the rect
            assert ti.active is False
        finally:
            pygame.quit()

    def test_handle_key_ignored_when_inactive(self, monkeypatch):
        ti, pygame = self._make(monkeypatch)
        try:
            ev = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a, unicode="a")
            assert ti.handle_key(ev) is False
            assert ti.text == ""
        finally:
            pygame.quit()

    def test_typing_and_backspace(self, monkeypatch):
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            for ch in "bot":
                ev = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_a, unicode=ch)
                ti.handle_key(ev)
            assert ti.text == "bot"
            ti.handle_key(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_BACKSPACE, unicode=""))
            assert ti.text == "bo"
        finally:
            pygame.quit()

    def test_invalid_chars_rejected(self, monkeypatch):
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ti.handle_key(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_1, unicode="!"))
            assert ti.text == ""  # '!' is not a valid username char
        finally:
            pygame.quit()

    def test_empty_unicode_keydown_inserts_letter_via_fallback(self, monkeypatch):
        # Regression (revised for the letter fallback): some Windows/IME setups
        # deliver KEYDOWN with an empty ``unicode``. KEYDOWN is CONSUMED (returns
        # True, so it does not leak to a global shortcut) and -- because some IMEs
        # never deliver a TEXTINPUT for the key -- the fallback now inserts the
        # char itself (K_b -> "b") and sets the dedup flag so the GUI's TEXTINPUT
        # handler skips the matching event. This is the fix for the recurring
        # "Profile Fetch Error": an IME that skips TEXTINPUT for letters used to
        # leave every letter dropped -> mangled token (lip_BXs... -> "_4") ->
        # Lichess HTTP 401 "No such token" -> "profile fetch failed".
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_b, unicode="")
            assert ti.handle_key(ev) is True           # consumed, no global leak
            assert ti.text == "b"                       # letter inserted by fallback
            assert ti._keydown_handled_char is True     # GUI TEXTINPUT handler skips
        finally:
            pygame.quit()

    def test_populated_unicode_keydown_inserts_and_sets_handled_flag(self, monkeypatch):
        # Non-IME path: KEYDOWN.unicode inserts the char and marks it handled so
        # the GUI's TEXTINPUT handler skips the matching event (no double-insert
        # of the same keypress).
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN, key=pygame.K_b, unicode="b")
            assert ti.handle_key(ev) is True
            assert ti.text == "b"
            assert ti._keydown_handled_char is True
        finally:
            pygame.quit()

    def test_fallback_captures_underscore_without_textinput(self, monkeypatch):
        # The user's IME skips TEXTINPUT for the underscore (Shift+hyphen). The
        # KEYDOWN fallback must still capture '_' from K_MINUS + Shift + empty
        # unicode, so lip_... tokens are not mangled to lip... (-> HTTP 401).
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN,
                                     {"key": pygame.K_MINUS, "unicode": "",
                                      "mod": pygame.KMOD_SHIFT})
            assert ti.handle_key(ev) is True
            assert ti.text == "_"
            assert ti._keydown_handled_char is True   # a later TEXTINPUT must skip
        finally:
            pygame.quit()

    def test_fallback_captures_digit_without_textinput(self, monkeypatch):
        # Digit fallback: K_5 + empty unicode (no Shift) -> "5" even with no
        # TEXTINPUT event. Digits are never IME-composed, so this is safe.
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN,
                                     {"key": pygame.K_5, "unicode": "", "mod": 0})
            assert ti.handle_key(ev) is True
            assert ti.text == "5"
        finally:
            pygame.quit()

    def test_fallback_captures_lowercase_letter_without_textinput(self, monkeypatch):
        # A letter with empty unicode (IME skips TEXTINPUT for it) must still be
        # captured from the key, else every letter in a token is dropped
        # (lip_BXs... -> "_4" -> HTTP 401 "No such token"). K_b + no Shift -> "b".
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN,
                                     {"key": pygame.K_b, "unicode": "", "mod": 0})
            assert ti.handle_key(ev) is True
            assert ti.text == "b"
            assert ti._keydown_handled_char is True   # a later TEXTINPUT must skip
        finally:
            pygame.quit()

    def test_fallback_captures_uppercase_letter_without_textinput(self, monkeypatch):
        # Shift+letter with empty unicode -> the UPPERCASE letter. This is the
        # user's actual recurring bug: their IME fires TEXTINPUT for unshifted
        # keys but NOT for Shift+letter, so every uppercase letter in
        # lip_BXsJDkm4DGXnoDrzrVEl (10 of them) was dropped -> mangled token
        # (lip_skm4norzrl) -> HTTP 401 -> "profile fetch failed".
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN,
                                     {"key": pygame.K_b, "unicode": "",
                                      "mod": pygame.KMOD_SHIFT})
            assert ti.handle_key(ev) is True
            assert ti.text == "B"
            assert ti._keydown_handled_char is True
        finally:
            pygame.quit()

    def test_fallback_letter_capslock_uppercases(self, monkeypatch):
        # CapsLock uppercases a letter just like Shift (XOR semantics).
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN,
                                     {"key": pygame.K_b, "unicode": "",
                                      "mod": pygame.KMOD_CAPS})
            assert ti.handle_key(ev) is True
            assert ti.text == "B"
        finally:
            pygame.quit()

    def test_fallback_letter_shift_xor_caps_is_lowercase(self, monkeypatch):
        # Shift + CapsLock cancel (XOR) -> lowercase, matching a real keyboard.
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN,
                                     {"key": pygame.K_b, "unicode": "",
                                      "mod": pygame.KMOD_SHIFT | pygame.KMOD_CAPS})
            assert ti.handle_key(ev) is True
            assert ti.text == "b"
        finally:
            pygame.quit()

    def test_populated_but_invalid_unicode_does_not_fallback(self, monkeypatch):
        # A populated-but-invalid unicode (e.g. "!") must be REJECTED, not
        # substituted by a key-based fallback (K_1 -> "1"). Otherwise pressing
        # '!' on the 1 key would insert '1'.
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ev = pygame.event.Event(pygame.KEYDOWN,
                                     {"key": pygame.K_1, "unicode": "!", "mod": 0})
            assert ti.handle_key(ev) is True
            assert ti.text == ""   # '!' rejected; NOT turned into '1'
        finally:
            pygame.quit()

    def test_click_focus_enables_and_disables_text_input(self, monkeypatch):
        # Focusing must enable pygame text-input mode so the OS delivers keys
        # reliably; defocusing disables it.
        ti, pygame = self._make(monkeypatch)
        calls = {"start": 0, "stop": 0}
        orig_start, orig_stop = pygame.key.start_text_input, pygame.key.stop_text_input

        def fake_start():
            calls["start"] += 1

        def fake_stop():
            calls["stop"] += 1

        try:
            pygame.key.start_text_input = fake_start
            pygame.key.stop_text_input = fake_stop
            ti.handle_click((50, 20))   # inside the rect -> focus
            assert ti.active is True
            assert calls["start"] == 1
            ti.handle_click((5, 5))      # outside the rect -> defocus
            assert ti.active is False
            assert calls["stop"] == 1
        finally:
            pygame.key.start_text_input = orig_start
            pygame.key.stop_text_input = orig_stop
            pygame.quit()

    def test_enter_defocuses(self, monkeypatch):
        ti, pygame = self._make(monkeypatch)
        try:
            ti.active = True
            ti.handle_key(pygame.event.Event(
                pygame.KEYDOWN, key=pygame.K_RETURN, unicode="\r"))
            assert ti.active is False
        finally:
            pygame.quit()

    def test_set_and_value_strips(self, monkeypatch):
        ti, pygame = self._make(monkeypatch)
        try:
            ti.set("  BotY  ")
            assert ti.text == "  BotY  "
            assert ti.value() == "BotY"
        finally:
            pygame.quit()

    def test_draw_runs_without_error(self, monkeypatch):
        ti, pygame = self._make(monkeypatch)
        try:
            screen = pygame.display.get_surface()
            ti.set("abc")
            ti.draw(screen)
            ti.active = True
            ti.draw(screen)
        finally:
            pygame.quit()

    def test_masked_display_text_is_stars(self, monkeypatch):
        # A masked field (for the secret Lichess token) shows one '*' per char
        # so the token is never visible on screen / in a screen recording. The
        # real text is still stored plainly for submission; only the display is
        # masked. '*' (not '•') because the bundled default font lacks U+2022.
        _, pygame = self._make(monkeypatch)
        try:
            from src.gui_textinput import TextInput
            font = pygame.font.Font(None, 22)
            masked = TextInput(font, (10, 10, 100, 24), mask=True)
            masked.set("abcd")
            assert masked.display_text() == "*" * 4
            # A plain (unmasked) field shows the real text.
            plain = TextInput(font, (10, 10, 100, 24))
            plain.set("abcd")
            assert plain.display_text() == "abcd"
        finally:
            pygame.quit()

    def test_insert_text_filters_by_valid_chars(self, monkeypatch):
        # Paste (Ctrl+V) inserts clipboard text, filtered to the field's
        # valid_chars — so pasting a token with stray whitespace/punctuation
        # into the token field keeps only the token chars.
        _, pygame = self._make(monkeypatch)
        try:
            from src.gui_textinput import TextInput
            font = pygame.font.Font(None, 22)
            ti = TextInput(font, (10, 10, 100, 24),
                           valid_chars=set("ab12"), max_len=20)
            ti.insert_text("a1! 2b")  # '!' and ' ' are not valid -> dropped
            assert ti.text == "a12b"
        finally:
            pygame.quit()

    def test_insert_text_respects_max_len(self, monkeypatch):
        _, pygame = self._make(monkeypatch)
        try:
            from src.gui_textinput import TextInput
            font = pygame.font.Font(None, 22)
            ti = TextInput(font, (10, 10, 100, 24),
                           valid_chars=set("abc"), max_len=3)
            ti.insert_text("abcdef")
            assert ti.text == "abc"  # truncated to max_len
        finally:
            pygame.quit()


# ---------------------------------------------------------------------------
# LichessClient — stream tracking / close_streams
# ---------------------------------------------------------------------------


class _RecordingResp(FakeResp):
    """``FakeResp`` that records whether ``close()`` was called."""

    def __init__(self, lines):
        super().__init__(lines)
        self.closed = False

    def close(self):
        self.closed = True


class TestClientCloseStreams:
    def test_close_streams_closes_open_responses(self):
        c = LichessClient(token="SECRET")
        r1, r2 = _RecordingResp(["{}"]), _RecordingResp(["{}"])
        c._register_stream(r1)
        c._register_stream(r2)
        assert len(c._open_streams) == 2
        c.close_streams()
        assert r1.closed and r2.closed
        assert c._open_streams == []

    def test_close_streams_is_noop_when_empty(self):
        c = LichessClient(token="SECRET")
        c.close_streams()  # must not raise
        assert c._open_streams == []

    def test_close_streams_is_idempotent(self):
        c = LichessClient(token="SECRET")
        r = _RecordingResp(["{}"])
        c._register_stream(r)
        c.close_streams()
        c.close_streams()  # second call: nothing left to close, must not raise
        assert r.closed

    def test_iter_ndjson_registers_and_releases_response(self):
        # While the NDJSON iterator is live the response is tracked (so
        # close_streams could tear it down); once exhausted it is released and
        # closed in the finally block.
        c = LichessClient(token="SECRET")
        resp = _RecordingResp(['{"a":1}', '{"b":2}'])
        with patch("urllib.request.urlopen", return_value=resp):
            it = c._iter_ndjson("https://lichess.org/api/stream/event")
            first = next(it)
            assert first == {"a": 1}
            assert len(c._open_streams) == 1  # tracked while open
            rest = list(it)
        assert rest == [{"b": 2}]
        assert resp.closed is True
        assert c._open_streams == []  # released after exhaustion

    def test_iter_ndjson_closes_response_if_stopped_mid_handshake(self):
        # If the stop event is already set when the (re)connect handshake
        # completes, _iter_ndjson closes the freshly-registered response and
        # yields nothing. This is the mid-handshake gap: the response registered
        # AFTER close_streams() already cleared the list (so close_streams missed
        # it), and this check tears it down instead — so a controller restart
        # never leaves a lingering second stream for this account.
        c = LichessClient(token="SECRET")
        resp = _RecordingResp(['{"a":1}'])
        stop = threading.Event()
        stop.set()
        with patch("urllib.request.urlopen", return_value=resp):
            out = list(c._iter_ndjson(
                "https://lichess.org/api/stream/event", stop=stop))
        assert out == []
        assert resp.closed is True
        assert c._open_streams == []


# ---------------------------------------------------------------------------
# LichessController — connect PID + reconnect logging + stop closes streams
# ---------------------------------------------------------------------------


class TestEventStreamLifecycle:
    def test_start_connect_status_includes_pid(self):
        fc = FakeClient("xmiao_glm", title="BOT")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        out = drain(ctrl)
        # The connect line carries the OS PID so a second `Connected as <name>`
        # line can be told apart from a reconnect: same pid = one process
        # reconnecting; a different pid = a second process. This is how we prove
        # from our own log that our side did not spawn a second event stream.
        assert any(isinstance(e, Status) and "Connected as xmiao_glm" in e.message
                   for e in out)
        assert any(isinstance(e, Status) and "PID" in e.message
                   and str(os.getpid()) in e.message for e in out)
        ctrl.stop()

    def test_start_singleton_success_logs_single_instance(self):
        # Opponent's xmiao_glm.md §3: prove our side runs a SINGLE process for
        # this account. start() acquires a per-account localhost lock; on
        # success the connect line says "single instance". The bind is injected
        # so this test never opens a real socket.
        fc = FakeClient("xmiao_glm", title="BOT")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        out = drain(ctrl)
        connect = [e for e in out if isinstance(e, Status)
                   and "Connected as xmiao_glm" in e.message]
        assert connect, out
        assert "single instance" in connect[0].message
        assert str(os.getpid()) in connect[0].message
        assert not any(isinstance(e, Error) and "Duplicate" in e.message
                       for e in out)
        ctrl.stop()

    def test_start_singleton_duplicate_warns_and_errors(self):
        # SOFT singleton: a second process for the same account is logged loudly
        # and an Error is pushed, but we keep running (non-trapping) so a
        # transient bind collision doesn't brick the GUI.
        fc = FakeClient("xmiao_glm", title="BOT")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose,
                                 singleton_bind=lambda name: False)
        ctrl.start()
        out = drain(ctrl)
        connect = [e for e in out if isinstance(e, Status)
                   and "Connected as xmiao_glm" in e.message]
        assert connect, out
        assert "DUPLICATE" in connect[0].message
        assert any(isinstance(e, Error) and "Duplicate" in e.message for e in out)
        ctrl.stop()

    def test_singleton_port_is_deterministic_per_account(self):
        # Same account -> same port (so a second process collides and is
        # detected); different accounts -> different ports (so two of OUR own
        # bots can coexist). Deterministic via hashlib.md5 (NOT hash(), which is
        # randomized per process and would defeat the lock).
        p1 = LichessController._singleton_port_for("xmiao_glm")
        p2 = LichessController._singleton_port_for("xmiao_glm")
        p3 = LichessController._singleton_port_for("xmiao_ds")
        assert p1 == p2
        assert p1 != p3
        assert 1024 <= p1 <= 65535  # registered port range

    def test_event_thread_logs_reconnect(self, monkeypatch):
        # Near-zero backoff so the reconnect line lands quickly. The event
        # stream is an empty iterator, which the event thread treats as a
        # dropped stream and reconnects. A reconnect is ONE process re-opening
        # its stream, NOT a second bot instance; logging it lets us prove from
        # our own log whether our side reconnected (and how many times).
        monkeypatch.setattr(LichessController, "_next_backoff",
                            staticmethod(lambda prev, ran: 0.02))
        fc = FakeClient("xmiao_glm", title="BOT")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.start()
        try:
            deadline = time.monotonic() + 2.0
            seen = False
            while time.monotonic() < deadline:
                try:
                    e = ctrl.event_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if isinstance(e, Status) and "Reconnecting event stream" in e.message:
                    seen = True
                    break
            assert seen, "expected a Reconnecting event stream Status"
        finally:
            ctrl.stop()

    def test_stop_calls_client_close_streams(self):
        class TrackClient(FakeClient):
            def __init__(self):
                super().__init__("xmiao_glm", title="BOT")
                self.close_calls = 0

            def close_streams(self):
                self.close_calls += 1
        fc = TrackClient()
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.stop()
        assert fc.close_calls == 1

    def test_stop_tolerates_client_without_close_streams(self):
        # FakeClient has no close_streams; stop() must not raise (guarded).
        fc = FakeClient("xmiao_glm", title="BOT")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl.stop()  # no exception

    def test_gamestart_opens_game_stream_before_cancel_http(self):
        # Regression for game LtnFaUxZ (opponent analysis xmiao_glm.md,
        # 2026-06-28): a ~2s gap between our gameStart and our game-stream open
        # let Lichess abort the game before we connected. The cause was
        # _cancel_all_pending() running a SYNCHRONOUS HTTP cancel on the
        # event-stream thread BEFORE _start_game_thread() — a wasted RTT on the
        # already-accepted challenge gated the time-critical stream open. The
        # game stream MUST open without waiting on the cancel HTTP, and the
        # cancel MUST NOT block the event stream from reading the next event.
        cancel_gate = threading.Event()
        stream_opened = threading.Event()

        class Client(FakeClient):
            def __init__(self):
                super().__init__("xmiao_glm", title="BOT")

            def stream_events(self, stop=None):
                # A pending outgoing challenge to "beta" was just accepted -> this
                # gameStart. Then keep the stream alive so the event thread does
                # not exit/reconnect mid-test.
                yield {"type": "gameStart", "game": {"id": "g1"}}
                if stop is not None:
                    stop.wait(30.0)

            def cancel_challenge(self, cid):
                self.cancelled.append(cid)
                # Block so we can prove the stream open did NOT wait on this HTTP.
                cancel_gate.wait(5.0)

            def stream_game(self, gid, stop=None):
                stream_opened.set()
                return iter([])

        fc = Client()
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose, singleton_bind=lambda name: True)
        ctrl._pending_outgoing["beta"] = "c1"  # the just-accepted challenge
        ctrl.start()
        try:
            assert stream_opened.wait(2.0), (
                "game stream did not open before the cancel HTTP completed — the "
                "cancel is gating the time-critical stream connection (the "
                "LtnFaUxZ abort window)")
        finally:
            cancel_gate.set()  # release the blocked cancel_challenge
            ctrl.stop()


class TestTokenFingerprint:
    """The activity-log token fingerprint describes the token's SHAPE (so a
    mangled capture is diagnosable from a copied log) without ever leaking the
    secret."""

    def _fp(self, monkeypatch, token):
        monkeypatch.setenv("SDL_VIDEODRIVER", "dummy")
        monkeypatch.setenv("SDL_AUDIODRIVER", "dummy")
        import pygame
        pygame.init()
        try:
            from src.gui import _token_fingerprint
            return _token_fingerprint(token)
        finally:
            pygame.quit()

    def test_describes_shape_of_real_token(self, monkeypatch):
        fp = self._fp(monkeypatch, "lip_BXsJDkm4DGXnoDrzrVEl")
        assert fp == "len=24 upper=10 lower=12 digit=1 under=1 other=0 prefix_lip=True"

    def test_never_contains_the_secret(self, monkeypatch):
        token = "lip_BXsJDkm4DGXnoDrzrVEl"
        fp = self._fp(monkeypatch, token)
        assert token not in fp
        assert "BXsJDkm4" not in fp  # no run of the actual token text leaks

    def test_flags_a_mangled_capture(self, monkeypatch):
        # The user's failure mode: capitals dropped -> lip_BXsJDkm4DGXnoDrzrVEl
        # became lip_skm4norzrl. The fingerprint makes that visible (len 14,
        # upper=0) without revealing either string.
        fp = self._fp(monkeypatch, "lip_skm4norzrl")
        assert "len=14" in fp
        assert "upper=0" in fp
        assert "prefix_lip=True" in fp
