"""System clipboard access for the pygame GUI (Windows-focused, stdlib only).

The Lichess activity log needs to be copyable for troubleshooting. pygame has no
native "select text" widget, and the rendered log lines are truncated, so the
GUI copies the full source log via :func:`copy_to_clipboard`. Several backends
are tried in order so copying succeeds even when one is unavailable.
"""

from __future__ import annotations

import sys


def copy_to_clipboard(text: str) -> bool:
    """Place ``text`` on the system clipboard. Returns ``True`` on success."""
    if _via_pygame_scrap(text):
        return True
    if _via_tkinter(text):
        return True
    return False


def paste_from_clipboard() -> str | None:
    """Read text from the system clipboard. Returns the text, or ``None``.

    The inverse of :func:`copy_to_clipboard`: used by the in-GUI Lichess token
    field's Ctrl+V paste. Backends are tried in the same order (pygame scrap
    first since it already owns the display, then tkinter). The raw clipboard
    text is returned unfiltered — the caller (the token field's
    ``insert_text``) strips characters that are not valid for a Lichess token,
    so a trailing newline or space from copying does not corrupt the token.
    """
    text = _paste_via_pygame_scrap()
    if text is not None:
        return text
    return _paste_via_tkinter()


def _paste_via_pygame_scrap() -> str | None:
    """Read the clipboard via pygame's scrap module, or ``None`` if unavailable."""
    try:
        import pygame

        if not pygame.display.get_init():
            return None
        scrap = getattr(pygame, "scrap", None)
        if scrap is None:
            return None
        try:
            scrap.init()
        except Exception:  # noqa: BLE001 - scrap optional; fall through
            return None
        got = scrap.get(pygame.SCRAP_TEXT)
        if isinstance(got, bytes):
            got = got.decode("utf-8", "ignore")
        # scrap may append a NUL terminator; drop it so it isn't pasted.
        if got:
            got = got.rstrip("\x00")
        return got or None
    except Exception:  # noqa: BLE001 - clipboard is best-effort
        return None


def _paste_via_tkinter() -> str | None:
    """Read the clipboard via the stdlib Tk clipboard, or ``None``."""
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            text = root.clipboard_get()
        finally:
            root.destroy()
        return text or None
    except Exception:  # noqa: BLE001 - Tk may be absent / clipboard empty
        return None


def _via_pygame_scrap(text: str) -> bool:
    """Copy via pygame's scrap module, verifying the round-trip.

    Used first because pygame already owns the display. The verification guards
    against builds where ``put`` succeeds silently without actually owning the
    clipboard.
    """
    try:
        import pygame

        if not pygame.display.get_init():
            return False
        scrap = getattr(pygame, "scrap", None)
        if scrap is None:
            return False
        try:
            scrap.init()
        except Exception:  # noqa: BLE001 - scrap optional; fall through
            return False
        scrap.put(pygame.SCRAP_TEXT, text.encode("utf-8"))
        got = scrap.get(pygame.SCRAP_TEXT)
        if isinstance(got, bytes):
            got = got.decode("utf-8", "ignore")
        return text in (got or "")
    except Exception:  # noqa: BLE001 - clipboard is best-effort
        return False


def _via_tkinter(text: str) -> bool:
    """Copy via the stdlib Tk clipboard. Very reliable on Windows."""
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - Tk may be absent on minimal installs
        return False


def supports_clipboard() -> bool:
    """Cheap probe: is at least one backend plausibly available?

    Tk is bundled with the standard Windows Python installer, so on the target
    platform this is essentially always ``True``.
    """
    if sys.platform.startswith("win"):
        return True
    try:
        import tkinter  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False
