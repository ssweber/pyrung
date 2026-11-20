"""Each family declaration in pyrung.click.validation.families agrees with the classes it names."""

from __future__ import annotations

import dataclasses

import laddercodec
import pytest

from pyrung.click.validation import legacy
from pyrung.click.validation.families import FAMILIES, family_of
from pyrung.click.validation.findings import __all__ as finding_names
from tests.core.analysis.test_crossings_coverage import _concrete_instruction_classes

_CLASSES = {cls.__name__: cls for cls in _concrete_instruction_classes()}


def _family_classes():
    return [
        pytest.param(family, name, id=f"{family.name}-{name}")
        for family in FAMILIES
        for name in family.classes
    ]


def _declared_attributes(cls):
    """Return each attribute that holds an operand or a condition of the call of *cls*."""
    return {
        *(
            one
            for _name, attribute in cls._call_operands or ()
            for one in ((attribute,) if isinstance(attribute, str) else attribute)
        ),
        *(attribute for _name, attribute in cls._call_keyword_operands),
        *(attribute for _name, attribute in cls._call_chained),
    }


@pytest.mark.parametrize("family,name", _family_classes())
def test_a_family_names_each_operand_of_the_call(family, name):
    # The table checks in tests/core/test_instruction_tables.py read the attributes of
    # ``classes``. A family that overrides ``operands`` has its own tests.
    if family.operands is not None:
        pytest.skip("the family overrides operands")
    cls = _CLASSES[name]
    assert cls._call_operands is not None, f"{name} declares no _call_operands"
    assert set(family.classes[name]) == _declared_attributes(cls)


def _call_names(cls):
    """Return the name of each keyword argument and chained call of *cls*."""
    return {
        *cls._call_keywords,
        *(name for name, _attribute in cls._call_keyword_operands),
        *(name for name, _attribute in cls._call_chained),
    }


@pytest.mark.parametrize("family", FAMILIES, ids=lambda family: family.name)
def test_each_argument_of_a_family_is_in_the_call(family):
    if family.call_text is not None:
        pytest.skip("the family overrides call_text")
    classes = [_CLASSES[name] for name in family.classes]
    keywords = {keyword for cls in classes for keyword in _call_names(cls)}
    positional = min(len(cls._call_operands or ()) for cls in classes)
    for field, argument in family.arguments.items():
        if isinstance(argument, int):
            assert 0 <= argument < positional, field
        else:
            assert argument in keywords, field


@pytest.mark.parametrize("family", FAMILIES, ids=lambda family: family.name)
def test_a_replaced_code_is_gone(family):
    assert not set(family.replaced_codes) & (set(finding_names) | set(legacy.CODES))


def test_each_class_is_in_one_family():
    for family in FAMILIES:
        for name in family.classes:
            instruction = _CLASSES[name].__new__(_CLASSES[name])
            assert family_of(instruction) is family


@pytest.mark.parametrize("family", FAMILIES, ids=lambda family: family.name)
def test_each_laddercodec_field_of_a_family_has_an_argument(family):
    # A field that laddercodec renames or adds would lose its caret.
    classes = {field.partition(".")[0] for field in family.arguments}
    fields = {
        f"{name}.{field.name}"
        for name in classes
        for field in dataclasses.fields(getattr(laddercodec, name))
        # The verb is the kind.
        if field.name != "kind"
    }
    assert set(family.arguments) == fields


def test_each_check_has_its_own_place_in_the_order():
    # Two checks with one order number would run in the order that the modules load.
    from pyrung.click.validation import _CHECKS

    places = [(check.phase, check.order) for check in (*_CHECKS, *legacy.CHECKS)]
    assert len(places) == len(set(places))


def test_no_class_is_in_a_family_and_in_legacy():
    in_families = {name for family in FAMILIES for name in family.classes}
    for name, rows in legacy.TABLES.items():
        keys = {key[0] if isinstance(key, tuple) else key for key in rows}
        assert not keys & in_families, name


