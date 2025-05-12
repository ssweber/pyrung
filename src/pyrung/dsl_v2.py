from enum import Enum


class PLCDataType(Enum):
    BIT = 1
    INT = 2
    INT2 = 3
    FLOAT = 4
    HEX = 5
    TXT = 6


class PLCDataTypeValidator:
    """Validates and converts values according to PLC data types"""

    @staticmethod
    def validate(value, data_type):
        """Validate and convert a value to the specified data type"""
        if data_type == PLCDataType.BIT:
            if value not in (0, 1, True, False):
                raise ValueError(f"Bit value must be 0, 1, True, or False, got {value}")
            return 1 if value else 0

        elif data_type == PLCDataType.INT:
            if not isinstance(value, (int, float)):
                raise ValueError(f"INT value must be a number, got {type(value)}")
            int_value = int(value)
            if int_value < -32768 or int_value > 32767:
                raise ValueError(
                    f"INT value must be between -32768 and 32767, got {value}"
                )
            return int_value

        elif data_type == PLCDataType.INT2:
            if not isinstance(value, (int, float)):
                raise ValueError(f"INT2 value must be a number, got {type(value)}")
            int_value = int(value)
            if int_value < -2147483648 or int_value > 2147483647:
                raise ValueError(
                    f"INT2 value must be between -2147483648 and 2147483647, got {value}"
                )
            return int_value

        elif data_type == PLCDataType.FLOAT:
            if not isinstance(value, (int, float)):
                raise ValueError(f"FLOAT value must be a number, got {type(value)}")
            float_value = float(value)
            if float_value < -3.4028235e38 or float_value > 3.4028235e38:
                raise ValueError(
                    f"FLOAT value must be between -3.4028235E+38 and 3.4028235E+38, got {value}"
                )
            return float_value

        elif data_type == PLCDataType.HEX:
            # Convert to integer if needed
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
                raise ValueError(
                    f"HEX value must be a string or integer, got {type(value)}"
                )

            if int_value < 0 or int_value > 0xFFFF:
                raise ValueError(f"HEX value must be between 0 and FFFF, got {value}")
            return int_value

        elif data_type == PLCDataType.TXT:
            if isinstance(value, int):
                # Allow integers for ASCII values
                if value < 0 or value > 255:
                    raise ValueError(
                        f"ASCII value must be between 0 and 255, got {value}"
                    )
                return chr(value)
            elif not isinstance(value, str):
                raise ValueError(f"TXT value must be a string, got {type(value)}")
            if len(value) != 1:
                raise ValueError(f"TXT value must be a single character, got '{value}'")
            return value

        else:
            raise ValueError(f"Unknown data type: {data_type}")

    @staticmethod
    def format_value(value, data_type):
        """Format a value for display according to its data type"""
        if data_type == PLCDataType.BIT:
            return "1" if value else "0"

        elif data_type == PLCDataType.INT or data_type == PLCDataType.INT2:
            return str(value)

        elif data_type == PLCDataType.FLOAT:
            return f"{value:.6f}"

        elif data_type == PLCDataType.HEX:
            return f"{value:04X}h"

        elif data_type == PLCDataType.TXT:
            if isinstance(value, str):
                return value
            else:
                return chr(value)

        return str(value)


class PLCAddressType:
    """Base class for different PLC address types"""

    def __init__(self, name, data_type):
        self.name = name
        self.data_type = data_type
        self._values = {}  # Stores the actual values
        self._refs = {}  # Maps addresses to names

    def __getattr__(self, name):
        """Handle attribute access for any attribute"""
        # Return a PLCReference for any attribute
        return PLCReference(self, name)

    def __setitem__(self, key, value):
        """Handle x[addr] = x.Name syntax"""
        if isinstance(value, PLCReference):
            # Store a mapping from key -> name
            self._refs[key] = value.name
        else:
            # Store an actual value, validating the data type
            try:
                self._values[key] = PLCDataTypeValidator.validate(value, self.data_type)
            except ValueError as e:
                raise ValueError(f"Error setting {self.name}[{key}]: {str(e)}")

    def __getitem__(self, key):
        """Handle x[addr] access"""
        # Return the value at this address
        return self._values.get(key, self._default_value())

    def _default_value(self):
        """Return a default value based on data type"""
        if self.data_type == PLCDataType.BIT:
            return 0
        elif self.data_type == PLCDataType.INT:
            return 0
        elif self.data_type == PLCDataType.INT2:
            return 0
        elif self.data_type == PLCDataType.FLOAT:
            return 0.0
        elif self.data_type == PLCDataType.HEX:
            return 0
        elif self.data_type == PLCDataType.TXT:
            return " "
        return None

    def format_value(self, value):
        """Format a value for display"""
        return PLCDataTypeValidator.format_value(value, self.data_type)


