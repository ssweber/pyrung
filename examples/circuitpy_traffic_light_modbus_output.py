# -- Imports -------------------------------------------------------------------
import json
import math
import os
import re
import struct
import time

import board
import busio
import P1AM
import sdcardio
import storage

import digitalio

from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K
import adafruit_wiznet5k.adafruit_wiznet5k_socketpool as _mb_socketpool

try:
    import microcontroller
except ImportError:
    microcontroller = None

import pyrung_rt as _rt
from pyrung_rt import _MB_CLIENT_IDLE, _mb_client_pack_register_values, _mb_client_raw_pack_register_values, _mb_client_raw_unpack_register_values, _mb_client_unpack_register_values, _store_bool, _store_copy_value_to_type

# -- Configuration -------------------------------------------------------------
TARGET_SCAN_MS = 10.0
WATCHDOG_MS = 5000
PRINT_SCAN_OVERRUNS = False

_SLOT_MODULES = ['P1-08SIM', 'P1-15TD2']
_RET_DEFAULTS = {'State': 'r'}
_RET_TYPES = {'State': 'CHAR'}
_RET_SCHEMA = "a603a70741819cfb57cb0a860df25ed01542841ea1af785607532037d2428ddc"

# -- Hardware bootstrap --------------------------------------------------------
base = P1AM.Base()
base.rollCall(_SLOT_MODULES)

_mb_cs = digitalio.DigitalInOut(board.D5)
_mb_spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
_mb_eth = WIZNET5K(_mb_spi, _mb_cs)
_mb_eth.ifconfig = ((192, 168, 1, 221), (255, 255, 255, 0), (192, 168, 1, 1), (0, 0, 0, 0))
_mb_pool = _mb_socketpool.SocketPool(_mb_eth)

_board_switch_io = digitalio.DigitalInOut(board.SWITCH)
_board_switch_io.direction = digitalio.Direction.INPUT

_wd_config = getattr(base, "config_watchdog", None)
_wd_start = getattr(base, "start_watchdog", None)
_wd_pet = getattr(base, "pet_watchdog", None)
if _wd_config is None or _wd_start is None or _wd_pet is None:
    raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")
_wd_config(WATCHDOG_MS)
_wd_start()

# -- Tags and blocks -----------------------------------------------------------
# Scalars (non-block tags).
_t_GreenTimer_Acc = 0
_t_GreenTimer_Done = False
_t_GreenTimer_EN = False
_t_GreenTimer_TT = False
_t_RedTimer_Acc = 0
_t_RedTimer_Done = False
_t_RedTimer_EN = False
_t_RedTimer_TT = False
_t_RxBusy = False
_t_RxErr = False
_t_RxExCode = 0
_t_RxOk = False
_t_State = 'r'
_t_WalkActive = False
_t_WalkRequest = False
_t_YellowTimer_Acc = 0
_t_YellowTimer_Done = False
_t_YellowTimer_EN = False
_t_YellowTimer_TT = False
_t_board_switch = False
_t_fault_address_error = False
_t_fault_code = 0
_t_fault_division_error = False
_t_fault_math_operation_error = False
_t_fault_out_of_range = False
_t_fault_plc_error = False
_t_firmware_main_ver_high = 0
_t_firmware_main_ver_low = 0
_t_firmware_sub_ver_high = 0
_t_firmware_sub_ver_low = 0
_t_rtc_apply_date = False
_t_rtc_apply_date_error = False
_t_rtc_apply_time = False
_t_rtc_apply_time_error = False
_t_rtc_day = 0
_t_rtc_hour = 0
_t_rtc_minute = 0
_t_rtc_month = 0
_t_rtc_new_day = 0
_t_rtc_new_hour = 0
_t_rtc_new_minute = 0
_t_rtc_new_month = 0
_t_rtc_new_second = 0
_t_rtc_new_year4 = 0
_t_rtc_second = 0
_t_rtc_weekday = 0
_t_rtc_year2 = 0
_t_rtc_year4 = 0
_t_storage_sd_delete_all_cmd = False
_t_storage_sd_eject_cmd = False
_t_storage_sd_error = False
_t_storage_sd_error_code = 0
_t_storage_sd_ready = False
_t_storage_sd_write_status = False
_t_sys_always_on = False
_t_sys_battery_present = False
_t_sys_clock_100ms = False
_t_sys_clock_10ms = False
_t_sys_clock_1h = False
_t_sys_clock_1m = False
_t_sys_clock_1s = False
_t_sys_clock_500ms = False
_t_sys_cmd_mode_stop = False
_t_sys_cmd_watchdog_reset = False
_t_sys_first_scan = False
_t_sys_fixed_scan_mode = False
_t_sys_interrupt_scan_time_ms = 0
_t_sys_mode_run = False
_t_sys_mode_switch_run = False
_t_sys_scan_clock_toggle = False
_t_sys_scan_counter = 0
_t_sys_scan_time_current_ms = 0
_t_sys_scan_time_fixed_setup_ms = 0
_t_sys_scan_time_max_ms = 0
_t_sys_scan_time_min_ms = 0

# Blocks (list-backed; PLC addresses remain 1-based, list indexes are 0-based).
_b_Slot1 = [False] * 8
_b_Slot2 = [False] * 15

_mem = {}
_last_scan_ns = time.monotonic_ns()
_scan_overrun_count = 0

_sd_available = False
_MEMORY_PATH = "/sd/memory.json"
_MEMORY_TMP_PATH = "/sd/_memory.tmp"
_MEMORY_BAK_PATH = "/sd/memory.json.bak"
_sd_spi = None
_sd = None
_sd_vfs = None
_sd_write_status = False
_sd_error = False
_sd_error_code = 0
_sd_save_cmd = False
_sd_eject_cmd = False
_sd_delete_all_cmd = False
_ret_snapshot = {}
_ret_last_save_ns = 0
_RET_AUTO_SAVE_NS = 30000000000

_mode_run = True
_stop_hold = False
_runstop_initialized = False
_runstop_raw = False
_runstop_debounced = False
_runstop_last_change_ns = 0

_sys_scan_n = 0
_sys_first = True
_sys_t0_ns = 0
_sys_prev_ns = 0

# -- Modbus TCP ----------------------------------------------------------------
_mb_server = _mb_pool.socket()
_mb_server.bind(('', 502))
_mb_server.listen(2)
_mb_server.settimeout(0)
_mb_clients = [None] * 2
_mb_buf = bytearray(260)

def _mb_client_i12_set_status(busy, success, error, exception_response):
    global _t_RxBusy, _t_RxErr, _t_RxExCode, _t_RxOk
    _t_RxBusy = bool(busy)
    _t_RxOk = bool(success)
    _t_RxErr = bool(error)
    _t_RxExCode = int(exception_response)