def test_laddercodec_names_each_tag_of_a_keyword_and_a_chained_call():
    # laddercodec quotes an address by the name that the call spells for it, so each
    # operand of the call needs an entry, wherever the call holds it.
    from pyrung.click import ClickBlocks, TagMap
    from pyrung.click.validation.fields import _call_names, declared_operands
    from pyrung.click.validation.resolve import _ResolutionCache
    from pyrung.core import Bool, Int
    from pyrung.core.instruction import SearchInstruction, ShiftInstruction

    k = ClickBlocks()
    clock, reset, result = Bool("Clk"), Bool("Rst"), Int("Res")
    resolver = _ResolutionCache(
        TagMap([clock.map_to(k.x[2]), reset.map_to(k.x[3]), result.map_to(k.ds[20])])
    )
    declared = type(
        "ShiftInstruction",
        (ShiftInstruction,),
        {
            "_call_operands": (("bit_range", "bit_range"),),
            "_call_chained": (("clock", "clock_condition"), ("reset", "reset_condition")),
        },
    )
    shift = declared(k.c.select(1, 2), None, clock, ~reset)
    assert _call_names(declared_operands(shift, {}), {}, resolver) == {
        "C1": "C1",
        "C2": "C2",
        "X002": "Clk",
        "X003": "Rst",
    }
    search = SearchInstruction(k.ds.select(1, 2) == 5, result=result, found=k.c[1])
    assert _call_names(declared_operands(search, {}), {}, resolver) == {
        "DS1": "DS1",
        "DS2": "DS2",
        "DS20": "Res",
        "C1": "C1",
    }


def _tables_read_by_shared_checks() -> set[str]:
    """Return the name of each legacy table that a module of pyrung.click.validation reads."""
    import ast
    from pathlib import Path

    import pyrung.click.validation as validation

    found: set[str] = set()
    for path in Path(validation.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            function = node.func
            # Both table("X") and legacy.table("X") read a table.
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", None)
            )
            first = node.args[0]
            if name in {"_family_rows", "table"} and isinstance(first, ast.Constant):
                found.add(first.value)
    return found


def _tables_defined_by_legacy_modules() -> dict[str, list[str]]:
    """Return each table that a module in legacy/ defines, with the modules that define it."""
    from importlib import import_module
    from pkgutil import iter_modules

    defined: dict[str, list[str]] = {}
    for info in iter_modules(legacy.__path__):
        module = import_module(f"{legacy.__name__}.{info.name}")
        for table in module.TABLES:
            defined.setdefault(table, []).append(info.name)
    return defined


# A shared check whose table no module in legacy/ defines any more. The orchestrator
# removes it with the bank tables.
_WAITING_FOR_THE_BANK_TABLES = {"ROLE_ASSIGNMENT_FIELDS"}


def test_a_table_in_legacy_is_read_by_a_shared_check():
    # Deleting a legacy module drops the rows that it fed to a shared check, and the
    # check goes quiet. A table that no check reads is a table that no one deleted.
    unread = set(_tables_defined_by_legacy_modules()) - _tables_read_by_shared_checks()
    assert not unread, f"{sorted(unread)} is defined in legacy/ and no shared check reads it"


def test_a_shared_check_reads_only_tables_that_legacy_defines():
    # A check that reads a table from a module that is gone never reports. Delete the
    # check, or move its rows to the family that took the instruction over.
    orphaned = (
        _tables_read_by_shared_checks()
        - set(_tables_defined_by_legacy_modules())
        - _WAITING_FOR_THE_BANK_TABLES
    )
    assert not orphaned, f"a shared check reads {sorted(orphaned)}, which no module defines"


def test_the_waiting_tables_are_still_read():
    # Remove an entry from _WAITING_FOR_THE_BANK_TABLES when its check goes.
    assert _WAITING_FOR_THE_BANK_TABLES <= _tables_read_by_shared_checks()
    assert not _WAITING_FOR_THE_BANK_TABLES & set(_tables_defined_by_legacy_modules())
