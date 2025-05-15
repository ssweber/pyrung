from typing import List, Set, Dict, Optional, Tuple, Union, Any, Callable
from abc import ABC

# Import our classes
from conditions import Condition, BitCondition
from instructions import Instruction
from memory_model import PLCVariable, PLCExecutionContext
from datatypes import BitType

class Rung:
    def __init__(self, *conditions: Union[Condition, bool, 'PLCVariable']):
        """Initialize a rung with its conditions"""
        
        processed_conditions_for_init: List[Union[Condition, bool]] = []
        for c_in in conditions:
            if c_in is True:
                # If True is one of the conditions, it's filtered out.
                # If Rung(True) is called, conditions list will be empty, making it unconditional.
                continue

            if isinstance(c_in, PLCVariable):
                # Automatically wrap PLCVariable in BitCondition if it's a BIT type
                if not isinstance(c_in.address_type.data_type_def, BitType):
                    raise TypeError(
                        f"Implicit normally open condition for a PLCVariable requires a BIT type, "
                        f"got {c_in.address_type.data_type_def.__class__.__name__} for {c_in}. "
                        f"For non-BIT types, use comparison operators (e.g., my_int_var == 10)."
                    )
                processed_conditions_for_init.append(BitCondition(c_in))
            else:
                # This appends already formed Condition objects or literal False
                processed_conditions_for_init.append(c_in)
        
        self.conditions: List[Union[Condition, bool]] = processed_conditions_for_init
        self.is_active = False  # Is this rung's condition true?
        self.chain_active = False  # Is this rung and all its parents active?
        self.instructions: List[Instruction] = []  # Instructions to execute
        self.coil_outputs: Set[PLCVariable] = set()  # Variables affected by out()
        self.parent_rung = None  # Parent rung if nested
        self.child_rungs: List[Rung] = []  # Child rungs (nested rungs)
        
    def evaluate_conditions(self, context: PLCExecutionContext) -> bool:
        """Evaluate all conditions for this rung"""
        # If no conditions (e.g., Rung() or Rung(True)), the rung is unconditionally true
        if not self.conditions:
            return True
        
        # Otherwise, all conditions must be true
        for cond in self.conditions:
            if cond is False:  # If a literal False was passed as a condition
                return False   # The entire rung evaluates to False
            
            # Assuming other items in self.conditions are Condition objects
            # due to the __init__ processing (PLCVariables are wrapped).
            if not cond.evaluate(context): # type: ignore
                return False
        return True

    def execute_instructions(self, context: PLCExecutionContext):
        """Execute all instructions in this rung"""
        for instruction in self.instructions:
            instruction.execute(context)

    def handle_outputs_on_rung_false(self, context: 'PLCExecutionContext'):
        """Handle outputs when rung becomes false"""
        # Reset coil (out instruction) outputs only
        for var in self.coil_outputs:
            var.address_type.handle_rung_continuity_lost(var, context)
            
        # Reset oneshot triggers for all instructions
        for instruction in self.instructions:
            if hasattr(instruction, 'reset_oneshot_trigger'):
                instruction.reset_oneshot_trigger()
        # Latched outputs (set instruction) are not reset

    def add_instruction(self, instruction: Instruction):
        """Add an instruction to this rung"""
        self.instructions.append(instruction)

    def add_coil_output(self, variable: PLCVariable):
        """Register a variable as a coil output (affected by out())"""
        self.coil_outputs.add(variable)
        
    def add_child_rung(self, rung: 'Rung'):
        """Add a child rung to this rung"""
        self.child_rungs.append(rung)
        rung.parent_rung = self


class ProgramBlock:
    """A block of PLC logic (main program or subroutine)"""
    
    def __init__(self, name: str):
        self.name = name
        self.rungs: List[Rung] = []

    def add_rung(self, rung: Rung):
        """Add a rung to this program block"""
        self.rungs.append(rung)


class PLCProgram:
    """A complete PLC program"""
    
    def __init__(self):
        self.main_program = ProgramBlock("main")
        self.subroutines: Dict[str, ProgramBlock] = {}
        self._current_rung_context_stack: List[Rung] = []

    def get_current_rung(self) -> Optional[Rung]:
        """Get the current rung being defined"""
        if not self._current_rung_context_stack:
            return None
        return self._current_rung_context_stack[-1]

    def push_rung_context(self, rung: Rung):
        """Push a rung context to the stack (for program definition)"""
        self._current_rung_context_stack.append(rung)

    def pop_rung_context(self):
        """Pop a rung context from the stack"""
        if self._current_rung_context_stack:
            return self._current_rung_context_stack.pop()
        return None
