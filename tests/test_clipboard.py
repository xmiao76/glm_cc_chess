"""Tests for the clipboard helper used by the Lichess "Copy Log" feature."""

import src.clipboard_util as cb


def test_copy_prefers_pygame_scrap(monkeypatch):
    calls = []

    def fake_scrap(text):
        calls.append(("scrap", text))
        return True

    monkeypatch.setattr(cb, "_via_pygame_scrap", fake_scrap)
    monkeypatch.setattr(cb, "_via_tkinter", lambda t: calls.append(("tk", t)) or True)
    assert cb.copy_to_clipboard("hello") is True
    assert calls == [("scrap", "hello")]  # tkinter never reached


def test_copy_falls_back_to_tkinter(monkeypatch):
    monkeypatch.setattr(cb, "_via_pygame_scrap", lambda t: False)
    seen = {}

    def fake_tk(text):
        seen["t"] = text
        return True

    monkeypatch.setattr(cb, "_via_tkinter", fake_tk)
    assert cb.copy_to_clipboard("hello") is True
    assert seen == {"t": "hello"}


def test_copy_returns_false_when_all_backends_fail(monkeypatch):
    monkeypatch.setattr(cb, "_via_pygame_scrap", lambda t: False)
    monkeypatch.setattr(cb, "_via_tkinter", lambda t: False)
    assert cb.copy_to_clipboard("hello") is False


def test_pygame_scrap_returns_false_without_display(monkeypatch):
    # No pygame display -> must return False (not crash) so tkinter can take over.
    import pygame

    monkeypatch.setattr(pygame.display, "get_init", lambda: False)
    assert cb._via_pygame_scrap("hello") is False


def test_paste_prefers_pygame_scrap(monkeypatch):
    calls = []

    def fake_scrap():
        calls.append("scrap")
        return "tok_from_scrap"

    monkeypatch.setattr(cb, "_paste_via_pygame_scrap", fake_scrap)
    monkeypatch.setattr(cb, "_paste_via_tkinter", lambda: calls.append("tk") or "tok_from_tk")
    assert cb.paste_from_clipboard() == "tok_from_scrap"
    assert calls == ["scrap"]  # tkinter never reached


def test_paste_falls_back_to_tkinter(monkeypatch):
    monkeypatch.setattr(cb, "_paste_via_pygame_scrap", lambda: None)
    assert cb._paste_via_tkinter is not None  # sanity
    monkeypatch.setattr(cb, "_paste_via_tkinter", lambda: "tok_from_tk")
    assert cb.paste_from_clipboard() == "tok_from_tk"


def test_paste_returns_none_when_all_backends_fail(monkeypatch):
    monkeypatch.setattr(cb, "_paste_via_pygame_scrap", lambda: None)
    monkeypatch.setattr(cb, "_paste_via_tkinter", lambda: None)
    assert cb.paste_from_clipboard() is None


def test_paste_pygame_scrap_returns_none_without_display(monkeypatch):
    import pygame

    monkeypatch.setattr(pygame.display, "get_init", lambda: False)
    assert cb._paste_via_pygame_scrap() is None
