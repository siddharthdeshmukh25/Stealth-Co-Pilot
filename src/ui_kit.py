"""Canvas-drawn UI kit: premium light-first SaaS dashboard, with a dark twin.

Tkinter's stock widgets cannot be themed beyond flat colours, so every control
that needs to look modern (rounded cards, hover-animated buttons, meters,
segmented switches, toasts, the loading spinner) is drawn on a ``tk.Canvas``
here. Keeping it in one module means ``main.py`` stays about the interview
flow, not about pixels.

Colours are module-level names (``TEXT``, ``PANEL``, ``ACCENT``...) that
``apply_theme()`` swaps in one go; widgets read them when they draw, so the app
rebuilds itself once on a theme change and everything follows.
"""
from __future__ import annotations

import math
import os
import tkinter as tk
from tkinter import font as tkfont

from paths import resource_path

# --------------------------------------------------------------- palette
# Two token sets, one shape. Light is the default; ``apply_theme`` rebinds these
# names, and everything drawn afterwards picks the new values up.
THEMES: dict[str, dict[str, str]] = {
    "light": {
        "BG": "#FFFFFF",          # page
        "BG_SOFT": "#FBFBFC",     # sidebar rail
        "PANEL": "#FFFFFF",       # cards
        "PANEL_HIGH": "#F6F7F8",  # pressed / active segment
        "PANEL_HOVER": "#EFF1F3",
        "FIELD": "#FCFCFD",
        "LINE": "#E5E7EB",        # the 1px hairline
        "LINE_SOFT": "#F0F1F3",
        "TEXT": "#0D0F12",
        "TEXT_DIM": "#565D66",
        "MUTED": "#8B9199",
        "ACCENT": "#C4F53C",      # neon-lime fill
        "ACCENT_BRIGHT": "#D6FF63",
        "ACCENT_INK": "#161B0A",  # text on lime
        "ACCENT_TEXT": "#4C7A05",  # lime readable on white
        "ACCENT_SOFT": "#F4FCDD",
        "INFO": "#2563EB",
        "INFO_SOFT": "#EFF5FF",
        "WARN": "#B45309",
        "WARN_SOFT": "#FFF8E8",
        "DANGER": "#DC2626",
        "DANGER_SOFT": "#FEF2F2",
        "SELECT": "#EAF7CE",
        "SELECT_FG": "#0D0F12",
        "BLUE": "#2563EB",
        "BLUE_SOFT": "#BFDBFE",
    },
    "dark": {
        "BG": "#0A0B0D",
        "BG_SOFT": "#0C0E11",
        "PANEL": "#111318",
        "PANEL_HIGH": "#171A20",
        "PANEL_HOVER": "#1E222A",
        "FIELD": "#0E1014",
        "LINE": "#232830",
        "LINE_SOFT": "#1A1D23",
        "TEXT": "#F4F6F8",
        "TEXT_DIM": "#A0A8B2",
        "MUTED": "#6E7681",
        "ACCENT": "#C4F53C",
        "ACCENT_BRIGHT": "#D6FF63",
        "ACCENT_INK": "#141A06",
        "ACCENT_TEXT": "#CBF75F",
        "ACCENT_SOFT": "#1C2410",
        "INFO": "#7CB2FF",
        "INFO_SOFT": "#132032",
        "WARN": "#F5C451",
        "WARN_SOFT": "#2A2110",
        "DANGER": "#F87171",
        "DANGER_SOFT": "#2C1418",
        "SELECT": "#26320F",
        "SELECT_FG": "#F4F6F8",
        "BLUE": "#60A5FA",
        "BLUE_SOFT": "#93C5FD",
    },
}

_THEME = "light"


def theme_name() -> str:
    return _THEME


def is_dark() -> bool:
    return _THEME == "dark"


def apply_theme(name: str) -> str:
    """Swap the token set. Widgets already on screen keep their colours, so the
    caller rebuilds the window afterwards (``main`` re-renders the same step)."""
    global _THEME
    name = name if name in THEMES else "light"
    _THEME = name
    globals().update(THEMES[name])
    return name


globals().update(THEMES[_THEME])

_SCALE = 1.0
_fonts: dict[tuple, tkfont.Font] = {}
_family: str | None = None
_mono: str | None = None


def set_scale(value: float) -> None:
    global _SCALE
    _SCALE = max(0.85, min(2.5, value))


def scale() -> float:
    return _SCALE


def S(px: float) -> int:
    """Design pixel -> device pixel, so the layout holds at any Windows scaling."""
    return max(1, int(round(px * _SCALE)))


def enable_dpi_awareness() -> float:
    """Crisp text on scaled displays; returns the display scale (1.0 = 96 DPI)."""
    if os.name != "nt":
        return 1.0
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:  # noqa: BLE001 - older Windows builds
            ctypes.windll.user32.SetProcessDPIAware()
        try:
            return max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
        except Exception:  # noqa: BLE001
            return 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def _pick_family() -> str:
    global _family
    if _family is None:
        available = set(tkfont.families())
        for name in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", "Arial"):
            if name in available:
                _family = name
                break
        else:
            _family = "TkDefaultFont"
    return _family


