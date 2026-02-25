import hashlib
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

try:
    import microcontroller
except ImportError:
    microcontroller = None

TARGET_SCAN_MS = 10.0
WATCHDOG_MS = 500
PRINT_SCAN_OVERRUNS = False

_SLOT_MODULES = ['P1-08SIM', 'P1-08TRS']
_RET_DEFAULTS = {'CalcOut': 0, 'CtdAcc': 0, 'CtuAcc': 0, 'FnOut': 0, 'FoundAddr': 0, 'Idx': 1, 'LoopCount': 3, 'PackedDword': 0, 'PackedWord': 0, 'Source': 0, 'Span': 2, 'TofAcc': 0, 'TonAcc': 0}
_RET_TYPES = {'CalcOut': 'INT', 'CtdAcc': 'DINT', 'CtuAcc': 'DINT', 'FnOut': 'INT', 'FoundAddr': 'INT', 'Idx': 'INT', 'LoopCount': 'INT', 'PackedDword': 'DINT', 'PackedWord': 'INT', 'Source': 'INT', 'Span': 'INT', 'TofAcc': 'INT', 'TonAcc': 'INT'}
_RET_SCHEMA = "a33dc6e7ff47d7557f83e4d01c10a6466d3c9f641dbf6d64f374f4f37b651132"

base = P1AM.Base()
base.rollCall(_SLOT_MODULES)

_wd_config = getattr(base, "config_watchdog", None)
_wd_start = getattr(base, "start_watchdog", None)
_wd_pet = getattr(base, "pet_watchdog", None)
if _wd_config is None or _wd_start is None or _wd_pet is None:
    raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")
_wd_config(WATCHDOG_MS)
_wd_start()

# Scalars (non-block tags).
_t_Abort = False
_t_AutoMode = False
_t_CalcOut = 0
_t_Clock = False
_t_CtdAcc = 0
_t_CtdDone = False
_t_CtuAcc = 0
_t_CtuDone = False
_t_Enable = False
_t_FnOut = 0
_t_Found = False
_t_FoundAddr = 0
_t_Idx = 1
_t_LoopCount = 3
_t_PackedDword = 0
_t_PackedWord = 0
_t_Running = False
_t_ShiftReset = False
_t_Source = 0
_t_Span = 2
_t_Start = False
_t_StepDone = False
_t_Stop = False
_t_TofAcc = 0
_t_TofDone = False
_t_TonAcc = 0
_t_TonDone = False
_t__forloop_idx = 0
_t_storage_sd_delete_all_cmd = False
_t_storage_sd_eject_cmd = False
_t_storage_sd_save_cmd = False

# Blocks (list-backed; PLC addresses remain 1-based, list indexes are 0-based).
_b_BITS = [False] * 32
_b_DD = [0] * 20
_b_DS = [0] * 20
_b_Slot1 = [False] * 8
_b_Slot2 = [False] * 8
_b_TXT = [''] * 8
_b_WORDS = [0] * 2

_mem = {}
_prev = {}
_last_scan_ts = time.monotonic()
_scan_overrun_count = 0

