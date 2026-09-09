"""Hardware channel inputs survive scalar and structured code generation."""

from dataclasses import replace

import pytest
from pyclickplc.addresses import get_addr_key
from pyclickplc.nicknames import read_csv, write_csv
from test_codegen_project import _exec_project

from pyrung import Bool, Program, Real, Rung, copy, out
from pyrung.click import (
    ClickBlocks,
    TagMap,
    ladder_to_pyrung,
    ladder_to_pyrung_project,
    pyrung_to_ladder,
)
from pyrung.core.validation.context import ValidationContext


@pytest.mark.parametrize("multi_file", [False, True])
@pytest.mark.parametrize("ownership", ["raw", "scalar", "block", "udt"])
def test_channel_input_reaches_samples_without_marking_output_external(
    tmp_path, multi_file, ownership
):
    blocks = ClickBlocks()
    source, output, sample = Real("LaserMeasurement"), Real("LaserOutput"), Real("DelayedLaser")
    enable, result = Bool("Sample"), Bool("Result")
    with Program() as program:
        with Rung(enable):
            copy(source, sample)
            copy(sample, output)
        with Rung(sample >= 95):
            out(result)
    mapping = TagMap(
        {
            source: blocks.df[1],
            output: blocks.df[2],
            sample: blocks.df[10],
            enable: blocks.x[1],
            result: blocks.y[1],
        },
        include_system=False,
    )
    bundle = pyrung_to_ladder(program, mapping)
    nickname_csv = tmp_path / "nicknames.csv"
    mapping.to_nickname_file(nickname_csv)
    if ownership in {"block", "udt"}:
        records = read_csv(nickname_csv)
        for index, comment in [(1, "<Channels:block>"), (2, "</Channels:block>")]:
            key = get_addr_key("DF", index)
            if ownership == "udt":
                comment = comment.replace("Channels:block", "Channels.value:udt")
                records[key] = replace(
                    records[key], nickname=f"Channels{index}_value", comment=comment
                )
            else:
                records[key] = replace(records[key], comment=comment)
        write_csv(nickname_csv, records)
    kwargs = {"analog_inputs": ["df001", "DF3"]}
    if ownership != "raw":
        kwargs["nickname_csv"] = nickname_csv
    if multi_file:
        namespace = _exec_project(ladder_to_pyrung_project(bundle, **kwargs), tmp_path)
    else:
        namespace = {}
        code = ladder_to_pyrung(bundle, **kwargs)
        code_path = tmp_path / "generated.py"
        code_path.write_text(code, encoding="utf-8")
        exec(compile(code, str(code_path), "exec"), namespace)
    generated = namespace["logic"]
    graph = ValidationContext(generated).graph
    source_name, output_name, sample_name = (
        ("DF1", "DF2", "DF10")
        if ownership == "raw"
        else ("LaserMeasurement", "LaserOutput", "DelayedLaser")
    )
    if ownership == "udt":
        source_name, output_name = "Channels1_value", "Channels2_value"
    assert graph.tags[source_name].external
    assert not graph.tags[output_name].external
    assert not graph.tags[sample_name].external
    assert not generated.check(select={"CMP_ALWAYS_FALSE"})
    if not multi_file:
        # Also preserve channels absent from the ladder (e.g. indirect reads).
        assert namespace["df"][3].external
