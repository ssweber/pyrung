from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Set, Union


class PLCDataTypeEnum(Enum):
    """Enum representing the PLC data types"""

    BIT = 1
    INT = 2
    INT2 = 3
    FLOAT = 4
    HEX = 5
    TXT = 6


class DataTypeDefinition(ABC):
    """Abstract base class defining the interface for a PLC data type"""

    @abstractmethod
    def validate(self, value: Any) -> Any:
        """Validates and converts a value to the correct type"""
        pass

    @abstractmethod
    def format_value(self, value: Any) -> str:
        """Formats a value for display"""
        pass

    @abstractmethod
    def default_value(self) -> Any:
        """Returns the default value for this data type"""
        pass

    @abstractmethod
    def get_allowed_operations(self) -> Set[str]:
        """Returns a set of string identifiers for allowed operations, e.g., '==', 'bool', '+'"""
        pass


class BitType(DataTypeDefinition):
    """Definition for binary (bit) data type"""

    def validate(self, value: Any) -> int:
        if value not in (0, 1, True, False):
            raise ValueError(f"Bit value must be 0, 1, True, or False, got {value}")
        return 1 if value else 0

    def format_value(self, value: Any) -> str:
        return "1" if value else "0"

    def default_value(self) -> int:
        return 0

    def get_allowed_operations(self) -> Set[str]:
        return {"bool"}  # Can be used in boolean context


class IntType(DataTypeDefinition):
    """Definition for integer data type"""

    MIN_VAL = -32768
    MAX_VAL = 32767

    def validate(self, value: Any) -> int:
        if not isinstance(value, (int, float)):  # Allow float for implicit conversion
            raise ValueError(f"INT value must be a number, got {type(value)}")
        int_value = int(value)
        if not (self.MIN_VAL <= int_value <= self.MAX_VAL):
            raise ValueError(
                f"INT value must be between {self.MIN_VAL} and {self.MAX_VAL}, got {value}"
            )
        return int_value

    def format_value(self, value: Any) -> str:
        return str(value)

    def default_value(self) -> int:
        return 0

    def get_allowed_operations(self) -> Set[str]:
        return {"==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "//", "%", "**"}


class Int2Type(DataTypeDefinition):
    """Definition for extended integer (INT2) data type"""

    MIN_VAL = -2147483648
    MAX_VAL = 2147483647

    def validate(self, value: Any) -> int:
        if not isinstance(value, (int, float)):
            raise ValueError(f"INT2 value must be a number, got {type(value)}")
        int_value = int(value)
        if not (self.MIN_VAL <= int_value <= self.MAX_VAL):
            raise ValueError(
                f"INT2 value must be between {self.MIN_VAL} and {self.MAX_VAL}, got {value}"
            )
        return int_value

    def format_value(self, value: Any) -> str:
        return str(value)

    def default_value(self) -> int:
        return 0

    def get_allowed_operations(self) -> Set[str]:
        return {"==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "//", "%", "**"}


class FloatType(DataTypeDefinition):
    """Definition for floating point data type"""

    MIN_VAL = -3.4028235e38
    MAX_VAL = 3.4028235e38

    def validate(self, value: Any) -> float:
        if not isinstance(value, (int, float)):
            raise ValueError(f"FLOAT value must be a number, got {type(value)}")
        float_value = float(value)
        if not (self.MIN_VAL <= float_value <= self.MAX_VAL):
            raise ValueError(
                f"FLOAT value must be between {self.MIN_VAL} and {self.MAX_VAL}, got {value}"
            )
        return float_value

    def format_value(self, value: Any) -> str:
        return f"{value:.6f}"

    def default_value(self) -> float:
        return 0.0

    def get_allowed_operations(self) -> Set[str]:
        return {"==", "!=", "<", "<=", ">", ">=", "+", "-", "*", "/", "**"}


class HexType(DataTypeDefinition):
    """Definition for hexadecimal data type"""

    def validate(self, value: Any) -> int:
        if isinstance(value, str):
            # Remove 'h' suffix if present
            if value.endswith("h"):
                value = value[:-1]
            # Add '0x' prefix if not present
            if not value.startswith("0x"):
                value = "0x" + value
            try:
                int_value = int(value, 16)
            except ValueError:
                raise ValueError(f"Invalid HEX value: {value}")
        elif isinstance(value, int):
            int_value = value
        else:
            raise ValueError(f"HEX value must be a string or integer, got {type(value)}")

        if int_value < 0 or int_value > 0xFFFF:
            raise ValueError(f"HEX value must be between 0 and FFFF, got {value}")
        return int_value

    def format_value(self, value: Any) -> str:
        return f"{value:04X}h"

    def default_value(self) -> int:
        return 0

    def get_allowed_operations(self) -> Set[str]:
        return {"==", "!=", "<", "<=", ">", ">=", "&", "|", "^", "<<", ">>"}


class TxtType(DataTypeDefinition):
    """Definition for text (character) data type"""

    def validate(self, value: Any) -> str:
        if isinstance(value, int):
            if not (0 <= value <= 255):  # Assuming ASCII
                raise ValueError(f"ASCII value must be between 0 and 255, got {value}")
            return chr(value)
        elif not isinstance(value, str):
            raise ValueError(f"TXT value must be a string or ASCII int, got {type(value)}")
        if len(value) != 1:
            raise ValueError(f"TXT value must be a single character, got '{value}'")
        return value

    def format_value(self, value: Any) -> str:
        return str(value)

    def default_value(self) -> str:
        return ""  # empty as default

    def get_allowed_operations(self) -> Set[str]:
        return {"==", "!="}  # Typically only equality for single chars in PLC context
