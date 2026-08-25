"""The public console grammar — the contract downstream completers consume.

These tests exist because the grammar is derived from human-facing ``usage=``
prose. Rewording a usage string can silently downgrade a slot (an expression slot
becoming freeform), which a downstream completer experiences as "autocomplete
stopped working" with nothing failing here. The degeneracy guards below turn that
into a test failure in this repo.
"""

from __future__ import annotations

import pytest

from pyrung.dap.grammar import command_grammar, parse_usage


@pytest.fixture(scope="module")
def grammar() -> dict:
    return command_grammar()


class TestCommandGrammar:
    def test_covers_every_registered_verb(self, grammar: dict):
        from pyrung.dap.console import _REGISTRY

        assert set(grammar) == set(_REGISTRY)
        assert len(grammar) >= 30

    def test_tag_commands_expose_tag_slots(self, grammar: dict):
        assert grammar["get"].slots[0].kind == "tag"
        assert grammar["get"].slots[1].repeat is True
        assert grammar["unforce"].slots[0].kind == "tag"
        assert grammar["why"].slots[0].kind == "tag"

    def test_force_takes_a_tag_then_a_value(self, grammar: dict):
        kinds = [s.kind for s in grammar["force"].slots]
        assert kinds == ["tag", "value"]

    def test_choices_are_enumerated(self, grammar: dict):
        prove = grammar["prove"].slots[0]
        assert prove.kind == "choices"
        assert set(prove.choices) == {"always", "never"}

        harness = grammar["harness"].slots[0]
        assert harness.kind == "choices"
        assert set(harness.choices) == {"install", "remove", "status"}

    def test_flags_are_flags(self, grammar: dict):
        flags = {s.choices[0] for s in grammar["prove"].slots if s.kind == "flag"}
        assert flags == {"--settled", "--paced"}

    def test_no_command_degenerates_to_all_text(self, grammar: dict):
        """A usage naming <tag>/<expression> must yield a completable slot.

        This is the guard: if a reword makes the derivation miss, the command's
        slots collapse to `text` (nothing to complete) and this fails.
        """
        completable = {"tag", "expression", "choices", "flag"}
        for verb, cg in grammar.items():
            names_tag_or_expr = "<tag>" in cg.usage or "<expression>" in cg.usage
            if not names_tag_or_expr:
                continue
            kinds = {s.kind for s in cg.slots}
            assert kinds & completable, f"{verb}: usage {cg.usage!r} derived {cg.slots!r}"


class TestHowGrammar:
    """`how` is the declared (not derived) case — the prose can't express it."""

    def test_targets_are_a_comma_separated_repeat(self, grammar: dict):
        target = grammar["how"].slots[0]
        assert target.kind == "expression"
        assert target.required is True
        assert target.repeat is True
        assert target.separator == ","
        assert target.keyword == ""

    def test_avoid_is_a_keyword_clause_and_a_comma_repeat(self, grammar: dict):
        avoid = next(s for s in grammar["how"].slots if s.keyword == "avoid")
        assert avoid.kind == "expression"
        assert avoid.required is False
        assert avoid.repeat is True
        assert avoid.separator == ","

    def test_avoid_is_the_only_keyword_clause(self, grammar: dict):
        assert {slot.keyword for slot in grammar["how"].slots if slot.keyword} == {"avoid"}

    def test_every_how_slot_takes_an_expression(self, grammar: dict):
        assert all(s.kind == "expression" for s in grammar["how"].slots)


class TestParseUsage:
    def test_nested_brackets_are_one_token(self):
        slots = parse_usage("x", "x [avoid <expression>[, <expression>...]]")
        assert len(slots) == 1
        assert slots[0].kind == "expression"
        assert slots[0].required is False

    def test_comma_repeat_sets_separator(self):
        slots = parse_usage("x", "x <expression>[, <expression>...]")
        assert slots[-1].repeat is True
        assert slots[-1].separator == ","

    def test_space_repeat_keeps_space_separator(self):
        slots = parse_usage("x", "x <tag> [tag2 ...]")
        assert slots[-1].repeat is True
        assert slots[-1].separator == " "

    def test_expr_abbreviation_is_still_an_expression(self):
        """pyrung abbreviates in places; a miss here silently kills completion."""
        assert parse_usage("x", "x <expr>")[0].kind == "expression"

    def test_trailing_parenthetical_is_dropped(self):
        slots = parse_usage("run", "run <N | duration>  (e.g. 10, 500ms, 2 s)")
        assert len(slots) == 1
        assert slots[0].kind == "text"

    def test_declared_slots_win_over_usage(self):
        from pyrung.dap.console import _REGISTRY

        assert _REGISTRY["how"].slots is not None
        assert command_grammar()["how"].slots == _REGISTRY["how"].slots
