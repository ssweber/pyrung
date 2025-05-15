from abc import ABC, abstractmethod
from typing import Any, Dict, Union, Optional, Set, Callable, List, Tuple
import operator  # For comparison operators

# Import datatypes
from datatypes import BitType, IntType, Int2Type, FloatType, HexType, TxtType, DataTypeDefinition

# Canonical address representation
Address = str

class PLCMemory:
    """Centralized storage for all PLC data values"""
    
    def __init__(self):
        self._data: Dict[Address, Any] = {}
        self._previous_scan_data: Dict[Address, Any] = {}  # For edge detection

    def read(self, address: Address, default_value: Any = None) -> Any:
        """Read a value from memory"""
        return self._data.get(address, default_value)

    def write(self, address: Address, value: Any):
        """Write a value to memory"""
        self._data[address] = value

    def get_previous_value(self, address: Address, default_value: Any = None) -> Any:
        """Get the value from the previous scan cycle"""
        return self._previous_scan_data.get(address, default_value)

    def end_scan_cycle(self):
        """Called at the end of a PLC scan to update previous values"""
        self._previous_scan_data = self._data.copy()

class PLCExecutionContext:
    """Context for instruction execution and condition evaluation"""
    
    def __init__(self, memory: PLCMemory):
        self.memory = memory
        # Could also store current rung status, etc.

class PLCVariable:
    """Represents a variable in the PLC system"""
    
    def __init__(self, address: Address, address_type: 'AddressType', plc_memory: PLCMemory):
        self.address = address
        self.address_type = address_type
        self.plc_memory = plc_memory
        self.nickname: Optional[str] = None

    def get_value(self) -> Any:
        """Get the current value of this variable"""
        return self.plc_memory.read(
            self.address, 
            self.address_type.data_type_def.default_value()
        )

    def set_value(self, value: Any):
        """Set the value of this variable"""
        validated_value = self.address_type.data_type_def.validate(value)
        self.plc_memory.write(self.address, validated_value)

    def get_previous_value(self) -> Any:
        """Get the value from the previous scan cycle"""
        return self.plc_memory.get_previous_value(
            self.address, 
            self.address_type.data_type_def.default_value()
        )

    def _check_op_allowed(self, op_name: str):
        """Check if an operation is allowed for this variable's data type"""
        if op_name not in self.address_type.data_type_def.get_allowed_operations():
            raise TypeError(
                f"Operation '{op_name}' not allowed for data type "
                f"{self.address_type.data_type_def.__class__.__name__} at "
                f"{self.address_type.name}{self.address}"
            )
        
    # Comparison operators
    def __eq__(self, other):
        from conditions import ComparisonCondition
        self._check_op_allowed('==')
        return ComparisonCondition(self, operator.eq, other)

    def __ne__(self, other):
        from conditions import ComparisonCondition
        self._check_op_allowed('!=')
        return ComparisonCondition(self, operator.ne, other)

    def __lt__(self, other):
        from conditions import ComparisonCondition
        self._check_op_allowed('<')
        return ComparisonCondition(self, operator.lt, other)

    def __le__(self, other):
        from conditions import ComparisonCondition
        self._check_op_allowed('<=')
        return ComparisonCondition(self, operator.le, other)

    def __gt__(self, other):
        from conditions import ComparisonCondition
        self._check_op_allowed('>')
        return ComparisonCondition(self, operator.gt, other)

    def __ge__(self, other):
        from conditions import ComparisonCondition
        self._check_op_allowed('>=')
        return ComparisonCondition(self, operator.ge, other)
    
    def __hash__(self):
        """Make PLCVariable instances hashable based on their address."""
        return hash(self.address)


    # Boolean conversion for use in Python if statements
    def __bool__(self) -> bool:
        self._check_op_allowed('bool')
        return bool(self.get_value())

    # Arithmetic operators
    def __add__(self, other):
        self._check_op_allowed('+')
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() + oval

    def __sub__(self, other):
        self._check_op_allowed('-')
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() - oval

    def __mul__(self, other):
        self._check_op_allowed('*')
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() * oval

    def __truediv__(self, other):
        self._check_op_allowed('/')
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() / oval

    def __floordiv__(self, other):
        self._check_op_allowed('//')
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() // oval

    def __mod__(self, other):
        self._check_op_allowed('%')
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() % oval

    def __pow__(self, other):
        self._check_op_allowed('**')
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() ** oval

    # String representation
    def __str__(self) -> str:
        if self.nickname:
            return f"{self.address_type.name}.{self.nickname} ({self.address})"
        return f"{self.address_type.name}{self.address}"

    def __repr__(self) -> str:
        return f"PLCVariable(address='{self.address}', type='{self.address_type.name}', nickname='{self.nickname}')"



