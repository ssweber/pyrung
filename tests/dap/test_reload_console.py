"""Tests for the DAP reload console commands."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import pytest

from pyrung.dap.adapter import DAPAdapter
from pyrung.dap.protocol import read_message


def _drain_messages(stream: io.BytesIO) -> list[dict[str, Any]]:
    data = stream.getvalue()
    reader = io.BytesIO(data)
    messages: list[dict[str, Any]] = []
    while True:
        message = read_message(reader)
        if message is None:
            break
        messages.append(message)
    stream.seek(0)
    stream.truncate(0)
    return messages


def _send_request(
    adapter: DAPAdapter,
    out_stream: io.BytesIO,
    *,
    seq: int,
    command: str,
    arguments: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    adapter.handle_request(
        {"seq": seq, "type": "request", "command": command, "arguments": arguments or {}}
    )
    return _drain_messages(out_stream)


def _write_script(tmp_path: Path, name: str, content: str) -> Path:
    script_path = tmp_path / name
    script_path.write_text(content, encoding="utf-8")
    return script_path


def _single_response(messages: list[dict[str, Any]]) -> dict[str, Any]:
    responses = [msg for msg in messages if msg.get("type") == "response"]
    assert len(responses) == 1
    return responses[0]


def _stopped_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [msg for msg in messages if msg.get("type") == "event" and msg.get("event") == "stopped"]


def _repl(
    adapter: DAPAdapter, out_stream: io.BytesIO, expression: str, *, seq: int = 10
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    messages = _send_request(
        adapter,
        out_stream,
        seq=seq,
        command="evaluate",
        arguments={"expression": expression, "context": "repl"},
    )
    response = _single_response(messages)
    stopped = _stopped_events(messages)
    return response, stopped


BASIC_SCRIPT = (
    "from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy\n"
    "\n"
    "button = Bool('Button')\n"
    "light = Bool('Light')\n"
    "counter = Int('Counter')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        out(light)\n"
    "    with Rung():\n"
    "        copy(0, counter)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


def _setup(
    tmp_path: Path, script: str = BASIC_SCRIPT, **launch_args: Any
) -> tuple[DAPAdapter, io.BytesIO, Path]:
    out_stream = io.BytesIO()
    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=out_stream)
    script_path = _write_script(tmp_path, "logic.py", script)
    _send_request(
        adapter,
        out_stream,
        seq=1,
        command="launch",
        arguments={"program": str(script_path), **launch_args},
    )
    _send_request(adapter, out_stream, seq=2, command="configurationDone")
    _drain_messages(out_stream)
    _send_request(adapter, out_stream, seq=3, command="next")
    _drain_messages(out_stream)
    return adapter, out_stream, script_path


TIMER_SCRIPT = (
    "from pyrung.core import PLC, Program, Rung, Timer, on_delay\n"
    "\n"
    "ton = Timer.clone('Ton')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung():\n"
    "        on_delay(ton, preset={preset}, unit='{unit}')\n"
    "\n"
    "runner = PLC(prog, dt=0.1)\n"
)


class TestReloadChangingATimersUnit:
    def _after_59_seconds_of_a_minute(self, tmp_path: Path) -> tuple[Any, ...]:
        adapter, out, script_path = _setup(tmp_path, TIMER_SCRIPT.format(preset=5, unit="Tm"))
        _repl(adapter, out, "step 590", seq=10)
        state = adapter._runner.current_state
        assert state.tags["Ton_Acc"] == 0
        assert state.memory["_frac:Ton_Acc"] == 59_000_000_000
        return adapter, out, script_path

    @pytest.mark.parametrize(("unit", "acc"), [("Tms", 100), ("Ts", 0)])
    def test_a_carry_read_by_a_shorter_unit_does_not_finish_the_timer(
        self, tmp_path: Path, unit: str, acc: int
    ):
        adapter, out, script_path = self._after_59_seconds_of_a_minute(tmp_path)
        script_path.write_text(TIMER_SCRIPT.format(preset=2000, unit=unit), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is True
        assert adapter._runner.current_state.memory["_frac:Ton_Acc"] == 0

        _repl(adapter, out, "step 1", seq=12)
        state = adapter._runner.current_state
        assert state.tags["Ton_Acc"] == acc
        assert state.tags["Ton_Done"] is False

    def test_a_carry_of_the_same_unit_is_untouched(self, tmp_path: Path):
        adapter, out, script_path = self._after_59_seconds_of_a_minute(tmp_path)
        script_path.write_text(TIMER_SCRIPT.format(preset=1, unit="Tm"), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is True
        assert adapter._runner.current_state.memory["_frac:Ton_Acc"] == 59_000_000_000

        _repl(adapter, out, "step 9", seq=12)
        assert adapter._runner.current_state.tags["Ton_Done"] is False
        _repl(adapter, out, "step 1", seq=13)
        assert adapter._runner.current_state.tags["Ton_Done"] is True


def _off_delay_script(off_delay: str) -> str:
    return (
        "from pyrung.core import Bool, PLC, Physical, Program, Rung, out\n"
        "\n"
        "en = Bool('En')\n"
        f"fb = Bool('Fb', physical=Physical('S', off_delay='{off_delay}'), link='En')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(en):\n"
        "        out(Bool('Echo'))\n"
        "    with Rung(fb):\n"
        "        out(Bool('Seen'))\n"
        "\n"
        "runner = PLC(prog, dt=0.25)\n"
    )


@pytest.mark.parametrize(
    ("off_delay", "edited", "extra_scans"),
    [("10s", "5s", 0), ("60s", "5s", 0), ("5s", "60s", 55 * 4)],
    ids=["same-unit", "narrowed-across", "widened-across"],
)
def test_a_reload_continues_a_running_off_delay(
    tmp_path: Path, off_delay: str, edited: str, extra_scans: int
):
    adapter, out, script_path = _setup(tmp_path, _off_delay_script(off_delay))
    adapter._runner.patch({"En": True})
    _repl(adapter, out, "step 10", seq=10)
    adapter._runner.patch({"En": False})
    _repl(adapter, out, "step 8", seq=11)
    assert adapter._runner.current_state.tags["Fb"] is True
    script_path.write_text(_off_delay_script(edited), encoding="utf-8")

    resp, _stopped = _repl(adapter, out, "reload", seq=12)
    assert resp["success"] is True

    runner = adapter._runner
    scans = 0
    while runner.current_state.tags["Fb"] and scans < 400:
        runner.step()
        scans += 1
    # The debounce's off side compares a scan after the fall: 15 (14 under an off-delay timer).
    assert scans == 15 + extra_scans


def _coil_script(horn: str) -> str:
    return (
        "from pyrung.core import Bool, PLC, Physical, Program, Rung, out\n"
        "\n"
        "motor = Bool('Motor')\n"
        "spare = Bool('Spare')\n"
        f"horn = {horn}\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(motor):\n"
        "        out(Bool('Echo'))\n"
        "    with Rung(spare, horn):\n"
        "        out(Bool('Seen'))\n"
        "\n"
        "runner = PLC(prog, dt=0.01)\n"
    )


def _coil_run(
    tmp_path: Path, script: str, link: str, before: int, *, reload: bool
) -> list[tuple[Any, ...]]:
    """Each scan's Spare, Horn and timer counts, after *before* scans with Motor on."""
    tmp_path.mkdir()
    adapter, out, _script_path = _setup(tmp_path, script)
    if link:
        resp, _ = _repl(adapter, out, link, seq=10)
        assert resp["success"] is True
    adapter._runner.patch({"Motor": True})
    _repl(adapter, out, f"step {before}", seq=11)
    if reload:
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
    seen = []
    for _ in range(60):
        adapter._runner.step()
        tags = adapter._runner.current_state.tags
        counts = sorted((k, v) for k, v in tags.items() if k.startswith(("__cpl_on", "__tmd_on")))
        seen.append((tags["Spare"], tags["Horn"], counts))
    return seen


@pytest.mark.parametrize(
    ("horn", "link", "before", "first", "last"),
    [
        ("Bool('Horn')", "link Spare Motor toggle", 30, (True, False), (True, False)),
        (
            "Bool('Horn')",
            "link Spare Motor timed=500ms maintained",
            30,
            (True, False),
            (True, False),
        ),
        (
            "Bool('Horn', physical=Physical('L', toggle=True), link='Motor')",
            "",
            30,
            (False, True),
            (False, True),
        ),
        (
            "Bool('Horn', physical=Physical('H', timed='500ms'), link='Motor')",
            "",
            10,
            (False, True),
            (False, False),
        ),
        (
            "Bool('Horn', physical=Physical('H', on_delay='500ms'), link='Motor')",
            "",
            10,
            (False, False),
            (False, True),
        ),
    ],
    ids=["bench-toggle", "bench-timed-maintained", "toggle", "timed-running", "on-delay-running"],
)
def test_a_reload_leaves_a_plant_coil_as_it_was(
    tmp_path: Path,
    horn: str,
    link: str,
    before: int,
    first: tuple[bool, bool],
    last: tuple[bool, bool],
):
    """A reload with the enable on neither fires a toggle or timed coil nor cuts its count short."""
    script = _coil_script(horn)
    reloaded = _coil_run(tmp_path / "reloaded", script, link, before, reload=True)
    assert reloaded == _coil_run(tmp_path / "stepped", script, link, before, reload=False)
    assert reloaded[0][:2] == first
    assert reloaded[-1][:2] == last


def _horn_bits(adapter: DAPAdapter, fb: str, scans: int) -> str:
    seen = ""
    for _ in range(scans):
        adapter._runner.step()
        seen += "#" if adapter._runner.current_state.tags[fb] else "."
    return seen


