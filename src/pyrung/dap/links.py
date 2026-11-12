"""Bench links: session feedback wiring layered over the program's declared links.

A program declares feedback with ``physical=`` + ``link=``; the harness runs
those couplings.  Bench links change that wiring for this debug session only,
without editing the program: add a link, replace a declared one, or cut it to
simulate a failed sensor.  The adapter owns the overlay, so every client (VS
Code, ``pyrung live``, ClickNick) sees and edits the same wiring, and it
survives ``reload``.

Each entry is keyed by feedback tag and holds a ``Coupling`` to run, or
``None`` to cut the declared link.  Entries can be disabled one at a time, and
``links bench off`` sets every entry aside without forgetting it.

Console verbs: ``link``, ``unlink``, ``restore``, ``links``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pyrung.core.harness import (
    Coupling,
    _parse_link_spec,
    _resolve_trigger_value,
    format_link_spec,
    link_problem,
)
from pyrung.core.physical import PHYSICAL_KEYS, Physical, parse_duration
from pyrung.core.tag import TagType
from pyrung.dap import execution_flow
from pyrung.dap.console import ConsoleResult, register
from pyrung.dap.grammar import Slot

#: Keys a link takes after its two tags, as in a Click nickname comment.
LINK_KEYS = (*PHYSICAL_KEYS, "physical", "system")


@dataclass(frozen=True)
class BenchLink:
    """One bench change on a feedback tag: a coupling to run, or ``None`` to cut."""

    coupling: Coupling | None
    enabled: bool = True


BenchDrop = tuple[str, BenchLink, str, str]
"""The feedback, entry, reason and tag of a bench change dropped at a harness install.

The reason is "gone" (the tag is not in the new program), "unlinked" (a cut whose
feedback the program no longer links), "in program" or "invalid".  The tag is the one
the reason is about, the enable when the enable left.
"""


# ---------------------------------------------------------------------------
# Building and describing links
# ---------------------------------------------------------------------------


def build_coupling_locked(
    adapter: Any, feedback: str, enable: str, keys: Mapping[str, str | bool]
) -> Coupling:
    """Resolve and validate one link: *enable* (``Tag``, ``Tag:trigger`` or ``~Tag``) drives
    *feedback*.

    Both tags may be tag names or Click addresses.  *keys* are ``LINK_KEYS``
    values as written in a comment (``on_delay="2s"``), with a bare flag such as
    ``toggle`` given as ``True``.  The physical name defaults to the feedback's
    own name.
    """
    unknown = sorted(set(keys) - set(LINK_KEYS))
    if unknown:
        raise adapter.DAPAdapterError(
            f"unknown link key(s): {', '.join(unknown)} (use {', '.join(LINK_KEYS)})"
        )
    runner = adapter._require_runner_locked()
    fb_name = adapter._resolve_target_locked(feedback)
    try:
        en_text, trigger_raw = _parse_link_spec(enable)
    except ValueError as exc:
        raise adapter.DAPAdapterError(f"cannot link {fb_name}: {exc}") from exc
    en_name = adapter._resolve_target_locked(en_text)
    try:
        name = keys.get("physical")
        system = keys.get("system")
        physical = Physical.from_text(
            name if isinstance(name, str) and name else fb_name,
            {key: value for key, value in keys.items() if key in PHYSICAL_KEYS},
            system=system if isinstance(system, str) else None,
        )
        en_tag = runner._tag_known_or_reached(en_name)
        trigger = (
            _resolve_trigger_value(trigger_raw, en_tag)
            if trigger_raw and en_tag is not None
            else None
        )
    except ValueError as exc:
        raise adapter.DAPAdapterError(f"cannot link {fb_name}: {exc}") from exc
    coupling = Coupling(en_name, fb_name, physical, trigger)
    problem = link_problem(runner, coupling)
    if problem is not None:
        raise adapter.DAPAdapterError(f"cannot link {fb_name}: {problem}")
    return coupling


def same_wiring(a: Coupling, b: Coupling) -> bool:
    """True when two couplings drive the same feedback the same way (names of the
    physical device aside)."""
    pa, pb = a.physical, b.physical
    return (
        a.en_name == b.en_name
        and a.fb_name == b.fb_name
        and a.trigger_value == b.trigger_value
        and _response(pa) == _response(pb)
    )


def _response(physical: Physical) -> tuple[Any, ...]:
    """The kind and its values, a time in ms, so ``1s`` matches ``1000ms`` and a missing delay 0."""
    values: list[Any] = [physical.kind]
    for key in physical.kind_spec.keys:
        value = getattr(physical, key.name)
        if key.value_type == "duration":
            value = parse_duration(value) if value is not None else 0
        values.append(value)
    return tuple(values)


def _is_bit(adapter: Any, tag_name: str) -> bool:
    tag = adapter._require_runner_locked()._tag_known_or_reached(tag_name)
    return tag is not None and tag.type == TagType.BOOL


def enable_text(adapter: Any, coupling: Coupling) -> str:
    """The enable as a ``link=`` value: ``Tag``, ``Tag:trigger`` or ``~Tag``."""
    return format_link_spec(
        coupling.en_name, coupling.trigger_value, bit=_is_bit(adapter, coupling.en_name)
    )


def physical_tokens(coupling: Coupling) -> list[str]:
    """The ``LINK_KEYS`` tokens that describe *coupling*'s response."""
    physical = coupling.physical
    tokens = physical.tokens()
    if physical.name != coupling.fb_name:
        tokens.append(f"physical={physical.name}")
    if physical.system is not None:
        tokens.append(f"system={physical.system}")
    return tokens


