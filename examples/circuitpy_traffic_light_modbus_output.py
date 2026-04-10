# -- Imports -------------------------------------------------------------------
import gc
import json
import math
import os
import struct
import time

import adafruit_wiznet5k.adafruit_wiznet5k_socket as _mb_socket
import board
import busio
import digitalio
import P1AM
import sdcardio
import storage
from adafruit_wiznet5k.adafruit_wiznet5k import WIZNET5K

try:
    import microcontroller
except ImportError:
    microcontroller = None

# -- Configuration -------------------------------------------------------------
TARGET_SCAN_MS = 10.0
WATCHDOG_MS = 5000
PRINT_SCAN_OVERRUNS = False

_SLOT_MODULES = ["P1-08SIM", "P1-08TRS"]
_RET_DEFAULTS = {"GreenAcc": 0, "RedAcc": 0, "State": "r", "YellowAcc": 0}
_RET_TYPES = {"GreenAcc": "INT", "RedAcc": "INT", "State": "CHAR", "YellowAcc": "INT"}
_RET_SCHEMA = "00e8dfc526c074a1fede3a1f74f6074ccaea57c73dd3d7d71798052aac7f3a1f"

# -- Hardware bootstrap --------------------------------------------------------
base = P1AM.Base()
base.rollCall(_SLOT_MODULES)

_mb_cs = digitalio.DigitalInOut(board.D5)
_mb_spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
_mb_eth = WIZNET5K(_mb_spi, _mb_cs)
_mb_eth.ifconfig = ((192, 168, 1, 200), (255, 255, 255, 0), (192, 168, 1, 1), (0, 0, 0, 0))
_mb_socket.set_interface(_mb_eth)


_wd_config = getattr(base, "config_watchdog", None)
_wd_start = getattr(base, "start_watchdog", None)
_wd_pet = getattr(base, "pet_watchdog", None)
if _wd_config is None or _wd_start is None or _wd_pet is None:
    raise RuntimeError("P1AM snake_case watchdog API not found on Base() instance")
_wd_config(WATCHDOG_MS)
_wd_start()

# -- Tags and blocks -----------------------------------------------------------
# Scalars (non-block tags).
_t_GreenAcc = 0
_t_GreenDone = False
_t_RedAcc = 0
_t_RedDone = False
_t_RxBusy = False
_t_RxErr = False
_t_RxExCode = 0
_t_RxOk = False
_t_State = "r"
_t_WalkActive = False
_t_WalkRequest = False
_t_YellowAcc = 0
_t_YellowDone = False

# Blocks (list-backed; PLC addresses remain 1-based, list indexes are 0-based).
_b_Slot1 = [False] * 8
_b_Slot2 = [False] * 8

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

# -- Modbus TCP ----------------------------------------------------------------
_mb_server = _mb_socket.socket(_mb_socket.AF_INET, _mb_socket.SOCK_STREAM)
_mb_server.bind(("", 502))
_mb_server.listen(2)
_mb_server.settimeout(0)
_mb_clients = [None] * 2
_mb_buf = bytearray(260)


def service_modbus_server():
    try:
        _client, _addr = _mb_server.accept()
    except OSError:
        _client = None
    if _client is not None:
        _client.settimeout(0)
        for _idx in range(len(_mb_clients)):
            if _mb_clients[_idx] is None:
                _mb_clients[_idx] = _client
                _client = None
                break
        if _client is not None:
            _client.close()
    for _idx in range(len(_mb_clients)):
        _sock = _mb_clients[_idx]
        if _sock is None:
            continue
        try:
            _n = _sock.recv_into(_mb_buf)
        except OSError:
            _sock.close()
            _mb_clients[_idx] = None
            continue
        if not _n:
            _sock.close()
            _mb_clients[_idx] = None
            continue
        _resp = _mb_handle(_mb_buf, int(_n))
        if _resp is None:
            continue
        try:
            _sock.send(_resp)
        except OSError:
            _sock.close()
            _mb_clients[_idx] = None


_MB_CLIENT_IDLE = 0
_MB_CLIENT_CONNECTING = 1
_MB_CLIENT_SENDING = 2
_MB_CLIENT_WAITING = 3
_MB_CLIENT_DONE = 4
_MB_CLIENT_ERROR = 5


def _mb_client_close(job):
    _sock = job.get("socket")
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
    job["socket"] = None


