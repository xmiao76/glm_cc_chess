"""A minimal single-line text input field for the pygame GUI.

Kept in its own module so :mod:`src.gui` stays focused on layout and game
state. The field is click-to-focus, validates characters (Lichess usernames are
letters/digits/``_``/``-``), and consumes key events while focused so global
shortcuts (e.g. ``N`` for new game) do not fire while the user is typing.

Keyboard robustness
-------------------
Character input has two complementary paths so typing works on every setup:

* ``KEYDOWN.unicode`` -- the typed character for an ordinary (non-IME)
  keypress. :meth:`handle_key` inserts it directly when it is present and valid.
* ``TEXTINPUT`` events (``event.text``) -- the OS-composed text, which is what
  an IME delivers and the only correct source for shifted symbols such as
  ``_`` (``pygame.key.name(K_MINUS)`` is the unshifted ``-``). The GUI routes
  these to :meth:`insert_text` via its TEXTINPUT handler.

To avoid double-inserting when BOTH fire for the same keypress (the normal
non-IME case), ``handle_key`` sets ``_keydown_handled_char`` when it inserts
a char (from ``unicode`` OR from the fallback) and the TEXTINPUT handler skips
a matching event. When ``KEYDOWN.unicode`` is empty (IME/composing) the
TEXTINPUT event normally supplies the character -- but some IMEs skip
TEXTINPUT for shifted keys (uppercase letters, ``_``) or for every key, so
``handle_key`` ALSO has a KEYDOWN fallback (:meth:`_fallback_char`) that
derives letters (with Shift/CapsLock), digits, and shifted symbols directly
from the key. Without the letter fallback every letter in a ``lip_BXs...``
token was dropped and the mangled token sent to Lichess -> HTTP 401 "No such
token" -> "profile fetch failed".

Focusing the field enables pygame's text-input mode so the OS delivers
``TEXTINPUT`` reliably; ``KEYDOWN`` is consumed while focused so global
shortcuts (e.g. ``N`` for new game) do not fire while the user is typing.
"""

from __future__ import annotations

import pygame

# Bumped every time the token-capture path changes. The GUI logs this on entering
# Lichess mode so a copied activity log shows EXACTLY which capture code the
# running binary has -- ruling out a stale EXE when a 401 keeps recurring.
CAPTURE_VERSION = "v4-letter-fallback"

# Characters allowed in a Lichess username.
_VALID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_MAX_LEN = 24

# Shifted-symbol keys -> the char Shift produces on a US layout. Used by the
# KEYDOWN fallback when ``unicode`` is empty: some IMEs fire TEXTINPUT for
# letters and digits but NOT for shifted symbols such as the underscore
# (Shift+hyphen), so a TEXTINPUT-only path would drop ``_`` and mangle
# ``lip_...`` tokens -> Lichess HTTP 401 "No such token". These keys are never
# IME-composition candidates, so deriving the char from the key is safe.
_SHIFTED_SYMBOLS = {
    pygame.K_MINUS: "_", pygame.K_EQUALS: "+", pygame.K_LEFTBRACKET: "{",
    pygame.K_RIGHTBRACKET: "}", pygame.K_BACKSLASH: "|", pygame.K_SEMICOLON: ":",
    pygame.K_QUOTE: '"', pygame.K_COMMA: "<", pygame.K_PERIOD: ">",
    pygame.K_SLASH: "?", pygame.K_BACKQUOTE: "~",
    pygame.K_0: ")", pygame.K_1: "!", pygame.K_2: "@", pygame.K_3: "#",
    pygame.K_4: "$", pygame.K_5: "%", pygame.K_6: "^", pygame.K_7: "&",
    pygame.K_8: "*", pygame.K_9: "(",
}
# Unshifted digit keys -> the digit char. Same fallback rationale; digits are
# never composed by an IME.
_DIGIT_KEYS = {
    pygame.K_0: "0", pygame.K_1: "1", pygame.K_2: "2", pygame.K_3: "3",
    pygame.K_4: "4", pygame.K_5: "5", pygame.K_6: "6", pygame.K_7: "7",
    pygame.K_8: "8", pygame.K_9: "9",
}
# Letter keys (a-z) -> the lowercase char. Used by the KEYDOWN fallback when
# ``unicode`` is empty: some IMEs fire TEXTINPUT for unshifted keys but NOT for
# Shift+letter (uppercase), and some skip TEXTINPUT for every key. Without a
# letter fallback every letter in a ``lip_BXs...`` token was dropped and the
# mangled token sent to Lichess -> HTTP 401 "No such token" -> "profile fetch
# failed". Letters are safe to derive from the key here: the token/username
# fields are ASCII-only (``valid_chars`` filters the rest) and the
# ``_keydown_handled_char`` dedup prevents a double-insert when TEXTINPUT fires.
_LETTER_KEYS = {getattr(pygame, "K_" + chr(c)): chr(c)
                for c in range(ord("a"), ord("z") + 1)}


