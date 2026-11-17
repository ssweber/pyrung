"""The pass pipeline that builds how()'s static facts (``_PriorFacts``)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.simplified import Expr
from pyrung.core.kernel import PROVE_FOR_ITERATION_CAP, CompiledKernel, ForIterationsBeyondCap

from .absorb import _prior_key_done_specs, _ThresholdAbsorptions
from .classify import (
    FAULT_FLAGS,
    _classify_dimensions_from_graph,
    _classify_profile_emit,
    _classify_profile_enabled,
    _classify_profile_mark,
    _collect_all_exprs,
    _collect_literal_write_domains,
    _collect_structural_domain_info,
)
from .elision import _elide_scan_local_stateful_dims
from .results import Intractable

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program

    from .absorb import _StateKeyDoneSpec, _ThresholdVectorSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PriorFacts:
    """What how()'s domain prior reads."""

    nondeterministic_dims: dict[str, tuple[Any, ...]]
    stateful_dims: dict[str, tuple[Any, ...]]
    stateful_names: tuple[str, ...]
    elided_tags: dict[str, str]
    functional_dep_projections: dict[str, tuple[str, int, int | float]]
    stepping_tags: frozenset[str]
    state_key_done_specs: tuple[_StateKeyDoneSpec, ...]
    threshold_vector_specs: tuple[_ThresholdVectorSpec, ...]


@dataclass
class _PassContext:
    """Mutable accumulator the passes build the facts in."""

    program: Program
    dt: float = 0.010
    # The kernel the elision pass probes with, compiled there when None.
    compiled: CompiledKernel | None = None

    graph: ProgramGraph | None = None
    all_exprs: list[Expr] | None = None
    intractable: Intractable | None = None

    stateful_dims: dict[str, tuple[Any, ...]] | None = None
    nondeterministic_dims: dict[str, tuple[Any, ...]] | None = None
    done_acc: dict[str, str] | None = None
    done_presets: dict[str, int] | None = None
    done_kinds: dict[str, str] | None = None

    # The absorptions classification made, and the PILOT world-key specs built
    # from them (absorb.py).
    threshold_absorptions: _ThresholdAbsorptions | None = None
    state_key_done_specs: tuple[Any, ...] | None = None

    stateful_names: tuple[str, ...] | None = None
    receive_dest_names: frozenset[str] = frozenset()
    _stepping_tags: frozenset[str] | None = None
    _elided_tags: dict[str, str] | None = None
    _functional_dep_projections: dict[str, tuple[str, int, int | float]] | None = None
    # Each init constant with its representative value and method.
    _init_constant_projections: dict[str, tuple[str, Any]] | None = None
    # Written tags the classification found no finite domain for, and the tags an
    # Intractable classification named.  Only the facts check in tests/facts reads them.
    _unclassified_written: frozenset[str] = frozenset()
    _pending_infeasible_tags: list[str] = field(default_factory=list)
    # The (domains, blockers) fixpoint, computed once per pipeline run (see
    # _get_structural_domain_info).
    _structural_domain_info: tuple[dict[str, tuple[Any, ...]], frozenset[str]] | None = None

    def prior_facts(self) -> _PriorFacts:
        assert self.stateful_dims is not None
        assert self.nondeterministic_dims is not None
        assert self.stateful_names is not None
        return _PriorFacts(
            nondeterministic_dims=self.nondeterministic_dims,
            stateful_dims=self.stateful_dims,
            stateful_names=self.stateful_names,
            elided_tags=dict(self._elided_tags or {}),
            functional_dep_projections=dict(self._functional_dep_projections or {}),
            stepping_tags=self._stepping_tags or frozenset(),
            state_key_done_specs=self.state_key_done_specs or (),
            threshold_vector_specs=(
                self.threshold_absorptions.vector_specs
                if self.threshold_absorptions is not None
                else ()
            ),
        )


