"""Force/patch custom-request handling for DAP.

Owns pyrungForce, pyrungUnforce, pyrungClearForces, pyrungPatch,
and pyrungListForces custom requests.  These provide structured
entry points parallel to the Debug Console text commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

HandlerResult = tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]


@dataclass(frozen=True)
class _ForceRequestArgs:
    tag: Any = None
    value: Any = None


@dataclass(frozen=True)
class _TagOnlyRequestArgs:
    tag: Any = None


@dataclass(frozen=True)
class _PatchRequestArgs:
    tag: Any = None
    value: Any = None


def _require_tag(parsed: Any, *, prefix: str, error: type[Exception]) -> str:
    tag = parsed.tag
    if not isinstance(tag, str) or not tag.strip():
        raise error(f"{prefix}.tag is required")
    return tag.strip()


def _require_value(parsed: Any, *, prefix: str, error: type[Exception]) -> bool | int | float:
    value = parsed.value
    if not isinstance(value, (bool, int, float)):
        raise error(f"{prefix}.value must be a bool or number")
    return value


def on_pyrung_force(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_ForceRequestArgs, args)
    tag_name = _require_tag(parsed, prefix="pyrungForce", error=adapter.DAPAdapterError)
    value = _require_value(parsed, prefix="pyrungForce", error=adapter.DAPAdapterError)

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        try:
            runner.force(tag_name, value)
        except KeyError as exc:
            raise adapter.DAPAdapterError(f"unknown tag: {tag_name}") from exc
        except ValueError as exc:
            raise adapter.DAPAdapterError(f"cannot force: {exc}") from exc
        except TypeError as exc:
            raise adapter.DAPAdapterError(str(exc)) from exc
        except Exception as exc:
            raise adapter.DAPAdapterError(
                f"force operation failed: {type(exc).__name__}: {exc}"
            ) from exc
    return {}, []


def on_pyrung_unforce(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_TagOnlyRequestArgs, args)
    tag_name = _require_tag(parsed, prefix="pyrungUnforce", error=adapter.DAPAdapterError)

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        try:
            runner.unforce(tag_name)
        except KeyError as exc:
            raise adapter.DAPAdapterError(f"unknown tag: {tag_name}") from exc
        except ValueError as exc:
            raise adapter.DAPAdapterError(f"cannot force: {exc}") from exc
        except Exception as exc:
            raise adapter.DAPAdapterError(
                f"force operation failed: {type(exc).__name__}: {exc}"
            ) from exc
    return {}, []


def on_pyrung_clear_forces(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        try:
            runner.clear_forces()
        except Exception as exc:
            raise adapter.DAPAdapterError(
                f"force operation failed: {type(exc).__name__}: {exc}"
            ) from exc
    return {}, []


def on_pyrung_patch(adapter: Any, args: dict[str, Any]) -> HandlerResult:
    parsed = adapter._parse_request_args(_PatchRequestArgs, args)
    tag_name = _require_tag(parsed, prefix="pyrungPatch", error=adapter.DAPAdapterError)
    value = _require_value(parsed, prefix="pyrungPatch", error=adapter.DAPAdapterError)

    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        try:
            runner.patch({tag_name: value})
        except KeyError as exc:
            raise adapter.DAPAdapterError(f"unknown tag: {tag_name}") from exc
        except ValueError as exc:
            raise adapter.DAPAdapterError(f"cannot patch: {exc}") from exc
        except TypeError as exc:
            raise adapter.DAPAdapterError(str(exc)) from exc
        except Exception as exc:
            raise adapter.DAPAdapterError(
                f"patch operation failed: {type(exc).__name__}: {exc}"
            ) from exc
    return {}, []


def on_pyrung_list_forces(adapter: Any, _args: dict[str, Any]) -> HandlerResult:
    with adapter._state_lock:
        runner = adapter._require_runner_locked()
        forces = json_safe_forces(runner.forces)
    return {"forces": forces}, []


def json_safe_forces(forces: Any) -> dict[str, bool | int | float | str]:
    """Convert forces mapping to JSON-serializable dict of primitives."""
    result: dict[str, bool | int | float | str] = {}
    for key, value in forces.items():
        if isinstance(value, bool):
            result[str(key)] = bool(value)
        elif isinstance(value, int):
            result[str(key)] = int(value)
        elif isinstance(value, float):
            result[str(key)] = float(value)
        else:
            result[str(key)] = str(value)
    return result
