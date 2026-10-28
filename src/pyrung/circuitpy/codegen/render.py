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
    _FAULT_ADDRESS_ERROR_TAG,
    _FAULT_MATH_OPERATION_ERROR_TAG,
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
    _STORE_TYPE_HELPERS,
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
from pyrung.circuitpy.codegen.compile import compile_rungs
from pyrung.circuitpy.codegen.compile._primitives import _load_cast_expr
from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.circuitpy.codegen.render_modbus import (
    _render_ethernet_setup,
    _render_modbus_accessors,
    _render_modbus_client,
    _render_modbus_client_codepy,
    _render_modbus_protocol,
    _render_modbus_server,
    _render_modbus_server_init,
)
from pyrung.core.storage import REAL_MAX
from pyrung.core.system_points import system

#: The free-running clocks and their half periods in ms.  A clock reads on in
#: the odd half periods since RUN started, as core's ``clock_high`` has it.
_CLOCK_HALF_PERIOD_MS = (
    (system.sys.clock_10ms.name, 5),
    (system.sys.clock_100ms.name, 50),
    (system.sys.clock_500ms.name, 250),
    (system.sys.clock_1s.name, 500),
    (system.sys.clock_1m.name, 30000),
    (system.sys.clock_1h.name, 1800000),
)
#: The RTC points and their expression over ``_rtc_now = time.localtime()``.
#: Weekday is CLICK's: Sunday = 1 ... Saturday = 7 (``tm_wday`` is Monday = 0).
_RTC_FIELDS = (
    (system.rtc.year4.name, "_rtc_now[0]"),
    (system.rtc.year2.name, "_rtc_now[0] % 100"),
    (system.rtc.month.name, "_rtc_now[1]"),
    (system.rtc.day.name, "_rtc_now[2]"),
    (system.rtc.weekday.name, "((_rtc_now[6] + 1) % 7) + 1"),
    (system.rtc.hour.name, "_rtc_now[3]"),
    (system.rtc.minute.name, "_rtc_now[4]"),
    (system.rtc.second.name, "_rtc_now[5]"),
)
#: Fault flags cleared at the start of every scan; an instruction that faults
#: sets its flag again for the rest of that scan.
_SCAN_CLEARED_FAULTS = (
    system.fault.division_error.name,
    system.fault.out_of_range.name,
    system.fault.address_error.name,
)


def _system_clock_symbols(ctx: CodegenContext) -> list[tuple[str, int]]:
    return [
        (symbol, half_ms)
        for name, half_ms in _CLOCK_HALF_PERIOD_MS
        if (symbol := ctx.symbol_if_referenced(name)) is not None
    ]


def _system_scan_count_needed(ctx: CodegenContext) -> bool:
    return any(
        name in ctx.referenced_tags
        for name in (system.sys.scan_counter.name, system.sys.scan_clock_toggle.name)
    )


def _system_first_needed(ctx: CodegenContext) -> bool:
    return system.sys.first_scan.name in ctx.referenced_tags or bool(_system_clock_symbols(ctx))


def _math_stop_symbol(ctx: CodegenContext) -> str | None:
    """SC46's symbol when a Math in the program can raise it, else ``None``.

    Call after the rungs are compiled: ``_math_func`` marks the program's use.
    """
    if "_math_func" not in ctx.used_helpers:
        return None
    return ctx.symbol_if_referenced(_FAULT_MATH_OPERATION_ERROR_TAG)


def _render_math_stop_lines(ctx: CodegenContext, indent: int) -> list[str]:
    """End of a RUN scan: a math error (SC46) puts the code in STOP.

    As in core, the faulting scan runs to its end, then the PLC is in STOP
    with SC50 on and SC11 off; no scan runs in STOP, so SC46 and SC50 read on
    until the STOP->RUN reset clears them.  With a RUN/STOP switch,
    ``_math_stop_hold`` keeps STOP until the switch reads STOP; without one,
    nothing returns the code to RUN but a restart.
    """
    math_error_symbol = _math_stop_symbol(ctx)
    if math_error_symbol is None:
        return []
    sp = " " * indent
    lines = [f"{sp}if {math_error_symbol}:", f"{sp}    _mode_run = False"]
    if ctx.runstop is not None:
        lines.append(f"{sp}    _math_stop_hold = True")
    cmd_mode_stop_symbol = ctx.symbol_if_referenced(_SYS_CMD_MODE_STOP_TAG)
    if cmd_mode_stop_symbol is not None:
        lines.append(f"{sp}    {cmd_mode_stop_symbol} = True")
    mode_run_symbol = ctx.symbol_if_referenced(_SYS_MODE_RUN_TAG)
    if mode_run_symbol is not None:
        lines.append(f"{sp}    {mode_run_symbol} = False")
    lines.extend([f"{sp}    save_memory()", f'{sp}    print("Mode: STOP")'])
    return lines