def _mb_client_i12_values():
    global _t_WalkRequest
    return [_t_WalkRequest]

def _mb_client_i12_build_request(tid):  # read coils: coil 0x0000 (1 coil) on ped_panel
    _pdu = struct.pack('>BHH', 1, 0, 1)
    return struct.pack('>HHHB', int(tid) & 0xFFFF, 0, len(_pdu) + 1, 1) + _pdu

def _mb_client_i12_apply_response(data, n):
    global _t_WalkRequest
    if int(n) < 8:
        return (False, 0)
    try:
        _tid, _pid, _length, _uid = struct.unpack('>HHHB', bytes(data[:7]))
    except Exception:
        return (False, 0)
    if _pid != 0 or _uid != 1:
        return (False, 0)
    if _tid != int(_mb_client_i12['tid']):
        return (False, 0)
    _frame_len = 6 + int(_length)
    if _frame_len > int(n) or _frame_len < 8:
        return (False, 0)
    _fc = int(data[7])
    if _fc & 0x80:
        if _frame_len < 9:
            return (False, 0)
        return (False, int(data[8]))
    if _fc != 1:
        return (False, 0)
    if _frame_len < 9:
        return (False, 0)
    _byte_count = int(data[8])
    if _frame_len < 9 + _byte_count:
        return (False, 0)
    _values = []
    for _offset in range(1):
        _byte = int(data[9 + (_offset // 8)])
        _values.append(bool((_byte >> (_offset % 8)) & 0x1))
    _t_WalkRequest = _store_bool(_values[0])
    return (True, 0)

_mb_client_i12 = {
    'name': '_mb_client_i12',
    'enabled': False,
    'state': _MB_CLIENT_IDLE,
    'socket': None,
    'request': b'',
    'sent_offset': 0,
    'rx_buf': bytearray(260),
    'rx_len': 0,
    'deadline': 0.0,
    'tid': 0,
    'host': '192.168.1.122',
    'port': 502,
    'timeout_s': 1.0,
    'build': _mb_client_i12_build_request,
    'apply': _mb_client_i12_apply_response,
    'set_status': _mb_client_i12_set_status,
}

_mb_client_jobs = [_mb_client_i12]

# -- Retentive memory (SD card) ------------------------------------------------
def _mount_sd():
    global _sd_available, _sd_spi, _sd, _sd_vfs, _sd_error, _sd_error_code
    try:
        _sd_spi = busio.SPI(board.SD_SCK, board.SD_MOSI, board.SD_MISO)
        _sd = sdcardio.SDCard(_sd_spi, board.SD_CS)
        _sd_vfs = storage.VfsFat(_sd)
        storage.mount(_sd_vfs, "/sd")
        _sd_available = True
        _sd_error = False
        _sd_error_code = 0
    except Exception as exc:
        _sd_available = False
        _sd_error = True
        _sd_error_code = 1
        print(f"Retentive storage unavailable: {exc}")

def load_memory():
    global _t_State, _sd_write_status, _sd_error, _sd_error_code, _ret_snapshot, _ret_last_save_ns
    if not _sd_available:
        print("Retentive load skipped: SD unavailable")
        return
    _sd_write_status = True
    if microcontroller is not None and len(microcontroller.nvm) > 0 and microcontroller.nvm[0] == 1:
        try:
            with open(_MEMORY_BAK_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
            print("Retentive memory recovered from backup")
        except Exception:
            _sd_error = True
            _sd_error_code = 2
            _sd_write_status = False
            print("Retentive load skipped: interrupted save, no backup available")
            return
        microcontroller.nvm[0] = 0
    else:
        try:
            with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as exc:
            try:
                with open(_MEMORY_BAK_PATH, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                print(f"Retentive memory loaded from backup ({exc})")
            except Exception:
                _sd_error = True
                _sd_error_code = 2
                _sd_write_status = False
                print(f"Retentive load skipped: {exc}")
                return
    if payload.get("schema") != _RET_SCHEMA:
        _sd_error = True
        _sd_error_code = 2
        _sd_write_status = False
        print("Retentive load skipped: schema mismatch")
        return
    values = payload.get("values", {})
    _entry = values.get("State")
    if isinstance(_entry, dict) and _entry.get("type") == "CHAR":
        try:
            _t_State = ((_entry.get("value", _t_State) or '\x00') if isinstance(_entry.get("value", _t_State), str) else '\x00')
        except Exception:
            pass
    print("Retentive memory loaded")
    _sd_error = False
    _sd_error_code = 0
    _sd_write_status = False
    _ret_snapshot = {"State": _t_State}
    _ret_last_save_ns = time.monotonic_ns()

def save_memory():
    global _t_State, _sd_write_status, _sd_error, _sd_error_code, _ret_snapshot, _ret_last_save_ns
    if not _sd_available:
        return
    _sd_write_status = True
    values = {}
    if _t_State != _RET_DEFAULTS["State"]:
        values["State"] = {"type": "CHAR", "value": _t_State}
    payload = {"schema": _RET_SCHEMA, "values": values}
    dirty_armed = False
    if microcontroller is not None and len(microcontroller.nvm) > 0:
        microcontroller.nvm[0] = 1
        dirty_armed = True
    try:
        with open(_MEMORY_TMP_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        try:
            os.rename(_MEMORY_PATH, _MEMORY_BAK_PATH)
        except OSError:
            pass
        os.rename(_MEMORY_TMP_PATH, _MEMORY_PATH)
    except Exception as exc:
        _sd_error = True
        _sd_error_code = 3
        _sd_write_status = False
        print(f"Retentive save failed: {exc}")
        return
    if dirty_armed:
        microcontroller.nvm[0] = 0
    try:
        os.remove(_MEMORY_BAK_PATH)
    except OSError:
        pass
    _sd_error = False
    _sd_error_code = 0
    _sd_write_status = False
    _ret_snapshot = {"State": _t_State}
    _ret_last_save_ns = time.monotonic_ns()

_mount_sd()
load_memory()

# -- Helpers -------------------------------------------------------------------
def _service_sd_commands():
    global _sd_write_status, _sd_error, _sd_error_code
    global _sd_save_cmd, _sd_eject_cmd, _sd_delete_all_cmd
    global _sd_available, _sd_spi, _sd, _sd_vfs
    if not (_sd_save_cmd or _sd_eject_cmd or _sd_delete_all_cmd):
        return
    _do_delete = bool(_sd_delete_all_cmd)
    _do_save = bool(_sd_save_cmd)
    _do_eject = bool(_sd_eject_cmd)
    _sd_save_cmd = False
    _sd_eject_cmd = False
    _sd_delete_all_cmd = False
    _sd_write_status = True
    _command_failed = False
    if _do_delete:
        try:
            for _path in (_MEMORY_PATH, _MEMORY_TMP_PATH, _MEMORY_BAK_PATH):
                try:
                    os.remove(_path)
                except OSError:
                    pass
        except Exception as exc:
            _command_failed = True
            _sd_error = True
            _sd_error_code = 3
            print(f"SD delete_all command failed: {exc}")
    if _do_save:
        try:
            save_memory()
            if _sd_error and _sd_error_code == 3:
                _command_failed = True
        except Exception as exc:
            _command_failed = True
            _sd_error = True
            _sd_error_code = 3
            print(f"SD save command failed: {exc}")
    if _do_eject:
        try:
            if _sd_available:
                storage.umount("/sd")
            _sd_available = False
            _sd_spi = None
            _sd = None
            _sd_vfs = None
        except Exception as exc:
            _command_failed = True
            _sd_error = True
            _sd_error_code = 3
            print(f"SD eject command failed: {exc}")
    if not _command_failed:
        _sd_error = False
        _sd_error_code = 0
    # SC69 pulses for this serviced-command scan; reset occurs at next scan start.
    _sd_write_status = True

def _reset_for_run_transition():
    global _mem, _ret_snapshot, _sd_delete_all_cmd, _sd_eject_cmd, _sd_save_cmd, _sys_first, _sys_scan_n, _t_GreenTimer_Acc, _t_GreenTimer_Done, _t_GreenTimer_EN, _t_GreenTimer_TT, _t_RedTimer_Acc, _t_RedTimer_Done, _t_RedTimer_EN, _t_RedTimer_TT, _t_RxBusy, _t_RxErr, _t_RxExCode, _t_RxOk, _t_State, _t_WalkActive, _t_WalkRequest, _t_YellowTimer_Acc, _t_YellowTimer_Done, _t_YellowTimer_EN, _t_YellowTimer_TT, _t_board_switch, _t_fault_address_error, _t_fault_code, _t_fault_division_error, _t_fault_math_operation_error, _t_fault_out_of_range, _t_fault_plc_error, _t_firmware_main_ver_high, _t_firmware_main_ver_low, _t_firmware_sub_ver_high, _t_firmware_sub_ver_low, _t_rtc_apply_date, _t_rtc_apply_date_error, _t_rtc_apply_time, _t_rtc_apply_time_error, _t_rtc_day, _t_rtc_hour, _t_rtc_minute, _t_rtc_month, _t_rtc_new_day, _t_rtc_new_hour, _t_rtc_new_minute, _t_rtc_new_month, _t_rtc_new_second, _t_rtc_new_year4, _t_rtc_second, _t_rtc_weekday, _t_rtc_year2, _t_rtc_year4, _t_storage_sd_delete_all_cmd, _t_storage_sd_eject_cmd, _t_storage_sd_error, _t_storage_sd_error_code, _t_storage_sd_ready, _t_storage_sd_write_status, _t_sys_always_on, _t_sys_battery_present, _t_sys_clock_100ms, _t_sys_clock_10ms, _t_sys_clock_1h, _t_sys_clock_1m, _t_sys_clock_1s, _t_sys_clock_500ms, _t_sys_cmd_mode_stop, _t_sys_cmd_watchdog_reset, _t_sys_first_scan, _t_sys_fixed_scan_mode, _t_sys_interrupt_scan_time_ms, _t_sys_mode_run, _t_sys_mode_switch_run, _t_sys_scan_clock_toggle, _t_sys_scan_counter, _t_sys_scan_time_current_ms, _t_sys_scan_time_fixed_setup_ms, _t_sys_scan_time_max_ms, _t_sys_scan_time_min_ms
    _mem = {}
    _ret_snapshot = {}
    _sd_save_cmd = False
    _sd_eject_cmd = False
    _sd_delete_all_cmd = False
    _sys_scan_n = 0
    _sys_first = True
    _t_GreenTimer_Acc = 0
    _t_GreenTimer_Done = False
    _t_GreenTimer_EN = False
    _t_GreenTimer_TT = False
    _t_RedTimer_Acc = 0
    _t_RedTimer_Done = False
    _t_RedTimer_EN = False
    _t_RedTimer_TT = False
    _t_RxBusy = False
    _t_RxErr = False
    _t_RxExCode = 0
    _t_RxOk = False
    _t_WalkActive = False
    _t_WalkRequest = False
    _t_YellowTimer_Acc = 0
    _t_YellowTimer_Done = False
    _t_YellowTimer_EN = False
    _t_YellowTimer_TT = False
    _t_fault_address_error = False
    _t_fault_code = 0
    _t_fault_division_error = False
    _t_fault_math_operation_error = False
    _t_fault_out_of_range = False
    _t_fault_plc_error = False
    _t_firmware_main_ver_high = 0
    _t_firmware_main_ver_low = 0
    _t_firmware_sub_ver_high = 0
    _t_firmware_sub_ver_low = 0
    _t_rtc_apply_date = False
    _t_rtc_apply_date_error = False
    _t_rtc_apply_time = False
    _t_rtc_apply_time_error = False
    _t_rtc_day = 0
    _t_rtc_hour = 0
    _t_rtc_minute = 0
    _t_rtc_month = 0
    _t_rtc_new_day = 0
    _t_rtc_new_hour = 0
    _t_rtc_new_minute = 0
    _t_rtc_new_month = 0
    _t_rtc_new_second = 0
    _t_rtc_new_year4 = 0
    _t_rtc_second = 0
    _t_rtc_weekday = 0
    _t_rtc_year2 = 0
    _t_rtc_year4 = 0
    _t_storage_sd_delete_all_cmd = False
    _t_storage_sd_eject_cmd = False
    _t_storage_sd_error = False
    _t_storage_sd_error_code = 0
    _t_storage_sd_ready = False
    _t_storage_sd_write_status = False
    _t_sys_always_on = False
    _t_sys_battery_present = False
    _t_sys_clock_100ms = False
    _t_sys_clock_10ms = False
    _t_sys_clock_1h = False
    _t_sys_clock_1m = False
    _t_sys_clock_1s = False
    _t_sys_clock_500ms = False
    _t_sys_cmd_mode_stop = False
    _t_sys_cmd_watchdog_reset = False
    _t_sys_first_scan = False
    _t_sys_fixed_scan_mode = False
    _t_sys_interrupt_scan_time_ms = 0
    _t_sys_mode_run = False
    _t_sys_mode_switch_run = False
    _t_sys_scan_clock_toggle = False
    _t_sys_scan_counter = 0
    _t_sys_scan_time_current_ms = 0
    _t_sys_scan_time_fixed_setup_ms = 0
    _t_sys_scan_time_max_ms = 0
    _t_sys_scan_time_min_ms = 0
    for _i in range(0, 15):
        _b_Slot2[_i] = False

def _force_outputs_off():
    global _t_GreenTimer_Acc, _t_GreenTimer_Done, _t_GreenTimer_EN, _t_GreenTimer_TT, _t_RedTimer_Acc, _t_RedTimer_Done, _t_RedTimer_EN, _t_RedTimer_TT, _t_RxBusy, _t_RxErr, _t_RxExCode, _t_RxOk, _t_State, _t_WalkActive, _t_WalkRequest, _t_YellowTimer_Acc, _t_YellowTimer_Done, _t_YellowTimer_EN, _t_YellowTimer_TT, _t_board_switch, _t_fault_address_error, _t_fault_code, _t_fault_division_error, _t_fault_math_operation_error, _t_fault_out_of_range, _t_fault_plc_error, _t_firmware_main_ver_high, _t_firmware_main_ver_low, _t_firmware_sub_ver_high, _t_firmware_sub_ver_low, _t_rtc_apply_date, _t_rtc_apply_date_error, _t_rtc_apply_time, _t_rtc_apply_time_error, _t_rtc_day, _t_rtc_hour, _t_rtc_minute, _t_rtc_month, _t_rtc_new_day, _t_rtc_new_hour, _t_rtc_new_minute, _t_rtc_new_month, _t_rtc_new_second, _t_rtc_new_year4, _t_rtc_second, _t_rtc_weekday, _t_rtc_year2, _t_rtc_year4, _t_storage_sd_delete_all_cmd, _t_storage_sd_eject_cmd, _t_storage_sd_error, _t_storage_sd_error_code, _t_storage_sd_ready, _t_storage_sd_write_status, _t_sys_always_on, _t_sys_battery_present, _t_sys_clock_100ms, _t_sys_clock_10ms, _t_sys_clock_1h, _t_sys_clock_1m, _t_sys_clock_1s, _t_sys_clock_500ms, _t_sys_cmd_mode_stop, _t_sys_cmd_watchdog_reset, _t_sys_first_scan, _t_sys_fixed_scan_mode, _t_sys_interrupt_scan_time_ms, _t_sys_mode_run, _t_sys_mode_switch_run, _t_sys_scan_clock_toggle, _t_sys_scan_counter, _t_sys_scan_time_current_ms, _t_sys_scan_time_fixed_setup_ms, _t_sys_scan_time_max_ms, _t_sys_scan_time_min_ms
    for _i in range(15):
        _b_Slot2[_i] = False
    _write_outputs()

# -- Modbus address mapping ----------------------------------------------------
_MB_COIL_MAP = {16384: ('C', 1), 16385: ('C', 2), 16386: ('C', 3), 16387: ('C', 4), 16388: ('C', 5), 61440: ('SC', 1), 61441: ('SC', 2), 61442: ('SC', 3), 61443: ('SC', 4), 61444: ('SC', 5), 61445: ('SC', 6), 61446: ('SC', 7), 61447: ('SC', 8), 61448: ('SC', 9), 61449: ('SC', 10), 61450: ('SC', 11), 61458: ('SC', 19), 61479: ('SC', 40), 61482: ('SC', 43), 61483: ('SC', 44), 61485: ('SC', 46), 61489: ('SC', 50), 61490: ('SC', 51), 61492: ('SC', 53), 61493: ('SC', 54), 61494: ('SC', 55), 61495: ('SC', 56), 61504: ('SC', 65), 61505: ('SC', 66), 61507: ('SC', 68), 61508: ('SC', 69), 61509: ('SC', 70), 61641: ('SC', 202), 61642: ('SC', 203), 45056: ('T', 1), 45057: ('T', 2), 45058: ('T', 3)}
_MB_REG_MAP = {0: ('DS', 1, 0), 61440: ('SD', 1, 0), 61444: ('SD', 5, 0), 61445: ('SD', 6, 0), 61446: ('SD', 7, 0), 61447: ('SD', 8, 0), 61448: ('SD', 9, 0), 61449: ('SD', 10, 0), 61450: ('SD', 11, 0), 61451: ('SD', 12, 0), 61452: ('SD', 13, 0), 61453: ('SD', 14, 0), 61458: ('SD', 19, 0), 61459: ('SD', 20, 0), 61460: ('SD', 21, 0), 61461: ('SD', 22, 0), 61462: ('SD', 23, 0), 61463: ('SD', 24, 0), 61464: ('SD', 25, 0), 61465: ('SD', 26, 0), 61468: ('SD', 29, 0), 61470: ('SD', 31, 0), 61471: ('SD', 32, 0), 61473: ('SD', 34, 0), 61474: ('SD', 35, 0), 61475: ('SD', 36, 0), 61508: ('SD', 69, 0), 45056: ('TD', 1, 0), 45057: ('TD', 2, 0), 45058: ('TD', 3, 0), 36864: ('TXT', 1, 0)}
_MB_COIL_SPANS = ((0, 287), (8192, 8479), (16384, 18383), (45056, 45555), (49152, 49401), (61440, 62439))
_MB_REG_SPANS = ((0, 4499), (16384, 18383), (24576, 25075), (28672, 29671), (36864, 37363), (45056, 45555), (49152, 49651), (61440, 62439))
_MB_COIL_READ_ONLY = ('CT', 'T', 'X')
_MB_SC_WRITABLE = (53, 55, 60, 61, 65, 66, 67, 75, 76, 120, 121)
_MB_SD_WRITABLE = (29, 31, 32, 34, 35, 36, 40, 41, 42, 50, 51, 60, 61, 106, 107, 108, 112, 113, 114, 140, 141, 142, 143, 144, 145, 146, 147, 214, 215)
_MB_WIDE = ('CTD', 'DD', 'DF')
_mb_yd = [0] * 17

def _mb_in(addr, spans):
    for _lo, _hi in spans:
        if addr < _lo:
            return False
        if addr <= _hi:
            return True
    return False

def _mb_read_coil_plc(bank, index):
    if bank == "C" and index == 1:
        return bool(_t_WalkActive)
    if bank == "C" and index == 2:
        return bool(_t_WalkRequest)
    if bank == "C" and index == 3:
        return bool(_t_RxBusy)
    if bank == "C" and index == 4:
        return bool(_t_RxOk)
    if bank == "C" and index == 5:
        return bool(_t_RxErr)
    if bank == "SC" and index == 1:
        return bool(_t_sys_always_on)
    if bank == "SC" and index == 2:
        return bool(_t_sys_first_scan)
    if bank == "SC" and index == 3:
        return bool(_t_sys_scan_clock_toggle)
    if bank == "SC" and index == 4:
        return bool(_t_sys_clock_10ms)
    if bank == "SC" and index == 5:
        return bool(_t_sys_clock_100ms)
    if bank == "SC" and index == 6:
        return bool(_t_sys_clock_500ms)
    if bank == "SC" and index == 7:
        return bool(_t_sys_clock_1s)
    if bank == "SC" and index == 8:
        return bool(_t_sys_clock_1m)
    if bank == "SC" and index == 9:
        return bool(_t_sys_clock_1h)
    if bank == "SC" and index == 10:
        return bool(_t_sys_mode_switch_run)
    if bank == "SC" and index == 11:
        return bool(_mode_run)
    if bank == "SC" and index == 19:
        return bool(_t_fault_plc_error)
    if bank == "SC" and index == 40:
        return bool(_t_fault_division_error)
    if bank == "SC" and index == 43:
        return bool(_t_fault_out_of_range)
    if bank == "SC" and index == 44:
        return bool(_t_fault_address_error)
    if bank == "SC" and index == 46:
        return bool(_t_fault_math_operation_error)
    if bank == "SC" and index == 50:
        return bool(_t_sys_cmd_mode_stop)
    if bank == "SC" and index == 51:
        return bool(_t_sys_cmd_watchdog_reset)
    if bank == "SC" and index == 53:
        return bool(_t_rtc_apply_date)
    if bank == "SC" and index == 54:
        return bool(_t_rtc_apply_date_error)
    if bank == "SC" and index == 55:
        return bool(_t_rtc_apply_time)
    if bank == "SC" and index == 56:
        return bool(_t_rtc_apply_time_error)
    if bank == "SC" and index == 65:
        return bool(_t_storage_sd_eject_cmd)
    if bank == "SC" and index == 66:
        return bool(_t_storage_sd_delete_all_cmd)
    if bank == "SC" and index == 68:
        return bool(_t_storage_sd_ready)
    if bank == "SC" and index == 69:
        return bool(_t_storage_sd_write_status)
    if bank == "SC" and index == 70:
        return bool(_t_storage_sd_error)
    if bank == "SC" and index == 202:
        return bool(_t_sys_fixed_scan_mode)
    if bank == "SC" and index == 203:
        return bool(_t_sys_battery_present)
    if bank == "T" and index == 1:
        return bool(_t_RedTimer_Done)
    if bank == "T" and index == 2:
        return bool(_t_GreenTimer_Done)
    if bank == "T" and index == 3:
        return bool(_t_YellowTimer_Done)
    return False

def _mb_write_coil_plc(bank, index, val):
    global _t_GreenTimer_Done, _t_RedTimer_Done, _t_RxBusy, _t_RxErr, _t_RxOk, _t_WalkActive, _t_WalkRequest, _t_YellowTimer_Done, _t_rtc_apply_date, _t_rtc_apply_time, _t_storage_sd_delete_all_cmd, _t_storage_sd_eject_cmd
    _value = bool(val)
    if bank == "C" and index == 1:
        _t_WalkActive = _value
        return
    if bank == "C" and index == 2:
        _t_WalkRequest = _value
        return
    if bank == "C" and index == 3:
        _t_RxBusy = _value
        return
    if bank == "C" and index == 4:
        _t_RxOk = _value
        return
    if bank == "C" and index == 5:
        _t_RxErr = _value
        return
    if bank == "SC" and index == 53:
        _t_rtc_apply_date = _value
        return
    if bank == "SC" and index == 55:
        _t_rtc_apply_time = _value
        return
    if bank == "SC" and index == 65:
        _t_storage_sd_eject_cmd = _value
        return
    if bank == "SC" and index == 66:
        _t_storage_sd_delete_all_cmd = _value
        return
    if bank == "T" and index == 1:
        _t_RedTimer_Done = _value
        return
    if bank == "T" and index == 2:
        _t_GreenTimer_Done = _value
        return
    if bank == "T" and index == 3:
        _t_YellowTimer_Done = _value
        return

def _mb_read_reg_plc(bank, index, reg_pos):
    if bank == "DS" and index == 1:
        return struct.unpack('<H', struct.pack('<h', int(_t_RxExCode)))[0]
    if bank == "SD" and index == 1:
        return struct.unpack('<H', struct.pack('<h', int(_t_fault_code)))[0]
    if bank == "SD" and index == 5:
        return struct.unpack('<H', struct.pack('<h', int(_t_firmware_main_ver_low)))[0]
    if bank == "SD" and index == 6:
        return struct.unpack('<H', struct.pack('<h', int(_t_firmware_main_ver_high)))[0]
    if bank == "SD" and index == 7:
        return struct.unpack('<H', struct.pack('<h', int(_t_firmware_sub_ver_low)))[0]
    if bank == "SD" and index == 8:
        return struct.unpack('<H', struct.pack('<h', int(_t_firmware_sub_ver_high)))[0]
    if bank == "SD" and index == 9:
        return struct.unpack('<H', struct.pack('<h', int(_t_sys_scan_counter)))[0]
    if bank == "SD" and index == 10:
        return struct.unpack('<H', struct.pack('<h', int(_t_sys_scan_time_current_ms)))[0]
    if bank == "SD" and index == 11:
        return struct.unpack('<H', struct.pack('<h', int(_t_sys_scan_time_min_ms)))[0]
    if bank == "SD" and index == 12:
        return struct.unpack('<H', struct.pack('<h', int(_t_sys_scan_time_max_ms)))[0]
    if bank == "SD" and index == 13:
        return struct.unpack('<H', struct.pack('<h', int(_t_sys_scan_time_fixed_setup_ms)))[0]
    if bank == "SD" and index == 14:
        return struct.unpack('<H', struct.pack('<h', int(_t_sys_interrupt_scan_time_ms)))[0]
    if bank == "SD" and index == 19:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_year4)))[0]
    if bank == "SD" and index == 20:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_year2)))[0]
    if bank == "SD" and index == 21:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_month)))[0]
    if bank == "SD" and index == 22:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_day)))[0]
    if bank == "SD" and index == 23:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_weekday)))[0]
    if bank == "SD" and index == 24:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_hour)))[0]
    if bank == "SD" and index == 25:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_minute)))[0]
    if bank == "SD" and index == 26:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_second)))[0]
    if bank == "SD" and index == 29:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_new_year4)))[0]
    if bank == "SD" and index == 31:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_new_month)))[0]
    if bank == "SD" and index == 32:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_new_day)))[0]
    if bank == "SD" and index == 34:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_new_hour)))[0]
    if bank == "SD" and index == 35:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_new_minute)))[0]
    if bank == "SD" and index == 36:
        return struct.unpack('<H', struct.pack('<h', int(_t_rtc_new_second)))[0]
    if bank == "SD" and index == 69:
        return struct.unpack('<H', struct.pack('<h', int(_t_storage_sd_error_code)))[0]
    if bank == "TD" and index == 1:
        return struct.unpack('<H', struct.pack('<h', int(_t_RedTimer_Acc)))[0]
    if bank == "TD" and index == 2:
        return struct.unpack('<H', struct.pack('<h', int(_t_GreenTimer_Acc)))[0]
    if bank == "TD" and index == 3:
        return struct.unpack('<H', struct.pack('<h', int(_t_YellowTimer_Acc)))[0]
    if bank == "TXT" and index == 1:
        return ((ord(_t_State) if _t_State else 0) & 0xFF) | (((0) & 0xFF) << 8)
    return 0