def _mb_client_reset_runtime(job):
    _mb_client_close(job)
    job["request"] = b""
    job["sent_offset"] = 0
    job["rx_len"] = 0
    job["state"] = _MB_CLIENT_IDLE


def _mb_client_frame_length(data, n):
    if int(n) < 7:
        return None
    try:
        _length = struct.unpack(">H", bytes(data[4:6]))[0]
    except Exception:
        return None
    return 6 + int(_length)


def _mb_client_pack_register_values(bank, values):
    if bank in ("DS", "TD", "SD"):
        return [struct.unpack("<H", struct.pack("<h", int(_value)))[0] for _value in values]
    if bank in ("DD", "CTD"):
        _regs = []
        for _value in values:
            _regs.extend(struct.unpack("<HH", struct.pack("<i", int(_value))))
        return _regs
    if bank == "DF":
        _regs = []
        for _value in values:
            _regs.extend(struct.unpack("<HH", struct.pack("<f", float(_value))))
        return _regs
    if bank in ("DH", "XD", "YD"):
        return [int(_value) & 0xFFFF for _value in values]
    if bank == "TXT":
        _regs = []
        _index = 0
        while _index < len(values):
            _lo_raw = values[_index]
            _hi_raw = values[_index + 1] if (_index + 1) < len(values) else ""
            _lo = ord(_lo_raw[0]) if isinstance(_lo_raw, str) and _lo_raw else 0
            _hi = ord(_hi_raw[0]) if isinstance(_hi_raw, str) and _hi_raw else 0
            _regs.append((_lo & 0xFF) | ((_hi & 0xFF) << 8))
            _index += 2
        return _regs
    return [int(_value) & 0xFFFF for _value in values]


def _mb_client_unpack_register_values(bank, regs, logical_count):
    if bank in ("DS", "TD", "SD"):
        return [
            struct.unpack("<h", struct.pack("<H", int(_reg) & 0xFFFF))[0]
            for _reg in regs[:logical_count]
        ]
    if bank in ("DD", "CTD"):
        _values = []
        for _index in range(0, len(regs), 2):
            if (_index + 1) >= len(regs):
                break
            _values.append(
                struct.unpack(
                    "<i",
                    struct.pack("<HH", int(regs[_index]) & 0xFFFF, int(regs[_index + 1]) & 0xFFFF),
                )[0]
            )
        return _values[:logical_count]
    if bank == "DF":
        _values = []
        for _index in range(0, len(regs), 2):
            if (_index + 1) >= len(regs):
                break
            _values.append(
                struct.unpack(
                    "<f",
                    struct.pack("<HH", int(regs[_index]) & 0xFFFF, int(regs[_index + 1]) & 0xFFFF),
                )[0]
            )
        return _values[:logical_count]
    if bank in ("DH", "XD", "YD"):
        return [(int(_reg) & 0xFFFF) for _reg in regs[:logical_count]]
    if bank == "TXT":
        _values = []
        for _reg in regs:
            _lo = int(_reg) & 0xFF
            _hi = (int(_reg) >> 8) & 0xFF
            _values.append("" if _lo == 0 else chr(_lo))
            _values.append("" if _hi == 0 else chr(_hi))
        return _values[:logical_count]
    return [(int(_reg) & 0xFFFF) for _reg in regs[:logical_count]]


def _mb_client_i1_set_status(busy, success, error, exception_response):
    global _t_RxBusy, _t_RxErr, _t_RxExCode, _t_RxOk
    _t_RxBusy = bool(busy)
    _t_RxOk = bool(success)
    _t_RxErr = bool(error)
    _t_RxExCode = int(exception_response)


def _mb_client_i1_values():
    global _t_WalkRequest
    return [_t_WalkRequest]


def _mb_client_i1_build_request(tid):  # read coils: C1 (1 coil) on ped_panel
    _pdu = struct.pack(">BHH", 1, 16384, 1)
    return struct.pack(">HHHB", int(tid) & 0xFFFF, 0, len(_pdu) + 1, 1) + _pdu


def _mb_client_i1_apply_response(data, n):
    global _t_WalkRequest
    if int(n) < 8:
        return (False, 0)
    try:
        _tid, _pid, _length, _uid = struct.unpack(">HHHB", bytes(data[:7]))
    except Exception:
        return (False, 0)
    if _pid != 0 or _uid != 1:
        return (False, 0)
    if _tid != int(_mb_client_i1["tid"]):
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
    _t_WalkRequest = _store_copy_value_to_type(_values[0], "BOOL")
    return (True, 0)


