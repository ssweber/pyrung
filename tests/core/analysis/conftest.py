"""Prove agreement oracle and debug context dumper."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, prove
from pyrung.core.analysis.prove import reachable_states as _original_reachable_states


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pyrung-prove", "pyrung prove agreement oracle")
    group.addoption(
        "--prove-agreement",
        action="store_true",
        default=False,
        help="Re-run every Proven result with optimizations disabled to check agreement.",
    )
    group.addoption(
        "--prove-debug",
        action="store_true",
        default=False,
        help="Inject _debug=True into prove()/reachable_states() calls and dump _ExploreContext on failure.",
    )


_NO_AGREEMENT = "_no_agreement"


def no_agreement(fn):
    """Mark a test to skip the prove agreement oracle."""
    setattr(fn, _NO_AGREEMENT, True)
    return fn


@pytest.fixture(autouse=True)
def _prove_agreement_oracle(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if not request.config.getoption("prove_agreement"):
        yield
        return

    fn = request.function
    if getattr(fn, _NO_AGREEMENT, False):
        yield
        return

    original = prove
    failures: list[str] = []

    def _checking_prove(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("_skip_optimizations"):
            return original(*args, **kwargs)

        result = original(*args, **kwargs)

        if isinstance(result, list):
            for i, r in enumerate(result):
                if isinstance(r, Proven) and not r.caveats:
                    _check_single(args, kwargs, i, failures)
        elif isinstance(result, Proven) and not result.caveats:
            _check_single(args, kwargs, None, failures)

        return result

    if not hasattr(request.module, "prove"):
        yield
        return

    monkeypatch.setattr(request.module, "prove", _checking_prove)

    yield

    if failures:
        raise AssertionError(
            "Prove agreement oracle failed — optimized returned Proven "
            "but unoptimized disagrees:\n" + "\n".join(failures)
        )


def _check_single(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    batch_index: int | None,
    failures: list[str],
) -> None:
    check_kwargs = {k: v for k, v in kwargs.items() if k != "_skip_optimizations"}
    check_kwargs["_skip_optimizations"] = True

    try:
        unopt = prove(*args, **check_kwargs)
    except Exception as exc:
        failures.append(f"  Unoptimized prove() raised {type(exc).__name__}: {exc}")
        return

    if isinstance(unopt, list) and batch_index is not None:
        unopt = unopt[batch_index]

    if isinstance(unopt, Counterexample):
        label = f"batch[{batch_index}]" if batch_index is not None else "single"
        trace_detail = "; ".join(f"inputs={s.inputs}, scans={s.scans}" for s in unopt.trace)
        failures.append(
            f"  {label}: unoptimized found Counterexample "
            f"(trace length {len(unopt.trace)}): [{trace_detail}]"
        )
    elif isinstance(unopt, Intractable):
        pass


# ---------------------------------------------------------------------------
# --prove-debug: inject _debug=True and dump _ExploreContext on failure
# ---------------------------------------------------------------------------


def _format_context(ctx: Any) -> str:
    """Format an _ExploreContext into a readable dump."""
    lines: list[str] = []
    lines.append(f"  stateful_names:         {ctx.stateful_names}")
    lines.append(f"  stateful_dims:          {dict(ctx.stateful_dims)}")
    lines.append(f"  nondeterministic_names: {ctx.nondeterministic_names}")
    lines.append(f"  nondeterministic_dims:  {dict(ctx.nondeterministic_dims)}")
    lines.append(f"  edge_tag_names:         {ctx.edge_tag_names}")
    lines.append(f"  memory_key_names:       {ctx.memory_key_names}")
    lines.append(f"  demoted_edge_names:     {ctx.demoted_edge_names}")

    lines.append(f"  threshold_vector_specs ({len(ctx.threshold_vector_specs)}):")
    for vs in ctx.threshold_vector_specs:
        lines.append(f"    acc={vs.acc_name}  kind={vs.kind}  atoms={vs.atoms}")

    lines.append(f"  done_event_specs ({len(ctx.done_event_specs)}):")
    for de in ctx.done_event_specs:
        lines.append(f"    acc={de.acc_name}  kind={de.kind}  preset={de.preset}")

    lines.append(f"  threshold_event_specs ({len(ctx.threshold_event_specs)}):")
    for te in ctx.threshold_event_specs:
        lines.append(f"    acc={te.acc_name}  kind={te.kind}  threshold={te.threshold}")

    if ctx.journal:
        lines.append(f"  journal ({len(ctx.journal)} tags):")
        for entry in ctx.journal:
            lines.append(f"    {entry.name}: {entry.outcome}")
            if entry.domain is not None:
                lines.append(f"      domain: {entry.domain} (source: {entry.domain_source})")
            for d in entry.decisions:
                lines.append(f"      [{d.pass_name}] {d.kind} -> {d.outcome}: {d.reason}")

    return "\n".join(lines)


def _dump_result(label: str, result: Any) -> str:
    """Format a prove/reachable_states result with its debug context."""
    lines: list[str] = [f"--- {label}: {type(result).__name__} ---"]

    ctx = getattr(result, "_debug_context", None)
    if ctx is not None:
        lines.append(_format_context(ctx))
    else:
        lines.append("  (no _debug_context attached)")

    if isinstance(result, Proven):
        lines.append(f"  states_explored: {result.states_explored}")
    if isinstance(result, Counterexample):
        lines.append(f"  trace ({len(result.trace)} steps):")
        for i, step in enumerate(result.trace):
            lines.append(f"    [{i}] inputs={step.inputs} scans={step.scans}")

    return "\n".join(lines)


@pytest.fixture(autouse=True)
def _prove_debug_dumper(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if not request.config.getoption("prove_debug"):
        yield
        return

    captured: list[tuple[str, Any]] = []

    original_prove = prove

    def _debug_prove(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("_debug", True)
        kwargs.setdefault("journal", True)
        result = original_prove(*args, **kwargs)
        skip = kwargs.get("_skip_optimizations", False)
        label = "prove(skip_opt)" if skip else "prove(optimized)"
        if isinstance(result, list):
            for i, r in enumerate(result):
                captured.append((f"{label}[{i}]", r))
        else:
            captured.append((label, result))
        return result

    def _debug_reachable(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("_debug", True)
        kwargs.setdefault("_journal", True)
        result = _original_reachable_states(*args, **kwargs)
        skip = kwargs.get("_skip_optimizations", False)
        label = "reachable(skip_opt)" if skip else "reachable(optimized)"
        captured.append((label, result))
        return result

    mod = request.module
    if hasattr(mod, "prove"):
        monkeypatch.setattr(mod, "prove", _debug_prove)
    if hasattr(mod, "reachable_states"):
        monkeypatch.setattr(mod, "reachable_states", _debug_reachable)

    yield

    if request.node.rep_call is not None and request.node.rep_call.failed and captured:
        out = sys.stderr
        out.write(f"\n{'=' * 70}\n")
        out.write(f"  --prove-debug dump for {request.node.nodeid}\n")
        out.write(f"{'=' * 70}\n")
        for label, result in captured:
            out.write(_dump_result(label, result))
            out.write("\n")
        out.write(f"{'=' * 70}\n")
        out.flush()


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):  # noqa: ANN001, ANN201
    """Stash the call report on the item so _prove_debug_dumper can read it."""
    import pluggy

    outcome: pluggy.Result = yield  # type: ignore[assignment]
    rep = outcome.get_result()
    if rep.when == "call":
        item.rep_call = rep  # type: ignore[attr-defined]