def _mb_write_reg_plc(bank, index, reg_pos, value):
    global _t_GreenTimer_Acc, _t_RedTimer_Acc, _t_RxExCode, _t_State, _t_YellowTimer_Acc, _t_rtc_new_day, _t_rtc_new_hour, _t_rtc_new_minute, _t_rtc_new_month, _t_rtc_new_second, _t_rtc_new_year4
    _word = int(value) & 0xFFFF
    if bank == "DS" and index == 1:
        _t_RxExCode = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "SD" and index == 29:
        _t_rtc_new_year4 = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "SD" and index == 31:
        _t_rtc_new_month = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "SD" and index == 32:
        _t_rtc_new_day = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "SD" and index == 34:
        _t_rtc_new_hour = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "SD" and index == 35:
        _t_rtc_new_minute = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "SD" and index == 36:
        _t_rtc_new_second = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "TD" and index == 1:
        _t_RedTimer_Acc = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "TD" and index == 2:
        _t_GreenTimer_Acc = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "TD" and index == 3:
        _t_YellowTimer_Acc = struct.unpack('<h', struct.pack('<H', _word))[0]
        return
    if bank == "TXT" and index == 1:
        _t_State = chr(_word & 0xFF)
        return

def _mb_xy_start(base, word):
    if word > 1 and word % 2:
        return None
    return base + word * 16

