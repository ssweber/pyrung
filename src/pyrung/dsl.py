from typing import Any, Callable, Union, List, Optional, Set

# Import our classes for IDE completions
from plc import PLC, get_current_plc
from memory_model import PLCExecutionContext, PLCVariable
from conditions import BitCondition, NormallyClosedCondition, RisingEdgeCondition, FallingEdgeCondition
from instructions import OutInstruction, LatchInstruction, ResetInstruction, CopyInstruction, MathInstruction
from program import Rung

# Add this to dsl.py at the top after imports
def requires_rung_context(func):
    """Decorator that ensures a function is called within an active rung context."""
    def wrapper(*args, **kwargs):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError(f"{func.__name__}() called with no active PLC context")
        
        current_rung = plc.program.get_current_rung()
        if not current_rung:
            raise RuntimeError(f"{func.__name__}() called outside of a Rung context")
        
        # Add plc and current_rung as keyword arguments
        kwargs['plc'] = plc
        kwargs['current_rung'] = current_rung
        
        return func(*args, **kwargs)
    return wrapper

# DSL Functions

def nc(variable: PLCVariable):
    """Create a normally closed contact (XIO)"""
    return NormallyClosedCondition(variable)

def re(variable: PLCVariable):
    """Create a rising edge detection (ONS)"""
    return RisingEdgeCondition(variable)

def fe(variable: PLCVariable):
    """Create a falling edge detection"""
    return FallingEdgeCondition(variable)

@requires_rung_context
def out(target: PLCVariable, oneshot: bool = False, *, plc=None, current_rung=None):
    """Create an output coil instruction"""
    # Create and add the instruction
    instruction = OutInstruction(target, oneshot)
    current_rung.add_instruction(instruction)
    
    # Register this variable for auto-reset when rung goes false
    current_rung.add_coil_output(target)
    
    return target

@requires_rung_context
def latch(target: PLCVariable, *, plc=None, current_rung=None):
    """Create a latch coil instruction"""
    # Create and add the instruction
    instruction = LatchInstruction(target)
    current_rung.add_instruction(instruction)

    return target

@requires_rung_context
def reset(target: PLCVariable, *, plc=None, current_rung=None):
    """Create an unlatch coil instruction"""
    
    # Create and add the instruction
    instruction = ResetInstruction(target)
    current_rung.add_instruction(instruction)
    
    return target

@requires_rung_context
def copy(source: Union[PLCVariable, Any], target: PLCVariable, oneshot: bool = False, *, plc=None, current_rung=None):
    """Create a copy instruction"""
    
    # Create and add the instruction
    instruction = CopyInstruction(source, target, oneshot)
    current_rung.add_instruction(instruction)
    
    return target

@requires_rung_context
def math_decimal(expression_func: Callable[[], Any], target: PLCVariable, oneshot: bool = False, *, plc=None, current_rung=None):
    """Create a math instruction"""
    
    # Create and add the instruction
    instruction = MathInstruction(expression_func, target, oneshot)
    current_rung.add_instruction(instruction)
    
    return target

class RungContextManager(Rung):
    """Context manager wrapper for Rung to handle enter/exit"""
    
    def __init__(self, *conditions):
        super().__init__(*conditions)
        
    def __enter__(self):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")
        
        # Add this rung to the main program or as a child to the current rung
        current_rung = plc.program.get_current_rung()
        if current_rung:
            self.parent_rung = current_rung
            current_rung.add_child_rung(self)
        else:
            plc.program.main_program.add_rung(self)
        
        # Push to stack for DSL functions to use
        plc.program.push_rung_context(self)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")
        
        # Remove from stack
        plc.program.pop_rung_context()

# Export Rung as the context manager class
Rung = RungContextManager

# Constants for timer units
class TimeUnit:
    Td = "days"
    Th = "hours"
    Tm = "minutes"
    Ts = "seconds"
    Tms = "milliseconds"