class AddressType(ABC):
    """Base class for different PLC address types (X, Y, DS, etc.)"""
    
    def __init__(self, name: str, data_type_def: DataTypeDefinition,
                 start_addr: int, end_addr: int, plc_memory: PLCMemory,
                 is_retentive: bool = False):
        self.name = name  # e.g., "X", "Y", "DS"
        self.data_type_def = data_type_def
        self.start_addr = start_addr
        self.end_addr = end_addr
        self.size = (end_addr - start_addr) + 1
        self.plc_memory = plc_memory
        self.is_retentive = is_retentive
        self._nicknames: Dict[str, Address] = {}  # Maps nicknames to addresses
        self._variables: Dict[Address, PLCVariable] = {}  # Caches PLCVariable instances

    def _make_address_str(self, index: int) -> Address:
        """Convert an index to a canonical address string"""
        return f"{self.name}{index}"

    def _parse_key(self, key: Union[int, str]) -> Address:
        """Parse an index or nickname into a canonical address"""
        if isinstance(key, str):  # Potentially a nickname
            if key in self._nicknames:
                return self._nicknames[key]
            # Try to interpret as direct address
            if key.startswith(self.name) and key[len(self.name):].isdigit():
                index = int(key[len(self.name):])
                if self.start_addr <= index <= self.end_addr:
                    return key
            raise KeyError(f"Nickname or address '{key}' not defined for {self.name}")
        elif isinstance(key, int):
            if not (self.start_addr <= key <= self.end_addr):
                raise IndexError(f"Address {self.name}{key} out of range ({self.start_addr}-{self.end_addr})")
            return self._make_address_str(key)
        raise TypeError(f"Invalid key type for address: {key}")

    def __getitem__(self, key: Union[int, str]) -> PLCVariable:
        """Access a variable by index or nickname, e.g., x[1] or x['Button']"""
        address_str = self._parse_key(key)
        if address_str not in self._variables:
            var = PLCVariable(address_str, self, self.plc_memory)
            if isinstance(key, str) and address_str == self._nicknames.get(key):
                var.nickname = key
            self._variables[address_str] = var
        return self._variables[address_str]

    def __setitem__(self, key: Union[int, str], nickname_or_value: Union[str, Any]):
        """
        If value is str, assign as nickname: x[1] = "Button"
        If value is data, set the variable's value: x[1] = True
        """
        address_str = self._parse_key(key)
        if isinstance(nickname_or_value, str) and not address_str.startswith(nickname_or_value):
            # Assigning a nickname
            if nickname_or_value in self._nicknames and self._nicknames[nickname_or_value] != address_str:
                raise ValueError(f"Nickname '{nickname_or_value}' already assigned to {self._nicknames[nickname_or_value]}")
            # Ensure the variable object exists for this address
            if address_str not in self._variables:
                self._variables[address_str] = PLCVariable(address_str, self, self.plc_memory)
            self._variables[address_str].nickname = nickname_or_value
            self._nicknames[nickname_or_value] = address_str
        else:  # Setting a value directly
            var = self[key]  # Get or create PLCVariable
            var.set_value(nickname_or_value)

    def __getattr__(self, name: str) -> PLCVariable:
        """Access a variable by nickname via attribute, e.g., x.Button"""
        if name in self._nicknames:
            address_str = self._nicknames[name]
            if address_str not in self._variables:
                self._variables[address_str] = PLCVariable(address_str, self, self.plc_memory)
                self._variables[address_str].nickname = name
            return self._variables[address_str]
        raise AttributeError(f"'{self.name}' address type has no nickname '{name}'")

    @abstractmethod
    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        """
        Defines behavior when a rung controlling an output of this type becomes false.
        This is triggered by the Rung execution logic.
        """
        pass


class XBank(AddressType):
    """Input Bits (X addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("X", BitType(), 1, 816, plc_memory, is_retentive=False)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # Inputs are not typically "reset" by rung logic, so do nothing
        pass


class YBank(AddressType):
    """Output Bits (Y addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("Y", BitType(), 1, 816, plc_memory, is_retentive=False)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # Standard behavior for non-retentive outputs: turn OFF if rung is false
        if not self.is_retentive:
            variable.set_value(self.data_type_def.default_value())  # Sets to 0 for BitType


class CBank(AddressType):
    """Control Bits (C addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("C", BitType(), 1, 2000, plc_memory, is_retentive=False)
        self._latched_addresses: Set[Address] = set()  # Track which C bits were set and shouldn't auto-reset

    def mark_as_latched(self, address: Address):
        """Mark an address as having been set with a set() instruction"""
        self._latched_addresses.add(address)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # If the bit was set with set() instruction, don't reset it when rung goes false
        if not self.is_retentive and variable.address not in self._latched_addresses:
            variable.set_value(self.data_type_def.default_value())


class DSBank(AddressType):
    """Data Store Integers (DS addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("DS", IntType(), 1, 4500, plc_memory, is_retentive=True)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DS is retentive, so do nothing when rung goes false
        pass


class DDBank(AddressType):
    """Double Data Integers (DD addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("DD", Int2Type(), 1, 1000, plc_memory, is_retentive=True)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DD is retentive, so do nothing when rung goes false
        pass


class DFBank(AddressType):
    """Float Data (DF addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("DF", FloatType(), 1, 500, plc_memory, is_retentive=True)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DF is retentive, so do nothing when rung goes false
        pass


class DHBank(AddressType):
    """Hex Data (DH addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("DH", HexType(), 1, 500, plc_memory, is_retentive=True)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DH is retentive, so do nothing when rung goes false
        pass


class TXTBank(AddressType):
    """Text Character Data (TXT addresses)"""
    
    def __init__(self, plc_memory: PLCMemory):
        super().__init__("TXT", TxtType(), 1, 500, plc_memory, is_retentive=True)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # TXT is retentive, so do nothing when rung goes false
        pass