class PLCReference:
    """Represents a reference to a PLC variable, handling different operations"""

    def __init__(self, device, name):
        self.device = device
        self.name = name

    def __eq__(self, other):
        """Handle comparison operations"""
        if isinstance(other, PLCReference):
            return PLCCondition(self, lambda x: x == other.get_value())
        return PLCCondition(self, lambda x: x == other)

    def __ne__(self, other):
        if isinstance(other, PLCReference):
            return PLCCondition(self, lambda x: x != other.get_value())
        return PLCCondition(self, lambda x: x != other)

    def __lt__(self, other):
        if isinstance(other, PLCReference):
            return PLCCondition(self, lambda x: x < other.get_value())
        return PLCCondition(self, lambda x: x < other)

    def __le__(self, other):
        if isinstance(other, PLCReference):
            return PLCCondition(self, lambda x: x <= other.get_value())
        return PLCCondition(self, lambda x: x <= other)

    def __gt__(self, other):
        if isinstance(other, PLCReference):
            return PLCCondition(self, lambda x: x > other.get_value())
        return PLCCondition(self, lambda x: x > other)

    def __ge__(self, other):
        if isinstance(other, PLCReference):
            return PLCCondition(self, lambda x: x >= other.get_value())
        return PLCCondition(self, lambda x: x >= other)

    def __bool__(self):
        """When used in a boolean context, get the actual value"""
        value = self.get_value()
        if self.device.data_type == PLCDataType.BIT:
            return bool(value)
        else:
            # For non-bit types, any non-zero/non-empty value is True
            if isinstance(value, (int, float)):
                return value != 0
            elif isinstance(value, str):
                return value.strip() != ""
            return bool(value)

    def get_value(self):
        """Get the current value of this reference"""
        # Look up the address this name is mapped to, if any
        for addr, ref in self.device._refs.items():
            if ref == self.name:
                return self.device._values.get(addr, self.device._default_value())
        # Otherwise, get the value directly
        return self.device._values.get(self.name, self.device._default_value())

    def set_value(self, value):
        """Set the value for this reference"""
        try:
            validated_value = PLCDataTypeValidator.validate(
                value, self.device.data_type
            )

            # Look up the address this name is mapped to, if any
            for addr, ref in self.device._refs.items():
                if ref == self.name:
                    self.device._values[addr] = validated_value
                    return
            # Otherwise, set it directly
            self.device._values[self.name] = validated_value
        except ValueError as e:
            raise ValueError(f"Error setting {self.device.name}.{self.name}: {str(e)}")

    # Arithmetic operator overloads for natural expressions
    def __add__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() + other.get_value()
        return self.get_value() + other

    def __radd__(self, other):
        return other + self.get_value()

    def __sub__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() - other.get_value()
        return self.get_value() - other

    def __rsub__(self, other):
        return other - self.get_value()

    def __mul__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() * other.get_value()
        return self.get_value() * other

    def __rmul__(self, other):
        return other * self.get_value()

    def __truediv__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() / other.get_value()
        return self.get_value() / other

    def __rtruediv__(self, other):
        return other / self.get_value()

    def __floordiv__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() // other.get_value()
        return self.get_value() // other

    def __rfloordiv__(self, other):
        return other // self.get_value()

    def __mod__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() % other.get_value()
        return self.get_value() % other

    def __rmod__(self, other):
        return other % self.get_value()

    def __pow__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() ** other.get_value()
        return self.get_value() ** other

    def __rpow__(self, other):
        return other ** self.get_value()

    # Bitwise operations for working with binary data
    def __and__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() & other.get_value()
        return self.get_value() & other

    def __rand__(self, other):
        return other & self.get_value()

    def __or__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() | other.get_value()
        return self.get_value() | other

    def __ror__(self, other):
        return other | self.get_value()

    def __xor__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() ^ other.get_value()
        return self.get_value() ^ other

    def __rxor__(self, other):
        return other ^ self.get_value()

    def __lshift__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() << other.get_value()
        return self.get_value() << other

    def __rlshift__(self, other):
        return other << self.get_value()

    def __rshift__(self, other):
        if isinstance(other, PLCReference):
            return self.get_value() >> other.get_value()
        return self.get_value() >> other

    def __rrshift__(self, other):
        return other >> self.get_value()

    # Unary operations
    def __neg__(self):
        return -self.get_value()

    def __pos__(self):
        return +self.get_value()

    def __abs__(self):
        return abs(self.get_value())

    def __invert__(self):
        return ~self.get_value()

    # For numeric conversions in expressions
    def __int__(self):
        return int(self.get_value())

    def __float__(self):
        return float(self.get_value())

    def __round__(self, ndigits=0):
        return round(self.get_value(), ndigits)


