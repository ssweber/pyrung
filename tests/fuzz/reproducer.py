"""Generate copy-pasteable standalone test code from fuzzer specs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from .pool import TagPool
    from .strategies import CondSpec, InstrSpec, ProgramSpec, PropertySpec

REPRODUCERS_DIR = Path(__file__).parent / "reproducers"

_TYPE_CONSTRUCTORS = {
    TagType.BOOL: "Bool",
    TagType.INT: "Int",
    TagType.DINT: "Dint",
    TagType.REAL: "Real",
    TagType.WORD: "Word",
    TagType.CHAR: "Char",
}


def _tag_decl(tag: Tag) -> str:
    ctor = _TYPE_CONSTRUCTORS[tag.type]
    kwargs: list[str] = []
    if tag.external:
        kwargs.append("external=True")
    if tag.choices is not None:
        kwargs.append(f"choices={dict(tag.choices)!r}")
    if tag.min is not None:
        kwargs.append(f"min={tag.min!r}")
    if tag.max is not None:
        kwargs.append(f"max={tag.max!r}")
    kwarg_str = f", {', '.join(kwargs)}" if kwargs else ""
    return f'{tag.name} = {ctor}("{tag.name}"{kwarg_str})'


def _pool_decls(pool: TagPool) -> list[str]:
    lines: list[str] = []
    for tag in pool.bool_inputs + pool.bool_internal:
        lines.append(_tag_decl(tag))
    for tag in pool.int_tags + pool.dint_tags + pool.real_tags + pool.word_tags:
        lines.append(_tag_decl(tag))
    for t in pool.timers:
        lines.append(f'{t.name} = Timer.clone("{t.name}")')
    for c in pool.counters:
        lines.append(f'{c.name} = Counter.clone("{c.name}")')
    if pool.int_block is not None:
        b = pool.int_block
        lines.append(f'{b.name} = Block("{b.name}", TagType.INT, {b.start}, {b.end})')
    return lines


def _tag_ref(tag: Any) -> str:
    if isinstance(tag, Tag):
        return tag.name
    return repr(tag)


def _cond_code(spec: CondSpec) -> str:
    if spec.kind == "bit":
        return _tag_ref(spec.tag)
    elif spec.kind == "negated":
        return f"~{_tag_ref(spec.tag)}"
    elif spec.kind == "compare":
        return f"{_tag_ref(spec.tag)} {spec.op} {spec.operand!r}"
    elif spec.kind == "truthy":
        return _tag_ref(spec.tag)
    elif spec.kind == "rise":
        return f"rise({_tag_ref(spec.tag)})"
    elif spec.kind == "fall":
        return f"fall({_tag_ref(spec.tag)})"
    elif spec.kind == "composite_and":
        c1, c2 = spec.operand
        return f"And({_cond_code(c1)}, {_cond_code(c2)})"
    elif spec.kind == "composite_or":
        c1, c2 = spec.operand
        return f"Or({_cond_code(c1)}, {_cond_code(c2)})"
    else:
        return _tag_ref(spec.tag)


def _instr_code(spec: InstrSpec) -> str:
    a = spec.args
    if spec.kind == "out":
        return f"out({_tag_ref(a['target'])})"
    elif spec.kind == "latch":
        return f"latch({_tag_ref(a['target'])})"
    elif spec.kind == "reset":
        return f"reset({_tag_ref(a['target'])})"
    elif spec.kind == "copy":
        return f"copy({_tag_ref(a['source'])}, {_tag_ref(a['dest'])})"
    elif spec.kind == "calc":
        ops = {"add": "+", "sub": "-", "mul": "*"}
        op_sym = ops.get(a["op"], a["op"])
        return f"calc({_tag_ref(a['source'])} {op_sym} {a['literal']!r}, {_tag_ref(a['dest'])})"
    elif spec.kind == "on_delay":
        base = f"on_delay({_tag_ref(a['timer'])}, {_tag_ref(a['preset'])})"
        if a.get("reset"):
            return f"{base}.reset({_tag_ref(a['reset'])})"
        return base
    elif spec.kind == "off_delay":
        return f"off_delay({_tag_ref(a['timer'])}, {_tag_ref(a['preset'])})"
    elif spec.kind == "count_up":
        base = f"count_up({_tag_ref(a['counter'])}, {_tag_ref(a['preset'])})"
        if a.get("down"):
            base = f"{base}.down({_tag_ref(a['down'])})"
        return f"{base}.reset({_tag_ref(a['reset'])})"
    elif spec.kind == "count_down":
        return f"count_down({_tag_ref(a['counter'])}, {_tag_ref(a['preset'])}).reset({_tag_ref(a['reset'])})"
    return f"# unknown instruction: {spec.kind}"


def _prop_code(spec: PropertySpec) -> str:
    if spec.kind == "always_false":
        return f"{_tag_ref(spec.tags[0])} == False"
    elif spec.kind == "always_true":
        return f"{_tag_ref(spec.tags[0])} == True"
    elif spec.kind == "bounded":
        return f"{_tag_ref(spec.tags[0])} < {spec.bound!r}"
    elif spec.kind == "mutual_exclusion":
        return f"Or(~{_tag_ref(spec.tags[0])}, ~{_tag_ref(spec.tags[1])})"
    elif spec.kind == "timer_never_fires":
        return f"{_tag_ref(spec.tags[0])} == False"
    elif spec.kind == "counter_bounded":
        return f"{_tag_ref(spec.tags[0])} < {spec.bound!r}"
    else:
        return f"{_tag_ref(spec.tags[0])} == False"


def format_soundness_reproducer(
    spec: ProgramSpec,
    prop_spec: PropertySpec,
    optimized_type: str,
    unoptimized_type: str,
) -> str:
    lines = [
        '"""Reproducer: optimization soundness disagreement."""',
        "",
        "from pyrung.core import (",
        "    And, Block, Bool, Counter, Dint, Int, Or, Program, Real, Rung,",
        "    TagType, Timer, Word, calc, copy, count_down, count_up, fall,",
        "    latch, off_delay, on_delay, out, reset, rise,",
        ")",
        "from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, prove",
        "",
        "",
        "def test_reproducer():",
    ]
    for decl in _pool_decls(spec.pool):
        lines.append(f"    {decl}")
    lines.append("")
    lines.append("    with Program(strict=False) as logic:")
    for rs in spec.rungs:
        conds = ", ".join(_cond_code(c) for c in rs.conditions)
        lines.append(f"        with Rung({conds}):")
        for instr in rs.instructions:
            lines.append(f"            {_instr_code(instr)}")
    lines.append("")
    prop = _prop_code(prop_spec)
    lines.append(f"    optimized = prove(logic, {prop}, max_states=10_000, depth_budget=20)")
    lines.append(f"    unoptimized = prove(logic, {prop}, max_states=10_000, depth_budget=20,")
    lines.append("                        _skip_optimizations=True)")
    lines.append("")
    lines.append(f"    # optimized={optimized_type}, unoptimized={unoptimized_type}")
    lines.append(
        "    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):"
    )
    lines.append("        return")
    lines.append("    assert type(optimized) is type(unoptimized), (")
    lines.append(
        '        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}"'
    )
    lines.append("    )")
    lines.append("")
    return "\n".join(lines)


def format_parity_reproducer(
    spec: ProgramSpec,
    scan: int,
    input_history: list[dict[str, bool]],
    diff: str,
) -> str:
    lines = [
        '"""Reproducer: engine parity disagreement."""',
        "",
        "import pytest",
        "",
        "from pyrung.core import (",
        "    PLC, And, Block, Bool, CompiledPLC, Counter, Dint, Int, Or, Program, Real, Rung,",
        "    TagType, Timer, Word, calc, copy, count_down, count_up, fall,",
        "    latch, off_delay, on_delay, out, reset, rise,",
        ")",
        "",
        "",
        "def test_reproducer():",
    ]
    for decl in _pool_decls(spec.pool):
        lines.append(f"    {decl}")
    lines.append("")
    lines.append("    with Program(strict=False) as logic:")
    for rs in spec.rungs:
        conds = ", ".join(_cond_code(c) for c in rs.conditions)
        lines.append(f"        with Rung({conds}):")
        for instr in rs.instructions:
            lines.append(f"            {_instr_code(instr)}")
    lines.append("")
    lines.append("    interpreted = PLC(logic, dt=0.010)")
    lines.append("    compiled = CompiledPLC(logic, dt=0.010)")
    lines.append("")
    # Only emit inputs up to the failing scan + 1
    for inputs in input_history[: scan + 1]:
        if inputs:
            lines.append(f"    interpreted.patch({inputs!r})")
            lines.append(f"    compiled.patch({inputs!r})")
        lines.append("    interpreted.step()")
        lines.append("    compiled.step()")
        lines.append("")
    lines.append("    i_state = interpreted.current_state")
    lines.append("    c_state = compiled.current_state")
    lines.append("    assert dict(i_state.tags) == dict(c_state.tags)")
    lines.append("    assert dict(i_state.memory) == dict(c_state.memory)")
    lines.append("")
    return "\n".join(lines)


def write_reproducer(code: str, prefix: str) -> Path:
    REPRODUCERS_DIR.mkdir(exist_ok=True)
    path = REPRODUCERS_DIR / f"{prefix}_latest.py"
    path.write_text(code, encoding="utf-8")
    return path
