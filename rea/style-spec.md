# Listen app style spec (v2)

Source of truth in code: `listen_app/theme.py`. Use its names; do not add raw hex values to windows.

## Reference
The Commander's Sticky Notepad screenshots (2026-10-18). Taken from it: dark body, light type, one blue accent,
rounded corners, thin monoline icons in a lavender title strip, a popover with swatches and an opacity slider.
Rejected: copying Apple's assets or fonts, decorative gradients, heavy shadows.
v2 applied the same sheet to every window, not just the sticky, on the Commander's instruction
("restyle everything ... keep the same style for all new additions ... make sure we have sort of a
html style in memory that we can apply to everything and build off of").

## Colour
| Token | Value | Use |
|---|---|---|
| BODY | #1F1A26 | window and note body |
| SURFACE | #2A2433 | cards, list rows, panels; also the flat fill of a disabled button |
| SURFACE_HI | #3A3343 | secondary buttons, fields |
| SURFACE_HOVER | #4A4356 | hover, active icon |
| POPOVER | #3A3A3C | popovers and tooltips |
| INPUT | #241F2C | entry and text wells inside a card |
| TEXT | #F2F0F5 | primary text and icons |
| TEXT_DIM | #A9A1B6 | secondary text, disabled label |
| ACCENT | #3478F6 | primary action, slider, caret, selection |
| ON_ACCENT | #FFFFFF | type and icons on a blue or red filled button |
| DANGER | #C4574F | destructive action only |
| CHROME | #9B8AAB | title strip on app windows |
| CHROME_HOVER | #8A79A0 | hover/press chip on a lavender strip |
| INK | #1C1622 | dark type and icons on a coloured note surface |

Note colours (`notes_store.NOTE_COLORS`): olive, mustard, sage, slate, mauve (default), rose.
That palette is note data, not chrome, so `notes_store.py` is the second and last file allowed hex values.

## Shape and rhythm
- Window radius 14, buttons and fields 8, chips and tooltips 6.
- Spacing in multiples of 8 (4, 8, 12, 16).

## Type
- Family: Helvetica Neue on macOS, Segoe UI on Windows, Helvetica elsewhere (`theme.FAMILY`).
- Sizes: title 20, heading 16, sub 13, body 12, small 11.
- Headings bold; completed to-do items use overstrike and TEXT_DIM.
- The editor toolbar shows each chip in the style it applies (H1 largest, I italic, S struck).

## Components
- Buttons: `theme.button(kind, small)` with kind = primary | secondary | ghost | danger.
- Enable/disable: `theme.set_enabled(widget, kind, enabled)`. customtkinter dims only the label, so a
  disabled Stop looked as live as an active one; this also flattens the fill, which is the part the eye reads.
- Wells: `theme.field()` for a CTkEntry or CTkTextbox.
- Surfaces: `theme.surface()` for cards and panels; `theme.body()` for windows and toplevels.
- Icons: `theme.icon(name)`; names are in `theme.ICON_NAMES` (18: close, plus, minus, pin, appearance,
  trash, bullet, mic, record, stop, play, check, reminder, file, globe, text, note, sparkle).
  Drawn in code at 4x and scaled down, so there is no extra asset or licence.

## Where the sheet is applied
| Window | Chrome |
|---|---|
| Main `Listen` | lavender header with the title and Notes / Summarize, a surfaced input card with icon buttons, a dark text well, a surfaced control row, accent Read |
| Notes list | lavender strip with New note and the count; surfaced rows with a colour chip, pin, Edit and Pop out |
| Note editor | surfaced top row (Pop Out, colour menu, To Reminders, Delete), toolbar chips, memo strip pinned to the bottom |
| Sticky | tinted title bar with dark ink icons, the note title shortened with an ellipsis |
| Colour popover | surfaced panel, swatch check in dark ink, accent slider |
| Finished popup | heading, one line of help, accent Restart with a play icon, secondary New |
| Summary dialog | body background, accent New note, surfaced note buttons |

## Rules
1. A new window starts from `theme.body()` and uses theme buttons and icons only.
2. A new icon is added to `theme.ICON_NAMES` and `_draw` together, and checked on a sheet render.
3. Keep button text labels where tests or probes use them by name (Read, Stop, Record memo, New note).
4. Icon-only buttons need a tooltip (`_IconButton.tip`).
5. Pack an expanding widget last, or claim the bottom with `side="bottom"` first. A fill-both-expand
   packed before a fixed strip squeezes the strip out of the window (this is what hid the memo row).
6. Tests enforce rule 1: `tests/test_theme_restyle.py` fails if any module other than `theme.py` and
   `notes_store.py` contains a hex colour literal.

## Status
- Done: every window above restyled from `theme.py`; 110 tests passing; `readthrough/look_probe.py`
  captures all six surfaces for an eye check.
- Not done: Windows and Linux are still untested (deferred by the Commander).