def _pick_mono() -> str:
    global _mono
    if _mono is None:
        available = set(tkfont.families())
        for name in ("Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New"):
            if name in available:
                _mono = name
                break
        else:
            _mono = "TkFixedFont"
    return _mono


POPPINS_FILES = ("Poppins-Regular.ttf", "Poppins-Medium.ttf", "Poppins-SemiBold.ttf", "Poppins-Bold.ttf")
_role_families: dict[str, tuple[str, str]] | None = None


def load_fonts() -> int:
    """Register the bundled Poppins weights for this process only.

    Windows loads them without installing anything, but Tk only sees them if
    they are registered before the first ``tk.Tk()`` - so this runs at import.
    """
    if os.name != "nt":
        return 0
    try:
        import ctypes

        folder = resource_path("assets/fonts")
    except Exception:  # noqa: BLE001 - a missing folder just means fallback fonts
        return 0
    added = 0
    for name in POPPINS_FILES:
        path = folder / name
        if not path.exists():
            continue
        try:
            added += ctypes.windll.gdi32.AddFontResourceExW(str(path), 0, 0)
        except Exception:  # noqa: BLE001
            pass
    return added


def _role_map() -> dict[str, tuple[str, str]]:
    """weight keyword -> (family, tk weight). Poppins ships Medium/SemiBold as
    their own families on Windows, which is how the design gets 4 real weights."""
    global _role_families
    if _role_families is None:
        available = set(tkfont.families())
        if "Poppins" in available:
            _role_families = {
                "normal": ("Poppins", "normal"),
                "medium": ("Poppins Medium" if "Poppins Medium" in available else "Poppins", "normal"),
                "semibold": ("Poppins SemiBold" if "Poppins SemiBold" in available else "Poppins", "bold"),
                "bold": ("Poppins", "bold"),
            }
        else:
            base = _pick_family()
            _role_families = {role: (base, "normal" if role == "normal" else "bold")
                              for role in ("normal", "medium", "semibold", "bold")}
    return _role_families


def font(size: float = 13, weight: str = "normal", mono: bool = False, slant: str = "roman") -> tkfont.Font:
    """Pixel-sized font (negative Tk size) so DPI scaling stays predictable."""
    key = (size, weight, mono, slant)
    cached = _fonts.get(key)
    if cached is None:
        if mono:
            family, tk_weight = _pick_mono(), "normal"
        else:
            family, tk_weight = _role_map().get(weight, _role_map()["normal"])
        cached = tkfont.Font(family=family, size=-S(size), weight=tk_weight, slant=slant)
        _fonts[key] = cached
    return cached


load_fonts()


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def mix(first: str, second: str, amount: float) -> str:
    """Blend two hex colours; ``amount`` 0 -> first, 1 -> second."""
    amount = max(0.0, min(1.0, amount))
    a, b = _rgb(first), _rgb(second)
    return "#" + "".join(f"{round(x + (y - x) * amount):02X}" for x, y in zip(a, b))


def _parent_bg(parent) -> str:
    try:
        return parent.cget("bg")
    except Exception:  # noqa: BLE001 - ttk parents have no bg option
        return BG


def _alive(widget) -> bool:
    """winfo_exists() raises once the interpreter is gone (window closing)."""
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, fill: str = "", outline: str = "",
               width: int = 1, tags=None, **kwargs):
    """Rounded rectangle: smoothed fill plus a crisp stroked path.

    Tk has no native rounded rect, and stroking a smoothed polygon leaves
    visible seams at the corners (invisible on dark, obvious on white). So the
    fill is one polygon and the 1px hairline is four arcs plus four lines.
    """
    radius = max(1, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1 + radius, x1, y1,
        x1 + radius, y1,
    ]
    kwargs.setdefault("splinesteps", 24)
    extra = {"tags": tags} if tags else {}
    first = None
    if fill:
        first = canvas.create_polygon(points, smooth=True, fill=fill, outline="", **kwargs, **extra)
    if outline and width:
        # Snap the stroke to whole pixels: a bottom line at y = height - 0.5
        # lands on the row past the canvas edge and disappears, which is what
        # made card, field and button bottoms look cut off.
        ox1, oy1 = int(round(x1)), int(round(y1))
        ox2, oy2 = int(round(x2)) - 1, int(round(y2)) - 1
        d = radius * 2
        arcs = (
            (ox1, oy1, ox1 + d, oy1 + d, 90),
            (ox2 - d, oy1, ox2, oy1 + d, 0),
            (ox2 - d, oy2 - d, ox2, oy2, 270),
            (ox1, oy2 - d, ox1 + d, oy2, 180),
        )
        for ax1, ay1, ax2, ay2, start in arcs:
            item = canvas.create_arc(ax1, ay1, ax2, ay2, start=start, extent=90,
                                     style="arc", outline=outline, width=width, **extra)
            if first is None:
                first = item
        edges = (
            (ox1 + radius, oy1, ox2 - radius, oy1),
            (ox2, oy1 + radius, ox2, oy2 - radius),
            (ox1 + radius, oy2, ox2 - radius, oy2),
            (ox1, oy1 + radius, ox1, oy2 - radius),
        )
        for ex1, ey1, ex2, ey2 in edges:
            item = canvas.create_line(ex1, ey1, ex2, ey2, fill=outline, width=width, **extra)
            if first is None:
                first = item
    return first