class PLCCondition:
    """Represents a conditional expression"""

    def __init__(self, reference, predicate):
        self.reference = reference
        self.predicate = predicate

    def __bool__(self):
        """Evaluate the condition"""
        return self.predicate(self.reference.get_value())


class PLCInstruction:
    """Base class for PLC instructions to be executed in a rung"""

    def execute(self):
        """Execute the instruction"""
        pass


class CopyInstruction(PLCInstruction):
    def __init__(self, value, target):
        self.value = value
        self.target = target

    def execute(self):
        if isinstance(self.target, PLCReference):
            if isinstance(self.value, PLCReference):
                self.target.set_value(self.value.get_value())
            else:
                self.target.set_value(self.value)


class ResetInstruction(PLCInstruction):
    def __init__(self, target):
        self.target = target

    def execute(self):
        if isinstance(self.target, PLCReference):
            self.target.set_value(self.target.device._default_value())


class OutInstruction(PLCInstruction):
    def __init__(self, target):
        self.target = target

    def execute(self):
        if (
            isinstance(self.target, PLCReference)
            and self.target.device.data_type == PLCDataType.BIT
        ):
            self.target.set_value(
                1
            )  # Always set to 1 when executed (only executed if rung is active)


class MathDecimalInstruction(PLCInstruction):
    def __init__(self, expression_func, target):
        self.expression_func = expression_func
        self.target = target

    def execute(self):
        if isinstance(self.target, PLCReference):
            # Call the function that contains the expression
            result = self.expression_func()
            self.target.set_value(result)


class Rung:
    """A PLC rung with condition checking and instruction execution"""

    _rung_stack = []  # Stack of active rungs

    def __init__(self, *conditions):
        """Initialize a rung with its condition"""
        self.conditions = conditions if conditions else [True]
        self.is_active = False  # Is this rung's condition true?
        self.chain_active = False  # Is this rung and all its parents active?
        self.outputs = {}  # Dictionary to store outputs
        self.instructions = []  # List of instructions to execute if rung is active
        self.parent_rung = None

    def __enter__(self):
        # Check if we have a parent rung
        if Rung._rung_stack:
            self.parent_rung = Rung._rung_stack[-1]

        # Add this rung to the stack
        Rung._rung_stack.append(self)

        # Evaluate this rung's condition
        self.is_active = all(bool(cond) for cond in self.conditions)

        # A rung chain is active only if all rungs in the chain are active
        if self.parent_rung:
            self.chain_active = self.is_active and self.parent_rung.chain_active
        else:
            self.chain_active = self.is_active

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # If rung is active, execute all instructions in order
        if self.chain_active:
            for instruction in self.instructions:
                instruction.execute()

        # For outputs that weren't touched by any instruction or need to be turned off
        for key, target in self.outputs.items():
            if (
                isinstance(target, PLCReference)
                and target.device.data_type == PLCDataType.BIT
            ):
                if not self.chain_active:
                    # Turn off all outputs if rung is not active
                    target.set_value(0)

        # Remove this rung from the stack
        if Rung._rung_stack and Rung._rung_stack[-1] == self:
            Rung._rung_stack.pop()


