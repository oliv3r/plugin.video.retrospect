# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fakes for xbmcgui classes not provided by sakee."""

from typing import Any, Optional


class FakeControl:
    """Records calls made by the dialog on each control."""

    def __init__(self) -> None:
        self.label: Optional[str] = None
        self.image: Optional[str] = None
        self.visible: bool = True
        self.width: Optional[int] = None
        self.color_diffuse: Optional[str] = None

    def setLabel(self, text: str) -> None:
        self.label = text

    def setImage(self, path: str) -> None:
        self.image = path

    def setVisible(self, flag: bool) -> None:
        self.visible = flag

    def setWidth(self, w: int) -> None:
        self.width = w

    def setColorDiffuse(self, color: str) -> None:
        self.color_diffuse = color


class FakeWindowXMLDialog:
    """Fully-functional stand-in for xbmcgui.WindowXMLDialog."""

    def __new__(cls, *args: Any, **kwargs: Any) -> "FakeWindowXMLDialog":
        return object.__new__(cls)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._controls: dict = {}
        self._closed: bool = False

    def getControl(self, control_id: int) -> FakeControl:
        return self._controls.setdefault(control_id, FakeControl())

    def close(self) -> None:
        self._closed = True
        if hasattr(self, "onClosed"):
            self.onClosed()  # type: ignore[attr-defined]

    def doModal(self) -> None:
        pass