def label(parent, text: str = "", size: float = 13, color: str | None = None, weight: str = "normal",
          bg: str | None = None, mono: bool = False, **kwargs) -> tk.Label:
    return tk.Label(parent, text=text, bg=bg or _parent_bg(parent), fg=color or TEXT,
                    font=font(size, weight, mono=mono), **kwargs)


class Surface(tk.Canvas):
    """Canvas that redraws itself on resize and matches its parent background."""

    def __init__(self, parent, height: int = 0, width: int = 0, bg: str | None = None, **kwargs):
        super().__init__(parent, bg=bg or _parent_bg(parent), highlightthickness=0, bd=0, **kwargs)
        if height:
            self.configure(height=S(height))
        if width:
            self.configure(width=S(width))
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, _event=None):
        self.redraw()

    def redraw(self) -> None:  # pragma: no cover - overridden
        pass


class Button(Surface):
    """Rounded button with animated hover/press states and an optional glyph."""

    def _variant_colors(self) -> tuple[str | None, str, str, str]:
        """Resolved lazily so a theme swap is picked up by the next redraw."""
        if self.variant == "primary":
            return ACCENT, ACCENT_BRIGHT, ACCENT_INK, ACCENT
        if self.variant == "secondary":
            return PANEL, PANEL_HIGH, TEXT, LINE
        if self.variant == "ghost":
            return None, PANEL_HIGH, TEXT_DIM, ""
        if self.variant == "danger":
            return DANGER_SOFT, mix(DANGER_SOFT, DANGER, 0.10), DANGER, mix(LINE, DANGER, 0.22)
        return ACCENT_SOFT, mix(ACCENT_SOFT, ACCENT, 0.16), ACCENT_TEXT, mix(LINE, ACCENT, 0.30)

    def __init__(self, parent, text: str = "", command=None, variant: str = "primary",
                 icon: str = "", height: int = 40, radius: int = 10, size: float = 14,
                 weight: str = "semibold", width: int | None = None, padx: int = 18,
                 bg: str | None = None, align: str = "center"):
        super().__init__(parent, height=height, width=width or 0, bg=bg or _parent_bg(parent))
        self.text = text
        self.icon = icon
        self.command = command
        self.variant = variant
        self.radius = S(radius)
        self.font = font(size, weight)
        self.padx = S(padx)
        self.align = align
        self.enabled = True
        self._hover = 0.0
        self._pressed = False
        self._anim_job = None
        self.configure(cursor="hand2")
        if width is None:
            self.configure(width=self._natural_width())
        self.bind("<Enter>", lambda _e: self._animate_to(1.0))
        self.bind("<Leave>", lambda _e: (setattr(self, "_pressed", False), self._animate_to(0.0)))
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.redraw()

    # ---------------------------------------------------------------- api
    def _caption(self) -> str:
        # An icon with no text is an icon-only button: no ghost padding around it.
        if self.icon and not self.text:
            return self.icon
        return f"{self.icon}  {self.text}" if self.icon else self.text

    def _natural_width(self) -> int:
        return self.font.measure(self._caption()) + self.padx * 2 + S(6)

    def set_text(self, text: str) -> None:
        self.text = text
        self.redraw()

    def set_icon(self, icon: str) -> None:
        self.icon = icon
        self.redraw()

    def set_command(self, command) -> None:
        self.command = command

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        if not enabled:
            self._hover = 0.0
        self.redraw()

    def flash(self) -> None:
        """Brief accent pulse, used to draw the eye to the next action."""
        self._animate_to(1.0)
        self.after(180, lambda: self._animate_to(0.0))

    # ------------------------------------------------------------ drawing
    def _colors(self):
        fill, hover_fill, fg, outline = self._variant_colors()
        if not self.enabled:
            return PANEL, PANEL, MUTED, LINE
        fill = fill or _parent_bg(self.master)
        blend = mix(fill, hover_fill, self._hover)
        if self._pressed and self.enabled:
            blend = mix(blend, "#000000", 0.10 if not is_dark() else 0.18)
        return blend, hover_fill, fg, outline

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("all")
        width = max(self.winfo_width(), S(24))
        height = max(self.winfo_height(), S(16))
        fill, _hover, fg, outline = self._colors()
        round_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self.radius,
                   fill=fill, outline=outline or fill, width=1)
        self.create_text(width / 2, height / 2 + (S(1) if self._pressed else 0), text=self._caption(),
                         fill=fg, font=self.font)

    # --------------------------------------------------------- animations
    def _animate_to(self, target: float) -> None:
        if self._anim_job:
            try:
                self.after_cancel(self._anim_job)
            except Exception:  # noqa: BLE001
                pass
            self._anim_job = None
        start = self._hover

        def step(frame: int = 0) -> None:
            if not _alive(self):
                return
            progress = min(1.0, frame / 5)
            eased = 1 - (1 - progress) ** 2
            self._hover = start + (target - start) * eased
            self.redraw()
            if progress < 1.0:
                self._anim_job = self.after(14, lambda: step(frame + 1))
            else:
                self._anim_job = None

        step()

    def _on_press(self, _event=None) -> None:
        if not self.enabled:
            return
        self._pressed = True
        self.redraw()

    def _on_release(self, _event=None) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self.redraw()
        if was_pressed and self.enabled and self.command:
            self.command()