def _pass_build_graph(ctx: _PassContext) -> None:
    ctx.graph = build_program_graph(ctx.program)
    ctx.all_exprs = _collect_all_exprs(ctx.program, ctx.graph)
    ctx.receive_dest_names = frozenset(_collect_receive_dest_names(ctx.program))


def _pass_classify_dimensions(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.all_exprs is not None
    profile = _classify_profile_enabled()
    timings: list[tuple[str, float]] = []
    profile_start = profile_last = time.perf_counter() if profile else 0.0
    unclassified: set[str] = set()
    stepping: set[str] = set()
    threshold_absorptions: list[_ThresholdAbsorptions] = []
    structural_domain_info = _get_structural_domain_info(ctx)
    if profile:
        profile_last = _classify_profile_mark(timings, "structural_domain_info", profile_last)
    result = _classify_dimensions_from_graph(
        ctx.program,
        ctx.graph,
        ctx.all_exprs,
        structural_domain_info=structural_domain_info,
        receive_dest_names=ctx.receive_dest_names,
        unclassified=unclassified,
        stepping_tags_out=stepping,
        threshold_absorptions_out=threshold_absorptions,
    )
    if profile:
        profile_last = _classify_profile_mark(timings, "classify_from_graph", profile_last)
    if threshold_absorptions:
        ctx.threshold_absorptions = threshold_absorptions[-1]
    ctx._stepping_tags = frozenset(stepping)
    ctx._unclassified_written = frozenset(unclassified)
    if isinstance(result, Intractable):
        ctx._pending_infeasible_tags.extend(result.tags)
        # The facts still build on the partial classification.
        if result._debug_context is not None:
            sd, nd, _comb, da, dp, dk = result._debug_context
            ctx.stateful_dims = sd
            ctx.nondeterministic_dims = nd
            ctx.done_acc = da
            ctx.done_presets = dp
            ctx.done_kinds = dk
        if profile:
            _classify_profile_emit(
                "_pass_classify_dimensions",
                timings,
                time.perf_counter() - profile_start,
                result="intractable",
                unclassified=len(unclassified),
                stepping=len(stepping),
            )
        return
    sd, nd, _comb, da, dp, dk = result
    ctx.stateful_dims = sd
    ctx.nondeterministic_dims = nd
    ctx.done_acc = da
    ctx.done_presets = dp
    ctx.done_kinds = dk
    if profile:
        _classify_profile_mark(timings, "ctx_store", profile_last)
        _classify_profile_emit(
            "_pass_classify_dimensions",
            timings,
            time.perf_counter() - profile_start,
            result="ok",
            stateful=len(sd),
            nondeterministic=len(nd),
            combinational=len(_comb),
            done=len(da),
            stepping=len(stepping),
            unclassified=len(unclassified),
        )


def _collect_receive_dest_names(program: Program) -> set[str]:
    from pyrung.core.instruction.send_receive._core import ModbusReceiveInstruction
    from pyrung.core.validation._common import walk_instructions

    names: set[str] = set()
    for instr in walk_instructions(program):
        if not isinstance(instr, ModbusReceiveInstruction):
            continue
        dest = instr.dest
        if hasattr(dest, "name"):
            names.add(dest.name)
        elif hasattr(dest, "tags"):
            for tag in dest.tags():
                names.add(tag.name)
    return names


def _pass_elide_scan_local_state(ctx: _PassContext) -> None:
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    from .expr import _edge_source_tags

    assert ctx.graph is not None
    assert ctx.stateful_dims is not None and ctx.nondeterministic_dims is not None
    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program, blockless=True, proof_metadata=True)
    original_stateful_dims = dict(ctx.stateful_dims)
    # Every written tag that is not state may be read after the scan.
    observed = frozenset(ctx.graph.writers_of) - frozenset(original_stateful_dims)
    elidable_dims, elided_dict = _elide_scan_local_stateful_dims(
        ctx.program,
        ctx.graph,
        original_stateful_dims,
        ctx.nondeterministic_dims,
        observed=observed,
    )
    # An edge source keeps its previous value from scan to scan, so it is not
    # listed as elided.
    edge_sources = _edge_source_tags(ctx.program)
    ctx.stateful_dims = elidable_dims
    ctx._elided_tags = {
        name: method for name, method in elided_dict.items() if name not in edge_sources
    }


