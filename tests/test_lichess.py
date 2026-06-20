"""Tests for src/lichess_client.py and src/lichess_controller.py.

The Lichess HTTP client is tested with ``unittest.mock`` patches on
``urllib.request.urlopen`` (no real network). The controller is tested with a
fake client feeding scripted NDJSON events. Token loading is tested through the
GUI helper using pygame's dummy video driver (headless).
"""

from __future__ import annotations

import json
import queue
from unittest.mock import patch

import pytest

from src.lichess_client import LichessClient, LichessAPIError
from src.lichess_controller import (
    LichessController,
    ChallengeReceived,
    GameStarted,
    GameUpdated,
    EngineMoved,
    GameFinished,
    Status,
    Error,
)
from src.board import STARTING_FEN, Board
from src.moves import generate_legal_moves, uci_to_move


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

    def __init__(self, username="MyBot"):
        self.username = username
        self.posted_moves = []
        self.profile_called = False
        self.last_accept = None
        self.last_decline = None
        self.last_resign = None
        self.last_abort = None

    def get_profile(self):
        self.profile_called = True
        return {"username": self.username, "id": self.username.lower()}

    def make_move(self, game_id, uci, draw=False):
        self.posted_moves.append((game_id, uci))

    def accept_challenge(self, cid):
        self.last_accept = cid

    def decline_challenge(self, cid, reason="generic"):
        self.last_decline = cid

    def resign(self, gid):
        self.last_resign = gid

    def abort(self, gid):
        self.last_abort = gid

    def stream_events(self):
        return iter([])

    def stream_game(self, gid):
        return iter([])


def fake_choose(board, time_limit_ms=None):
    """Deterministic engine: play the first legal move (or None)."""
    moves = generate_legal_moves(board, board.active_color)
    return moves[0] if moves else None


def make_controller(client, **kw):
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

    def test_make_move_with_draw_adds_query(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.make_move("g1", "a7a8q", draw=True)
        assert "draw=1" in captured["url"]
        assert "/move/a7a8q" in captured["url"]

    def test_accept_and_decline_challenge(self):
        captured = {}

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            return FakeResp([])

        c = LichessClient(token="SECRET")
        with patch("urllib.request.urlopen", side_effect=fake):
            c.accept_challenge("ch1")
        assert "/api/challenge/ch1/accept" in captured["url"]
        with patch("urllib.request.urlopen", side_effect=fake):
            c.decline_challenge("ch1", "nothanks")
        assert "/api/challenge/ch1/decline" in captured["url"]
        assert "reason=nothanks" in captured["url"]

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
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose)
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
        assert any(isinstance(e, Status) for e in out)


class TestControllerActions:
    def test_accept_decline_resign_abort_propagate(self):
        fc = FakeClient("MyBot")
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose)
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
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose)
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
        ctrl = LichessController(token="x", client=fc, engine_choose=fake_choose)
        ctrl.resign("gA")
        out = drain(ctrl)
        assert any(isinstance(e, Error) for e in out)


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