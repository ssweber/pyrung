"""Generate copy-pasteable standalone test code from fuzzer specs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyrung.core.structure import _StructRuntime
from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from .pool import TagPool
    from .strategies import CondSpec, InstrSpec, ProgramSpec, PropertySpec, RungSpec, SubroutineSpec

REPRODUCERS_DIR = Path(__file__).parent / "reproducers"
_RUN_ID = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

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
    for tag in pool.int_inputs:
        lines.append(_tag_decl(tag))
    for tag in pool.int_tags + pool.dint_tags + pool.real_tags + pool.word_tags + pool.char_tags:
        lines.append(_tag_decl(tag))
    for t in pool.timers:
        lines.append(f'{t.name} = Timer.clone("{t.name}")')
    for c in pool.counters:
        lines.append(f'{c.name} = Counter.clone("{c.name}")')
    if pool.int_block is not None:
        b = pool.int_block
        lines.append(f'{b.name} = Block("{b.name}", TagType.INT, {b.start}, {b.end})')
    if pool.bool_block is not None:
        b = pool.bool_block
        lines.append(f'{b.name} = Block("{b.name}", TagType.BOOL, {b.start}, {b.end})')
    if pool.char_block is not None:
        b = pool.char_block
        lines.append(f'{b.name} = Block("{b.name}", TagType.CHAR, {b.start}, {b.end})')
    return lines


def _synthetic_tag_decls(rungs: list[RungSpec], subroutines: list[SubroutineSpec] | None = None) -> list[str]:
    """Extract tag declarations for instruction-created tags not in the pool."""
    seen: set[str] = set()
    lines: list[str] = []

    def _add_tag(tag: Any, ctor: str) -> None:
        if tag.name not in seen:
            seen.add(tag.name)
            lines.append(f'{tag.name} = {ctor}("{tag.name}")')

    def _collect_from_instrs(instrs: list[InstrSpec]) -> None:
        for instr in instrs:
            if instr.kind == "event_drum":
                _add_tag(instr.args["step"], "Int")
                _add_tag(instr.args["done"], "Bool")
            elif instr.kind == "time_drum":
                _add_tag(instr.args["step"], "Int")
                _add_tag(instr.args["acc"], "Int")
                _add_tag(instr.args["done"], "Bool")
            elif instr.kind == "receive":
                _add_tag(instr.args["receiving"], "Bool")
                _add_tag(instr.args["success"], "Bool")
                _add_tag(instr.args["error"], "Bool")
                _add_tag(instr.args["exception_response"], "Int")

    for rs in rungs:
        _collect_from_instrs(rs.instructions)
        for bs in rs.branches:
            _collect_from_instrs(bs.instructions)
    if subroutines:
        for sub in subroutines:
            for rs in sub.rungs:
                _collect_from_instrs(rs.instructions)
                for bs in rs.branches:
                    _collect_from_instrs(bs.instructions)

    return lines


def _build_ref_map(pool: TagPool) -> dict[int, str]:
    """Map tag id → code reference for sub-field and block element tags."""
    refs: dict[int, str] = {}
    for t in pool.timers:
        refs[id(t)] = t.name
        refs[id(t.Done)] = f"{t.name}.Done"
        refs[id(t.Acc)] = f"{t.name}.Acc"
    for c in pool.counters:
        refs[id(c)] = c.name
        refs[id(c.Done)] = f"{c.name}.Done"
        refs[id(c.Acc)] = f"{c.name}.Acc"
    for blk in [pool.int_block, pool.bool_block, pool.char_block]:
        if blk is not None:
            for addr in range(blk.start, blk.end + 1):
                refs[id(blk[addr])] = f"{blk.name}[{addr}]"
    return refs


_REF_MAP: dict[int, str] = {}


def _tag_ref(tag: Any) -> str:
    ref = _REF_MAP.get(id(tag))
    if ref is not None:
        return ref
    if isinstance(tag, _StructRuntime):
        return tag.name
    if isinstance(tag, Tag):
        return tag.name
    return repr(tag)


def _kw_suffix(**kwargs: Any) -> str:
    parts = [f"{name}={value!r}" for name, value in kwargs.items() if value is not None]
    return f", {', '.join(parts)}" if parts else ""


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
        return f"out({_tag_ref(a['target'])}{_kw_suffix(oneshot=True) if a.get('oneshot') else ''})"
    elif spec.kind == "latch":
        return f"latch({_tag_ref(a['target'])})"
    elif spec.kind == "reset":
        return f"reset({_tag_ref(a['target'])})"
    elif spec.kind == "copy":
        return f"copy({_tag_ref(a['source'])}, {_tag_ref(a['dest'])}{_kw_suffix(oneshot=True) if a.get('oneshot') else ''})"
    elif spec.kind == "calc":
        ops = {"add": "+", "sub": "-", "mul": "*", "floordiv": "//", "mod": "%", "pow": "**"}
        op_sym = ops.get(a["op"], a["op"])
        return f"calc({_tag_ref(a['source'])} {op_sym} {a['literal']!r}, {_tag_ref(a['dest'])})"
    elif spec.kind == "calc_tag_tag":
        ops = {
            "add": "+",
            "sub": "-",
            "mul": "*",
            "mod": "%",
            "bitand": "&",
            "bitor": "|",
            "bitxor": "^",
        }
        op_sym = ops.get(a["op"], a["op"])
        return (
            f"calc({_tag_ref(a['source1'])} {op_sym} {_tag_ref(a['source2'])}, "
            f"{_tag_ref(a['dest'])})"
        )
    elif spec.kind == "calc_shift":
        return (
            f"calc({a['shift_op']}({_tag_ref(a['source'])}, {a['count']!r}), {_tag_ref(a['dest'])})"
        )
    elif spec.kind == "on_delay":
        unit_kw = _kw_suffix(unit=a.get("unit")) if a.get("unit", "ms") != "ms" else ""
        base = f"on_delay({_tag_ref(a['timer'])}, {_tag_ref(a['preset'])}{unit_kw})"
        if a.get("reset") is not None:
            return f"{base}.reset({_tag_ref(a['reset'])})"
        return base
    elif spec.kind == "off_delay":
        unit_kw = _kw_suffix(unit=a.get("unit")) if a.get("unit", "ms") != "ms" else ""
        return f"off_delay({_tag_ref(a['timer'])}, {_tag_ref(a['preset'])}{unit_kw})"
    elif spec.kind == "count_up":
        base = f"count_up({_tag_ref(a['counter'])}, {_tag_ref(a['preset'])})"
        if a.get("down") is not None:
            base = f"{base}.down({_tag_ref(a['down'])})"
        return f"{base}.reset({_tag_ref(a['reset'])})"
    elif spec.kind == "count_down":
        return f"count_down({_tag_ref(a['counter'])}, {_tag_ref(a['preset'])}).reset({_tag_ref(a['reset'])})"
    elif spec.kind == "fill":
        oneshot_kw = _kw_suffix(oneshot=True) if a.get("oneshot") else ""
        return (
            f"fill({a['value']!r}, {a['block'].name}.select({a['start']}, {a['end']}){oneshot_kw})"
        )
    elif spec.kind == "blockcopy":
        b = a["block"].name
        oneshot_kw = _kw_suffix(oneshot=True) if a.get("oneshot") else ""
        return f"blockcopy({b}.select({a['src_start']}, {a['src_end']}), {b}.select({a['dst_start']}, {a['dst_end']}){oneshot_kw})"
    elif spec.kind == "search":
        b = a["block"].name
        return f"search({b}.select({a['start']}, {a['end']}) {a['op']} {a['value']!r}, result={_tag_ref(a['result'])}, found={_tag_ref(a['found'])})"
    elif spec.kind == "shift":
        b = a["block"].name
        return f"shift({b}.select({a['start']}, {a['end']})).clock({_tag_ref(a['clock'])}).reset({_tag_ref(a['reset'])})"
    elif spec.kind == "pack_bits":
        b = a["block"].name
        return f"pack_bits({b}.select({a['start']}, {a['end']}), {_tag_ref(a['dest'])})"
    elif spec.kind == "unpack_to_bits":
        b = a["block"].name
        return f"unpack_to_bits({_tag_ref(a['source'])}, {b}.select({a['start']}, {a['end']}))"
    elif spec.kind == "pack_words":
        b = a["block"].name
        return f"pack_words({b}.select({a['start']}, {a['end']}), {_tag_ref(a['dest'])})"
    elif spec.kind == "unpack_to_words":
        b = a["block"].name
        return f"unpack_to_words({_tag_ref(a['source'])}, {b}.select({a['start']}, {a['end']}))"
    elif spec.kind == "copy_convert":
        converter = a["converter"]
        if converter == "to_text":
            kw_parts = []
            if not a.get("suppress_zero", True):
                kw_parts.append("suppress_zero=False")
            if a.get("termination_code") is not None:
                kw_parts.append(f"termination_code={a['termination_code']!r}")
            conv_str = f"to_text({', '.join(kw_parts)})"
        else:
            conv_str = converter
        return f"copy({_tag_ref(a['source'])}, {_tag_ref(a['dest'])}, convert={conv_str})"
    elif spec.kind == "indirect_copy":
        b = a["block"].name
        ref = (
            f"{b}[{_tag_ref(a['ptr'])} + {a['offset']}]"
            if a["offset"]
            else f"{b}[{_tag_ref(a['ptr'])}]"
        )
        if a["is_source"]:
            return f"copy({ref}, {_tag_ref(a['dest'])})"
        return f"copy({a['source']!r}, {ref})"
    elif spec.kind == "receive":
        return (
            f"receive(target={a['target']!r}, remote_start={a['remote_start']!r}, "
            f"dest={_tag_ref(a['dest'])}, receiving={_tag_ref(a['receiving'])}, "
            f"success={_tag_ref(a['success'])}, error={_tag_ref(a['error'])}, "
            f"exception_response={_tag_ref(a['exception_response'])})"
        )
    elif spec.kind == "pack_text":
        b = a["block"].name
        ws_kw = ", allow_whitespace=True" if a.get("allow_whitespace") else ""
        return f"pack_text({b}.select({a['start']}, {a['end']}), {_tag_ref(a['dest'])}{ws_kw})"
    elif spec.kind == "return_early":
        return "return_early()"
    elif spec.kind == "call":
        return f"call({a['name']!r})"
    elif spec.kind == "event_drum":
        outputs_str = ", ".join(_tag_ref(t) for t in a["outputs"])
        events_str = ", ".join(_tag_ref(t) for t in a["events"])
        base = (
            f"event_drum(outputs=[{outputs_str}], events=[{events_str}], "
            f"pattern={a['pattern']!r}, current_step={_tag_ref(a['step'])}, "
            f"completion_flag={_tag_ref(a['done'])}).reset({_tag_ref(a['reset'])})"
        )
        if a.get("jump") is not None:
            base = f"{base}.jump({_tag_ref(a['jump'])}, step={a['jump_step']!r})"
        if a.get("jog") is not None:
            base = f"{base}.jog({_tag_ref(a['jog'])})"
        return base
    elif spec.kind == "time_drum":
        outputs_str = ", ".join(_tag_ref(t) for t in a["outputs"])
        presets_str = ", ".join(repr(p) for p in a["presets"])
        base = (
            f"time_drum(outputs=[{outputs_str}], presets=[{presets_str}], "
            f"unit={a.get('unit', 'ms')!r}, "
            f"pattern={a['pattern']!r}, current_step={_tag_ref(a['step'])}, "
            f"accumulator={_tag_ref(a['acc'])}, "
            f"completion_flag={_tag_ref(a['done'])}).reset({_tag_ref(a['reset'])})"
        )
        if a.get("jump") is not None:
            base = f"{base}.jump({_tag_ref(a['jump'])}, step={a['jump_step']!r})"
        if a.get("jog") is not None:
            base = f"{base}.jog({_tag_ref(a['jog'])})"
        return base
    elif spec.kind == "range_sum_calc":
        return (
            f"calc({a['block'].name}.select({a['start']}, {a['end']}).sum(), {_tag_ref(a['dest'])})"
        )
    return f"# unknown instruction: {spec.kind}"


def _subroutine_lines(subs: list[SubroutineSpec], indent: str = "        ") -> list[str]:
    lines: list[str] = []
    for sub in subs:
        lines.append(f"{indent}with subroutine({sub.name!r}):")
        for rs in sub.rungs:
            conds = ", ".join(_cond_code(c) for c in rs.conditions)
            lines.append(f"{indent}    with Rung({conds}):")
            lines.extend(_rung_body_lines(rs, indent=f"{indent}        "))
    return lines


def _rung_body_lines(rs: RungSpec, indent: str = "            ") -> list[str]:
    lines: list[str] = []
    if rs.forloop is not None:
        fl = rs.forloop
        count_str = _tag_ref(fl.count) if not isinstance(fl.count, int) else repr(fl.count)
        oneshot_kw = ", oneshot=True" if fl.oneshot else ""
        lines.append(f"{indent}with forloop({count_str}{oneshot_kw}):")
        for instr in rs.instructions:
            lines.append(f"{indent}    {_instr_code(instr)}")
    else:
        for instr in rs.instructions:
            lines.append(f"{indent}{_instr_code(instr)}")
        for bs in rs.branches:
            branch_conds = ", ".join(_cond_code(c) for c in bs.conditions)
            lines.append(f"{indent}with branch({branch_conds}):")
            for instr in bs.instructions:
                lines.append(f"{indent}    {_instr_code(instr)}")
    return lines


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
    global _REF_MAP  # noqa: PLW0603
    _REF_MAP = _build_ref_map(spec.pool)
    lines = [
        '"""Reproducer: optimization soundness disagreement."""',
        "",
        "from pyrung.core import (",
        "    And, Block, Bool, Char, Counter, Dint, Int, Or, Program, Real, Rung,",
        "    TagType, Timer, Word, blockcopy, branch, calc, call, copy, count_down, count_up,",
        "    event_drum, fall, fill, forloop, latch, lro, lsh, off_delay, on_delay, out, pack_bits,",
        "    pack_words, receive, reset, return_early, rise, rro, rsh, search, shift, subroutine,",
        "    time_drum,",
        "    to_ascii, to_binary, to_text, to_value, unpack_to_bits, unpack_to_words,",
        ")",
        "from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, prove",
        "",
        "",
        "def test_reproducer():",
    ]
    for decl in _pool_decls(spec.pool):
        lines.append(f"    {decl}")
    for decl in _synthetic_tag_decls(spec.rungs, spec.subroutines):
        lines.append(f"    {decl}")
    lines.append("")
    lines.append("    with Program(strict=False) as logic:")
    for rs in spec.rungs:
        conds = ", ".join(_cond_code(c) for c in rs.conditions)
        lines.append(f"        with Rung({conds}):")
        lines.extend(_rung_body_lines(rs))
    if spec.subroutines:
        lines.extend(_subroutine_lines(spec.subroutines))
    lines.append("")
    prop = _prop_code(prop_spec)
    lines.append(f"    # To add to test_prove.py, use: _assert_soundness(logic, {prop})")
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
    input_history: list[dict[str, bool | int]],
    diff: str,
) -> str:
    global _REF_MAP  # noqa: PLW0603
    _REF_MAP = _build_ref_map(spec.pool)
    lines = [
        '"""Reproducer: engine parity disagreement."""',
        "",
        "import pytest",
        "",
        "from pyrung.core import (",
        "    PLC, And, Block, Bool, Char, CompiledPLC, Counter, Dint, Int, Or, Program, Real, Rung,",
        "    TagType, Timer, Word, blockcopy, branch, calc, call, copy, count_down, count_up,",
        "    event_drum, fall, fill, forloop, latch, lro, lsh, off_delay, on_delay, out, pack_bits,",
        "    pack_words, receive, reset, return_early, rise, rro, rsh, search, shift, subroutine,",
        "    time_drum,",
        "    to_ascii, to_binary, to_text, to_value, unpack_to_bits, unpack_to_words,",
        ")",
        "",
        "",
        "def test_reproducer():",
    ]
    for decl in _pool_decls(spec.pool):
        lines.append(f"    {decl}")
    for decl in _synthetic_tag_decls(spec.rungs, spec.subroutines):
        lines.append(f"    {decl}")
    lines.append("")
    lines.append("    with Program(strict=False) as logic:")
    for rs in spec.rungs:
        conds = ", ".join(_cond_code(c) for c in rs.conditions)
        lines.append(f"        with Rung({conds}):")
        lines.extend(_rung_body_lines(rs))
    if spec.subroutines:
        lines.extend(_subroutine_lines(spec.subroutines))
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


def format_reachability_reproducer(
    spec: ProgramSpec,
    scan: int,
    input_history: list[dict[str, bool | int]],
    projection: list[str],
    sim_state: dict[str, Any],
    bfs_size: int,
) -> str:
    global _REF_MAP  # noqa: PLW0603
    _REF_MAP = _build_ref_map(spec.pool)
    lines = [
        '"""Reproducer: reachability cross-check — simulation state not in BFS set."""',
        "",
        "from pyrung.core import (",
        "    PLC, And, Block, Bool, Char, Counter, Dint, Int, Or, Program, Real, Rung,",
        "    TagType, Timer, Word, blockcopy, branch, calc, call, copy, count_down, count_up,",
        "    event_drum, fall, fill, forloop, latch, lro, lsh, off_delay, on_delay, out, pack_bits,",
        "    pack_words, receive, reset, return_early, rise, rro, rsh, search, shift, subroutine,",
        "    time_drum,",
        "    to_ascii, to_binary, to_text, to_value, unpack_to_bits, unpack_to_words,",
        ")",
        "from pyrung.core.analysis.prove import Intractable, reachable_states",
        "",
        "",
        "def test_reproducer():",
    ]
    for decl in _pool_decls(spec.pool):
        lines.append(f"    {decl}")
    for decl in _synthetic_tag_decls(spec.rungs, spec.subroutines):
        lines.append(f"    {decl}")
    lines.append("")
    lines.append("    with Program(strict=False) as logic:")
    for rs in spec.rungs:
        conds = ", ".join(_cond_code(c) for c in rs.conditions)
        lines.append(f"        with Rung({conds}):")
        lines.extend(_rung_body_lines(rs))
    if spec.subroutines:
        lines.extend(_subroutine_lines(spec.subroutines))
    lines.append("")
    lines.append(f"    projection = {projection!r}")
    lines.append("    bfs_result = reachable_states(logic, project=projection,")
    lines.append("                                  max_states=10_000, depth_budget=20)")
    lines.append("    assert not isinstance(bfs_result, Intractable)")
    lines.append("")
    lines.append("    plc = PLC(logic, dt=0.010)")
    for inputs in input_history[: scan + 1]:
        if inputs:
            lines.append(f"    plc.patch({inputs!r})")
        lines.append("    plc.step()")
    lines.append("")
    lines.append(f"    # Expected BFS set size: {bfs_size}")
    lines.append(f"    # Simulated state at scan {scan}: {sim_state}")
    lines.append("    tags = plc.current_state.tags")
    lines.append(f"    state = frozenset((name, tags[name]) for name in {projection!r})")
    lines.append("    assert state in bfs_result, (")
    lines.append('        f"Simulation state not in BFS set: {dict(state)}"')
    lines.append("    )")
    lines.append("")
    return "\n".join(lines)


def write_reproducer(code: str, prefix: str) -> Path:
    REPRODUCERS_DIR.mkdir(exist_ok=True)
    path = REPRODUCERS_DIR / f"{prefix}_{_RUN_ID}.py"
    path.write_text(code, encoding="utf-8")
    return path