# PLC operation functions
def out(target):
    """Register an output with the current rung"""
    if Rung._rung_stack:
        current_rung = Rung._rung_stack[-1]
        # Create a unique key for this reference
        key = (id(target.device), target.name)
        current_rung.outputs[key] = target

        # Add as instruction for execution if rung is active
        current_rung.instructions.append(OutInstruction(target))
    return target


def reset(target):
    """Reset a target to its default value"""
    if Rung._rung_stack:
        current_rung = Rung._rung_stack[-1]
        current_rung.instructions.append(ResetInstruction(target))
    return target


def copy(value, target):
    """Copy a value to a target"""
    if Rung._rung_stack:
        current_rung = Rung._rung_stack[-1]
        current_rung.instructions.append(CopyInstruction(value, target))
    return target


def math_decimal(expression_func, target):
    """Evaluate a math expression function and store the result"""
    if Rung._rung_stack:
        current_rung = Rung._rung_stack[-1]
        current_rung.instructions.append(
            MathDecimalInstruction(expression_func, target)
        )
    return target


# Create specific device types
class BitAddress(PLCAddressType):
    def __init__(self, name="Bit"):
        super().__init__(name, PLCDataType.BIT)


class IntAddress(PLCAddressType):
    def __init__(self, name="Int"):
        super().__init__(name, PLCDataType.INT)


class Int2Address(PLCAddressType):
    def __init__(self, name="Int2"):
        super().__init__(name, PLCDataType.INT2)


class FloatAddress(PLCAddressType):
    def __init__(self, name="Float"):
        super().__init__(name, PLCDataType.FLOAT)


class HexAddress(PLCAddressType):
    def __init__(self, name="Hex"):
        super().__init__(name, PLCDataType.HEX)


class TxtAddress(PLCAddressType):
    def __init__(self, name="Txt"):
        super().__init__(name, PLCDataType.TXT)


# Create the PLC devices
x = BitAddress("Input")  # Inputs are bit-type
y = BitAddress("Output")  # Outputs are bit-type
c = BitAddress("Control")  # Control bits
ds = IntAddress("DataStore")  # Data store uses integers


# Example Usage
def setup():
    """Set up the PLC configuration"""
    x[1] = x.Button
    y[1] = y.Light
    y[2] = y.Indicator
    y[3] = y.Buzzer
    y[4] = y.NestedLight
    c[1] = c.AutoMode
    ds[1] = ds.Step


def main():
    """Main PLC logic using explicit Rung context managers"""
    # Simple condition
    with Rung():
        out(y.Light)
        copy(1, ds.Step)

    # Comparison condition
    with Rung(ds.Step == 1):
        out(y.Indicator)
        copy(2, ds.Step)

    # Test nested rungs
    with Rung(ds.Step == 2):
        out(y.Buzzer)
        with Rung(c.AutoMode):
            out(y.NestedLight)
        copy(3, ds.Step)


def test_nested():
    """Test the behavior of nested rungs"""
    with Rung(ds.Step == 2):
        out(y.Buzzer)  # This should be on when Step is 2, regardless of AutoMode
        with Rung(c.AutoMode):
            out(
                y.NestedLight
            )  # This should only be on when both Step is 2 AND AutoMode is on


def test_multiple_ops():
    """Test multiple operations in a single rung"""
    with Rung(ds.Step == 2):
        copy(1, ds.Step)  # First set to 1
        math_decimal(lambda: ds.Step + 1, ds.Step)  # Then add 1 to get 2
        out(y.Indicator)  # Should be on if the rung is active


