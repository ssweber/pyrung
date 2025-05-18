from abc import ABC, abstractmethod
from typing import Any, Dict, Union, Optional, Set, Callable, List, Tuple
import operator  # For comparison operators

from datatypes import BitType, IntType, Int2Type, FloatType, HexType, TxtType, DataTypeDefinition
from system_nicknames import SYSTEM_CONTROL_NICKNAMES, SYSTEM_DATA_NICKNAMES


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

    def __init__(
        self,
        address: Address,
        address_type: "AddressType",
        plc_memory: PLCMemory,
        initial_value: Any = None,  # New parameter
        is_retentive: Optional[bool] = None,
    ):  # New parameter
        self.address = address
        self.address_type = address_type
        self.plc_memory = plc_memory
        self.nickname: Optional[str] = None

        # Use provided value or get default from data type
        if initial_value is None:
            initial_value = self.address_type.data_type_def.default_value()

        # Set initial value (validated)
        self._initial_value = self.address_type.data_type_def.validate(initial_value)

        # Set retentive status (use bank default if not specified)
        if is_retentive is None:
            self._is_retentive = self.address_type.default_retentive
        else:
            # Check if this address type allows retentive configuration
            if (
                not self.address_type.allows_retentive_config
                and is_retentive != self.address_type.default_retentive
            ):
                raise ValueError(
                    f"Retentive status for address type {self.address_type.name} "
                    f"({self.address}) cannot be changed from its default."
                )
            self._is_retentive = is_retentive

        # Initialize memory with initial value if not already set
        if self.plc_memory.read(self.address) is None:
            self.plc_memory.write(self.address, self._initial_value)

    def get_value(self) -> Any:
        """Get the current value of this variable"""
        return self.plc_memory.read(self.address, self._initial_value)

    def set_value(self, value: Any):
        """Set the value of this variable"""
        validated_value = self.address_type.data_type_def.validate(value)
        self.plc_memory.write(self.address, validated_value)

    def get_previous_value(self) -> Any:
        """Get the value from the previous scan cycle"""
        return self.plc_memory.get_previous_value(self.address, self._initial_value)

    @property
    def is_retentive(self) -> bool:
        """Get the retentive status of this variable"""
        return self._is_retentive

    @property
    def initial_value(self) -> Any:
        """Get the initial value of this variable"""
        return self._initial_value

    def configure(self, initial_value: Optional[Any] = None, is_retentive: Optional[bool] = None):
        """Update the variable's configuration"""
        if initial_value is not None:
            self._initial_value = self.address_type.data_type_def.validate(initial_value)

        if is_retentive is not None:
            if not self.address_type.allows_retentive_config:
                raise ValueError(
                    f"Retentive status for address type {self.address_type.name} "
                    f"({self.address}) cannot be changed."
                )
            self._is_retentive = is_retentive

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

        self._check_op_allowed("==")
        return ComparisonCondition(self, operator.eq, other)

    def __ne__(self, other):
        from conditions import ComparisonCondition

        self._check_op_allowed("!=")
        return ComparisonCondition(self, operator.ne, other)

    def __lt__(self, other):
        from conditions import ComparisonCondition

        self._check_op_allowed("<")
        return ComparisonCondition(self, operator.lt, other)

    def __le__(self, other):
        from conditions import ComparisonCondition

        self._check_op_allowed("<=")
        return ComparisonCondition(self, operator.le, other)

    def __gt__(self, other):
        from conditions import ComparisonCondition

        self._check_op_allowed(">")
        return ComparisonCondition(self, operator.gt, other)

    def __ge__(self, other):
        from conditions import ComparisonCondition

        self._check_op_allowed(">=")
        return ComparisonCondition(self, operator.ge, other)

    def __hash__(self):
        """Make PLCVariable instances hashable based on their address."""
        return hash(self.address)

    # Boolean conversion for use in Rung/Branch statements
    def __bool__(self) -> bool:
        self._check_op_allowed("bool")
        return bool(self.get_value())

    # Arithmetic operators
    def __add__(self, other):
        self._check_op_allowed("+")
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() + oval

    def __sub__(self, other):
        self._check_op_allowed("-")
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() - oval

    def __mul__(self, other):
        self._check_op_allowed("*")
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() * oval

    def __truediv__(self, other):
        self._check_op_allowed("/")
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() / oval

    def __floordiv__(self, other):
        self._check_op_allowed("//")
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() // oval

    def __mod__(self, other):
        self._check_op_allowed("%")
        oval = other.get_value() if isinstance(other, PLCVariable) else other
        return self.get_value() % oval

    def __pow__(self, other):
        self._check_op_allowed("**")
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

    def __init__(
        self,
        name: str,
        data_type_def: DataTypeDefinition,
        start_addr: int,
        end_addr: int,
        plc_memory: PLCMemory,
        default_retentive: bool = False,
        allows_retentive_config: bool = True,
    ):
        self.name = name  # e.g., "X", "Y", "DS"
        self.data_type_def = data_type_def
        self.start_addr = start_addr
        self.end_addr = end_addr
        self.size = (end_addr - start_addr) + 1
        self.plc_memory = plc_memory
        self.default_retentive = default_retentive
        self.allows_retentive_config = allows_retentive_config

        self._address_to_nickname: Dict[Address, str] = {}  # Maps addresses to nicknames
        self._nickname_to_address: Dict[str, Address] = {}  # Maps nicknames to addresses
        self._variables: Dict[Address, PLCVariable] = {}  # Caches PLCVariable instances

        # Per-address configuration storage
        self._per_address_retentive: Dict[Address, bool] = {}
        self._per_address_initial_value: Dict[Address, Any] = {}

    def _make_address_str(self, index: int) -> Address:
        """Convert an index to a canonical address string"""
        return f"{self.name}{index}"

    def _parse_key(self, key: Union[int, str]) -> Address:
        """Parse an index or nickname into a canonical address"""
        if isinstance(key, str):  # Potentially a nickname
            if key in self._nickname_to_address:
                return self._nickname_to_address[key]
            # Try to interpret as direct address
            if key.startswith(self.name) and key[len(self.name) :].isdigit():
                index = int(key[len(self.name) :])
                if self.start_addr <= index <= self.end_addr:
                    return key
            raise KeyError(f"Nickname or address '{key}' not defined for {self.name}")
        elif isinstance(key, int):
            if not (self.start_addr <= key <= self.end_addr):
                raise IndexError(
                    f"Address {self.name}{key} out of range ({self.start_addr}-{self.end_addr})"
                )
            return self._make_address_str(key)
        raise TypeError(f"Invalid key type for address: {key}")

    def set_address_retentive(self, key: Union[int, str], is_retentive: bool):
        """Configure retentive status for a specific address"""
        if not self.allows_retentive_config:
            raise ValueError(f"Retentive status for {self.name} addresses cannot be configured.")
        address_str = self._parse_key(key)
        self._per_address_retentive[address_str] = is_retentive
        # Update variable if it exists
        if address_str in self._variables:
            self._variables[address_str].configure(is_retentive=is_retentive)

    def set_address_initial_value(self, key: Union[int, str], initial_value: Any):
        """Configure initial value for a specific address"""
        address_str = self._parse_key(key)
        validated_value = self.data_type_def.validate(initial_value)
        self._per_address_initial_value[address_str] = validated_value
        # Update variable if it exists
        if address_str in self._variables:
            self._variables[address_str].configure(initial_value=validated_value)

    def get_address_retentive(self, address_str: Address) -> bool:
        """Get retentive status for a specific address (with fallback to default)"""
        return self._per_address_retentive.get(address_str, self.default_retentive)

    def get_address_initial_value(self, address_str: Address) -> Any:
        """Get initial value for a specific address (with fallback to default)"""
        return self._per_address_initial_value.get(address_str, self.data_type_def.default_value())

    def __getitem__(self, key: Union[int, str]) -> PLCVariable:
        """Access a variable by index or nickname, e.g., x[1] or x['Button']"""
        address_str = self._parse_key(key)
        if address_str not in self._variables:
            # Get the configured or default values for this address
            is_retentive = self.get_address_retentive(address_str)
            initial_value = self.get_address_initial_value(address_str)

            # Create the variable with these values
            var = PLCVariable(
                address_str,
                self,
                self.plc_memory,
                initial_value=initial_value,
                is_retentive=is_retentive,
            )

            # Set nickname if applicable
            if address_str in self._address_to_nickname:
                var.nickname = self._address_to_nickname[address_str]

            self._variables[address_str] = var

        return self._variables[address_str]

    def __setitem__(self, key: Union[int, str], nickname_or_value: Union[str, Any]):
        """
        If value is str, assign as nickname: x[1] = "Button"
        If value is data, set the variable's value: x[1] = True
        """
        address_str = self._parse_key(key)

        # Determine if this is a nickname assignment or value assignment
        is_nickname = False
        if isinstance(nickname_or_value, str):
            try:
                # Try to validate as a value
                self.data_type_def.validate(nickname_or_value)
            except (ValueError, TypeError):
                # If validation fails, it's likely a nickname
                is_nickname = True

            # Special case: don't treat address strings as nicknames (e.g., x[1] = "X1")
            if nickname_or_value.startswith(self.name):
                is_nickname = False

            # Check for system nicknames
            if (
                hasattr(self, "_default_nicknames")
                and nickname_or_value in getattr(self, "_default_nicknames", {}).values()
            ):
                is_nickname = True

        if is_nickname:
            # Assigning a nickname
            if (
                nickname_or_value in self._nickname_to_address
                and self._nickname_to_address[nickname_or_value] != address_str
            ):
                raise ValueError(
                    f"Nickname '{nickname_or_value}' already assigned to {self._nickname_to_address[nickname_or_value]}"
                )

            # Get or create the variable
            var = self[key]
            var.nickname = nickname_or_value

            # Update mappings
            self._address_to_nickname[address_str] = nickname_or_value
            self._nickname_to_address[nickname_or_value] = address_str
        else:
            # Setting a value
            var = self[key]
            var.set_value(nickname_or_value)

    def __getattr__(self, name: str) -> PLCVariable:
        """Access a variable by nickname via attribute, e.g., x.Button"""
        if name in self._nickname_to_address:
            return self[name]  # This will use __getitem__
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
        super().__init__(
            "X",
            BitType(),
            1,
            816,
            plc_memory,
            default_retentive=False,  # X inputs are non-retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # Inputs are not typically "reset" by rung logic, so do nothing
        pass


class YBank(AddressType):
    """Output Bits (Y addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "Y",
            BitType(),
            1,
            816,
            plc_memory,
            default_retentive=False,  # Y outputs are non-retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )
        self._latched_addresses: Set[Address] = set()  # Track which Y bits were set with latch()

    def mark_as_latched(self, address: Address):
        """Mark an address as having been set with a set() instruction"""
        self._latched_addresses.add(address)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # If the bit was set with latch() instruction, don't reset it when rung goes false
        if not variable.is_retentive and variable.address not in self._latched_addresses:
            variable.set_value(variable.initial_value)  # Reset to initial value


class CBank(AddressType):
    """Control Bits (C addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "C",
            BitType(),
            1,
            2000,
            plc_memory,
            default_retentive=False,  # C bits are non-retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )
        self._latched_addresses: Set[Address] = (
            set()
        )  # Track which C bits were set and shouldn't auto-reset

    def mark_as_latched(self, address: Address):
        """Mark an address as having been set with a set() instruction"""
        self._latched_addresses.add(address)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # If the bit was set with set() instruction, don't reset it when rung goes false
        if not variable.is_retentive and variable.address not in self._latched_addresses:
            variable.set_value(variable.initial_value)


class SCBank(AddressType):
    """System Control Relay Bits (SC addresses)"""

    WRITEABLE_SC_BITS = {50, 51, 53, 55, 60, 61, 65, 66, 67, 75, 76, 120, 121}

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "SC",
            BitType(),
            1,
            1000,
            plc_memory,
            default_retentive=False,  # SC bits are always non-retentive
            allows_retentive_config=False,  # No per-address configuration for SC bits
        )
        self._latched_addresses: Set[Address] = set()  # Track which SC bits were set with latch()

        # Set default nicknames for writeable bits based on the image
        self._default_nicknames = SYSTEM_CONTROL_NICKNAMES

        # Set the default nicknames
        for bit_num, nickname in self._default_nicknames.items():
            self[bit_num] = nickname

    def mark_as_latched(self, address: Address):
        """Mark an address as having been set with a set() instruction"""
        self._latched_addresses.add(address)

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # If the bit was set with latch() instruction, don't reset it when rung goes false
        if not variable.is_retentive and variable.address not in self._latched_addresses:
            variable.set_value(variable.initial_value)  # Reset to initial value

    def __setitem__(self, key: Union[int, str], nickname_or_value: Union[str, Any]):
        """
        Override the default __setitem__ to add write protection for SC bits.
        If value is str, assign as nickname: sc[50] = "PLC_Mode_Change_to_STOP"
        If value is data, set the variable's value: sc[50] = True
        """
        address_str = self._parse_key(key)

        # Check if this is a nickname assignment
        is_nickname = False
        if isinstance(nickname_or_value, str):
            try:
                self.data_type_def.validate(nickname_or_value)
            except (ValueError, TypeError):
                is_nickname = True

            if nickname_or_value.startswith(self.name):
                is_nickname = False

            if nickname_or_value in self._default_nicknames.values():
                is_nickname = True

        if is_nickname:
            # Assigning a nickname - proceed with parent implementation
            super().__setitem__(key, nickname_or_value)
        else:
            # Setting a value - check if this SC bit is writeable
            if address_str.startswith(self.name):
                bit_num = int(address_str[len(self.name) :])
                if bit_num not in self.WRITEABLE_SC_BITS:
                    raise ValueError(f"SC bit {bit_num} is read-only and cannot be written to")

            # If we get here, either the bit is writeable or it wasn't an SC bit format
            # Create or get the variable and set its value
            var = self[key]
            var.set_value(nickname_or_value)


class TBank(AddressType):
    """Timer Bits (T addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "T",
            BitType(),
            1,
            500,
            plc_memory,
            default_retentive=False,  # T bits are non-retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        if not variable.is_retentive:
            variable.set_value(variable.initial_value)


class CTBank(AddressType):
    """Counter Bits (CT addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "CT",
            BitType(),
            1,
            250,
            plc_memory,
            default_retentive=True,  # CT bits are retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        if not variable.is_retentive:
            variable.set_value(variable.initial_value)


class DSBank(AddressType):
    """Data Store Integers (DS addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "DS",
            IntType(),
            1,
            4500,
            plc_memory,
            default_retentive=True,  # DS is retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DS is typically retentive, so do nothing when rung goes false
        # If it was configured as non-retentive, we might reset it, but this is less common for data registers
        if not variable.is_retentive:
            # We could choose to reset to initial value for non-retentive DS
            # variable.set_value(variable.initial_value)
            pass  # For now, do nothing


class DDBank(AddressType):
    """Double Data Integers (DD addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "DD",
            Int2Type(),
            1,
            1000,
            plc_memory,
            default_retentive=True,  # DD is retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DD is retentive by default, similar to DS
        if not variable.is_retentive:
            # Handle non-retentive configuration if needed
            # variable.set_value(variable.initial_value)
            pass


class DFBank(AddressType):
    """Float Data (DF addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "DF",
            FloatType(),
            1,
            500,
            plc_memory,
            default_retentive=True,  # DF is retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DF is retentive by default
        if not variable.is_retentive:
            # Handle non-retentive configuration if needed
            # variable.set_value(variable.initial_value)
            pass


class DHBank(AddressType):
    """Hex Data (DH addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "DH",
            HexType(),
            1,
            500,
            plc_memory,
            default_retentive=True,  # DH is retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # DH is retentive by default
        if not variable.is_retentive:
            # Handle non-retentive configuration if needed
            # variable.set_value(variable.initial_value)
            pass


class SDBank(AddressType):
    """System Data Integers (SD addresses)"""

    WRITEABLE_SD_ADDRESSES = {
        29,
        31,
        32,
        34,
        35,
        36,
        40,
        41,
        42,
        50,
        51,
        60,
        61,
        106,
        107,
        108,
        112,
        113,
        114,
        140,
        141,
        142,
        143,
        144,
        145,
        146,
        147,
        214,
        215,
    }

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "SD",
            IntType(),
            1,
            1000,
            plc_memory,
            default_retentive=False,  # SD is always non-retentive
            allows_retentive_config=False,  # No per-address configuration for SD
        )

        # Set default nicknames
        self._default_nicknames = SYSTEM_DATA_NICKNAMES

        # Apply the default nicknames
        for address, nickname in self._default_nicknames.items():
            self[address] = nickname

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # SD is system data, typically not affected by rung logic
        pass

    def __setitem__(self, key: Union[int, str], nickname_or_value: Union[str, Any]):
        """
        Override the default __setitem__ to add write protection for SD addresses.
        If value is str, assign as nickname: sd[50] = "Some_Nickname"
        If value is data, set the variable's value: sd[50] = 1234
        """
        address_str = self._parse_key(key)

        # Check if this is a nickname assignment
        is_nickname = False
        if isinstance(nickname_or_value, str):
            try:
                self.data_type_def.validate(nickname_or_value)
            except (ValueError, TypeError):
                is_nickname = True

            if nickname_or_value.startswith(self.name):
                is_nickname = False

            if (
                hasattr(self, "_default_nicknames")
                and nickname_or_value in self._default_nicknames.values()
            ):
                is_nickname = True

        if is_nickname:
            # Assigning a nickname - proceed with parent implementation
            super().__setitem__(key, nickname_or_value)
        else:
            # Setting a value - check if this SD address is writeable
            if address_str.startswith(self.name):
                addr_num = int(address_str[len(self.name) :])
                if addr_num not in self.WRITEABLE_SD_ADDRESSES:
                    raise ValueError(f"SD address {addr_num} is read-only and cannot be written to")

            # If we get here, either the address is writeable or it wasn't an SD address format
            var = self[key]
            var.set_value(nickname_or_value)


class TXTBank(AddressType):
    """Text Character Data (TXT addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "TXT",
            TxtType(),
            1,
            500,
            plc_memory,
            default_retentive=True,  # TXT is always retentive
            allows_retentive_config=False,  # No per-address configuration for TXT
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # TXT is retentive, so do nothing when rung goes false
        pass


# New classes for addresses in your spec that weren't in the original code


class TDBank(AddressType):
    """Timer Current Values (TD addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "TD",
            IntType(),
            1,
            500,
            plc_memory,
            default_retentive=False,  # TD is non-retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        if not variable.is_retentive:
            variable.set_value(variable.initial_value)


class CTDBank(AddressType):
    """Counter Current Values (CTD addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "CTD",
            Int2Type(),
            1,
            250,
            plc_memory,
            default_retentive=True,  # CTD is retentive by default
            allows_retentive_config=True,  # Allow per-address configuration
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        if not variable.is_retentive:
            variable.set_value(variable.initial_value)


class XDBank(AddressType):
    """Input Register (XD addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "XD",
            HexType(),
            0,
            8,
            plc_memory,
            default_retentive=False,  # XD is always non-retentive
            allows_retentive_config=False,  # No per-address configuration for XD
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # Input registers reflect hardware state
        pass


class YDBank(AddressType):
    """Output Register (YD addresses)"""

    def __init__(self, plc_memory: PLCMemory):
        super().__init__(
            "YD",
            HexType(),
            0,
            8,
            plc_memory,
            default_retentive=False,  # YD is always non-retentive
            allows_retentive_config=False,  # No per-address configuration for YD
        )

    def handle_rung_continuity_lost(self, variable: PLCVariable, context: PLCExecutionContext):
        # Output registers are handled specially, potentially through hardware interfaces
        pass
