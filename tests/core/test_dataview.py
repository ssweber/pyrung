"""Tests for the DataView chainable query API."""

from __future__ import annotations

import importlib
import sys

import pytest

from pyrung.core import (
    Bool,
    InputBlock,
    OutputBlock,
    Program,
    Rung,
    TagType,
    build_program_graph,
    latch,
    out,
)
from pyrung.core.analysis import TagRole
from pyrung.core.analysis.dataview import DataView, TagNameMatcher

# ------------------------------------------------------------------
# TagNameMatcher unit tests
# ------------------------------------------------------------------


class TestWordSplitting:
    def test_camel_case(self) -> None:
        assert TagNameMatcher._split_words("ConveyorMotor") == ["Conveyor", "Motor"]

    def test_underscore_separator(self) -> None:
        assert TagNameMatcher._split_words("Start_Button") == ["Start", "Button"]

    def test_single_char_words_dropped(self) -> None:
        # "A" is only 1 char, dropped by len > 1 filter
        assert TagNameMatcher._split_words("Bin_A_Sensor") == ["Bin", "Sensor"]

    def test_all_lowercase(self) -> None:
        assert TagNameMatcher._split_words("parsevalue") == ["parsevalue"]

    def test_mixed(self) -> None:
        assert TagNameMatcher._split_words("parseDataValue") == ["parse", "Data", "Value"]


class TestAbbreviations:
    def test_consonants_conveyor(self) -> None:
        assert TagNameMatcher._consonants_abbr("conveyor") == "cnvyr"

    def test_consonants_command(self) -> None:
        assert TagNameMatcher._consonants_abbr("command") == "cmnd"

    def test_consonants_control(self) -> None:
        assert TagNameMatcher._consonants_abbr("control") == "cntrl"

    def test_reduced_conveyor(self) -> None:
        # n dropped (first consonant after vowel 'o', next 'v' is consonant)
        assert TagNameMatcher._reduced_consonants_abbr("conveyor") == "cvyr"

    def test_reduced_control(self) -> None:
        assert TagNameMatcher._reduced_consonants_abbr("control") == "ctrl"

    def test_reduced_command(self) -> None:
        assert TagNameMatcher._reduced_consonants_abbr("command") == "cmd"

    def test_special_case_same_letter(self) -> None:
        assert TagNameMatcher._special_case("YYYY") == "YYYY"

    def test_special_case_short(self) -> None:
        assert TagNameMatcher._special_case("AB") == "ab"

    def test_special_case_all_consonants(self) -> None:
        assert TagNameMatcher._special_case("XML") == "xml"

    def test_special_case_normal_returns_none(self) -> None:
        assert TagNameMatcher._special_case("Alarm") is None

    def test_abbreviations_pipeline(self) -> None:
        abbrs = TagNameMatcher._abbreviations("conveyor")
        assert "cnvyr" in abbrs
        assert "cvyr" in abbrs


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture
def conveyor_graph(monkeypatch):
    module_name = "examples.click_conveyor"
    monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
    sys.modules.pop(module_name, None)
    module = importlib.import_module(module_name)
    return build_program_graph(module.logic)


@pytest.fixture
def dv(conveyor_graph):
    return DataView.from_graph(conveyor_graph)


# ------------------------------------------------------------------
# Role filter tests
# ------------------------------------------------------------------


class TestRoleFilters:
    def test_inputs_contains_buttons(self, dv) -> None:
        inputs = dv.inputs()
        assert "StartBtn" in inputs
        assert "StopBtn" in inputs
        assert "EntrySensor" in inputs

    def test_inputs_excludes_non_inputs(self, dv) -> None:
        inputs = dv.inputs()
        assert "Running" not in inputs
        assert "ConveyorMotor" not in inputs

    def test_pivots_includes_running(self, dv) -> None:
        pivots = dv.pivots()
        assert "Running" in pivots

    def test_pivots_includes_state(self, dv) -> None:
        pivots = dv.pivots()
        assert "State" in pivots

    def test_terminals_includes_outputs(self, dv) -> None:
        terminals = dv.terminals()
        assert "ConveyorMotor" in terminals
        assert "StatusLight" in terminals

    def test_mutually_exclusive_roles(self, dv) -> None:
        assert len(dv.pivots().terminals()) == 0


# ------------------------------------------------------------------
# Physicality filter tests
# ------------------------------------------------------------------


