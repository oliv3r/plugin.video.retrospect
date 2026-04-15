# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for DeviceAuthDialog.

``xbmcgui.WindowXMLDialog`` is stubbed by conftest.py; ``FakeControl`` and
``FakeWindowXMLDialog`` are defined there and imported here so that tests can
reference them by name (e.g. to patch ``FakeWindowXMLDialog.__new__``).
"""

import os
import sys
import tempfile
import threading
import types
import unittest
from typing import Any, Optional, Type
from unittest.mock import MagicMock, patch

from fakexbmcgui import FakeControl, FakeWindowXMLDialog  # noqa: F401 — re-exported for tests


class _FakeListItem:
    """Minimal ListItem stub so sakee's xbmc.py can import from xbmcgui."""


    def __init__(self, *a: Any, **kw: Any) -> None:
        pass


try:
    import xbmcgui as _xbmcgui_module
except ImportError:
    _xbmcgui_module = types.ModuleType("xbmcgui")  # type: ignore[assignment]
    sys.modules["xbmcgui"] = _xbmcgui_module  # type: ignore[assignment]

if not hasattr(_xbmcgui_module, "ListItem"):
    _xbmcgui_module.ListItem = _FakeListItem  # type: ignore[attr-defined]

from resources.lib.authentication.authenticationhandler import DeviceAuthResult
from resources.lib.deviceauthdialog import (
    ACTION_NAV_BACK,
    ACTION_PREVIOUS_MENU,
    DeviceAuthDialog,
    XML_FILENAME,
    XML_ID_BTN_CANCEL,
    XML_ID_BTN_MANUAL,
    XML_ID_PROGRESS,
    XML_ID_QR_IMAGE,
    XML_ID_QR_TEXT,
    XML_ID_TIME,
    XML_PROGRESS_BAR_WIDTH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dialog() -> "DeviceAuthDialog":
    """Return a DeviceAuthDialog with default content already set."""

    dlg = DeviceAuthDialog(
        title="Title",
        visit_text="Visit",
        visit_url="https://example.com/activate",
        code_text="Enter code:",
        code="ABC-123",
        timeout=300,
    )
    return dlg


def _fake_action(action_id: int) -> MagicMock:
    action = MagicMock()
    action.getId.return_value = action_id
    return action


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeviceAuthDialogInitialState(unittest.TestCase):


    def test_result_none_after_construction(self) -> None:
        dlg = _make_dialog()
        self.assertIsNone(dlg.result)


    def test_stop_event_not_set_after_construction(self) -> None:
        dlg = _make_dialog()
        self.assertIsInstance(dlg.stop_event, threading.Event)
        self.assertFalse(dlg.stop_event.is_set())


class TestDeviceAuthDialogConstruction(unittest.TestCase):


    def test_fields_stored(self) -> None:
        dlg = _make_dialog()
        self.assertEqual(dlg._title, "Title")
        self.assertEqual(dlg._visit_url, "https://example.com/activate")
        self.assertEqual(dlg._code, "ABC-123")
        self.assertEqual(dlg._timeout, 300)


    def test_qr_path_none_when_no_qr_url(self) -> None:
        dlg = _make_dialog()
        self.assertIsNone(dlg._qr_path)
        self.assertIsNone(dlg._qr_url)


    def test_manual_label_set_by_default(self) -> None:
        dlg = _make_dialog()
        self.assertIsNotNone(dlg._manual_label)


    def test_new_passes_xml_and_path_to_base(self) -> None:
        """
        __new__ must pass xml filename and addon path to WindowXMLDialog.__new__,
        matching the real Kodi C-extension interface (2 required positional args).
        """

        from unittest.mock import patch
        from resources.lib.retroconfig import Config
        with patch.object(FakeWindowXMLDialog, "__new__",
                          wraps=FakeWindowXMLDialog.__new__) as mock_new:
            _make_dialog()
        args = mock_new.call_args.args
        self.assertEqual(len(args), 3,  # cls + xml + path
                         "WindowXMLDialog.__new__ must receive exactly cls, xml_filename, script_path")
        self.assertEqual(args[1], XML_FILENAME)
        self.assertEqual(args[2], Config.rootDir.rstrip("/\\"),
                         "addon_path passed to __new__ must match Config.rootDir (stripped)")


class TestUpdateProgress(unittest.TestCase):


    def setUp(self) -> None:
        self.dlg = _make_dialog()
        self.dlg._timeout = 300
        # update_progress() is a no-op before onInit sets _start_time.

        self.dlg.update_progress()
        self.assertIsNone(self.dlg.getControl(XML_ID_PROGRESS).width)


    def test_full_bar_at_start(self) -> None:
        import time as _time
        self.dlg._start_time = _time.time()
        self.dlg.update_progress()
        self.assertGreater(self.dlg.getControl(XML_ID_PROGRESS).width, XML_PROGRESS_BAR_WIDTH - 5)


    def test_half_bar_midway(self) -> None:
        import time as _time
        self.dlg._start_time = _time.time() - 150  # 150 s into a 300 s window
        self.dlg.update_progress()
        self.assertAlmostEqual(
            self.dlg.getControl(XML_ID_PROGRESS).width, XML_PROGRESS_BAR_WIDTH // 2, delta=5)


    def test_expired_closes_dialog(self) -> None:
        import time as _time
        self.dlg._start_time = _time.time() - 301
        self.dlg.update_progress()
        self.assertEqual(self.dlg.result, DeviceAuthResult.TIMEOUT)
        self.assertTrue(self.dlg._closed)


    def test_time_format_minutes_and_seconds(self) -> None:
        import time as _time
        self.dlg._start_time = _time.time() - 210  # 210 s elapsed → 90 s remaining
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_TIME).label, "1:30")


    def test_time_format_seconds_only(self) -> None:
        import time as _time
        self.dlg._start_time = _time.time() - 255  # 255 s elapsed → 45 s remaining
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_TIME).label, "0:45")


    def test_time_format_leading_zero_on_seconds(self) -> None:
        import time as _time
        self.dlg._start_time = _time.time() - 235  # 235 s elapsed → 65 s remaining
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_TIME).label, "1:05")


    def test_progress_bar_color_full_red_in_danger_zone(self) -> None:
        import time as _time
        # 295 s elapsed of 300 → ~1.7 % remaining, below the 5 % danger threshold
        self.dlg._start_time = _time.time() - 295
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_PROGRESS).color_diffuse, "FFFF0000")


    def test_progress_bar_color_interpolated_in_warn_zone(self) -> None:
        import time as _time
        # 255 s elapsed of 300 → 15 % remaining, between danger (5 %) and warn (25 %)
        # t = (15 - 5) / (25 - 5) = 0.5  →  g = b = int(255 * 0.5) = 127 = 0x7F
        self.dlg._start_time = _time.time() - 255
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_PROGRESS).color_diffuse, "FFFF7F7F")


