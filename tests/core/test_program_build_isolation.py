"""A program under construction is the current thread's, and the innermost open one."""

from __future__ import annotations

import threading
from typing import Any

from pyrung.core import Bool, Int, Program, Rung, out, receive, send
from pyrung.core.instruction.send_receive import is_modbus_status_tag
from pyrung.core.tag import Tag


def _targets(program: Program) -> list[str]:
    return [i.target.name for rung in program.rungs for i in rung._instructions]


def test_a_program_built_inside_another_gives_the_outer_one_back():
    with Program(strict=False) as outer:
        with Rung():
            out(Bool("Before"))
        with Program(strict=False) as inner:
            with Rung():
                out(Bool("Inner"))
        with Rung():
            out(Bool("After"))
    assert _targets(outer) == ["Before", "After"]
    assert _targets(inner) == ["Inner"]


def test_two_threads_build_two_programs_at_once():
    a_open, b_open, a_wrote = threading.Event(), threading.Event(), threading.Event()
    built: dict[str, Program] = {}
    errors: list[BaseException] = []

    def build_a() -> None:
        with Program(strict=False) as program:
            with Rung():
                a_open.set()
                assert b_open.wait(5)
                out(Bool("A"))
                a_wrote.set()
        built["a"] = program

    def build_b() -> None:
        assert a_open.wait(5)
        with Program(strict=False) as program:
            with Rung():
                b_open.set()
                assert a_wrote.wait(5)
                out(Bool("B"))
        built["b"] = program

    def run(build: Any) -> None:
        try:
            build()
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(build,)) for build in (build_a, build_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []
    assert _targets(built["a"]) == ["A"]
    assert _targets(built["b"]) == ["B"]


def test_a_status_bit_left_off_has_the_same_name_in_every_build():
    def build() -> list[str]:
        with Program(strict=False) as program:
            with Rung():
                send(target="panel", remote_start="DS1", source=Int("X"))
            with Rung():
                receive(target="panel", remote_start="DS2", dest=Int("Y"))
        return [
            value.name
            for rung in program.rungs
            for instruction in rung._instructions
            for value in vars(instruction).values()
            if isinstance(value, Tag) and is_modbus_status_tag(value.name)
        ]

    first = build()
    assert first[0] == "_modbus.1.sending"
    assert build() == first