class Segmented(Surface):
    """Two-or-more option switch; replaces the stock Windows combobox."""

    def __init__(self, parent, options: tuple[str, ...], value: str | None = None,
                 command=None, height: int = 32, size: float = 12, pad: int = 3,
                 bg: str | None = None):
        self.options = tuple(options)
        self._value = value or self.options[0]
        self.command = command
        self.size = size
        self.pad = S(pad)
        self.font = font(size, "medium")
        self.seg_widths = [self.font.measure(option) + S(22) for option in self.options]
        super().__init__(parent, height=height, width=sum(self.seg_widths) + self.pad * 2,
                         bg=bg or _parent_bg(parent))
        self.height = S(height)
        self._hover_index = -1
        self.configure(cursor="hand2")
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda _e: self._set_hover(-1))
        self.bind("<Button-1>", self._on_click)
        self.redraw()

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        if value in self.options:
            self._value = value
            self.redraw()

    def _segment_at(self, x: int) -> int:
        edge = self.pad
        for index, width in enumerate(self.seg_widths):
            if edge <= x <= edge + width:
                return index
            edge += width
        return -1

    def _set_hover(self, index: int) -> None:
        if index != self._hover_index:
            self._hover_index = index
            self.redraw()

    def _on_motion(self, event) -> None:
        self._set_hover(self._segment_at(event.x))

    def _on_click(self, event) -> None:
        index = self._segment_at(event.x)
        if index < 0:
            return
        chosen = self.options[index]
        if chosen == self._value:
            return
        self._value = chosen
        self.redraw()
        if self.command:
            self.command(chosen)

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("all")
        width = max(self.winfo_width(), sum(self.seg_widths) + self.pad * 2)
        height = max(self.winfo_height(), self.height)
        round_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, S(9),
                   fill=FIELD, outline=LINE, width=1)
        edge = self.pad
        for index, option in enumerate(self.options):
            seg_w = self.seg_widths[index]
            active = option == self._value
            if active:
                round_rect(self, edge, self.pad, edge + seg_w, height - self.pad, S(7),
                           fill=PANEL_HIGH, outline=LINE, width=1)
            elif index == self._hover_index:
                round_rect(self, edge, self.pad, edge + seg_w, height - self.pad, S(7),
                           fill=mix(FIELD, PANEL_HIGH, 0.6), outline="", width=0)
            color = TEXT if active else TEXT_DIM
            self.create_text(edge + seg_w / 2, height / 2, text=option, fill=color,
                             font=self.font)
            edge += seg_w


class Card(Surface):
    """Rounded panel; children go into ``card.body``."""

    def __init__(self, parent, pad: int = 18, radius: int = 14, fill: str | None = None,
                 outline: str | None = None, autosize: bool = True, min_height: int = 0,
                 stretch: bool = False, bg: str | None = None):
        # Defaults are resolved here, never in the signature: a signature default
        # would freeze the palette of whichever theme was active at import.
        fill = fill or PANEL
        outline = outline or LINE
        super().__init__(parent, bg=bg or _parent_bg(parent))
        self.pad = S(pad)
        self.radius = S(radius)
        self.fill = fill
        self.outline = outline
        self.autosize = autosize
        self.stretch = stretch
        self.min_height = S(min_height)
        if min_height:
            self.configure(height=self.min_height)
        self.body = tk.Frame(self, bg=fill)
        self._window = self.create_window(self.pad, self.pad, anchor="nw", window=self.body)
        if autosize:
            self.body.bind("<Configure>", self._autosize)
        self.redraw()

    def _autosize(self, _event=None) -> None:
        wanted = max(self.body.winfo_reqheight() + self.pad * 2, self.min_height)
        if abs(wanted - self.winfo_height()) > 1:
            self.configure(height=wanted)

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("outline")
        width = max(self.winfo_width(), S(40))
        height = max(self.winfo_height(), S(40))
        round_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self.radius,
                   fill=self.fill, outline=self.outline, width=1, tags="outline")
        self.tag_lower("outline")
        self.coords(self._window, self.pad, self.pad)
        self.itemconfigure(self._window, width=max(width - self.pad * 2, S(40)))
        if self.stretch:
            self.itemconfigure(self._window, height=max(height - self.pad * 2, S(40)))