class TestPhysicalityFilters:
    def test_physical_io_with_blocks(self) -> None:
        x = InputBlock("X", TagType.BOOL, 1, 2)
        y = OutputBlock("Y", TagType.BOOL, 1, 1)
        relay = Bool("Relay")

        with Program() as prog:
            with Rung(x[1]):
                latch(relay)
            with Rung(relay):
                out(y[1])

        dv = DataView.from_graph(build_program_graph(prog))
        assert "X1" in dv.physical_inputs()
        assert "Y1" in dv.physical_outputs()
        assert "X1" not in dv.internal()
        assert "Y1" not in dv.internal()
        assert "Relay" in dv.internal()

    def test_standalone_tags_not_physical(self, dv) -> None:
        # click_conveyor uses standalone Bool tags, not InputBlock/OutputBlock
        assert len(dv.physical_inputs()) == 0
        assert len(dv.physical_outputs()) == 0
        assert len(dv.internal()) == len(dv)


# ------------------------------------------------------------------
# Name matching tests
# ------------------------------------------------------------------


class TestContains:
    def test_contains_btn_abbreviation(self, dv) -> None:
        result = dv.contains("btn")
        assert "StartBtn" in result
        assert "StopBtn" in result
        assert "DiverterBtn" in result

    def test_contains_motor(self, dv) -> None:
        result = dv.contains("motor")
        assert "ConveyorMotor" in result

    def test_contains_sensor(self, dv) -> None:
        result = dv.contains("sensor")
        assert "EntrySensor" in result
        assert "BinASensor" in result
        assert "BinBSensor" in result

    def test_case_insensitive(self, dv) -> None:
        upper = set(dv.contains("SENSOR"))
        lower = set(dv.contains("sensor"))
        assert upper == lower

    def test_contains_with_role_chain(self, dv) -> None:
        result = dv.inputs().contains("btn")
        assert "StartBtn" in result
        assert "StopBtn" in result
        assert "ConveyorMotor" not in result

    def test_empty_needle_returns_all(self, dv) -> None:
        assert len(dv.contains("")) == len(dv)

    def test_single_char_needle_filters(self) -> None:
        x = InputBlock("X", TagType.BOOL, 1, 1)
        y = OutputBlock("Y", TagType.BOOL, 1, 1)
        relay = Bool("Relay")

        with Program() as prog:
            with Rung(x[1]):
                latch(relay)
            with Rung(relay):
                out(y[1])

        dv = DataView.from_graph(build_program_graph(prog))
        assert set(dv.contains("x")) == {"X1"}
        assert set(dv.contains("r")) == {"Relay"}
        assert set(dv.contains("x")) != set(dv)


# ------------------------------------------------------------------
# Slicing tests
# ------------------------------------------------------------------


class TestSlicing:
    def test_upstream_of_conveyor_motor(self, conveyor_graph) -> None:
        upstream = conveyor_graph.upstream_slice("ConveyorMotor")
        assert "Running" in upstream
        assert "EstopOK" in upstream
        assert "StartBtn" in upstream
        assert "ConveyorMotor" not in upstream  # self excluded

    def test_downstream_of_start_btn(self, conveyor_graph) -> None:
        downstream = conveyor_graph.downstream_slice("StartBtn")
        assert "Running" in downstream
        assert "ConveyorMotor" in downstream
        assert "StatusLight" in downstream

    def test_upstream_view_intersects(self, dv) -> None:
        result = dv.inputs().upstream("ConveyorMotor")
        assert "StartBtn" in result
        assert "Running" not in result  # PIVOT, filtered by .inputs()

    def test_nonexistent_tag_gives_empty(self, conveyor_graph) -> None:
        assert conveyor_graph.upstream_slice("NoSuchTag") == frozenset()
        assert conveyor_graph.downstream_slice("NoSuchTag") == frozenset()


# ------------------------------------------------------------------
# Chaining + iteration tests
# ------------------------------------------------------------------


class TestIteration:
    def test_iter_sorted(self, dv) -> None:
        tag_list = list(dv)
        assert tag_list == sorted(tag_list)

    def test_len_positive(self, dv) -> None:
        assert len(dv) > 0

    def test_contains_membership(self, dv) -> None:
        assert "StartBtn" in dv
        assert "NoSuchTag" not in dv

    def test_bool(self, dv) -> None:
        assert dv
        assert not dv.contains("zzz_impossible_needle_xyz")

    def test_roles_returns_correct_values(self, dv) -> None:
        roles = dv.inputs().roles()
        assert all(r == TagRole.INPUT for r in roles.values())

    def test_tags_property(self, dv) -> None:
        assert isinstance(dv.tags, frozenset)


# ------------------------------------------------------------------
# Graph edges tests
# ------------------------------------------------------------------


