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
        self, source: Union["PLCVariable", Any], target: "PLCVariable", oneshot: bool = False
    ):
        super().__init__(oneshot=oneshot)

        # Validate target is a PLCVariable
        if not isinstance(target, PLCVariable):
            raise TypeError(f"Target must be a PLCVariable, got {type(target)}")

        # Store source & target
        self.source = source
        self.target = target

        # Type validation for direct source-to-target compatibility
        # If source is a PLCVariable, use new compatibility checking methods
        if isinstance(source, PLCVariable):
            source.check_copy_allowed(target)
        else:
            # For literal values, attempt validation through target's data type
            try:
                target.address_type.data_type_def.validate(source)
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

            self.target.set_value(value)
        except (ValueError, TypeError) as e:
            # In case runtime value is incompatible with target type
            plc = get_current_plc()
            if plc and hasattr(plc, "SC"):
                plc.SC[44].set_value(1)  # SC44: Address Error

    def __str__(self):
        source_str = str(self.source) if not isinstance(self.source, str) else f"'{self.source}'"
        oneshot_str = ", ONS" if self.oneshot else ""
        return f"CPY({source_str}, {self.target}{oneshot_str})"


class CopyBlockInstruction(OneShotCapableInstruction):
    """Copy Block Instruction (CPYBLK) - Copies a range of addresses to consecutive destinations"""

    def __init__(
        self,
        source_start: "PLCVariable",
        dest_start: "PLCVariable",
        count: int,
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)

        # Basic parameter validation
        if not isinstance(source_start, PLCVariable):
            raise TypeError(f"Source_start must be a PLCVariable, got {type(source_start)}")
        if not isinstance(dest_start, PLCVariable):
            raise TypeError(f"Dest_start must be a PLCVariable, got {type(dest_start)}")
        if not isinstance(count, int) or count <= 0:
            raise ValueError(f"Count must be a positive integer, got {count}")

        # Type compatibility validation using new method
        # Since all elements in a bank have the same compatibility, we only need to check once
        source_start.check_copy_allowed(dest_start)

        # Store parameters
        self.source_start = source_start
        self.dest_start = dest_start
        self.count = count

    def _perform_action(self, context: "PLCExecutionContext"):
        """Copy multiple consecutive values from source to destination"""
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context for CopyBlock execution")

        try:
            # Extract address components for iteration
            source_bank = self.source_start.address_type
            source_prefix = source_bank.name
            source_base_addr = int(self.source_start.address[len(source_prefix) :])

            dest_bank = self.dest_start.address_type
            dest_prefix = dest_bank.name
            dest_base_addr = int(self.dest_start.address[len(dest_prefix) :])

            # Perform the block copy
            for i in range(self.count):
                # Get current source and destination variables
                try:
                    current_source = source_bank[source_base_addr + i]
                except (IndexError, KeyError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[43].set_value(1)  # SC43: Out of Range
                    return

                # Get destination variable safely
                try:
                    current_dest = dest_bank[dest_base_addr + i]
                except (IndexError, KeyError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[43].set_value(1)  # SC43: Out of Range
                    return

                # Get the value from source
                value = current_source.get_value()

                # Copy to destination
                try:
                    current_dest.set_value(value)
                except (ValueError, TypeError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[44].set_value(1)  # SC44: Address Error
                    return

        except Exception as e:
            # General exception handler to prevent PLC program from crashing
            if plc and hasattr(plc, "SC"):
                plc.SC[44].set_value(1)  # SC44: Address Error

    def __str__(self):
        oneshot_str = ", ONS" if self.oneshot else ""
        return f"CPYBLK({self.source_start}, {self.dest_start}, K{self.count}{oneshot_str})"


class CopyFillInstruction(OneShotCapableInstruction):
    """Copy Fill Instruction (FILL) - Copies a single value to multiple consecutive addresses"""

    def __init__(
        self,
        source: Union["PLCVariable", Any],
        dest_start: "PLCVariable",
        count: int,
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)

        # Basic parameter validation
        if not isinstance(dest_start, PLCVariable):
            raise TypeError(f"Dest_start must be a PLCVariable, got {type(dest_start)}")
        if not isinstance(count, int) or count <= 0:
            raise ValueError(f"Count must be a positive integer, got {count}")

        # Type compatibility validation
        if isinstance(source, PLCVariable):
            # If source is a variable, check compatibility with destination
            source.check_copy_allowed(dest_start)
        else:
            # If source is a literal value, try to validate against destination type
            try:
                dest_start.address_type.data_type_def.validate(source)
            except (ValueError, TypeError) as e:
                raise TypeError(
                    f"Source value {source} cannot be converted to destination type "
                    f"{dest_start.address_type.data_type_def.__class__.__name__}: {e}"
                )

        # Store parameters
        self.source = source
        self.dest_start = dest_start
        self.count = count

    def _perform_action(self, context: "PLCExecutionContext"):
        """Fill multiple consecutive addresses with the same value"""
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context for Fill operation")

        # Get the source value once
        if isinstance(self.source, PLCVariable):
            try:
                fill_value = self.source.get_value()
            except Exception:
                if plc and hasattr(plc, "SC"):
                    plc.SC[44].set_value(1)  # SC44: Address Error
                return
        else:
            fill_value = self.source

        try:
            # Extract destination address components for iteration
            dest_bank = self.dest_start.address_type
            dest_prefix = dest_bank.name
            dest_base_addr = int(self.dest_start.address[len(dest_prefix) :])

            # Perform the fill operation
            for i in range(self.count):
                # Get current destination variable
                try:
                    current_dest = dest_bank[dest_base_addr + i]
                except (IndexError, KeyError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[43].set_value(1)  # SC43: Out of Range
                    return

                # Set the fill value to destination
                try:
                    current_dest.set_value(fill_value)
                except (ValueError, TypeError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[44].set_value(1)  # SC44: Address Error
                    return

        except Exception as e:
            # General exception handler to prevent PLC program from crashing
            if plc and hasattr(plc, "SC"):
                plc.SC[44].set_value(1)  # SC44: Address Error

    def __str__(self):
        source_str = str(self.source) if not isinstance(self.source, str) else f"'{self.source}'"
        oneshot_str = ", ONS" if self.oneshot else ""
        return f"FILL({source_str}, {self.dest_start}, K{self.count}{oneshot_str})"


class CopyPackInstruction(OneShotCapableInstruction):
    """Copy Pack Instruction (PACK) - Packs bits into a word data type"""

    def __init__(
        self,
        source_bit_start: "PLCVariable",
        dest_word: "PLCVariable",
        bit_count: int,
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)

        # Basic parameter validation
        if not isinstance(source_bit_start, PLCVariable):
            raise TypeError(f"Source_bit_start must be a PLCVariable, got {type(source_bit_start)}")
        if not isinstance(dest_word, PLCVariable):
            raise TypeError(f"Dest_word must be a PLCVariable, got {type(dest_word)}")
        if not isinstance(bit_count, int) or bit_count <= 0:
            raise ValueError(f"Bit_count must be a positive integer, got {bit_count}")

        # Use new compatibility checking method
        source_bit_start.check_pack_allowed(dest_word, bit_count)

        # Store parameters
        self.source_bit_start = source_bit_start
        self.dest_word = dest_word
        self.bit_count = bit_count

    def _perform_action(self, context: "PLCExecutionContext"):
        """Pack bits into a word"""
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context for Pack operation")

        try:
            # Extract source address components for iteration
            source_bank = self.source_bit_start.address_type
            source_prefix = source_bank.name
            source_base_addr = int(self.source_bit_start.address[len(source_prefix) :])

            # Initialize packed value
            packed_value = 0

            # Read bits and build the packed value
            for i in range(self.bit_count):
                try:
                    # Get current bit
                    current_bit = source_bank[source_base_addr + i]
                except (IndexError, KeyError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[43].set_value(1)  # SC43: Out of Range
                    return

                # Read bit value
                bit_value = current_bit.get_value()

                # Add to packed value - LSB first (bit 0 is rightmost)
                # This is a common convention but could be adjusted if MSB first is needed
                packed_value |= (bit_value & 1) << i

            # Store result in destination word
            try:
                self.dest_word.set_value(packed_value)
            except (ValueError, TypeError):
                if plc and hasattr(plc, "SC"):
                    plc.SC[44].set_value(1)  # SC44: Address Error

        except Exception as e:
            # General exception handler
            if plc and hasattr(plc, "SC"):
                plc.SC[44].set_value(1)  # SC44: Address Error

    def __str__(self):
        oneshot_str = ", ONS" if self.oneshot else ""
        # Calculate source end address for display
        source_prefix = self.source_bit_start.address_type.name
        source_base = int(self.source_bit_start.address[len(source_prefix) :])
        source_end = f"{source_prefix}{source_base + self.bit_count - 1}"

        return f"PACK({self.source_bit_start} to {source_end}, {self.dest_word}{oneshot_str})"


class CopyUnpackInstruction(OneShotCapableInstruction):
    """Copy Unpack Instruction (UNPACK) - Unpacks word data type into bits"""

    def __init__(
        self,
        source_word: "PLCVariable",
        dest_bit_start: "PLCVariable",
        bit_count: int,
        oneshot: bool = False,
    ):
        super().__init__(oneshot=oneshot)

        # Basic parameter validation
        if not isinstance(source_word, PLCVariable):
            raise TypeError(f"Source_word must be a PLCVariable, got {type(source_word)}")
        if not isinstance(dest_bit_start, PLCVariable):
            raise TypeError(f"Dest_bit_start must be a PLCVariable, got {type(dest_bit_start)}")
        if not isinstance(bit_count, int) or bit_count <= 0:
            raise ValueError(f"Bit_count must be a positive integer, got {bit_count}")

        # Use new compatibility checking method
        source_word.check_unpack_allowed(dest_bit_start, bit_count)

        # Store parameters
        self.source_word = source_word
        self.dest_bit_start = dest_bit_start
        self.bit_count = bit_count

    def _perform_action(self, context: "PLCExecutionContext"):
        """Unpack a word into bits"""
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context for Unpack operation")

        try:
            # Get source word value
            try:
                word_value = self.source_word.get_value()
            except Exception:
                if plc and hasattr(plc, "SC"):
                    plc.SC[44].set_value(1)  # SC44: Address Error
                return

            # Extract destination address components for iteration
            dest_bank = self.dest_bit_start.address_type
            dest_prefix = dest_bank.name
            dest_base_addr = int(self.dest_bit_start.address[len(dest_prefix) :])

            # Unpack the value into individual bits
            for i in range(self.bit_count):
                try:
                    # Get current destination bit
                    current_bit = dest_bank[dest_base_addr + i]
                except (IndexError, KeyError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[43].set_value(1)  # SC43: Out of Range
                    return

                # Extract bit from word - LSB first (bit 0 is rightmost)
                bit_value = (word_value >> i) & 1

                # Write bit to destination
                try:
                    current_bit.set_value(bit_value)
                except (ValueError, TypeError):
                    if plc and hasattr(plc, "SC"):
                        plc.SC[44].set_value(1)  # SC44: Address Error
                    return

        except Exception as e:
            # General exception handler
            if plc and hasattr(plc, "SC"):
                plc.SC[44].set_value(1)  # SC44: Address Error

    def __str__(self):
        oneshot_str = ", ONS" if self.oneshot else ""
        # Calculate destination end address for display
        dest_prefix = self.dest_bit_start.address_type.name
        dest_base = int(self.dest_bit_start.address[len(dest_prefix) :])
        dest_end = f"{dest_prefix}{dest_base + self.bit_count - 1}"

        return f"UNPACK({self.source_word}, {self.dest_bit_start} to {dest_end}{oneshot_str})"


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
