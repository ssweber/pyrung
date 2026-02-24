"""P1AM-200 hardware configuration model.

Provides the :class:`P1AM` class which maps physical I/O module slots to
pyrung :class:`~pyrung.core.memory_block.InputBlock` and
:class:`~pyrung.core.memory_block.OutputBlock` instances.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from pyrung.circuitpy.catalog import (
    MODULE_CATALOG,
    ModuleSpec,
)
from pyrung.core.memory_block import InputBlock, OutputBlock

MAX_SLOTS: Final[int] = 15
"""Maximum number of I/O module slots on the P1AM-200 base unit."""


def _make_formatter(prefix: str) -> Callable[[str, int], str]:
    """Create an address formatter that produces ``"Prefix.N"`` tag names."""

    def formatter(block_name: str, addr: int) -> str:
        return f"{prefix}.{addr}"

    return formatter


class P1AM:
    """P1AM-200 hardware configuration.

    Represents a P1AM-200 base unit with up to 15 I/O module slots.
    Each slot is configured with a module part number via :meth:`slot`,
    which constructs and returns the appropriate
    :class:`~pyrung.core.memory_block.InputBlock` /
    :class:`~pyrung.core.memory_block.OutputBlock`.

    This class holds no runtime state — it produces Block instances
    whose tags reference values in ``SystemState.tags`` via the core engine.

    Example::

        hw = P1AM()
        inputs  = hw.slot(1, "P1-08SIM")        # InputBlock
        outputs = hw.slot(2, "P1-08TRS")         # OutputBlock
        inp, out = hw.slot(3, "P1-16CDR")        # combo → tuple

        Button = inputs[1]    # LiveInputTag("Slot1.1", BOOL)
        Light  = outputs[1]   # LiveOutputTag("Slot2.1", BOOL)
    """

    def __init__(self) -> None:
        self._slots: dict[
            int, tuple[ModuleSpec, InputBlock | OutputBlock | tuple[InputBlock, OutputBlock]]
        ] = {}

    def slot(
        self,
        number: int,
        module: str,
        *,
        name: str | None = None,
    ) -> InputBlock | OutputBlock | tuple[InputBlock, OutputBlock]:
        """Configure a module in the given slot and return its block(s).

        Args:
            number: Slot number (1–15 inclusive).
            module: Module part number (e.g. ``"P1-08SIM"``).  Must exist
                in :data:`~pyrung.circuitpy.catalog.MODULE_CATALOG`.
            name: Optional custom name prefix for tags in this slot.
                Defaults to ``"Slot{number}"``.

        Returns:
            - :class:`~pyrung.core.memory_block.InputBlock` for input-only modules.
            - :class:`~pyrung.core.memory_block.OutputBlock` for output-only modules.
            - ``tuple[InputBlock, OutputBlock]`` for combo modules.

        Raises:
            ValueError: If *number* is out of range, *module* is not in the
                catalog, or the slot is already configured.
        """
        if not isinstance(number, int) or number < 1 or number > MAX_SLOTS:
            msg = f"Slot number must be 1–{MAX_SLOTS}, got {number!r}."
            raise ValueError(msg)

        if number in self._slots:
            existing = self._slots[number][0]
            msg = f"Slot {number} is already configured with {existing.part_number}."
            raise ValueError(msg)

        spec = MODULE_CATALOG.get(module)
        if spec is None:
            msg = (
                f"Unknown module {module!r}. "
                f"See pyrung.circuitpy.catalog.MODULE_CATALOG for valid part numbers."
            )
            raise ValueError(msg)

        prefix = name if name is not None else f"Slot{number}"
        result = self._build_blocks(prefix, spec)
        self._slots[number] = (spec, result)
        return result

    @property
    def configured_slots(self) -> dict[int, ModuleSpec]:
        """Mapping of slot number → :class:`ModuleSpec` for all configured slots."""
        return {num: spec for num, (spec, _) in self._slots.items()}

    def get_slot(self, number: int) -> InputBlock | OutputBlock | tuple[InputBlock, OutputBlock]:
        """Retrieve the block(s) for an already-configured slot.

        Raises:
            ValueError: If the slot has not been configured.
        """
        if number not in self._slots:
            msg = f"Slot {number} is not configured."
            raise ValueError(msg)
        return self._slots[number][1]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_blocks(
        prefix: str,
        spec: ModuleSpec,
    ) -> InputBlock | OutputBlock | tuple[InputBlock, OutputBlock]:
        input_group = spec.input_group
        output_group = spec.output_group

        inputs: InputBlock | None = None
        outputs: OutputBlock | None = None

        if input_group is not None:
            input_name = f"{prefix}_In" if spec.is_combo else prefix
            inputs = InputBlock(
                name=input_name,
                type=input_group.tag_type,
                start=1,
                end=input_group.count,
                address_formatter=_make_formatter(input_name),
            )

        if output_group is not None:
            output_name = f"{prefix}_Out" if spec.is_combo else prefix
            outputs = OutputBlock(
                name=output_name,
                type=output_group.tag_type,
                start=1,
                end=output_group.count,
                address_formatter=_make_formatter(output_name),
            )

        if inputs is not None and outputs is not None:
            return (inputs, outputs)
        if inputs is not None:
            return inputs
        # output_group is guaranteed non-None if input_group is None
        # (every module in the catalog has at least one group).
        assert outputs is not None  # noqa: S101
        return outputs

    def __repr__(self) -> str:
        if not self._slots:
            return "P1AM()"
        configured = ", ".join(
            f"{n}={spec.part_number}" for n, (spec, _) in sorted(self._slots.items())
        )
        return f"P1AM({configured})"
