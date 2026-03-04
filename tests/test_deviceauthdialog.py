# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for DeviceAuthDialog.

``xbmcgui.WindowXMLDialog`` is not provided by the sakee emulator, so this
module injects minimal fakes before importing the dialog under test.
"""

import os
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("KODI_INTERACTIVE", "0")
os.environ.setdefault("KODI_HOME", "tests/home")


class FakeControl:
    """Records calls made by the dialog on each control."""

    def __init__(self):
        self.label = None
        self.image = None
        self.visible = True
        self.width = None

    def setLabel(self, text):
        self.label = text

    def setImage(self, path):
        self.image = path

    def setVisible(self, flag):
        self.visible = flag

    def setWidth(self, w):
        self.width = w


class FakeWindowXMLDialog:
    """Minimal stand-in for xbmcgui.WindowXMLDialog."""

    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, *args, **kwargs):
        self._controls = {}
        self._closed = False

    def getControl(self, control_id):
        if control_id not in self._controls:
            self._controls[control_id] = FakeControl()
        return self._controls[control_id]

    def close(self):
        self._closed = True
        self.onClosed()

    def doModal(self):
        pass


class _FakeListItem:
    """Minimal ListItem stub so sakee's xbmc.py can import from xbmcgui."""
    def __init__(self, *a, **kw):
        pass


if "xbmcgui" not in sys.modules:
    _xbmcgui = types.ModuleType("xbmcgui")
    _xbmcgui.WindowXMLDialog = FakeWindowXMLDialog
    _xbmcgui.ListItem = _FakeListItem
    sys.modules["xbmcgui"] = _xbmcgui
else:
    sys.modules["xbmcgui"].WindowXMLDialog = FakeWindowXMLDialog
    if not hasattr(sys.modules["xbmcgui"], "ListItem"):
        sys.modules["xbmcgui"].ListItem = _FakeListItem

from resources.lib.deviceauthdialog import DeviceAuthDialog  # noqa: E402

ACTION_PREVIOUS_MENU = DeviceAuthDialog.ACTION_PREVIOUS_MENU
ACTION_NAV_BACK = DeviceAuthDialog.ACTION_NAV_BACK
XML_ID_BTN_CANCEL = DeviceAuthDialog.XML_ID_BTN_CANCEL
XML_ID_BTN_MANUAL = DeviceAuthDialog.XML_ID_BTN_MANUAL
XML_ID_PROGRESS = DeviceAuthDialog.XML_ID_PROGRESS
XML_ID_TIME = DeviceAuthDialog.XML_ID_TIME
XML_ID_QR_IMAGE = DeviceAuthDialog.XML_ID_QR_IMAGE
XML_ID_QR_TEXT = DeviceAuthDialog.XML_ID_QR_TEXT
XML_PROGRESS_BAR_WIDTH = DeviceAuthDialog.XML_PROGRESS_BAR_WIDTH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dialog():
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


def _fake_action(action_id):
    action = MagicMock()
    action.getId.return_value = action_id
    return action


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeviceAuthDialogInitialState(unittest.TestCase):

    def test_result_none_after_construction(self):
        dlg = _make_dialog()
        self.assertIsNone(dlg.result)

    def test_stop_event_not_set_after_construction(self):
        dlg = _make_dialog()
        self.assertIsInstance(dlg.stop_event, threading.Event)
        self.assertFalse(dlg.stop_event.is_set())


class TestDeviceAuthDialogConstruction(unittest.TestCase):

    def test_fields_stored(self):
        dlg = _make_dialog()
        self.assertEqual(dlg._title, "Title")
        self.assertEqual(dlg._visit_url, "https://example.com/activate")
        self.assertEqual(dlg._code, "ABC-123")
        self.assertEqual(dlg._timeout, 300)

    def test_qr_path_none_when_no_qr_url(self):
        dlg = _make_dialog()
        self.assertIsNone(dlg._qr_path)
        self.assertIsNone(dlg._qr_url)

    def test_manual_label_none_by_default(self):
        dlg = _make_dialog()
        self.assertIsNone(dlg._manual_label)

    def test_new_passes_xml_and_path_to_base(self):
        """__new__ must pass xml filename and addon path to WindowXMLDialog.__new__,
        matching the real Kodi C-extension interface (2 required positional args)."""
        from unittest.mock import patch
        from resources.lib.retroconfig import Config
        with patch.object(FakeWindowXMLDialog, "__new__",
                          wraps=FakeWindowXMLDialog.__new__) as mock_new:
            _make_dialog()
        args = mock_new.call_args.args
        self.assertEqual(len(args), 3,  # cls + xml + path
                         "WindowXMLDialog.__new__ must receive exactly cls, xml_filename, script_path")
        self.assertEqual(args[1], DeviceAuthDialog.XML_FILENAME)
        self.assertEqual(args[2], Config.rootDir.rstrip("/\\"),
                         "addon_path passed to __new__ must match Config.rootDir (stripped)")


