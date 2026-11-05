"""Char against Char comparisons export to CLICK ladder and import back."""

from __future__ import annotations

import pytest

from pyrung.click import ClickBlocks, TagMap, pyrung_to_ladder, validate_click_program
from pyrung.click.ladder import LadderExportError
from pyrung.click.validation import CLK_TEXT_ORDER_COMPARE, CLK_TEXT_WINDOW_NOT_CONTIGUOUS
from pyrung.core import Bool, Char, Program, rung
from pyrung.core.program import out

x, y, c, t, ct, sc, ds, dd, dh, df, xd, yd, xd0u, yd0u, td, ctd, sd, txt = ClickBlocks()

_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def _program(op, right):
    a = Char("A")
    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(_OPS[op](a, right)):
            out(hit)
    return prog


@pytest.mark.parametrize("op", ["==", "!="])
@pytest.mark.parametrize("literal", [False, True])
def test_char_equality_exports_as_a_contact(op, literal):
    b = Char("B")
    prog = _program(op, "T" if literal else b)
    mapping = TagMap({Char("A"): txt[1], b: txt[2], Bool("Hit"): c[1]}, include_system=False)
    bundle = pyrung_to_ladder(prog, mapping)
    assert bundle.main_rows[1][1] == (f'TXT1{op}"T"' if literal else f"TXT1{op}TXT2")