def _render_system_point_state_lines(ctx: CodegenContext) -> list[str]:
    """Module-level state behind the derived system points the program uses.

    As in core, the scan counter and the clocks count from the first scan in
    RUN, and a STOP->RUN transition starts them again.
    """
    lines: list[str] = []
    if _system_scan_count_needed(ctx):
        lines.append("_sys_scan_n = 0")
    if _system_first_needed(ctx):
        lines.append("_sys_first = True")
    if _system_clock_symbols(ctx):
        lines.extend(["_sys_t0_ns = 0", "_sys_prev_ns = 0"])
    if lines:
        lines.append("")
    return lines


def _system_point_reset_globals(ctx: CodegenContext) -> list[str]:
    names: list[str] = []
    if _system_scan_count_needed(ctx):
        names.append("_sys_scan_n")
    if _system_first_needed(ctx):
        names.append("_sys_first")
    return names


def _render_system_point_reset_lines(ctx: CodegenContext) -> list[str]:
    """Restart the scan counter and the clocks on STOP->RUN, as core restarts scan_id."""
    lines: list[str] = []
    if _system_scan_count_needed(ctx):
        lines.append("    _sys_scan_n = 0")
    if _system_first_needed(ctx):
        lines.append("    _sys_first = True")
    return lines


def _render_system_point_scan_lines(ctx: CodegenContext, indent: int) -> list[str]:
    """Derived system points for the scan about to run, as core resolves them.

    The fault flags clear; then the scan counter (SD9, ``scan_id % 32768``),
    the scan clock (SC3, on when SD9 is odd), the first-scan bit, the
    free-running clocks over the time of the RUN scans before this one (0 on
    the first scan, so every clock starts off), the scan time (SD10) and the
    RTC.
    """
    sp = " " * indent
    lines: list[str] = []
    for name in _SCAN_CLEARED_FAULTS:
        symbol = ctx.symbol_if_referenced(name)
        if symbol is not None:
            lines.append(f"{sp}{symbol} = False")
    always_on = ctx.symbol_if_referenced(system.sys.always_on.name)
    if always_on is not None:
        lines.append(f"{sp}{always_on} = True")
    counter = ctx.symbol_if_referenced(system.sys.scan_counter.name)
    if counter is not None:
        lines.append(f"{sp}{counter} = _sys_scan_n")
    scan_clock = ctx.symbol_if_referenced(system.sys.scan_clock_toggle.name)
    if scan_clock is not None:
        lines.append(f"{sp}{scan_clock} = (_sys_scan_n & 1) == 1")
    first_scan = ctx.symbol_if_referenced(system.sys.first_scan.name)
    if first_scan is not None:
        lines.append(f"{sp}{first_scan} = _sys_first")
    clocks = _system_clock_symbols(ctx)
    if clocks:
        # Integer ms from the scan's monotonic_ns() reading: a float
        # monotonic() loses ms resolution within hours on CircuitPython's
        # 30-bit floats.
        lines.extend(
            [
                f"{sp}if _sys_first:",
                f"{sp}    _sys_t0_ns = scan_start_ns",
                f"{sp}    _sys_prev_ns = scan_start_ns",
                f"{sp}_sys_ms = (_sys_prev_ns - _sys_t0_ns) // 1000000",
                f"{sp}_sys_prev_ns = scan_start_ns",
            ]
        )
        for symbol, half_ms in clocks:
            lines.append(f"{sp}{symbol} = ((_sys_ms // {half_ms}) & 1) == 1")
    scan_ms = ctx.symbol_if_referenced(system.sys.scan_time_current_ms.name)
    if scan_ms is not None:
        lines.append(f"{sp}{scan_ms} = int(round(dt * 1000.0))")
    rtc = [
        (symbol, expr)
        for name, expr in _RTC_FIELDS
        if (symbol := ctx.symbol_if_referenced(name)) is not None
    ]
    if rtc:
        lines.append(f"{sp}_rtc_now = time.localtime()")
        for symbol, expr in rtc:
            lines.append(f"{sp}{symbol} = {expr}")
    return lines


