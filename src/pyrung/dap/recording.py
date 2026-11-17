"""Saved recordings, with the header of a transcript and a replay with no debugger attached.

A recording is a transcript of console commands. Its header is a block of ``# key: value``
comment lines at the top, so ``replay`` and a paste into the console skip it like any comment.
The header names the program, its scan time and whether its harness was on, so a replay
runs the commands the way they ran.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

#: A file with this suffix is a recording. The pytest plugin collects every one.
RECORDING_SUFFIX = ".rec"

_ACTION = "action"
_PROGRAM = "program"
_PROGRAM_SHA256 = "program sha256"
_PYRUNG = "pyrung"
_SCAN_TIME = "scan time"
_HARNESS = "harness"
_REALTIME = "realtime"


class RecordingFormatError(ValueError):
    """A header line that replay cannot read."""

    def __init__(self, line: int, message: str) -> None:
        super().__init__(message)
        self.line = line


class ReplayFailed(Exception):
    """The first line of a recording that failed, and why."""

    def __init__(self, line: int | None, reason: str) -> None:
        super().__init__(f"line {line}: {reason}" if line is not None else reason)
        self.line = line
        self.reason = reason


@dataclass(frozen=True)
class RecordingHeader:
    """What a recording says about the session it came from.

    ``program`` is the path as the file writes it, relative to the recording's folder.
    ``scan_time`` is in seconds, and ``realtime`` means scans took their time from the clock.
    """

    action: str | None = None
    program: str | None = None
    program_sha256: str | None = None
    pyrung_version: str | None = None
    scan_time: float | None = None
    realtime: bool = False
    harness: bool | None = None

    def lines(self) -> list[str]:
        out: list[str] = []
        if self.action is not None:
            out.append(f"# {_ACTION}: {self.action}")
        if self.program is not None:
            out.append(f"# {_PROGRAM}: {self.program}")
        if self.program_sha256 is not None:
            out.append(f"# {_PROGRAM_SHA256}: {self.program_sha256}")
        if self.pyrung_version is not None:
            out.append(f"# {_PYRUNG}: {self.pyrung_version}")
        if self.realtime:
            out.append(f"# {_SCAN_TIME}: {_REALTIME}")
        elif self.scan_time is not None:
            # repr() reads back as the same float, so the replay's timers count the same.
            out.append(f"# {_SCAN_TIME}: {self.scan_time!r} s")
        if self.harness is not None:
            out.append(f"# {_HARNESS}: {'on' if self.harness else 'off'}")
        return out


def parse_header(lines: list[str]) -> RecordingHeader:
    """Read the ``# key: value`` lines above the first command.

    A comment that is not one of the keys is left alone, so a note at the top stays a note.
    """
    values: dict[str, Any] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("#"):
            break
        key, sep, value = stripped[1:].partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not sep:
            continue
        lineno = index + 1
        if key == _ACTION:
            values["action"] = value
        elif key == _PROGRAM:
            # A path typed by hand on Windows works on every machine that replays it.
            values["program"] = value.replace("\\", "/")
        elif key == _PROGRAM_SHA256:
            values["program_sha256"] = value
        elif key == _PYRUNG:
            values["pyrung_version"] = value
        elif key == _SCAN_TIME:
            if value.lower() == _REALTIME:
                values["realtime"] = True
            else:
                values["scan_time"] = _parse_scan_time(lineno, value)
        elif key == _HARNESS:
            if value.lower() not in {"on", "off"}:
                raise RecordingFormatError(lineno, f"harness must be on or off, not '{value}'")
            values["harness"] = value.lower() == "on"
    return RecordingHeader(**values)


def _parse_scan_time(lineno: int, value: str) -> float:
    number = value.lower().removesuffix("s").strip()
    try:
        seconds = float(number)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise RecordingFormatError(
            lineno, f"scan time must be seconds above 0 (such as 0.01 s) or realtime, not '{value}'"
        )
    return seconds


@dataclass(frozen=True)
class FinishedRecording:
    """A stopped recording, kept until ``record save`` writes it."""

    header: RecordingHeader
    program_path: Path | None
    commands: list[str] = field(default_factory=list)

    def text(self, folder: Path | None) -> str:
        """The file's text, naming the program relative to *folder*."""
        header = self.header
        if self.program_path is not None:
            header = replace(header, program=_relative_path(self.program_path, folder))
        return "\n".join([*header.lines(), *self.commands]) + "\n"