class TestOnClick(unittest.TestCase):


    def test_cancel_button_result_canceled(self) -> None:
        dlg = _make_dialog()
        dlg._on_click(XML_ID_BTN_CANCEL)
        self.assertEqual(dlg.result, DeviceAuthResult.CANCELED)


    def test_cancel_button_calls_close(self) -> None:
        dlg = _make_dialog()
        dlg._on_click(XML_ID_BTN_CANCEL)
        self.assertTrue(dlg._closed)


    def test_cancel_button_sets_stop_event_via_onClosed(self) -> None:
        """stop_event is set via onClosed(), which close() always triggers."""

        dlg = _make_dialog()
        dlg._on_click(XML_ID_BTN_CANCEL)
        self.assertTrue(dlg.stop_event.is_set())


    def test_manual_button_result_manual(self) -> None:
        dlg = _make_dialog()
        dlg._on_click(XML_ID_BTN_MANUAL)
        self.assertEqual(dlg.result, DeviceAuthResult.MANUAL)


    def test_manual_button_calls_close(self) -> None:
        dlg = _make_dialog()
        dlg._on_click(XML_ID_BTN_MANUAL)
        self.assertTrue(dlg._closed)


    def test_manual_button_sets_stop_event_via_onClosed(self) -> None:
        dlg = _make_dialog()
        dlg._on_click(XML_ID_BTN_MANUAL)
        self.assertTrue(dlg.stop_event.is_set())


    def test_unknown_control_does_nothing(self) -> None:
        dlg = _make_dialog()
        dlg._on_click(9999)
        self.assertIsNone(dlg.result)


