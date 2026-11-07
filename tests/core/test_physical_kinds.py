"""The physical kinds table: each kind's keys, Python <-> comment round trips, and misuse."""

from __future__ import annotations

from typing import Any

import pytest

from pyrung.click.tag_map import parse_tag_meta
from pyrung.click.tag_map._parsers import format_tag_meta
from pyrung.core import PLC, Bool, Harness, Program, Real, Rung, out
from pyrung.core.physical import (
    PHYSICAL_KEYS,
    PHYSICAL_KINDS,
    Physical,
    kind_of_keys,
    physical_kind,
)

# One Physical per kind, and the comment text that writes it.
EVERY_KIND: list[tuple[str, dict[str, Any], str]] = [
    ("delay", {"on_delay": "2s", "off_delay": "500ms"}, "on_delay=2s, off_delay=500ms"),
    ("delay", {"off_delay": "1s"}, "off_delay=1s"),
    ("flasher", {"flasher_on": "8ms", "flasher_off": "12ms"}, "flasher_on=8ms, flasher_off=12ms"),
    ("timed", {"timed": "200ms"}, "timed=200ms"),
    ("timed", {"timed": "1s", "maintained": True}, "timed=1s, maintained"),
    ("toggle", {"toggle": True}, "toggle"),
    ("ramp", {"ramp_on": 0.8, "ramp_off": -0.05}, "ramp_on=0.8, ramp_off=-0.05"),
    ("ramp", {"ramp_on": 2}, "ramp_on=2"),
    ("approach", {"approach_to": 180, "approach_rate": 0.3}, "approach_to=180, approach_rate=0.3"),
    (
        "approach",
        {"approach_to": "Oven_SP", "approach_rate": 0.3},
        "approach_to=Oven_SP, approach_rate=0.3",
    ),
]


def _ids() -> list[str]:
    return [text for _kind, _keywords, text in EVERY_KIND]


class TestKindsTable:
    def test_every_key_belongs_to_the_kind_that_lists_it(self):
        for kind in PHYSICAL_KINDS:
            for key in kind.keys:
                assert key.kind == kind.name
                assert PHYSICAL_KEYS[key.name] is key

    def test_kinds_and_keys_in_form_order(self):
        assert [kind.name for kind in PHYSICAL_KINDS] == [
            "delay",
            "flasher",
            "timed",
            "toggle",
            "ramp",
            "approach",
        ]
        assert list(PHYSICAL_KEYS) == [
            "on_delay",
            "off_delay",
            "flasher_on",
            "flasher_off",
            "timed",
            "maintained",
            "toggle",
            "ramp_on",
            "ramp_off",
            "approach_to",
            "approach_rate",
        ]

    def test_feedback_types(self):
        bools = {kind.name for kind in PHYSICAL_KINDS if kind.feedback == "bool"}
        assert bools == {"delay", "flasher", "timed", "toggle"}

    def test_flags_are_toggle_and_maintained(self):
        assert {name for name, key in PHYSICAL_KEYS.items() if key.is_flag} == {
            "toggle",
            "maintained",
        }

    def test_only_ramp_off_has_a_number_default(self):
        defaults = {name: key.default for name, key in PHYSICAL_KEYS.items()}
        assert defaults["ramp_off"] == 0.0
        assert defaults["maintained"] is False
        assert {name for name, value in defaults.items() if type(value) is float} == {"ramp_off"}

    def test_required_rules(self):
        delay = physical_kind("delay")
        assert delay.needs == "any"
        assert delay.missing([]) == ("on_delay", "off_delay")
        assert delay.missing(["off_delay"]) == ()
        assert physical_kind("ramp").missing(["ramp_off"]) == ("ramp_on",)
        assert physical_kind("timed").missing(["maintained"]) == ("timed",)

    def test_every_key_has_a_label_and_help_and_a_value_key_a_suggestion(self):
        for key in PHYSICAL_KEYS.values():
            assert key.label and key.help
            if not key.is_flag:
                assert key.suggested is not None
                key.parse(key.suggested)

    def test_unknown_kind_name(self):
        with pytest.raises(ValueError, match="Unknown physical kind 'pulse'"):
            physical_kind("pulse")


class TestRoundTrip:
    @pytest.mark.parametrize(("kind", "keywords", "text"), EVERY_KIND, ids=_ids())
    def test_python_and_comment_give_the_same_physical(self, kind, keywords, text):
        from_python = Physical("Fb", **keywords)
        meta, rest = parse_tag_meta(f"[link=Run, {text}] note")
        assert meta is not None and rest == "note"
        from_comment = Physical.from_text("Fb", meta.physical_values())
        assert from_python == from_comment
        assert from_python.kind == kind
        assert meta.physical_kind is physical_kind(kind)

    @pytest.mark.parametrize(("kind", "keywords", "text"), EVERY_KIND, ids=_ids())
    def test_the_writer_writes_the_keys(self, kind, keywords, text):
        physical = Physical("Fb", **keywords)
        assert ", ".join(physical.tokens()) == text
        meta, _rest = parse_tag_meta(f"[{text}]")
        assert format_tag_meta(meta) == f"[{text}]"

    @pytest.mark.parametrize(("kind", "keywords", "text"), EVERY_KIND, ids=_ids())
    def test_keywords_rebuild_the_physical(self, kind, keywords, text):
        physical = Physical("Fb", **keywords, system="Line")
        assert Physical("Fb", **physical.keywords(), system="Line") == physical
        assert eval(repr(physical), {"Physical": Physical}) == physical

    def test_ramp_off_left_out_is_zero(self):
        physical = Physical("T", ramp_on=1.0)
        assert physical.ramp_off == 0.0
        assert physical == Physical("T", ramp_on=1.0, ramp_off=0)
        assert physical.tokens() == ["ramp_on=1"]

    def test_durations_keep_their_text(self):
        physical = Physical("M", on_delay="1s500ms")
        assert physical.on_delay == "1s500ms"
        assert physical.on_delay_ms == 1500


