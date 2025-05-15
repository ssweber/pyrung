from typing import Any, Callable, Union, List, Optional, Set

# Import our classes for IDE completions
from plc import PLC, get_current_plc
from memory_model import PLCExecutionContext, PLCVariable
from conditions import BitCondition, NormallyClosedCondition, RisingEdgeCondition, FallingEdgeCondition
from instructions import OutInstruction, SetInstruction, ResetInstruction, CopyInstruction, MathInstruction
from program import Rung

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

def out(target: PLCVariable, oneshot: bool = False):
    """Create an output coil instruction"""
    plc = get_current_plc()
    if not plc:
        raise RuntimeError("No active PLC context")
    
    current_rung = plc.program.get_current_rung()
    if not current_rung:
        raise RuntimeError("out() called outside of a Rung context")
    
    # Create and add the instruction
    instruction = OutInstruction(target, oneshot)
    current_rung.add_instruction(instruction)
    
    # Register this variable for auto-reset when rung goes false
    current_rung.add_coil_output(target)
    
    return target

def set_instr(target: PLCVariable):
    """Create a latch coil instruction"""
    plc = get_current_plc()
    if not plc:
        raise RuntimeError("No active PLC context")
    
    current_rung = plc.program.get_current_rung()
    if not current_rung:
        raise RuntimeError("set() called outside of a Rung context")
    
    # Create and add the instruction
    instruction = SetInstruction(target)
    current_rung.add_instruction(instruction)
    
    # Register this variable as latched (not auto-reset)
    current_rung.add_latched_output(target)
    
    return target

def reset(target: PLCVariable):
    """Create an unlatch coil instruction"""
    plc = get_current_plc()
    if not plc:
        raise RuntimeError("No active PLC context")
    
    current_rung = plc.program.get_current_rung()
    if not current_rung:
        raise RuntimeError("reset() called outside of a Rung context")
    
    # Create and add the instruction
    instruction = ResetInstruction(target)
    current_rung.add_instruction(instruction)
    
    return target

def copy(source: Union[PLCVariable, Any], target: PLCVariable, oneshot: bool = False):
    """Create a copy instruction"""
    plc = get_current_plc()
    if not plc:
        raise RuntimeError("No active PLC context")
    
    current_rung = plc.program.get_current_rung()
    if not current_rung:
        raise RuntimeError("copy() called outside of a Rung context")
    
    # Create and add the instruction
    instruction = CopyInstruction(source, target, oneshot)
    current_rung.add_instruction(instruction)
    
    # Register this variable as a copy destination
    current_rung.add_copied_output(target)
    
    return target

def math_decimal(expression_func: Callable[[], Any], target: PLCVariable, oneshot: bool = False):
    """Create a math instruction"""
    plc = get_current_plc()
    if not plc:
        raise RuntimeError("No active PLC context")
    
    current_rung = plc.program.get_current_rung()
    if not current_rung:
        raise RuntimeError("math_decimal() called outside of a Rung context")
    
    # Create and add the instruction
    instruction = MathInstruction(expression_func, target, oneshot)
    current_rung.add_instruction(instruction)
    
    # Register this variable as a math destination
    current_rung.add_copied_output(target)
    
    return target

class RungContextManager(Rung):
    """Context manager wrapper for Rung to handle enter/exit"""
    
    def __init__(self, *conditions):
        super().__init__(*conditions)
        
    def __enter__(self):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")
        
        # Get parent rung's active status
        parent_chain_active = plc.program.get_parent_chain_active()
        
        # Add this rung to the main program or as a child to the current rung
        current_rung = plc.program.get_current_rung()
        if current_rung:
            self.parent_rung = current_rung
            current_rung.add_child_rung(self)
        else:
            plc.program.main_program.add_rung(self)
        
        # Evaluate this rung
        context = PLCExecutionContext(plc.memory)
        self.is_active = self.evaluate_conditions(context)
        self.chain_active = parent_chain_active and self.is_active
        
        # Push to stack
        plc.program.push_rung_context(self, self.chain_active)
        
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        plc = get_current_plc()
        if not plc:
            raise RuntimeError("No active PLC context")
        
        # Execute instructions if rung chain is active
        if self.chain_active:
            context = PLCExecutionContext(plc.memory)
            self.execute_instructions(context)
        else:
            # Handle outputs for non-active rung
            context = PLCExecutionContext(plc.memory)
            self.handle_outputs_on_rung_false(context)
        
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