_mb_client_i1 = {
    "name": "_mb_client_i1",
    "enabled": False,
    "state": _MB_CLIENT_IDLE,
    "socket": None,
    "request": b"",
    "sent_offset": 0,
    "rx_buf": bytearray(260),
    "rx_len": 0,
    "deadline": 0.0,
    "tid": 0,
    "host": "192.168.1.50",
    "port": 502,
    "timeout_s": 1.0,
    "build": _mb_client_i1_build_request,
    "apply": _mb_client_i1_apply_response,
    "set_status": _mb_client_i1_set_status,
}

_mb_client_jobs = [_mb_client_i1]


def service_modbus_client():
    _now = time.monotonic()
    for _job in _mb_client_jobs:
        if not bool(_job["enabled"]):
            _mb_client_reset_runtime(_job)
            _job["set_status"](False, False, False, 0)
            continue
        if _job["state"] in (_MB_CLIENT_DONE, _MB_CLIENT_ERROR):
            _mb_client_reset_runtime(_job)
        if _job["state"] == _MB_CLIENT_IDLE:
            _job["tid"] = (int(_job["tid"]) + 1) & 0xFFFF
            if _job["tid"] == 0:
                _job["tid"] = 1
            _job["request"] = _job["build"](_job["tid"])
            _job["sent_offset"] = 0
            _job["rx_len"] = 0
            _job["deadline"] = _now + float(_job["timeout_s"])
            _job["set_status"](True, False, False, 0)
            _job["state"] = _MB_CLIENT_CONNECTING
            continue
        if _job["state"] == _MB_CLIENT_CONNECTING:
            if _job["socket"] is None:
                try:
                    _job["socket"] = _mb_socket.socket(_mb_socket.AF_INET, _mb_socket.SOCK_STREAM)
                    _job["socket"].settimeout(0)
                except OSError:
                    _job["set_status"](False, False, True, 0)
                    _job["state"] = _MB_CLIENT_ERROR
                    continue
            try:
                _job["socket"].connect((_job["host"], int(_job["port"])))
            except OSError:
                if _now >= float(_job["deadline"]):
                    _job["set_status"](False, False, True, 0)
                    _job["state"] = _MB_CLIENT_ERROR
                    _mb_client_close(_job)
                else:
                    _job["state"] = _MB_CLIENT_CONNECTING
                continue
            _job["state"] = _MB_CLIENT_SENDING
            continue
        if _job["state"] == _MB_CLIENT_SENDING:
            try:
                _sent = int(_job["socket"].send(_job["request"][int(_job["sent_offset"]) :]))
            except OSError:
                _job["set_status"](False, False, True, 0)
                _job["state"] = _MB_CLIENT_ERROR
                _mb_client_close(_job)
                continue
            if _sent < 0:
                _sent = 0
            _job["sent_offset"] = int(_job["sent_offset"]) + _sent
            if int(_job["sent_offset"]) >= len(_job["request"]):
                _job["state"] = _MB_CLIENT_WAITING
            elif _now >= float(_job["deadline"]):
                _job["set_status"](False, False, True, 0)
                _job["state"] = _MB_CLIENT_ERROR
                _mb_client_close(_job)
            continue
        if _job["state"] != _MB_CLIENT_WAITING:
            continue
        try:
            _view = memoryview(_job["rx_buf"])[int(_job["rx_len"]) :]
            _n = int(_job["socket"].recv_into(_view))
        except OSError:
            _n = 0
        if _n > 0:
            _job["rx_len"] = int(_job["rx_len"]) + _n
            _frame_len = _mb_client_frame_length(_job["rx_buf"], _job["rx_len"])
            if _frame_len is not None and int(_job["rx_len"]) >= int(_frame_len):
                _ok, _exception = _job["apply"](_job["rx_buf"], int(_frame_len))
                if _ok:
                    _job["set_status"](False, True, False, 0)
                    _job["state"] = _MB_CLIENT_DONE
                else:
                    _job["set_status"](False, False, True, int(_exception))
                    _job["state"] = _MB_CLIENT_ERROR
                _mb_client_close(_job)
                continue
        if _now >= float(_job["deadline"]):
            _job["set_status"](False, False, True, 0)
            _job["state"] = _MB_CLIENT_ERROR
            _mb_client_close(_job)


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
    global \
        _t_GreenAcc, \
        _t_RedAcc, \
        _t_State, \
        _t_YellowAcc, \
        _sd_write_status, \
        _sd_error, \
        _sd_error_code
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
        with open(_MEMORY_PATH, encoding="utf-8") as f:
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
    _entry = values.get("GreenAcc")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_GreenAcc = max(-32768, min(32767, int(_entry.get("value", _t_GreenAcc))))
        except Exception:
            pass
    _entry = values.get("RedAcc")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_RedAcc = max(-32768, min(32767, int(_entry.get("value", _t_RedAcc))))
        except Exception:
            pass
    _entry = values.get("State")
    if isinstance(_entry, dict) and _entry.get("type") == "CHAR":
        try:
            _t_State = (
                _entry.get("value", _t_State)
                if isinstance(_entry.get("value", _t_State), str)
                else ""
            )
        except Exception:
            pass
    _entry = values.get("YellowAcc")
    if isinstance(_entry, dict) and _entry.get("type") == "INT":
        try:
            _t_YellowAcc = max(-32768, min(32767, int(_entry.get("value", _t_YellowAcc))))
        except Exception:
            pass
    _sd_error = False
    _sd_error_code = 0
    _sd_write_status = False