def link_command(adapter: Any, coupling: Coupling) -> str:
    """The console command that creates *coupling* as a bench link."""
    return " ".join(
        ["link", coupling.fb_name, enable_text(adapter, coupling), *physical_tokens(coupling)]
    )


# ---------------------------------------------------------------------------
# Overlay state
# ---------------------------------------------------------------------------


def active_overrides(adapter: Any) -> dict[str, Coupling | None]:
    """The bench entries the harness should run now."""
    return _active(adapter._bench_links, adapter._bench_links_on)


def bench_active(entry: BenchLink, bench_on: bool) -> bool:
    """Whether the harness runs *entry*.  A disabled entry, or any entry with bench off, waits."""
    return bench_on and entry.enabled


def _active(bench_links: Mapping[str, BenchLink], bench_on: bool) -> dict[str, Coupling | None]:
    return {
        fb: entry.coupling for fb, entry in bench_links.items() if bench_active(entry, bench_on)
    }


def _declared(adapter: Any) -> dict[str, Coupling]:
    harness = adapter._harness
    if harness is None:
        return {}
    return {c.fb_name: c for c in harness.declared()}


def links_changed(adapter: Any) -> None:
    """Tell clients the wiring changed, and restart a scan stepped part way."""
    adapter._links_revision += 1
    execution_flow.invalidate_mid_scan(adapter)


def sync_harness_locked(adapter: Any) -> None:
    """Run the declared links plus the active bench links, installing the harness if needed."""
    overrides = active_overrides(adapter)
    harness = adapter._harness
    if harness is None and any(c is not None for c in overrides.values()):
        from pyrung.dap.harness_console import install_harness

        harness = install_harness(adapter)
        harness.set_overrides(overrides)
        # The program's own couplings settle as at any install.  A link the user adds
        # with `link` takes effect as it is, so no bench link settles here.
        harness._settle(skip=overrides)
    elif harness is not None:
        harness.set_overrides(overrides)
    links_changed(adapter)


def fit_bench_links(
    runner: Any,
    harness: Any,
    bench_links: Mapping[str, BenchLink],
    bench_on: bool,
    drops: list[BenchDrop] | None = None,
) -> tuple[dict[str, BenchLink], list[str]]:
    """Fit *bench_links* to a harness freshly installed on *runner* (launch, reload, replay).

    Drops a bench link the program now declares the same way (it reached the
    project), a cut whose declared link is gone, and a link that no longer
    validates against the reloaded program.  Returns the bench links kept and a note
    per dropped entry.  Each drop is also appended to *drops* (see :data:`BenchDrop`).
    The adapter is not touched, so a reload can stage the fit and still back out.
    """
    declared = {c.fb_name: c for c in harness.declared()}
    notes: list[str] = []
    kept: dict[str, BenchLink] = {}
    dropped = drops if drops is not None else []
    for fb, entry in bench_links.items():
        coupling = entry.coupling
        if coupling is None:
            if fb in declared:
                kept[fb] = entry
            else:
                notes.append(f"Dropped the bench cut on {fb}: the program no longer links it.")
                gone = fb not in runner._known_tags_by_name
                dropped.append((fb, entry, "gone" if gone else "unlinked", fb))
            continue
        project = declared.get(fb)
        if project is not None and same_wiring(coupling, project):
            notes.append(f"Bench link on {fb} is now in the program.")
            dropped.append((fb, entry, "in program", fb))
            continue
        problem = link_problem(runner, coupling)
        if problem is not None:
            notes.append(f"Dropped the bench link on {fb}: {problem}.")
            if coupling.fb_name not in runner._known_tags_by_name:
                dropped.append((fb, entry, "gone", fb))
            elif coupling.en_name not in runner._known_tags_by_name:
                dropped.append((fb, entry, "gone", coupling.en_name))
            else:
                dropped.append((fb, entry, "invalid", fb))
            continue
        kept[fb] = entry
    harness.set_overrides(_active(kept, bench_on))
    return kept, notes


