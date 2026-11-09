from __future__ import annotations

import objc
import pytest
from Foundation import NSMutableArray

from macos_harness import MacOS


@pytest.mark.parametrize("fails", [False, True], ids=["return", "exception"])
def test_window_observation_releases_temporary_native_objects(fails) -> None:
    references: list[objc.WeakRef] = []

    class TemporaryWindowsMacOS(MacOS):
        def _server_windows(self, *, on_screen_only):
            temporary = NSMutableArray.array()
            references.append(objc.WeakRef(temporary))
            if fails:
                raise ValueError("window server unavailable")
            return []

    with TemporaryWindowsMacOS() as mac:
        if fails:
            with pytest.raises(ValueError, match="window server unavailable"):
                mac.windows()
        else:
            assert mac.windows() == []

        assert references[0]() is None


def test_raw_accessibility_object_remains_usable_after_return() -> None:
    class NativeElementMacOS(MacOS):
        def _local_element(self, element):
            return NSMutableArray.array()

        def _server_windows(self, *, on_screen_only):
            return []

    with NativeElementMacOS() as mac:
        native = mac.ax.raw(1)
        native.addObject_("retained result")

        mac.windows()

        assert list(native) == ["retained result"]