def save_memory():
    global \
        _t_GreenAcc, \
        _t_RedAcc, \
        _t_State, \
        _t_YellowAcc, \
        _sd_write_status, \
        _sd_error, \
        _sd_error_code
    if not _sd_available:
        return
    _sd_write_status = True
    values = {}
    if _t_GreenAcc != _RET_DEFAULTS["GreenAcc"]:
        values["GreenAcc"] = {"type": "INT", "value": _t_GreenAcc}
    if _t_RedAcc != _RET_DEFAULTS["RedAcc"]:
        values["RedAcc"] = {"type": "INT", "value": _t_RedAcc}
    if _t_State != _RET_DEFAULTS["State"]:
        values["State"] = {"type": "CHAR", "value": _t_State}
    if _t_YellowAcc != _RET_DEFAULTS["YellowAcc"]:
        values["YellowAcc"] = {"type": "INT", "value": _t_YellowAcc}
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
    # SC69 pulses for this serviced-command scan; reset occurs at next scan start.
    _sd_write_status = True


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
def _mb_reverse_coil(addr):
    # X (digital inputs)
    if 0 <= addr <= 31:
        _offset = addr - 0
        if _offset < 16:
            return ("X", _offset + 1)
        return ("X", 21 + (_offset - 16))
    if 32 <= addr <= 47:
        return ("X", 101 + (addr - 32))
    if 64 <= addr <= 79:
        return ("X", 201 + (addr - 64))
    if 96 <= addr <= 111:
        return ("X", 301 + (addr - 96))
    if 128 <= addr <= 143:
        return ("X", 401 + (addr - 128))
    if 160 <= addr <= 175:
        return ("X", 501 + (addr - 160))
    if 192 <= addr <= 207:
        return ("X", 601 + (addr - 192))
    if 224 <= addr <= 239:
        return ("X", 701 + (addr - 224))
    if 256 <= addr <= 271:
        return ("X", 801 + (addr - 256))
    # Y (digital outputs)
    if 8192 <= addr <= 8223:
        _offset = addr - 8192
        if _offset < 16:
            return ("Y", _offset + 1)
        return ("Y", 21 + (_offset - 16))
    if 8224 <= addr <= 8239:
        return ("Y", 101 + (addr - 8224))
    if 8256 <= addr <= 8271:
        return ("Y", 201 + (addr - 8256))
    if 8288 <= addr <= 8303:
        return ("Y", 301 + (addr - 8288))
    if 8320 <= addr <= 8335:
        return ("Y", 401 + (addr - 8320))
    if 8352 <= addr <= 8367:
        return ("Y", 501 + (addr - 8352))
    if 8384 <= addr <= 8399:
        return ("Y", 601 + (addr - 8384))
    if 8416 <= addr <= 8431:
        return ("Y", 701 + (addr - 8416))
    if 8448 <= addr <= 8463:
        return ("Y", 801 + (addr - 8448))
    # C (bit memory)
    if 16384 <= addr <= 18383:
        return ("C", (addr - 16384) + 1)
    # T (timer done bits)
    if 45056 <= addr <= 45555:
        return ("T", (addr - 45056) + 1)
    # CT (counter done bits)
    if 49152 <= addr <= 49401:
        return ("CT", (addr - 49152) + 1)
    # SC (system control bits)
    if 61440 <= addr <= 62439:
        return ("SC", (addr - 61440) + 1)
    return None