def set_link_locked(adapter: Any, coupling: Coupling) -> str:
    """Add or replace the bench link on the coupling's feedback."""
    fb = coupling.fb_name
    project = _declared(adapter).get(fb)
    if project is not None and same_wiring(coupling, project):
        adapter._bench_links.pop(fb, None)
        text = f"{fb} already has this link in the program; bench change removed"
    else:
        adapter._bench_links[fb] = BenchLink(coupling)
        text = f"Linked {enable_text(adapter, coupling)} -> {fb} on the bench"
    sync_harness_locked(adapter)
    return text


def unlink_locked(adapter: Any, feedback: str) -> str:
    """Cut the program's link on *feedback*, or remove its bench-only link."""
    fb = adapter._resolve_target_locked(feedback)
    if fb in _declared(adapter):
        adapter._bench_links[fb] = BenchLink(None)
        text = f"Cut the link on {fb} on the bench; it is a free input now"
    elif fb in adapter._bench_links:
        del adapter._bench_links[fb]
        text = f"Removed the bench link on {fb}"
    else:
        raise adapter.DAPAdapterError(f"{fb} has no link")
    sync_harness_locked(adapter)
    return text


def restore_locked(adapter: Any, feedback: str) -> str:
    """Drop the bench change on *feedback*, back to what the program declares."""
    fb = adapter._resolve_target_locked(feedback)
    if fb not in adapter._bench_links:
        raise adapter.DAPAdapterError(f"{fb} has no bench change")
    del adapter._bench_links[fb]
    sync_harness_locked(adapter)
    if fb in _declared(adapter):
        return f"Restored the program's link on {fb}"
    return f"Removed the bench link on {fb}"


def set_bench_locked(adapter: Any, on: bool) -> str:
    adapter._bench_links_on = bool(on)
    sync_harness_locked(adapter)
    return f"Bench wiring {'on' if on else 'off'}"


def set_enabled_locked(adapter: Any, feedback: str, enabled: bool) -> str:
    fb = adapter._resolve_target_locked(feedback)
    entry = adapter._bench_links.get(fb)
    if entry is None:
        raise adapter.DAPAdapterError(f"{fb} has no bench change")
    adapter._bench_links[fb] = BenchLink(entry.coupling, enabled=bool(enabled))
    sync_harness_locked(adapter)
    return f"{'Enabled' if enabled else 'Disabled'} the bench change on {fb}"


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _coupling_fields(coupling: Coupling) -> dict[str, Any]:
    physical = coupling.physical
    return {
        "feedback": coupling.fb_name,
        "enable": coupling.en_name,
        "trigger": coupling.trigger_value,
        "kind": physical.kind,
        **{key.dap_field: False if key.is_flag else None for key in PHYSICAL_KEYS.values()},
        **{PHYSICAL_KEYS[key].dap_field: text for key, text in physical.comment_text().items()},
        "keys": " ".join(physical.tokens()),
        "physical": physical.name,
        "system": physical.system,
    }


def link_rows_locked(adapter: Any) -> list[dict[str, Any]]:
    """Every program link and bench change, with whether it runs right now.

    Program rows have ``source="project"``; ``active`` is false while a bench
    change replaces or cuts them.  Bench rows have ``source="bench"``, their
    ``enabled`` flag, and ``cut=True`` for a cut (no enable or response).
    """
    harness = adapter._harness
    overrides = active_overrides(adapter) if harness is not None else {}

    def running(fb: str) -> bool:
        return harness is not None and harness.pending(fb)

    rows: list[dict[str, Any]] = []
    for coupling in harness.declared() if harness is not None else ():
        active = coupling.fb_name not in overrides
        rows.append(
            {
                **_coupling_fields(coupling),
                "source": "project",
                "active": active,
                "pending": active and running(coupling.fb_name),
            }
        )
    for fb, entry in adapter._bench_links.items():
        active = harness is not None and fb in overrides
        if entry.coupling is None:
            rows.append(
                {
                    "feedback": fb,
                    "source": "bench",
                    "cut": True,
                    "enabled": entry.enabled,
                    "active": active,
                    "pending": False,
                }
            )
            continue
        rows.append(
            {
                **_coupling_fields(entry.coupling),
                "source": "bench",
                "cut": False,
                "enabled": entry.enabled,
                "active": active,
                "pending": active and running(fb),
            }
        )
    return rows