_sd_available = False
_MEMORY_PATH = "/sd/memory.json"
_MEMORY_TMP_PATH = "/sd/_memory.tmp"
_sd_spi = None
_sd = None
_sd_vfs = None
_sd_write_status = False
_sd_error = False
_sd_error_code = 0
_sd_save_cmd = False
_sd_eject_cmd = False
_sd_delete_all_cmd = False

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
    global _t_CalcOut, _t_CtdAcc, _t_CtuAcc, _t_FnOut, _t_FoundAddr, _t_Idx, _t_LoopCount, _t_PackedDword, _t_PackedWord, _t_Source, _t_Span, _t_TofAcc, _t_TonAcc, _sd_write_status, _sd_error, _sd_error_code
    if not _sd_available:
        print("Retentive load skipped: SD unavailable")
        return
    _sd_write_status = True
    if microcontroller is not None and len(microcontroller.nvm) > 0 and microcontroller.nvm[0] == 1:
        _sd_error = True
        _sd_error_code = 2
        _sd_write_status = False
        print("Retentive load skipped: interrupted previous save detected")
        return
    try:
        with open(_MEMORY_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as exc:
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
    _entry = values.get("CalcOut")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_CalcOut = max(-32768, min(32767, int(_entry.get("value", _t_CalcOut))))
        except Exception:
            pass
    _entry = values.get("CtdAcc")
    if isinstance(_entry, dict) and _entry.get("type") == "DINT":
        try:
            _t_CtdAcc = max(-2147483648, min(2147483647, int(_entry.get("value", _t_CtdAcc))))
        except Exception:
            pass
    _entry = values.get("CtuAcc")
    if isinstance(_entry, dict) and _entry.get("type") == "DINT":
        try:
            _t_CtuAcc = max(-2147483648, min(2147483647, int(_entry.get("value", _t_CtuAcc))))
        except Exception:
            pass
    _entry = values.get("FnOut")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_FnOut = max(-32768, min(32767, int(_entry.get("value", _t_FnOut))))
        except Exception:
            pass
    _entry = values.get("FoundAddr")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_FoundAddr = max(-32768, min(32767, int(_entry.get("value", _t_FoundAddr))))
        except Exception:
            pass
    _entry = values.get("Idx")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_Idx = max(-32768, min(32767, int(_entry.get("value", _t_Idx))))
        except Exception:
            pass
    _entry = values.get("LoopCount")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_LoopCount = max(-32768, min(32767, int(_entry.get("value", _t_LoopCount))))
        except Exception:
            pass
    _entry = values.get("PackedDword")
    if isinstance(_entry, dict) and _entry.get("type") == "DINT":
        try:
            _t_PackedDword = max(-2147483648, min(2147483647, int(_entry.get("value", _t_PackedDword))))
        except Exception:
            pass
    _entry = values.get("PackedWord")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_PackedWord = max(-32768, min(32767, int(_entry.get("value", _t_PackedWord))))
        except Exception:
            pass
    _entry = values.get("Source")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_Source = max(-32768, min(32767, int(_entry.get("value", _t_Source))))
        except Exception:
            pass
    _entry = values.get("Span")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_Span = max(-32768, min(32767, int(_entry.get("value", _t_Span))))
        except Exception:
            pass
    _entry = values.get("TofAcc")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_TofAcc = max(-32768, min(32767, int(_entry.get("value", _t_TofAcc))))
        except Exception:
            pass
    _entry = values.get("TonAcc")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_TonAcc = max(-32768, min(32767, int(_entry.get("value", _t_TonAcc))))
        except Exception:
            pass
    _sd_error = False
    _sd_error_code = 0
    _sd_write_status = False

def save_memory():
    global _t_CalcOut, _t_CtdAcc, _t_CtuAcc, _t_FnOut, _t_FoundAddr, _t_Idx, _t_LoopCount, _t_PackedDword, _t_PackedWord, _t_Source, _t_Span, _t_TofAcc, _t_TonAcc, _sd_write_status, _sd_error, _sd_error_code
    if not _sd_available:
        return
    _sd_write_status = True
    values = {}
    if _t_CalcOut != _RET_DEFAULTS["CalcOut"]:
        values["CalcOut"] = {"type": "INT", "value": _t_CalcOut}
    if _t_CtdAcc != _RET_DEFAULTS["CtdAcc"]:
        values["CtdAcc"] = {"type": "DINT", "value": _t_CtdAcc}
    if _t_CtuAcc != _RET_DEFAULTS["CtuAcc"]:
        values["CtuAcc"] = {"type": "DINT", "value": _t_CtuAcc}
    if _t_FnOut != _RET_DEFAULTS["FnOut"]:
        values["FnOut"] = {"type": "INT", "value": _t_FnOut}
    if _t_FoundAddr != _RET_DEFAULTS["FoundAddr"]:
        values["FoundAddr"] = {"type": "INT", "value": _t_FoundAddr}
    if _t_Idx != _RET_DEFAULTS["Idx"]:
        values["Idx"] = {"type": "INT", "value": _t_Idx}
    if _t_LoopCount != _RET_DEFAULTS["LoopCount"]:
        values["LoopCount"] = {"type": "INT", "value": _t_LoopCount}
    if _t_PackedDword != _RET_DEFAULTS["PackedDword"]:
        values["PackedDword"] = {"type": "DINT", "value": _t_PackedDword}
    if _t_PackedWord != _RET_DEFAULTS["PackedWord"]:
        values["PackedWord"] = {"type": "INT", "value": _t_PackedWord}
    if _t_Source != _RET_DEFAULTS["Source"]:
        values["Source"] = {"type": "INT", "value": _t_Source}
    if _t_Span != _RET_DEFAULTS["Span"]:
        values["Span"] = {"type": "INT", "value": _t_Span}
    if _t_TofAcc != _RET_DEFAULTS["TofAcc"]:
        values["TofAcc"] = {"type": "INT", "value": _t_TofAcc}
    if _t_TonAcc != _RET_DEFAULTS["TonAcc"]:
        values["TonAcc"] = {"type": "INT", "value": _t_TonAcc}
    payload = {"schema": _RET_SCHEMA, "values": values}
    dirty_armed = False
    if microcontroller is not None and len(microcontroller.nvm) > 0:
        microcontroller.nvm[0] = 1
        dirty_armed = True
    try:
        with open(_MEMORY_TMP_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(_MEMORY_TMP_PATH, _MEMORY_PATH)
    except Exception as exc:
        _sd_error = True
        _sd_error_code = 3
        _sd_write_status = False
        print(f"Retentive save failed: {exc}")
        return
    if dirty_armed:
        microcontroller.nvm[0] = 0
    _sd_error = False
    _sd_error_code = 0
    _sd_write_status = False

_mount_sd()
load_memory()

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
            for _path in (_MEMORY_PATH, _MEMORY_TMP_PATH):
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
    _sd_write_status = True

def _resolve_index_b_DD(addr):
    if addr < 1 or addr > 20:
        raise IndexError(f"Address {addr} out of range for DD (1-20)")
    return int(addr) - 1

def _resolve_index_b_DS(addr):
    if addr < 1 or addr > 20:
        raise IndexError(f"Address {addr} out of range for DS (1-20)")
    return int(addr) - 1

def _wrap_int(value, bits, signed):
    mask = (1 << bits) - 1
    v = int(value) & mask
    if signed and v >= (1 << (bits - 1)):
        v -= (1 << bits)
    return v

def _rise(curr, prev):
    return bool(curr) and not bool(prev)

def _fall(curr, prev):
    return not bool(curr) and bool(prev)

def _parse_pack_text_value(text, dest_type):
    if text == "":
        raise ValueError("empty text cannot be parsed")
    if dest_type in {"INT", "DINT"}:
        if not re.fullmatch(r"[+-]?\d+", text):
            raise ValueError("integer parse failed")
        parsed = int(text, 10)
        if dest_type == "INT" and (parsed < -32768 or parsed > 32767):
            raise ValueError("integer out of INT range")
        if dest_type == "DINT" and (parsed < -2147483648 or parsed > 2147483647):
            raise ValueError("integer out of DINT range")
        return parsed
    if dest_type == "WORD":
        if not re.fullmatch(r"[0-9A-Fa-f]+", text):
            raise ValueError("hex parse failed")
        parsed = int(text, 16)
        if parsed < 0 or parsed > 0xFFFF:
            raise ValueError("hex out of WORD range")
        return parsed
    if dest_type == "REAL":
        parsed = float(text)
        if not math.isfinite(parsed):
            raise ValueError("REAL parse produced non-finite value")
        struct.pack("<f", parsed)
        return parsed
    raise TypeError(f"Unsupported pack_text destination type: {dest_type}")

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

def gated_scale(enabled, value, factor):
    scaled = int(value) * int(factor)
    return {"result": scaled if enabled else int(value)}
_fn_gated_scale = gated_scale

def plus_offset(value, offset):
    return {"result": int(value) + int(offset)}
_fn_plus_offset = plus_offset

def _sub_service():
    global _b_DD, _b_DS, _t_Abort, _t_FnOut, _t_Found, _t_Idx, _t_Running
    _rung_16_enabled = bool((bool(_t_Abort)))
    if _rung_16_enabled:
        return
    _rung_17_enabled = bool(((bool(_t_Running) and bool(_t_Found))))
    if _rung_17_enabled:
        _b_DS[9] = _store_copy_value_to_type(_b_DD[_resolve_index_b_DD(int(_t_Idx))], "INT")
    if _rung_17_enabled:
        _b_DS[10] = _store_copy_value_to_type((_t_FnOut + 1), "INT")

def _run_main_rungs():
    global _b_BITS, _b_DD, _b_DS, _b_TXT, _b_WORDS, _mem, _prev, _t_Abort, _t_AutoMode, _t_CalcOut, _t_Clock, _t_CtdAcc, _t_CtdDone, _t_CtuAcc, _t_CtuDone, _t_Enable, _t_FnOut, _t_Found, _t_FoundAddr, _t_Idx, _t_LoopCount, _t_PackedDword, _t_PackedWord, _t_Running, _t_ShiftReset, _t_Source, _t_Span, _t_Start, _t_StepDone, _t_Stop, _t_TofAcc, _t_TofDone, _t_TonAcc, _t_TonDone, _t__forloop_idx, _t_storage_sd_delete_all_cmd, _t_storage_sd_eject_cmd, _t_storage_sd_save_cmd
    _rung_1_enabled = bool(((bool(_t_Enable) or _rise(bool(_t_Start), bool(_prev.get("Start", False))) or _fall(bool(_t_Stop), bool(_prev.get("Stop", False))))))
    if _rung_1_enabled:
        _t_Running = True
    _rung_2_enabled = bool(((bool(_t_Stop) or bool(_t_Abort))))
    if _rung_2_enabled:
        _t_Running = False
    _rung_3_enabled = bool((bool(_t_Running)))
    _frac = float(_mem.get("_frac:TonAcc", 0.0))
    if bool(_t_ShiftReset):
        _mem["_frac:TonAcc"] = 0.0
        _t_TonDone = False
        _t_TonAcc = 0
    else:
        if _rung_3_enabled:
            _dt = float(_mem.get("_dt", 0.0))
            _acc = int(_t_TonAcc)
            _dt_units = ((_dt * 1000.0) + _frac)
            _int_units = int(_dt_units)
            _new_frac = _dt_units - _int_units
            _acc = min(_acc + _int_units, 32767)
            _preset = int(250)
            _mem["_frac:TonAcc"] = _new_frac
            _t_TonDone = (_acc >= _preset)
            _t_TonAcc = _acc
        else:
            pass
    _rung_4_enabled = bool((bool(_t_Running)))
    _frac = float(_mem.get("_frac:TofAcc", 0.0))
    if _rung_4_enabled:
        _mem["_frac:TofAcc"] = 0.0
        _t_TofDone = True
        _t_TofAcc = 0
    else:
        _dt = float(_mem.get("_dt", 0.0))
        _acc = int(_t_TofAcc)
        _dt_units = ((_dt * 1000.0) + _frac)
        _int_units = int(_dt_units)
        _new_frac = _dt_units - _int_units
        _acc = min(_acc + _int_units, 32767)
        _preset = int(100)
        _mem["_frac:TofAcc"] = _new_frac
        _t_TofDone = (_acc < _preset)
        _t_TofAcc = _acc
    _rung_5_enabled = bool((bool(_t_Running)))
    if bool(_t_Stop):
        _t_CtuDone = False
        _t_CtuAcc = 0
    else:
        _acc = int(_t_CtuAcc)
        _delta = 0
        if _rung_5_enabled:
            _delta += 1
        _acc = max(-2147483648, min(2147483647, _acc + _delta))
        _preset = int(50)
        _t_CtuDone = (_acc >= _preset)
        _t_CtuAcc = _acc
    _rung_6_enabled = bool((bool(_t_Running)))
    if bool(_t_ShiftReset):
        _t_CtdDone = False
        _t_CtdAcc = 0
    else:
        _acc = int(_t_CtdAcc)
        if _rung_6_enabled:
            _acc -= 1
        _acc = max(-2147483648, min(2147483647, _acc))
        _preset = int(5)
        _t_CtdDone = (_acc <= -_preset)
        _t_CtdAcc = _acc
    _rung_7_enabled = bool(((bool(_t_Running) and bool(_t_TonDone))))
    if _rung_7_enabled:
        _t_Source = _store_copy_value_to_type(120, "INT")
    if _rung_7_enabled:
        try:
            _calc_value = (((_t_Source * 2) + (int(_t_Idx) << int(1))) - 3)
        except ZeroDivisionError:
            _calc_value = 0
        if isinstance(_calc_value, float) and not math.isfinite(_calc_value):
            _calc_value = 0
        _t_CalcOut = _wrap_int(int(_calc_value), 16, True)
    if _rung_7_enabled:
        _b_DD[_resolve_index_b_DD(int((_t_Idx + 1)))] = _store_copy_value_to_type(_b_DS[_resolve_index_b_DS(int(_t_Idx))], "INT")
    if _rung_7_enabled:
        _b_DS[_resolve_index_b_DS(int((_t_Idx + _t_Span)))] = _store_copy_value_to_type((_t_CalcOut // 2), "INT")
    if _rung_7_enabled:
        if len(range(0, 4)) != len(range(1, 5)):
            raise ValueError(f"BlockCopy length mismatch: source has {len(range(0, 4))} elements, dest has {len(range(1, 5))} elements")
        for _src_idx, _dst_idx in zip(range(0, 4), range(1, 5)):
            _raw = _b_DS[_src_idx]
            _b_DS[_dst_idx] = _store_copy_value_to_type(_raw, "INT")
    if _rung_7_enabled:
        _fill_1_dst_1_start = int(_t_Idx)
        _fill_1_dst_1_end = int((_t_Idx + _t_Span))
        if _fill_1_dst_1_start > _fill_1_dst_1_end:
            raise ValueError("Indirect range start must be <= end")
        _fill_1_dst_1_indices = []
        _fill_1_dst_1_addrs = []
        for _fill_1_dst_1_addr in range(_fill_1_dst_1_start, _fill_1_dst_1_end + 1):
            _fill_1_dst_1_idx = _resolve_index_b_DD(int(_fill_1_dst_1_addr))
            _fill_1_dst_1_indices.append(_fill_1_dst_1_idx)
            _fill_1_dst_1_addrs.append(int(_fill_1_dst_1_addr))
        _fill_value = _t_CalcOut
        for _dst_idx in _fill_1_dst_1_indices:
            _b_DD[_dst_idx] = _store_copy_value_to_type(_fill_value, "INT")
    _rung_8_enabled = bool((bool(_t_Running)))
    if _rung_8_enabled:
        if not range(1, 21):
            _cursor_index = None
        else:
            _current_result = int(_t_FoundAddr)
            if _current_result == 0:
                _cursor_index = 0
            elif _current_result == -1:
                _cursor_index = None
            else:
                _cursor_index = None
                for _idx, _addr in enumerate(range(1, 21)):
                    if _addr > _current_result:
                        _cursor_index = _idx
                        break
        if _cursor_index is None:
            _t_FoundAddr = -1
            _t_Found = False
        else:
            _rhs = _t_CalcOut
            _matched = None
            for _idx in range(_cursor_index, len(range(0, 20))):
                _candidate = _b_DD[range(0, 20)[_idx]]
                if (_candidate >= _rhs):
                    _matched = _idx
                    break
            if _matched is None:
                _t_FoundAddr = -1
                _t_Found = False
            else:
                _t_FoundAddr = range(1, 21)[_matched]
                _t_Found = True
    if _rung_8_enabled:
        if not range(1, 9):
            _cursor_index = None
        else:
            _cursor_index = 0
        if _cursor_index is None:
            _t_FoundAddr = -1
            _t_Found = False
        else:
            if '==' not in ("==", "!="):
                raise ValueError("Text search only supports '==' and '!=' conditions")
            _rhs = str('AB')
            if _rhs == "":
                raise ValueError("Text search value cannot be empty")
            _window_len = len(_rhs)
            if _window_len > len(range(0, 8)):
                _t_FoundAddr = -1
                _t_Found = False
            else:
                _last_start = len(range(0, 8)) - _window_len
                if _cursor_index > _last_start:
                    _t_FoundAddr = -1
                    _t_Found = False
                else:
                    _matched = None
                    for _start in range(_cursor_index, _last_start + 1):
                        _candidate = ''.join(str(_b_TXT[range(0, 8)[_start + _off]]) for _off in range(_window_len))
                        if ((_candidate == _rhs)):
                            _matched = _start
                            break
                    if _matched is None:
                        _t_FoundAddr = -1
                        _t_Found = False
                    else:
                        _t_FoundAddr = range(1, 9)[_matched]
                        _t_Found = True
    _rung_9_enabled = bool((bool(_t_Running)))
    if not range(0, 8):
        raise ValueError("shift bit_range resolved to an empty range")
    _clock_curr = bool(bool(_t_Clock))
    _clock_prev = bool(_mem.get('_shift_prev_clock:C:\\Users\\Sam\\Documents\\GitHub\\pyrung\\examples\\circuitpy_codegen_review.py:135', False))
    _rising_edge = _clock_curr and not _clock_prev
    if _rising_edge:
        _prev_values = [bool(_b_BITS[_idx]) for _idx in range(0, 8)]
        _b_BITS[range(0, 8)[0]] = bool(_rung_9_enabled)
        for _pos in range(1, len(range(0, 8))):
            _b_BITS[range(0, 8)[_pos]] = _prev_values[_pos - 1]
    if bool(_t_ShiftReset):
        for _idx in range(0, 8):
            _b_BITS[_idx] = False
    _mem['_shift_prev_clock:C:\\Users\\Sam\\Documents\\GitHub\\pyrung\\examples\\circuitpy_codegen_review.py:135'] = _clock_curr
    _rung_10_enabled = bool((bool(_t_Running)))
    if _rung_10_enabled:
        if len(range(0, 16)) > 16:
            raise ValueError(f"pack_bits destination width is 16 bits but block has {len(range(0, 16))} tags")
        _packed = 0
        for _bit_index, _src_idx in enumerate(range(0, 16)):
            if bool(_b_BITS[_src_idx]):
                _packed |= (1 << _bit_index)
        _packed_value = _wrap_int(int(_packed), 16, True)
        _t_PackedWord = _packed_value
    if _rung_10_enabled:
        if len(range(0, 2)) != 2:
            raise ValueError(f"pack_words requires exactly 2 source tags; got {len(range(0, 2))}")
        _lo_value = int(_b_WORDS[range(0, 2)[0]])
        _hi_value = int(_b_WORDS[range(0, 2)[1]])
        _packed = ((_hi_value << 16) | (_lo_value & 0xFFFF))
        _packed_value = _wrap_int(int(_packed), 32, True)
        _t_PackedDword = _packed_value
    if _rung_10_enabled:
        _text = ''.join(str(_b_TXT[_idx]) for _idx in range(0, 8))
        _text = _text.strip()
        try:
            _parsed = _parse_pack_text_value(_text, "DINT")
            _packed_value = _store_copy_value_to_type(_parsed, "DINT")
            _t_PackedDword = _packed_value
        except (TypeError, ValueError, OverflowError):
            pass
    if _rung_10_enabled:
        if len(range(0, 32)) > 32:
            raise ValueError(f"unpack_to_bits source width is 32 bits but block has {len(range(0, 32))} tags")
        _bits = (int(_t_PackedDword) & 0xFFFFFFFF)
        for _bit_index, _dst_idx in enumerate(range(0, 32)):
            _b_BITS[_dst_idx] = bool((_bits >> _bit_index) & 1)
    if _rung_10_enabled:
        if len(range(0, 2)) != 2:
            raise ValueError(f"unpack_to_words requires exactly 2 destination tags; got {len(range(0, 2))}")
        _bits = (int(_t_PackedDword) & 0xFFFFFFFF)
        _lo_word = (_bits & 0xFFFF)
        _hi_word = ((_bits >> 16) & 0xFFFF)
        _b_WORDS[range(0, 2)[0]] = _wrap_int(_lo_word, 16, True)
        _b_WORDS[range(0, 2)[1]] = _wrap_int(_hi_word, 16, True)
    _rung_11_enabled = bool(((bool(_t_Running) and bool(_t_AutoMode))))
    if _rung_11_enabled:
        _fn_result_1 = _fn_plus_offset(offset=5, value=_t_CalcOut)
        if _fn_result_1 is None:
            raise TypeError("run_function: 'plus_offset' returned None but outs were declared")
        if 'result' not in _fn_result_1:
            raise KeyError(
                f"run_function: 'plus_offset' missing key 'result'; got {sorted(_fn_result_1)}"
            )
        _t_FnOut = _store_copy_value_to_type(_fn_result_1['result'], "INT")
    _fn_result_2 = _fn_gated_scale(bool(_rung_11_enabled), factor=2, value=_t_FnOut)
    if _fn_result_2 is None:
        raise TypeError("run_enabled_function: 'gated_scale' returned None but outs were declared")
    if 'result' not in _fn_result_2:
        raise KeyError(
            f"run_enabled_function: 'gated_scale' missing key 'result'; got {sorted(_fn_result_2)}"
        )
    _t_FnOut = _store_copy_value_to_type(_fn_result_2['result'], "INT")
    if not (_rung_11_enabled):
        _mem['_oneshot:C:\\Users\\Sam\\Documents\\GitHub\\pyrung\\examples\\circuitpy_codegen_review.py:152:ForLoopInstruction'] = False
        if False:
            _b_DD[_resolve_index_b_DD(int((_t__forloop_idx + 1)))] = _store_copy_value_to_type((_t__forloop_idx + _t_Idx), "INT")
    elif not bool(_mem.get('_oneshot:C:\\Users\\Sam\\Documents\\GitHub\\pyrung\\examples\\circuitpy_codegen_review.py:152:ForLoopInstruction', False)):
        _iterations = max(0, int(_t_LoopCount))
        for _for_i in range(_iterations):
            _t__forloop_idx = _for_i
            if True:
                _b_DD[_resolve_index_b_DD(int((_t__forloop_idx + 1)))] = _store_copy_value_to_type((_t__forloop_idx + _t_Idx), "INT")
        _mem['_oneshot:C:\\Users\\Sam\\Documents\\GitHub\\pyrung\\examples\\circuitpy_codegen_review.py:152:ForLoopInstruction'] = True
    if _rung_11_enabled:
        _sub_service()
    _rung_12_enabled = bool(((_b_DD[_resolve_index_b_DD(int(_t_Idx))] > 0)))
    if _rung_12_enabled:
        _t_StepDone = True
    else:
        _t_StepDone = False
    _rung_13_enabled = bool(((bool(_t_AutoMode) or bool(_t_Found))))
    if _rung_13_enabled:
        _t_storage_sd_save_cmd = True
    else:
        _t_storage_sd_save_cmd = False
    _rung_14_enabled = bool((bool(_t_Abort)))
    if _rung_14_enabled:
        _t_storage_sd_delete_all_cmd = True
    else:
        _t_storage_sd_delete_all_cmd = False
    _rung_15_enabled = bool((bool(_t_Stop)))
    if _rung_15_enabled:
        _t_storage_sd_eject_cmd = True
    else:
        _t_storage_sd_eject_cmd = False

def _read_inputs():
    global _b_Slot1
    _mask_s1_1 = int(base.readDiscrete(1))
    _b_Slot1[0] = bool((_mask_s1_1 >> 0) & 1)
    _b_Slot1[1] = bool((_mask_s1_1 >> 1) & 1)
    _b_Slot1[2] = bool((_mask_s1_1 >> 2) & 1)
    _b_Slot1[3] = bool((_mask_s1_1 >> 3) & 1)
    _b_Slot1[4] = bool((_mask_s1_1 >> 4) & 1)
    _b_Slot1[5] = bool((_mask_s1_1 >> 5) & 1)
    _b_Slot1[6] = bool((_mask_s1_1 >> 6) & 1)
    _b_Slot1[7] = bool((_mask_s1_1 >> 7) & 1)

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
    base.writeDiscrete(_out_mask_s2_1, 2)

while True:
    scan_start = time.monotonic()
    _sd_write_status = False
    dt = scan_start - _last_scan_ts
    if dt < 0:
        dt = 0.0
    _last_scan_ts = scan_start
    _mem["_dt"] = dt

    _sd_save_cmd = bool(_t_storage_sd_save_cmd)
    _sd_eject_cmd = bool(_t_storage_sd_eject_cmd)
    _sd_delete_all_cmd = bool(_t_storage_sd_delete_all_cmd)
    _service_sd_commands()
    _t_storage_sd_save_cmd = _sd_save_cmd
    _t_storage_sd_eject_cmd = _sd_eject_cmd
    _t_storage_sd_delete_all_cmd = _sd_delete_all_cmd
    _read_inputs()
    _run_main_rungs()
    _write_outputs()

    _prev["Start"] = _t_Start
    _prev["Stop"] = _t_Stop

    _wd_pet()

    elapsed_ms = (time.monotonic() - scan_start) * 1000.0
    sleep_ms = TARGET_SCAN_MS - elapsed_ms
    if sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)
    else:
        _scan_overrun_count += 1
        if PRINT_SCAN_OVERRUNS:
            print(f"Scan overrun #{_scan_overrun_count}: {-sleep_ms:.3f} ms late")
