from abc import ABC, abstractmethod
from typing import Any, Callable, Union, List, Optional

from datatypes import BitType, TxtType
from memory_model import PLCVariable, PLCExecutionContext
from registry import get_current_plc


class Instruction(ABC):
    """Base class for all PLC instructions"""

    @abstractmethod
    def execute(self, context: "PLCExecutionContext"):
        """Execute the instruction"""
        pass


class OneShotCapableInstruction(Instruction):
    """Base class for instructions that can operate in one-shot mode"""

    def __init__(self, oneshot: bool = False):
        self.oneshot = oneshot
        self._has_executed_this_rung_true_cycle = False

    def execute(self, context: "PLCExecutionContext"):
        """Execute the instruction, respecting oneshot behavior"""
        execute_action = True
        if self.oneshot:
            if self._has_executed_this_rung_true_cycle:
                execute_action = False  # Already executed this true cycle
            else:
                self._has_executed_this_rung_true_cycle = True

        if execute_action:
            self._perform_action(context)

    @abstractmethod
    def _perform_action(self, context: "PLCExecutionContext"):
        """The core action of the instruction, to be implemented by subclasses"""
        pass

    def reset_oneshot_trigger(self):
        """Reset the oneshot execution state when rung goes false"""
        self._has_executed_this_rung_true_cycle = False


class OutInstruction(OneShotCapableInstruction):
    """Output Coil Instruction (OUT)"""

    def __init__(self, target: "PLCVariable", oneshot: bool = False):
        super().__init__(oneshot=oneshot)
        self.target = target
        # Could add validation for bit type here

    def _perform_action(self, context: "PLCExecutionContext"):
        """Set the target bit to 1"""
        self.target.set_value(1)

    def __str__(self):
        oneshot_str = ", oneshot=True" if self.oneshot else ""
        return f"OUT({self.target}{oneshot_str})"


class LatchInstruction(Instruction):
    """Latch Coil Instruction (SET)"""

    def __init__(self, target: "PLCVariable"):
        self.target = target
        # Could add validation for bit type here

        # Mark the target as latched in its address type (if supported)
        if hasattr(self.target.address_type, "mark_as_latched"):
            self.target.address_type.mark_as_latched(self.target.address)

    def execute(self, context: "PLCExecutionContext"):
        """Set the target bit to 1 (and keep it on even if rung goes false)"""
        self.target.set_value(1)

    def __str__(self):
        return f"SET({self.target})"


class ResetInstruction(Instruction):
    """Unlatch Coil Instruction (RST)"""

    def __init__(self, target: "PLCVariable"):
        self.target = target
        # Could add validation for bit type here

    def execute(self, context: "PLCExecutionContext"):
        """Reset the target to its default value"""
        self.target.set_value(self.target.address_type.data_type_def.default_value())

    def __str__(self):
        return f"RST({self.target})"


class CopyInstruction(OneShotCapableInstruction):
    """Copy Instruction (MOV/CPY) - Single Value Copy"""

    def __init__(
        self, source: Union["PLCVariable", Any], dest: "PLCVariable", oneshot: bool = False
    ):
        super().__init__(oneshot=oneshot)

        # Validate target is a PLCVariable
        if not isinstance(dest, PLCVariable):
            raise TypeError(f"Target must be a PLCVariable, got {type(target)}")

        # Store source & target
        self.source = source
        self.dest = dest

        # Type validation for direct source-to-target compatibility
        # If source is a PLCVariable, use new compatibility checking methods
        if isinstance(source, PLCVariable):
            source.check_copy_allowed(target)
        else:
            # For literal values, attempt validation through target's data type
            try:
                dest.address_type.data_type_def.validate(source)
            except (ValueError, TypeError) as e:
                raise TypeError(
                    f"Source value {source} cannot be converted to target type "
                    f"{target.address_type.data_type_def.__class__.__name__}: {e}"
                )

    def _perform_action(self, context: "PLCExecutionContext"):
        """Copy the source value to the target"""
        try:
            if isinstance(self.source, PLCVariable):
                value = self.source.get_value()
            else:
                value = self.source

            self.dest.set_value(value)
        except (ValueError, TypeError) as e:
            # In case runtime value is incompatible with target type
            plc = get_current_plc()
            if plc and hasattr(plc, "SC"):
                plc.SC[44].set_value(1)  # SC44: Address Error

    def __str__(self):
        source_str = str(self.source) if not isinstance(self.source, str) else f"'{self.source}'"
        oneshot_str = ", ONS" if self.oneshot else ""
        return f"CPY({source_str}, {self.dest}{oneshot_str})"


class CopyBlockInstruction(OneShotCapableInstruction):
    """Copy Block Instruction (CPYBLK) - Copies a range of addresses to consecutive destinations"""

    def __init__(
        self,
        source_start: "PLCVariable",
        source_end: "PLCVariable",
        dest_start: "PLCVariable",
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)
        pass

    def _perform_action(self, context: "PLCExecutionContext"):
        pass

    def __str__(self):
        pass


class CopyFillInstruction(OneShotCapableInstruction):
    """Copy Fill Instruction (FILL) - Copies a single value to multiple consecutive addresses"""

    def __init__(
        self,
        source: Union["PLCVariable", Any],
        dest_start: "PLCVariable",
        dest_end: "PLCVariable",
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)
        pass

    def _perform_action(self, context: "PLCExecutionContext"):
        pass

    def __str__(self):
        pass


class CopyPackInstruction(OneShotCapableInstruction):
    """Copy Pack Instruction (PACK)"""

    def __init__(
        self,
        source_start: "PLCVariable",
        source_end: "PLCVariable",
        dest: "PLCVariable",
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)
        pass

    def _perform_action(self, context: "PLCExecutionContext"):
        pass

    def __str__(self):
        pass


class CopyUnpackInstruction(OneShotCapableInstruction):
    """Copy Unpack Instruction (UNPACK)"""

    def __init__(
        self,
        source: "PLCVariable",
        dest_start: "PLCVariable",
        dest_end: "PLCVariable",
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)
        pass

    def _perform_action(self, context: "PLCExecutionContext"):
        pass

    def __str__(self):
        pass


class MathInstruction(OneShotCapableInstruction):
    """Math Instruction (MATH)"""

    def __init__(
        self, expression_func: Callable[[], Any], target: "PLCVariable", oneshot: bool = False
    ):
        super().__init__(oneshot=oneshot)
        self.expression_func = expression_func
        self.target = target

    def _perform_action(self, context: "PLCExecutionContext"):
        """Calculate the expression and store in the target"""
        result = self.expression_func()
        self.target.set_value(result)

    def __str__(self):
        oneshot_str = ", oneshot=True" if self.oneshot else ""
        return f"MATH(<expression>, {self.target}{oneshot_str})"


class CallInstruction(Instruction):
    """Call a subroutine"""

    def __init__(self, subroutine_name: str):
        self.subroutine_name = subroutine_name

    def execute(self, context: "PLCExecutionContext"):
        """Execute the call by running the subroutine"""
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")

        # Get the subroutine
        subroutine = plc.program.subroutines.get(self.subroutine_name)
        if not subroutine:
            raise ValueError(f"Subroutine '{self.subroutine_name}' not found")

        # Execute the subroutine
        plc._execute_program_block(subroutine, context)

    def __str__(self):
        return f"CALL({self.subroutine_name})"
