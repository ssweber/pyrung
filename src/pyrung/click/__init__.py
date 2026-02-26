"""Click PLC dialect for pyrung.

This module adds Click-hardware-specific building blocks on top of the
hardware-agnostic ``pyrung`` core:

**Pre-built blocks** (constructed from ``pyclickplc`` bank metadata):

+--------+---------------------+---------+-----------+
| Name   | Click bank          | Type    | Kind      |
+========+=====================+=========+===========+
| ``x``  | X (digital inputs)  | BOOL    | InputBlock|
+--------+---------------------+---------+-----------+
| ``y``  | Y (digital outputs) | BOOL    | OutputBlock|
+--------+---------------------+---------+-----------+
| ``c``  | C (bit memory)      | BOOL    | Block     |
+--------+---------------------+---------+-----------+
| ``ds`` | DS (INT memory)     | INT     | Block     |
+--------+---------------------+---------+-----------+
| ``dd`` | DD (DINT memory)    | DINT    | Block     |
+--------+---------------------+---------+-----------+
| ``dh`` | DH (WORD memory)    | WORD    | Block     |
+--------+---------------------+---------+-----------+
| ``df`` | DF (REAL memory)    | REAL    | Block     |
+--------+---------------------+---------+-----------+
| ``t``  | T (timer done bits) | BOOL    | Block     |
+--------+---------------------+---------+-----------+
| ``td`` | TD (timer acc)      | INT     | Block     |
+--------+---------------------+---------+-----------+
| ``ct`` | CT (counter done)   | BOOL    | Block     |
+--------+---------------------+---------+-----------+
| ``ctd``| CTD (counter acc)   | DINT    | Block     |
+--------+---------------------+---------+-----------+
| ``sc`` | SC (system control) | BOOL    | Block     |
+--------+---------------------+---------+-----------+
| ``sd`` | SD (system data)    | INT     | Block     |
+--------+---------------------+---------+-----------+
| ``txt``| TXT (text memory)   | CHAR    | Block     |
+--------+---------------------+---------+-----------+
| ``xd`` | XD (input words)    | WORD    | InputBlock|
+--------+---------------------+---------+-----------+
| ``yd`` | YD (output words)   | WORD    | OutputBlock|
+--------+---------------------+---------+-----------+

**Type aliases** (Click-familiar names for IEC constructors):

- ``Bit = Bool``
- ``Int2 = Dint``
- ``Float = Real``
- ``Hex = Word``
- ``Txt = Char``

**Mapping and validation:**

- :class:`TagMap` — maps logical Tags/Blocks to Click hardware addresses.
- :func:`validate_click_program` — checks a Program against Click hardware restrictions.

**Soft PLC adapter:**

- :class:`ClickDataProvider` — bridges ``SystemState`` to pyclickplc's Modbus server.

**Communication instructions:**

- :func:`send` / :func:`receive` — Modbus TCP communication between Click PLCs.

Typical usage::

    from pyrung import *
    from pyrung.click import x, y, ds, TagMap

    Button = Bool("Button")
    Light  = Bool("Light")

    with Program() as logic:
        with Rung(Button):
            out(Light)

    mapping = TagMap({Button: x[1], Light: y[1]})
"""

from typing import Any, cast

from pyclickplc.addresses import format_address_display
from pyclickplc.banks import BANKS, DEFAULT_RETENTIVE, BankConfig, DataType

from pyrung.core import (
    Block,
    Bool,
    Char,
    Dint,
    InputBlock,
    InputTag,
    OutputBlock,
    OutputTag,
    Real,
    TagType,
    Word,
)
from pyrung.core.program import Program

Bit = Bool
Int2 = Dint
Float = Real
Hex = Word
Txt = Char

CLICK_TO_IEC: dict[DataType, TagType] = {
    DataType.BIT: TagType.BOOL,
    DataType.INT: TagType.INT,
    DataType.INT2: TagType.DINT,
    DataType.FLOAT: TagType.REAL,
    DataType.HEX: TagType.WORD,
    DataType.TXT: TagType.CHAR,
}


