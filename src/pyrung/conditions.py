from abc import ABC, abstractmethod
from typing import Any, Callable, Union
import operator  # For comparison functions

# Forward references
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memory_model import PLCVariable, PLCExecutionContext
    from datatypes import BitType


class Condition(ABC):
    """Base class for all rung conditions"""

    @abstractmethod
    def evaluate(self, context: "PLCExecutionContext") -> bool:
        """Evaluate whether this condition is true"""
        pass


class ComparisonCondition(Condition):
    """Condition that compares two values"""

    def __init__(
        self, lhs: "PLCVariable", op: Callable[[Any, Any], bool], rhs: Union["PLCVariable", Any]
    ):
        self.lhs = lhs
        self.op = op
        self.rhs = rhs
        # Type compatibility could be checked here

    def evaluate(self, context: "PLCExecutionContext") -> bool:
        """Compare the left and right values using the operator"""
        lhs_val = self.lhs.get_value()
        rhs_val = self.rhs.get_value() if hasattr(self.rhs, "get_value") else self.rhs
        return self.op(lhs_val, rhs_val)

    def __str__(self):
        """String representation of the condition"""
        op_name = self.op.__name__
        rhs_str = str(self.rhs) if not isinstance(self.rhs, str) else f"'{self.rhs}'"
        return f"{self.lhs} {op_name} {rhs_str}"


class BitCondition(Condition):
    """Normally open contact (XIC)"""

    def __init__(self, variable: "PLCVariable"):
        from datatypes import BitType

        if not isinstance(variable.address_type.data_type_def, BitType):
            raise TypeError(
                f"BitCondition requires a BIT type variable, got "
                f"{variable.address_type.data_type_def.__class__.__name__} for {variable}"
            )
        self.variable = variable

    def evaluate(self, context: "PLCExecutionContext") -> bool:
        """Return true if the bit is on (1)"""
        return bool(self.variable.get_value())

    def __str__(self):
        return f"XIC({self.variable})"


class NormallyClosedCondition(Condition):
    """Normally closed contact (XIO)"""

    def __init__(self, variable: "PLCVariable"):
        from datatypes import BitType

        if not isinstance(variable.address_type.data_type_def, BitType):
            raise TypeError(
                f"NormallyClosedCondition requires a BIT type variable, got "
                f"{variable.address_type.data_type_def.__class__.__name__} for {variable}"
            )
        self.variable = variable

    def evaluate(self, context: "PLCExecutionContext") -> bool:
        """Return true if the bit is off (0)"""
        return not bool(self.variable.get_value())

    def __str__(self):
        return f"XIO({self.variable})"


class RisingEdgeCondition(Condition):
    """One-shot rising edge detection (ONS)"""

    def __init__(self, variable: "PLCVariable"):
        from datatypes import BitType

        if not isinstance(variable.address_type.data_type_def, BitType):
            raise TypeError(f"RisingEdgeCondition requires a BIT type variable, got {variable}")
        self.variable = variable

    def evaluate(self, context: "PLCExecutionContext") -> bool:
        """Return true only on transition from off to on"""
        current_val = bool(self.variable.get_value())
        previous_val = bool(self.variable.get_previous_value())
        return current_val and not previous_val

    def __str__(self):
        return f"ONS({self.variable})"


class FallingEdgeCondition(Condition):
    """One-shot falling edge detection"""

    def __init__(self, variable: "PLCVariable"):
        from datatypes import BitType

        if not isinstance(variable.address_type.data_type_def, BitType):
            raise TypeError(f"FallingEdgeCondition requires a BIT type variable, got {variable}")
        self.variable = variable

    def evaluate(self, context: "PLCExecutionContext") -> bool:
        """Return true only on transition from on to off"""
        current_val = bool(self.variable.get_value())
        previous_val = bool(self.variable.get_previous_value())
        return not current_val and previous_val

    def __str__(self):
        return f"FE({self.variable})"
