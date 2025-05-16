from typing import Dict, Optional, List, Any, Callable, Union, Set, Tuple

from registry import register_current_plc


# Import our classes
from memory_model import (
    PLCMemory,
    PLCExecutionContext,
    PLCVariable,
    XBank,
    YBank,
    CBank,
    DSBank,
    DDBank,
    DFBank,
    DHBank,
    TXTBank,
)
from conditions import (
    Condition,
    BitCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
    FallingEdgeCondition,
)
from instructions import (
    Instruction,
    OutInstruction,
    LatchInstruction,
    ResetInstruction,
    CopyInstruction,
    MathInstruction,
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
            "TXT": self.txt,
        }

        # Initialize program
        self.program = PLCProgram()

        # Register this instance as the current PLC
        register_current_plc(self)

    def scan(self):
        """Execute one scan cycle of the PLC program"""
        context = PLCExecutionContext(self.memory)

        # Execute the main program
        self._execute_program_block(self.program.main_program, context)

        # Update previous values for edge detection
        self.memory.end_scan_cycle()

    def _execute_program_block(self, program_block: ProgramBlock, context: PLCExecutionContext):
        """Execute a program block (main or subroutine) with two-phase execution model"""
        # PHASE 1: Evaluate all rung and branch conditions first
        for rung in program_block.rungs:
            # Evaluate main rung conditions
            rung.is_active = rung.evaluate_conditions(context)

            # Evaluate branch conditions (if any)
            for branch in rung.branches:
                branch.is_active = branch.evaluate_conditions(context)
                # Note: We just store evaluation results, no execution yet

        # PHASE 2: Execute instructions for active rungs and branches
        for rung in program_block.rungs:
            # Only proceed if rung is active
            if rung.is_active:
                rung.chain_active = (
                    True  # This rung is at program level, so it's always in an active chain
                )
                rung.execute_instructions(context)

                # Execute instructions for active branches within this rung
                for branch in rung.branches:
                    branch.chain_active = (
                        branch.is_active
                    )  # Branch is active if its own conditions are true
                    if branch.chain_active:
                        branch.execute_instructions(context)
                    else:
                        branch.handle_outputs_on_branch_false(context)
            else:
                # Rung is not active
                rung.chain_active = False
                rung.handle_outputs_on_rung_false(context)

    def _execute_rung(
        self,
        rung: Rung,
        context: PLCExecutionContext,
        execution_stack: List[Tuple[Rung, bool]],
        parent_chain_active: bool = True,
    ):
        """Execute a single rung and its child rungs"""
        # Evaluate rung conditions
        rung.is_active = rung.evaluate_conditions(context)

        # Determine if the whole chain is active
        rung.chain_active = parent_chain_active and rung.is_active

        # Push this rung to the execution stack
        execution_stack.append((rung, rung.chain_active))

        # Execute instructions if rung is active
        if rung.chain_active:
            rung.execute_instructions(context)

            # Execute all child rungs if this rung is active
            for child_rung in rung.child_rungs:
                self._execute_rung(child_rung, context, execution_stack, rung.chain_active)
        else:
            rung.handle_outputs_on_rung_false(context)

        # Pop this rung from the execution stack
        execution_stack.pop()