class TestGraphEdges:
    def test_returns_list_of_dicts(self, conveyor_graph) -> None:
        edges = conveyor_graph.graph_edges()
        assert isinstance(edges, list)
        assert all(isinstance(e, dict) for e in edges)

    def test_edge_keys(self, conveyor_graph) -> None:
        edges = conveyor_graph.graph_edges()
        for edge in edges:
            assert set(edge.keys()) == {"source", "target", "type"}

    def test_edge_types_valid(self, conveyor_graph) -> None:
        edges = conveyor_graph.graph_edges()
        valid_types = {"condition", "data", "write"}
        for edge in edges:
            assert edge["type"] in valid_types

    def test_condition_edges_point_tag_to_rung(self, conveyor_graph) -> None:
        edges = conveyor_graph.graph_edges()
        condition_edges = [e for e in edges if e["type"] == "condition"]
        assert len(condition_edges) > 0
        for edge in condition_edges:
            assert not edge["source"].startswith("rung:")
            assert edge["target"].startswith("rung:")

    def test_write_edges_point_rung_to_tag(self, conveyor_graph) -> None:
        edges = conveyor_graph.graph_edges()
        write_edges = [e for e in edges if e["type"] == "write"]
        assert len(write_edges) > 0
        for edge in write_edges:
            assert edge["source"].startswith("rung:")
            assert not edge["target"].startswith("rung:")

    def test_start_btn_has_condition_edge(self, conveyor_graph) -> None:
        edges = conveyor_graph.graph_edges()
        start_btn_edges = [e for e in edges if e["source"] == "StartBtn"]
        assert len(start_btn_edges) > 0
        assert all(e["type"] == "condition" for e in start_btn_edges)

    def test_conveyor_motor_has_write_edge(self, conveyor_graph) -> None:
        edges = conveyor_graph.graph_edges()
        motor_writes = [e for e in edges if e["target"] == "ConveyorMotor" and e["type"] == "write"]
        assert len(motor_writes) > 0

    def test_edges_consistent_with_rung_nodes(self, conveyor_graph) -> None:
        """Every edge endpoint references an existing tag or rung index."""
        edges = conveyor_graph.graph_edges()
        all_tags = set(conveyor_graph.tag_roles.keys())
        max_rung_idx = len(conveyor_graph.rung_nodes) - 1

        for edge in edges:
            for endpoint in (edge["source"], edge["target"]):
                if endpoint.startswith("rung:"):
                    idx = int(endpoint.split(":")[1])
                    assert 0 <= idx <= max_rung_idx, f"Invalid rung index {idx}"
                else:
                    assert endpoint in all_tags, f"Unknown tag {endpoint}"


# ------------------------------------------------------------------
# Serialization tests
# ------------------------------------------------------------------


class TestSerialization:
    def test_to_json_dict_structure(self, conveyor_graph) -> None:
        d = conveyor_graph.to_json_dict()
        assert isinstance(d["rungNodes"], list)
        assert isinstance(d["tagRoles"], dict)
        assert isinstance(d["tags"], list)
        assert isinstance(d["readersOf"], dict)
        assert isinstance(d["writersOf"], dict)
        assert isinstance(d["graphEdges"], list)

    def test_to_json_dict_role_values(self, conveyor_graph) -> None:
        d = conveyor_graph.to_json_dict()
        assert d["tagRoles"]["Running"] == "pivot"
        assert d["tagRoles"]["StartBtn"] == "input"

    def test_to_json_dict_sorted_lists(self, conveyor_graph) -> None:
        d = conveyor_graph.to_json_dict()
        for name, indices in d["readersOf"].items():
            assert indices == sorted(indices), f"readersOf[{name}] not sorted"

    def test_to_json_dict_no_frozensets(self, conveyor_graph) -> None:
        """Verify all collections are JSON-safe (list/dict/str/int/None)."""
        import json

        d = conveyor_graph.to_json_dict()
        # If this raises, something is not JSON-serializable
        json.dumps(d)


# ------------------------------------------------------------------
# Program.dataview() tests
# ------------------------------------------------------------------


class TestProgramDataview:
    def test_returns_dataview(self, monkeypatch) -> None:
        module_name = "examples.click_conveyor"
        monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)

        dv = module.logic.dataview()
        assert isinstance(dv, DataView)
        assert "StartBtn" in dv

    def test_caches_graph(self, monkeypatch) -> None:
        module_name = "examples.click_conveyor"
        monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)

        dv1 = module.logic.dataview()
        dv2 = module.logic.dataview()
        assert dv1._graph is dv2._graph

    def test_rebuilds_after_program_mutation(self) -> None:
        a = Bool("A")
        b = Bool("B")
        c = Bool("C")

        with Program(strict=False) as prog:
            with Rung(a):
                out(b)

            before = prog.dataview()

            with Rung(b):
                out(c)

            after = prog.dataview()

        assert "C" not in before
        assert "C" in after
        assert before._graph is not after._graph


# ------------------------------------------------------------------
# Integration smoke test
# ------------------------------------------------------------------


def test_conveyor_smoke(dv) -> None:
    """Smoke test: dataview on click_conveyor produces sensible results."""
    assert len(dv) > 10
    assert len(dv.inputs()) > 0
    assert len(dv.pivots()) > 0
    assert len(dv.terminals()) > 0
    # Every tag has a role
    assert len(dv.roles()) == len(dv)
