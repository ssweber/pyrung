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
    TBank,
    CTBank,
    SCBank,
    DSBank,
    DDBank,
    DHBank,
    DFBank,
    XDBank,
    YDBank,
    TDBank,
    CTDBank,
    SDBank,
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
        self.t = TBank(self.memory)
        self.ct = CTBank(self.memory)
        self.sc = SCBank(self.memory)
        self.ds = DSBank(self.memory)
        self.dd = DDBank(self.memory)
        self.df = DFBank(self.memory)
        self.dh = DHBank(self.memory)
        self.xd = XDBank(self.memory)
        self.yd = YDBank(self.memory)
        self.td = TDBank(self.memory)
        self.ctd = CTDBank(self.memory)
        self.sd = SDBank(self.memory)
        self.txt = TXTBank(self.memory)

        # Add to dictionary for easier access
        self.address_types = {
            "X": self.x,
            "Y": self.y,
            "C": self.c,
            "T": self.t,
            "CT": self.ct,
            "SC": self.sc,
            "DS": self.ds,
            "DD": self.dd,
            "DF": self.df,
            "DH": self.dh,
            "XD": self.xd,
            "YD": self.yd,
            "TD": self.td,
            "CTD": self.ctd,
            "SD": self.sd,
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
        """Execute a program block (main or subroutine) rung by rung.

        Each rung execution follows a typical PLC two-phase model:
        1.  All conditions within the rung (including branches) are evaluated *before* any instructions.
        2.  Instructions are executed and outputs handled *after* evaluation, based on the condition results.
        """

        # Process each rung sequentially
        for rung in program_block.rungs:
            # PHASE 1: Evaluate this Rung and it's branches conditions.
            rung.is_active = rung.evaluate_conditions(context)

            if rung.is_active:
                rung.chain_active = True

                # Evaluate branch conditions (if any) - only done if rung is active
                for branch in rung.branches:
                    branch.is_active = branch.evaluate_conditions(context)
                    branch.chain_active = (
                        branch.is_active
                    )  # Branch is active if its own conditions are true

                # PHASE 2: Execute instructions or handle inactive state
                # Execute rung instructions
                rung.execute_instructions(context)

                # Execute active branch instructions
                for branch in rung.branches:
                    if branch.chain_active:
                        branch.execute_instructions(context)
                    else:
                        branch.handle_outputs_on_branch_false(context)
            else:
                # Rung is not active
                rung.chain_active = False

                # Handle outputs for the inactive rung
                rung.handle_outputs_on_rung_false(context)

                # Deactivate all branches when rung is inactive
                for branch in rung.branches:
                    branch.is_active = False
                    branch.chain_active = False
                    branch.handle_outputs_on_branch_false(context)

    def initialize_on_power_up(self):
        """Initialize memory on power-up or mode transition from STOP to RUN"""
        for bank_name, bank in self.address_types.items():
            for i in range(bank.start_addr, bank.end_addr + 1):
                try:
                    address_str = bank._make_address_str(i)
                    # Get the variable (will create it if it doesn't exist)
                    var = bank[i]

                    # If the variable is non-retentive, reset to initial value
                    if not var.is_retentive:
                        var.set_value(var.initial_value)
                except Exception as e:
                    # Log error and continue
                    print(f"Error initializing {bank_name}{i}: {e}")
