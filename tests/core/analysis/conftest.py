"""Prove agreement oracle — re-run Proven results without optimizations."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.core.analysis.prove import Counterexample, Intractable, Proven, prove


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("pyrung-prove", "pyrung prove agreement oracle")
    group.addoption(
        "--prove-agreement",
        action="store_true",
        default=False,
        help="Re-run every Proven result with optimizations disabled to check agreement.",
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
