"""Hardware profile definition for pyrung validation."""

from __future__ import annotations

from typing import Protocol, cast

from .capabilities import CLICK_HARDWARE_PROFILE


class HardwareProfile(Protocol):
    """Validation profile contract consumed by the pyrung click validator."""

    def is_writable(self, memory_type: str, address: int | None = None) -> bool: ...

    def valid_for_role(self, memory_type: str, role: str) -> bool: ...

    def copy_compatible(self, operation: str, source_type: str, dest_type: str) -> bool: ...


def load_default_profile() -> HardwareProfile:
    """Load the default pyrung hardware profile."""
    return cast(HardwareProfile, CLICK_HARDWARE_PROFILE)