def format_links_locked(adapter: Any) -> str:
    rows = link_rows_locked(adapter)
    lines = [f"Bench wiring: {'on' if adapter._bench_links_on else 'off'}"]
    if not rows:
        lines.append("  no links")
    bench = {row["feedback"]: row for row in rows if row["source"] == "bench"}
    for row in rows:
        fb = row["feedback"]
        if row.get("cut"):
            text = f"  bench    {fb} cut"
        else:
            enable = format_link_spec(
                row["enable"], row["trigger"], bit=_is_bit(adapter, row["enable"])
            )
            text = f"  {row['source']:<8} {enable} -> {fb}  {row['keys']}"
        if row["source"] == "project" and not row["active"]:
            change = bench.get(fb)
            text += (
                " (cut on the bench)"
                if change and change.get("cut")
                else " (replaced on the bench)"
            )
        elif row["source"] == "bench" and not row["enabled"]:
            text += " (disabled)"
        if row["pending"]:
            text += " [pending]"
        lines.append(text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Console verbs
# ---------------------------------------------------------------------------

_LINK_USAGE = "link <feedback> <[~]enable[:trigger]> key=value ..."


def parse_link_keys(adapter: Any, tokens: list[str]) -> dict[str, str | bool]:
    """Read ``key=value`` tokens, and a bare flag such as ``toggle`` as ``True``."""
    keys: dict[str, str | bool] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep and key in PHYSICAL_KEYS and PHYSICAL_KEYS[key].is_flag:
            keys[key] = True
            continue
        if sep != "=" or not key or not value:
            raise adapter.DAPAdapterError(f"expected key=value, got {token!r}")
        keys[key] = value
    return keys


@register(
    "link",
    usage=_LINK_USAGE,
    group="data",
    hint="(bench wiring)",
    slots=(
        Slot(kind="tag", label="feedback"),
        Slot(kind="tag", label="enable"),
        Slot(kind="text", label="key=value ...", required=False),
    ),
)
def _cmd_link(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) < 4:
        raise adapter.DAPAdapterError(f"Usage: {_LINK_USAGE}")
    coupling = build_coupling_locked(
        adapter, parts[1], parts[2], parse_link_keys(adapter, parts[3:])
    )
    return ConsoleResult(set_link_locked(adapter, coupling))


@register("unlink", usage="unlink <tag>", group="data", hint="(cut on the bench)")
def _cmd_unlink(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) != 2:
        raise adapter.DAPAdapterError("Usage: unlink <tag>")
    return ConsoleResult(unlink_locked(adapter, parts[1]))


@register("restore", usage="restore <tag>", group="data", hint="(drop a bench change)")
def _cmd_restore(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) != 2:
        raise adapter.DAPAdapterError("Usage: restore <tag>")
    return ConsoleResult(restore_locked(adapter, parts[1]))


_LINKS_USAGE = "links [bench on|off | enable <tag> | disable <tag>]"


@register(
    "links",
    usage=_LINKS_USAGE,
    group="data",
    slots=(
        Slot(
            kind="choices",
            label="action",
            required=False,
            choices=("bench", "enable", "disable"),
        ),
        Slot(kind="text", label="on|off or tag", required=False),
    ),
)
def _cmd_links(adapter: Any, expression: str) -> ConsoleResult:
    parts = expression.strip().split()
    if len(parts) == 1:
        adapter._require_runner_locked()
        return ConsoleResult(format_links_locked(adapter))
    if len(parts) == 3 and parts[1] == "bench" and parts[2] in ("on", "off"):
        return ConsoleResult(set_bench_locked(adapter, parts[2] == "on"))
    if len(parts) == 3 and parts[1] in ("enable", "disable"):
        return ConsoleResult(set_enabled_locked(adapter, parts[2], parts[1] == "enable"))
    raise adapter.DAPAdapterError(f"Usage: {_LINKS_USAGE}")
