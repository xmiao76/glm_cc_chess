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
