# SPDX-License-Identifier: GPL-3.0-or-later
"""Device flow authentication dialog with progress bar."""

import os
import struct
import zlib

import xbmcgui

from resources.lib.logger import Logger

ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92
ACTION_SELECT_ITEM = 7
ACTION_MOUSE_LEFT_CLICK = 100
ACTION_MOVE_LEFT = 1
ACTION_MOVE_RIGHT = 2

_DLG_W = 700
_DLG_H = 400
_DLG_X = (1280 - _DLG_W) // 2
_DLG_Y = (720 - _DLG_H) // 2
_MARGIN = 25
_ALIGN_CENTER = 0x00000002 | 0x00000004

_COLOR_BG = "FF1A1A2E"
_COLOR_TEXT = "FFFFFFFF"
_COLOR_TEXT_DIM = "FFCCCCCC"
_COLOR_ACCENT = "FF00BFFF"
_COLOR_BTN_TEXT = "FF000000"
_COLOR_FOCUSED = _COLOR_ACCENT
_COLOR_UNFOCUSED = _COLOR_TEXT


def _ensure_bg_texture():
    """Create a 1x1 white pixel PNG for use as tintable background."""

    from resources.lib.retroconfig import Config
    cache_dir = Config.cacheDir
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "bg_1x1.png")
    if os.path.exists(path):
        return path

    def _chunk(tag, data):
        raw = tag + data
        return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 1, 0, 0, 0, 0)))
        fh.write(_chunk(b"IDAT", zlib.compress(b"\x00\x80")))
        fh.write(_chunk(b"IEND", b""))
    return path


