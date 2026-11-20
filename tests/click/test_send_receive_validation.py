"""CLICK validation of send() and receive(), which laddercodec's Send and Receive check.

These tests cover how a refusal prints: the call, the caret under the refused argument, and
the hint. Both calls take only keywords, so the caret marks a keyword's value, and a
refusal of the target marks the whole target.
"""

from __future__ import annotations

import pytest
from laddercodec import Code, CsvReadError, Receive, Send

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import (
    CLK_BANK_UNRESOLVED,
    validate_click_program,
)
from pyrung.core import Block, Bool, Dint, Int, TagType
from pyrung.core.instruction.send_receive import (
    ModbusAddress,
    ModbusRtuTarget,
    ModbusTcpTarget,
    RegisterType,
    receive,
    send,
)
from pyrung.core.program import Program, rung

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_TARGET = ModbusTcpTarget("plc2", "192.168.1.2")
_TARGET_TEXT = "ModbusTcpTarget('plc2', '192.168.1.2')"


def _program(write) -> Program:
    prog = Program(strict=False)
    with prog:
        with rung(x[1]):
            write()
    return prog


def _errors(write, mappings=()):
    tag_map = TagMap(list(mappings), include_system=False)
    return validate_click_program(_program(write), tag_map, mode="strict").errors


def _frame(finding):
    (frame,) = finding.display.frames
    return frame.lines, frame.caret


def _marked(finding) -> str:
    (frame,) = finding.display.frames
    _line, start, width = frame.caret
    return frame.lines[0][start : start + width]


def _send(**keywords):
    keywords = {"target": _TARGET, "remote_start": "DS1", "source": ds[5], **keywords}
    return lambda: send(**keywords)


def _receive(**keywords):
    keywords = {"target": _TARGET, "remote_start": "DS1", "dest": ds[5], **keywords}
    return lambda: receive(**keywords)


