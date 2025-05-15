from abc import ABC, abstractmethod
from typing import Any, Callable, Union, List, Optional

# Forward references
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from memory_model import PLCVariable, PLCExecutionContext

class Instruction(ABC):
    """Base class for all PLC instructions"""
    
    @abstractmethod
    def execute(self, context: 'PLCExecutionContext'):
        """Execute the instruction"""
        pass


class OneShotCapableInstruction(Instruction):
    """Base class for instructions that can operate in one-shot mode"""
    
    def __init__(self, oneshot: bool = False):
        self.oneshot = oneshot
        self._has_executed_this_rung_true_cycle = False

    def execute(self, context: 'PLCExecutionContext'):
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
    def _perform_action(self, context: 'PLCExecutionContext'):
        """The core action of the instruction, to be implemented by subclasses"""
        pass

    def reset_oneshot_trigger(self):
        """Reset the oneshot execution state when rung goes false"""
        self._has_executed_this_rung_true_cycle = False


class OutInstruction(OneShotCapableInstruction):
    """Output Coil Instruction (OUT)"""
    
    def __init__(self, target: 'PLCVariable', oneshot: bool = False):
        super().__init__(oneshot=oneshot)
        self.target = target
        # Could add validation for bit type here

    def _perform_action(self, context: 'PLCExecutionContext'):
        """Set the target bit to 1"""
        self.target.set_value(1)

    def __str__(self):
        oneshot_str = ", oneshot=True" if self.oneshot else ""
        return f"OUT({self.target}{oneshot_str})"


class LatchInstruction(Instruction):
    """Latch Coil Instruction (SET)"""
    
    def __init__(self, target: 'PLCVariable'):
        self.target = target
        # Could add validation for bit type here
        
        # Mark the target as latched in its address type (if supported)
        if hasattr(self.target.address_type, 'mark_as_latched'):
            self.target.address_type.mark_as_latched(self.target.address)

    def execute(self, context: 'PLCExecutionContext'):
        """Set the target bit to 1 (and keep it on even if rung goes false)"""
        self.target.set_value(1)

    def __str__(self):
        return f"SET({self.target})"


class ResetInstruction(Instruction):
    """Unlatch Coil Instruction (RST)"""
    
    def __init__(self, target: 'PLCVariable'):
        self.target = target
        # Could add validation for bit type here

    def execute(self, context: 'PLCExecutionContext'):
        """Reset the target to its default value"""
        self.target.set_value(self.target.address_type.data_type_def.default_value())

    def __str__(self):
        return f"RST({self.target})"


class CopyInstruction(OneShotCapableInstruction):
    """Copy Instruction (MOV/CPY)"""
    
    def __init__(self, source: Union['PLCVariable', Any], target: 'PLCVariable', oneshot: bool = False):
        super().__init__(oneshot=oneshot)
        self.source = source
        self.target = target
        # Type compatibility could be validated here

    def _perform_action(self, context: 'PLCExecutionContext'):
        """Copy the source value to the target"""
        if hasattr(self.source, 'get_value'):
            value = self.source.get_value()
        else:
            value = self.source
        self.target.set_value(value)

    def __str__(self):
        source_str = str(self.source) if not isinstance(self.source, str) else f"'{self.source}'"
        oneshot_str = ", oneshot=True" if self.oneshot else ""
        return f"CPY({source_str}, {self.target}{oneshot_str})"


class MathInstruction(OneShotCapableInstruction):
    """Math Instruction (MATH)"""
    
    def __init__(self, expression_func: Callable[[], Any], target: 'PLCVariable', oneshot: bool = False):
        super().__init__(oneshot=oneshot)
        self.expression_func = expression_func
        self.target = target

    def _perform_action(self, context: 'PLCExecutionContext'):
        """Calculate the expression and store in the target"""
        result = self.expression_func()
        self.target.set_value(result)

    def __str__(self):
        oneshot_str = ", oneshot=True" if self.oneshot else ""
        return f"MATH(<expression>, {self.target}{oneshot_str})"