class TestUpdateProgress(unittest.TestCase):

    def setUp(self):
        self.dlg = _make_dialog()
        self.dlg._timeout = 300

    def test_before_init_is_noop(self):
        """update_progress() is a no-op before onInit sets _start_time."""
        self.dlg.update_progress()
        self.assertIsNone(self.dlg.getControl(XML_ID_PROGRESS).width)

    def test_full_bar_at_start(self):
        import time as _time
        self.dlg._start_time = _time.time()
        self.dlg.update_progress()
        self.assertGreater(self.dlg.getControl(XML_ID_PROGRESS).width, XML_PROGRESS_BAR_WIDTH - 5)

    def test_half_bar_midway(self):
        import time as _time
        self.dlg._start_time = _time.time() - 150  # 150 s into a 300 s window
        self.dlg.update_progress()
        self.assertAlmostEqual(
            self.dlg.getControl(XML_ID_PROGRESS).width, XML_PROGRESS_BAR_WIDTH // 2, delta=5)

    def test_expired_closes_dialog(self):
        import time as _time
        self.dlg._start_time = _time.time() - 301
        self.dlg.update_progress()
        self.assertEqual(self.dlg.result, "timeout")
        self.assertTrue(self.dlg._closed)

    def test_time_format_minutes_and_seconds(self):
        import time as _time
        self.dlg._start_time = _time.time() - 210  # 210 s elapsed → 90 s remaining
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_TIME).label, "1:30")

    def test_time_format_seconds_only(self):
        import time as _time
        self.dlg._start_time = _time.time() - 255  # 255 s elapsed → 45 s remaining
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_TIME).label, "0:45")

    def test_time_format_leading_zero_on_seconds(self):
        import time as _time
        self.dlg._start_time = _time.time() - 235  # 235 s elapsed → 65 s remaining
        self.dlg.update_progress()
        self.assertEqual(self.dlg.getControl(XML_ID_TIME).label, "1:05")


class TestOnClick(unittest.TestCase):

    def test_cancel_button_result_cancelled(self):
        dlg = _make_dialog()
        dlg.onClick(XML_ID_BTN_CANCEL)
        self.assertEqual(dlg.result, "cancelled")

    def test_cancel_button_calls_close(self):
        dlg = _make_dialog()
        dlg.onClick(XML_ID_BTN_CANCEL)
        self.assertTrue(dlg._closed)

    def test_cancel_button_sets_stop_event_via_onClosed(self):
        """stop_event is set via onClosed(), which close() always triggers."""
        dlg = _make_dialog()
        dlg.onClick(XML_ID_BTN_CANCEL)
        self.assertTrue(dlg.stop_event.is_set())

    def test_manual_button_result_manual(self):
        dlg = _make_dialog()
        dlg.onClick(XML_ID_BTN_MANUAL)
        self.assertEqual(dlg.result, "manual")

    def test_manual_button_calls_close(self):
        dlg = _make_dialog()
        dlg.onClick(XML_ID_BTN_MANUAL)
        self.assertTrue(dlg._closed)

    def test_manual_button_sets_stop_event_via_onClosed(self):
        dlg = _make_dialog()
        dlg.onClick(XML_ID_BTN_MANUAL)
        self.assertTrue(dlg.stop_event.is_set())

    def test_unknown_control_does_nothing(self):
        dlg = _make_dialog()
        dlg.onClick(9999)
        self.assertIsNone(dlg.result)


class TestOnAction(unittest.TestCase):

    def test_previous_menu_cancels(self):
        dlg = _make_dialog()
        dlg.onAction(_fake_action(ACTION_PREVIOUS_MENU))
        self.assertEqual(dlg.result, "cancelled")

    def test_nav_back_cancels(self):
        dlg = _make_dialog()
        dlg.onAction(_fake_action(ACTION_NAV_BACK))
        self.assertEqual(dlg.result, "cancelled")

    def test_other_action_ignored(self):
        dlg = _make_dialog()
        dlg.onAction(_fake_action(999))
        self.assertIsNone(dlg.result)

    def test_back_action_sets_stop_event_via_onClosed(self):
        dlg = _make_dialog()
        dlg.onAction(_fake_action(ACTION_NAV_BACK))
        self.assertTrue(dlg.stop_event.is_set())