class TestOnAction(unittest.TestCase):


    def test_previous_menu_cancels(self) -> None:
        dlg = _make_dialog()
        dlg._on_action(_fake_action(ACTION_PREVIOUS_MENU))
        self.assertEqual(dlg.result, DeviceAuthResult.CANCELED)


    def test_nav_back_cancels(self) -> None:
        dlg = _make_dialog()
        dlg._on_action(_fake_action(ACTION_NAV_BACK))
        self.assertEqual(dlg.result, DeviceAuthResult.CANCELED)


    def test_other_action_ignored(self) -> None:
        dlg = _make_dialog()
        dlg._on_action(_fake_action(999))
        self.assertIsNone(dlg.result)


    def test_back_action_sets_stop_event_via_onClosed(self) -> None:
        dlg = _make_dialog()
        dlg._on_action(_fake_action(ACTION_NAV_BACK))
        self.assertTrue(dlg.stop_event.is_set())


class TestOnClosed(unittest.TestCase):


    def test_stop_event_is_set(self) -> None:
        dlg = _make_dialog()
        dlg._on_closed()
        self.assertTrue(dlg.stop_event.is_set())


    def test_safety_net_sets_canceled_when_no_explicit_button(self) -> None:
        dlg = _make_dialog()
        dlg._on_closed()
        self.assertEqual(dlg.result, DeviceAuthResult.CANCELED)


    def test_safety_net_does_not_override_manual(self) -> None:
        dlg = _make_dialog()
        dlg._manual = True
        dlg._on_closed()
        self.assertEqual(dlg.result, DeviceAuthResult.MANUAL)


    def test_safety_net_does_not_override_canceled(self) -> None:
        dlg = _make_dialog()
        dlg._canceled = True
        dlg._on_closed()
        self.assertEqual(dlg.result, DeviceAuthResult.CANCELED)


    def test_safety_net_does_not_fire_when_poll_result_set(self) -> None:
        dlg = _make_dialog()
        dlg._poll_result = DeviceAuthResult.SUCCESS
        dlg._on_closed()
        self.assertEqual(dlg.result, DeviceAuthResult.SUCCESS)
        self.assertFalse(dlg._canceled)


class TestCloseWith(unittest.TestCase):


    def test_close_with_sets_poll_result_and_closes(self) -> None:
        dlg = _make_dialog()
        dlg.close_with(DeviceAuthResult.SUCCESS)
        self.assertEqual(dlg._poll_result, DeviceAuthResult.SUCCESS)
        self.assertTrue(dlg._closed)


    def test_close_with_result_accessible_via_property(self) -> None:
        dlg = _make_dialog()
        dlg.close_with(DeviceAuthResult.TIMEOUT)
        self.assertEqual(dlg.result, DeviceAuthResult.TIMEOUT)


    def test_close_with_canceled_not_set(self) -> None:
        dlg = _make_dialog()
        dlg.close_with(DeviceAuthResult.SUCCESS)
        self.assertFalse(dlg._canceled)


    def test_close_with_sets_stop_event_via_onClosed(self) -> None:
        """stop_event is set via onClosed(), which close() always triggers."""

        dlg = _make_dialog()
        dlg.close_with(DeviceAuthResult.SUCCESS)
        self.assertTrue(dlg.stop_event.is_set())


    def test_manual_takes_priority_over_poll_result(self) -> None:
        dlg = _make_dialog()
        dlg._manual = True
        dlg.close_with(DeviceAuthResult.SUCCESS)
        self.assertEqual(dlg.result, DeviceAuthResult.MANUAL)


