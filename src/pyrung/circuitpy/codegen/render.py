"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

from pyrung.circuitpy.codegen._constants import (
    _BOARD_LED_TAG,
    _BOARD_NEOPIXEL_B_TAG,
    _BOARD_NEOPIXEL_G_TAG,
    _BOARD_NEOPIXEL_R_TAG,
    _BOARD_SAVE_MEMORY_CMD_TAG,
    _BOARD_SWITCH_TAG,
    _DINT_MAX,
    _DINT_MIN,
    _HELPER_ORDER,
    _INT_MAX,
    _INT_MIN,
    _SD_DELETE_ALL_CMD_TAG,
    _SD_EJECT_CMD_TAG,
    _SD_ERROR_CODE_TAG,
    _SD_ERROR_TAG,
    _SD_LOAD_ERROR,
    _SD_MOUNT_ERROR,
    _SD_READY_TAG,
    _SD_SAVE_ERROR,
    _SD_WRITE_STATUS_TAG,
    _SYS_CMD_MODE_STOP_TAG,
    _SYS_MODE_RUN_TAG,
    _TYPE_DEFAULTS,
)
from pyrung.circuitpy.codegen._util import (
    _first_defined_name,
    _global_line,
    _ret_defaults_literal,
    _ret_types_literal,
    _subroutine_symbol,
)
from pyrung.circuitpy.codegen.compile import _load_cast_expr, compile_rung
from pyrung.circuitpy.codegen.context import CodegenContext


def _tag_is_input_endpoint(ctx: CodegenContext, tag_name: str) -> bool:
    if tag_name == _BOARD_SWITCH_TAG:
        return True
    block_info = ctx.tag_block_addresses.get(tag_name)
    if block_info is None:
        return False
    block_id, _ = block_info
    binding = ctx.block_bindings.get(block_id)
    if binding is None:
        return False
    return binding.direction == "input"