def _mb_coil_cell(addr):
    return None

def _mb_reg_cell(addr):
    return None

def _mb_cell_word(bank, cells, i, pos):
    return 0

def _mb_cell_store(bank, cells, i, pos, _word):
    return None

def _mb_coil_get(addr):
    _mapped = _MB_COIL_MAP.get(addr)
    if _mapped is not None:
        return _mb_read_coil_plc(_mapped[0], _mapped[1])
    _cell = _mb_coil_cell(addr)
    return None if _cell is None else bool(_cell[1][_cell[2]])

def _mb_coil_set(addr, val):
    _mapped = _MB_COIL_MAP.get(addr)
    if _mapped is not None:
        _mb_write_coil_plc(_mapped[0], _mapped[1], val)
        return
    _cell = _mb_coil_cell(addr)
    if _cell is not None:
        _cell[1][_cell[2]] = bool(val)

def _mb_reg_get(addr):
    _mapped = _MB_REG_MAP.get(addr)
    if _mapped is not None:
        return _mb_read_reg_plc(_mapped[0], _mapped[1], _mapped[2])
    _cell = _mb_reg_cell(addr)
    return None if _cell is None else _mb_cell_word(_cell[0], _cell[1], _cell[2], _cell[3])

def _mb_xy_word(base, word, held):
    _start = _mb_xy_start(base, word)
    if _start is None:
        return held
    for _bit in range(16):
        _value = _mb_coil_get(_start + _bit)
        if _value is not None:
            if _value:
                held |= 1 << _bit
            else:
                held &= ~(1 << _bit)
    return held

