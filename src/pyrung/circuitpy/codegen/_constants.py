"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

import re
from typing import Any

from pyrung.core.tag import TagType

_IDENT_RE = re.compile(r"[^A-Za-z0-9_]")


_TYPE_DEFAULTS: dict[TagType, Any] = {
    TagType.BOOL: False,
    TagType.INT: 0,
    TagType.DINT: 0,
    TagType.REAL: 0.0,
    TagType.WORD: 0,
    TagType.CHAR: "",
}


_HELPER_ORDER = (
    "_clamp_int",
    "_wrap_int",
    "_rise",
    "_fall",
    "_int_to_float_bits",
    "_float_to_int_bits",
    "_ascii_char_from_code",
    "_as_single_ascii_char",
    "_text_from_source_value",
    "_store_numeric_text_digit",
    "_format_int_text",
    "_render_text_from_numeric",
    "_termination_char",
    "_parse_pack_text_value",
    "_store_copy_value_to_type",
)


_INT_MIN = -32768


_INT_MAX = 32767


_DINT_MIN = -2147483648


_DINT_MAX = 2147483647


_SD_READY_TAG = "storage.sd.ready"


_SD_WRITE_STATUS_TAG = "storage.sd.write_status"


_SD_ERROR_TAG = "storage.sd.error"


_SD_ERROR_CODE_TAG = "storage.sd.error_code"


_SD_EJECT_CMD_TAG = "storage.sd.eject_cmd"


_SD_DELETE_ALL_CMD_TAG = "storage.sd.delete_all_cmd"


_FAULT_OUT_OF_RANGE_TAG = "fault.out_of_range"


_SYS_MODE_RUN_TAG = "sys.mode_run"


_SYS_CMD_MODE_STOP_TAG = "sys.cmd_mode_stop"


_BOARD_SWITCH_TAG = "board.switch"


_BOARD_LED_TAG = "board.led"


_BOARD_NEOPIXEL_R_TAG = "board.neopixel.r"


_BOARD_NEOPIXEL_G_TAG = "board.neopixel.g"


_BOARD_NEOPIXEL_B_TAG = "board.neopixel.b"


_BOARD_SAVE_MEMORY_CMD_TAG = "board.save_memory_cmd"


_SD_MOUNT_ERROR = 1


_SD_LOAD_ERROR = 2


_SD_SAVE_ERROR = 3