class TestTimeout(unittest.TestCase):


    def test_close_with_timeout_sets_poll_result(self) -> None:
        dlg = _make_dialog()
        dlg.close_with(DeviceAuthResult.TIMEOUT)
        self.assertEqual(dlg._poll_result, DeviceAuthResult.TIMEOUT)


    def test_close_with_timeout_closes_dialog(self) -> None:
        dlg = _make_dialog()
        dlg.close_with(DeviceAuthResult.TIMEOUT)
        self.assertTrue(dlg._closed)


class TestOnInitQrVisibility(unittest.TestCase):


    def _run_onInit(self, dlg: DeviceAuthDialog) -> None:
        """Run onInit with Config and LanguageHelper patched out."""

        with patch("resources.lib.retroconfig.Config") as mock_cfg:
            mock_cfg.rootDir = "/fake"
            with patch("resources.lib.helpers.languagehelper.LanguageHelper"
                       ".get_localized_string", return_value="[mocked]"):
                dlg._on_init()


    def test_no_qr_url_hides_qr_image_and_text(self) -> None:
        dlg = _make_dialog()
        self._run_onInit(dlg)
        self.assertFalse(dlg.getControl(XML_ID_QR_IMAGE).visible)
        self.assertFalse(dlg.getControl(XML_ID_QR_TEXT).visible)


    def test_qr_url_but_missing_module_hides_image_shows_error(self) -> None:
        with patch.dict(sys.modules, {"qrcode": None}):
            dlg = DeviceAuthDialog(
                title="T", visit_text="V", visit_url="https://x.com",
                code_text="E", code="X", timeout=60,
                qr_url="https://x.com/activate",
            )
        self.assertIsNone(dlg._qr_path)
        self.assertIsNotNone(dlg._qr_url)
        self._run_onInit(dlg)
        self.assertFalse(dlg.getControl(XML_ID_QR_IMAGE).visible)
        self.assertIsNotNone(dlg.getControl(XML_ID_QR_TEXT).label)


    def test_manual_button_visible_by_default(self) -> None:
        dlg = _make_dialog()
        self._run_onInit(dlg)
        self.assertTrue(dlg.getControl(XML_ID_BTN_MANUAL).visible)


    def test_qr_path_shows_qr_image_and_text(self) -> None:
        dlg = _make_dialog()
        dlg._qr_path = "/fake/qr.png"
        self._run_onInit(dlg)
        self.assertTrue(dlg.getControl(XML_ID_QR_IMAGE).visible)
        self.assertEqual(dlg.getControl(XML_ID_QR_IMAGE).image, "/fake/qr.png")
        self.assertIsNotNone(dlg.getControl(XML_ID_QR_TEXT).label)


class TestQrCleanup(unittest.TestCase):


    def test_del_removes_qr_file(self) -> None:
        fd, fake_path = tempfile.mkstemp(suffix=".png", prefix="qr_test_")
        os.close(fd)
        dlg = _make_dialog()
        dlg._qr_path = fake_path
        self.assertTrue(os.path.exists(fake_path))
        dlg.__del__()
        self.assertFalse(os.path.exists(fake_path))


    def test_del_does_nothing_when_no_qr_file(self) -> None:
        dlg = _make_dialog()
        self.assertIsNone(dlg._qr_path)
        dlg.__del__()  # must not raise


    def test_del_tolerates_already_removed_file(self) -> None:
        dlg = _make_dialog()
        dlg._qr_path = "/tmp/nonexistent_qr_test.png"
        dlg.__del__()  # must not raise