class TestOnClosed(unittest.TestCase):

    def test_stop_event_is_set(self):
        dlg = _make_dialog()
        dlg.onClosed()
        self.assertTrue(dlg.stop_event.is_set())

    def test_safety_net_sets_cancelled_when_no_explicit_button(self):
        dlg = _make_dialog()
        dlg.onClosed()
        self.assertEqual(dlg.result, "cancelled")

    def test_safety_net_does_not_override_manual(self):
        dlg = _make_dialog()
        dlg._manual = True
        dlg.onClosed()
        self.assertEqual(dlg.result, "manual")

    def test_safety_net_does_not_override_cancelled(self):
        dlg = _make_dialog()
        dlg._cancelled = True
        dlg.onClosed()
        self.assertEqual(dlg.result, "cancelled")

    def test_safety_net_does_not_fire_when_poll_result_set(self):
        dlg = _make_dialog()
        dlg._poll_result = "success"
        dlg.onClosed()
        self.assertEqual(dlg.result, "success")
        self.assertFalse(dlg._cancelled)


class TestCloseWith(unittest.TestCase):

    def test_close_with_sets_poll_result_and_closes(self):
        dlg = _make_dialog()
        dlg.close_with("success")
        self.assertEqual(dlg._poll_result, "success")
        self.assertTrue(dlg._closed)

    def test_close_with_result_accessible_via_property(self):
        dlg = _make_dialog()
        dlg.close_with("timeout")
        self.assertEqual(dlg.result, "timeout")

    def test_close_with_cancelled_not_set(self):
        dlg = _make_dialog()
        dlg.close_with("success")
        self.assertFalse(dlg._cancelled)

    def test_close_with_sets_stop_event_via_onClosed(self):
        """stop_event is set via onClosed(), which close() always triggers."""
        dlg = _make_dialog()
        dlg.close_with("success")
        self.assertTrue(dlg.stop_event.is_set())

    def test_manual_takes_priority_over_poll_result(self):
        dlg = _make_dialog()
        dlg._manual = True
        dlg.close_with("success")
        self.assertEqual(dlg.result, "manual")


class TestTimeout(unittest.TestCase):

    def test_close_with_timeout_sets_poll_result(self):
        dlg = _make_dialog()
        dlg.close_with("timeout")
        self.assertEqual(dlg._poll_result, "timeout")

    def test_close_with_timeout_closes_dialog(self):
        dlg = _make_dialog()
        dlg.close_with("timeout")
        self.assertTrue(dlg._closed)


class TestOnInitQrVisibility(unittest.TestCase):

    def _run_onInit(self, dlg):
        """Run onInit with Config and LanguageHelper patched out."""
        with patch("resources.lib.retroconfig.Config") as mock_cfg:
            mock_cfg.rootDir = "/fake"
            with patch("resources.lib.helpers.languagehelper.LanguageHelper"
                       ".get_localized_string", return_value="[mocked]"):
                dlg.onInit()

    def test_no_qr_url_hides_qr_image_and_text(self):
        dlg = _make_dialog()
        self._run_onInit(dlg)
        self.assertFalse(dlg.getControl(XML_ID_QR_IMAGE).visible)
        self.assertFalse(dlg.getControl(XML_ID_QR_TEXT).visible)

    def test_qr_url_but_missing_module_hides_image_shows_error(self):
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

    def test_manual_label_none_hides_manual_button(self):
        dlg = _make_dialog()
        self._run_onInit(dlg)
        self.assertFalse(dlg.getControl(XML_ID_BTN_MANUAL).visible)


class TestQrCleanup(unittest.TestCase):

    def test_del_removes_qr_file(self):
        fd, fake_path = tempfile.mkstemp(suffix=".png", prefix="qr_test_")
        os.close(fd)
        dlg = _make_dialog()
        dlg._qr_path = fake_path
        self.assertTrue(os.path.exists(fake_path))
        dlg.__del__()
        self.assertFalse(os.path.exists(fake_path))

    def test_del_does_nothing_when_no_qr_file(self):
        dlg = _make_dialog()
        self.assertIsNone(dlg._qr_path)
        dlg.__del__()  # must not raise

    def test_del_tolerates_already_removed_file(self):
        dlg = _make_dialog()
        dlg._qr_path = "/tmp/nonexistent_qr_test.png"
        dlg.__del__()  # must not raise


class TestQrCodeGeneration(unittest.TestCase):

    def test_qr_generation_success(self):
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

    def test_logo_path_default(self):
        """Test that logo_path defaults to addon icon if not provided."""
        with patch("resources.lib.retroconfig.Config.rootDir", "/addon/root"):
            dlg = DeviceAuthDialog(
                title="T", visit_text="V", visit_url="U",
                code_text="C", code="123", timeout=300
            )
            self.assertTrue(dlg._logo_path.endswith("icon.png"))
            self.assertIn("/addon/root", dlg._logo_path)

    def test_logo_path_custom(self):
        """Test that custom logo_path is used if provided."""
        dlg = DeviceAuthDialog(
            title="T", visit_text="V", visit_url="U",
            code_text="C", code="123", timeout=300,
            logo_path="/custom/logo.png"
        )
        self.assertEqual(dlg._logo_path, "/custom/logo.png")