def _mb_read_coil(addr):
    _value = _mb_coil_get(addr)
    if _value is None:
        return False if _mb_in(addr, _MB_COIL_SPANS) else None
    return _value

def _mb_coil_ok(addr):
    _mapped = _MB_COIL_MAP.get(addr) or _mb_coil_cell(addr)
    if _mapped is None or _mapped[0] in _MB_COIL_READ_ONLY:
        return False
    return _mapped[0] != "SC" or _mapped[1] in _MB_SC_WRITABLE

def _mb_write_coils(start, values):
    for _offset in range(len(values)):
        if not _mb_coil_ok(start + _offset):
            return False
    for _offset in range(len(values)):
        _mb_coil_set(start + _offset, values[_offset])
    return True

def _mb_read_reg(addr):
    if 57344 <= addr < 57361:
        return _mb_xy_word(0, addr - 57344, 0)
    if 57856 <= addr < 57873:
        return _mb_xy_word(8192, addr - 57856, _mb_yd[addr - 57856])
    try:
        _word = _mb_reg_get(addr)
    except Exception:
        return None
    if _word is None:
        return 0 if _mb_in(addr, _MB_REG_SPANS) else None
    return _word

def _mb_reg_ok(addr):
    if 57856 <= addr < 57873:
        return True
    _mapped = _MB_REG_MAP.get(addr) or _mb_reg_cell(addr)
    if _mapped is None:
        return False
    if _mapped[0] == "SD" and _mapped[1] not in _MB_SD_WRITABLE:
        return False
    if _mapped[0] in _MB_WIDE:
        try:
            _mb_reg_get(addr)
        except Exception:
            return False
    return True

