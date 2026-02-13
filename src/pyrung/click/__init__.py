"""Click-style constructor aliases and prebuilt memory blocks."""

from typing import Any

from pyclickplc.addresses import format_address_display
from pyclickplc.banks import BANKS, DEFAULT_RETENTIVE, BankConfig, DataType

from pyrung.core import Block, Bool, Char, Dint, InputBlock, OutputBlock, Real, TagType, Word
from pyrung.core.program import Program
from pyrung.core.tag import MappingEntry

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


def _block_from_bank_config(config: BankConfig) -> Block | InputBlock | OutputBlock:
    name = config.name
    tag_type = CLICK_TO_IEC[config.data_type]
    start = config.min_addr
    end = config.max_addr
    valid_ranges = config.valid_ranges

    if name == "X":
        return InputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=format_address_display,
        )
    if name == "Y":
        return OutputBlock(
            name=name,
            type=tag_type,
            start=start,
            end=end,
            valid_ranges=valid_ranges,
            address_formatter=format_address_display,
        )
    return Block(
        name=name,
        type=tag_type,
        start=start,
        end=end,
        retentive=DEFAULT_RETENTIVE[name],
        valid_ranges=valid_ranges,
        address_formatter=format_address_display,
    )


x = _block_from_bank_config(BANKS["X"])
y = _block_from_bank_config(BANKS["Y"])
c = _block_from_bank_config(BANKS["C"])
t = _block_from_bank_config(BANKS["T"])
ct = _block_from_bank_config(BANKS["CT"])
sc = _block_from_bank_config(BANKS["SC"])
ds = _block_from_bank_config(BANKS["DS"])
dd = _block_from_bank_config(BANKS["DD"])
dh = _block_from_bank_config(BANKS["DH"])
df = _block_from_bank_config(BANKS["DF"])
td = _block_from_bank_config(BANKS["TD"])
ctd = _block_from_bank_config(BANKS["CTD"])
sd = _block_from_bank_config(BANKS["SD"])
txt = _block_from_bank_config(BANKS["TXT"])

from pyrung.click.data_provider import ClickDataProvider
from pyrung.click.send_receive import receive, send
from pyrung.click.tag_map import TagMap
from pyrung.click.validation import ClickFinding, ClickValidationReport, validate_click_program


def _click_dialect_validator(program: Program, *, mode: str = "warn", **kwargs: Any) -> Any:
    tag_map = kwargs.pop("tag_map", None)
    if tag_map is None:
        raise TypeError("Program.validate('click', ...) requires keyword argument 'tag_map'.")
    if not isinstance(tag_map, TagMap):
        raise TypeError("Program.validate('click', ...) expects tag_map=TagMap(...).")
    return validate_click_program(program, tag_map=tag_map, mode=mode, **kwargs)


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
    "td",
    "ctd",
    "sd",
    "txt",
    "TagMap",
    "ClickDataProvider",
    "ClickFinding",
    "ClickValidationReport",
    "validate_click_program",
    "send",
    "receive",
    "MappingEntry",
]
