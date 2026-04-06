"""Split ``core/instruction/send_receive.py`` into a package.

The split is intentionally conservative:

- Copy existing section bodies verbatim into new modules.
- Keep ``ModbusSendInstruction`` / ``ModbusReceiveInstruction`` at the
  package root so existing monkeypatch-heavy tests keep working.
- Use ``ruff`` afterwards for import cleanup and formatting.
"""

from __future__ import annotations

from pathlib import Path
import re
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/pyrung/core/instruction/send_receive.py"
PACKAGE = ROOT / "src/pyrung/core/instruction/send_receive"
FUTURE_IMPORT = "from __future__ import annotations\n"

SECTION_RE = re.compile(
    r"(?m)^# ---------------------------------------------------------------------------\n"
    r"# (?P<title>.+)\n"
    r"# ---------------------------------------------------------------------------\n"
)

SECTION_GROUPS = {
    "types": [
        "enums",
        "modbusaddress - modbus register address",
        "target dataclasses",
    ],
    "helpers": [
        "click-specific helpers",
        "generic helpers",
        "raw modbus value packing / unpacking",
    ],
    "backends": [
        "async modbus backend - click path",
        "async modbus backend - raw path (pymodbus)",
        "shared backend helpers",
    ],
    "root": [
        "instruction classes",
        "public dsl functions",
    ],
}


def _split_docstring(source_text: str) -> tuple[str, str]:
    try:
        future_index = source_text.index(FUTURE_IMPORT)
    except ValueError as exc:
        raise RuntimeError(f"Could not find {FUTURE_IMPORT!r} in {SOURCE}") from exc
    return source_text[:future_index], source_text[future_index + len(FUTURE_IMPORT) :]


def _extract_sections(source_text: str) -> dict[str, str]:
    matches = list(SECTION_RE.finditer(source_text))
    if not matches:
        raise RuntimeError(f"Could not find any section banners in {SOURCE}")

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = _normalize_title(match.group("title"))
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source_text)
        sections[title] = source_text[start:end].strip() + "\n"
    return sections


def _normalize_title(title: str) -> str:
    return (
        title.replace("\u2014", "-").replace("\u2013", "-").replace("â€”", "-").strip().casefold()
    )


def _require_sections(sections: dict[str, str], titles: list[str]) -> list[str]:
    missing = [title for title in titles if title not in sections]
    if missing:
        raise RuntimeError(f"Missing expected section(s) in {SOURCE}: {', '.join(missing)}")
    return [sections[title] for title in titles]


def _join_blocks(*blocks: str) -> str:
    parts = [block.strip() for block in blocks if block.strip()]
    return "\n\n".join(parts) + "\n"


def _render_types_module(sections: dict[str, str]) -> str:
    body = _join_blocks(*_require_sections(sections, SECTION_GROUPS["types"]))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            import enum
            from dataclasses import dataclass
            """
        ),
        body,
    )


def _render_helpers_module(sections: dict[str, str]) -> str:
    body = _join_blocks(*_require_sections(sections, SECTION_GROUPS["helpers"]))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            import struct
            from typing import TYPE_CHECKING, Any

            from pyclickplc.banks import BANKS

            from pyrung.core.memory_block import BlockRange
            from pyrung.core.tag import Tag, TagType

            from ..resolvers import resolve_block_range_tags_ctx, resolve_tag_ctx
            from .types import RegisterType, WordOrder

            if TYPE_CHECKING:
                from pyrung.core.context import ScanContext
            """
        ),
        body,
    )