def _render_system_point_advance_lines(ctx: CodegenContext, indent: int) -> list[str]:
    """After a scan in RUN: step the scan counter, drop the first-scan bit."""
    sp = " " * indent
    lines: list[str] = []
    if _system_scan_count_needed(ctx):
        lines.append(f"{sp}_sys_scan_n = (_sys_scan_n + 1) % 32768")
    if _system_first_needed(ctx):
        lines.append(f"{sp}_sys_first = False")
    return lines


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
        ctx.emitted_bindings(), key=lambda b: (ctx.block_symbols[b.block_id], b.block_id)
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


def _section(label: str) -> str:
    """Build a fixed-width 80-char ASCII section header."""
    prefix = f"# -- {label} "
    return prefix + "-" * max(0, 80 - len(prefix))


def _runtime_import_names(ctx: CodegenContext) -> set[str]:
    """Return the set of names to ``from pyrung_rt import ...``."""
    needed = set(ctx.used_helpers)
    # A flag that code.py needs ``import math``, not a runtime name.
    needed.discard("_calc_math_isfinite")
    if "_store_numeric_text_digit" in needed:
        needed.add("_as_single_ascii_char")
    if "_termination_char" in needed:
        needed.update({"_as_single_ascii_char", "_ascii_char_from_code"})
    if "_render_text_from_numeric" in needed:
        needed.add("_format_int_text")
    if "_math_func" in needed:
        needed.add("_MathDomainError")
    # Client pack/unpack helpers and constants used by per-job code in code.py
    if ctx.modbus_client is not None:
        specs = list(ctx.modbus_client_specs)
        if specs:
            needed.add("_MB_CLIENT_IDLE")
            needed.add("_mb_client_pack_register_values")
            needed.add("_mb_client_unpack_register_values")
            if any(s.bank is None for s in specs):
                needed.add("_mb_client_raw_pack_register_values")
                needed.add("_mb_client_raw_unpack_register_values")
    if ctx.modbus_client is not None and ctx.modbus_client_specs:
        for spec in ctx.modbus_client_specs:
            for item in spec.items:
                helper = _STORE_TYPE_HELPERS.get(item.tag_type) if item.tag_type else None
                if helper is not None:
                    needed.add(helper)
                else:
                    needed.add("_store_copy_value_to_type")
    return needed


def _render_runtime_wiring(ctx: CodegenContext) -> list[str]:
    """Emit assignment lines that wire code.py functions into pyrung_rt slots."""
    lines: list[str] = ["", "# -- Wire runtime " + "-" * 60]
    if ctx.modbus_server is not None:
        lines.extend(
            [
                "_rt._mb_read_coil = _mb_read_coil",
                "_rt._mb_write_coil = _mb_write_coil",
                "_rt._mb_read_reg = _mb_read_reg",
                "_rt._mb_write_reg = _mb_write_reg",
                "_rt._mb_server = _mb_server",
                "_rt._mb_clients = _mb_clients",
                "_rt._mb_buf = _mb_buf",
            ]
        )
    if ctx.modbus_client is not None:
        lines.extend(
            [
                "_rt._mb_pool = _mb_pool",
                "_rt._mb_client_jobs = _mb_client_jobs",
            ]
        )
    lines.append("")
    return lines


