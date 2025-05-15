from typing import Dict, Optional, List, Any, Callable, Union, Set

# Import our classes
from memory_model import (
    PLCMemory, PLCExecutionContext, PLCVariable, 
    XBank, YBank, CBank, DSBank, DDBank, DFBank, DHBank, TXTBank
)
from conditions import (
    Condition, BitCondition, NormallyClosedCondition, 
    RisingEdgeCondition, FallingEdgeCondition
)
from instructions import (
    Instruction, OutInstruction, SetInstruction, 
    ResetInstruction, CopyInstruction, MathInstruction
)
from program import Rung, ProgramBlock, PLCProgram

class PLC:
    """The main PLC class that orchestrates execution"""
    
    def __init__(self):
        """Initialize the PLC system"""
        self.memory = PLCMemory()
        
        # Initialize address banks
        self.x = XBank(self.memory)
        self.y = YBank(self.memory)
        self.c = CBank(self.memory)
        self.ds = DSBank(self.memory)
        self.dd = DDBank(self.memory)
        self.df = DFBank(self.memory)
        self.dh = DHBank(self.memory)
        self.txt = TXTBank(self.memory)
        
        # Add to dictionary for easier access
        self.address_types = {
            "X": self.x,
            "Y": self.y,
            "C": self.c,
            "DS": self.ds,
            "DD": self.dd,
            "DF": self.df,
            "DH": self.dh,
            "TXT": self.txt
        }
        
        # Initialize program
        self.program = PLCProgram()
        
        # Make this instance the current context
        _set_current_plc(self)

    def scan(self):
        """Execute one scan cycle of the PLC program"""
        context = PLCExecutionContext(self.memory)
        
        # Execute the main program
        self._execute_program_block(self.program.main_program, context)
        
        # Update previous values for edge detection
        self.memory.end_scan_cycle()

    def _execute_program_block(self, program_block: ProgramBlock, context: PLCExecutionContext):
        """Execute a program block (main or subroutine)"""
        # Clear the rung stack
        self.program._current_rung_context_stack = []
        
        # Execute each rung
        for rung in program_block.rungs:
            self._execute_rung(rung, context)

    def _execute_rung(self, rung: Rung, context: PLCExecutionContext, parent_chain_active: bool = True):
        """Execute a single rung and its child rungs"""
        # Evaluate rung conditions
        rung.is_active = rung.evaluate_conditions(context)
        
        # Determine if the whole chain is active
        rung.chain_active = parent_chain_active and rung.is_active
        
        # Push this rung to the context stack
        self.program.push_rung_context(rung, rung.chain_active)
        
        # Execute instructions if rung is active
        if rung.chain_active:
            rung.execute_instructions(context)
            
            # Execute all child rungs if this rung is active
            for child_rung in rung.child_rungs:
                self._execute_rung(child_rung, context, rung.chain_active)
        else:
            rung.handle_outputs_on_rung_false(context)
        
        # Pop this rung from the stack
        self.program.pop_rung_context()


# Global current PLC instance
_current_plc: Optional[PLC] = None

def _set_current_plc(plc: PLC):
    """Set the current PLC instance"""
    global _current_plc
    _current_plc = plc

def get_current_plc() -> Optional[PLC]:
    """Get the current PLC instance"""
    return _current_plc