def complex_logic():
    """Demonstrate more complex conditional logic"""
    # Combining conditions with AND logic
    with Rung(x.Button, c.AutoMode):
        out(y.Light)

    # Combining conditions with OR logic
    with Rung(any([x.Button, ds.Step > 0])):
        out(y.Indicator)

    # More complex expression
    with Rung(ds.Step > 5 and ds.Step < 10):
        out(y.Alarm)
        # Nested condition
        with Rung(c.AutoMode):
            copy(0, ds.Step)  # Reset step in auto mode


# Initialize our system
setup()

# Set initial values
x._values[1] = 1  # Button is pressed
c._values[1] = 0  # AutoMode is off initially

# Run the logic
print("Initial state:")
print(f"x.Button = {x.format_value(x._values.get(1))}")
print(f"c.AutoMode = {c.format_value(c._values.get(1))}")
print(f"y.Light = {y.format_value(y._values.get(1, 0))}")
print(f"y.Indicator = {y.format_value(y._values.get(2, 0))}")
print(f"y.Buzzer = {y.format_value(y._values.get(3, 0))}")
print(f"y.NestedLight = {y.format_value(y._values.get(4, 0))}")
print(f"ds.Step = {ds.format_value(ds._values.get(1, 0))}")

# First scan
print("\nRunning first scan...")
main()

print("\nAfter first execution:")
print(f"x.Button = {x.format_value(x._values.get(1))}")
print(f"c.AutoMode = {c.format_value(c._values.get(1))}")
print(f"y.Light = {y.format_value(y._values.get(1, 0))}")
print(f"y.Indicator = {y.format_value(y._values.get(2, 0))}")
print(f"y.Buzzer = {y.format_value(y._values.get(3, 0))}")
print(f"y.NestedLight = {y.format_value(y._values.get(4, 0))}")
print(f"ds.Step = {ds.format_value(ds._values.get(1, 0))}")

# Second scan
print("\nRunning second scan...")
main()

print("\nAfter second execution:")
print(f"x.Button = {x.format_value(x._values.get(1))}")
print(f"c.AutoMode = {c.format_value(c._values.get(1))}")
print(f"y.Light = {y.format_value(y._values.get(1, 0))}")
print(f"y.Indicator = {y.format_value(y._values.get(2, 0))}")
print(f"y.Buzzer = {y.format_value(y._values.get(3, 0))}")
print(f"y.NestedLight = {y.format_value(y._values.get(4, 0))}")
print(f"ds.Step = {ds.format_value(ds._values.get(1, 0))}")

# Test nested ifs with both conditions
print("\nTesting nested if with Step=2, AutoMode=0...")
ds._values[1] = 2
c._values[1] = 0
test_nested()

print("\nAfter nested test with AutoMode OFF:")
print(f"ds.Step = {ds.format_value(ds._values.get(1, 0))}")
print(f"c.AutoMode = {c.format_value(c._values.get(1))}")
print(f"y.Buzzer = {y.format_value(y._values.get(3, 0))}")
print(f"y.NestedLight = {y.format_value(y._values.get(4, 0))}")

# Now with AutoMode on
print("\nTesting nested if with Step=2, AutoMode=1...")
c._values[1] = 1
test_nested()

print("\nAfter nested test with AutoMode ON:")
print(f"ds.Step = {ds.format_value(ds._values.get(1, 0))}")
print(f"c.AutoMode = {c.format_value(c._values.get(1))}")
print(f"y.Buzzer = {y.format_value(y._values.get(3, 0))}")
print(f"y.NestedLight = {y.format_value(y._values.get(4, 0))}")

# Test the math_decimal with lambda
print("\nTesting math_decimal with lambda...")
ds._values[1] = 2  # Set Step to 2 to match our rung condition
test_multiple_ops()

print("\nAfter math_decimal with lambda test:")
print(f"ds.Step = {ds.format_value(ds._values.get(1, 0))}")
print(f"y.Indicator = {y.format_value(y._values.get(2, 0))}")