def _relative_path(path: Path, folder: Path | None) -> str:
    if folder is None:
        return path.as_posix()
    try:
        return Path(os.path.relpath(path, folder)).as_posix()
    except ValueError:  # another drive on Windows
        return path.as_posix()


def program_sha256(path: Path) -> str | None:
    """The SHA-256 of the program file, so a reader can tell the program changed since."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def pyrung_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("pyrung")
    except PackageNotFoundError:
        return None


def replay_recording(path: Path) -> str:
    """Replay a saved recording on a fresh load of its program, with no debugger attached.

    Runs the same replayer as the console's ``replay``, so pytest and the console agree on
    what a recording does. Raises ``ReplayFailed`` at the first line that fails.
    """
    from pyrung.dap.adapter import DAPAdapter, DAPAdapterError
    from pyrung.dap.capture import replay_file
    from pyrung.dap.handlers.lifecycle_launch import load_program
    from pyrung.dap.harness_console import uninstall_harness

    path = Path(path).resolve()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        header = parse_header(lines)
    except OSError as exc:
        raise ReplayFailed(None, f"cannot read {path}: {exc}") from exc
    except RecordingFormatError as exc:
        raise ReplayFailed(exc.line, str(exc)) from exc
    if header.program is None:
        raise ReplayFailed(None, "the recording names no program (add a '# program: <file>' line)")
    program = (path.parent / header.program).resolve()
    if not program.is_file():
        raise ReplayFailed(None, f"program not found: {program}")

    adapter = DAPAdapter(in_stream=io.BytesIO(), out_stream=io.BytesIO())
    try:
        with _program_folder_importable(program.parent):
            try:
                load_program(adapter, program)
            except DAPAdapterError:
                raise
            except Exception as exc:
                raise ReplayFailed(
                    None, f"program failed to load: {type(exc).__name__}: {exc}"
                ) from exc
            with adapter._state_lock:
                result = replay_file(adapter, path, None)
    except DAPAdapterError as exc:
        line = getattr(exc, "replay_line", None)
        reason = getattr(exc, "replay_reason", None) or str(exc)
        raise ReplayFailed(line, reason) from exc
    finally:
        uninstall_harness(adapter)
    return result.text


@contextmanager
def _program_folder_importable(folder: Path) -> Iterator[None]:
    """Let the program import the modules beside it, as a launch from its folder does.

    The modules it imported from there are dropped afterwards, because another recording in
    the same pytest process may belong to a program with its own ``helpers.py``. Only a module
    whose top-level name is a file or folder in *folder* goes. A package the folder holds
    deeper down (a virtualenv, an editable install under ``src/``) stays loaded, since a
    second copy of it would fail ``isinstance`` against the first.

    A module of the same name already loaded from somewhere else is set aside meanwhile, so
    the program gets its own sibling and not that one. It comes back afterwards.
    """
    entry = str(folder)
    # A folder with no __init__.py loses to a package of that name further down sys.path,
    # so only a .py file or a real package here takes over a name. A standard-library name
    # never does, because in a launch that module is loaded before the program runs.
    tops = {path.stem for path in folder.glob("*.py")} | {
        path.parent.name for path in folder.glob("*/__init__.py")
    }
    tops -= sys.stdlib_module_names
    shadowed = {
        name: module
        for name, module in list(sys.modules.items())
        if name.partition(".")[0] in tops and not _imported_from(folder, name, module)
    }
    for name in shadowed:
        del sys.modules[name]
    before = set(sys.modules)
    sys.path.insert(0, entry)
    try:
        yield
    finally:
        try:
            sys.path.remove(entry)
        except ValueError:
            pass
        try:
            for name in set(sys.modules) - before:
                if _imported_from(folder, name, sys.modules.get(name)):
                    del sys.modules[name]
        finally:
            sys.modules.update(shadowed)


def _imported_from(folder: Path, name: str, module: Any) -> bool:
    top = name.partition(".")[0]
    top_file = folder / f"{top}.py"
    top_package = folder / top
    if not (top_file.is_file() or top_package.is_dir()):
        return False
    module_file = getattr(module, "__file__", None)
    if module_file is None:  # a namespace package, which has a folder but no file
        return top_package.is_dir()
    module_path = Path(module_file).resolve()
    return module_path == top_file.resolve() or module_path.is_relative_to(top_package.resolve())