@pytest.mark.parametrize(
    ("physical", "settled"),
    [
        ("toggle=True", "........"),
        ("timed='50ms'", "........"),
        ("timed='50ms', maintained=True", "########"),
        ("on_delay='50ms'", "########"),
    ],
    ids=["toggle", "timed", "timed-maintained", "on-delay"],
)
def test_a_harness_install_after_remove_settles(tmp_path: Path, physical: str, settled: str):
    """An enable turned on while the harness was off is held, not a rise."""
    horn = f"Bool('Horn', physical=Physical('H', {physical}), link='Motor')"
    adapter, out, _script_path = _setup(tmp_path, _coil_script(horn))
    _repl(adapter, out, "step 5", seq=10)
    resp, _ = _repl(adapter, out, "harness remove", seq=11)
    assert resp["success"] is True
    adapter._runner.patch({"Motor": True})
    _repl(adapter, out, "step 100", seq=12)
    resp, _ = _repl(adapter, out, "harness install", seq=13)
    assert resp["success"] is True
    assert _horn_bits(adapter, "Horn", 8) == settled


@pytest.mark.parametrize(
    ("link", "settled"),
    [("link Spare Motor toggle", "########"), ("link Spare Motor timed=500ms", "........")],
    ids=["toggle", "timed"],
)
def test_a_harness_install_after_remove_settles_a_bench_link(
    tmp_path: Path, link: str, settled: str
):
    """The bench links the install puts back settle too, so a held Motor fires none of them."""
    adapter, out, _script_path = _setup(tmp_path, _coil_script("Bool('Horn')"))
    resp, _ = _repl(adapter, out, link, seq=10)
    assert resp["success"] is True
    adapter._runner.patch({"Motor": True})
    _repl(adapter, out, "step 80", seq=11)
    _repl(adapter, out, "harness remove", seq=12)
    _repl(adapter, out, "step 3", seq=13)
    resp, _ = _repl(adapter, out, "harness install", seq=14)
    assert resp["success"] is True
    assert _horn_bits(adapter, "Spare", 8) == settled


@pytest.mark.parametrize("link", ["link Horn Motor toggle", "link Horn Motor timed=30ms"])
@pytest.mark.parametrize("how", ["harness remove and install", "reload"])
def test_a_bench_link_over_a_declared_coupling_settles_as_the_bench_link(
    tmp_path: Path, link: str, how: str
):
    """The install settles the coil that runs, not the declared on-delay the link replaces.

    Motor goes on, off and on, so the link leaves Horn off with Motor on.  Seeded for the
    on-delay, Horn would turn on at install and stay on under the new coil.
    """
    horn = "Bool('Horn', physical=Physical('H', on_delay='20ms'), link='Motor')"
    adapter, out, _script_path = _setup(tmp_path, _coil_script(horn))
    resp, _ = _repl(adapter, out, link, seq=10)
    assert resp["success"] is True
    for seq, motor in enumerate((True, False, True), start=11):
        adapter._runner.patch({"Motor": motor})
        _repl(adapter, out, "step 5", seq=seq)
    assert adapter._runner.current_state.tags["Horn"] is False
    if how == "reload":
        resp, _ = _repl(adapter, out, "reload", seq=20)
    else:
        _repl(adapter, out, "harness remove", seq=20)
        resp, _ = _repl(adapter, out, "harness install", seq=21)
    assert resp["success"] is True
    assert adapter._runner.current_state.tags["Horn"] is False
    assert _horn_bits(adapter, "Horn", 8) == "........"


def _relink_script(enable: str) -> str:
    return (
        "from pyrung.core import Bool, PLC, Physical, Program, Rung, out\n"
        "\n"
        "motor = Bool('Motor')\n"
        "pump = Bool('Pump', default=True)\n"
        f"fb = Bool('Fb', physical=Physical('X', toggle=True), link='{enable}')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(motor, pump):\n"
        "        out(Bool('Echo'))\n"
        "    with Rung(fb):\n"
        "        out(Bool('Seen'))\n"
        "\n"
        "runner = PLC(prog, dt=0.01)\n"
    )


def test_a_reload_that_links_a_coil_to_another_enable_settles_it(tmp_path: Path):
    """Fb's coil saw Motor off.  Pump is on, so carrying that memory on would flip Fb."""
    adapter, out, script_path = _setup(tmp_path, _relink_script("Motor"))
    _repl(adapter, out, "step 5", seq=10)
    script_path.write_text(_relink_script("Pump"), encoding="utf-8")
    resp, _ = _repl(adapter, out, "reload", seq=11)
    assert resp["success"] is True
    assert _horn_bits(adapter, "Fb", 6) == "......"


def _feedback_script(fb: str, physical: str) -> str:
    return (
        "from pyrung.core import Bool, PLC, Physical, Program, Rung, out\n"
        "\n"
        "motor = Bool('Motor')\n"
        f"fb = Bool('{fb}', physical=Physical('X', {physical}), link='Motor')\n"
        "\n"
        "with Program(strict=False) as prog:\n"
        "    with Rung(motor):\n"
        "        out(Bool('Echo'))\n"
        "    with Rung(fb):\n"
        "        out(Bool('Seen'))\n"
        "\n"
        "runner = PLC(prog, dt=0.01)\n"
    )


@pytest.mark.parametrize(
    ("middle", "last", "settled"),
    [
        (("Fb2", "toggle=True"), ("Fb", "toggle=True"), "........"),
        (("Fb", "toggle=True"), ("Fb", "timed='50ms'"), "........"),
    ],
    ids=["rename-and-back", "kind-change-and-back"],
)
def test_a_reload_back_to_a_coil_settles_it(
    tmp_path: Path, middle: tuple[str, str], last: tuple[str, str], settled: str
):
    """A coil whose memory went stale on the way does not read it after a reload back.

    The first program runs with Motor off, so its coil remembers its rung as off.
    Motor turns on under the program between.  Read back, the stale memory would turn
    that held Motor into a rise.
    """
    first = ("Fb", last[1])
    adapter, out, script_path = _setup(tmp_path, _feedback_script(*first))
    _repl(adapter, out, "step 3", seq=10)
    script_path.write_text(_feedback_script(*middle), encoding="utf-8")
    resp, _ = _repl(adapter, out, "reload", seq=11)
    assert resp["success"] is True
    assert "harness coil memory value(s) had no matching coupling" in resp["body"]["result"]
    adapter._runner.patch({"Motor": True})
    _repl(adapter, out, "step 3", seq=12)
    script_path.write_text(_feedback_script(*last), encoding="utf-8")
    resp, _ = _repl(adapter, out, "reload", seq=13)
    assert resp["success"] is True
    assert _horn_bits(adapter, "Fb", 8) == settled


AMBIENT_SCRIPT = (
    "from pyrung.core import Bool, PLC, Physical, Program, Real, Rung, out\n"
    "\n"
    "en = Bool('En')\n"
    "temp = Real(\n"
    "    'Temp',\n"
    "    physical=Physical('T', approach_to='Ambient', approach_rate=0.3),\n"
    "    link='En',\n"
    ")\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(en):\n"
    "        out(Bool('Echo'))\n"
    "    with Rung(temp > 1000.0):\n"
    "        out(Bool('Seen'))\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


class TestASetpointNoRungNames:
    @pytest.mark.filterwarnings("ignore:.*no rung names")
    def test_launch_and_reload_keep_the_plant(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path, AMBIENT_SCRIPT)
        assert adapter._harness is not None
        assert adapter._runner.current_state.tags["Ambient"] == 0.0

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]
        assert adapter._harness is not None
        assert adapter._runner._harness is adapter._harness
        assert adapter._harness.coupling_summary()["profile_couplings"]


