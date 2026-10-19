"""Shared look for every Listen window: colours, fonts, radii, spacing and small line icons.

This is the style sheet. Windows take their look from these names, never from raw hex
values, so any new window built from these helpers matches the rest of the app.
Matched to the Sticky Notepad reference screenshots: dark body, light type, one blue accent,
rounded corners, thin monoline icons. Written in plain Python, no network, no extra licence.
"""

from __future__ import annotations

import math
import sys

from PIL import Image, ImageColor, ImageDraw

import customtkinter as ctk

# ----- colour ---------------------------------------------------------------
BODY = "#1F1A26"  # window and note body
SURFACE = "#2A2433"  # cards, list rows, panels
SURFACE_HI = "#3A3343"  # hover, pressed, secondary buttons
SURFACE_HOVER = "#4A4356"
POPOVER = "#3A3A3C"  # popovers and tooltips
INPUT = "#241F2C"  # entry and text field wells: one step darker than a card
TEXT = "#F2F0F5"
TEXT_DIM = "#A9A1B6"
ACCENT = "#3478F6"  # system blue: primary action, slider, caret, selection
ACCENT_HOVER = "#2A63D0"
DANGER = "#C4574F"
DANGER_HOVER = "#A94740"
CHROME = "#9B8AAB"  # lavender title strip for app windows
CHROME_HOVER = "#8A79A0"  # pressed/hover chip on a lavender title strip
INK = "#1C1622"  # dark type on a coloured note surface (notes keep their tint)
ON_ACCENT = "#FFFFFF"  # type and icons on a blue or red filled button

# ----- shape and rhythm -----------------------------------------------------
RADIUS_WINDOW = 14
RADIUS = 8  # buttons and fields
RADIUS_SMALL = 6  # chips and tooltips
UNIT = 8  # spacing comes in multiples: 4, 8, 12, 16

# ----- type -----------------------------------------------------------------
FAMILY = {"darwin": "Helvetica Neue", "win32": "Segoe UI"}.get(sys.platform, "Helvetica")
SIZE_TITLE = 20
SIZE_HEAD = 16
SIZE_SUB = 13
SIZE_BODY = 12
SIZE_SMALL = 11


def font(size: int = SIZE_BODY, *styles: str) -> tuple:
    """A Tk font tuple. Pass 'bold', 'italic', 'underline' or 'overstrike' as styles."""
    return (FAMILY, size, *styles) if styles else (FAMILY, size)


# ----- buttons --------------------------------------------------------------
_BUTTON_KINDS = {
    "primary": {"fg_color": ACCENT, "hover_color": ACCENT_HOVER, "text_color": ON_ACCENT},
    "secondary": {"fg_color": SURFACE_HI, "hover_color": SURFACE_HOVER, "text_color": TEXT},
    "ghost": {"fg_color": "transparent", "hover_color": SURFACE_HI, "text_color": TEXT},
    "danger": {"fg_color": DANGER, "hover_color": DANGER_HOVER, "text_color": ON_ACCENT},
}

DISABLED_FILL = SURFACE  # a disabled button sits flat on its card instead of raised on SURFACE_HI


def button(kind: str = "secondary", small: bool = False) -> dict:
    """Keyword arguments for a CTkButton of the given kind. Use: CTkButton(parent, text=..., **button('primary'))."""
    height, size = (24, SIZE_SMALL) if small else (32, SIZE_BODY)
    return {**_BUTTON_KINDS[kind], "text_color_disabled": TEXT_DIM, "corner_radius": RADIUS,
            "height": height, "font": font(size)}


def set_enabled(widget, kind: str, enabled: bool) -> None:
    """Enable or disable a theme button so the state is visible.

    customtkinter dims only the label, so a disabled Stop looked as live as an active one.
    This also flattens the fill, which is the part the eye reads.
    """
    widget.configure(
        state="normal" if enabled else "disabled",
        fg_color=_BUTTON_KINDS[kind]["fg_color"] if enabled else DISABLED_FILL,
        hover_color=_BUTTON_KINDS[kind]["hover_color"] if enabled else DISABLED_FILL,
    )


def surface() -> dict:
    """Keyword arguments for a card or panel frame."""
    return {"fg_color": SURFACE, "corner_radius": RADIUS}


def body() -> dict:
    """Keyword arguments for a plain window or transparent holder."""
    return {"fg_color": BODY}