def test_a_refused_source_marks_the_source():
    (finding,) = _errors(_send(source=x[5], success=c[2]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    line = f"send(target={_TARGET_TEXT}, remote_start='DS1', source=X005, success=C2)"
    assert _frame(finding) == ((line,), (0, line.index("X005"), 4))
    assert finding.location == "main.rung[1].instruction[0](ModbusSendInstruction).instruction"


def test_a_refused_destination_marks_the_dest_keyword():
    (finding,) = _errors(_receive(dest=x[5], success=c[2]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    line = f"receive(target={_TARGET_TEXT}, remote_start='DS1', dest=X005, success=C2)"
    assert _frame(finding) == ((line,), (0, line.index("X005"), 4))


@pytest.mark.parametrize("write", [_send, _receive], ids=["send", "receive"])
@pytest.mark.parametrize("keyword", ["success", "error"])
def test_a_refused_status_bit_marks_its_keyword(write, keyword):
    (finding,) = _errors(write(**{keyword: y[3]}))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _marked(finding) == "Y003"
    assert finding.display.frames[0].lines[0].endswith(f"{keyword}=Y003)")


@pytest.mark.parametrize(
    "write, keyword", [(_send, "sending"), (_receive, "receiving")], ids=["send", "receive"]
)
def test_a_refused_busy_flag_marks_its_keyword(write, keyword):
    (finding,) = _errors(write(**{keyword: x[3]}))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert finding.display.frames[0].lines[0].endswith(f"{keyword}=X003)")
    assert _marked(finding) == "X003"


def test_a_refused_exception_response_marks_its_keyword():
    (finding,) = _errors(_send(exception_response=td[3]))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert _marked(finding) == "TD3"


def test_a_refused_target_marks_the_whole_target():
    bad = ModbusTcpTarget("plc2", "999.1.1.1")
    (finding,) = _errors(_send(target=bad))
    assert finding.code == Code.INVALID_MODBUS_TARGET
    assert _marked(finding) == "ModbusTcpTarget('plc2', '999.1.1.1')"


def test_a_target_shows_the_values_that_are_not_the_default():
    bad = ModbusRtuTarget("rtu", device_id=9, com_port="slot0_1")
    call = _send(target=bad, word_swap=True)
    # The call is shown in full, so the author sees the device id and the port.
    (finding,) = _errors(call)
    assert finding.code == Code.OPTION_NOT_OFFERED
    line = finding.display.frames[0].lines[0]
    assert line.startswith("send(target=ModbusRtuTarget('rtu', device_id=9, com_port='slot0_1')")
    assert _marked(finding) == "True"


def test_a_refused_remote_start_marks_the_remote_start():
    (finding,) = _errors(_send(remote_start="SD1"))
    assert finding.code == Code.NOT_WRITABLE
    assert _marked(finding) == "'SD1'"


def test_a_remote_address_is_shown_as_the_author_wrote_it():
    address = ModbusAddress("FFFEh", RegisterType.HOLDING)
    (finding,) = _errors(_send(remote_start=address, source=ds.select(1, 4)))
    assert finding.code == Code.COUNT_OUT_OF_RANGE
    line = finding.display.frames[0].lines[0]
    assert "remote_start=ModbusAddress('FFFEh', RegisterType.HOLDING)" in line
    assert _marked(finding) == "DS1..DS4"


def test_the_label_gives_the_address_and_the_hint_gives_clicks_error():
    source = Bool("Src")
    (finding,) = _errors(_send(source=source), [source.map_to(x[5])])
    label = finding.display.frames[0].caret_label
    assert "X005" in label
    assert "Src" not in label
    assert "OBJ-004" in finding.display.hint
    assert finding.message.isascii()


def test_a_word_swap_that_the_remote_start_cannot_take_marks_the_flag():
    (finding,) = _errors(_send(word_swap=True))
    assert finding.code == Code.OPTION_NOT_OFFERED
    assert finding.display.frames[0].lines[0].endswith("word_swap=True)")
    assert _marked(finding) == "True"


@pytest.mark.parametrize(
    "write",
    [
        _send(source=ds[5], sending=c[1], success=c[2], error=c[3], exception_response=ds[9]),
        _send(source=dd[5], remote_start="DD1", success=c[2]),
        _send(source=c[5], remote_start="C1", success=c[2]),
        _send(source=txt[5], remote_start="TXT1"),
        _send(source=ds.select(1, 4), remote_start="DS10"),
        _send(source=ds[5], target=ModbusRtuTarget("rtu", device_id=7)),
        _send(source=ds[5], remote_start=ModbusAddress(400001)),
        _receive(dest=ds[5], receiving=c[1], success=c[2], error=c[3], exception_response=dd[9]),
        _receive(dest=y[5], remote_start="C1"),
        _receive(dest=txt.select(1, 2), remote_start="TXT1"),
        _receive(dest=ds[5], target="plc2"),
    ],
    ids=[
        "send all flags",
        "send DD",
        "send C",
        "send TXT",
        "send range",
        "send RTU",
        "send MODBUS 984",
        "receive all flags",
        "receive Y",
        "receive TXT",
        "receive by name",
    ],
)
def test_a_call_that_click_takes_passes(write):
    assert _errors(write) == ()


@pytest.mark.parametrize(
    ("write", "verb"),
    [(_send(target="plc"), "send"), (_receive(target="plc"), "receive")],
    ids=["send", "receive"],
)
def test_the_export_refuses_a_target_that_is_only_a_name(write, verb):
    # Validation passes this call. The export refuses it, because CLICK needs the address.
    with pytest.raises(LadderExportError) as refused:
        pyrung_to_ladder(_program(write), TagMap())
    (issue,) = refused.value.issues
    assert issue["message"] == (
        f"target='plc' is only a name. A CLICK {verb}() needs the address of its target. "
        "Give the address with ModbusTcpTarget or ModbusRtuTarget."
    )
    assert issue["path"].endswith(".target")


def test_a_status_bit_left_off_needs_no_address():
    source = Int("Src")
    assert _errors(_send(source=source), [source.map_to(ds[5])]) == ()
    dest = Int("Dst")
    assert _errors(_receive(dest=dest), [dest.map_to(ds[6])]) == ()


@pytest.mark.parametrize("write", [_send, _receive], ids=["send", "receive"])
def test_each_unmapped_operand_names_its_argument(write):
    local, ok, busy, exception = Int("Local"), Bool("Ok"), Bool("Busy"), Int("Exc")
    keyword = "source" if write is _send else "dest"
    flag = "sending" if write is _send else "receiving"
    findings = _errors(
        write(**{keyword: local, flag: busy, "success": ok, "exception_response": exception})
    )
    assert [f.code for f in findings] == [CLK_BANK_UNRESOLVED] * 4
    word = "source" if write is _send else "destination"
    assert [f.message.split(" at ")[0] for f in findings] == [
        f"pyrung cannot find one CLICK address for the {word}",
        f"pyrung cannot find one CLICK address for the {flag} flag",
        "pyrung cannot find one CLICK address for the success flag",
        "pyrung cannot find one CLICK address for the exception response flag",
    ]


def test_an_unmapped_device_id_is_reported():
    device_id = Int("Slave")
    target = ModbusTcpTarget("plc2", "192.168.1.2", device_id=device_id)
    (finding,) = _errors(_send(target=target))
    assert finding.code == CLK_BANK_UNRESOLVED
    assert finding.message.startswith("pyrung cannot find one CLICK address for the device id")
    assert finding.location.endswith("instruction.device_id")


def test_a_device_id_outside_ds_marks_the_target():
    device_id = Int("Slave")
    target = ModbusTcpTarget("plc2", "192.168.1.2", device_id=device_id)
    (finding,) = _errors(_send(target=target), [device_id.map_to(td[3])])
    assert finding.code == Code.INVALID_MODBUS_TARGET
    line = finding.display.frames[0].lines[0]
    assert line.startswith("send(target=ModbusTcpTarget('plc2', '192.168.1.2', device_id=Slave)")
    assert _marked(finding).startswith("ModbusTcpTarget(")


def test_a_source_of_scattered_addresses_is_refused():
    gap = Block("Gap", TagType.INT, 1, 3)
    mappings = [gap[1].map_to(ds[1]), gap[2].map_to(ds[2]), gap[3].map_to(ds[5])]
    (finding,) = _errors(_send(source=gap.select(1, 3)), mappings)
    assert finding.code == "CLK_COPY_RANGE_NOT_CONSECUTIVE"
    (finding,) = _errors(_receive(dest=gap.select(1, 3)), mappings)
    assert finding.code == "CLK_COPY_RANGE_NOT_CONSECUTIVE"


def _gapped_ranges():
    gap = Block("Gap", TagType.INT, 1, 3)
    scattered = [gap[1].map_to(ds[1]), gap[2].map_to(ds[2]), gap[3].map_to(ds[5])]
    desc = Block("Desc", TagType.INT, 1, 3)
    descending = [desc[1].map_to(ds[13]), desc[2].map_to(ds[12]), desc[3].map_to(ds[11])]
    return {
        "scattered": (gap.select(1, 3), scattered),
        "reversed": (ds.select(1, 3).reverse(), []),
        "mapped-descending": (desc.select(1, 3), descending),
    }


@pytest.mark.parametrize("kind", ["scattered", "reversed", "mapped-descending"])
@pytest.mark.parametrize("call", [_send, _receive], ids=["send", "receive"])
def test_the_export_refuses_a_gapped_range_even_under_allow(call, kind):
    block_range, mappings = _gapped_ranges()[kind]
    keyword = "source" if call is _send else "dest"
    tag_map = TagMap(mappings, include_system=False)
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE"):
        pyrung_to_ladder(
            _program(call(**{keyword: block_range})),
            tag_map,
            allow=("CLK_COPY_RANGE_NOT_CONSECUTIVE",),
        )


def test_the_export_refuses_a_send_range_that_skips_addresses_under_allow():
    with pytest.raises(LadderExportError, match="CLK_COPY_RANGE_NOT_CONSECUTIVE"):
        pyrung_to_ladder(
            _program(_send(source=y.select(15, 21))),
            TagMap(),
            allow=("CLK_COPY_RANGE_NOT_CONSECUTIVE",),
        )


def test_a_range_that_is_too_long_marks_the_range():
    (finding,) = _errors(_send(source=ds.select(1, 128)))
    assert finding.code == Code.COUNT_OUT_OF_RANGE
    assert _marked(finding) == "DS1..DS128"


def test_the_export_refuses_a_call_that_click_refuses():
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(_send(source=x[5])), TagMap())
    with pytest.raises(LadderExportError, match=Code.BANK_NOT_ALLOWED):
        pyrung_to_ladder(_program(_receive(dest=x[5])), TagMap())


def test_allow_names_the_code_that_laddercodec_reports():
    write = _send(source=x[5])
    pyrung_to_ladder(_program(write), TagMap(), allow=(Code.BANK_NOT_ALLOWED,))


def test_allow_with_the_old_bank_code_names_what_replaced_it():
    with pytest.raises(ValueError, match="no longer reports CLK_BANK_WRONG_ROLE. Pass bank-not"):
        pyrung_to_ladder(_program(_send(source=x[5])), TagMap(), allow=("CLK_BANK_WRONG_ROLE",))


def test_a_local_tag_at_the_remote_address_does_not_name_it():
    source, exception = Dint("Src"), Int("Exc")
    findings = _errors(
        _send(source=source, exception_response=exception),
        [source.map_to(dd[5]), exception.map_to(ds[1])],
    )
    remote = [
        f for f in findings if f.code == Code.BANK_NOT_ALLOWED and "remote_start" in f.message
    ]
    assert remote
    for finding in remote:
        assert "Exc (DS1)" not in finding.message
        assert "Exc" not in finding.display.frames[0].caret_label


@pytest.mark.parametrize(
    "write, keyword, hint",
    [(_send, "source", "Pick a slave address in DD or CTD"), (_receive, "dest", "takes DD, CTD")],
)
def test_a_pairing_refusal_names_the_banks_that_pair(write, keyword, hint):
    (finding,) = _errors(write(**{keyword: dd[5]}))
    assert hint in finding.display.hint
    assert "not confirmed" not in finding.display.hint


@pytest.mark.parametrize("write", [_send, _receive], ids=["send", "receive"])
@pytest.mark.parametrize("keyword", ["success", "error"])
@pytest.mark.parametrize("bank", [x, y, sc], ids=["X", "Y", "SC"])
def test_a_status_bit_outside_c_is_refused(write, keyword, bank):
    (finding,) = _errors(write(**{keyword: bank[3]}))
    assert finding.code == Code.BANK_NOT_ALLOWED
    assert finding.display.frames[0].lines[0].endswith(f"{keyword}={_marked(finding)})")


@pytest.mark.parametrize("write", [_send, _receive], ids=["send", "receive"])
@pytest.mark.parametrize("bank", [x, y, sc], ids=["X", "Y", "SC"])
def test_a_busy_flag_outside_c_is_refused(write, bank):
    keyword = "sending" if write is _send else "receiving"
    (finding,) = _errors(write(**{keyword: bank[3]}))
    assert finding.code == Code.BANK_NOT_ALLOWED


@pytest.mark.parametrize("write", [_send, _receive], ids=["send", "receive"])
@pytest.mark.parametrize("bank", [td, sd], ids=["TD", "SD"])
def test_an_exception_response_outside_ds_and_dd_is_refused(write, bank):
    (finding,) = _errors(write(exception_response=bank[3]))
    assert finding.code == Code.BANK_NOT_ALLOWED


@pytest.mark.parametrize("bank", [x, sc], ids=["X", "SC"])
def test_a_receive_destination_in_a_read_only_bank_is_refused(bank):
    (finding,) = _errors(_receive(dest=bank[5]))
    assert finding.code == Code.BANK_NOT_ALLOWED


@pytest.mark.parametrize("write", [_send, _receive], ids=["send", "receive"])
def test_a_token_that_laddercodec_cannot_read_is_a_finding(write, monkeypatch):
    def refuse(token):
        raise CsvReadError(f"Could not read {token!r} as written.")

    monkeypatch.setattr(Send, "from_csv_token", refuse)
    monkeypatch.setattr(Receive, "from_csv_token", refuse)
    (finding,) = _errors(write())
    assert finding.code == str(Code.INVALID_TOKEN)
    assert "CLICK cannot read" in finding.message
    assert "Could not read" in finding.message
    assert finding.message.isascii()


@pytest.mark.parametrize("start", [2, 4, 1000])
def test_a_send_of_text_from_an_even_address_is_refused(start):
    # CLICK starts a Send of TXT at an odd address (OBJ-028).
    write = _send(remote_start="TXT1", source=txt[start])
    (finding,) = _errors(write)
    assert finding.code == Code.TXT_START_NOT_ODD
    with pytest.raises(LadderExportError, match="txt-start-not-odd"):
        pyrung_to_ladder(_program(write), TagMap(include_system=False))


@pytest.mark.parametrize("start", [1, 3, 999])
def test_a_send_of_text_from_an_odd_address_passes(start):
    assert not _errors(_send(remote_start="TXT1", source=txt[start]))