class Entry(Surface):
    """Rounded single-line input with a focus glow and a placeholder."""

    def __init__(self, parent, placeholder: str = "", initial: str = "", height: int = 42,
                 radius: int = 10, size: float = 13, bg: str | None = None):
        super().__init__(parent, height=height, bg=bg or _parent_bg(parent))
        self.radius = S(radius)
        self.placeholder = placeholder
        self._focused = False
        self.height = S(height)
        self.var = tk.StringVar(value=initial)
        self.entry = tk.Entry(self, textvariable=self.var, bg=FIELD, fg=TEXT,
                              insertbackground=ACCENT_TEXT, relief="flat", bd=0,
                              font=font(size), highlightthickness=0,
                              selectbackground=SELECT, selectforeground=SELECT_FG)
        self._window = self.create_window(S(14), self.height / 2, anchor="w", window=self.entry)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self._show_placeholder()
        self.redraw()

    def _show_placeholder(self) -> None:
        if self.placeholder and not self.var.get():
            self.entry.configure(fg=MUTED)
            self.var.set(self.placeholder)
            self._placeholder_active = True
        else:
            self._placeholder_active = False

    def _on_focus_in(self, _event=None) -> None:
        self._focused = True
        if getattr(self, "_placeholder_active", False):
            self.var.set("")
            self.entry.configure(fg=TEXT)
            self._placeholder_active = False
        self.redraw()

    def _on_focus_out(self, _event=None) -> None:
        self._focused = False
        self._show_placeholder()
        self.redraw()

    def get(self) -> str:
        return "" if getattr(self, "_placeholder_active", False) else self.var.get().strip()

    def set(self, text: str) -> None:
        self._placeholder_active = False
        self.entry.configure(fg=TEXT)
        self.var.set(text)
        self.redraw()

    def focus(self) -> None:
        self.entry.focus_set()

    def redraw(self) -> None:
        if not _alive(self):
            return
        # Only the background is redrawn: deleting "all" would remove the
        # embedded tk.Entry window item and the field would render as a blank box.
        self.delete("bg")
        width = max(self.winfo_width(), S(60))
        height = max(self.winfo_height(), self.height)
        outline = ACCENT_TEXT if self._focused else LINE
        round_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self.radius,
                   fill=FIELD, outline=outline, width=1, tags="bg")
        self.tag_lower("bg")
        self.coords(self._window, S(14), height / 2)
        self.itemconfigure(self._window, width=max(width - S(28), S(40)))


class TextArea(Card):
    """Multi-line input inside a rounded card, with an optional live counter."""

    def __init__(self, parent, placeholder: str = "", initial: str = "", height: int = 200,
                 fill: str | None = None, outline: str | None = None, size: float = 13,
                 max_length: int | None = None):
        fill = fill or FIELD
        super().__init__(parent, pad=10, radius=12, fill=fill, outline=outline, autosize=False)
        self.configure(height=S(height))
        self.text = tk.Text(self.body, bg=fill, fg=TEXT, insertbackground=ACCENT, relief="flat",
                            bd=0, highlightthickness=0, wrap="word", font=font(size),
                            padx=S(8), pady=S(6), spacing1=S(2), spacing3=S(4),
                            selectbackground=SELECT, selectforeground=SELECT_FG,
                            tabs=())
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", initial)
        self._focused = False
        self.text.bind("<FocusIn>", self._on_focus)
        self.text.bind("<FocusOut>", self._on_blur)
        self.max_length = max_length
        if max_length:
            self.text.bind("<<Modified>>", self._clip_to_limit)
        self.redraw()

    def _clip_to_limit(self, _event=None) -> None:
        """Typing or pasting past ``max_length`` is trimmed instead of accepted."""
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if not self.max_length:
            return
        if len(self.text.get("1.0", "end-1c")) > self.max_length:
            self.text.delete(f"1.0 + {self.max_length} chars", "end")

    def _on_focus(self, _event=None) -> None:
        self._focused = True
        self.redraw()

    def _on_blur(self, _event=None) -> None:
        self._focused = False
        self.redraw()

    def get(self) -> str:
        return self.text.get("1.0", "end").strip()

    def set(self, value: str) -> None:
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)

    def focus(self) -> None:
        self.text.focus_set()

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("outline")
        width = max(self.winfo_width(), S(40))
        height = max(self.winfo_height(), S(40))
        outline = ACCENT_TEXT if getattr(self, "_focused", False) else self.outline
        round_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, self.radius,
                   fill=self.fill, outline=outline, width=1, tags="outline")
        self.tag_lower("outline")
        self.coords(self._window, self.pad, self.pad)
        self.itemconfigure(self._window, width=max(width - self.pad * 2, S(40)),
                           height=max(height - self.pad * 2, S(40)))