def field(font_size: int = SIZE_BODY, **extra) -> dict:
    """Keyword arguments for a CTkEntry or CTkTextbox: a darker well inside a card.

    Extra keywords (placeholder_text, wrap, height, ...) are merged, so a caller can still
    set what is specific to its widget without restating the colours.
    """
    return {
        "fg_color": INPUT,
        "border_color": SURFACE_HI,
        "border_width": 1,
        "corner_radius": RADIUS,
        "text_color": TEXT,
        "font": font(font_size),
        **extra,
    }


# ----- icons ----------------------------------------------------------------
ICON_NAMES = (
    "close", "plus", "minus", "pin", "appearance", "trash", "bullet",
    "mic", "record", "stop", "play", "check", "reminder",
    "file", "globe", "text", "note", "sparkle",
)
ICON_SIZE = 18


_PICTURES: dict[tuple[str, str, int], Image.Image] = {}


def icon(name: str, color: str = TEXT, size: int = ICON_SIZE) -> ctk.CTkImage:
    """A thin monoline icon drawn at 4x and scaled down, so edges stay clean on Retina and standard screens.

    The drawing is cached per name, colour and size. Redrawing it for every row and every window
    cost time and memory for identical pixels; a row that is re-pointed as the list scrolls never
    needs a fresh drawing.
    """
    if name not in ICON_NAMES:
        raise KeyError(f"unknown icon {name!r}")
    key = (name, color, size)
    picture = _PICTURES.get(key)
    if picture is None:
        picture = _draw(name, color, size)
        _PICTURES[key] = picture
    # The CTkImage is built per call: it is bound to the Tk root that exists now. Caching it
    # across roots left dangling 'pyimage' references once a window was destroyed (Windows CI).
    return ctk.CTkImage(light_image=picture, dark_image=picture, size=(size, size))