@pytest.mark.parametrize("op", ["<", "<=", ">", ">="])
@pytest.mark.parametrize("literal", [False, True])
def test_char_order_compare_is_a_finding_and_refused_on_export(op, literal):
    a, b, hit = Char("A"), Char("B"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(_OPS[op](a, "T" if literal else b)):
            out(hit)
    mapping = TagMap({a: txt[1], b: txt[2], hit: c[1]}, include_system=False)

    report = validate_click_program(prog, mapping, mode="strict")
    found = [f for f in report.errors if f.code == CLK_TEXT_ORDER_COMPARE]
    assert len(found) == 1
    assert found[0].message.isascii() and found[0].suggestion.isascii()
    assert f"'{op}'" in found[0].message

    with pytest.raises(LadderExportError, match=CLK_TEXT_ORDER_COMPARE):
        pyrung_to_ladder(prog, mapping)
    assert pyrung_to_ladder(prog, mapping, allow=(CLK_TEXT_ORDER_COMPARE,)).main_rows


def test_numeric_order_compare_is_not_flagged():
    from pyrung.core import Int

    n, hit = Int("N"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(n > 3):
            out(hit)
    report = validate_click_program(
        prog, TagMap({n: ds[1], hit: c[1]}, include_system=False), mode="strict"
    )
    assert CLK_TEXT_ORDER_COMPARE not in {
        f.code for f in (*report.errors, *report.warnings, *report.hints)
    }


@pytest.mark.parametrize("op", ["==", "!="])
def test_a_txt_against_a_longer_literal_exports_as_written(op):
    # pyrung compares it over TXT1 and TXT2, as a CLICK does; the ladder keeps the one compare.
    hit, other = Bool("Hit"), Bool("Other")
    with Program(strict=False) as prog:
        with rung(other, _OPS[op](txt[1], "AB")):
            out(hit)
    mapping = TagMap({hit: c[1], other: c[2]}, include_system=False)
    report = validate_click_program(prog, mapping, mode="strict")
    assert not report.errors
    bundle = pyrung_to_ladder(prog, mapping)
    assert tuple(bundle.main_rows[1][1:3]) == ("C2", f'TXT1{op}"AB"')


def _window_codes(prog, mapping):
    report = validate_click_program(prog, mapping, mode="strict")
    return [f for f in report.errors if f.code == CLK_TEXT_WINDOW_NOT_CONTIGUOUS]


def test_a_window_onto_a_named_array_gap_is_a_finding_and_refused():
    # Padded1_b's next slot (TXT3) is no field's, so pyrung compares Padded1_b alone;
    # a CLICK compares TXT2 and TXT3.
    from pyrung.core import named_array

    @named_array(Char, count=2, stride=3)
    class Padded:
        a = ""
        b = ""

    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(Padded.b[1] == "XY"):
            out(hit)
    mapping = TagMap([*Padded.map_to(txt.select(1, 6)), hit.map_to(c[1])], include_system=False)

    found = _window_codes(prog, mapping)
    assert len(found) == 1
    assert found[0].message.isascii() and found[0].suggestion.isascii()
    assert "Padded1_b (TXT2) alone" in found[0].message
    assert "TXT2, TXT3" in found[0].message
    with pytest.raises(LadderExportError, match=CLK_TEXT_WINDOW_NOT_CONTIGUOUS):
        pyrung_to_ladder(prog, mapping)
    bundle = pyrung_to_ladder(prog, mapping, allow=(CLK_TEXT_WINDOW_NOT_CONTIGUOUS,))
    assert bundle.main_rows[1][1] == 'TXT2=="XY"'


@pytest.mark.parametrize("op", ["==", "!="])
def test_a_window_over_a_block_mapped_out_of_order_is_a_finding(op):
    # Built before the mapping, the compare reads its block: Msg1 then Msg2.
    from pyrung.core import Block, TagType

    msg, hit = Block("Msg", TagType.CHAR, 1, 2), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(_OPS[op](msg[1], "AB")):
            out(hit)
    mapping = TagMap(
        [msg[1].map_to(txt[5]), msg[2].map_to(txt[3]), hit.map_to(c[1])], include_system=False
    )
    found = _window_codes(prog, mapping)
    assert len(found) == 1
    assert "Msg1 (TXT5), Msg2 (TXT3)" in found[0].message
    assert "TXT5, TXT6" in found[0].message


def test_a_lone_char_against_a_longer_literal_is_a_finding():
    # Built before the mapping, a Char of no block reads only itself; a CLICK
    # reads TXT9 and TXT10.
    a, hit = Char("A"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(a == "AB"):
            out(hit)
    found = _window_codes(prog, TagMap({a: txt[9], hit: c[1]}, include_system=False))
    assert len(found) == 1
    assert "A (TXT9) alone" in found[0].message
    assert "TXT9, TXT10" in found[0].message


def test_a_window_in_a_branch_and_a_pin_is_checked_once_each():
    from pyrung.core import Block, Counter, TagType, branch, count_up

    msg = Block("Msg", TagType.CHAR, 1, 2)
    hit, other, ok = Bool("Hit"), Bool("Other"), Bool("Ok")
    cnt = Counter.clone("Cnt")
    with Program(strict=False) as prog:
        with rung(other):
            out(hit)
            with branch(msg[1] == "XY"):
                out(ok)
        with rung(other):
            count_up(cnt, 5).reset(msg[1] != "XY")
    mapping = TagMap(
        [
            msg[1].map_to(txt[5]),
            msg[2].map_to(txt[3]),
            hit.map_to(c[1]),
            other.map_to(c[2]),
            ok.map_to(c[3]),
            cnt.Done.map_to(ct[1]),
            cnt.Acc.map_to(ctd[1]),
        ],
        include_system=False,
    )
    found = _window_codes(prog, mapping)
    assert [f.location for f in found] == ["main rung 0", "main rung 1"]


def test_a_compare_built_after_its_mapping_is_not_a_finding():
    # Mapped first, every spelling reads the TXTs a CLICK compares: the logical
    # tag and the raw slot, out of order, a lone Char, in order, onto padding.
    from pyrung.core import Block, TagType, named_array

    blocks = ClickBlocks()
    bank = blocks.txt
    msg, inorder = Block("Msg", TagType.CHAR, 1, 2), Block("MsgC", TagType.CHAR, 1, 4)
    code, hit = Char("Code"), Bool("Hit")

    @named_array(Char, count=2, stride=3)
    class Padded:
        a = ""
        b = ""

    mapping = TagMap(
        [
            msg[1].map_to(bank[5]),
            msg[2].map_to(bank[3]),
            code.map_to(bank[21]),
            inorder.map_to(bank.select(31, 34)),
            *Padded.map_to(bank.select(41, 46)),
            hit.map_to(blocks.c[1]),
        ],
        include_system=False,
    )
    # The dict form records the slot too.
    other = Char("Other")
    TagMap({other: bank[25]}, include_system=False)
    assert [cond.tag.name for cond in (other == "AB").conditions] == ["Other", "TXT26"]

    with Program(strict=False) as prog:
        with rung(msg[1] == "AB", code != "AB", Padded.b[1] == "XY"):
            out(hit)
        with rung(bank[5] == "AB", bank[21] == "AB", bank[34] == "XY", bank[42] == "XY"):
            out(hit)
    assert _window_codes(prog, mapping) == []
    assert pyrung_to_ladder(prog, mapping).main_rows


def test_a_window_mapped_to_the_next_txts_is_not_a_finding():
    from pyrung.core import Block, TagType, named_array

    @named_array(Char, count=3)
    class Seq:
        ch = ""

    @named_array(Char, count=2, stride=2)
    class Pair:
        a = ""
        b = ""

    msg, hit = Block("Msg", TagType.CHAR, 1, 4), Bool("Hit")
    with Program(strict=False) as prog:
        with rung(msg[2] == "ABC", txt[998] != "XYZ", Seq.ch[1] == "AB", Pair.a[1] == "XYZ"):
            out(hit)
    mapping = TagMap(
        [
            msg.map_to(txt.select(101, 104)),
            *Seq.map_to(txt.select(11, 13)),
            *Pair.map_to(txt.select(1, 4)),
            hit.map_to(c[1]),
        ],
        include_system=False,
    )
    assert _window_codes(prog, mapping) == []
    assert pyrung_to_ladder(prog, mapping).main_rows


def test_a_pointer_compare_against_a_longer_literal_stays_refused_as_a_pointer():
    # A CLICK contact takes no pointer, so the window question never reaches one.
    from pyrung.click.validation import CLK_PTR_CONTEXT_ONLY_COPY

    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(txt[ds[1]] == "AB"):
            out(hit)
    mapping = TagMap({hit: c[1]}, include_system=False)
    report = validate_click_program(prog, mapping, mode="strict")
    codes = {f.code for f in report.errors}
    assert CLK_PTR_CONTEXT_ONLY_COPY in codes
    assert CLK_TEXT_WINDOW_NOT_CONTIGUOUS not in codes


def test_a_named_array_window_imports_back_comparing_the_same_txts(tmp_path):
    # Exported as TXT1=="XY" and imported back through the nickname file, the
    # compare is written on the structure and still reads TXT1 and TXT2.
    from pyrung.click.codegen.api import ladder_to_pyrung
    from pyrung.core import named_array

    @named_array(Char, count=2, stride=2)
    class Pair:
        a = ""
        b = ""

    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(Pair.a[1] == "XY"):
            out(hit)
    mapping = TagMap([*Pair.map_to(txt.select(1, 4)), hit.map_to(c[1])], include_system=False)
    bundle = pyrung_to_ladder(prog, mapping)
    assert bundle.main_rows[1][1] == 'TXT1=="XY"'
    nicknames = tmp_path / "Address.csv"
    mapping.to_nickname_file(nicknames)

    source = ladder_to_pyrung(bundle, nickname_csv=nicknames)
    assert 'Pair[1].a == "XY"' in source
    namespace: dict = {}
    exec(compile(source, "generated.py", "exec"), namespace)
    window = namespace["logic"].rungs[0]._conditions[0]
    assert [cond.tag.name for cond in window.conditions] == ["Pair1_a", "Pair1_b"]
    assert _window_codes(namespace["logic"], namespace["mapping"]) == []


def test_a_window_on_a_box_rung_is_one_finding():
    # A timer's or counter's enable holds the rung's conditions again; the
    # compare is still reported once, on the rung or the branch that has it.
    from pyrung.core import Block, Counter, TagType, Timer, branch, count_up, on_delay

    msg = Block("Msg", TagType.CHAR, 1, 2)
    hit, ok, rst = Bool("Hit"), Bool("Ok"), Bool("Rst")
    cnt, tmr, tmr2 = Counter.clone("Cnt"), Timer.clone("Tmr"), Timer.clone("Tmr2")
    with Program(strict=False) as prog:
        with rung(msg[1] == "AB"):
            count_up(cnt, 5).reset(rst)
        with rung(msg[1] != "AB"):
            on_delay(tmr, 100)
        with rung(hit):
            out(ok)
            with branch(msg[1] == "XY"):
                on_delay(tmr2, 100)
    mapping = TagMap(
        [
            msg[1].map_to(txt[5]),
            msg[2].map_to(txt[3]),
            hit.map_to(c[1]),
            ok.map_to(c[2]),
            rst.map_to(c[3]),
            cnt.Done.map_to(ct[1]),
            cnt.Acc.map_to(ctd[1]),
            tmr.Done.map_to(t[1]),
            tmr.Acc.map_to(td[1]),
            tmr2.Done.map_to(t[2]),
            tmr2.Acc.map_to(td[2]),
        ],
        include_system=False,
    )
    found = _window_codes(prog, mapping)
    assert [f.location for f in found] == ["main rung 0", "main rung 1", "main rung 2"]


def test_a_window_past_the_last_txt_says_so():
    hit = Bool("Hit")
    with Program(strict=False) as prog:
        with rung(txt[1000] == "AB"):
            out(hit)
    found = _window_codes(prog, TagMap({hit: c[1]}, include_system=False))
    assert len(found) == 1
    assert "TXT1001" not in found[0].message
    assert "TXT1000 and the TXTs after it, which run past TXT1000" in found[0].message