def _render_backends_module(sections: dict[str, str]) -> str:
    body = _join_blocks(*_require_sections(sections, SECTION_GROUPS["backends"]))
    return _join_blocks(
        '"""Automatically generated module split."""',
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            import asyncio
            import re
            from concurrent.futures import Future, ThreadPoolExecutor
            from dataclasses import dataclass
            from typing import Any

            from pyclickplc import ClickClient
            from pyclickplc.addresses import format_address_display
            from pyclickplc.banks import BANKS

            from pyrung.core.tag import Tag

            from .helpers import _contiguous_runs
            from .types import ModbusRtuTarget, ModbusTcpTarget, RegisterType

            _EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pyrung-modbus")
            _DEFAULT_TIMEOUT_SECONDS = 1
            """
        ),
        body,
    )


def _render_root_module(docstring: str, sections: dict[str, str]) -> str:
    body = _join_blocks(*_require_sections(sections, SECTION_GROUPS["root"]))
    return _join_blocks(
        docstring,
        FUTURE_IMPORT.strip(),
        textwrap.dedent(
            """
            from concurrent.futures import Future
            from dataclasses import dataclass, field
            from typing import TYPE_CHECKING, Any

            from pyclickplc.addresses import parse_address

            from pyrung.core._source import _capture_source
            from pyrung.core.memory_block import BlockRange
            from pyrung.core.program.context import _require_rung_context
            from pyrung.core.tag import Tag

            from ..base import Instruction
            from ..conversions import _store_copy_value_to_tag_type
            from . import backends as _backends
            from .helpers import (
                _addresses_for_count,
                _calculate_register_count,
                _normalize_operand_count,
                _normalize_operand_tags,
                _pack_values_to_registers,
                _preview_operand_tag_types,
                _status_clear_tags,
                _unpack_registers_to_values,
                _validate_status_tags,
            )
            from .types import (
                ModbusAddress,
                ModbusRtuTarget,
                ModbusTcpTarget,
                RegisterType,
                VALID_COM_PORTS,
                WordOrder,
            )

            if TYPE_CHECKING:
                from pyrung.core.context import ScanContext

            ClickClient = _backends.ClickClient
            _PendingRequest = _backends._PendingRequest
            _RequestResult = _backends._RequestResult
            _create_raw_client = _backends._create_raw_client
            _discard_pending_request = _backends._discard_pending_request
            _extract_exception_code = _backends._extract_exception_code
            _run_raw_receive_request = _backends._run_raw_receive_request
            _run_raw_send_request = _backends._run_raw_send_request


            def _submit_click_send_request(
                *,
                host: str,
                port: int,
                device_id: int,
                bank: str,
                addresses: tuple[int, ...],
                values: tuple[Any, ...],
            ) -> Future[_RequestResult]:
                _backends.ClickClient = ClickClient
                return _backends._submit_click_send_request(
                    host=host,
                    port=port,
                    device_id=device_id,
                    bank=bank,
                    addresses=addresses,
                    values=values,
                )


            def _submit_click_receive_request(
                *,
                host: str,
                port: int,
                device_id: int,
                bank: str,
                start: int,
                end: int,
            ) -> Future[_RequestResult]:
                _backends.ClickClient = ClickClient
                return _backends._submit_click_receive_request(
                    host=host,
                    port=port,
                    device_id=device_id,
                    bank=bank,
                    start=start,
                    end=end,
                )


            def _run_click_send_request(
                host: str,
                port: int,
                device_id: int,
                bank: str,
                addresses: tuple[int, ...],
                values: tuple[Any, ...],
            ) -> _RequestResult:
                _backends.ClickClient = ClickClient
                return _backends._run_click_send_request(
                    host,
                    port,
                    device_id,
                    bank,
                    addresses,
                    values,
                )


            def _run_click_receive_request(
                host: str,
                port: int,
                device_id: int,
                bank: str,
                start: int,
                end: int,
            ) -> _RequestResult:
                _backends.ClickClient = ClickClient
                return _backends._run_click_receive_request(
                    host,
                    port,
                    device_id,
                    bank,
                    start,
                    end,
                )


            def _submit_raw_send_request(
                *,
                target: ModbusTcpTarget | ModbusRtuTarget,
                address: int,
                register_type: RegisterType,
                registers: list[Any],
                device_id: int,
            ) -> Future[_RequestResult]:
                return _backends._submit_raw_send_request(
                    target=target,
                    address=address,
                    register_type=register_type,
                    registers=registers,
                    device_id=device_id,
                )


            def _submit_raw_receive_request(
                *,
                target: ModbusTcpTarget | ModbusRtuTarget,
                address: int,
                register_type: RegisterType,
                count: int,
                device_id: int,
            ) -> Future[_RequestResult]:
                return _backends._submit_raw_receive_request(
                    target=target,
                    address=address,
                    register_type=register_type,
                    count=count,
                    device_id=device_id,
                )
            """
        ),
        body,
    )


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Expected source module at {SOURCE}")

    if PACKAGE.exists() and any(PACKAGE.iterdir()):
        raise SystemExit(f"Refusing to overwrite existing package at {PACKAGE}")

    source_text = SOURCE.read_text(encoding="utf-8")
    docstring, body_text = _split_docstring(source_text)
    sections = _extract_sections(body_text)

    PACKAGE.mkdir(parents=True, exist_ok=True)
    _write(PACKAGE / "types.py", _render_types_module(sections))
    _write(PACKAGE / "helpers.py", _render_helpers_module(sections))
    _write(PACKAGE / "backends.py", _render_backends_module(sections))
    _write(PACKAGE / "__init__.py", _render_root_module(docstring, sections))
    SOURCE.unlink()

    print(f"Split {SOURCE} -> {PACKAGE}")


if __name__ == "__main__":
    main()