class TestReload:
    def test_reload_basic(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "step 5", seq=10)
        old_scan_id = adapter._runner.current_state.scan_id

        resp, stopped = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]
        assert adapter._runner.current_state.scan_id == old_scan_id
        assert len(stopped) >= 1

    def test_reload_preserves_tag_values(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        assert adapter._runner.current_state.tags["Light"] is True

        _repl(adapter, out, "reload", seq=12)
        assert adapter._runner.current_state.tags["Light"] is True

    def test_reload_preserves_forces(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "reload", seq=11)
        assert adapter._runner.forces["Button"] is True

    def test_reload_scan_continuity(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "step 5", seq=10)
        scan_before = adapter._runner.current_state.scan_id

        _repl(adapter, out, "reload", seq=11)
        _repl(adapter, out, "step 1", seq=12)
        assert adapter._runner.current_state.scan_id == scan_before + 1

    def test_reload_emits_stopped_event(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, stopped = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is True
        assert len(stopped) >= 1
        assert stopped[0]["body"]["reason"] == "entry"

    def test_reload_with_new_rung(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        old_scan_id = adapter._runner.current_state.scan_id

        new_script = (
            "from pyrung.core import Bool, Int, PLC, Program, Rung, out, copy\n"
            "\n"
            "button = Bool('Button')\n"
            "light = Bool('Light')\n"
            "counter = Int('Counter')\n"
            "extra = Bool('Extra')\n"
            "\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung(button):\n"
            "        out(light)\n"
            "    with Rung():\n"
            "        copy(0, counter)\n"
            "    with Rung(button):\n"
            "        out(extra)\n"
            "\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        script_path.write_text(new_script, encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert adapter._runner.current_state.scan_id == old_scan_id
        assert adapter._runner.current_state.tags["Light"] is True
        assert adapter._runner.forces["Button"] is True
        assert "Extra" in adapter._runner._known_tags_by_name

    def test_a_reload_reads_the_edited_rung_lines(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        assert [r.end_line for r in adapter._runner.program.rungs] == [9, 11]

        # The edit keeps the first rung on its first line and grows both bodies.
        script_path.write_text(
            BASIC_SCRIPT.replace("out(light)\n", "out(light)\n        out(light)\n").replace(
                "copy(0, counter)\n", "copy(0, counter)\n        copy(1, counter)\n"
            ),
            encoding="utf-8",
        )

        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert [r.end_line for r in adapter._runner.program.rungs] == [10, 13]


ONESHOT_SCRIPT = (
    "from pyrung.core import Bool, PLC, Program, Rung, out\n"
    "\n"
    "button = Bool('Button')\n"
    "pulse = Bool('Pulse')\n"
    "extra = Bool('Extra')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "{extra}"
    "    with Rung(button):\n"
    "        out(pulse, oneshot=True)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)
EXTRA_RUNG = "    with Rung(button):\n        out(extra, oneshot=True)\n"
EDGE_SCRIPT = (
    ONESHOT_SCRIPT.replace("import Bool,", "import Bool, rise,")
    .replace("with Rung(button):", "with Rung(rise(button)):")
    .replace("out(pulse, oneshot=True)", "out(pulse)")
)
EDGE_EXTRA_RUNG = "    with Rung(rise(button)):\n        out(extra)\n"


class TestReloadSiteMemory:
    def test_oneshot_memory_follows_its_site(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, ONESHOT_SCRIPT.format(extra=""))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 2", seq=11)
        assert adapter._runner.current_state.memory["_oneshot:i1"] is True
        assert adapter._runner.current_state.tags["Pulse"] is False

        script_path.write_text(ONESHOT_SCRIPT.format(extra=EXTRA_RUNG), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        memory = adapter._runner.current_state.memory
        # Pulse's one-shot moved from i1 to i2; the new rung's is unwritten.
        assert memory["_oneshot:i2"] is True
        assert "_oneshot:i1" not in memory

        _repl(adapter, out, "step 1", seq=13)
        tags = adapter._runner.current_state.tags
        assert tags["Pulse"] is False  # already fired before the reload
        assert tags["Extra"] is True

    def test_unmatched_site_memory_is_dropped(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, ONESHOT_SCRIPT.format(extra=""))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        script_path.write_text(
            ONESHOT_SCRIPT.format(extra="").replace("out(pulse", "out(extra"), encoding="utf-8"
        )
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "no matching site" in resp["body"]["result"]
        assert "_oneshot:i1" not in adapter._runner.current_state.memory

    def test_an_inserted_edge_contact_does_not_rise_on_a_held_bit(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, EDGE_SCRIPT.format(extra=""))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 2", seq=11)
        assert adapter._runner.current_state.memory["_edge:e1"] is True
        assert adapter._runner.current_state.tags["Pulse"] is False

        # The new contact lands on the old one's line, above it.
        script_path.write_text(
            EDGE_SCRIPT.format(extra=EDGE_EXTRA_RUNG),
            encoding="utf-8",
        )
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "no matching site" not in result
        assert "1 edge contact(s) new or edited start from their tag's current value" in result
        assert "rise(Button) line 8 = on" in result
        memory = adapter._runner.current_state.memory
        assert memory["_edge:e1"] is True
        assert memory["_edge:e2"] is True

        _repl(adapter, out, "step 1", seq=13)
        tags = adapter._runner.current_state.tags
        assert tags["Pulse"] is False
        assert tags["Extra"] is False

    def test_reloading_an_unchanged_script_seeds_no_edge_contact(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path, EDGE_SCRIPT.format(extra=""))
        _repl(adapter, out, "step 1", seq=10)
        _repl(adapter, out, "force Button true", seq=11)
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "edge contact(s)" not in resp["body"]["result"]
        assert "_edge:e1" not in adapter._runner.current_state.memory

        _repl(adapter, out, "step 1", seq=13)
        assert adapter._runner.current_state.tags["Pulse"] is True


OUT_COIL_SCRIPT = (
    "from pyrung.core import Bool, PLC, Program, Rung, Timer, debounce, timed\n"
    "\n"
    "button = Bool('Button')\n"
    "horn = Bool('Horn')\n"
    "tank = Bool('Tank')\n"
    "t = Timer.clone('Ot', count=4)\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        timed(horn, t[1], {on})\n"
    "    with Rung(button):\n"
    "        debounce(tank, on=(t[2], {on}), off=(t[3], 20))\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


class TestReloadRungMemory:
    def test_an_edited_preset_does_not_restart_a_held_rung(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, OUT_COIL_SCRIPT.format(on=30))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 5", seq=11)
        tags = adapter._runner.current_state.tags
        assert tags["Horn"] is False  # its pulse is spent
        assert tags["Tank"] is True
        assert tags["Ot2_Acc"] == 50

        script_path.write_text(OUT_COIL_SCRIPT.format(on=40), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "no matching site" not in resp["body"]["result"]
        memory = adapter._runner.current_state.memory
        assert memory["_timed_rung:i1"] is True
        assert memory["_debounce_rung:i2"] is True

        _repl(adapter, out, "step 2", seq=13)
        tags = adapter._runner.current_state.tags
        # The held rung is no new edge: no second pulse, and the on side runs on.
        assert tags["Horn"] is False
        assert tags["Ot1_Acc"] == 30
        assert tags["Tank"] is True
        assert tags["Ot2_Acc"] == 70

    def test_an_edited_target_restarts_the_coil_and_says_so(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, OUT_COIL_SCRIPT.format(on=30))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 5", seq=11)
        script_path.write_text(
            OUT_COIL_SCRIPT.format(on=30).replace("timed(horn,", "timed(Bool('Siren'),"),
            encoding="utf-8",
        )
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "1 site memory value(s) had no matching site" in result
        assert "reset: _timed_rung:i1" in result

        _repl(adapter, out, "step 1", seq=13)
        # Another coil: the held rung is its first edge.
        assert adapter._runner.current_state.tags["Siren"] is True


COUNTER_SCRIPT = (
    "from pyrung.core import Bool, Counter, PLC, Program, Rung, count_up\n"
    "\n"
    "part = Bool('Part')\n"
    "ctr = Counter.clone('Parts')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(part):\n"
    "        count_up(ctr, preset={preset}).down({down}).reset({reset})\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)

FLASHER_SCRIPT = (
    "from pyrung.core import Bool, PLC, Program, Rung, Timer, flasher\n"
    "\n"
    "button = Bool('Button')\n"
    "beacon = Bool('Beacon')\n"
    "t = Timer.clone('Fl', count=2)\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        flasher(beacon, on=(t[1], {on}), off=(t[2], 50))\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)

ONESHOT_COPY_SCRIPT = (
    "from pyrung.core import Bool, Int, PLC, Program, Rung, copy\n"
    "\n"
    "button = Bool('Button')\n"
    "setpoint = Int('Setpoint')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        copy({value}, setpoint, oneshot=True)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)

TOGGLE_SCRIPT = (
    "from pyrung.core import Bool, PLC, Program, Rung, toggle\n"
    "\n"
    "button = Bool('Button')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        toggle(Bool('{target}'))\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)

DRUM_SCRIPT = (
    "from pyrung.core import Bool, Dint, Int, PLC, Program, Rung, time_drum\n"
    "\n"
    "running = Bool('Running')\n"
    "jump = Bool('Jump')\n"
    "short = Int('Short', default=100)\n"
    "long = Int('Long', default=300)\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(running):\n"
    "        time_drum(\n"
    "            outputs=[Bool('Fill'), Bool('Drain')],\n"
    "            presets=[{first}, {second}],\n"
    "            unit='ms',\n"
    "            pattern=[[1, 0], [0, 1]],\n"
    "            current_step=Int('Step'),\n"
    "            accumulator=Dint('DrumAcc'),\n"
    "            completion_flag=Bool('DrumDone'),\n"
    "        ).reset(Bool('DrumReset')).jump(jump, step=1)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


class TestReloadInputMemory:
    """An edit to what an instruction reads keeps its inputs as it last saw them."""

    def test_an_edited_counter_preset_does_not_count_a_held_input_again(self, tmp_path: Path):
        script = COUNTER_SCRIPT.format(preset=10, down="Bool('Dn')", reset="Bool('Clear')")
        adapter, out, script_path = _setup(tmp_path, script)
        _repl(adapter, out, "force Part true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        assert adapter._runner.current_state.tags["Parts_Acc"] == 1

        script_path.write_text(script.replace("preset=10", "preset=20"), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "no matching site" not in resp["body"]["result"]
        assert adapter._runner.current_state.memory["_cu:i1"] is True

        _repl(adapter, out, "step 2", seq=13)
        assert adapter._runner.current_state.tags["Parts_Acc"] == 1
        _repl(adapter, out, "force Part false", seq=14)
        _repl(adapter, out, "step 1", seq=15)
        _repl(adapter, out, "force Part true", seq=16)
        _repl(adapter, out, "step 1", seq=17)
        assert adapter._runner.current_state.tags["Parts_Acc"] == 2

    def test_a_preset_and_reset_leg_edit_does_not_count_a_held_input_again(self, tmp_path: Path):
        script = COUNTER_SCRIPT.format(preset=10, down="Bool('Dn')", reset="Bool('Clear')")
        adapter, out, script_path = _setup(tmp_path, script)
        _repl(adapter, out, "force Part true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        assert adapter._runner.current_state.tags["Parts_Acc"] == 1
        script_path.write_text(
            COUNTER_SCRIPT.format(preset=20, down="Bool('Dn')", reset="Bool('Clear2')"),
            encoding="utf-8",
        )
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        # The count input is the rung, which the reset leg does not touch.
        assert "no matching site" not in resp["body"]["result"]

        _repl(adapter, out, "step 1", seq=13)
        assert adapter._runner.current_state.tags["Parts_Acc"] == 1

    def _counted_down_once(self, tmp_path: Path) -> tuple[Any, ...]:
        script = COUNTER_SCRIPT.format(preset=10, down="Bool('Dn')", reset="Bool('Clear')")
        adapter, out, script_path = _setup(tmp_path, script)
        _repl(adapter, out, "force Dn true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        assert adapter._runner.current_state.tags["Parts_Acc"] == -1
        return adapter, out, script_path

    def test_a_preset_and_down_leg_edit_resets_the_down_legs_memory(self, tmp_path: Path):
        adapter, out, script_path = self._counted_down_once(tmp_path)
        script_path.write_text(
            COUNTER_SCRIPT.format(preset=20, down="Bool('Dn2')", reset="Bool('Clear')"),
            encoding="utf-8",
        )
        resp, _ = _repl(adapter, out, "reload", seq=13)
        assert resp["success"] is True
        result = resp["body"]["result"]
        assert "1 site memory value(s) had no matching site" in result
        assert "reset: _cd:i1" in result

        # The new leg, held, is its first edge.
        _repl(adapter, out, "force Dn2 true", seq=14)
        _repl(adapter, out, "step 1", seq=15)
        assert adapter._runner.current_state.tags["Parts_Acc"] == -2

    def test_a_leg_only_edit_keeps_the_edited_legs_memory(self, tmp_path: Path):
        adapter, out, script_path = self._counted_down_once(tmp_path)
        script_path.write_text(
            COUNTER_SCRIPT.format(preset=10, down="Bool('Dn2')", reset="Bool('Clear')"),
            encoding="utf-8",
        )
        resp, _ = _repl(adapter, out, "reload", seq=13)
        assert resp["success"] is True
        # A helper leg is no part of the counter's identity: the site matches as it
        # is, and the new leg's first scan compares with the old leg's last value.
        assert "site memory" not in resp["body"]["result"]
        assert adapter._runner.current_state.memory["_cd:i1"] is True

        _repl(adapter, out, "force Dn2 true", seq=14)
        _repl(adapter, out, "step 1", seq=15)
        assert adapter._runner.current_state.tags["Parts_Acc"] == -1

    def test_an_edited_flasher_preset_runs_on_in_its_phase(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, FLASHER_SCRIPT.format(on=50))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        tags = adapter._runner.current_state.tags
        assert tags["Beacon"] is True
        on_acc = tags["Fl1_Acc"]
        assert on_acc > 0

        script_path.write_text(FLASHER_SCRIPT.format(on=80), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "no matching site" not in resp["body"]["result"]

        _repl(adapter, out, "step 1", seq=13)
        tags = adapter._runner.current_state.tags
        # No restart from 0: the on phase runs on to the new preset.
        assert tags["Fl1_Acc"] == on_acc + 10
        assert tags["Beacon"] is True

    def test_an_edited_oneshot_source_does_not_fire_again(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, ONESHOT_COPY_SCRIPT.format(value=5))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 2", seq=11)
        assert adapter._runner.current_state.tags["Setpoint"] == 5

        script_path.write_text(ONESHOT_COPY_SCRIPT.format(value=7), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "no matching site" not in resp["body"]["result"]

        _repl(adapter, out, "step 1", seq=13)
        assert adapter._runner.current_state.tags["Setpoint"] == 5
        _repl(adapter, out, "force Button false", seq=14)
        _repl(adapter, out, "step 1", seq=15)
        _repl(adapter, out, "force Button true", seq=16)
        _repl(adapter, out, "step 1", seq=17)
        assert adapter._runner.current_state.tags["Setpoint"] == 7

    def test_a_retargeted_toggle_resets_and_says_so(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path, TOGGLE_SCRIPT.format(target="Lamp"))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 2", seq=11)
        assert adapter._runner.current_state.tags["Lamp"] is True

        script_path.write_text(TOGGLE_SCRIPT.format(target="Lamp2"), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "reset: _toggle:i1" in resp["body"]["result"]

        _repl(adapter, out, "step 1", seq=13)
        tags = adapter._runner.current_state.tags
        # Another coil: the held rung is its first edge.  The old target keeps its value.
        assert tags["Lamp2"] is True
        assert tags["Lamp"] is True

    def _drum_mid_step_one(self, tmp_path: Path) -> tuple[Any, ...]:
        adapter, out, script_path = _setup(tmp_path, DRUM_SCRIPT.format(first="short", second=100))
        _repl(adapter, out, "force Running true", seq=10)
        _repl(adapter, out, "force Jump true", seq=11)
        _repl(adapter, out, "step 6", seq=12)
        tags = adapter._runner.current_state.tags
        assert tags["Step"] == 1
        acc = tags["DrumAcc"]
        assert 0 < acc < 100
        return adapter, out, script_path, acc

    def test_an_edited_drum_preset_runs_its_step_on(self, tmp_path: Path):
        adapter, out, script_path, acc = self._drum_mid_step_one(tmp_path)

        # The step's preset moves from one register to another.
        script_path.write_text(DRUM_SCRIPT.format(first="long", second=100), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=13)
        assert resp["success"] is True
        # The rung and the held jump are not new edges.
        assert "Warnings" not in resp["body"]["result"]

        _repl(adapter, out, "step 1", seq=14)
        tags = adapter._runner.current_state.tags
        assert tags["Step"] == 1
        assert tags["DrumAcc"] == acc + 10
        # It ends at the 300 the new register holds.
        scans = (300 - (acc + 10)) // 10
        _repl(adapter, out, f"step {scans - 1}", seq=15)
        tags = adapter._runner.current_state.tags
        assert tags["Step"] == 1
        assert tags["Fill"] is True
        _repl(adapter, out, "step 1", seq=16)
        assert adapter._runner.current_state.tags["Step"] == 2

    def test_an_edit_to_another_steps_preset_runs_this_step_on(self, tmp_path: Path):
        adapter, out, script_path, acc = self._drum_mid_step_one(tmp_path)
        script_path.write_text(DRUM_SCRIPT.format(first="short", second=150), encoding="utf-8")
        resp, _ = _repl(adapter, out, "reload", seq=13)
        assert resp["success"] is True
        assert "Warnings" not in resp["body"]["result"]

        _repl(adapter, out, "step 1", seq=14)
        tags = adapter._runner.current_state.tags
        assert tags["Step"] == 1
        assert tags["DrumAcc"] == acc + 10
        # It ends at the 100 its register holds.
        scans = (100 - (acc + 10)) // 10
        _repl(adapter, out, f"step {scans - 1}", seq=15)
        assert adapter._runner.current_state.tags["Step"] == 1
        _repl(adapter, out, "step 1", seq=16)
        assert adapter._runner.current_state.tags["Step"] == 2


class TestReloadTagTypeChange:
    def test_type_change_drops_value(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        assert adapter._runner.current_state.tags["Light"] is True

        changed_script = (
            "from pyrung.core import Int, PLC, Program, Rung, copy\n"
            "\n"
            "light = Int('Light')\n"
            "counter = Int('Counter')\n"
            "\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung():\n"
            "        copy(0, light)\n"
            "    with Rung():\n"
            "        copy(0, counter)\n"
            "\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        script_path.write_text(changed_script, encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=12)
        assert resp["success"] is True
        assert "type changed" in resp["body"]["result"]
        assert adapter._runner.current_state.tags["Light"] == 0


class TestReloadErrors:
    def test_syntax_error(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        old_runner = adapter._runner
        script_path.write_text("def broken(\n", encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is False
        assert adapter._runner is old_runner

    def test_no_runner(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        old_runner = adapter._runner
        script_path.write_text("x = 42\n", encoding="utf-8")

        resp, _ = _repl(adapter, out, "reload", seq=10)
        assert resp["success"] is False
        assert adapter._runner is old_runner


class TestReloadBlockedByRecording:
    def test_reload_blocked_during_recording(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        resp, _ = _repl(adapter, out, "reload", seq=11)
        assert resp["success"] is False
        assert "recording" in resp.get("message", resp.get("body", {}).get("result", "")).lower()

    def test_reload_allowed_after_recording_stops(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "record test_action", seq=10)
        _repl(adapter, out, "patch Button true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "patch Button false", seq=13)
        _repl(adapter, out, "step 3", seq=14)
        _repl(adapter, out, "patch Button true", seq=15)
        _repl(adapter, out, "step 3", seq=16)
        _repl(adapter, out, "patch Button false", seq=17)
        _repl(adapter, out, "step 3", seq=18)
        _repl(adapter, out, "record stop", seq=19)
        resp, _ = _repl(adapter, out, "reload", seq=20)
        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]


class TestAutoReload:
    def test_autoreload_starts_thread(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "autoreload", seq=10)
        assert resp["success"] is True
        assert "Auto-reload enabled" in resp["body"]["result"]
        assert getattr(adapter, "_autoreload_thread", None) is not None
        assert adapter._autoreload_thread.is_alive()

        resp, _ = _repl(adapter, out, "autoreload off", seq=11)
        assert resp["success"] is True
        assert "disabled" in resp["body"]["result"]
        assert getattr(adapter, "_autoreload_thread", None) is None

    def test_autoreload_already_running(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        _repl(adapter, out, "autoreload", seq=10)
        resp, _ = _repl(adapter, out, "autoreload", seq=11)
        assert "Already" in resp["body"]["result"]

        _repl(adapter, out, "autoreload off", seq=12)

    def test_autoreload_off_when_not_running(self, tmp_path: Path):
        adapter, out, script_path = _setup(tmp_path)
        resp, _ = _repl(adapter, out, "autoreload off", seq=10)
        assert "disabled" in resp["body"]["result"]


class TestMemoryPaneLabels:
    def test_site_memory_names_its_contact_and_sorts_by_number(self, tmp_path: Path):
        rungs = "".join(
            f"    with Rung(rise(button)):\n        out(Bool('Out{n}'))\n" for n in range(11)
        )
        adapter, out, _script_path = _setup(tmp_path, EDGE_SCRIPT.format(extra=rungs))
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 1", seq=11)
        messages = _send_request(
            adapter,
            out,
            seq=12,
            command="variables",
            arguments={"variablesReference": adapter.MEMORY_SCOPE_REF},
        )
        variables = _single_response(messages)["body"]["variables"]
        edges = [item for item in variables if item["name"].startswith("_edge:")]
        assert [item["name"] for item in edges] == [f"_edge:e{n}" for n in range(1, 13)]
        assert edges[0]["value"] == "True (rise(Button) line 8)"
        assert edges[11]["value"] == "True (rise(Button) line 30)"


RETENTIVE_SCRIPT = (
    "from pyrung.core import Bool, Int, PLC, Program, Rung, Timer, Counter, on_delay, count_down\n"
    "\n"
    "button = Bool('Button')\n"
    "rst = Bool('Rst')\n"
    "td = Timer.clone('TD1')\n"
    "ctd = Counter.clone('CTD1')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(button):\n"
    "        on_delay(td, preset=1000)\n"
    "    with Rung(button):\n"
    "        count_down(ctd, preset=100).reset(rst)\n"
    "\n"
    "runner = PLC(prog, dt=0.010)\n"
)


class TestReloadKeepsTheMode:
    def test_a_reload_in_stop_stays_in_stop(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path)
        _repl(adapter, out, "mode stop", seq=10)
        assert adapter._runner.mode == "stop"

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert resp["success"] is True
        assert adapter._runner.mode == "stop"

    def test_a_reload_in_run_stays_in_run(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path)
        _repl(adapter, out, "step 3", seq=10)
        assert adapter._runner.mode == "run"

        _repl(adapter, out, "reload", seq=11)

        assert adapter._runner.mode == "run"

    def test_autoreload_shares_the_reload_path(self, tmp_path: Path):
        from pyrung.dap import reload_console

        adapter, out, _script_path = _setup(tmp_path)
        _repl(adapter, out, "mode stop", seq=10)

        reload_console._reload_locked(adapter)

        assert adapter._runner.mode == "stop"

    def test_stop_reload_run_resets_the_timer_and_keeps_the_rest(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path, RETENTIVE_SCRIPT, keepForcesOnRun=True)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 5", seq=11)
        _repl(adapter, out, "mode stop", seq=12)
        assert adapter._runner.current_state.tags["TD1_Acc"] > 0
        ctd_acc = adapter._runner.current_state.tags["CTD1_Acc"]
        assert ctd_acc != 0

        _repl(adapter, out, "reload", seq=13)
        assert adapter._runner.mode == "stop"
        assert adapter._runner.forces == {"Button": True}
        _repl(adapter, out, "mode run", seq=14)

        assert adapter._runner.mode == "run"
        assert adapter._runner.current_state.tags["TD1_Acc"] == 0
        assert adapter._runner.current_state.tags["CTD1_Acc"] == ctd_acc
        assert adapter._runner.forces == {"Button": True}

    def test_a_reload_in_run_keeps_the_timer_and_forces(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path, RETENTIVE_SCRIPT, keepForcesOnRun=True)
        _repl(adapter, out, "force Button true", seq=10)
        _repl(adapter, out, "step 5", seq=11)
        acc = adapter._runner.current_state.tags["TD1_Acc"]
        ctd_acc = adapter._runner.current_state.tags["CTD1_Acc"]
        assert acc > 0

        assert adapter._runner.forces == {"Button": True}
        _repl(adapter, out, "reload", seq=12)

        assert adapter._runner.current_state.tags["TD1_Acc"] == acc
        assert adapter._runner.current_state.tags["CTD1_Acc"] == ctd_acc
        assert adapter._runner.forces == {"Button": True}


class TestReloadWhileContinueRuns:
    def test_a_reload_right_after_pause_waits_for_the_pause(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path)
        _send_request(adapter, out, seq=10, command="continue")
        _send_request(adapter, out, seq=11, command="pause")

        resp, _stopped = _repl(adapter, out, "reload", seq=12)

        assert resp["success"] is True
        assert "Reloaded" in resp["body"]["result"]

    def test_a_reload_while_running_pauses_first(self, tmp_path: Path):
        adapter, out, _script_path = _setup(tmp_path)
        _send_request(adapter, out, seq=10, command="continue")

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert resp["success"] is True
        assert not adapter._thread_running_locked()


CARRY_SCRIPT = (
    "from pyrung.core import Bool, Int, Physical, PLC, Program, Rung, out\n"
    "from pyrung.click import ClickBlocks, TagMap\n"
    "x, y, c, t, ct, sc, ds, *_ = ClickBlocks()\n"
    "\n"
    "a = {a_decl}\n"
    "cmd = Bool('Cmd')\n"
    "lamp = Bool('Lamp')\n"
    "\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(cmd):\n"
    "        out(lamp)\n"
    "    with Rung(a):\n"
    "        out(Bool('Sink'))\n"
    "\n"
    "mapping = TagMap({{a: {a_addr}, cmd: {cmd_addr}, lamp: y[1]}})\n"
    "runner = PLC(prog, dt=0.010)\n"
)


def _carry_script(**kwargs: str) -> str:
    values = {"a": "A", "a_addr": "c[1]", "cmd_addr": "x[1]", "a_type": "Bool", **kwargs}
    values["a_decl"] = values.pop("a_decl", f"{values['a_type']}('{values['a']}')")
    return CARRY_SCRIPT.format(**values)


def _carry(tmp_path: Path, *, a: str = "A", a_addr: str = "c[1]") -> tuple[Any, Any, Path]:
    return _setup(tmp_path, _carry_script(a=a, a_addr=a_addr))


def _rewrite(script_path: Path, **kwargs: str) -> None:
    script_path.write_text(_carry_script(**kwargs))


def _int_a_script() -> str:
    """A is an Int now, so a force or a bench link on it no longer fits."""
    return _carry_script(a_type="Int", a_addr="ds[1]").replace("Rung(a)", "Rung(a > 0)")


def _declared_a_script() -> str:
    decl = "Bool('A', physical=Physical('A', on_delay='0ms'), link='Cmd')"
    return _carry_script(a_decl=decl)


class TestReloadReportsWhatMoved:
    def test_a_force_whose_tag_moved_is_reported_with_both_addresses(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _rewrite(script, a_addr="c[5]")

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert "The force on A moved from C1 to C5." in resp["body"]["result"]
        assert resp["body"]["carried"] == [
            {"tag": "A", "kind": "force", "old": "C1", "new": "C5", "dropped": False, "value": True}
        ]
        assert adapter._runner.forces == {"A": True}

    def test_a_force_dropped_by_a_rename_is_unforced_and_reported(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _rewrite(script, a="B")

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert (
            "Dropped the force on A because A is not in the new program."
            in (resp["body"]["result"])
        )
        assert resp["body"]["carried"] == [
            {
                "tag": "A",
                "kind": "force",
                "old": "C1",
                "new": None,
                "dropped": True,
                "value": True,
                "reason": "gone",
            }
        ]
        assert adapter._runner.forces == {}

    def test_a_dropped_force_is_reported_with_no_tag_map(self, tmp_path: Path):
        adapter, out, script = _setup(tmp_path)
        _repl(adapter, out, "force Button true", seq=10)
        script.write_text(BASIC_SCRIPT.replace("Button", "Press"))

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert (
            "Dropped the force on Button because Button is not in the new program."
            in (resp["body"]["result"])
        )
        assert resp["body"]["carried"] == [
            {
                "tag": "Button",
                "kind": "force",
                "old": None,
                "new": None,
                "dropped": True,
                "value": True,
                "reason": "gone",
            }
        ]
        assert adapter._runner.forces == {}

    def test_a_force_on_a_system_tag_stays_and_is_not_reported(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "force rtc.new_hour true", seq=10)

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert resp["body"]["carried"] == []
        assert adapter._runner.forces == {"rtc.new_hour": True}

    def test_a_force_on_a_tag_that_changed_type_is_dropped_with_that_reason(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        script.write_text(_int_a_script())

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        entry = resp["body"]["carried"][0]
        assert (entry["tag"], entry["dropped"], entry["reason"]) == ("A", True, "type changed")
        assert adapter._runner.forces == {}

    def test_a_bench_link_whose_feedback_moved_is_reported(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        assert "A" in adapter._bench_links
        _rewrite(script, a_addr="c[5]")

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert "The bench link on A moved from C1 to C5." in resp["body"]["result"]
        assert resp["body"]["carried"] == [
            {
                "tag": "A",
                "kind": "link",
                "feedback": "A",
                "old": "C1",
                "new": "C5",
                "dropped": False,
                "enabled": True,
            }
        ]
        assert "A" in adapter._bench_links

    def test_a_moved_enable_is_reported_under_the_links_feedback(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        _rewrite(script, cmd_addr="x[3]")

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        assert (
            "The bench link on A reads Cmd, which moved from X001 to X003."
            in (resp["body"]["result"])
        )
        entry = resp["body"]["carried"][0]
        assert (entry["tag"], entry["feedback"], entry["kind"]) == ("Cmd", "A", "link")

    def test_a_disabled_link_is_reported_with_its_flag(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        link = adapter._bench_links["A"]
        adapter._bench_links["A"] = type(link)(link.coupling, enabled=False)
        _rewrite(script, a_addr="c[5]")

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        entry = resp["body"]["carried"][0]
        assert (entry["kind"], entry["enabled"], entry["new"]) == ("link", False, "C5")

    def test_a_link_dropped_by_a_rename_is_reported_gone(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        _rewrite(script, a="B")

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        entry = resp["body"]["carried"][0]
        assert (entry["kind"], entry["feedback"], entry["dropped"], entry["reason"]) == (
            "link",
            "A",
            True,
            "gone",
        )
        assert entry["new"] is None
        assert "A" not in adapter._bench_links

    def test_a_link_whose_enable_left_is_reported_under_the_enable(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        script.write_text(_carry_script().replace("'Cmd'", "'Go'"))

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        entry = resp["body"]["carried"][0]
        assert (entry["tag"], entry["feedback"], entry["old"], entry["reason"]) == (
            "Cmd",
            "A",
            "X001",
            "gone",
        )
        assert "A" not in adapter._bench_links

    def test_a_link_that_no_longer_validates_is_reported_invalid(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        script.write_text(_int_a_script())

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        entry = resp["body"]["carried"][0]
        assert (entry["kind"], entry["dropped"], entry["reason"]) == ("link", True, "invalid")

    def test_a_link_the_program_now_declares_is_reported_in_program(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        script.write_text(_declared_a_script())

        resp, _stopped = _repl(adapter, out, "reload", seq=11)

        entry = resp["body"]["carried"][0]
        assert (entry["kind"], entry["dropped"], entry["reason"]) == ("link", True, "in program")

    def test_a_cut_is_reported_when_the_program_no_longer_links_it(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        script.write_text(_declared_a_script())
        _repl(adapter, out, "reload", seq=10)
        _repl(adapter, out, "unlink A", seq=11)
        assert adapter._bench_links["A"].coupling is None
        _rewrite(script)

        resp, _stopped = _repl(adapter, out, "reload", seq=12)

        entry = resp["body"]["carried"][0]
        assert (entry["kind"], entry["dropped"], entry["reason"]) == ("cut", True, "unlinked")

    def test_a_cut_whose_feedback_left_is_reported_gone(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        script.write_text(_declared_a_script())
        _repl(adapter, out, "reload", seq=10)
        _repl(adapter, out, "unlink A", seq=11)
        _rewrite(script, a="B")

        resp, _stopped = _repl(adapter, out, "reload", seq=12)

        entry = resp["body"]["carried"][0]
        assert (entry["kind"], entry["tag"], entry["reason"]) == ("cut", "A", "gone")

    def test_nothing_is_reported_when_nothing_moved(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=11)

        resp, _stopped = _repl(adapter, out, "reload", seq=12)

        assert resp["body"]["carried"] == []
        assert "moved" not in resp["body"]["result"]
        assert "Dropped" not in resp["body"]["result"]


class TestReloadWaitsForThePause:
    def test_a_reload_after_pause_waits_until_the_worker_lands(self, tmp_path: Path, monkeypatch):
        import threading

        adapter, out, _script_path = _setup(tmp_path)
        gate = threading.Event()
        monkeypatch.setattr(adapter, "_continue_worker", lambda: gate.wait(10))
        _send_request(adapter, out, seq=10, command="continue")
        _send_request(adapter, out, seq=11, command="pause")
        assert adapter._thread_running_locked()
        threading.Timer(0.2, gate.set).start()

        resp, _stopped = _repl(adapter, out, "reload", seq=12)

        assert resp["success"] is True
        assert gate.is_set()
        assert not adapter._thread_running_locked()

    def test_a_reload_refuses_when_the_pause_never_lands(self, tmp_path: Path, monkeypatch):
        import threading

        from pyrung.dap.handlers import simulation

        monkeypatch.setattr(simulation, "_PAUSE_JOIN_TIMEOUT", 0.05)
        adapter, out, _script_path = _setup(tmp_path)
        gate = threading.Event()
        monkeypatch.setattr(adapter, "_continue_worker", lambda: gate.wait(10))
        _send_request(adapter, out, seq=10, command="continue")
        old_runner = adapter._runner
        try:
            resp, _stopped = _repl(adapter, out, "reload", seq=11)
        finally:
            gate.set()

        assert resp["success"] is False
        assert "Timed out waiting for execution to pause" in resp["message"]
        assert adapter._runner is old_runner

    def test_a_failed_reload_in_stop_keeps_stop_forces_and_links(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=11)
        _repl(adapter, out, "mode stop", seq=12)
        script.write_text("this is not python (\n")

        resp, _stopped = _repl(adapter, out, "reload", seq=13)

        assert resp["success"] is False
        assert "Reload failed" in resp["message"]
        assert adapter._runner.mode == "stop"
        assert adapter._runner.forces == {"A": True}
        assert "A" in adapter._bench_links


class TestAutoreloadReport:
    def test_the_output_event_carries_the_report(self, tmp_path: Path, monkeypatch):
        from pyrung.dap import reload_console

        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        events: list[tuple[str, Any]] = []
        monkeypatch.setattr(
            adapter, "_enqueue_internal_event", lambda e, b=None: events.append((e, b))
        )
        monkeypatch.setattr(adapter, "_request_state_frame", lambda _info: None)

        class OneEdit:
            """Edits the program on the first wait, then stops the loop."""

            calls = 0

            def wait(self, timeout: float) -> bool:
                self.calls += 1
                if self.calls == 1:
                    _rewrite(script, a_addr="c[5]")
                    stat = script.stat()
                    os.utime(script, (stat.st_atime, stat.st_mtime + 5))
                return self.calls > 2

        reload_console._autoreload_loop(adapter, script.parent, OneEdit())  # type: ignore[arg-type]

        outputs = [body for name, body in events if name == "output"]
        assert outputs[0]["output"].startswith("[autoreload] Reloaded")
        assert outputs[0]["data"]["carried"] == [
            {"tag": "A", "kind": "force", "old": "C1", "new": "C5", "dropped": False, "value": True}
        ]


def _plan_reload(
    adapter: DAPAdapter,
    out: io.BytesIO,
    *,
    forces: Any = None,
    links: Any = None,
    declared: Any = None,
    seq: int = 20,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if forces is not None:
        arguments["forces"] = forces
    if links is not None:
        arguments["links"] = links
    if declared is not None:
        arguments["declared"] = declared
    messages = _send_request(adapter, out, seq=seq, command="pyrungReload", arguments=arguments)
    return _single_response(messages)


def _carried(resp: dict[str, Any], tag: str, kind: str) -> dict[str, Any]:
    (entry,) = [e for e in resp["body"]["carried"] if e["tag"] == tag and e["kind"] == kind]
    return entry


def _timed_count(adapter: DAPAdapter, fb: str) -> int:
    return adapter._runner.current_state.tags[f"__tmd_on__{fb}"]


class TestPlannedReload:
    def test_a_planned_force_moves_to_its_new_tag(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _rewrite(script, a="B")

        resp = _plan_reload(adapter, out, forces={"A": "B"})

        assert resp["success"] is True
        assert "Moved the force on A to B." in resp["body"]["result"]
        assert resp["body"]["carried"] == [
            {
                "tag": "A",
                "kind": "force",
                "to": "B",
                "old": "C1",
                "new": "C1",
                "planned": True,
                "released": False,
                "dropped": False,
                "value": True,
            }
        ]
        assert adapter._runner.forces == {"B": True}

    def test_a_force_planned_null_is_released(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)

        resp = _plan_reload(adapter, out, forces={"A": None})

        entry = _carried(resp, "A", "force")
        assert (entry["released"], entry["dropped"], entry["to"]) == (True, False, None)
        assert "Released the force on A." in resp["body"]["result"]
        assert adapter._runner.forces == {}

    def test_a_renamed_link_keeps_a_timed_coils_count(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd timed=100ms", seq=10)
        _repl(adapter, out, "force Cmd true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        count = _timed_count(adapter, "A")
        assert 0 < count < 100
        _rewrite(script, a="B")

        resp = _plan_reload(adapter, out, links={"A": {"en": "Cmd", "fb": "B"}})

        assert resp["success"] is True
        assert "Moved the bench link on A to B." in resp["body"]["result"]
        assert "reset" not in resp["body"]["result"]
        entry = _carried(resp, "A", "link")
        assert (entry["to"], entry["enable"], entry["planned"]) == ("B", "Cmd", True)
        assert list(adapter._bench_links) == ["B"]
        tags = adapter._runner.current_state.tags
        assert _timed_count(adapter, "B") == count
        assert tags["B"] is True
        assert "__tmd_on__A" not in tags
        _repl(adapter, out, "step 1", seq=13)
        assert _timed_count(adapter, "B") == count + 10
        assert adapter._runner.current_state.tags["B"] is True

    def test_a_link_whose_enable_is_renamed_keeps_its_count(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd timed=100ms", seq=10)
        _repl(adapter, out, "force Cmd true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        count = _timed_count(adapter, "A")
        script.write_text(_carry_script(a="B").replace("'Cmd'", "'Go'"))

        resp = _plan_reload(
            adapter, out, forces={"Cmd": "Go"}, links={"A": {"en": "Go", "fb": "B"}}
        )

        assert "The bench link on B reads Go (it read Cmd)." in resp["body"]["result"]
        assert adapter._bench_links["B"].coupling.en_name == "Go"
        assert adapter._runner.forces == {"Go": True}
        assert _timed_count(adapter, "B") == count
        _repl(adapter, out, "step 1", seq=13)
        assert _timed_count(adapter, "B") == count + 10

    def test_a_renamed_toggle_keeps_its_state(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd toggle", seq=10)
        _repl(adapter, out, "force Cmd true", seq=11)
        _repl(adapter, out, "step 2", seq=12)
        assert adapter._runner.current_state.tags["A"] is True
        _rewrite(script, a="B")

        _plan_reload(adapter, out, links={"A": {"en": "Cmd", "fb": "B"}})
        _repl(adapter, out, "step 2", seq=13)

        assert adapter._runner.current_state.tags["B"] is True

    def test_a_link_planned_null_is_removed(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)

        resp = _plan_reload(adapter, out, links={"A": None})

        entry = _carried(resp, "A", "link")
        assert (entry["released"], entry["dropped"], entry["to"]) == (True, False, None)
        assert "Removed the bench link on A." in resp["body"]["result"]
        assert adapter._bench_links == {}

    def test_a_cut_moves_to_the_feedback_its_plan_names(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        script.write_text(_declared_a_script())
        _repl(adapter, out, "reload", seq=10)
        _repl(adapter, out, "unlink A", seq=11)
        script.write_text(_declared_a_script().replace("'A'", "'B'"))

        resp = _plan_reload(adapter, out, links={"A": {"cut": True, "fb": "B"}})

        entry = _carried(resp, "A", "cut")
        assert (entry["to"], entry["dropped"]) == ("B", False)
        assert adapter._bench_links["B"].coupling is None

    def test_a_cut_on_a_feedback_the_program_does_not_link_is_dropped(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        script.write_text(_declared_a_script())
        _repl(adapter, out, "reload", seq=10)
        _repl(adapter, out, "unlink A", seq=11)
        _rewrite(script, a="B")

        resp = _plan_reload(adapter, out, links={"A": {"cut": True, "fb": "B"}})

        entry = _carried(resp, "A", "cut")
        assert (entry["dropped"], entry["reason"], entry["to"]) == (True, "unlinked", None)
        assert entry["detail"] == "the new program does not link B"
        assert adapter._bench_links == {}

    def test_an_entry_the_plan_leaves_out_follows_its_name(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _repl(adapter, out, "force Lamp true", seq=11)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=12)

        resp = _plan_reload(adapter, out, forces={"A": "A"})

        assert _carried(resp, "A", "force")["planned"] is True
        lamp = _carried(resp, "Lamp", "force")
        assert (lamp["planned"], lamp["to"], lamp["old"], lamp["new"]) == (
            False,
            "Lamp",
            "Y001",
            "Y001",
        )
        link = _carried(resp, "A", "link")
        assert (link["planned"], link["to"], link["enable"]) == (False, "A", "Cmd")
        text = resp["body"]["result"]
        assert "The force on Lamp is not in the plan, so it stays on Lamp." in text
        assert "The bench link on A is not in the plan, so it stays on A." in text
        assert adapter._runner.forces == {"A": True, "Lamp": True}
        assert list(adapter._bench_links) == ["A"]

    def test_a_plan_naming_a_missing_tag_drops_that_entry(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _repl(adapter, out, "force Lamp true", seq=11)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=12)

        resp = _plan_reload(
            adapter,
            out,
            forces={"A": "Nope", "Lamp": "Lamp", "Ghost": "Lamp"},
            links={"A": {"en": "Gone", "fb": "A"}},
        )

        assert resp["success"] is True
        force = _carried(resp, "A", "force")
        assert (force["dropped"], force["reason"], force["to"]) == (True, "gone", None)
        assert force["detail"] == "Nope is not in the new program"
        ghost = _carried(resp, "Ghost", "force")
        assert (ghost["dropped"], ghost["reason"]) == (True, "not forced")
        link = _carried(resp, "A", "link")
        assert (link["dropped"], link["reason"], link["detail"]) == (
            True,
            "gone",
            "Gone is not in the new program",
        )
        text = resp["body"]["result"]
        assert "Dropped the force on A because Nope is not in the new program." in text
        assert "The plan names a force on Ghost, but Ghost is not forced." in text
        assert adapter._runner.forces == {"Lamp": True}
        assert adapter._bench_links == {}

    def test_a_link_plan_with_no_enable_is_an_error_not_a_cut(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)

        resp = _plan_reload(adapter, out, links={"A": {"fb": "A"}, "Lamp": None})

        entry = _carried(resp, "A", "link")
        assert (entry["dropped"], entry["reason"]) == (True, "bad plan")
        assert entry["detail"] == "its plan names no enable (en) and is not a cut"
        assert _carried(resp, "Lamp", "link")["reason"] == "no bench change"
        assert adapter._bench_links == {}

    def test_two_forces_onto_one_tag_are_both_dropped(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _repl(adapter, out, "force Lamp false", seq=11)

        resp = _plan_reload(adapter, out, forces={"A": "Lamp"})

        a, lamp = _carried(resp, "A", "force"), _carried(resp, "Lamp", "force")
        assert (a["reason"], a["detail"]) == ("conflict", "the force on Lamp also goes to Lamp")
        assert (lamp["reason"], lamp["detail"]) == ("conflict", "the force on A also goes to Lamp")
        assert adapter._runner.forces == {}

    def test_two_links_planned_onto_one_feedback_are_both_dropped(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        _repl(adapter, out, "link Cmd Lamp on_delay=0ms", seq=11)

        resp = _plan_reload(
            adapter,
            out,
            links={"A": {"en": "Lamp", "fb": "A"}, "Cmd": {"en": "Lamp", "fb": "A"}},
        )

        a, cmd = _carried(resp, "A", "link"), _carried(resp, "Cmd", "link")
        assert (a["dropped"], a["reason"], a["to"]) == (True, "conflict", None)
        assert a["detail"] == "the bench change on Cmd also goes to A"
        assert (cmd["dropped"], cmd["reason"], cmd["detail"]) == (
            True,
            "conflict",
            "the bench change on A also goes to A",
        )
        assert (
            "Dropped the bench link on Cmd because the bench change on A also goes to A."
            in (resp["body"]["result"])
        )
        assert adapter._bench_links == {}

    def test_a_link_planned_onto_a_feedback_another_link_keeps_drops_both(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=10)
        _repl(adapter, out, "link Cmd Lamp on_delay=0ms", seq=11)

        resp = _plan_reload(adapter, out, links={"A": {"en": "Lamp", "fb": "Cmd"}})

        assert _carried(resp, "A", "link")["reason"] == "conflict"
        cmd = _carried(resp, "Cmd", "link")
        assert (cmd["planned"], cmd["reason"]) == (False, "conflict")
        assert adapter._bench_links == {}

    def test_a_bad_plan_shape_refuses_the_reload(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        old_runner = adapter._runner

        resp = _plan_reload(adapter, out, forces=["A"])

        assert resp["success"] is False
        assert "pyrungReload.forces must be an object" in resp["message"]
        assert adapter._runner is old_runner

    def test_a_failed_reload_leaves_the_plan_unapplied(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _repl(adapter, out, "link A Cmd on_delay=0ms", seq=11)
        _repl(adapter, out, "mode stop", seq=12)
        old_runner = adapter._runner
        script.write_text("this is not python (\n")

        resp = _plan_reload(adapter, out, forces={"A": "B"}, links={"A": None})

        assert resp["success"] is False
        assert "Reload failed" in resp["message"]
        assert adapter._runner is old_runner
        assert adapter._runner.mode == "stop"
        assert adapter._runner.forces == {"A": True}
        assert list(adapter._bench_links) == ["A"]

    def test_a_planned_reload_while_running_pauses_first(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _send_request(adapter, out, seq=10, command="continue")

        resp = _plan_reload(adapter, out)

        assert resp["success"] is True
        assert resp["body"]["carried"] == []
        assert not adapter._thread_running_locked()

    def test_a_planned_reload_is_logged_as_a_reload(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)

        _plan_reload(adapter, out, forces={})

        assert adapter._action_log == [(adapter._runner.current_state.scan_id, "reload", "console")]

    def test_a_force_dropped_for_a_conflict_is_released(self, tmp_path: Path):
        adapter, out, _script = _carry(tmp_path)
        _repl(adapter, out, "force A true", seq=10)
        _repl(adapter, out, "force Lamp false", seq=11)

        resp = _plan_reload(adapter, out, forces={"A": "Lamp"})

        for tag in ("A", "Lamp"):
            entry = _carried(resp, tag, "force")
            assert (entry["dropped"], entry["released"], entry["to"]) == (True, True, None)

    def test_a_force_moves_onto_a_tag_whose_old_namesake_had_another_type(self, tmp_path: Path):
        old = (
            "from pyrung.core import Bool, Int, PLC, Program, Rung, out\n"
            "a = Int('A')\n"
            "b = Bool('B')\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung(a > 0):\n"
            "        out(Bool('Sink'))\n"
            "    with Rung(b):\n"
            "        out(Bool('Sink2'))\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        new = (
            "from pyrung.core import Bool, Int, PLC, Program, Rung, out\n"
            "b = Int('B')\n"
            "with Program(strict=False) as prog:\n"
            "    with Rung(b > 0):\n"
            "        out(Bool('Sink'))\n"
            "runner = PLC(prog, dt=0.010)\n"
        )
        adapter, out, script = _setup(tmp_path, old)
        _repl(adapter, out, "force A 5", seq=10)
        script.write_text(new)

        resp = _plan_reload(adapter, out, forces={"A": "B"})

        entry = _carried(resp, "A", "force")
        assert (entry["to"], entry["dropped"]) == ("B", False)
        assert adapter._runner.forces == {"B": 5}

    def test_a_force_planned_onto_a_tag_of_another_type_is_dropped(self, tmp_path: Path):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "force Lamp true", seq=10)
        script.write_text(_int_a_script())

        resp = _plan_reload(adapter, out, forces={"Lamp": "A"})

        entry = _carried(resp, "Lamp", "force")
        assert (entry["dropped"], entry["reason"]) == (True, "type changed")
        assert entry["detail"] == "A is an Int in the new program"
        assert (
            "Dropped the force on Lamp because A is an Int in the new program."
            in (resp["body"]["result"])
        )
        assert adapter._runner.forces == {}


TWO_SCRIPT = (
    "from pyrung.core import Bool, Int, Physical, PLC, Program, Real, Rung, out\n"
    "a = {a}\n"
    "b = {b}\n"
    "cmd = Bool('Cmd')\n"
    "go = Bool('Go')\n"
    "with Program(strict=False) as prog:\n"
    "    with Rung(cmd):\n"
    "        out(Bool('Lamp'))\n"
    "    with Rung(go):\n"
    "        out(Bool('Lamp2'))\n"
    "    with Rung({a_read}):\n"
    "        out(Bool('Sink'))\n"
    "    with Rung(b):\n"
    "        out(Bool('Sink2'))\n"
    "runner = PLC(prog, dt=0.010)\n"
)


def _two(a: str = "Bool('A')", b: str = "Bool('B')", a_read: str = "a") -> str:
    """A program with two feedbacks, ``a`` and ``b``, and two enables, Cmd and Go."""
    return TWO_SCRIPT.format(a=a, b=b, a_read=a_read)


def _timed(name: str, enable: str = "Cmd", ms: int = 100) -> str:
    return f"Bool('{name}', physical=Physical('{name}', timed='{ms}ms'), link='{enable}')"


def _counts(adapter: DAPAdapter, *fbs: str) -> tuple[int, ...]:
    return tuple(_timed_count(adapter, fb) for fb in fbs)


class TestPlannedCoilMoves:
    def test_a_moved_link_leaves_the_coil_of_a_feedback_named_with_an_underscore(
        self, tmp_path: Path
    ):
        adapter, out, script = _setup(tmp_path, _two(b="Bool('_A')"))
        _repl(adapter, out, "link A Cmd timed=100ms", seq=10)
        _repl(adapter, out, "link _A Cmd timed=1000ms", seq=11)
        _repl(adapter, out, "force Cmd true", seq=12)
        _repl(adapter, out, "step 3", seq=13)
        (count,) = _counts(adapter, "_A")
        script.write_text(_two(a="Bool('B')", b="Bool('_A')"))

        _plan_reload(adapter, out, links={"A": {"en": "Cmd", "fb": "B"}})
        _repl(adapter, out, "step 1", seq=14)

        tags = adapter._runner.current_state.tags
        assert _counts(adapter, "B", "_A") == (count + 10, count + 10)
        assert "__tmd_on___B" not in tags
        assert "__tmd_ond___B" not in tags

    def test_a_ramp_moved_onto_an_int_starts_from_the_new_default(self, tmp_path: Path):
        old = _two(a="Real('A')", b="Bool('Other')", a_read="a > 5")
        adapter, out, script = _setup(tmp_path, old)
        _repl(adapter, out, "link A Cmd ramp_on=37.3", seq=10)
        _repl(adapter, out, "force Cmd true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        assert adapter._runner.current_state.tags["A"] > 0
        script.write_text(old.replace("Real('A')", "Int('B')"))

        resp = _plan_reload(adapter, out, links={"A": {"en": "Cmd", "fb": "B"}})

        assert _carried(resp, "A", "link")["to"] == "B"
        b = adapter._runner.current_state.tags.get("B", 0)
        assert b == 0 and type(b) is int

    def test_two_links_swapped_keep_each_others_counts(self, tmp_path: Path):
        adapter, out, _script = _setup(tmp_path, _two())
        _repl(adapter, out, "link A Cmd timed=100ms", seq=10)
        _repl(adapter, out, "link B Go timed=1000ms", seq=11)
        _repl(adapter, out, "force Cmd true", seq=12)
        _repl(adapter, out, "step 3", seq=13)
        _repl(adapter, out, "force Go true", seq=14)
        _repl(adapter, out, "step 1", seq=15)
        a, b = _counts(adapter, "A", "B")
        assert a != b

        _plan_reload(
            adapter,
            out,
            links={"A": {"en": "Cmd", "fb": "B"}, "B": {"en": "Go", "fb": "A"}},
        )

        assert _counts(adapter, "A", "B") == (b, a)
        assert adapter._bench_links["B"].coupling.en_name == "Cmd"

    def test_a_link_is_not_moved_onto_a_feedback_whose_own_coil_stays(self, tmp_path: Path):
        program = _two(b=_timed("B", "Go"))
        adapter, out, script = _setup(tmp_path, program)
        _repl(adapter, out, "link A Cmd timed=100ms", seq=10)
        _repl(adapter, out, "force Cmd true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "force Go true", seq=13)
        _repl(adapter, out, "step 1", seq=14)
        a, b = _counts(adapter, "A", "B")
        assert a != b
        script.write_text(program)

        resp = _plan_reload(adapter, out, links={"A": {"en": "Go", "fb": "B"}})

        entry = _carried(resp, "A", "link")
        assert (entry["dropped"], entry["reason"], entry["to"]) == (True, "in use", None)
        assert entry["detail"] == "the old program already ran a coil on B"
        assert (
            "Dropped the bench link on A because the old program already ran a coil on B."
            in (resp["body"]["result"])
        )
        assert adapter._bench_links == {}
        assert _counts(adapter, "B") == (b,)


class TestPlannedDeclaredLinks:
    def test_a_declared_link_renamed_keeps_its_count(self, tmp_path: Path):
        adapter, out, script = _setup(tmp_path, _two(a=_timed("A")))
        _repl(adapter, out, "force Cmd true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        (count,) = _counts(adapter, "A")
        assert 0 < count < 100
        script.write_text(_two(a=_timed("C")))

        resp = _plan_reload(adapter, out, declared={"A": "C"})

        assert resp["success"] is True
        assert "Moved the declared link on A to C." in resp["body"]["result"]
        assert _carried(resp, "A", "declared") == {
            "tag": "A",
            "kind": "declared",
            "to": "C",
            "old": None,
            "new": None,
            "planned": True,
            "released": False,
            "dropped": False,
            "feedback": "A",
            "enable": "Cmd",
        }
        assert _counts(adapter, "C") == (count,)
        _repl(adapter, out, "step 1", seq=12)
        assert _counts(adapter, "C") == (count + 10,)
        assert adapter._runner.current_state.tags["C"] is True

    def test_without_the_plan_the_renamed_declared_link_settles(self, tmp_path: Path):
        adapter, out, script = _setup(tmp_path, _two(a=_timed("A")))
        _repl(adapter, out, "force Cmd true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        script.write_text(_two(a=_timed("C")))

        _plan_reload(adapter, out)

        assert _counts(adapter, "C") == (100,)
        assert adapter._runner.current_state.tags.get("C", False) is False

    def test_a_declared_link_whose_enable_is_renamed_keeps_its_count(self, tmp_path: Path):
        adapter, out, script = _setup(tmp_path, _two(a=_timed("A")))
        _repl(adapter, out, "force Cmd true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        (count,) = _counts(adapter, "A")
        script.write_text(_two(a=_timed("C", "Go")))

        resp = _plan_reload(adapter, out, forces={"Cmd": "Go"}, declared={"A": "C"})

        assert _carried(resp, "A", "declared")["enable"] == "Go"
        assert _counts(adapter, "C") == (count,)

    def test_an_entry_without_a_declared_link_on_either_side_is_dropped(self, tmp_path: Path):
        adapter, out, script = _setup(tmp_path, _two(a=_timed("A"), b=_timed("B", "Go")))
        _repl(adapter, out, "link B Cmd timed=100ms", seq=10)
        script.write_text(_two(a=_timed("A")))

        resp = _plan_reload(adapter, out, declared={"A": "Lamp", "Lamp": "C", "B": "C", "Go": 5})

        reasons = {
            entry["tag"]: (entry["reason"], entry["detail"])
            for entry in resp["body"]["carried"]
            if entry["kind"] == "declared"
        }
        assert reasons == {
            "A": ("unlinked", "the new program does not link Lamp"),
            "Lamp": ("not declared", "the old program declares no link on Lamp"),
            "B": ("not declared", "a bench change replaced the program's link on B"),
            "Go": ("bad plan", "its plan is not a tag name"),
        }

    def test_a_declared_link_onto_a_missing_tag_is_dropped_gone(self, tmp_path: Path):
        adapter, out, script = _setup(tmp_path, _two(a=_timed("A")))

        resp = _plan_reload(adapter, out, declared={"A": "Nope"})

        entry = _carried(resp, "A", "declared")
        assert (entry["reason"], entry["detail"]) == ("gone", "Nope is not in the new program")

    def test_a_declared_link_is_not_moved_onto_a_feedback_whose_own_coil_stays(
        self, tmp_path: Path
    ):
        adapter, out, script = _setup(tmp_path, _two(a=_timed("A"), b=_timed("B", "Go")))
        _repl(adapter, out, "force Cmd true", seq=10)
        _repl(adapter, out, "step 3", seq=11)
        _repl(adapter, out, "force Go true", seq=12)
        _repl(adapter, out, "step 1", seq=13)
        (b,) = _counts(adapter, "B")
        script.write_text(_two(b=_timed("B", "Go")))

        resp = _plan_reload(adapter, out, declared={"A": "B"})

        entry = _carried(resp, "A", "declared")
        assert (entry["dropped"], entry["reason"]) == (True, "in use")
        assert _counts(adapter, "B") == (b,)

    def test_a_declared_link_and_a_bench_link_onto_one_feedback_are_both_dropped(
        self, tmp_path: Path
    ):
        adapter, out, script = _setup(tmp_path, _two(a=_timed("A")))
        _repl(adapter, out, "link B Go timed=100ms", seq=10)
        script.write_text(_two(b=_timed("B")))

        resp = _plan_reload(adapter, out, declared={"A": "B"})

        declared, bench = _carried(resp, "A", "declared"), _carried(resp, "B", "link")
        assert (declared["reason"], declared["detail"]) == (
            "conflict",
            "the bench change on B also goes to B",
        )
        assert (bench["reason"], bench["detail"]) == (
            "conflict",
            "the declared link on A also goes to B",
        )
        assert adapter._bench_links == {}


def _bad_trigger(script: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script.write_text(
        _carry_script(a_decl="Bool('A', physical=Physical('A', on_delay='0ms'), link='Cmd:foo')")
    )


def _harness_raises(script: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pyrung.core.harness import Harness

    def broken(self: Any) -> None:
        raise RuntimeError("the harness broke")

    script.write_text(_carry_script(a="B"))
    monkeypatch.setattr(Harness, "_install", broken)


def _tag_map_raises(script: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pyrung.dap.handlers import lifecycle_launch

    def broken(*_args: Any) -> None:
        raise RuntimeError("the tag map broke")

    script.write_text(_carry_script(a="B"))
    monkeypatch.setattr(lifecycle_launch, "discover_click_project", broken)


def _adapter_view(adapter: DAPAdapter) -> dict[str, Any]:
    runner = adapter._runner
    assert runner is not None
    breakpoints = adapter._breakpoints
    harness = adapter._harness
    return {
        "runner": runner,
        "harness": harness,
        "harness overrides": dict(harness.overrides) if harness is not None else None,
        "forces": dict(runner.forces),
        "bench links": dict(adapter._bench_links),
        "mode": runner.mode,
        "state": runner.current_state,
        "breakpoints": breakpoints,
        "breakpoint lines": {
            path: sorted(lines) for path, lines in breakpoints.source_breakpoints_by_file.items()
        },
        "breakpoint rungs": dict(breakpoints.breakpoint_rung_map),
        "tag map": adapter._tag_map,
        "click provider": adapter._click_provider,
        "links revision": adapter._links_revision,
    }


class TestAFailedReloadChangesNothing:
    @pytest.mark.parametrize("planned", [False, True], ids=["reload", "pyrungReload"])
    @pytest.mark.parametrize(
        ("edit", "message"),
        [
            (_bad_trigger, "Trigger value 'foo' is not an int literal"),
            (_harness_raises, "the harness broke"),
            (_tag_map_raises, "the tag map broke"),
        ],
        ids=["bad link trigger", "harness install raises", "tag map bind raises"],
    )
    def test_a_reload_that_fails_after_the_program_ran(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        planned: bool,
        edit: Any,
        message: str,
    ):
        adapter, out, script = _carry(tmp_path)
        _repl(adapter, out, "link A Cmd timed=100ms", seq=10)
        _repl(adapter, out, "force Cmd true", seq=11)
        _repl(adapter, out, "step 3", seq=12)
        _repl(adapter, out, "mode stop", seq=13)
        _send_request(
            adapter,
            out,
            seq=14,
            command="setBreakpoints",
            arguments={"source": {"path": str(script)}, "lines": [10]},
        )
        before = _adapter_view(adapter)
        assert before["breakpoint lines"] and before["harness"] is not None
        edit(script, monkeypatch)

        if planned:
            resp = _plan_reload(
                adapter, out, forces={"Cmd": None}, links={"A": {"en": "Cmd", "fb": "B"}}
            )
        else:
            resp, _stopped = _repl(adapter, out, "reload", seq=15)

        assert resp["success"] is False
        assert _adapter_view(adapter) == before
        assert "Reload failed" in resp["message"] and message in resp["message"]
