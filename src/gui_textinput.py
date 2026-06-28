"""A minimal single-line text input field for the pygame GUI.

Kept in its own module so :mod:`src.gui` stays focused on layout and game
state. The field is click-to-focus, validates characters (Lichess usernames are
letters/digits/``_``/``-``), and consumes key events while focused so global
shortcuts (e.g. ``N`` for new game) do not fire while the user is typing.

Keyboard robustness
-------------------
``KEYDOWN.unicode`` is the normal source of the typed character, but on some
Windows keyboard/IME setups it arrives empty even for ordinary keys. When that
happens the field derives the character from the key name (honoring Shift) so
typing still works. Focusing the field also enables pygame's text-input mode,
which makes the OS deliver key/IME input to the window reliably.
"""

from __future__ import annotations

import pygame

# Characters allowed in a Lichess username.
_VALID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_MAX_LEN = 24


class TextInput:
    """A focused, single-line text input backed by a fixed :class:`pygame.Rect`."""

    def __init__(self, font: pygame.font.Font, rect,
                 max_len: int = _MAX_LEN,
                 valid_chars: set[str] | None = None) -> None:
        self.font = font
        self.rect = pygame.Rect(rect)
        self.text: str = ""
        self.active: bool = False
        self.max_len = max_len
        self._valid = valid_chars if valid_chars is not None else _VALID_CHARS

    def set(self, text: str) -> None:
        """Replace the field contents (used to prefill from env/config)."""
        self.text = (text or "")[: self.max_len]

    def value(self) -> str:
        return self.text.strip()

    def _set_active(self, value: bool) -> None:
        """Set focus and keep the OS text-input mode in sync (best-effort)."""
        self.active = value
        try:
            if value:
                pygame.key.start_text_input()
            else:
                pygame.key.stop_text_input()
        except Exception:
            # text-input mode is a reliability aid, not a requirement — never
            # let a failure here block normal keyboard handling.
            pass

    def handle_click(self, pos) -> None:
        """Focus on click inside the rect; defocus on click outside."""
        self._set_active(self.rect.collidepoint(pos))

    @staticmethod
    def _char_from_event(event: pygame.event.Event) -> str:
        """Best-effort typed character for a KEYDOWN event.

        Prefers ``event.unicode``; when it is empty (seen on some Windows/IME
        setups) falls back to the key name, upper-casing it if Shift is held.
        Returns "" for keys with no printable character.
        """
        ch = getattr(event, "unicode", "") or ""
        if ch:
            return ch
        name = pygame.key.name(event.key)
        if len(name) == 1:
            if pygame.key.get_mods() & pygame.KMOD_SHIFT:
                name = name.upper()
            return name
        # Some pygame versions report single keys as "[a]".
        if len(name) == 3 and name[0] == "[" and name[2] == "]":
            return name[1]
        return ""

    def handle_key(self, event: pygame.event.Event) -> bool:
        """Process a KEYDOWN event. Returns True if the event was consumed.

        Always consumes key events while focused so they never leak to global
        shortcuts. Returns False when not focused (caller should handle it).
        """
        if not self.active:
            return False
        if event.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
            return True
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
            self._set_active(False)
            return True
        ch = self._char_from_event(event)
        if ch and ch in self._valid and len(self.text) < self.max_len:
            self.text += ch
        return True

    def draw(self, screen: pygame.Surface, hint: str = "username") -> None:
        border = (120, 170, 220) if self.active else (90, 90, 90)
        pygame.draw.rect(screen, (60, 60, 60), self.rect, border_radius=3)
        pygame.draw.rect(screen, border, self.rect, 1, border_radius=3)
        text_y = self.rect.y + (self.rect.h - self.font.get_height()) // 2
        if self.text:
            surf = self.font.render(self.text, True, (235, 235, 235))
            screen.blit(surf, (self.rect.x + 6, text_y))
        else:
            hint_surf = self.font.render(hint, True, (110, 110, 110))
            screen.blit(hint_surf, (self.rect.x + 6, text_y))
        if self.active:
            text_w = self.font.size(self.text)[0]
            cursor_x = self.rect.x + 6 + text_w
            pygame.draw.rect(screen, (235, 235, 235),
                             (cursor_x, self.rect.y + 4, 2, self.rect.h - 8))
