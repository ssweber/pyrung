"""Scratch: a CLICK-like event drum model, monkeypatched, to see whether it
reproduces every drum reading from the real CLICK.  Model A:
- jump/jog act on their edges whether or not the drum is enabled;
- on one scan, reset wins: step 1, flag off, ALL outputs off, jump/jog dropped
  (their edge memory still updates);
- otherwise jog, then jump (jump overrides);
- a disabled drum leaves its outputs alone;
- the scan the last step's event ends the sequence, all outputs go off and stay
  off until the step changes (done memory)."""
import sys
from pyrung.core.instruction import drums as D
from pyrung.core.instruction.drums import _read_step_tag_value, _resolve_step_value

def execute(self, ctx, enabled):
    cv = D.instruction_condition_view(ctx)
    step = _read_step_tag_value(ctx, self.current_step)
    step_changed = False
    if not self._step_is_valid(step):
        step = 1
        if enabled:
            ctx.set_tag(self.current_step.name, 1)
            step_changed = True
    jump_curr, jump_edge = self._resolve_jump_edge(ctx, cv)
    jog_curr, jog_edge = self._resolve_jog_edge(ctx, cv)
    reset_active = bool(self.reset_condition.evaluate(cv))
    done = bool(ctx.get_memory(self.memory_key("_drum_done"), False))
    if reset_active:
        step = 1; step_changed = True; done = False
        ctx.set_tags({self.current_step.name: 1, self.completion_flag.name: False})
        ctx.set_tags({t.name: False for t in self.outputs})
    else:
        if enabled:
            event_curr = bool(self.events[step - 1].evaluate(cv))
            last_step = int(ctx.get_memory(self.memory_key("_drum_last_step"), 0))
            ready = bool(ctx.get_memory(self.memory_key("_drum_event_ready"), True))
            prev = bool(ctx.get_memory(self.memory_key("_drum_event_prev"), False))
            if last_step != step or step_changed:
                ready = not event_curr; prev = event_curr
            elif not ready and not event_curr:
                ready = True
            if ready and event_curr and not prev:
                if step < self.step_count:
                    step += 1; step_changed = True
                    ctx.set_tag(self.current_step.name, step)
                else:
                    ctx.set_tag(self.completion_flag.name, True); done = True
        if jog_edge and step < self.step_count:
            step += 1; step_changed = True; done = False
            ctx.set_tag(self.current_step.name, step)
        if jump_edge:
            target = _resolve_step_value(self.jump_step, ctx)
            if target is not None and self._step_is_valid(target):
                step_changed = step_changed or step != target; step = target; done = False
                ctx.set_tag(self.current_step.name, step)
        if enabled:
            if done:
                ctx.set_tags({t.name: False for t in self.outputs})
            else:
                self._apply_outputs(ctx, step)
    fe = bool(self.events[step - 1].evaluate(cv))
    ready = bool(ctx.get_memory(self.memory_key("_drum_event_ready"), True))
    if step_changed:
        ready = not fe
    elif not ready and not fe:
        ready = True
    ctx.set_memory(self.memory_key("_drum_event_ready"), ready)
    ctx.set_memory(self.memory_key("_drum_event_prev"), fe)
    ctx.set_memory(self.memory_key("_drum_last_step"), step)
    ctx.set_memory(self.memory_key("_drum_done"), done)
    self._write_control_prev_state(ctx, jump_curr=jump_curr, jog_curr=jog_curr)

if "--patch" in sys.argv:
    D.EventDrumInstruction.execute = execute

from pyrung.conformance import run, format_report
from tests.conformance.test_drum import group
res = run(group)
for r in res:
    print(r.case.id if hasattr(r, "case") else r, getattr(r, "passed", None), getattr(r, "actual", None), getattr(r, "note", ""))
