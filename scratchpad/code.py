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

try:
    import microcontroller
except ImportError:
    microcontroller = None

# -- Configuration -------------------------------------------------------------
TARGET_SCAN_MS = 10.0
WATCHDOG_MS = 5000
PRINT_SCAN_OVERRUNS = False

_SLOT_MODULES = ['P1-08SIM', 'P1-15TD2']
_RET_DEFAULTS = {'Count': 0}
_RET_TYPES = {'Count': 'INT'}
_RET_SCHEMA = "5ffb0f2f8f3a5d865170ba73b14889818e521f7df7e987b2efc6df54f704b08f"

# -- Hardware bootstrap --------------------------------------------------------
base = P1AM.Base()
base.rollCall(_SLOT_MODULES)

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
_t_Count = 0
_t_board_save_memory_cmd = False
_t_board_switch = False
_t_sys_cmd_mode_stop = False
_t_sys_mode_run = False

# Blocks (list-backed; PLC addresses remain 1-based, list indexes are 0-based).
_b_Slot1 = [False] * 8
_b_Slot2 = [False] * 15

_mem = {}
_prev = {}
_last_scan_ts = time.monotonic()
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
_ret_last_save_ts = 0.0
_RET_AUTO_SAVE_S = 30.0

_mode_run = True
_runstop_initialized = False
_runstop_raw = False
_runstop_debounced = False
_runstop_last_change_ts = 0.0

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
    global _t_Count, _sd_write_status, _sd_error, _sd_error_code, _ret_snapshot, _ret_last_save_ts
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
    _entry = values.get("Count")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_Count = max(-32768, min(32767, int(_entry.get("value", _t_Count))))
        except Exception:
            pass
    print("Retentive memory loaded")
    _sd_error = False
    _sd_error_code = 0
    _sd_write_status = False
    _ret_snapshot = {"Count": _t_Count}
    _ret_last_save_ts = time.monotonic()

def save_memory():
    global _t_Count, _sd_write_status, _sd_error, _sd_error_code, _ret_snapshot, _ret_last_save_ts
    if not _sd_available:
        return
    _sd_write_status = True
    values = {}
    if _t_Count != _RET_DEFAULTS["Count"]:
        values["Count"] = {"type": "INT", "value": _t_Count}
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
    _ret_snapshot = {"Count": _t_Count}
    _ret_last_save_ts = time.monotonic()

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
    global _mem, _prev, _ret_snapshot, _sd_delete_all_cmd, _sd_eject_cmd, _sd_save_cmd, _t_Count, _t_board_save_memory_cmd, _t_board_switch, _t_sys_cmd_mode_stop, _t_sys_mode_run
    _mem = {}
    _prev = {}
    _ret_snapshot = {}
    _sd_save_cmd = False
    _sd_eject_cmd = False
    _sd_delete_all_cmd = False
    _b_Slot2[0] = False
    _b_Slot2[1] = False
    _t_board_save_memory_cmd = False
    _t_sys_cmd_mode_stop = False
    _t_sys_mode_run = False

def _force_outputs_off():
    global _t_Count, _t_board_save_memory_cmd, _t_board_switch, _t_sys_cmd_mode_stop, _t_sys_mode_run
    for _i in range(15):
        _b_Slot2[_i] = False
    _write_outputs()

def _rise(curr, prev):
    return bool(curr) and not bool(prev)

def _store_copy_value_to_type(value, dest_type):
    if isinstance(value, float) and not math.isfinite(value):
        value = 0
    if dest_type == "INT":
        return max(-32768, min(32767, int(value)))
    if dest_type == "DINT":
        return max(-2147483648, min(2147483647, int(value)))
    if dest_type == "WORD":
        return int(value) & 0xFFFF
    if dest_type == "REAL":
        return float(value)
    if dest_type == "BOOL":
        return bool(value)
    if dest_type == "CHAR":
        if not isinstance(value, str):
            raise ValueError("CHAR value must be a string")
        if value == "":
            return value
        if len(value) != 1 or ord(value) > 127:
            raise ValueError("CHAR value must be blank or one ASCII character")
        return value
    return value

# -- Modbus address mapping ----------------------------------------------------
_MB_COIL_MAP = {}

def _mb_reverse_coil(addr):
    return _MB_COIL_MAP.get(int(addr))

_MB_REG_MAP = {}

def _mb_reverse_register(addr):
    return _MB_REG_MAP.get(int(addr))

def _mb_read_coil_plc(bank, index):
    return False

def _mb_write_coil_plc(bank, index, val):
    global _mode_run
    _value = bool(val)
    if bank in ("Y", "C"):
        return True
    if bank == "SC":
        return index in [53, 55, 60, 61, 65, 66, 67, 75, 76, 120, 121]
    return False

def _mb_read_reg_plc(bank, index, reg_pos):
    return 0

