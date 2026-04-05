"""Property-based round-trip test for named_array stride handling in codegen."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.hypothesis

from hypothesis import given, settings
from hypothesis import strategies as st

from pyrung.click import TagMap, ds, ladder_to_pyrung, pyrung_to_ladder, x
from pyrung.core import Bool, Program, Rung, TagType
from pyrung.core.program import copy
from pyrung.core.structure import _FieldSpec, _NamedArrayRuntime


def _field_name(i: int) -> str:
    """Generate a field name like f0, f1, ..."""
    return f"f{i}"


@st.composite
def named_array_params(draw):
    """Generate valid (count, field_count, stride) triples."""
    count = draw(st.integers(min_value=1, max_value=20))
    field_count = draw(st.integers(min_value=1, max_value=20))
    stride = draw(st.integers(min_value=field_count, max_value=field_count + 5))
    return count, field_count, stride


@given(params=named_array_params())
@settings(max_examples=200, deadline=None)
def test_named_array_stride_round_trip(params, tmp_path_factory):
    """Any (count, stride, field_count) triple should round-trip through codegen.

    Asserts:
    1. The emitted ``stride=`` kwarg matches the original stride.
    2. The emitted ``map_to`` range has exactly ``count * stride`` addresses.
    3. The generated code executes without error and ``map_to`` succeeds.
    """
    count, field_count, stride = params

    # --- Build named_array runtime dynamically ---
    field_specs = tuple(
        _FieldSpec(name=_field_name(i), type=TagType.INT, default=0, retentive=True)
        for i in range(field_count)
    )
    runtime = _NamedArrayRuntime(
        name="Arr",
        type=TagType.INT,
        count=count,
        stride=stride,
        field_specs=field_specs,
    )

    # --- Build a minimal program that references two array tags ---
    Enable = Bool("Enable")
    # Use first field of first instance and last field of last instance
    # to guarantee we always reference two distinct tags from the array.
    src_tag = runtime._blocks[_field_name(0)][1]
    dst_tag = runtime._blocks[_field_name(field_count - 1)][count]

    with Program(strict=False) as logic:
        with Rung(Enable):
            copy(src_tag, dst_tag)

    # --- Map to hardware ---
    hw_base = 100
    hw_total = count * stride
    mapping = TagMap(
        [
            Enable.map_to(x[1]),
            *runtime.map_to(ds.select(hw_base, hw_base + hw_total - 1)),
        ],
        include_system=False,
    )

    # --- Export to CSV ---
    tmp_path = tmp_path_factory.mktemp("stride")
    bundle = pyrung_to_ladder(logic, mapping)
    csv_dir = tmp_path / "csv_out"
    bundle.write(csv_dir)

    nick_csv = tmp_path / "nicknames.csv"
    mapping.to_nickname_file(nick_csv)

    # --- Codegen round-trip ---
    code = ladder_to_pyrung(csv_dir / "main.csv", nickname_csv=nick_csv)

    # Assertion 1: stride kwarg matches original
    if stride > 1:
        assert f"stride={stride}" in code, (
            f"Expected stride={stride} in generated code for "
            f"count={count}, fields={field_count}, stride={stride}.\n"
            f"Generated code:\n{code}"
        )

    # Assertion 2: map_to range has exactly count * stride addresses
    select_match = re.search(r"Arr\.map_to\(ds\.select\((\d+),\s*(\d+)\)\)", code)
    assert select_match is not None, (
        f"Expected Arr.map_to(ds.select(...)) in generated code.\nGenerated code:\n{code}"
    )
    sel_start, sel_end = int(select_match.group(1)), int(select_match.group(2))
    actual_span = sel_end - sel_start + 1
    assert actual_span == hw_total, (
        f"Expected map_to span of {hw_total} addresses "
        f"(count={count} * stride={stride}), got {actual_span}.\n"
        f"Generated code:\n{code}"
    )

    # Assertion 3: generated code executes and map_to succeeds
    ns: dict = {}
    exec(code, ns)
    assert "logic" in ns, "Generated code must define 'logic'"
    assert "mapping" in ns, "Generated code must define 'mapping'"
