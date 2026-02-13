"""Click hardware profile adapter for pyrung validation."""

from __future__ import annotations

from typing import Protocol


class HardwareProfile(Protocol):
    """Validation profile contract consumed by pyrung click validator."""

    def is_writable(self, memory_type: str, address: int | None = None) -> bool: ...

    def valid_for_role(self, memory_type: str, role: str) -> bool: ...

    def copy_compatible(self, operation: str, source_type: str, dest_type: str) -> bool: ...


class ClickHardwareProfileAdapter:
    """Adapter over pyclickplc's default hardware profile object."""

    def __init__(self, profile: object):
        self._profile = profile

    def is_writable(self, memory_type: str, address: int | None = None) -> bool:
        return bool(self._profile.is_writable(memory_type, address))

    def valid_for_role(self, memory_type: str, role: str) -> bool:
        return bool(self._profile.valid_for_role(memory_type, role))

    def copy_compatible(self, operation: str, source_type: str, dest_type: str) -> bool:
        return bool(self._profile.copy_compatible(operation, source_type, dest_type))


def load_default_profile() -> HardwareProfile | None:
    """Load pyclickplc default profile when available."""
    try:
        from pyclickplc import CLICK_HARDWARE_PROFILE
    except (ImportError, AttributeError):
        return None
    return ClickHardwareProfileAdapter(CLICK_HARDWARE_PROFILE)