def _render_run_transition_reset_lines(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for tag_name in sorted(ctx.referenced_tags):
        tag = ctx.referenced_tags[tag_name]
        if tag.retentive:
            continue
        if _tag_is_input_endpoint(ctx, tag_name):
            continue
        lines.append(f"    {ctx.symbol_for_tag(tag)} = {repr(tag.default)}")
    return lines


def _render_stop_output_clear_lines(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for binding in sorted(
        ctx.block_bindings.values(), key=lambda b: (ctx.block_symbols[b.block_id], b.block_id)
    ):
        if binding.direction != "output":
            continue
        symbol = ctx.block_symbols[binding.block_id]
        default = _TYPE_DEFAULTS[binding.tag_type]
        size = binding.end - binding.start + 1
        lines.append(f"    for _i in range({size}):")
        lines.append(f"        {symbol}[_i] = {repr(default)}")
    led_symbol = ctx.symbol_if_referenced(_BOARD_LED_TAG)
    if led_symbol is not None:
        lines.append(f"    {led_symbol} = False")
    for channel_tag in (_BOARD_NEOPIXEL_R_TAG, _BOARD_NEOPIXEL_G_TAG, _BOARD_NEOPIXEL_B_TAG):
        channel_symbol = ctx.symbol_if_referenced(channel_tag)
        if channel_symbol is not None:
            lines.append(f"    {channel_symbol} = 0")
    return lines


def _render_code(ctx: CodegenContext) -> str:
    main_fn_lines = _render_main_function(ctx)
    sub_fn_lines = _render_subroutine_functions(ctx)
    io_lines = _render_io_helpers(ctx)
    helper_lines = _render_helper_section(ctx)
    function_source_lines = _render_embedded_functions(ctx)
    needs_digitalio = ctx.uses_board_switch or ctx.uses_board_led
    needs_neopixel = ctx.uses_board_neopixel

    lines: list[str] = []

    # 1) imports
    lines.extend(
        [
            "import json",
            "import math",
            "import os",
            "import re",
            "import struct",
            "import time",
            "",
            "import board",
            "import busio",
            "import P1AM",
            "import sdcardio",
            "import storage",
            "",
        ]
    )
    if needs_digitalio:
        lines.extend(["import digitalio", ""])
    if needs_neopixel:
        lines.extend(["import neopixel", ""])
    lines.extend(
        [
            "try:",
            "    import microcontroller",
            "except ImportError:",
            "    microcontroller = None",
            "",
        ]
    )

    # 2) config constants
    lines.extend(
        [
            f"TARGET_SCAN_MS = {ctx.target_scan_ms!r}",
            f"WATCHDOG_MS = {ctx.watchdog_ms!r}",
            "PRINT_SCAN_OVERRUNS = False",
            "",
            f"_SLOT_MODULES = {[slot.part_number for slot in ctx.slot_bindings]!r}",
            f"_RET_DEFAULTS = {_ret_defaults_literal(ctx)!r}",
            f"_RET_TYPES = {_ret_types_literal(ctx)!r}",
            f'_RET_SCHEMA = "{ctx.compute_retentive_schema_hash()}"',
            "",
        ]
    )

    # 3) hardware bootstrap + roll-call
    lines.extend(
        [
            "base = P1AM.Base()",
            "base.rollCall(_SLOT_MODULES)",
            "",
        ]
    )
    if needs_digitalio:
        if ctx.uses_board_switch:
            lines.extend(
                [
                    "_board_switch_io = digitalio.DigitalInOut(board.SWITCH)",
                    "_board_switch_io.direction = digitalio.Direction.INPUT",
                ]
            )
        if ctx.uses_board_led:
            lines.extend(
                [
                    "_board_led_io = digitalio.DigitalInOut(board.LED)",
                    "_board_led_io.direction = digitalio.Direction.OUTPUT",
                    "_board_led_io.value = False",
                ]
            )
        lines.append("")
    if needs_neopixel:
        lines.extend(
            [
                "_board_pixel = neopixel.NeoPixel(board.NEOPIXEL, 1, auto_write=True)",
                "_board_pixel[0] = (0, 0, 0)",
                "",
            ]
        )

    # 4) watchdog API binding + startup config
    if ctx.watchdog_ms is not None:
        lines.extend(
            [
                '_wd_config = getattr(base, "config_watchdog", None)',
                '_wd_start = getattr(base, "start_watchdog", None)',
                '_wd_pet = getattr(base, "pet_watchdog", None)',
                "if _wd_config is None or _wd_start is None or _wd_pet is None:",
                '    raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")',
                "_wd_config(WATCHDOG_MS)",
                "_wd_start()",
                "",
            ]
        )

    # 5) tag and block declarations
    lines.append("# Scalars (non-block tags).")
    if ctx.scalar_tags:
        for tag_name in sorted(ctx.scalar_tags):
            tag = ctx.scalar_tags[tag_name]
            symbol = ctx.symbol_table[tag_name]
            lines.append(f"{symbol} = {repr(tag.default)}")
    else:
        lines.append("pass")
    lines.append("")

    lines.append("# Blocks (list-backed; PLC addresses remain 1-based, list indexes are 0-based).")
    block_bindings = sorted(
        ctx.block_bindings.values(), key=lambda b: (ctx.block_symbols[b.block_id], b.block_id)
    )
    for binding in block_bindings:
        symbol = ctx.block_symbols[binding.block_id]
        size = binding.end - binding.start + 1
        default = _TYPE_DEFAULTS[binding.tag_type]
        lines.append(f"{symbol} = [{repr(default)}] * {size}")
    lines.append("")

    # 6) runtime memory declarations (stubbed persistence state)
    lines.extend(
        [
            "_mem = {}",
            "_prev = {}",
            "_last_scan_ts = time.monotonic()",
            "_scan_overrun_count = 0",
            "",
            "_sd_available = False",
            '_MEMORY_PATH = "/sd/memory.json"',
            '_MEMORY_TMP_PATH = "/sd/_memory.tmp"',
            "_sd_spi = None",
            "_sd = None",
            "_sd_vfs = None",
            "_sd_write_status = False",
            "_sd_error = False",
            "_sd_error_code = 0",
            "_sd_save_cmd = False",
            "_sd_eject_cmd = False",
            "_sd_delete_all_cmd = False",
            "",
        ]
    )
    if ctx.runstop is not None:
        lines.extend(
            [
                "_mode_run = True",
                "_runstop_initialized = False",
                "_runstop_raw = False",
                "_runstop_debounced = False",
                "_runstop_last_change_ts = 0.0",
                "",
            ]
        )

    # 7) SD mount + load memory startup call
    ret_globals = [ctx.symbol_for_tag(tag) for _, tag in sorted(ctx.retentive_tags.items())]
    load_globals = ", ".join(ret_globals + ["_sd_write_status", "_sd_error", "_sd_error_code"])
    save_globals = load_globals
    lines.extend(
        [
            "def _mount_sd():",
            "    global _sd_available, _sd_spi, _sd, _sd_vfs, _sd_error, _sd_error_code",
            "    try:",
            "        _sd_spi = busio.SPI(board.SD_SCK, board.SD_MOSI, board.SD_MISO)",
            "        _sd = sdcardio.SDCard(_sd_spi, board.SD_CS)",
            "        _sd_vfs = storage.VfsFat(_sd)",
            '        storage.mount(_sd_vfs, "/sd")',
            "        _sd_available = True",
            "        _sd_error = False",
            "        _sd_error_code = 0",
            "    except Exception as exc:",
            "        _sd_available = False",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_MOUNT_ERROR}",
            '        print(f"Retentive storage unavailable: {exc}")',
            "",
            "def load_memory():",
        ]
    )
    if load_globals:
        lines.append(f"    global {load_globals}")
    lines.extend(
        [
            "    if not _sd_available:",
            '        print("Retentive load skipped: SD unavailable")',
            "        return",
            "    _sd_write_status = True",
            "    if microcontroller is not None and len(microcontroller.nvm) > 0 and microcontroller.nvm[0] == 1:",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_LOAD_ERROR}",
            "        _sd_write_status = False",
            '        print("Retentive load skipped: interrupted previous save detected")',
            "        return",
            "    try:",
            '        with open(_MEMORY_PATH, "r", encoding="utf-8") as f:',
            "            payload = json.load(f)",
            "    except Exception as exc:",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_LOAD_ERROR}",
            "        _sd_write_status = False",
            '        print(f"Retentive load skipped: {exc}")',
            "        return",
            '    if payload.get("schema") != _RET_SCHEMA:',
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_LOAD_ERROR}",
            "        _sd_write_status = False",
            '        print("Retentive load skipped: schema mismatch")',
            "        return",
            '    values = payload.get("values", {})',
        ]
    )
    for name, tag in sorted(ctx.retentive_tags.items()):
        symbol = ctx.symbol_for_tag(tag)
        load_expr = _load_cast_expr('_entry.get("value", ' + symbol + ")", tag.type.name)
        lines.extend(
            [
                f'    _entry = values.get("{name}")',
                f'    if isinstance(_entry, dict) and _entry.get("type") == "{tag.type.name}":',
                "        try:",
                f"            {symbol} = {load_expr}",
                "        except Exception:",
                "            pass",
            ]
        )
    lines.extend(
        [
            "    _sd_error = False",
            "    _sd_error_code = 0",
            "    _sd_write_status = False",
            "",
            "def save_memory():",
        ]
    )
    if save_globals:
        lines.append(f"    global {save_globals}")
    lines.extend(
        [
            "    if not _sd_available:",
            "        return",
            "    _sd_write_status = True",
            "    values = {}",
        ]
    )
    for name, tag in sorted(ctx.retentive_tags.items()):
        symbol = ctx.symbol_for_tag(tag)
        lines.extend(
            [
                f'    if {symbol} != _RET_DEFAULTS["{name}"]:',
                f'        values["{name}"] = {{"type": "{tag.type.name}", "value": {symbol}}}',
            ]
        )
    lines.extend(
        [
            '    payload = {"schema": _RET_SCHEMA, "values": values}',
            "    dirty_armed = False",
            "    if microcontroller is not None and len(microcontroller.nvm) > 0:",
            "        microcontroller.nvm[0] = 1",
            "        dirty_armed = True",
            "    try:",
            '        with open(_MEMORY_TMP_PATH, "w", encoding="utf-8") as f:',
            "            json.dump(payload, f)",
            "        os.replace(_MEMORY_TMP_PATH, _MEMORY_PATH)",
            "    except Exception as exc:",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_SAVE_ERROR}",
            "        _sd_write_status = False",
            '        print(f"Retentive save failed: {exc}")',
            "        return",
            "    if dirty_armed:",
            "        microcontroller.nvm[0] = 0",
            "    _sd_error = False",
            "    _sd_error_code = 0",
            "    _sd_write_status = False",
            "",
            "_mount_sd()",
            "load_memory()",
            "",
        ]
    )

    # 8) helper definitions
    lines.extend(helper_lines)

    # 9) embedded user function sources
    lines.extend(function_source_lines)

    # 10) compiled subroutine functions
    lines.extend(sub_fn_lines)

    # 11) compiled main-rung function
    lines.extend(main_fn_lines)

    # 12) scan-time I/O read/write helpers
    lines.extend(io_lines)

    # 13) main scan loop
    lines.extend(_render_scan_loop(ctx))

    return "\n".join(lines).rstrip() + "\n"