def _mb_write_regs(start, values):
    for _offset in range(len(values)):
        if not _mb_reg_ok(start + _offset):
            return False
    for _offset in range(len(values)):
        _addr = start + _offset
        _word = int(values[_offset]) & 0xFFFF
        if 57856 <= _addr < 57873:
            _mb_yd[_addr - 57856] = _word
            _start = _mb_xy_start(8192, _addr - 57856)
            for _bit in range(16 if _start is not None else 0):
                if _mb_coil_get(_start + _bit) is not None:
                    _mb_coil_set(_start + _bit, (_word >> _bit) & 1)
            continue
        _mapped = _MB_REG_MAP.get(_addr)
        try:
            if _mapped is not None:
                _mb_write_reg_plc(_mapped[0], _mapped[1], _mapped[2], _word)
            else:
                _cell = _mb_reg_cell(_addr)
                _mb_cell_store(_cell[0], _cell[1], _cell[2], _cell[3], _word)
        except Exception:
            return False
    return True


# -- Wire runtime ------------------------------------------------------------
_rt._mb_read_coil = _mb_read_coil
_rt._mb_write_coils = _mb_write_coils
_rt._mb_read_reg = _mb_read_reg
_rt._mb_write_regs = _mb_write_regs
_rt._mb_server = _mb_server
_rt._mb_clients = _mb_clients
_rt._mb_buf = _mb_buf
_rt._mb_pool = _mb_pool
_rt._mb_client_jobs = _mb_client_jobs