def _mb_reverse_register(addr):
    # DS (int memory)
    if 0 <= addr <= 4499:
        _offset = addr - 0
        return ("DS", (_offset // 1) + 1, _offset % 1)
    # DD (double-int memory)
    if 16384 <= addr <= 18383:
        _offset = addr - 16384
        return ("DD", (_offset // 2) + 1, _offset % 2)
    # DH (hex/word memory)
    if 24576 <= addr <= 25075:
        _offset = addr - 24576
        return ("DH", (_offset // 1) + 1, _offset % 1)
    # DF (float memory)
    if 28672 <= addr <= 29671:
        _offset = addr - 28672
        return ("DF", (_offset // 2) + 1, _offset % 2)
    # TXT (text memory)
    if 36864 <= addr <= 37363:
        return ("TXT", ((addr - 36864) * 2) + 1, 0)
    # TD (timer accumulators)
    if 45056 <= addr <= 45555:
        _offset = addr - 45056
        return ("TD", (_offset // 1) + 1, _offset % 1)
    # CTD (counter accumulators)
    if 49152 <= addr <= 49651:
        _offset = addr - 49152
        return ("CTD", (_offset // 2) + 1, _offset % 2)
    # XD (input words)
    if 57344 <= addr <= 57360:
        return ("XD", addr - 57344, 0)
    # YD (output words)
    if 57856 <= addr <= 57872:
        return ("YD", addr - 57856, 0)
    # SD (system data)
    if 61440 <= addr <= 62439:
        _offset = addr - 61440
        return ("SD", (_offset // 1) + 1, _offset % 1)
    return None


def _mb_xy_word_start(word_index):
    _idx = int(word_index)
    if _idx < 0 or _idx > 16:
        return None
    return (
        1
        if int(word_index) == 0
        else 21
        if int(word_index) == 1
        else (((int(word_index) // 2) * 100) + 1)
    )


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
    if bank == "T" and index == 1:
        return bool(_t_RedDone)
    if bank == "T" and index == 2:
        return bool(_t_GreenDone)
    if bank == "T" and index == 3:
        return bool(_t_YellowDone)
    return False


def _mb_write_coil_plc(bank, index, val):
    global \
        _t_GreenDone, \
        _t_RedDone, \
        _t_RxBusy, \
        _t_RxErr, \
        _t_RxOk, \
        _t_WalkActive, \
        _t_WalkRequest, \
        _t_YellowDone
    _value = bool(val)
    if bank == "C" and index == 1:
        _t_WalkActive = _value
        return True
    if bank == "C" and index == 2:
        _t_WalkRequest = _value
        return True
    if bank == "C" and index == 3:
        _t_RxBusy = _value
        return True
    if bank == "C" and index == 4:
        _t_RxOk = _value
        return True
    if bank == "C" and index == 5:
        _t_RxErr = _value
        return True
    if bank == "T" and index == 1:
        _t_RedDone = _value
        return True
    if bank == "T" and index == 2:
        _t_GreenDone = _value
        return True
    if bank == "T" and index == 3:
        _t_YellowDone = _value
        return True
    if bank in ("Y", "C"):
        return True
    if bank == "SC":
        return index in [53, 55, 60, 61, 65, 66, 67, 75, 76, 120, 121]
    return False


def _mb_read_mirrored_word(bank, word_index):
    _start = _mb_xy_word_start(word_index)
    if _start is None:
        return 0
    _word = 0
    for _bit_index in range(16):
        if _mb_read_coil_plc(bank, _start + _bit_index):
            _word |= 1 << _bit_index
    return _word


def _mb_write_mirrored_word(word_index, value):
    _start = _mb_xy_word_start(word_index)
    if _start is None:
        return False
    _word = int(value) & 0xFFFF
    for _bit_index in range(16):
        _mb_write_coil_plc("Y", _start + _bit_index, bool((_word >> _bit_index) & 0x1))
    return True


def _mb_read_reg_plc(bank, index, reg_pos):
    if bank == "XD":
        return _mb_read_mirrored_word("X", index)
    if bank == "YD":
        return _mb_read_mirrored_word("Y", index)
    if bank == "DS" and index == 1:
        return struct.unpack("<H", struct.pack("<h", int(_t_RxExCode)))[0]
    if bank == "TD" and index == 1:
        return struct.unpack("<H", struct.pack("<h", int(_t_RedAcc)))[0]
    if bank == "TD" and index == 2:
        return struct.unpack("<H", struct.pack("<h", int(_t_GreenAcc)))[0]
    if bank == "TD" and index == 3:
        return struct.unpack("<H", struct.pack("<h", int(_t_YellowAcc)))[0]
    if bank == "TXT" and index == 1:
        return ((ord(_t_State) if _t_State else 0) & 0xFF) | (((0) & 0xFF) << 8)
    return 0


def _mb_write_reg_plc(bank, index, reg_pos, value):
    global _t_GreenAcc, _t_RedAcc, _t_RxExCode, _t_State, _t_YellowAcc
    _word = int(value) & 0xFFFF
    if bank == "XD":
        return False
    if bank == "YD":
        return _mb_write_mirrored_word(index, _word)
    if bank == "DS" and index == 1:
        _t_RxExCode = struct.unpack("<h", struct.pack("<H", _word))[0]
        return True
    if bank == "TD" and index == 1:
        _t_RedAcc = struct.unpack("<h", struct.pack("<H", _word))[0]
        return True
    if bank == "TD" and index == 2:
        _t_GreenAcc = struct.unpack("<h", struct.pack("<H", _word))[0]
        return True
    if bank == "TD" and index == 3:
        _t_YellowAcc = struct.unpack("<h", struct.pack("<H", _word))[0]
        return True
    if bank == "TXT" and index == 1:
        _t_State = chr(_word & 0xFF)
        return True
    if bank in ("DS", "DD", "DH", "DF", "TXT", "TD", "CTD"):
        return True
    if bank == "SD":
        return index in [
            29,
            31,
            32,
            34,
            35,
            36,
            40,
            41,
            42,
            50,
            51,
            60,
            61,
            106,
            107,
            108,
            112,
            113,
            114,
            140,
            141,
            142,
            143,
            144,
            145,
            146,
            147,
            214,
            215,
        ]
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


def _mb_err(tid, uid, fc, code):
    return struct.pack(
        ">HHHBB", int(tid) & 0xFFFF, 0, 3, int(uid) & 0xFF, (int(fc) & 0x7F) | 0x80
    ) + bytes([int(code) & 0xFF])


def _mb_handle(data, n):
    if n < 8:
        return None
    try:
        tid, pid, length, uid = struct.unpack(">HHHB", bytes(data[:7]))
    except Exception:
        return None
    if pid != 0:
        return None
    if length < 2 or (length + 6) > int(n):
        return None
    fc = int(data[7])
    pdu_end = 6 + length
    if fc in (1, 2):
        if pdu_end < 12:
            return _mb_err(tid, uid, fc, 3)
        start, count = struct.unpack(">HH", bytes(data[8:12]))
        if count < 1 or count > 2000:
            return _mb_err(tid, uid, fc, 3)
        bits = []
        for _offset in range(count):
            _bit = _mb_read_coil(start + _offset)
            if _bit is None:
                return _mb_err(tid, uid, fc, 2)
            bits.append(bool(_bit))
        byte_count = (count + 7) // 8
        payload = bytearray(byte_count)
        for _offset, _bit in enumerate(bits):
            if _bit:
                payload[_offset // 8] |= 1 << (_offset % 8)
        return struct.pack(">HHHBBB", tid, 0, len(payload) + 3, uid, fc, len(payload)) + bytes(
            payload
        )
    if fc in (3, 4):
        if pdu_end < 12:
            return _mb_err(tid, uid, fc, 3)
        start, count = struct.unpack(">HH", bytes(data[8:12]))
        if count < 1 or count > 125:
            return _mb_err(tid, uid, fc, 3)
        regs = []
        for _offset in range(count):
            _reg = _mb_read_reg(start + _offset)
            if _reg is None:
                return _mb_err(tid, uid, fc, 2)
            regs.append(int(_reg) & 0xFFFF)
        payload = bytearray()
        for _reg in regs:
            payload.extend(struct.pack(">H", _reg))
        return struct.pack(">HHHBBB", tid, 0, len(payload) + 3, uid, fc, len(payload)) + bytes(
            payload
        )
    if fc == 5:
        if pdu_end < 12:
            return _mb_err(tid, uid, fc, 3)
        addr, raw = struct.unpack(">HH", bytes(data[8:12]))
        if raw not in (0x0000, 0xFF00):
            return _mb_err(tid, uid, fc, 3)
        if not _mb_write_coil(addr, raw == 0xFF00):
            return _mb_err(tid, uid, fc, 2)
        return bytes(data[:12])
    if fc == 6:
        if pdu_end < 12:
            return _mb_err(tid, uid, fc, 3)
        addr, raw = struct.unpack(">HH", bytes(data[8:12]))
        if not _mb_write_reg(addr, raw):
            return _mb_err(tid, uid, fc, 2)
        return bytes(data[:12])
    if fc == 15:
        if pdu_end < 13:
            return _mb_err(tid, uid, fc, 3)
        start, count, byte_count = struct.unpack(">HHB", bytes(data[8:13]))
        if count < 1 or count > 1968 or byte_count != ((count + 7) // 8):
            return _mb_err(tid, uid, fc, 3)
        if pdu_end < 13 + byte_count:
            return _mb_err(tid, uid, fc, 3)
        payload = data[13 : 13 + byte_count]
        for _offset in range(count):
            _bit = bool((payload[_offset // 8] >> (_offset % 8)) & 0x1)
            if not _mb_write_coil(start + _offset, _bit):
                return _mb_err(tid, uid, fc, 2)
        return struct.pack(">HHHBBHH", tid, 0, 6, uid, fc, start, count)
    if fc == 16:
        if pdu_end < 13:
            return _mb_err(tid, uid, fc, 3)
        start, count, byte_count = struct.unpack(">HHB", bytes(data[8:13]))
        if count < 1 or count > 123 or byte_count != (count * 2):
            return _mb_err(tid, uid, fc, 3)
        if pdu_end < 13 + byte_count:
            return _mb_err(tid, uid, fc, 3)
        for _offset in range(count):
            _base = 13 + (_offset * 2)
            _reg = struct.unpack(">H", bytes(data[_base : _base + 2]))[0]
            if not _mb_write_reg(start + _offset, _reg):
                return _mb_err(tid, uid, fc, 2)
        return struct.pack(">HHHBBHH", tid, 0, 6, uid, fc, start, count)
    return _mb_err(tid, uid, fc, 1)


# Embedded function call targets.
# None emitted in foundation step.


# -- Ladder logic --------------------------------------------------------------
def _run_main_rungs():
    global \
        _b_Slot1, \
        _b_Slot2, \
        _mb_client_i1, \
        _mem, \
        _prev, \
        _t_GreenAcc, \
        _t_GreenDone, \
        _t_RedAcc, \
        _t_RedDone, \
        _t_RxBusy, \
        _t_RxErr, \
        _t_RxExCode, \
        _t_RxOk, \
        _t_State, \
        _t_WalkActive, \
        _t_WalkRequest, \
        _t_YellowAcc, \
        _t_YellowDone
    _rung_1_enabled = (_t_State == "r") and (not bool(_b_Slot1[0]))
    _frac = float(_mem.get("_frac:RedAcc", 0.0))
    if _rung_1_enabled:
        _dt = float(_mem.get("_dt", 0.0))
        _acc = int(_t_RedAcc)
        _dt_units = (_dt * 1000.0) + _frac
        _int_units = int(_dt_units)
        _new_frac = _dt_units - _int_units
        _acc = min(_acc + _int_units, 32767)
        _preset = 5000
        _mem["_frac:RedAcc"] = _new_frac
        _t_RedDone = _acc >= _preset
        _t_RedAcc = _acc
    else:
        _mem["_frac:RedAcc"] = 0.0
        _t_RedDone = False
        _t_RedAcc = 0
    _rung_2_enabled = bool(_t_RedDone)
    if _rung_2_enabled:
        _t_State = _store_copy_value_to_type("g", "CHAR")
    _rung_3_enabled = (_t_State == "g") and (not bool(_b_Slot1[0]))
    _frac = float(_mem.get("_frac:GreenAcc", 0.0))
    if _rung_3_enabled:
        _dt = float(_mem.get("_dt", 0.0))
        _acc = int(_t_GreenAcc)
        _dt_units = (_dt * 1000.0) + _frac
        _int_units = int(_dt_units)
        _new_frac = _dt_units - _int_units
        _acc = min(_acc + _int_units, 32767)
        _preset = 4000
        _mem["_frac:GreenAcc"] = _new_frac
        _t_GreenDone = _acc >= _preset
        _t_GreenAcc = _acc
    else:
        _mem["_frac:GreenAcc"] = 0.0
        _t_GreenDone = False
        _t_GreenAcc = 0
    _rung_4_enabled = bool(_t_GreenDone)
    if _rung_4_enabled:
        _t_State = _store_copy_value_to_type("y", "CHAR")
    _rung_5_enabled = (_t_State == "y") and (not bool(_b_Slot1[0]))
    _frac = float(_mem.get("_frac:YellowAcc", 0.0))
    if _rung_5_enabled:
        _dt = float(_mem.get("_dt", 0.0))
        _acc = int(_t_YellowAcc)
        _dt_units = (_dt * 1000.0) + _frac
        _int_units = int(_dt_units)
        _new_frac = _dt_units - _int_units
        _acc = min(_acc + _int_units, 32767)
        _preset = 1500
        _mem["_frac:YellowAcc"] = _new_frac
        _t_YellowDone = _acc >= _preset
        _t_YellowAcc = _acc
    else:
        _mem["_frac:YellowAcc"] = 0.0
        _t_YellowDone = False
        _t_YellowAcc = 0
    _rung_6_enabled = bool(_t_YellowDone)
    if _rung_6_enabled:
        _t_State = _store_copy_value_to_type("r", "CHAR")
    _rung_7_enabled = _rise(bool(_t_WalkRequest), bool(_prev.get("WalkRequest", False))) or _rise(
        bool(_b_Slot1[1]), bool(_prev.get("Slot1.2", False))
    )
    if _rung_7_enabled:
        _t_WalkActive = True
    else:
        _t_WalkActive = False
    _rung_8_enabled = bool(_t_GreenDone)
    if _rung_8_enabled:
        _t_WalkActive = _store_copy_value_to_type(False, "BOOL")
    _rung_9_enabled = _t_State == "r"
    if _rung_9_enabled:
        _b_Slot2[0] = True
    else:
        _b_Slot2[0] = False
    _rung_10_enabled = _t_State == "g"
    if _rung_10_enabled:
        _b_Slot2[2] = True
    else:
        _b_Slot2[2] = False
    _rung_11_enabled = _t_State == "y"
    if _rung_11_enabled:
        _b_Slot2[1] = True
    else:
        _b_Slot2[1] = False
    _rung_12_enabled = True
    _mb_client_i1["enabled"] = bool(_rung_12_enabled)


# -- I/O -----------------------------------------------------------------------
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
        _out_mask_s2_1 |= 1 << 0
    if bool(_b_Slot2[1]):
        _out_mask_s2_1 |= 1 << 1
    if bool(_b_Slot2[2]):
        _out_mask_s2_1 |= 1 << 2
    if bool(_b_Slot2[3]):
        _out_mask_s2_1 |= 1 << 3
    if bool(_b_Slot2[4]):
        _out_mask_s2_1 |= 1 << 4
    if bool(_b_Slot2[5]):
        _out_mask_s2_1 |= 1 << 5
    if bool(_b_Slot2[6]):
        _out_mask_s2_1 |= 1 << 6
    if bool(_b_Slot2[7]):
        _out_mask_s2_1 |= 1 << 7
    base.writeDiscrete(_out_mask_s2_1, 2)


# -- Main scan loop ------------------------------------------------------------
gc.disable()

while True:
    scan_start = time.monotonic()
    _sd_write_status = False
    dt = scan_start - _last_scan_ts
    if dt < 0:
        dt = 0.0
    _last_scan_ts = scan_start
    _mem["_dt"] = dt

    _service_sd_commands()
    _read_inputs()
    _run_main_rungs()
    _write_outputs()

    service_modbus_server()
    service_modbus_client()

    _prev["Slot1.2"] = _b_Slot1[1]
    _prev["WalkRequest"] = _t_WalkRequest

    _wd_pet()

    elapsed_ms = (time.monotonic() - scan_start) * 1000.0
    sleep_ms = TARGET_SCAN_MS - elapsed_ms
    if sleep_ms > 0:
        time.sleep(sleep_ms / 1000.0)
    else:
        _scan_overrun_count += 1
        if PRINT_SCAN_OVERRUNS:
            print(f"Scan overrun #{_scan_overrun_count}: {-sleep_ms:.3f} ms late")

    gc.collect()