def _render_helper_section(ctx: CodegenContext) -> list[str]:
    lines = [
        "def _service_sd_commands():",
        "    global _sd_write_status, _sd_error, _sd_error_code",
        "    global _sd_save_cmd, _sd_eject_cmd, _sd_delete_all_cmd",
        "    global _sd_available, _sd_spi, _sd, _sd_vfs",
        "    if not (_sd_save_cmd or _sd_eject_cmd or _sd_delete_all_cmd):",
        "        return",
        "    _do_delete = bool(_sd_delete_all_cmd)",
        "    _do_save = bool(_sd_save_cmd)",
        "    _do_eject = bool(_sd_eject_cmd)",
        "    _sd_save_cmd = False",
        "    _sd_eject_cmd = False",
        "    _sd_delete_all_cmd = False",
        "    _sd_write_status = True",
        "    _command_failed = False",
        "    if _do_delete:",
        "        try:",
        "            for _path in (_MEMORY_PATH, _MEMORY_TMP_PATH):",
        "                try:",
        "                    os.remove(_path)",
        "                except OSError:",
        "                    pass",
        "        except Exception as exc:",
        "            _command_failed = True",
        "            _sd_error = True",
        f"            _sd_error_code = {_SD_SAVE_ERROR}",
        '            print(f"SD delete_all command failed: {exc}")',
        "    if _do_save:",
        "        try:",
        "            save_memory()",
        f"            if _sd_error and _sd_error_code == {_SD_SAVE_ERROR}:",
        "                _command_failed = True",
        "        except Exception as exc:",
        "            _command_failed = True",
        "            _sd_error = True",
        f"            _sd_error_code = {_SD_SAVE_ERROR}",
        '            print(f"SD save command failed: {exc}")',
        "    if _do_eject:",
        "        try:",
        "            if _sd_available:",
        '                storage.umount("/sd")',
        "            _sd_available = False",
        "            _sd_spi = None",
        "            _sd = None",
        "            _sd_vfs = None",
        "        except Exception as exc:",
        "            _command_failed = True",
        "            _sd_error = True",
        f"            _sd_error_code = {_SD_SAVE_ERROR}",
        '            print(f"SD eject command failed: {exc}")',
        "    if not _command_failed:",
        "        _sd_error = False",
        "        _sd_error_code = 0",
        "    # SC69 pulses for this serviced-command scan; reset occurs at next scan start.",
        "    _sd_write_status = True",
        "",
    ]
    if ctx.runstop is not None:
        scalar_symbols = [ctx.symbol_table[name] for name in sorted(ctx.scalar_tags)]
        reset_lines = _render_run_transition_reset_lines(ctx)
        reset_globals = sorted(
            {
                "_mem",
                "_prev",
                "_sd_save_cmd",
                "_sd_eject_cmd",
                "_sd_delete_all_cmd",
                *scalar_symbols,
            }
        )
        lines.append("def _reset_for_run_transition():")
        lines.append(f"    global {', '.join(reset_globals)}")
        lines.extend(
            [
                "    _mem = {}",
                "    _prev = {}",
                "    _sd_save_cmd = False",
                "    _sd_eject_cmd = False",
                "    _sd_delete_all_cmd = False",
            ]
        )
        if reset_lines:
            lines.extend(reset_lines)
        else:
            lines.append("    pass")
        lines.append("")

        lines.append("def _force_outputs_off():")
        if scalar_symbols:
            lines.append(f"    global {', '.join(sorted(set(scalar_symbols)))}")
        output_clear_lines = _render_stop_output_clear_lines(ctx)
        if output_clear_lines:
            lines.extend(output_clear_lines)
        lines.append("    _write_outputs()")
        lines.append("")

    for binding in sorted(
        (ctx.block_bindings[bid] for bid in ctx.used_indirect_blocks),
        key=lambda b: ctx.index_helper_name(b.block_id),
    ):
        helper_name = ctx.index_helper_name(binding.block_id)
        lines.append(f"def {helper_name}(addr):")
        lines.append(f"    if addr < {binding.start} or addr > {binding.end}:")
        lines.append(
            f'        raise IndexError(f"Address {{addr}} out of range for {binding.logical_name} ({binding.start}-{binding.end})")'
        )
        if binding.valid_addresses is not None:
            lines.append(f"    if addr not in {binding.valid_addresses!r}:")
            lines.append(
                f'        raise IndexError(f"Address {{addr}} out of range for {binding.logical_name} ({binding.start}-{binding.end})")'
            )
        lines.append(f"    return int(addr) - {binding.start}")
        lines.append("")

    helper_defs = {
        "_clamp_int": [
            "def _clamp_int(value):",
            "    if value < -32768:",
            "        return -32768",
            "    if value > 32767:",
            "        return 32767",
            "    return int(value)",
            "",
        ],
        "_wrap_int": [
            "def _wrap_int(value, bits, signed):",
            "    mask = (1 << bits) - 1",
            "    v = int(value) & mask",
            "    if signed and v >= (1 << (bits - 1)):",
            "        v -= (1 << bits)",
            "    return v",
            "",
        ],
        "_rise": [
            "def _rise(curr, prev):",
            "    return bool(curr) and not bool(prev)",
            "",
        ],
        "_fall": [
            "def _fall(curr, prev):",
            "    return not bool(curr) and bool(prev)",
            "",
        ],
        "_int_to_float_bits": [
            "def _int_to_float_bits(n):",
            '    return struct.unpack("<f", struct.pack("<I", int(n) & 0xFFFFFFFF))[0]',
            "",
        ],
        "_float_to_int_bits": [
            "def _float_to_int_bits(f):",
            '    return struct.unpack("<I", struct.pack("<f", float(f)))[0]',
            "",
        ],
        "_ascii_char_from_code": [
            "def _ascii_char_from_code(code):",
            "    if code < 0 or code > 127:",
            '        raise ValueError("ASCII code out of range")',
            "    return chr(code)",
            "",
        ],
        "_as_single_ascii_char": [
            "def _as_single_ascii_char(value):",
            "    if not isinstance(value, str):",
            '        raise ValueError("CHAR value must be a string")',
            '    if value == "":',
            "        return value",
            "    if len(value) != 1 or ord(value) > 127:",
            '        raise ValueError("CHAR value must be blank or one ASCII character")',
            "    return value",
            "",
        ],
        "_text_from_source_value": [
            "def _text_from_source_value(value):",
            "    if isinstance(value, str):",
            "        return value",
            '    raise ValueError("text conversion source must resolve to str")',
            "",
        ],
        "_store_numeric_text_digit": [
            "def _store_numeric_text_digit(char, mode):",
            "    _char = _as_single_ascii_char(char)",
            '    if _char == "":',
            '        raise ValueError("empty CHAR cannot be converted to numeric")',
            '    if mode == "value":',
            '        if _char < "0" or _char > "9":',
            '            raise ValueError("Copy Character Value accepts only digits 0-9")',
            '        return ord(_char) - ord("0")',
            '    if mode == "ascii":',
            "        return ord(_char)",
            '    raise ValueError(f"Unsupported text->numeric mode: {mode}")',
            "",
        ],
        "_format_int_text": [
            "def _format_int_text(value, width, suppress_zero, signed=True):",
            "    if suppress_zero:",
            "        return str(value)",
            "    if not signed:",
            '        return f"{value:0{width}X}"',
            "    if value < 0:",
            '        return f"-{abs(value):0{width}d}"',
            '    return f"{value:0{width}d}"',
            "",
        ],
        "_render_text_from_numeric": [
            "def _render_text_from_numeric(",
            "    value,",
            "    *,",
            "    source_type=None,",
            "    suppress_zero=True,",
            "    pad=None,",
            "    exponential=False,",
            "):",
            '    if source_type == "REAL" or isinstance(value, float):',
            "        numeric = float(value)",
            "        if not math.isfinite(numeric):",
            '            raise ValueError("REAL source is not finite")',
            '        return f"{numeric:.7E}" if exponential else f"{numeric:.7f}"',
            "",
            "    number = int(value)",
            "    effective_suppress_zero = suppress_zero if pad is None else False",
            "    signed_width = max(pad - 1, 0) if pad is not None and number < 0 else pad",
            "",
            '    if source_type == "WORD":',
            "        width = 4 if pad is None else pad",
            "        return _format_int_text(number & 0xFFFF, width, effective_suppress_zero, False)",
            '    if source_type == "DINT":',
            "        width = 10 if signed_width is None else signed_width",
            "        return _format_int_text(number, width, effective_suppress_zero)",
            '    if source_type == "INT":',
            "        width = 5 if signed_width is None else signed_width",
            "        return _format_int_text(number, width, effective_suppress_zero)",
            "",
            "    if pad is None:",
            '        return str(number) if suppress_zero else f"{number:05d}"',
            "    width = 5 if signed_width is None else signed_width",
            "    return _format_int_text(number, width, False)",
            "",
        ],
        "_termination_char": [
            "def _termination_char(termination_code):",
            "    if termination_code is None:",
            '        return ""',
            "    if isinstance(termination_code, str):",
            "        if len(termination_code) != 1:",
            '            raise ValueError("termination_code must be one character or int ASCII code")',
            "        return _as_single_ascii_char(termination_code)",
            "    if not isinstance(termination_code, int):",
            '        raise TypeError("termination_code must be int, str, or None")',
            "    return _ascii_char_from_code(termination_code)",
            "",
        ],
        "_parse_pack_text_value": [
            "def _parse_pack_text_value(text, dest_type):",
            '    if text == "":',
            '        raise ValueError("empty text cannot be parsed")',
            '    if dest_type in {"INT", "DINT"}:',
            '        if not re.fullmatch(r"[+-]?\\d+", text):',
            '            raise ValueError("integer parse failed")',
            "        parsed = int(text, 10)",
            '        if dest_type == "INT" and (parsed < -32768 or parsed > 32767):',
            '            raise ValueError("integer out of INT range")',
            '        if dest_type == "DINT" and (parsed < -2147483648 or parsed > 2147483647):',
            '            raise ValueError("integer out of DINT range")',
            "        return parsed",
            '    if dest_type == "WORD":',
            '        if not re.fullmatch(r"[0-9A-Fa-f]+", text):',
            '            raise ValueError("hex parse failed")',
            "        parsed = int(text, 16)",
            "        if parsed < 0 or parsed > 0xFFFF:",
            '            raise ValueError("hex out of WORD range")',
            "        return parsed",
            '    if dest_type == "REAL":',
            "        parsed = float(text)",
            "        if not math.isfinite(parsed):",
            '            raise ValueError("REAL parse produced non-finite value")',
            '        struct.pack("<f", parsed)',
            "        return parsed",
            '    raise TypeError(f"Unsupported pack_text destination type: {dest_type}")',
            "",
        ],
        "_store_copy_value_to_type": [
            "def _store_copy_value_to_type(value, dest_type):",
            "    if isinstance(value, float) and not math.isfinite(value):",
            "        value = 0",
            '    if dest_type == "INT":',
            f"        return max({_INT_MIN}, min({_INT_MAX}, int(value)))",
            '    if dest_type == "DINT":',
            f"        return max({_DINT_MIN}, min({_DINT_MAX}, int(value)))",
            '    if dest_type == "WORD":',
            "        return int(value) & 0xFFFF",
            '    if dest_type == "REAL":',
            "        return float(value)",
            '    if dest_type == "BOOL":',
            "        return bool(value)",
            '    if dest_type == "CHAR":',
            "        if not isinstance(value, str):",
            '            raise ValueError("CHAR value must be a string")',
            '        if value == "":',
            "            return value",
            "        if len(value) != 1 or ord(value) > 127:",
            '            raise ValueError("CHAR value must be blank or one ASCII character")',
            "        return value",
            "    return value",
            "",
        ],
    }
    needed_helpers = set(ctx.used_helpers)
    if "_store_numeric_text_digit" in needed_helpers:
        needed_helpers.add("_as_single_ascii_char")
    if "_termination_char" in needed_helpers:
        needed_helpers.update({"_as_single_ascii_char", "_ascii_char_from_code"})
    if "_render_text_from_numeric" in needed_helpers:
        needed_helpers.add("_format_int_text")

    for helper in _HELPER_ORDER:
        if helper in needed_helpers:
            lines.extend(helper_defs[helper])
    return lines