def _render_code(ctx: CodegenContext, *, has_runtime: bool = False) -> str:
    main_fn_lines = _render_main_function(ctx)
    sub_fn_lines = _render_subroutine_functions(ctx)
    io_lines = _render_io_helpers(ctx)
    helper_lines = _render_helper_section(ctx, helpers_in_runtime=has_runtime)
    function_source_lines = _render_embedded_functions(ctx)
    modbus_imports_enabled = ctx.modbus_server is not None or ctx.modbus_client is not None
    modbus_bootstrap_lines = _render_ethernet_setup(ctx)
    if has_runtime:
        modbus_server_lines: list[str] = []
        modbus_protocol_lines: list[str] = []
        modbus_client_lines: list[str] = []
        modbus_server_init_lines = _render_modbus_server_init(ctx)
        modbus_client_codepy_lines = _render_modbus_client_codepy(ctx)
    else:
        modbus_server_lines = _render_modbus_server(ctx)
        modbus_protocol_lines = _render_modbus_protocol(ctx)
        modbus_client_lines = _render_modbus_client(ctx)
        modbus_server_init_lines = []
        modbus_client_codepy_lines = []
    modbus_accessor_lines = _render_modbus_accessors(ctx)
    needs_digitalio = ctx.uses_board_switch or ctx.uses_board_led or modbus_imports_enabled
    needs_neopixel = ctx.uses_board_neopixel

    lines: list[str] = []

    # 1) imports
    lines.append(_section("Imports"))
    lines.extend(
        [
            "import json",
            "import math",
            "import os",
            *(["import random"] if ctx.uses_random else []),
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
    if modbus_imports_enabled:
        lines.extend(
            [
                "from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K",
                "import adafruit_wiznet5k.adafruit_wiznet5k_socketpool as _mb_socketpool",
                "",
            ]
        )
    lines.extend(
        [
            "try:",
            "    import microcontroller",
            "except ImportError:",
            "    microcontroller = None",
            "",
        ]
    )
    if has_runtime:
        lines.append("import pyrung_rt as _rt")
        rt_imports = _runtime_import_names(ctx)
        if rt_imports:
            lines.append(f"from pyrung_rt import {', '.join(sorted(rt_imports))}")
        lines.append("")

    # 2) config constants
    lines.append(_section("Configuration"))
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
    lines.append(_section("Hardware bootstrap"))
    if ctx.hw.configured_slots:
        lines.extend(
            [
                "base = P1AM.Base()",
                "base.rollCall(_SLOT_MODULES)",
                "",
            ]
        )
    else:
        lines.append("")
    if modbus_bootstrap_lines:
        lines.extend(modbus_bootstrap_lines)
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
    lines.append(_section("Tags and blocks"))
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
        ctx.emitted_bindings(), key=lambda b: (ctx.block_symbols[b.block_id], b.block_id)
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
            # Scan time in integer ns: a float monotonic() loses ms resolution
            # within hours on CircuitPython's 30-bit floats.
            "_last_scan_ns = time.monotonic_ns()",
            "_scan_overrun_count = 0",
            "",
            "_sd_available = False",
            '_MEMORY_PATH = "/sd/memory.json"',
            '_MEMORY_TMP_PATH = "/sd/_memory.tmp"',
            '_MEMORY_BAK_PATH = "/sd/memory.json.bak"',
            "_sd_spi = None",
            "_sd = None",
            "_sd_vfs = None",
            "_sd_write_status = False",
            "_sd_error = False",
            "_sd_error_code = 0",
            "_sd_save_cmd = False",
            "_sd_eject_cmd = False",
            "_sd_delete_all_cmd = False",
            "_ret_snapshot = {}",
            "_ret_last_save_ns = 0",
            "_RET_AUTO_SAVE_NS = 30000000000",
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
                "_runstop_last_change_ns = 0",
            ]
        )
        if _math_stop_symbol(ctx) is not None:
            lines.append("_math_stop_hold = False")
        lines.append("")
    elif _math_stop_symbol(ctx) is not None:
        lines.extend(["_mode_run = True", ""])
    lines.extend(_render_system_point_state_lines(ctx))
    if has_runtime:
        if modbus_server_init_lines or modbus_client_codepy_lines:
            lines.append(_section("Modbus TCP"))
        if modbus_server_init_lines:
            lines.extend(modbus_server_init_lines)
        if modbus_client_codepy_lines:
            lines.extend(modbus_client_codepy_lines)
    else:
        if modbus_server_lines or modbus_client_lines:
            lines.append(_section("Modbus TCP"))
        if modbus_server_lines:
            lines.extend(modbus_server_lines)
        if modbus_client_lines:
            lines.extend(modbus_client_lines)

    # 7) SD mount + load memory startup call
    lines.append(_section("Retentive memory (SD card)"))
    ret_globals = sorted(
        {ctx.global_symbol_for_tag(tag) for _, tag in sorted(ctx.retentive_tags.items())}
    )
    snapshot_globals = ["_ret_snapshot", "_ret_last_save_ns"]
    load_globals = ", ".join(
        ret_globals + ["_sd_write_status", "_sd_error", "_sd_error_code"] + snapshot_globals
    )
    save_globals = load_globals
    # Build snapshot dict literal: {"Count": _t_Count, "Total": _t_Total}
    snapshot_entries = ", ".join(
        f'"{name}": {ctx.symbol_for_tag(tag)}' for name, tag in sorted(ctx.retentive_tags.items())
    )
    snapshot_literal = "{" + snapshot_entries + "}"
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
            "        try:",
            '            with open(_MEMORY_BAK_PATH, "r", encoding="utf-8") as f:',
            "                payload = json.load(f)",
            '            print("Retentive memory recovered from backup")',
            "        except Exception:",
            "            _sd_error = True",
            f"            _sd_error_code = {_SD_LOAD_ERROR}",
            "            _sd_write_status = False",
            '            print("Retentive load skipped: interrupted save, no backup available")',
            "            return",
            "        microcontroller.nvm[0] = 0",
            "    else:",
            "        try:",
            '            with open(_MEMORY_PATH, "r", encoding="utf-8") as f:',
            "                payload = json.load(f)",
            "        except Exception as exc:",
            "            try:",
            '                with open(_MEMORY_BAK_PATH, "r", encoding="utf-8") as f:',
            "                    payload = json.load(f)",
            '                print(f"Retentive memory loaded from backup ({exc})")',
            "            except Exception:",
            "                _sd_error = True",
            f"                _sd_error_code = {_SD_LOAD_ERROR}",
            "                _sd_write_status = False",
            '                print(f"Retentive load skipped: {exc}")',
            "                return",
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
            '    print("Retentive memory loaded")',
            "    _sd_error = False",
            "    _sd_error_code = 0",
            "    _sd_write_status = False",
            f"    _ret_snapshot = {snapshot_literal}",
            "    _ret_last_save_ns = time.monotonic_ns()",
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
            "        try:",
            "            os.rename(_MEMORY_PATH, _MEMORY_BAK_PATH)",
            "        except OSError:",
            "            pass",
            "        os.rename(_MEMORY_TMP_PATH, _MEMORY_PATH)",
            "    except Exception as exc:",
            "        _sd_error = True",
            f"        _sd_error_code = {_SD_SAVE_ERROR}",
            "        _sd_write_status = False",
            '        print(f"Retentive save failed: {exc}")',
            "        return",
            "    if dirty_armed:",
            "        microcontroller.nvm[0] = 0",
            "    try:",
            "        os.remove(_MEMORY_BAK_PATH)",
            "    except OSError:",
            "        pass",
            "    _sd_error = False",
            "    _sd_error_code = 0",
            "    _sd_write_status = False",
            f"    _ret_snapshot = {snapshot_literal}",
            "    _ret_last_save_ns = time.monotonic_ns()",
            "",
            "_mount_sd()",
            "load_memory()",
            "",
        ]
    )

    # 8) helper definitions
    lines.append(_section("Helpers"))
    lines.extend(helper_lines)
    if modbus_accessor_lines:
        lines.append(_section("Modbus address mapping"))
        lines.extend(modbus_accessor_lines)
        if has_runtime:
            lines.extend(_render_runtime_wiring(ctx))
        else:
            lines.extend(modbus_protocol_lines)

    # 9) embedded user function sources
    lines.extend(function_source_lines)

    # 10) compiled subroutine functions
    lines.extend(sub_fn_lines)

    # 11) compiled main-rung function
    lines.append(_section("Ladder logic"))
    lines.extend(main_fn_lines)

    # 12) scan-time I/O read/write helpers
    lines.append(_section("I/O"))
    lines.extend(io_lines)

    # 13) main scan loop
    lines.append(_section("Main scan loop"))
    lines.extend(_render_scan_loop(ctx, has_runtime=has_runtime))

    return "\n".join(lines).rstrip() + "\n"