class DeviceAuthDialog(xbmcgui.WindowDialog):
    """Device flow dialog with URL, code, and countdown.

    All user-visible text is supplied via constructor parameters so callers
    can pass localized strings.  The dialog reports which button was pressed
    via the ``cancelled`` and ``manual_login`` properties.
    """

    def __init__(self, title, visit_text, verification_uri,
                 enter_code_text, user_code, expires_in, cancel_label,
                 manual_label=None):
        """Create the dialog.

        :param str title:            Dialog title.
        :param str visit_text:       Instruction text above the URL.
        :param str verification_uri: URL to display.
        :param str enter_code_text:  Text above the user code.
        :param str user_code:        The code to display.
        :param int expires_in:       Seconds until the code expires.
        :param str cancel_label:     Label for the cancel button.
        :param str|None manual_label: Label for the manual-login button (omit to hide).
        """

        super().__init__()
        self._cancelled = False
        self._manual = False
        self._build_ui(title, visit_text, verification_uri,
                       enter_code_text, user_code, expires_in,
                       cancel_label, manual_label)

    # -- UI construction ---------------------------------------------------

    def _add_label(self, x, y, w, h, text, **kwargs):
        label = xbmcgui.ControlLabel(x, y, w, h, text, **kwargs)
        self.addControl(label)
        return label

    def _build_ui(self, title, visit_text, verification_uri,
                  enter_code_text, user_code, expires_in,
                  cancel_label, manual_label):
        bg_path = _ensure_bg_texture()
        left_x = _DLG_X + _MARGIN
        text_w = _DLG_W - _MARGIN * 2

        # Full-screen dim overlay
        overlay = xbmcgui.ControlImage(0, 0, 1280, 720, bg_path,
                                       colorDiffuse="AA000000")
        self.addControl(overlay)

        # Dialog background
        dialog_bg = xbmcgui.ControlImage(_DLG_X, _DLG_Y, _DLG_W, _DLG_H,
                                         bg_path, colorDiffuse=_COLOR_BG)
        self.addControl(dialog_bg)

        # Title
        self._add_label(left_x, _DLG_Y + 12, _DLG_W - _MARGIN * 2, 30,
                        title, font="font14", textColor=_COLOR_TEXT)

        # Instructions
        y = _DLG_Y + 55
        self._add_label(left_x, y, text_w, 22,
                        visit_text, textColor=_COLOR_TEXT_DIM)

        y += 28
        self._add_label(left_x, y, text_w, 26,
                        verification_uri, font="font13",
                        textColor=_COLOR_ACCENT)

        y += 40
        self._add_label(left_x, y, text_w, 22,
                        enter_code_text, textColor=_COLOR_TEXT_DIM)
        y += 28
        self._add_label(left_x, y, text_w, 35,
                        user_code, font="font14", textColor=_COLOR_TEXT)

        # Progress bar
        bar_y = _DLG_Y + _DLG_H - 70
        self._progress = xbmcgui.ControlProgress(
            left_x, bar_y, _DLG_W - _MARGIN * 2 - 70, 10)
        self.addControl(self._progress)
        self._progress.setPercent(100)

        self._time_label = xbmcgui.ControlLabel(
            _DLG_X + _DLG_W - _MARGIN - 60, bar_y - 2, 60, 18, "",
            textColor=_COLOR_TEXT_DIM, font="font12")
        self.addControl(self._time_label)
        self.update_progress(100, expires_in)

        # Buttons — use ControlImage + ControlLabel instead of ControlButton.
        # ControlButton consumes ACTION_SELECT_ITEM at the C++ level and
        # dispatches onClick via the GUI message queue, which cannot fire
        # while the Python thread is blocked in the polling loop.  By using
        # plain image+label pairs we keep all input routed through onAction.
        btn_w = 160
        btn_h = 32
        btn_y = _DLG_Y + _DLG_H - 38

        self._btn_bgs = []
        self._btn_rects = []
        self._btn_count = 0
        self._focused_btn = 0

        if manual_label:
            gap = 20
            total_w = btn_w * 2 + gap
            btn_start_x = _DLG_X + (_DLG_W - total_w) // 2

            cancel_bg = xbmcgui.ControlImage(
                btn_start_x, btn_y, btn_w, btn_h,
                bg_path, colorDiffuse=_COLOR_FOCUSED)
            self.addControl(cancel_bg)
            self._add_label(btn_start_x, btn_y, btn_w, btn_h,
                            cancel_label, textColor=_COLOR_BTN_TEXT,
                            alignment=_ALIGN_CENTER)

            manual_x = btn_start_x + btn_w + gap
            manual_bg = xbmcgui.ControlImage(
                manual_x, btn_y, btn_w, btn_h,
                bg_path, colorDiffuse=_COLOR_UNFOCUSED)
            self.addControl(manual_bg)
            self._add_label(manual_x, btn_y, btn_w, btn_h,
                            manual_label, textColor=_COLOR_BTN_TEXT,
                            alignment=_ALIGN_CENTER)

            self._btn_bgs = [cancel_bg, manual_bg]
            self._btn_rects = [
                (btn_start_x, btn_y, btn_start_x + btn_w, btn_y + btn_h),
                (manual_x, btn_y, manual_x + btn_w, btn_y + btn_h),
            ]
            self._btn_count = 2
            self._has_manual = True
        else:
            cx = _DLG_X + (_DLG_W - btn_w) // 2
            cancel_bg = xbmcgui.ControlImage(
                cx, btn_y, btn_w, btn_h,
                bg_path, colorDiffuse=_COLOR_FOCUSED)
            self.addControl(cancel_bg)
            self._add_label(cx, btn_y, btn_w, btn_h,
                            cancel_label, textColor=_COLOR_BTN_TEXT,
                            alignment=_ALIGN_CENTER)

            self._btn_bgs = [cancel_bg]
            self._btn_rects = [(cx, btn_y, cx + btn_w, btn_y + btn_h)]
            self._btn_count = 1
            self._has_manual = False

    # -- Public interface --------------------------------------------------

    @property
    def cancelled(self):
        return self._cancelled

    @property
    def manual_login(self):
        return self._manual

    def update_progress(self, percent, remaining_seconds):
        """Update the progress bar and time remaining label."""

        self._progress.setPercent(percent)
        mins = remaining_seconds // 60
        secs = remaining_seconds % 60
        self._time_label.setLabel(f"{mins}:{secs:02d}")

    # -- Kodi event handlers -----------------------------------------------

    def _update_focus(self):
        """Update button backgrounds to reflect the currently focused button."""

        for i, bg in enumerate(self._btn_bgs):
            bg.setColorDiffuse(
                _COLOR_FOCUSED if i == self._focused_btn else _COLOR_UNFOCUSED)

    def _hit_test(self, x, y):
        """Return the button index at (x, y), or -1 if none."""

        for i, (x1, y1, x2, y2) in enumerate(self._btn_rects):
            if x1 <= x < x2 and y1 <= y < y2:
                return i
        return -1

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self._cancelled = True
            self.close()
        elif action_id == ACTION_MOVE_LEFT:
            if self._focused_btn > 0:
                self._focused_btn -= 1
                self._update_focus()
        elif action_id == ACTION_MOVE_RIGHT:
            if self._focused_btn < self._btn_count - 1:
                self._focused_btn += 1
                self._update_focus()
        elif action_id == ACTION_SELECT_ITEM:
            if self._focused_btn == 0:
                self._cancelled = True
                self.close()
            elif self._focused_btn == 1 and self._has_manual:
                self._manual = True
                self.close()
        elif action_id == ACTION_MOUSE_LEFT_CLICK:
            hit = self._hit_test(
                int(action.getAmount1()), int(action.getAmount2()))
            if hit == 0:
                self._cancelled = True
                self.close()
            elif hit == 1 and self._has_manual:
                self._manual = True
                self.close()