def _draw(name: str, color: str, size: int) -> Image.Image:
    scale = 4
    s = size * scale
    rgba = ImageColor.getrgb(color) + (255,)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(2, round(s * 0.085))  # stroke width
    r = w / 2

    def at(v: float) -> float:
        return s * v

    def line(*points: tuple[float, float]) -> None:
        pts = [(at(x), at(y)) for x, y in points]
        d.line(pts, fill=rgba, width=w, joint="curve")
        for x, y in pts:  # round caps
            d.ellipse((x - r, y - r, x + r, y + r), fill=rgba)

    def dot(x: float, y: float, rad: float) -> None:
        d.ellipse((at(x) - at(rad), at(y) - at(rad), at(x) + at(rad), at(y) + at(rad)), fill=rgba)

    lo, hi, mid = 0.22, 0.78, 0.5
    if name == "close":
        line((lo, lo), (hi, hi))
        line((hi, lo), (lo, hi))
    elif name == "plus":
        line((mid, lo), (mid, hi))
        line((lo, mid), (hi, mid))
    elif name == "minus":
        line((lo, mid), (hi, mid))
    elif name == "check":
        line((0.2, 0.52), (0.42, 0.74), (0.8, 0.3))
    elif name == "pin":
        d.rounded_rectangle((at(0.36), at(0.16), at(0.64), at(0.5)), radius=at(0.06), outline=rgba, width=w)
        line((lo + 0.06, 0.5), (hi - 0.06, 0.5))
        line((mid, 0.5), (mid, 0.84))
    elif name == "appearance":
        d.ellipse((at(lo), at(lo), at(hi), at(hi)), outline=rgba, width=w)
        d.pieslice((at(lo), at(lo), at(hi), at(hi)), start=-90, end=90, fill=rgba)
    elif name == "bullet":
        for y in (0.3, 0.5, 0.7):
            dot(0.26, y, w / s * 0.9)
            line((0.44, y), (hi, y))
    elif name == "trash":
        line((0.24, 0.3), (0.76, 0.3))
        line((0.42, 0.3), (0.42, 0.22), (0.58, 0.22), (0.58, 0.3))
        d.rounded_rectangle((at(0.3), at(0.36), at(0.7), at(0.8)), radius=at(0.05), outline=rgba, width=w)
        line((0.43, 0.46), (0.43, 0.7))
        line((0.57, 0.46), (0.57, 0.7))
    elif name == "mic":
        d.rounded_rectangle((at(0.38), at(0.14), at(0.62), at(0.6)), radius=at(0.12), outline=rgba, width=w)
        d.arc((at(0.26), at(0.4), at(0.74), at(0.8)), start=0, end=180, fill=rgba, width=w)
        line((mid, 0.8), (mid, 0.9))
    elif name == "record":
        d.ellipse((at(0.2), at(0.2), at(0.8), at(0.8)), fill=rgba)
    elif name == "stop":
        d.rounded_rectangle((at(0.24), at(0.24), at(0.76), at(0.76)), radius=at(0.06), fill=rgba)
    elif name == "play":
        d.polygon([(at(0.32), at(0.2)), (at(0.8), at(0.5)), (at(0.32), at(0.8))], fill=rgba)
    elif name == "reminder":
        d.ellipse((at(0.18), at(0.18), at(0.82), at(0.82)), outline=rgba, width=w)
        line((0.36, 0.5), (0.46, 0.6), (0.64, 0.4))
    elif name == "file":
        d.rounded_rectangle((at(0.24), at(0.14), at(0.76), at(0.86)), radius=at(0.06), outline=rgba, width=w)
        line((0.36, 0.36), (0.56, 0.36))
        line((0.36, 0.52), (0.64, 0.52))
        line((0.36, 0.68), (0.58, 0.68))
    elif name == "globe":
        d.ellipse((at(0.14), at(0.14), at(0.86), at(0.86)), outline=rgba, width=w)
        line((0.14, 0.5), (0.86, 0.5))
        d.ellipse((at(0.35), at(0.14), at(0.65), at(0.86)), outline=rgba, width=w)
    elif name == "text":
        line((0.2, 0.28), (0.74, 0.28))
        line((0.2, 0.5), (0.8, 0.5))
        line((0.2, 0.72), (0.62, 0.72))
    elif name == "note":
        d.rounded_rectangle((at(0.2), at(0.16), at(0.8), at(0.84)), radius=at(0.07), outline=rgba, width=w)
        line((0.32, 0.38), (0.68, 0.38))
        line((0.32, 0.56), (0.6, 0.56))
        line((0.32, 0.72), (0.52, 0.72))
    elif name == "sparkle":
        # Four-point star: filled, because an outline smears at 18 px.
        star = []
        for step in range(8):
            reach = 0.38 if step % 2 == 0 else 0.13
            angle = math.radians(-90 + step * 45)
            star.append((at(0.5 + reach * math.cos(angle)), at(0.5 + reach * math.sin(angle))))
        d.polygon(star, fill=rgba)
    return img.resize((size, size), Image.LANCZOS)


# ----- scrolling list ---------------------------------------------------------
# Two problems, one widget:
#
# 1. customtkinter 6.0.0's CTkScrollableFrame re-enters its own scrollbar redraw
#    (CTkScrollbar._draw -> update_idletasks -> set -> _draw). With a few dozen rows it
#    stalls for seconds, and at about 120 rows it never returns, so the Notes list froze
#    for a user with a long list. ScrollList uses a plain tk.Canvas and tk.Scrollbar, which
#    do not recurse.
# 2. Building CTk rows inside a shown canvas costs about 52 s at 300 rows, and the cost grows
#    faster than the row count (readthrough/round13/round13-list-backend.md). ScrollList is
#    therefore virtual: it keeps a small pool of row frames and re-points them as you scroll,
#    so a 10,000-row list builds the same widgets as a 20-row one.

import tkinter as tk  # noqa: E402