class TestQrCodeGeneration(unittest.TestCase):


    def test_qr_generation_success(self) -> None:
        """Test that QR code is generated when module is present and URL is provided."""

        mock_qrcode = MagicMock()
        mock_image = MagicMock()
        mock_qrcode.make.return_value = mock_image

        with patch.dict(sys.modules, {"qrcode": mock_qrcode}):
            # We need to ensure os.makedirs doesn't actually make dirs and open/close works
            with patch("os.makedirs"), \
                 patch("tempfile.mkstemp", return_value=(123, "/tmp/qr_test.png")), \
                 patch("os.close"):
                dlg = DeviceAuthDialog(
                    title="T", visit_text="V", visit_url="U",
                    code_text="C", code="123", timeout=300,
                    qr_url="https://example.com/qr"
                )

                self.assertEqual(dlg._qr_path, "/tmp/qr_test.png")
                mock_qrcode.make.assert_called_with("https://example.com/qr")
                mock_image.save.assert_called_with("/tmp/qr_test.png")


    def test_qr_generation_exception_falls_back_gracefully(self) -> None:
        """Non-ImportError from qrcode.make() is caught; _qr_path stays None."""

        mock_qrcode = MagicMock()
        mock_qrcode.make.side_effect = IOError("disk full")

        with patch.dict(sys.modules, {"qrcode": mock_qrcode}):
            with patch("os.makedirs"), \
                 patch("tempfile.mkstemp", return_value=(123, "/tmp/qr_test.png")), \
                 patch("os.close"):
                dlg = DeviceAuthDialog(
                    title="T", visit_text="V", visit_url="U",
                    code_text="C", code="123", timeout=300,
                    qr_url="https://example.com/qr"
                )

        self.assertIsNone(dlg._qr_path)
        self.assertIsNotNone(dlg._qr_url)


    def test_logo_path_default(self) -> None:
        """Test that logo_path defaults to addon icon if not provided."""

        with patch("resources.lib.retroconfig.Config.rootDir", "/addon/root"):
            dlg = DeviceAuthDialog(
                title="T", visit_text="V", visit_url="U",
                code_text="C", code="123", timeout=300
            )
            self.assertTrue(dlg._logo_path.endswith("icon.png"))
            self.assertIn("/addon/root", dlg._logo_path)


    def test_logo_path_custom(self) -> None:
        """Test that custom logo_path is used if provided."""

        dlg = DeviceAuthDialog(
            title="T", visit_text="V", visit_url="U",
            code_text="C", code="123", timeout=300,
            logo_path="/custom/logo.png"
        )
        self.assertEqual(dlg._logo_path, "/custom/logo.png")


class TestInitValidation(unittest.TestCase):
    """DeviceAuthDialog.__new__ returns None for invalid required arguments."""

    def test_none_visit_url_returns_none(self) -> None:
        dlg = DeviceAuthDialog(visit_url=None, code="ABC", timeout=60)  # type: ignore[arg-type]
        self.assertIsNone(dlg)

    def test_empty_visit_url_returns_none(self) -> None:
        dlg = DeviceAuthDialog(visit_url="", code="ABC", timeout=60)
        self.assertIsNone(dlg)

    def test_none_code_returns_none(self) -> None:
        dlg = DeviceAuthDialog(visit_url="https://example.com", code=None, timeout=60)  # type: ignore[arg-type]
        self.assertIsNone(dlg)

    def test_empty_code_returns_none(self) -> None:
        dlg = DeviceAuthDialog(visit_url="https://example.com", code="", timeout=60)
        self.assertIsNone(dlg)

    def test_none_timeout_returns_none(self) -> None:
        dlg = DeviceAuthDialog(visit_url="https://example.com", code="ABC", timeout=None)  # type: ignore[arg-type]
        self.assertIsNone(dlg)

    def test_zero_timeout_returns_none(self) -> None:
        dlg = DeviceAuthDialog(visit_url="https://example.com", code="ABC", timeout=0)
        self.assertIsNone(dlg)

    def test_negative_timeout_returns_none(self) -> None:
        dlg = DeviceAuthDialog(visit_url="https://example.com", code="ABC", timeout=-1)
        self.assertIsNone(dlg)

    def test_valid_args_returns_instance(self) -> None:
        dlg = _make_dialog()
        self.assertIsNotNone(dlg)
        self.assertIsInstance(dlg, DeviceAuthDialog)