def _mb_write_reg_plc(bank, index, reg_pos, value):
    _word = int(value) & 0xFFFF
    if bank in ("DS", "DD", "DH", "DF", "TXT", "TD", "CTD"):
        return True
    if bank == "SD":
        return index in [29, 31, 32, 34, 35, 36, 40, 41, 42, 50, 51, 60, 61, 106, 107, 108, 112, 113, 114, 140, 141, 142, 143, 144, 145, 146, 147, 214, 215]
    return False

def _mb_read_coil(addr):
    _mapped = _mb_reverse_coil(int(addr))
    if _mapped is None:
        return None
    _bank, _index = _mapped
    return _mb_read_coil_plc(_bank, _index)

def _mb_write_coil(addr, val):
    _mapped = _mb_reverse_coil(int(addr))
    if _mapped is None:
        return False
    _bank, _index = _mapped
    return _mb_write_coil_plc(_bank, _index, val)

def _mb_read_reg(addr):
    _mapped = _mb_reverse_register(int(addr))
    if _mapped is None:
        return None
    _bank, _index, _reg_pos = _mapped
    return _mb_read_reg_plc(_bank, _index, _reg_pos)

def _mb_write_reg(addr, val):
    _mapped = _mb_reverse_register(int(addr))
    if _mapped is None:
        return False
    _bank, _index, _reg_pos = _mapped
    return _mb_write_reg_plc(_bank, _index, _reg_pos, val)

# Embedded function call targets.
# None emitted in foundation step.

# -- Ladder logic --------------------------------------------------------------
def _run_main_rungs():
    global _b_Slot1, _b_Slot2, _prev, _t_Count, _t_board_save_memory_cmd
    _rung_1_enabled = _rise(bool(_b_Slot1[0]), bool(_prev.get("Slot1.1", False)))
    if _rung_1_enabled:
        _t_Count = _store_copy_value_to_type((_t_Count + 1), "INT")
    _rung_2_enabled = bool(_b_Slot1[1])
    if _rung_2_enabled:
        _t_Count = _store_copy_value_to_type(0, "INT")
    _rung_3_enabled = True
    if _rung_3_enabled:
        _b_Slot2[0] = True
    else:
        _b_Slot2[0] = False
    _rung_4_enabled = (_rise(bool(_b_Slot1[0]), bool(_prev.get("Slot1.1", False))) or bool(_b_Slot1[1]))
    if _rung_4_enabled:
        _t_board_save_memory_cmd = True
    else:
        _t_board_save_memory_cmd = False
    _rung_5_enabled = (_t_Count > 0)
    if _rung_5_enabled:
        _b_Slot2[1] = True
    else:
        _b_Slot2[1] = False

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
    scan_start = time.monotonic()
    _sd_write_status = False
    dt = scan_start - _last_scan_ts
    if dt < 0:
        dt = 0.0
    _last_scan_ts = scan_start
    _mem["_dt"] = dt

    _sd_save_cmd = bool(_t_board_save_memory_cmd)
    _service_sd_commands()
    _t_board_save_memory_cmd = _sd_save_cmd
    _read_inputs()
    _runstop_sample = bool(_t_board_switch)
    if not _runstop_initialized:
        _runstop_raw = _runstop_sample
        _runstop_debounced = _runstop_sample
        _runstop_last_change_ts = scan_start
        _runstop_initialized = True
    elif _runstop_sample != _runstop_raw:
        _runstop_raw = _runstop_sample
        _runstop_last_change_ts = scan_start
    elif ((scan_start - _runstop_last_change_ts) * 1000.0) >= 30:
        _runstop_debounced = _runstop_raw

    _desired_run = bool(_runstop_debounced)
    if bool(_t_sys_cmd_mode_stop):
        _desired_run = False
        _t_sys_cmd_mode_stop = False
    if _desired_run != _mode_run:
        if _desired_run:
            _reset_for_run_transition()
            print("Mode: RUN")
        else:
            save_memory()
            print("Mode: STOP")
        _mode_run = _desired_run
    _t_sys_mode_run = bool(_mode_run)
    if _mode_run:
        _run_main_rungs()
        _write_outputs()
    else:
        _force_outputs_off()

    if (scan_start - _ret_last_save_ts) >= _RET_AUTO_SAVE_S:
        if _t_Count != _ret_snapshot.get("Count"):
            save_memory()
        else:
            _ret_last_save_ts = scan_start

    _prev["Slot1.1"] = _b_Slot1[0]

    _wd_pet()

    elapsed_ms = (time.monotonic() - scan_start) * 1000.0
    sleep_ms = TARGET_SCAN_MS - elapsed_ms
    if sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)
    else:
        _scan_overrun_count += 1
        if PRINT_SCAN_OVERRUNS:
            print(f"Scan overrun #{_scan_overrun_count}: {-sleep_ms:.3f} ms late")