class TextInput:
    """A focused, single-line text input backed by a fixed :class:`pygame.Rect`."""

    def __init__(self, font: pygame.font.Font, rect,
                 max_len: int = _MAX_LEN,
                 valid_chars: set[str] | None = None,
                 mask: bool = False) -> None:
        self.font = font
        self.rect = pygame.Rect(rect)
        self.text: str = ""
        self.active: bool = False
        self.max_len = max_len
        self._valid = valid_chars if valid_chars is not None else _VALID_CHARS
        # When True the field renders one '*' per character (a password-style
        # mask) so a secret like the Lichess BOT token is never visible on
        # screen or in a screen recording. The raw text is still stored plainly
        # for submission; only the display is masked. '*' is used (not '•')
        # because the bundled default font lacks U+2022 and renders it as tofu.
        self.mask = mask
        # Set True by handle_key when it inserted the char -- from KEYDOWN.unicode
        # (non-IME) OR from the empty-unicode fallback (letters/digits/symbols).
        # The GUI's TEXTINPUT handler reads+resets it to avoid double-inserting
        # the same keypress. False => neither path inserted a char (an invalid
        # char, or a composing key the fallback does not map) and the TEXTINPUT
        # event, if any, must supply it.
        self._keydown_handled_char: bool = False

    def set(self, text: str) -> None:
        """Replace the field contents (used to prefill from env/config)."""
        self.text = (text or "")[: self.max_len]

    def value(self) -> str:
        return self.text.strip()

    def display_text(self) -> str:
        """The text as rendered on screen — masked when ``mask`` is set."""
        return "*" * len(self.text) if self.mask else self.text

    def insert_text(self, text: str) -> None:
        """Insert paste/clipboard text, filtered to valid chars and max_len.

        Used by the GUI's Ctrl+V paste so a token copied with trailing
        whitespace or a newline keeps only the legal characters (a stray char
        would otherwise corrupt the token sent to Lichess).
        """
        for ch in (text or ""):
            if ch in self._valid and len(self.text) < self.max_len:
                self.text += ch

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
    def _fallback_char(event: pygame.event.Event) -> str | None:
        """Char for an empty-unicode KEYDOWN, for non-composing keys only.

        Letters are derived from the key with Shift/CapsLock (XOR) so they are
        captured even when the IME skips TEXTINPUT for them -- this is the cause
        of the recurring "Profile Fetch Error": the uppercase letters in a
        ``lip_BXs...`` token were dropped and the mangled token sent to Lichess
        -> HTTP 401 "No such token". Shifted symbols (``K_MINUS`` + Shift ->
        ``_``) and digits are likewise captured. Returns ``None`` for anything
        else (the TEXTINPUT event, if any, must then supply the char).
        """
        mods = getattr(event, "mod", 0) or 0
        letter = _LETTER_KEYS.get(event.key)
        if letter is not None:
            shift = bool(mods & pygame.KMOD_SHIFT)
            caps = bool(mods & pygame.KMOD_CAPS)
            return letter.upper() if (shift != caps) else letter
        if mods & pygame.KMOD_SHIFT:
            return _SHIFTED_SYMBOLS.get(event.key)
        return _DIGIT_KEYS.get(event.key)

    def handle_key(self, event: pygame.event.Event) -> bool:
        """Process a KEYDOWN event. Returns True if the event was consumed.

        Two character-entry paths, chosen by whether ``event.unicode`` is
        populated:

        * Populated (non-IME): insert it when valid and set
          ``_keydown_handled_char = True`` so the GUI's TEXTINPUT handler skips
          the matching event (dedup). A populated-but-invalid char (e.g. ``!``
          on the 1 key) is rejected -- it is NOT substituted by a key-based
          fallback.
        * Empty (IME/composing): some IMEs skip TEXTINPUT for shifted keys
          (uppercase letters, ``_``) or for every key, so a TEXTINPUT-only path
          would drop letters and mangle ``lip_BXs...`` tokens (-> HTTP 401).
          Non-composing keys -- letters (with Shift/CapsLock), digits, shifted
          symbols -- are captured here from the key + modifier state via
          :meth:`_fallback_char`; the dedup flag suppresses a double-insert when
          TEXTINPUT also fires.

        Always consumes key events while focused so they never leak to global
        shortcuts; returns False when not focused (caller should handle it).
        """
        if not self.active:
            return False
        if event.key == pygame.K_BACKSPACE:
            self.text = self.text[:-1]
            self._keydown_handled_char = False
            return True
        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_ESCAPE):
            self._set_active(False)
            self._keydown_handled_char = False
            return True
        ch = getattr(event, "unicode", "") or ""
        if ch:
            # Populated unicode: insert if valid; never substitute a fallback
            # for a populated-but-invalid char ("!" must be rejected, not "1").
            if ch in self._valid and len(self.text) < self.max_len:
                self.text += ch
                self._keydown_handled_char = True   # TEXTINPUT handler skips
            else:
                self._keydown_handled_char = False
        else:
            # Empty unicode (IME): capture non-composing keys (shifted symbols
            # + digits) now; let TEXTINPUT supply letters and anything else.
            fb = self._fallback_char(event)
            if fb is not None and fb in self._valid and len(self.text) < self.max_len:
                self.text += fb
                self._keydown_handled_char = True   # a later TEXTINPUT must skip
            else:
                self._keydown_handled_char = False
        return True

    def draw(self, screen: pygame.Surface, hint: str = "username") -> None:
        border = (120, 170, 220) if self.active else (90, 90, 90)
        pygame.draw.rect(screen, (60, 60, 60), self.rect, border_radius=3)
        pygame.draw.rect(screen, border, self.rect, 1, border_radius=3)
        text_y = self.rect.y + (self.rect.h - self.font.get_height()) // 2
        shown = self.display_text()
        if shown:
            surf = self.font.render(shown, True, (235, 235, 235))
            screen.blit(surf, (self.rect.x + 6, text_y))
        else:
            hint_surf = self.font.render(hint, True, (110, 110, 110))
            screen.blit(hint_surf, (self.rect.x + 6, text_y))
        if self.active:
            text_w = self.font.size(shown)[0]
            cursor_x = self.rect.x + 6 + text_w
            pygame.draw.rect(screen, (235, 235, 235),
                             (cursor_x, self.rect.y + 4, 2, self.rect.h - 8))