class TestMisuse:
    @pytest.mark.parametrize(
        ("keywords", "error", "match"),
        [
            ({"on_delay": 500}, TypeError, "on_delay takes a time with units"),
            ({"flasher_on": 8, "flasher_off": "8ms"}, TypeError, "flasher_on takes a time"),
            ({"timed": 0.2}, TypeError, "timed takes a time with units"),
            ({"on_delay": "fast"}, ValueError, "on_delay"),
            ({"ramp_on": "0.8"}, TypeError, "ramp_on takes a number"),
            ({"ramp_on": True}, TypeError, "ramp_on takes a number"),
            ({"ramp_on": float("nan")}, ValueError, "finite number"),
            ({"approach_to": "180", "approach_rate": 0.3}, ValueError, "looks like a number"),
            ({"approach_to": 180, "approach_rate": "0.3"}, TypeError, "approach_rate takes"),
            ({"toggle": "yes"}, TypeError, "toggle takes True or False"),
            ({"maintained": True}, ValueError, "needs timed"),
            ({"ramp_off": -0.5}, ValueError, "needs ramp_on"),
            ({"approach_to": 100}, ValueError, "needs approach_rate"),
            ({"flasher_on": "8ms"}, ValueError, "needs flasher_off"),
            ({"on_delay": "1s", "ramp_on": 1.0}, ValueError, "two kinds"),
            ({"toggle": True, "timed": "1s"}, ValueError, "two kinds"),
            ({"flasher_on": "8ms", "flasher_off": "8ms", "off_delay": "1s"}, ValueError, "two"),
            ({}, ValueError, "has no response"),
            ({"profile": "x"}, TypeError, "no longer takes profile="),
        ],
    )
    def test_each_misuse_raises_ascii(self, keywords, error, match):
        with pytest.raises(error, match=match) as caught:
            Physical("Fb", **keywords)
        assert str(caught.value).isascii()

    def test_two_kinds_names_both_keys(self):
        with pytest.raises(ValueError) as caught:
            # ty refuses this call too, through the overload per kind.
            Physical("Fb", flasher_on="8ms", flasher_off="8ms", ramp_on=1.0)  # ty: ignore[no-matching-overload]
        assert str(caught.value) == (
            "Physical 'Fb': flasher_on (flasher) and ramp_on (ramp) are keys of two kinds. "
            "A feedback has one kind."
        )

    def test_kind_of_keys(self):
        assert kind_of_keys([]) is None
        assert kind_of_keys(["toggle"]) is physical_kind("toggle")
        with pytest.raises(ValueError, match="unknown key 'up'"):
            kind_of_keys(["up"])

    @pytest.mark.parametrize(
        ("comment", "match"),
        [
            ("[toggle=1]", "toggle is a bare flag"),
            ("[timed=1s, maintained=yes]", "maintained is a bare flag"),
            ("[on_delay=500]", "on_delay"),
            ("[ramp_on=fast]", "ramp_on: expected a number"),
            ("[ramp_on=1, on_delay=1s]", "two kinds"),
            ("[maintained]", "needs timed"),
        ],
    )
    def test_each_comment_misuse_raises_ascii(self, comment, match):
        with pytest.raises(ValueError, match=match) as caught:
            parse_tag_meta(comment)
        assert str(caught.value).isascii()

    def test_from_text_wants_a_flag_bare(self):
        with pytest.raises(ValueError, match="toggle is a bare flag"):
            Physical.from_text("Fb", {"toggle": "1"})


class TestNotYetInTheHarness:
    @pytest.mark.parametrize("keywords", [{"timed": "200ms"}, {"toggle": True}])
    def test_timed_and_toggle_say_not_yet(self, keywords):
        fb = Bool("Fb", physical=Physical("Fb", **keywords), link="Cmd")
        with Program(strict=False) as prog:
            with Rung(Bool("Cmd")):
                out(Bool("Lamp"))
            with Rung(fb):
                out(Bool("Seen"))
        plc = PLC(prog, dt=0.010)
        with pytest.raises(NotImplementedError, match="does not run in the harness yet"):
            Harness(plc).install()

    def test_a_ramp_still_runs(self):
        temp = Real("Temp", physical=Physical("Temp", ramp_on=10.0), link="Heat")
        with Program(strict=False) as prog:
            with Rung():
                out(Bool("Heat"))
            with Rung(temp > 5.0):
                out(Bool("Hot"))
        plc = PLC(prog, dt=0.100)
        Harness(plc).install()
        plc.run_for(1.0)
        assert plc.state.tags["Temp"] > 5.0
