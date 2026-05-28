"""Tests for split_at (user-guided state-space decomposition)."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, Or, Program, Rung, copy, latch, out, rise
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    _build_explore_context,
    explore,
    prove,
    reachable_states,
)
from pyrung.core.analysis.prove.independence import _find_bridge_tags

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _two_zone_with_shared_bool():
    """Two independent zones coupled by a shared AutoMode Bool.

    Zone1: A1 & AutoMode → Zone1Out
    Zone2: B1 & AutoMode → Zone2Out
    AutoMode: latched by SetAuto
    """
    a1 = Bool("A1", external=True)
    b1 = Bool("B1", external=True)
    auto_mode = Bool("AutoMode")
    set_auto = Bool("SetAuto", external=True)
    zone1_out = Bool("Zone1Out")
    zone2_out = Bool("Zone2Out")

    with Program(strict=False) as logic:
        with Rung(set_auto):
            latch(auto_mode)
        with Rung(a1, auto_mode):
            out(zone1_out)
        with Rung(b1, auto_mode):
            out(zone2_out)

    return logic, auto_mode, zone1_out, zone2_out


def _two_zone_choices():
    """Two zones coupled by a shared Mode tag with choices (stateful, not external)."""
    a = Bool("A", external=True)
    b = Bool("B", external=True)
    mode = Int("Mode", choices={0: "Off", 1: "Zone1", 2: "Zone2"}, external=False)
    set_mode = Bool("SetMode", external=True)
    x = Bool("X")
    y = Bool("Y")

    with Program(strict=False) as logic:
        with Rung(set_mode):
            copy(1, mode)
        with Rung(a, mode):
            out(x)
        with Rung(b, mode):
            out(y)

    return logic, mode


# ---------------------------------------------------------------------------
# split_at validation
# ---------------------------------------------------------------------------


class TestSplitAtValidation:
    def test_nonexistent_tag_raises(self):
        a = Bool("A", external=True)
        x = Bool("X")
        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
        with pytest.raises(ValueError, match="does not exist"):
            prove(logic, x, split_at=["NoSuchTag"])

    def test_external_input_raises(self):
        a = Bool("A", external=True)
        x = Bool("X")
        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
        with pytest.raises(ValueError, match="external input"):
            prove(logic, x, split_at=["A"])

    def test_rise_fall_tag_raises(self):
        """A tag used in rise() cannot be split."""
        trigger = Bool("Trigger", external=True)
        flag = Bool("Flag")
        x = Bool("X")
        with Program(strict=False) as logic:
            with Rung(trigger):
                latch(flag)
            with Rung(rise(flag)):
                out(x)
        with pytest.raises(ValueError, match="rise.*fall"):
            prove(logic, x, split_at=["Flag"])

    def test_unbounded_domain_raises(self):
        a = Bool("A", external=True)
        counter = Int("Counter", external=False)
        x = Bool("X")
        with Program(strict=False) as logic:
            with Rung(a):
                copy(1, counter)
            with Rung(counter):
                out(x)
        with pytest.raises(ValueError, match="no small enumerable domain"):
            prove(logic, x, split_at=["Counter"])


# ---------------------------------------------------------------------------
# split_at on prove()
# ---------------------------------------------------------------------------


class TestSplitAtProve:
    def test_prove_with_split_at_verdict_matches_unfactored(self):
        logic, auto_mode, zone1_out, _zone2_out = _two_zone_with_shared_bool()

        result_normal = prove(logic, Or(~zone1_out, auto_mode))
        result_split = prove(logic, Or(~zone1_out, auto_mode), split_at=["AutoMode"])

        assert type(result_normal) is type(result_split)

    def test_proven_no_spurious_caveats(self):
        logic, auto_mode, zone1_out, _zone2_out = _two_zone_with_shared_bool()

        result = prove(logic, Or(~zone1_out, auto_mode), split_at=["AutoMode"])
        assert isinstance(result, Proven)
        assert not any("split_at" in c for c in result.caveats)

    def test_counterexample_has_split_caveat(self):
        a = Bool("A", external=True)
        mode = Bool("Mode")
        x = Bool("X")
        with Program(strict=False) as logic:
            with Rung(a):
                latch(mode)
            with Rung(mode):
                out(x)

        result = prove(logic, ~x, split_at=["Mode"])
        assert isinstance(result, Counterexample)
        assert any("split_at" in c for c in result.caveats)

    def test_split_at_promotes_to_nondeterministic(self):
        logic, _auto, _z1, _z2 = _two_zone_with_shared_bool()

        ctx = _build_explore_context(logic, split_at=["AutoMode"])
        assert not isinstance(ctx, Intractable)
        assert "AutoMode" in ctx.nondeterministic_dims
        assert "AutoMode" not in ctx.stateful_dims
        assert "AutoMode" in ctx.free_input_names

    def test_split_at_enables_factoring(self):
        logic, _auto, _z1, _z2 = _two_zone_with_shared_bool()

        ctx_normal = _build_explore_context(logic)
        assert not isinstance(ctx_normal, Intractable)
        assert ctx_normal.free_input_factoring is None

        ctx_split = _build_explore_context(logic, split_at=["AutoMode"])
        assert not isinstance(ctx_split, Intractable)
        assert ctx_split.free_input_factoring is not None
        assert len(ctx_split.free_input_factoring.groups) >= 2
        assert "AutoMode" in ctx_split.free_input_factoring.shared_inputs


# ---------------------------------------------------------------------------
# split_at on reachable_states()
# ---------------------------------------------------------------------------


class TestSplitAtReachableStates:
    def test_split_at_superset_of_normal(self):
        logic, _auto, _z1, _z2 = _two_zone_with_shared_bool()

        normal = reachable_states(logic)
        assert not isinstance(normal, Intractable)

        split = reachable_states(logic, split_at=["AutoMode"])
        assert not isinstance(split, Intractable)

        assert normal <= split

    def test_choices_split_explores_all_keys(self):
        logic, mode = _two_zone_choices()

        ctx = _build_explore_context(logic, split_at=["Mode"])
        assert not isinstance(ctx, Intractable)
        assert "Mode" in ctx.nondeterministic_dims
        assert set(ctx.nondeterministic_dims["Mode"]) == {0, 1, 2}


# ---------------------------------------------------------------------------
# split_at on explore()
# ---------------------------------------------------------------------------


class TestSplitAtExplore:
    def test_explore_with_split_at_returns_graph(self):
        logic, _auto, _z1, _z2 = _two_zone_with_shared_bool()

        result = explore(logic, split_at=["AutoMode"])
        assert not isinstance(result, Intractable)
        assert result.state_count > 0


# ---------------------------------------------------------------------------
# Bridge tag detection (split_at hints)
# ---------------------------------------------------------------------------


class TestBridgeTagDetection:
    def test_find_bridge_tags_detects_automode(self):
        """_find_bridge_tags identifies AutoMode as a bridge in the two-zone program."""
        logic, _auto, _z1, _z2 = _two_zone_with_shared_bool()

        ctx = _build_explore_context(logic)
        assert not isinstance(ctx, Intractable)
        assert ctx.free_input_factoring is None

        bridges = _find_bridge_tags(
            ctx.graph,
            ctx.stateful_dims,
            ctx.nondeterministic_dims,
            ctx.exclusive_input_groups,
            ctx.free_input_names,
            ctx.nondeterministic_names,
        )
        assert len(bridges) >= 1
        bridge_names = [name for name, _count in bridges]
        assert "AutoMode" in bridge_names
        automode_count = next(count for name, count in bridges if name == "AutoMode")
        assert automode_count >= 2

    def test_no_bridges_when_already_independent(self):
        """No bridge suggestions when inputs are already independent."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)

        ctx = _build_explore_context(logic)
        assert not isinstance(ctx, Intractable)

        bridges = _find_bridge_tags(
            ctx.graph,
            ctx.stateful_dims,
            ctx.nondeterministic_dims,
            ctx.exclusive_input_groups,
            ctx.free_input_names,
            ctx.nondeterministic_names,
        )
        assert bridges == []

    def test_intractable_hints_contain_split_at_suggestion(self):
        """Intractable from max_states includes a split_at hint for the bridge tag.

        The program creates a transitive dependency: each E_i writes O_i,
        O_i latches AutoMode, and AutoMode gates the next zone's rung.
        This makes all free inputs dependent through AutoMode (one group,
        factoring=None), so _build_intractable_hints runs _find_bridge_tags.
        """
        auto_mode = Bool("AutoMode")
        set_auto = Bool("SetAuto", external=True)
        enables = [Bool(f"E{i}", external=True) for i in range(4)]
        outputs = [Bool(f"O{i}") for i in range(4)]

        with Program(strict=False) as logic:
            with Rung(set_auto):
                latch(auto_mode)
            for i in range(4):
                with Rung(enables[i], auto_mode):
                    latch(outputs[i])
                with Rung(outputs[i]):
                    latch(auto_mode)

        project = ["AutoMode"] + [f"O{i}" for i in range(4)]
        result = reachable_states(logic, project=project, max_states=5)
        assert isinstance(result, Intractable)
        assert any("split_at" in h for h in result.hints)