class NavItem(Surface):
    """Sidebar step row: badge, title, caption, animated highlight."""

    def __init__(self, parent, index: int, title: str, caption: str, height: int = 58,
                 command=None, bg: str | None = None, glyph: str = ""):
        super().__init__(parent, height=height, bg=bg or _parent_bg(parent))
        self.index = index
        self.title = title
        self.caption = caption
        self.glyph = glyph
        self.command = command
        self.state = "upcoming"  # upcoming | reached | active
        self._hover = 0.0
        self._anim_job = None
        self.configure(cursor="hand2")
        self.bind("<Enter>", lambda _e: self._animate(1.0))
        self.bind("<Leave>", lambda _e: self._animate(0.0))
        self.bind("<Button-1>", lambda _e: self.command and self.command(self.index))
        self.redraw()

    def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.redraw()

    def _animate(self, target: float) -> None:
        if self._anim_job:
            try:
                self.after_cancel(self._anim_job)
            except Exception:  # noqa: BLE001
                pass
        start = self._hover

        def step(frame: int = 0) -> None:
            if not _alive(self):
                return
            progress = min(1.0, frame / 4)
            eased = 1 - (1 - progress) ** 2
            self._hover = start + (target - start) * eased
            self.redraw()
            if progress < 1.0:
                self._anim_job = self.after(16, lambda: step(frame + 1))
            else:
                self._anim_job = None

        step()

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("all")
        width = max(self.winfo_width(), S(120))
        height = max(self.winfo_height(), S(40))
        active = self.state == "active"
        reached = self.state in {"active", "reached"}
        if active:
            fill = PANEL_HIGH
            outline = LINE
        elif reached:
            fill = mix(_parent_bg(self), PANEL, 0.55 + 0.35 * self._hover)
            outline = ""
        else:
            fill = mix(_parent_bg(self), PANEL, 0.35 * self._hover)
            outline = ""
        round_rect(self, 1, 1, width - 1, height - 1, S(10), fill=fill,
                   outline=outline or fill, width=1)
        if active:
            round_rect(self, 0, S(14), S(3), height - S(14), S(2), fill=ACCENT, outline="")
        badge_x = S(16)
        badge_size = S(26)
        badge_fill = ACCENT_SOFT if active else (PANEL_HIGH if reached else FIELD)
        badge_outline = mix(LINE, ACCENT, 0.85) if active else (LINE if reached else LINE_SOFT)
        badge_fg = ACCENT_TEXT if active else (TEXT if reached else MUTED)
        round_rect(self, badge_x, height / 2 - badge_size / 2, badge_x + badge_size,
                   height / 2 + badge_size / 2, S(8), fill=badge_fill,
                   outline=badge_outline, width=1)
        glyph = "✓" if self.state == "reached" else (self.glyph or f"{self.index:02d}")
        self.create_text(badge_x + badge_size / 2, height / 2, text=glyph, fill=badge_fg,
                         font=font(10, "semibold"))
        text_x = badge_x + badge_size + S(12)
        self.create_text(text_x, height / 2 - S(9), text=self.title, anchor="w",
                         fill=TEXT if reached else TEXT_DIM, font=font(14, "semibold"))
        self.create_text(text_x, height / 2 + S(9), text=self.caption, anchor="w",
                         fill=MUTED if not active else TEXT_DIM, font=font(11))


class Pill(Surface):
    """Rounded status chip with a dot; ``pulse`` animates the dot."""

    def __init__(self, parent, text: str = "", color: str | None = None, height: int = 26,
                 size: float = 11, bg: str | None = None):
        super().__init__(parent, height=height, bg=bg or _parent_bg(parent))
        self.text = text
        self.color = color or MUTED
        self.size = size
        self.font = font(size, "semibold")
        self._pulse = 0.0
        self._pulse_dir = 1
        self._pulse_job = None
        self.pulsing = False
        self.configure(width=self.font.measure(text) + S(34))
        self.redraw()

    def set(self, text: str | None = None, color: str | None = None, pulsing: bool | None = None) -> None:
        if text is not None:
            self.text = text
            self.configure(width=self.font.measure(text) + S(34))
        if color is not None:
            self.color = color
        if pulsing is not None:
            self.pulsing = pulsing
            if pulsing and not self._pulse_job:
                self._start_pulse()
            elif not pulsing and self._pulse_job:
                self.after_cancel(self._pulse_job)
                self._pulse_job = None
                self._pulse = 0.0
        self.redraw()

    def _start_pulse(self) -> None:
        def tick() -> None:
            if not self.winfo_exists() or not self.pulsing:
                self._pulse_job = None
                return
            self._pulse += 0.08 * self._pulse_dir
            if self._pulse >= 1.0:
                self._pulse, self._pulse_dir = 1.0, -1
            elif self._pulse <= 0.0:
                self._pulse, self._pulse_dir = 0.0, 1
            self.redraw()
            # Scheduled on the toplevel: a chip destroyed mid-animation would
            # otherwise leave a timer bound to a deleted widget command.
            self._pulse_job = self.winfo_toplevel().after(60, tick)

        tick()

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("all")
        width = max(self.winfo_width(), S(50))
        height = max(self.winfo_height(), S(20))
        round_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, height / 2,
                   fill=mix(PANEL, self.color, 0.14), outline=mix(PANEL, self.color, 0.32), width=1)
        dot = mix(self.color, "#FFFFFF", 0.45 * self._pulse)
        self.create_oval(S(9), height / 2 - S(3), S(15), height / 2 + S(3),
                         fill=dot, outline="")
        self.create_text(S(22), height / 2, text=self.text, anchor="w",
                         fill=mix(self.color, TEXT, 0.45), font=self.font)