# Embedded function call targets.
# None emitted in foundation step.

# -- Ladder logic --------------------------------------------------------------
def _run_main_rungs():
    global _b_Slot1, _b_Slot2, _mb_client_i12, _mem, _t_GreenTimer_Acc, _t_GreenTimer_Done, _t_GreenTimer_EN, _t_GreenTimer_TT, _t_RedTimer_Acc, _t_RedTimer_Done, _t_RedTimer_EN, _t_RedTimer_TT, _t_RxBusy, _t_RxErr, _t_RxExCode, _t_RxOk, _t_State, _t_WalkActive, _t_WalkRequest, _t_YellowTimer_Acc, _t_YellowTimer_Done, _t_YellowTimer_EN, _t_YellowTimer_TT, _t_fault_address_error, _t_fault_out_of_range
    _rung_1_enabled = (_t_State == 'r') and (not bool(_b_Slot1[0]))
    if _rung_1_enabled:
        _acc = int(_t_RedTimer_Acc)
        _carry = _mem.get("_frac:RedTimer_Acc", 0) + _mem.get("_dt_ns", 0)
        _int_units = _carry // 1000000
        _new_frac = _carry % 1000000
        _acc = min(_acc + _int_units, 32767)
        _preset = int(5000)
        _mem["_frac:RedTimer_Acc"] = _new_frac
        _t_RedTimer_Done = (_acc >= _preset)
        _t_RedTimer_Acc = _acc
    else:
        _mem["_frac:RedTimer_Acc"] = 0
        _t_RedTimer_Done = False
        _t_RedTimer_Acc = 0
    _t_RedTimer_EN = bool(_rung_1_enabled)
    _t_RedTimer_TT = bool(_rung_1_enabled) and not _t_RedTimer_Done
    _rung_2_enabled = bool(_t_RedTimer_Done)
    if _rung_2_enabled:
        _t_fault_address_error = False
        _t_fault_out_of_range = False
        _t_State = _store_copy_value_to_type('g', "CHAR")
    _rung_3_enabled = (_t_State == 'g') and (not bool(_b_Slot1[0]))
    if _rung_3_enabled:
        _acc = int(_t_GreenTimer_Acc)
        _carry = _mem.get("_frac:GreenTimer_Acc", 0) + _mem.get("_dt_ns", 0)
        _int_units = _carry // 1000000
        _new_frac = _carry % 1000000
        _acc = min(_acc + _int_units, 32767)
        _preset = int(4000)
        _mem["_frac:GreenTimer_Acc"] = _new_frac
        _t_GreenTimer_Done = (_acc >= _preset)
        _t_GreenTimer_Acc = _acc
    else:
        _mem["_frac:GreenTimer_Acc"] = 0
        _t_GreenTimer_Done = False
        _t_GreenTimer_Acc = 0
    _t_GreenTimer_EN = bool(_rung_3_enabled)
    _t_GreenTimer_TT = bool(_rung_3_enabled) and not _t_GreenTimer_Done
    _rung_4_enabled = bool(_t_GreenTimer_Done)
    if _rung_4_enabled:
        _t_fault_address_error = False
        _t_fault_out_of_range = False
        _t_State = _store_copy_value_to_type('y', "CHAR")
    _rung_5_enabled = (_t_State == 'y') and (not bool(_b_Slot1[0]))
    if _rung_5_enabled:
        _acc = int(_t_YellowTimer_Acc)
        _carry = _mem.get("_frac:YellowTimer_Acc", 0) + _mem.get("_dt_ns", 0)
        _int_units = _carry // 1000000
        _new_frac = _carry % 1000000
        _acc = min(_acc + _int_units, 32767)
        _preset = int(1500)
        _mem["_frac:YellowTimer_Acc"] = _new_frac
        _t_YellowTimer_Done = (_acc >= _preset)
        _t_YellowTimer_Acc = _acc
    else:
        _mem["_frac:YellowTimer_Acc"] = 0
        _t_YellowTimer_Done = False
        _t_YellowTimer_Acc = 0
    _t_YellowTimer_EN = bool(_rung_5_enabled)
    _t_YellowTimer_TT = bool(_rung_5_enabled) and not _t_YellowTimer_Done
    _rung_6_enabled = bool(_t_YellowTimer_Done)
    if _rung_6_enabled:
        _t_fault_address_error = False
        _t_fault_out_of_range = False
        _t_State = _store_copy_value_to_type('r', "CHAR")
    _edge_e1_now = bool(_t_WalkRequest)
    _edge_e1_was = _mem.get('_edge:e1', False)
    if _edge_e1_now != _edge_e1_was:
        _mem['_edge:e1'] = _edge_e1_now
    _edge_e2_now = bool(_b_Slot1[1])
    _edge_e2_was = _mem.get('_edge:e2', False)
    if _edge_e2_now != _edge_e2_was:
        _mem['_edge:e2'] = _edge_e2_now
    _rung_7_enabled = ((_edge_e1_now and not _edge_e1_was) or (_edge_e2_now and not _edge_e2_was))
    if _rung_7_enabled:
        _t_WalkActive = True
    else:
        _t_WalkActive = False
    _rung_8_enabled = bool(_t_GreenTimer_Done)
    if _rung_8_enabled:
        _t_WalkActive = False
    _rung_9_enabled = (_t_State == 'r')
    if _rung_9_enabled:
        _b_Slot2[0] = True
    else:
        _b_Slot2[0] = False
    _rung_10_enabled = (_t_State == 'g')
    if _rung_10_enabled:
        _b_Slot2[2] = True
    else:
        _b_Slot2[2] = False
    _rung_11_enabled = (_t_State == 'y')
    if _rung_11_enabled:
        _b_Slot2[1] = True
    else:
        _b_Slot2[1] = False
    _rung_12_enabled = True
    _mb_client_i12["enabled"] = bool(_rung_12_enabled)

# -- I/O -----------------------------------------------------------------------
def _read_inputs():
    global _b_Slot1, _t_board_switch
    _mask_s1_1 = int(base.readDiscrete(1))
    _b_Slot1[0] = bool((_mask_s1_1 >> 0) & 1)
    _b_Slot1[1] = bool((_mask_s1_1 >> 1) & 1)
    _b_Slot1[2] = bool((_mask_s1_1 >> 2) & 1)
    _b_Slot1[3] = bool((_mask_s1_1 >> 3) & 1)
    _b_Slot1[4] = bool((_mask_s1_1 >> 4) & 1)
    _b_Slot1[5] = bool((_mask_s1_1 >> 5) & 1)
    _b_Slot1[6] = bool((_mask_s1_1 >> 6) & 1)
    _b_Slot1[7] = bool((_mask_s1_1 >> 7) & 1)
    _t_board_switch = bool(_board_switch_io.value)