def _writers_all_unconditional(
    y_name: str,
    y_writers: frozenset[int],
    graph: Any,
    program: Any,
) -> bool:
    """Return True when every writer of Y is an unconditional top-level rung in one scope.

    The gate for ND-input-sourced functional deps: with no X writers the
    ``x_writers <= y_writers`` containment is vacuous, so this is what
    separates "Y is recomputed from the input every scan" (``math(100 -
    analog, pv)``) from a gated ``copy(REF, y)`` where Y holds its old
    value whenever the gate is off.  A write the rung's power alone does not
    decide (a oneshot, a forloop body: :func:`_writes_run_when_powered`) is
    gated too.
    """
    if not y_writers:
        return False
    y_scopes = {(graph.rung_nodes[i].subroutine, graph.rung_nodes[i].scope) for i in y_writers}
    if len(y_scopes) != 1:
        return False
    y_sub, _y_scope = next(iter(y_scopes))
    if y_sub is not None:
        scope_rungs = program.subroutines.get(y_sub, [])
    else:
        scope_rungs = program.rungs
    for y_idx in y_writers:
        y_node = graph.rung_nodes[y_idx]
        if y_node.branch_path:
            return False
        if y_node.rung_index >= len(scope_rungs):
            return False
        if y_name not in y_node.sure_writes:
            return False  # a pointer or a text writes Y only for some values
        rung = scope_rungs[y_node.rung_index]
        if rung._conditions:
            return False
        if not _writes_run_when_powered(rung._instructions, y_name):
            return False
    return True


def _instructions_write(instructions: Iterable[Any], name: str) -> bool:
    """Return whether any of *instructions*, forloop bodies included, writes *name*."""
    from pyrung.core.instruction.control import ForLoopInstruction

    from .absorb import _all_write_targets

    for instr in instructions:
        if any(target == name for target, _itype in _all_write_targets(instr)):
            return True
        if isinstance(instr, ForLoopInstruction) and _instructions_write(instr.instructions, name):
            return True
    return False


def _writes_run_when_powered(instructions: Iterable[Any], name: str) -> bool:
    """Return True when every write of *name* among *instructions* runs on each scan.

    The write runs on each scan that its rung is on.

    A rung's contacts are not all that decide whether a write on it runs.  A
    oneshot write runs only on the rung's rising edge.  A forloop body runs only
    when its loop does.  A oneshot For runs only on the rung's rising edge.  A For
    whose count is not a literal of at least one iteration may run none (a tag
    count of 0, :func:`for_loop_iterations`).  A body rung's contacts gate its
    writes, and a ``break_loop()`` / ``continue_loop()`` in one gates the rest
    of the body, so a loop with body rungs that writes *name* is refused whole.
    *instructions* is one node's own list (``rung._instructions``); a branch is
    a node of its own.
    """
    from pyrung.core.instruction.control import (
        ForLoopInstruction,
        for_count_mask,
        for_loop_iterations,
    )

    for instr in instructions:
        if isinstance(instr, ForLoopInstruction):
            if not _instructions_write(instr.instructions, name):
                continue
            count = instr.count
            literal = isinstance(count, int) and not isinstance(count, bool)
            if instr._oneshot or instr.body_rungs or not literal:
                return False
            if for_loop_iterations(count, for_count_mask(count)) == 0:
                return False
            if not _writes_run_when_powered(instr.instructions, name):
                return False
        elif getattr(instr, "_oneshot", False) and _instructions_write((instr,), name):
            return False
    return True