class Progress(Surface):
    """Thin progress track with an eased fill; used for the setup flow."""

    def __init__(self, parent, height: int = 4, color: str | None = None,
                 track: str | None = None, bg: str | None = None):
        super().__init__(parent, height=height, bg=bg or _parent_bg(parent))
        self.color = color or ACCENT
        self.track = track or LINE_SOFT
        self.height_px = S(height)
        self.value = 0.0
        self._target = 0.0
        self._job = None
        self.configure(height=self.height_px)
        self.redraw()

    def set(self, fraction: float, animate: bool = True) -> None:
        self._target = max(0.0, min(1.0, fraction))
        if not animate:
            self.value = self._target
            self.redraw()
            return
        if self._job:
            try:
                self.after_cancel(self._job)
            except Exception:  # noqa: BLE001
                pass
            self._job = None

        def step() -> None:
            if not _alive(self):
                return
            delta = self._target - self.value
            if abs(delta) < 0.004:
                self.value = self._target
                self.redraw()
                self._job = None
                return
            self.value += delta * 0.28
            self.redraw()
            self._job = self.after(16, step)

        step()

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("all")
        width = max(self.winfo_width(), S(40))
        height = self.height_px
        round_rect(self, 0, 0, width, height, height / 2, fill=self.track, outline="")
        filled = max(0.0, min(1.0, self.value)) * width
        if filled > 2:
            round_rect(self, 0, 0, filled, height, height / 2, fill=self.color, outline="")


class Meter(Surface):
    """Level meter used for the live speaker capture."""

    BARS = 34

    def __init__(self, parent, height: int = 34, bars: int | None = None,
                 bg: str | None = None, color: str | None = None):
        super().__init__(parent, height=height, bg=bg or _parent_bg(parent))
        self.bars = bars or self.BARS
        self.color = color or ACCENT
        self.level = 0.0
        self.target = 0.0
        self.active = False
        self._job = None
        self.height_px = S(height)
        self.configure(height=self.height_px)
        self._start()

    def set_level(self, level: float, active: bool | None = None) -> None:
        self.target = max(0.0, min(1.0, level))
        if active is not None:
            self.active = active

    def _start(self) -> None:
        def tick() -> None:
            if not _alive(self):
                self._job = None
                return
            target = self.target if self.active else 0.0
            # Rise fast, fall slow: reads like a real VU meter.
            rate = 0.45 if target > self.level else 0.12
            self.level += (target - self.level) * rate
            self.redraw()
            self._job = self.winfo_toplevel().after(40, tick)

        tick()

    def redraw(self) -> None:
        if not _alive(self):
            return
        self.delete("all")
        width = max(self.winfo_width(), S(60))
        height = max(self.winfo_height(), S(16))
        gap = S(3)
        bar_w = max(S(2), (width - gap * (self.bars - 1)) / self.bars)
        lit = int(round(self.level * self.bars))
        for index in range(self.bars):
            x1 = index * (bar_w + gap)
            # Centre-weighted envelope so idle bars still look like a waveform.
            envelope = 0.35 + 0.65 * (1 - abs(index - (self.bars - 1) / 2) / ((self.bars - 1) / 2))
            height_ratio = max(0.12, min(1.0, envelope * self.level)) if index < lit else 0.12
            bar_h = height * height_ratio
            if index < lit:
                color = mix(self.color, WARN, max(0.0, (index - self.bars * 0.72) / (self.bars * 0.28)))
                if index > self.bars * 0.88:
                    color = DANGER
            else:
                rest = LINE_SOFT if is_dark() else LINE
                color = rest if not self.active else mix(rest, self.color, 0.22)
            round_rect(self, x1, height / 2 - bar_h / 2, x1 + bar_w, height / 2 + bar_h / 2,
                       bar_w / 2, fill=color, outline="")


def show_toast(host: tk.Misc, message: str, kind: str = "info", duration: int = 2400) -> None:
    """Float a rounded notice just above the bottom edge, then fade it out."""
    palette = {"info": INFO, "success": ACCENT, "warn": WARN, "error": DANGER}
    color = palette.get(kind, INFO)
    text_font = font(12, "bold")
    max_width = S(520)
    usable = max_width - S(56)
    # Long messages used to run past the right edge, so wrap them and grow the
    # strip upwards instead of letting the text leak out of the window.
    lines: list[str] = []
    current = ""
    for word in message.split():
        candidate = f"{current} {word}".strip()
        if current and text_font.measure(candidate) > usable:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    lines = lines or [""]
    if len(lines) > 3:
        lines = lines[:3]
        while len(lines[-1]) > 4 and text_font.measure(lines[-1] + "…") > usable:
            lines[-1] = lines[-1][:-2].rstrip()
        lines[-1] += "…"
    width = min(max_width, max(text_font.measure(line) for line in lines) + S(56))
    height = text_font.metrics("linespace") * len(lines) + S(18)
    canvas = tk.Canvas(host, width=width, height=height, bg=_parent_bg(host),
                       highlightthickness=0, bd=0)
    # Negative y keeps the strip inside the window; a positive rest offset used
    # to push every toast below the bottom edge, where it vanished.
    rest_y = -S(18)
    start_y = S(40)
    canvas.place(relx=0.5, rely=1.0, anchor="s", y=start_y)
    # Canvas.lift() is tag_raise (an item operation); raising the window needs Tk's raise.
    try:
        canvas.tk.call("raise", str(canvas))
    except tk.TclError:
        pass
    round_rect(canvas, 0.5, 0.5, width - 0.5, height - 0.5, S(9),
               fill=mix(PANEL, color, 0.14), outline=mix(PANEL, color, 0.40), width=1)
    canvas.create_oval(S(12), height / 2 - S(3), S(18), height / 2 + S(3), fill=color, outline="")
    canvas.create_text(S(26), height / 2, text="\n".join(lines), anchor="w", justify="left",
                       fill=mix(color, TEXT, 0.45), font=text_font)

    def slide(step: int = 0) -> None:
        if not canvas.winfo_exists():
            return
        progress = min(1.0, step / 7)
        eased = 1 - (1 - progress) ** 3
        canvas.place_configure(y=int(start_y + (rest_y - start_y) * eased))
        if progress < 1.0:
            canvas.after(16, lambda: slide(step + 1))

    slide()
    canvas.after(duration, lambda: canvas.destroy() if canvas.winfo_exists() else None)