class ScrollList(ctk.CTkFrame):
    """A vertically scrolling column of fixed-height rows, built only for what is in view.

    Rows must share one height, so the list can place them by arithmetic. Drive it with
    set_items(items, build, update):

        build(parent)      -> an empty row frame, master = the list's `.body`
        update(row, item)  -> re-point that row at its content, button commands included

    `.body` is still a real frame for content that is not a row, such as an empty-state label.
    `winfo_children()` returns the rows that are in view, in list order.
    """

    EMPTY_MIN_H = 80  # keeps an empty-state label visible when the list holds no rows
    SIDE_PAD = 2      # matches the padx the rows used to carry themselves

    def __init__(self, master, row_height: int = 46, gap: int = 4, side_pad: int = SIDE_PAD, **kwargs) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=0, **kwargs)
        self._row_height = row_height
        self._gap = gap
        self._side_pad = side_pad
        self._pitch = row_height + 2 * gap
        self._items: list = []
        self._build = None
        self._update = None
        self._slots: list = []  # row frames, reused as the view moves
        self._bound: list = []  # slot -> index of the item it shows, or None when hidden

        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=BODY)
        # A plain tk.Scrollbar: ctk.CTkScrollbar re-enters its own redraw and stalls (see above).
        self._bar = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview, width=10,
                                 bg=SURFACE_HI, troughcolor=BODY, activebackground=SURFACE_HOVER, bd=0,
                                 highlightthickness=0, relief="flat")
        self._canvas.configure(yscrollcommand=self._on_yscroll)
        self._bar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self.body = ctk.CTkFrame(self._canvas, fg_color="transparent", corner_radius=0)
        self._window = self._canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body)
        self._canvas.bind("<Configure>", self._on_canvas)
        self._canvas.bind("<Enter>", lambda _e: self._canvas.bind_all("<MouseWheel>", self._wheel))
        self._canvas.bind("<Leave>", lambda _e: self._canvas.unbind_all("<MouseWheel>"))

    # ----- driving it -------------------------------------------------------
    def set_items(self, items, build=None, update=None) -> None:
        """Show `items`, building rows on demand. Resets the view to the top.

        `build` and `update` are optional so an empty list can be shown without a row builder.
        """
        if build is not None:
            self._build = build
        if update is not None:
            self._update = update
        self._items = list(items)
        self._resize_extent()
        self._canvas.yview_moveto(0)
        # Force every slot to re-point, so a refresh cannot leave stale content on screen.
        self._bound = [None] * len(self._slots)
        self._rebind()

    def _resize_extent(self) -> None:
        """Tell the canvas how tall the whole list is, from the row count."""
        height = max(len(self._items) * self._pitch, self.EMPTY_MIN_H)
        self._canvas.itemconfigure(self._window, height=height)
        bbox = self._canvas.bbox("all")
        if bbox:
            self._canvas.configure(scrollregion=bbox)

    # ----- geometry ---------------------------------------------------------
    def _on_body(self, _event=None) -> None:
        bbox = self._canvas.bbox("all")
        if bbox:
            self._canvas.configure(scrollregion=bbox)

    def _on_canvas(self, event) -> None:
        # The rows follow the canvas width, so text wraps to the window and not past it.
        # The body is inset by the side pad, so a row keeps the same margins it had when packed.
        self._canvas.itemconfigure(self._window, width=max(event.width - 2 * self._side_pad, 1))
        self._rebind()

    def _on_yscroll(self, first, last) -> None:
        self._bar.set(first, last)
        self._rebind()

    def _rebind(self) -> None:
        """Point the row pool at what is in view. Slots already showing the right item do nothing."""
        if self._update is None:
            return
        count = len(self._items)
        pitch = self._pitch
        if count == 0:
            for slot, row in enumerate(self._slots):
                if self._bound[slot] is not None:
                    row.place_forget()
                    self._bound[slot] = None
            return
        first = min(max(int(self._canvas.canvasy(0) // pitch), 0), count - 1)
        # One row past the viewport, so a row that is part-scrolled in is ready before it is needed.
        need = min(count, max(1, int(self._canvas.winfo_height() // pitch) + 2))
        while len(self._slots) < need:
            row = self._build(self.body)
            row.pack_propagate(False)  # the fixed height is what keeps placement exact
            row.configure(height=self._row_height)
            row.bind("<MouseWheel>", self._wheel)
            self._slots.append(row)
            self._bound.append(None)
        for slot in range(len(self._slots)):
            index = first + slot
            row = self._slots[slot]
            if index >= count or slot >= need:
                if self._bound[slot] is not None:
                    row.place_forget()
                    self._bound[slot] = None
                continue
            if self._bound[slot] != index:
                self._update(row, self._items[index])
                self._bound[slot] = index
                row.place(x=self._side_pad, y=index * pitch + self._gap, relwidth=1.0)

    def _wheel(self, event) -> None:
        self._canvas.yview_scroll(int(-event.delta / 40) or (-1 if event.delta > 0 else 1), "units")

    def winfo_children(self):
        """The rows in view, in list order. Off-screen rows are deliberately not built."""
        return [row for row, bound in zip(self._slots, self._bound) if bound is not None]