def _click_address_formatter(name: str, addr: int) -> str:
    if name in {"X", "Y"}:
        return f"{name}{addr:03d}"
    if name in {"XD", "YD"}:
        # Display-indexed API: XD0..XD8 / YD0..YD8.
        return f"{name}{addr}"
    return format_address_display(name, addr)


def _block_from_bank_config(config: BankConfig) -> Block | InputBlock | OutputBlock:
    name = config.name
    tag_type = CLICK_TO_IEC[config.data_type]
    start = config.min_addr
    end = config.max_addr
    valid_ranges = config.valid_ranges

    if name in {"XD", "YD"}:
        # Ergonomic display-indexed contract for click dialect users.
        start = 0
        end = 8
        valid_ranges = None

    if name in {"X", "XD"}:
        return InputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=_click_address_formatter,
        )
    if name in {"Y", "YD"}:
        return OutputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=_click_address_formatter,
        )
    return Block(
        name=name,
        type=tag_type,
        start=start,
        end=end,
        retentive=DEFAULT_RETENTIVE[name],
        valid_ranges=valid_ranges,
        address_formatter=_click_address_formatter,
    )


x: InputBlock = cast(InputBlock, _block_from_bank_config(BANKS["X"]))
y: OutputBlock = cast(OutputBlock, _block_from_bank_config(BANKS["Y"]))
c: Block = _block_from_bank_config(BANKS["C"])
t: Block = _block_from_bank_config(BANKS["T"])
ct: Block = _block_from_bank_config(BANKS["CT"])
sc: Block = _block_from_bank_config(BANKS["SC"])
ds: Block = _block_from_bank_config(BANKS["DS"])
dd: Block = _block_from_bank_config(BANKS["DD"])
dh: Block = _block_from_bank_config(BANKS["DH"])
df: Block = _block_from_bank_config(BANKS["DF"])
xd: InputBlock = cast(InputBlock, _block_from_bank_config(BANKS["XD"]))
yd: OutputBlock = cast(OutputBlock, _block_from_bank_config(BANKS["YD"]))
xd0u = InputTag("XD0u", TagType.WORD, retentive=False)
yd0u = OutputTag("YD0u", TagType.WORD, retentive=False)
td: Block = _block_from_bank_config(BANKS["TD"])
ctd: Block = _block_from_bank_config(BANKS["CTD"])
sd: Block = _block_from_bank_config(BANKS["SD"])
txt: Block = _block_from_bank_config(BANKS["TXT"])

from pyrung.click.data_provider import ClickDataProvider
from pyrung.click.send_receive import receive, send
from pyrung.click.tag_map import TagMap
from pyrung.click.validation import (
    ValidationMode,
    validate_click_program,
)


def _click_dialect_validator(program: Program, *, mode: str = "warn", **kwargs: Any) -> Any:
    tag_map = kwargs.pop("tag_map", None)
    if tag_map is None:
        raise TypeError("Program.validate('click', ...) requires keyword argument 'tag_map'.")
    if not isinstance(tag_map, TagMap):
        raise TypeError("Program.validate('click', ...) expects tag_map=TagMap(...).")
    if mode not in {"warn", "strict"}:
        raise ValueError("Program.validate('click', ...) mode must be 'warn' or 'strict'.")
    validated_mode = cast(ValidationMode, mode)
    return validate_click_program(program, tag_map=tag_map, mode=validated_mode, **kwargs)


Program.register_dialect("click", _click_dialect_validator)

__all__ = [
    "Bit",
    "Int2",
    "Float",
    "Hex",
    "Txt",
    "x",
    "y",
    "c",
    "t",
    "ct",
    "sc",
    "ds",
    "dd",
    "dh",
    "df",
    "xd",
    "yd",
    "xd0u",
    "yd0u",
    "td",
    "ctd",
    "sd",
    "txt",
    "TagMap",
    "ClickDataProvider",
    "validate_click_program",
    "send",
    "receive",
]