def _is_sequential_unconditional_same_scope(
    x_name: str,
    y_name: str,
    graph: Any,
    program: Any,
) -> bool:
    """Check if Y is safely derived from X via sequential unconditional writes.

    Returns True when:
    - X and Y are written in the same subroutine (or both main-line)
    - Y's writer follows X's last writer in node order within that scope
    - Y's writer rung is unconditional (no conditions on the rung itself),
      and Y is not written in one of its branches
    - No ``return_early()`` between X's last writer and Y's writer
    - Every write of Y on that rung runs each scan: no oneshot, and no forloop
      that may skip it (:func:`_writes_run_when_powered`)
    """
    from pyrung.core.analysis.pdg import _rung_has_return

    x_writers = graph.writers_of.get(x_name, frozenset())
    y_writers = graph.writers_of.get(y_name, frozenset())
    if not x_writers or not y_writers:
        return False

    y_scopes = {(graph.rung_nodes[i].subroutine, graph.rung_nodes[i].scope) for i in y_writers}
    if len(y_scopes) != 1:
        return False
    y_sub, y_scope = next(iter(y_scopes))

    if y_sub is not None:
        scope_rungs = program.subroutines.get(y_sub, [])
    else:
        scope_rungs = program.rungs

    for y_idx in y_writers:
        y_node = graph.rung_nodes[y_idx]
        if y_node.branch_path:
            return False
        if y_node.rung_index >= len(scope_rungs):
            return False
        if y_name not in y_node.sure_writes:
            return False  # a pointer or a text writes Y only for some values
        rung = scope_rungs[y_node.rung_index]
        if rung._conditions:
            return False
        if not _writes_run_when_powered(rung._instructions, y_name):
            return False

    x_in_scope = [
        i
        for i in x_writers
        if graph.rung_nodes[i].subroutine == y_sub and graph.rung_nodes[i].scope == y_scope
    ]
    if not x_in_scope:
        return False

    x_last = max(x_in_scope)
    y_first = min(y_writers)
    if x_last >= y_first:
        return False

    x_last_rung_index = graph.rung_nodes[x_last].rung_index
    y_first_rung_index = graph.rung_nodes[y_first].rung_index
    for ri in range(x_last_rung_index + 1, y_first_rung_index):
        if ri >= len(scope_rungs):
            break
        if _rung_has_return(scope_rungs[ri]):
            return False

    return True