def _render_embedded_functions(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    if not ctx.function_sources:
        lines.append("# Embedded function call targets.")
        lines.append("# None emitted in foundation step.")
        lines.append("")
        return lines
    for symbol in sorted(ctx.function_sources):
        src = ctx.function_sources[symbol].rstrip()
        lines.append(src)
        fn_name = _first_defined_name(src)
        if fn_name is not None and fn_name != symbol:
            lines.append(f"{symbol} = {fn_name}")
        lines.append("")
    return lines


def _render_subroutine_functions(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []
    for sub_name in ctx.subroutine_names:
        fn_name = _subroutine_symbol(sub_name)
        ctx.function_globals[fn_name] = set()
        ctx.set_current_function(fn_name)
        body: list[str] = []
        for rung in ctx.program.subroutines[sub_name]:
            body.extend(compile_rung(rung, fn_name, ctx, indent=4))
        ctx.set_current_function(None)
        globals_line = _global_line(ctx.globals_for_function(fn_name), indent=4)
        lines.append(f"def {fn_name}():")
        if globals_line is not None:
            lines.append(globals_line)
        if body:
            lines.extend(body)
        else:
            lines.append("    pass")
        lines.append("")
    return lines


def _render_main_function(ctx: CodegenContext) -> list[str]:
    fn_name = "_run_main_rungs"
    ctx.function_globals[fn_name] = set()
    ctx.set_current_function(fn_name)
    body: list[str] = []
    for rung in ctx.program.rungs:
        body.extend(compile_rung(rung, fn_name, ctx, indent=4))
    ctx.set_current_function(None)

    lines = [f"def {fn_name}():"]
    globals_line = _global_line(ctx.globals_for_function(fn_name), indent=4)
    if globals_line is not None:
        lines.append(globals_line)
    if body:
        lines.extend(body)
    else:
        lines.append("    pass")
    lines.append("")
    return lines


def _render_io_helpers(ctx: CodegenContext) -> list[str]:
    lines: list[str] = []

    read_fn = "_read_inputs"
    ctx.function_globals[read_fn] = set()
    ctx.set_current_function(read_fn)
    read_body: list[str] = []
    for slot in ctx.slot_bindings:
        if slot.input_block_id is None:
            continue
        binding = ctx.block_bindings[slot.input_block_id]
        symbol = ctx.symbol_for_block(binding.block)
        if slot.input_kind == "discrete":
            mask = ctx.next_name(f"_mask_s{slot.slot_number}")
            read_body.append(f"    {mask} = int(base.readDiscrete({slot.slot_number}))")
            for ch in range(1, slot.input_count + 1):
                index = ch - binding.start
                read_body.append(f"    {symbol}[{index}] = bool(({mask} >> {ch - 1}) & 1)")
        elif slot.input_kind == "analog":
            for ch in range(1, slot.input_count + 1):
                index = ch - binding.start
                read_body.append(
                    f"    {symbol}[{index}] = int(base.readAnalog({slot.slot_number}, {ch}))"
                )
        elif slot.input_kind == "temperature":
            for ch in range(1, slot.input_count + 1):
                index = ch - binding.start
                read_body.append(
                    f"    {symbol}[{index}] = float(base.readTemperature({slot.slot_number}, {ch}))"
                )
    board_switch_symbol = ctx.symbol_if_referenced(_BOARD_SWITCH_TAG)
    if board_switch_symbol is not None:
        read_body.append(f"    {board_switch_symbol} = bool(_board_switch_io.value)")
    ctx.set_current_function(None)
    lines.append(f"def {read_fn}():")
    read_globals = _global_line(ctx.globals_for_function(read_fn), indent=4)
    if read_globals is not None:
        lines.append(read_globals)
    if read_body:
        lines.extend(read_body)
    else:
        lines.append("    pass")
    lines.append("")

    write_fn = "_write_outputs"
    ctx.function_globals[write_fn] = set()
    ctx.set_current_function(write_fn)
    write_body: list[str] = []
    for slot in ctx.slot_bindings:
        if slot.output_block_id is None:
            continue
        binding = ctx.block_bindings[slot.output_block_id]
        symbol = ctx.symbol_for_block(binding.block)
        if slot.output_kind == "discrete":
            mask = ctx.next_name(f"_out_mask_s{slot.slot_number}")
            write_body.append(f"    {mask} = 0")
            for ch in range(1, slot.output_count + 1):
                index = ch - binding.start
                write_body.append(f"    if bool({symbol}[{index}]):")
                write_body.append(f"        {mask} |= (1 << {ch - 1})")
            write_body.append(f"    base.writeDiscrete({mask}, {slot.slot_number})")
        elif slot.output_kind == "analog":
            for ch in range(1, slot.output_count + 1):
                index = ch - binding.start
                write_body.append(
                    f"    base.writeAnalog(int({symbol}[{index}]), {slot.slot_number}, {ch})"
                )
    board_led_symbol = ctx.symbol_if_referenced(_BOARD_LED_TAG)
    if board_led_symbol is not None:
        write_body.append(f"    _board_led_io.value = bool({board_led_symbol})")

    np_r_symbol = ctx.symbol_if_referenced(_BOARD_NEOPIXEL_R_TAG)
    np_g_symbol = ctx.symbol_if_referenced(_BOARD_NEOPIXEL_G_TAG)
    np_b_symbol = ctx.symbol_if_referenced(_BOARD_NEOPIXEL_B_TAG)
    if np_r_symbol is not None or np_g_symbol is not None or np_b_symbol is not None:
        r_expr = np_r_symbol if np_r_symbol is not None else "0"
        g_expr = np_g_symbol if np_g_symbol is not None else "0"
        b_expr = np_b_symbol if np_b_symbol is not None else "0"
        write_body.extend(
            [
                f"    _pixel_r = max(0, min(255, int({r_expr})))",
                f"    _pixel_g = max(0, min(255, int({g_expr})))",
                f"    _pixel_b = max(0, min(255, int({b_expr})))",
                "    _board_pixel[0] = (_pixel_r, _pixel_g, _pixel_b)",
            ]
        )
    ctx.set_current_function(None)
    lines.append(f"def {write_fn}():")
    write_globals = _global_line(ctx.globals_for_function(write_fn), indent=4)
    if write_globals is not None:
        lines.append(write_globals)
    if write_body:
        lines.extend(write_body)
    else:
        lines.append("    pass")
    lines.append("")
    return lines


def _render_scan_loop(ctx: CodegenContext) -> list[str]:
    sd_ready_symbol = ctx.symbol_if_referenced(_SD_READY_TAG)
    sd_write_symbol = ctx.symbol_if_referenced(_SD_WRITE_STATUS_TAG)
    sd_error_symbol = ctx.symbol_if_referenced(_SD_ERROR_TAG)
    sd_error_code_symbol = ctx.symbol_if_referenced(_SD_ERROR_CODE_TAG)
    sd_save_symbol = ctx.symbol_if_referenced(_BOARD_SAVE_MEMORY_CMD_TAG)
    sd_eject_symbol = ctx.symbol_if_referenced(_SD_EJECT_CMD_TAG)
    sd_delete_symbol = ctx.symbol_if_referenced(_SD_DELETE_ALL_CMD_TAG)
    runstop_source_symbol = (
        ctx.symbol_if_referenced(ctx.runstop.source) if ctx.runstop is not None else None
    )
    mode_run_symbol = ctx.symbol_if_referenced(_SYS_MODE_RUN_TAG)
    cmd_mode_stop_symbol = ctx.symbol_if_referenced(_SYS_CMD_MODE_STOP_TAG)

    lines = [
        "while True:",
        "    scan_start = time.monotonic()",
        "    _sd_write_status = False",
        "    dt = scan_start - _last_scan_ts",
        "    if dt < 0:",
        "        dt = 0.0",
        "    _last_scan_ts = scan_start",
        '    _mem["_dt"] = dt',
        "",
    ]

    if sd_save_symbol is not None:
        lines.append(f"    _sd_save_cmd = bool({sd_save_symbol})")
    if sd_eject_symbol is not None:
        lines.append(f"    _sd_eject_cmd = bool({sd_eject_symbol})")
    if sd_delete_symbol is not None:
        lines.append(f"    _sd_delete_all_cmd = bool({sd_delete_symbol})")
    lines.extend(
        [
            "    _service_sd_commands()",
        ]
    )
    if sd_save_symbol is not None:
        lines.append(f"    {sd_save_symbol} = _sd_save_cmd")
    if sd_eject_symbol is not None:
        lines.append(f"    {sd_eject_symbol} = _sd_eject_cmd")
    if sd_delete_symbol is not None:
        lines.append(f"    {sd_delete_symbol} = _sd_delete_all_cmd")
    if sd_ready_symbol is not None:
        lines.append(f"    {sd_ready_symbol} = bool(_sd_available)")
    if sd_write_symbol is not None:
        lines.append(f"    {sd_write_symbol} = bool(_sd_write_status)")
    if sd_error_symbol is not None:
        lines.append(f"    {sd_error_symbol} = bool(_sd_error)")
    if sd_error_code_symbol is not None:
        lines.append(f"    {sd_error_code_symbol} = int(_sd_error_code)")

    lines.extend(
        [
            "    _read_inputs()",
        ]
    )
    if ctx.runstop is not None:
        if runstop_source_symbol is None:
            raise RuntimeError("RunStopConfig source tag was not referenced")
        lines.extend(
            [
                f"    _runstop_sample = bool({runstop_source_symbol})",
                "    if not _runstop_initialized:",
                "        _runstop_raw = _runstop_sample",
                "        _runstop_debounced = _runstop_sample",
                "        _runstop_last_change_ts = scan_start",
                "        _runstop_initialized = True",
                "    elif _runstop_sample != _runstop_raw:",
                "        _runstop_raw = _runstop_sample",
                "        _runstop_last_change_ts = scan_start",
                f"    elif ((scan_start - _runstop_last_change_ts) * 1000.0) >= {ctx.runstop.debounce_ms}:",
                "        _runstop_debounced = _runstop_raw",
                "",
            ]
        )
        if ctx.runstop.run_when_high:
            lines.append("    _desired_run = bool(_runstop_debounced)")
        else:
            lines.append("    _desired_run = (not bool(_runstop_debounced))")
        if ctx.runstop.expose_mode_tags and cmd_mode_stop_symbol is not None:
            lines.extend(
                [
                    f"    if bool({cmd_mode_stop_symbol}):",
                    "        _desired_run = False",
                    f"        {cmd_mode_stop_symbol} = False",
                ]
            )
        lines.extend(
            [
                "    if _desired_run != _mode_run:",
                "        if _desired_run:",
                "            _reset_for_run_transition()",
                "        _mode_run = _desired_run",
            ]
        )
        if ctx.runstop.expose_mode_tags and mode_run_symbol is not None:
            lines.append(f"    {mode_run_symbol} = bool(_mode_run)")
        lines.extend(
            [
                "    if _mode_run:",
                "        _run_main_rungs()",
                "        _write_outputs()",
                "    else:",
                "        _force_outputs_off()",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "    _run_main_rungs()",
                "    _write_outputs()",
                "",
            ]
        )

    for tag_name in sorted(ctx.edge_prev_tags):
        tag = ctx.referenced_tags[tag_name]
        lines.append(f'    _prev["{tag_name}"] = {ctx.symbol_for_tag(tag)}')
    lines.append("")
    if ctx.watchdog_ms is not None:
        lines.append("    _wd_pet()")
        lines.append("")
    lines.extend(
        [
            "    elapsed_ms = (time.monotonic() - scan_start) * 1000.0",
            "    sleep_ms = TARGET_SCAN_MS - elapsed_ms",
            "    if sleep_ms > 0:",
            "        time.sleep(sleep_ms / 1000.0)",
            "    else:",
            "        _scan_overrun_count += 1",
            "        if PRINT_SCAN_OVERRUNS:",
            '            print(f"Scan overrun #{_scan_overrun_count}: {-sleep_ms:.3f} ms late")',
            "",
        ]
    )
    return lines