def _write_outputs():
    global _b_Slot2
    _out_mask_s2_1 = 0
    if bool(_b_Slot2[0]):
        _out_mask_s2_1 |= (1 << 0)
    if bool(_b_Slot2[1]):
        _out_mask_s2_1 |= (1 << 1)
    if bool(_b_Slot2[2]):
        _out_mask_s2_1 |= (1 << 2)
    if bool(_b_Slot2[3]):
        _out_mask_s2_1 |= (1 << 3)
    if bool(_b_Slot2[4]):
        _out_mask_s2_1 |= (1 << 4)
    if bool(_b_Slot2[5]):
        _out_mask_s2_1 |= (1 << 5)
    if bool(_b_Slot2[6]):
        _out_mask_s2_1 |= (1 << 6)
    if bool(_b_Slot2[7]):
        _out_mask_s2_1 |= (1 << 7)
    if bool(_b_Slot2[8]):
        _out_mask_s2_1 |= (1 << 8)
    if bool(_b_Slot2[9]):
        _out_mask_s2_1 |= (1 << 9)
    if bool(_b_Slot2[10]):
        _out_mask_s2_1 |= (1 << 10)
    if bool(_b_Slot2[11]):
        _out_mask_s2_1 |= (1 << 11)
    if bool(_b_Slot2[12]):
        _out_mask_s2_1 |= (1 << 12)
    if bool(_b_Slot2[13]):
        _out_mask_s2_1 |= (1 << 13)
    if bool(_b_Slot2[14]):
        _out_mask_s2_1 |= (1 << 14)
    base.writeDiscrete(_out_mask_s2_1, 2)

# -- Main scan loop ------------------------------------------------------------
while True:
    scan_start_ns = time.monotonic_ns()
    _sd_write_status = False
    _dt_ns = scan_start_ns - _last_scan_ns
    dt = _dt_ns / 1000000000
    _last_scan_ns = scan_start_ns
    _mem["_dt"] = dt
    _mem["_dt_ns"] = _dt_ns

    _sd_eject_cmd = bool(_t_storage_sd_eject_cmd)
    _sd_delete_all_cmd = bool(_t_storage_sd_delete_all_cmd)
    _service_sd_commands()
    _t_storage_sd_eject_cmd = _sd_eject_cmd
    _t_storage_sd_delete_all_cmd = _sd_delete_all_cmd
    _t_storage_sd_ready = bool(_sd_available)
    _t_storage_sd_write_status = bool(_sd_write_status)
    _t_storage_sd_error = bool(_sd_error)
    _t_storage_sd_error_code = int(_sd_error_code)
    _read_inputs()
    _runstop_sample = bool(_t_board_switch)
    if not _runstop_initialized:
        _runstop_raw = _runstop_sample
        _runstop_debounced = _runstop_sample
        _runstop_last_change_ns = scan_start_ns
        _runstop_initialized = True
    elif _runstop_sample != _runstop_raw:
        _runstop_raw = _runstop_sample
        _runstop_last_change_ns = scan_start_ns
    elif (scan_start_ns - _runstop_last_change_ns) >= 30000000:
        _runstop_debounced = _runstop_raw

    _desired_run = bool(_runstop_debounced)
    if _stop_hold:
        if _desired_run:
            _desired_run = False
        else:
            _stop_hold = False
    if _desired_run != _mode_run:
        if _desired_run:
            _reset_for_run_transition()
            _mem["_dt"] = dt
            _mem["_dt_ns"] = _dt_ns
            print("Mode: RUN")
        else:
            save_memory()
            print("Mode: STOP")
        _mode_run = _desired_run
    _t_sys_mode_switch_run = bool(_runstop_debounced)
    _t_sys_mode_run = bool(_mode_run)
    if _mode_run:
        _stop_cmd = _t_sys_cmd_mode_stop
        if _stop_cmd:
            _t_sys_cmd_mode_stop = False
            _t_sys_mode_run = False
        _t_fault_division_error = False
        _t_fault_math_operation_error = False
        _t_fault_out_of_range = False
        _t_fault_address_error = False
        _t_sys_always_on = True
        _t_sys_scan_counter = _sys_scan_n
        _t_sys_scan_clock_toggle = (_sys_scan_n & 1) == 0
        _t_sys_first_scan = _sys_first
        if _sys_first:
            _sys_t0_ns = scan_start_ns
            _sys_prev_ns = scan_start_ns
        _sys_ms = (_sys_prev_ns - _sys_t0_ns) // 1000000
        _sys_prev_ns = scan_start_ns
        _t_sys_clock_10ms = ((_sys_ms // 5) & 1) == 1
        _t_sys_clock_100ms = ((_sys_ms // 50) & 1) == 1
        _t_sys_clock_500ms = ((_sys_ms // 250) & 1) == 1
        _t_sys_clock_1s = ((_sys_ms // 500) & 1) == 1
        _t_sys_clock_1m = ((_sys_ms // 30000) & 1) == 1
        _t_sys_clock_1h = ((_sys_ms // 1800000) & 1) == 1
        _t_sys_scan_time_current_ms = int(round(dt * 1000.0))
        _rtc_now = time.localtime()
        _t_rtc_year4 = _rtc_now[0]
        _t_rtc_year2 = _rtc_now[0] % 100
        _t_rtc_month = _rtc_now[1]
        _t_rtc_day = _rtc_now[2]
        _t_rtc_weekday = ((_rtc_now[6] + 1) % 7) + 1
        _t_rtc_hour = _rtc_now[3]
        _t_rtc_minute = _rtc_now[4]
        _t_rtc_second = _rtc_now[5]
        _run_main_rungs()
        _write_outputs()
        _sys_scan_n = (_sys_scan_n + 1) % 32768
        _sys_first = False
        if _stop_cmd and _mode_run:
            _mode_run = False
            _stop_hold = True
            _t_sys_mode_run = False
            save_memory()
            print("Mode: STOP")
    else:
        _force_outputs_off()

    _rt.service_modbus_server()
    _rt.service_modbus_client()

    if (scan_start_ns - _ret_last_save_ns) >= _RET_AUTO_SAVE_NS:
        if _t_State != _ret_snapshot.get("State"):
            save_memory()
        else:
            _ret_last_save_ns = scan_start_ns


    _wd_pet()

    elapsed_ms = (time.monotonic_ns() - scan_start_ns) / 1000000
    sleep_ms = TARGET_SCAN_MS - elapsed_ms
    if sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)
    else:
        _scan_overrun_count += 1
        if PRINT_SCAN_OVERRUNS:
            print(f"Scan overrun #{_scan_overrun_count}: {-sleep_ms:.3f} ms late")