def _pass_detect_functional_dependencies(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.stateful_dims is not None

    from pyrung.core.analysis.affine import extract_forward_affine
    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.validation._common import walk_instructions

    from .absorb import _all_write_targets
    from .expr import _edge_source_tags

    source_offsets: dict[str, set[tuple[str, int, int | float]]] = {}
    disqualified: set[str] = set()

    # Slice elision has already claimed write-before-read scratch (the
    # jump-table pointer idiom: ``math(S_StateRequested + 150,
    # sm__jump_target_ds_idx)``), but the walker still wants the
    # ``y = x + offset`` relation as idx-chase advice.  Elided tags are
    # candidates whose projections are advice only, with no dims or elision
    # bookkeeping.  The interpreted trial checks every walker use.
    admissible = set(ctx.stateful_dims)
    if ctx._elided_tags:
        admissible.update(ctx._elided_tags)

    for instr in walk_instructions(ctx.program):
        targets = [name for name, _itype in _all_write_targets(instr)]
        if not targets:
            continue
        fwd = extract_forward_affine(instr)
        for target_name in targets:
            if target_name not in admissible or target_name in disqualified:
                continue
            if fwd is None:
                disqualified.add(target_name)
                source_offsets.pop(target_name, None)
            else:
                source_offsets.setdefault(target_name, set()).add(fwd)

    edge_sources = _edge_source_tags(ctx.program)
    candidates: dict[str, tuple[str, int, int | float]] = {}
    advice_only: set[str] = set()
    for y_name, offsets in source_offsets.items():
        if y_name in disqualified:
            continue
        if len(offsets) != 1:
            continue
        x_name, scale, offset = next(iter(offsets))
        if x_name == y_name:
            continue
        # A fault flag moves at instructions the write targets do not list
        # (``_fault_writes``), so a copy of one never keeps up with it.
        if x_name in FAULT_FLAGS or y_name in FAULT_FLAGS:
            continue
        # Walk-grade widenings: a negated form (``lit - x``) or an
        # ND-input source cannot drop a tag from the world key, but it is
        # what the walker's chase needs (``pv = 100 - analog_input``).  They
        # are advice only.
        walk_grade = scale != 1 or x_name not in ctx.stateful_dims
        x_writers = ctx.graph.writers_of.get(x_name, frozenset())
        y_writers = ctx.graph.writers_of.get(y_name, frozenset())
        if walk_grade:
            if x_name not in ctx.stateful_dims:
                if ctx.nondeterministic_dims is None or x_name not in ctx.nondeterministic_dims:
                    continue
                # An ND input has no writers, so the ``x_writers <=
                # y_writers`` containment below is vacuous — require Y be
                # recomputed every scan instead, else a gated
                # ``copy(REF, y)`` masquerades as a dep (y holds its old
                # value while the gate is off) and the chase hops onto
                # the REF register that ``ref_constant_order`` defers.
                if not _writers_all_unconditional(y_name, y_writers, ctx.graph, ctx.program):
                    continue
        if y_name in edge_sources:
            continue
        if walk_grade or y_name not in ctx.stateful_dims:
            advice_only.add(y_name)
        # Containment: every rung that writes X writes Y too, and on each of
        # them Y's write runs whenever the rung does (not a oneshot, not in a
        # forloop that may skip it), so X never moves without Y following.
        contained = x_writers <= y_writers and all(
            (rung := resolve_rung(ctx.program, ctx.graph.rung_nodes[i])) is not None
            and _writes_run_when_powered(rung._instructions, y_name)
            for i in x_writers
        )
        if not contained:
            if not _is_sequential_unconditional_same_scope(x_name, y_name, ctx.graph, ctx.program):
                # Ordering violations (the CopyOrJump loop rewrites x
                # after y's writer) make the projection unsound as a
                # state-key elision but fine as chase advice.
                advice_only.add(y_name)
        candidates[y_name] = (x_name, scale, offset)

    projected = {y: v for y, v in candidates.items() if v[0] not in candidates}

    if not projected:
        return

    if ctx._elided_tags is None:
        ctx._elided_tags = {}
    ctx._functional_dep_projections = projected
    for y_name in projected:
        if y_name in advice_only:
            continue
        del ctx.stateful_dims[y_name]
        ctx._elided_tags[y_name] = "functional_dep"


def _pass_detect_init_constants(ctx: _PassContext) -> None:
    assert ctx.graph is not None and ctx.stateful_dims is not None
    assert ctx.nondeterministic_dims is not None

    from pyrung.core.analysis.init_constants import detect_init_constants
    from pyrung.core.validation._common import _collect_write_sites

    from .absorb import _all_write_targets
    from .expr import _edge_source_tags

    all_sites = _collect_write_sites(ctx.program, target_extractor=_all_write_targets)
    sites_by_target: dict[str, list[Any]] = {}
    for site in all_sites:
        sites_by_target.setdefault(site.target_name, []).append(site)

    projected = detect_init_constants(
        program=ctx.program,
        graph=ctx.graph,
        sites_by_target=sites_by_target,
        # The write sites leave out the fault flags' implicit writers
        # (``_fault_writes``), so a flag is never taken for constant.
        candidate_tags=set(ctx.stateful_dims) - FAULT_FLAGS,
        nondeterministic_inputs=set(ctx.nondeterministic_dims),
        edge_source_tags=_edge_source_tags(ctx.program),
    )

    if not projected:
        return

    if ctx._elided_tags is None:
        ctx._elided_tags = {}
    ctx._init_constant_projections = projected
    for x_name in projected:
        if x_name in ctx.stateful_dims:
            del ctx.stateful_dims[x_name]
            ctx._elided_tags[x_name] = projected[x_name][1]


def _pass_compile_kernel(ctx: _PassContext) -> None:
    """Compile the kernel if no pass has, and name the stateful tags."""
    from pyrung.circuitpy.codegen import compile_kernel as _compile_kernel

    if ctx.compiled is None:
        ctx.compiled = _compile_kernel(ctx.program, blockless=True, proof_metadata=True)
    assert ctx.stateful_dims is not None
    ctx.stateful_names = tuple(sorted(ctx.stateful_dims))


def _pass_build_prior_key_specs(ctx: _PassContext) -> None:
    """Build the PILOT world key's Done specs (absorb.py)."""
    assert ctx.stateful_names is not None
    ctx.state_key_done_specs = _prior_key_done_specs(
        ctx.stateful_names, ctx.done_acc or {}, ctx.done_kinds or {}
    )


def _get_structural_domain_info(
    ctx: _PassContext,
) -> tuple[dict[str, tuple[Any, ...]], frozenset[str]]:
    """Compute the structural domains and reverse blockers once per pipeline run.

    The fixpoint over ``(program, graph, all_exprs)`` takes most of the build
    time, so *ctx* keeps it.  REAL gap points stay off so how() keeps its
    whole-number picks.
    """
    if ctx._structural_domain_info is None:
        assert ctx.graph is not None and ctx.all_exprs is not None
        literal_write_domains = _collect_literal_write_domains(ctx.program, ctx.graph.tags)
        ctx._structural_domain_info = _collect_structural_domain_info(
            ctx.program,
            ctx.graph,
            ctx.all_exprs,
            literal_write_domains,
            real_gap_points=False,
        )
    return ctx._structural_domain_info


def _for_iterations_intractable(exc: ForIterationsBeyondCap, dimensions: int) -> Intractable:
    """Return the refusal for a For that the kernel stopped past ``PROVE_FOR_ITERATION_CAP``."""
    return Intractable(
        reason=(
            f"forloop({exc.count}) at {exc.where} can run {exc.iterations} times in one"
            f" scan, more than the facts run ({PROVE_FOR_ITERATION_CAP})"
        ),
        dimensions=dimensions,
        estimated_space=0,
        tags=list(exc.tags),
        hints=[
            f"  {exc.count}: a Dint count is read whole, so a positive one can run up to"
            " 2147483647 times; narrow it with min=/max=/choices=, or test the loop"
            " with run_for()",
        ],
    )


#: The passes in the order they run.
_PRIOR_PASSES: tuple[Callable[[_PassContext], None], ...] = (
    _pass_build_graph,
    _pass_classify_dimensions,
    _pass_elide_scan_local_state,
    _pass_detect_functional_dependencies,
    _pass_detect_init_constants,
    _pass_compile_kernel,
    _pass_build_prior_key_specs,
)


def _run_prior_pipeline(ctx: _PassContext) -> _PriorFacts | Intractable:
    """Run the passes that build how()'s facts, then read them."""
    for run in _PRIOR_PASSES:
        t0 = time.monotonic()
        try:
            run(ctx)
        except ForIterationsBeyondCap as exc:
            ctx.intractable = _for_iterations_intractable(
                exc, len(ctx.stateful_dims or {}) + len(ctx.nondeterministic_dims or {})
            )
        name = getattr(run, "__name__", repr(run))
        logger.info("pass %s completed in %.2fs", name, time.monotonic() - t0)
        if ctx.intractable is not None:
            return ctx.intractable
    return ctx.prior_facts()