def divider(parent, color: str = LINE_SOFT, pad: int = 0) -> tk.Frame:
    line = tk.Frame(parent, bg=color, height=1)
    line.pack(fill="x", pady=S(pad))
    return line


class Spinner(tk.Canvas):
    """Thin circular gradient arc that turns at a constant, linear speed.

    The animation is configured by two numbers:

    * ``interval`` - milliseconds between frames (16 ms ≈ 60 fps)
    * ``degrees``  - how far the arc advances per frame

    Each frame adds ``degrees`` to ``angle`` and re-places the same 44 short
    round-capped strokes around the centre, so there is no easing and no
    keyframe list to maintain: 5.2° per 16 ms is one turn every ~1.1 s.
    """

    SEGMENTS = 44

    def __init__(self, parent, size: int = 48, thickness: int = 3, sweep: float = 282.0,
                 degrees: float = 5.2, interval: int = 16, bg: str | None = None):
        self.bg = bg or _parent_bg(parent)
        super().__init__(parent, width=S(size), height=S(size), bg=self.bg,
                         highlightthickness=0, bd=0)
        self.size_px = S(size)
        self.thickness = max(2, S(thickness))
        self.span = sweep / self.SEGMENTS
        self.degrees = degrees
        self.interval = interval
        self.angle = 0.0
        self._job = None
        radius = self.size_px / 2 - self.thickness
        self._offsets: list[float] = []
        self._items: list[int] = []
        for index in range(self.SEGMENTS):
            t = index / (self.SEGMENTS - 1)
            # The tail melts into the page so the arc has no hard start edge;
            # the head stays saturated blue.
            fade = 0.15 + 0.85 * min(1.0, t / 0.3)
            color = mix(mix(self.bg, BLUE_SOFT, fade), BLUE, t ** 0.8)
            self._items.append(self.create_line(0, 0, 0, 0, width=self.thickness,
                                                fill=color, capstyle="round"))
            self._offsets.append(t * sweep)
        self._radius = radius
        self._draw()

    def _draw(self) -> None:
        centre = self.size_px / 2
        for item, offset in zip(self._items, self._offsets):
            start = math.radians(self.angle + offset)
            end = math.radians(self.angle + offset + self.span)
            self.coords(
                item,
                centre + self._radius * math.cos(start), centre + self._radius * math.sin(start),
                centre + self._radius * math.cos(end), centre + self._radius * math.sin(end),
            )

    def start(self) -> None:
        if self._job:
            return

        def tick() -> None:
            if not _alive(self):
                self._job = None
                return
            self.angle = (self.angle + self.degrees) % 360.0
            self._draw()
            self._job = self.winfo_toplevel().after(self.interval, tick)

        tick()

    def stop(self) -> None:
        if not self._job:
            return
        try:
            self.after_cancel(self._job)
        except tk.TclError:
            pass
        self._job = None


class LoadingOverlay(tk.Frame):
    """Full-window loading state: centred spinner, one grey line of copy."""

    def __init__(self, master, text: str = "Loading dashboard..."):
        super().__init__(master, bg=BG)
        self.place(relx=0, rely=0, relwidth=1, relheight=1)
        try:
            self.tk.call("raise", str(self))
        except tk.TclError:
            pass
        centre = tk.Frame(self, bg=BG)
        centre.place(relx=0.5, rely=0.5, anchor="center")
        self.spinner = Spinner(centre, size=48, thickness=3, bg=BG)
        self.spinner.pack()
        self.caption = tk.Label(centre, text=text, bg=BG, fg=MUTED, font=font(12))
        self.caption.pack(pady=(S(18), 0))
        self.spinner.start()

    def set_text(self, text: str) -> None:
        if self.caption.winfo_exists():
            self.caption.configure(text=text)

    def close(self) -> None:
        self.spinner.stop()
        if self.winfo_exists():
            self.destroy()