def _render_helper_section(ctx: CodegenContext, *, helpers_in_runtime: bool = False) -> list[str]:
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
        "            for _path in (_MEMORY_PATH, _MEMORY_TMP_PATH, _MEMORY_BAK_PATH):",
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
                "_ret_snapshot",
                "_sd_save_cmd",
                "_sd_eject_cmd",
                "_sd_delete_all_cmd",
                *scalar_symbols,
                *_system_point_reset_globals(ctx),
            }
        )
        lines.append("def _reset_for_run_transition():")
        lines.append(f"    global {', '.join(reset_globals)}")
        lines.extend(
            [
                "    _mem = {}",
                "    _prev = {}",
                "    _ret_snapshot = {}",
                "    _sd_save_cmd = False",
                "    _sd_eject_cmd = False",
                "    _sd_delete_all_cmd = False",
                *_render_system_point_reset_lines(ctx),
            ]
        )
        if reset_lines:
            lines.extend(reset_lines)
        else:
            lines.append("    pass")
        lines.append("")

    if ctx.runstop is not None or _math_stop_symbol(ctx) is not None:
        scalar_symbols = [ctx.symbol_table[name] for name in sorted(ctx.scalar_tags)]
        lines.append("def _force_outputs_off():")
        if scalar_symbols:
            lines.append(f"    global {', '.join(sorted(set(scalar_symbols)))}")
        output_clear_lines = _render_stop_output_clear_lines(ctx)
        if output_clear_lines:
            lines.extend(output_clear_lines)
        lines.append("    _write_outputs()")
        lines.append("")

    address_fault_symbol = ctx.symbol_if_referenced(_FAULT_ADDRESS_ERROR_TAG)
    address_fault_global = (
        ctx.global_symbol_for_tag(ctx.referenced_tags[_FAULT_ADDRESS_ERROR_TAG])
        if address_fault_symbol is not None
        else None
    )
    for binding in sorted(
        (ctx.block_bindings[bid] for bid in ctx.used_indirect_blocks),
        key=lambda b: ctx.index_helper_name(b.block_id),
    ):
        helper_name = ctx.index_helper_name(binding.block_id)
        lines.append(f"def {helper_name}(addr):")
        # An out-of-range pointer raises the address-error flag (SC44), as the
        # kernel's copy of this helper does (render_kernel.py).
        if address_fault_global is not None:
            lines.append(f"    global {address_fault_global}")
        lines.append(f"    if addr < {binding.start} or addr > {binding.end}:")
        if address_fault_symbol is not None:
            lines.append(f"        {address_fault_symbol} = True")
        lines.append(
            f'        raise IndexError(f"Address {{addr}} out of range for {binding.logical_name} ({binding.start}-{binding.end})")'
        )
        if binding.valid_addresses is not None:
            lines.append(f"    if addr not in {binding.valid_addresses!r}:")
            if address_fault_symbol is not None:
                lines.append(f"        {address_fault_symbol} = True")
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
            '        return "\\x00"',
            "    if len(value) != 1 or ord(value) > 127:",
            '        raise ValueError("CHAR value must be blank or one ASCII character")',
            "    return value",
            "",
        ],
        "_text_from_source_value": [
            "def _text_from_source_value(value):",
            "    if isinstance(value, str):",
            '        return value if value != "" else "\\x00"',
            '    raise ValueError("text conversion source must resolve to str")',
            "",
        ],
        "_store_numeric_text_digit": [
            "def _store_numeric_text_digit(char, mode):",
            "    _char = _as_single_ascii_char(char)",
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
            "        return _store_real(value)",
            '    if dest_type == "BOOL":',
            "        return bool(value)",
            '    if dest_type == "CHAR":',
            "        if not isinstance(value, str):",
            '            raise ValueError("CHAR value must be a string")',
            '        if value == "":',
            '            return "\\x00"',
            "        if len(value) != 1 or ord(value) > 127:",
            '            raise ValueError("CHAR value must be blank or one ASCII character")',
            "        return value",
            "    return value",
            "",
        ],
        "_store_int": [
            "def _store_int(value):",
            "    if type(value) is int:",
            f"        return {_INT_MAX} if value > {_INT_MAX} else ({_INT_MIN} if value < {_INT_MIN} else value)",
            "    if type(value) is float and not math.isfinite(value):",
            "        return 0",
            f"    return max({_INT_MIN}, min({_INT_MAX}, int(value)))",
            "",
        ],
        "_store_dint": [
            "def _store_dint(value):",
            "    if type(value) is int:",
            f"        return {_DINT_MAX} if value > {_DINT_MAX} else ({_DINT_MIN} if value < {_DINT_MIN} else value)",
            "    if type(value) is float and not math.isfinite(value):",
            "        return 0",
            f"    return max({_DINT_MIN}, min({_DINT_MAX}, int(value)))",
            "",
        ],
        "_store_word": [
            "def _store_word(value):",
            "    if type(value) is int:",
            "        return value & 0xFFFF",
            "    if type(value) is float and not math.isfinite(value):",
            "        return 0",
            "    return int(value) & 0xFFFF",
            "",
        ],
        "_store_real": [
            "def _store_real(value):",
            "    if type(value) is float:",
            "        if not math.isfinite(value):",
            "            return 0.0",
            "    else:",
            "        try:",
            "            value = float(value)",
            "        except OverflowError:",
            f"            return {REAL_MAX!r} if value > 0 else {-REAL_MAX!r}",
            f"    if value > {REAL_MAX!r}:",
            f"        return {REAL_MAX!r}",
            f"    if value < {-REAL_MAX!r}:",
            f"        return {-REAL_MAX!r}",
            "    return value",
            "",
        ],
        "_math_func": [
            "class _MathDomainError(ValueError):",
            "    pass",
            "",
            "",
            "def _math_func(func, value):",
            "    if type(value) is float and not math.isfinite(value):",
            "        return value",
            "    try:",
            "        return func(value)",
            "    except ValueError:",
            "        raise _MathDomainError()",
            "",
        ],
        "_store_bool": [
            "def _store_bool(value):",
            "    if type(value) is float and not math.isfinite(value):",
            "        return False",
            "    return bool(value)",
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
    if "_store_copy_value_to_type" in needed_helpers:
        needed_helpers.add("_store_real")

    if not helpers_in_runtime:
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
        body = compile_rungs(ctx.program.subroutines[sub_name], fn_name, ctx, indent=4)
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
    body = compile_rungs(ctx.program.rungs, fn_name, ctx, indent=4)
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


def _render_scan_loop(ctx: CodegenContext, *, has_runtime: bool = False) -> list[str]:
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
    math_error_symbol = _math_stop_symbol(ctx)
    mode_switch_symbol = ctx.symbol_if_referenced(system.sys.mode_switch_run.name)

    lines = [
        "while True:",
        "    scan_start_ns = time.monotonic_ns()",
        "    _sd_write_status = False",
        # dt in seconds, as core's timers take it, from the exact ns difference.
        "    dt = (scan_start_ns - _last_scan_ns) / 1000000000",
        "    _last_scan_ns = scan_start_ns",
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
        debounce_ns = ctx.runstop.debounce_ms * 1000000
        lines.extend(
            [
                f"    _runstop_sample = bool({runstop_source_symbol})",
                "    if not _runstop_initialized:",
                "        _runstop_raw = _runstop_sample",
                "        _runstop_debounced = _runstop_sample",
                "        _runstop_last_change_ns = scan_start_ns",
                "        _runstop_initialized = True",
                "    elif _runstop_sample != _runstop_raw:",
                "        _runstop_raw = _runstop_sample",
                "        _runstop_last_change_ns = scan_start_ns",
                f"    elif (scan_start_ns - _runstop_last_change_ns) >= {debounce_ns}:",
                "        _runstop_debounced = _runstop_raw",
                "",
            ]
        )
        if ctx.runstop.run_when_high:
            lines.append("    _desired_run = bool(_runstop_debounced)")
        else:
            lines.append("    _desired_run = (not bool(_runstop_debounced))")
        if math_error_symbol is not None:
            # A math error put the code in STOP at the end of its scan
            # (_render_math_stop_lines); the hold keeps STOP while the switch
            # says RUN.  The switch reading STOP releases it, and its next RUN
            # resets SC46 and SC50 with the other tags, as core's STOP->RUN.
            lines.extend(
                [
                    "    if _math_stop_hold:",
                    "        if _desired_run:",
                    "            _desired_run = False",
                    "        else:",
                    "            _math_stop_hold = False",
                ]
            )
        if ctx.runstop.expose_mode_tags and cmd_mode_stop_symbol is not None:
            # SC50 on with SC46 is the math error's STOP, not a command: it
            # reads on until RUN.
            cmd_condition = (
                f"bool({cmd_mode_stop_symbol})"
                if math_error_symbol is None
                else f"bool({cmd_mode_stop_symbol}) and not {math_error_symbol}"
            )
            lines.extend(
                [
                    f"    if {cmd_condition}:",
                    "        _desired_run = False",
                    f"        {cmd_mode_stop_symbol} = False",
                ]
            )
        lines.extend(
            [
                "    if _desired_run != _mode_run:",
                "        if _desired_run:",
                "            _reset_for_run_transition()",
                # The first RUN scan times the loop pass it runs in, as core's
                # first scan after start() advances by a full dt.
                '            _mem["_dt"] = dt',
                '            print("Mode: RUN")',
                "        else:",
                "            save_memory()",
                '            print("Mode: STOP")',
                "        _mode_run = _desired_run",
            ]
        )
        if mode_switch_symbol is not None:
            # SC10 is the switch itself, set after the STOP->RUN reset: on in
            # RUN even while a math error or cmd_mode_stop holds the PLC in STOP.
            switch_run = (
                "bool(_runstop_debounced)"
                if ctx.runstop.run_when_high
                else "(not bool(_runstop_debounced))"
            )
            lines.append(f"    {mode_switch_symbol} = {switch_run}")
        if ctx.runstop.expose_mode_tags and mode_run_symbol is not None:
            lines.append(f"    {mode_run_symbol} = bool(_mode_run)")
        lines.extend(
            [
                "    if _mode_run:",
                *_render_system_point_scan_lines(ctx, 8),
                "        _run_main_rungs()",
                "        _write_outputs()",
                *_render_system_point_advance_lines(ctx, 8),
                *_render_math_stop_lines(ctx, 8),
                "    else:",
                "        _force_outputs_off()",
                "",
            ]
        )
    elif math_error_symbol is None:
        # No RUN/STOP switch: the code is always in RUN, as core's mode bits
        # read with no mode change (SC10 and SC11 on).
        for symbol in (mode_switch_symbol, mode_run_symbol):
            if symbol is not None:
                lines.append(f"    {symbol} = True")
        lines.extend(
            [
                *_render_system_point_scan_lines(ctx, 4),
                "    _run_main_rungs()",
                "    _write_outputs()",
                *_render_system_point_advance_lines(ctx, 4),
                "",
            ]
        )
    else:
        # No RUN/STOP switch, but a math error still stops the code, as on a
        # CLICK; with no switch to turn back to RUN, only a restart does.
        if mode_switch_symbol is not None:
            lines.append(f"    {mode_switch_symbol} = True")
        lines.append("    if _mode_run:")
        if mode_run_symbol is not None:
            lines.append(f"        {mode_run_symbol} = True")
        lines.extend(
            [
                *_render_system_point_scan_lines(ctx, 8),
                "        _run_main_rungs()",
                "        _write_outputs()",
                *_render_system_point_advance_lines(ctx, 8),
                *_render_math_stop_lines(ctx, 8),
                "    else:",
                "        _force_outputs_off()",
                "",
            ]
        )
    if ctx.modbus_server is not None:
        if has_runtime:
            lines.append("    _rt.service_modbus_server()")
        else:
            lines.append("    service_modbus_server()")
    if ctx.modbus_client is not None:
        if has_runtime:
            lines.append("    _rt.service_modbus_client()")
        else:
            lines.append("    service_modbus_client()")
    if ctx.modbus_server is not None or ctx.modbus_client is not None:
        lines.append("")

    if ctx.retentive_tags:
        dirty_parts = []
        for name, tag in sorted(ctx.retentive_tags.items()):
            symbol = ctx.symbol_for_tag(tag)
            dirty_parts.append(f'{symbol} != _ret_snapshot.get("{name}")')
        dirty_check = " or ".join(dirty_parts)
        lines.extend(
            [
                "    if (scan_start_ns - _ret_last_save_ns) >= _RET_AUTO_SAVE_NS:",
                f"        if {dirty_check}:",
                "            save_memory()",
                "        else:",
                "            _ret_last_save_ns = scan_start_ns",
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
            "    elapsed_ms = (time.monotonic_ns() - scan_start_ns) / 1000000",
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
