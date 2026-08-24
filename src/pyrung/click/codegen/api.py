from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pyrung.click.codegen.analyzer import _analyze_rungs
from pyrung.click.codegen.collector import _collect_operands
from pyrung.click.codegen.emitter import _generate_code
from pyrung.click.codegen.parser import (
    _find_call_names,
    _parse_csv,
    _parse_rows,
    _parse_subroutines,
)
from pyrung.core.structure import resolve_default

if TYPE_CHECKING:
    from pyrung.click.codegen.models import _AnalyzedRung, _OperandCollection, _SubroutineInfo
    from pyrung.click.ladder.types import LadderBundle
    from pyrung.click.tag_map import TagMap


class CodegenIdentityError(Exception):
    """Generated code would not reconstruct the source project's per-slot values.

    Raised by the ``validate=True`` self-check when the compressed structure
    representation the emitter is about to write (field default + ``auto()``
    sequence + per-slot overrides) does not resolve back to every value read
    from the source runtime.
    """


_MISSING = object()


def _verify_codegen_identity(
    collection: _OperandCollection,
    structured_map: TagMap | None,
) -> None:
    """Fail loudly when emitted structure defaults would drop source values.

    Decl-level check (no exec, no re-parse): for each structure field, resolve
    what the emitted representation reconstructs and compare it to the source
    per-slot default.  Retentive fields are skipped — pyrung's model discards
    their power-on defaults, so they are intentionally not reconstructed.
    """
    if structured_map is None:
        return

    mismatches: list[str] = []
    for decl in collection.structures:
        si = structured_map.structure_by_name(decl.name)
        if si is None:
            continue
        runtime = si.runtime
        for field_name, _type_name, base_default in decl.fields:
            if decl.field_retentive.get(field_name, False):
                continue
            block = runtime._blocks.get(field_name)
            if block is None:
                continue
            for index in range(1, decl.count + 1):
                override = decl.field_slot_default.get((field_name, index), _MISSING)
                reconstructed = (
                    override if override is not _MISSING else resolve_default(base_default, index)
                )
                source = block.slot(index).default
                if reconstructed != source:
                    mismatches.append(
                        f"{decl.name}.{field_name}[{index}]: source default {source!r} "
                        f"but generated code reconstructs {reconstructed!r}"
                    )

    if mismatches:
        raise CodegenIdentityError(
            "codegen identity check failed (validate=True):\n  " + "\n  ".join(mismatches)
        )


def _prepare_codegen(
    source: str | Path | LadderBundle,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
    validate: bool = False,
) -> tuple[
    list[_AnalyzedRung],
    _OperandCollection,
    dict[str, str] | None,
    list[_SubroutineInfo],
    TagMap | None,
]:
    """Shared pipeline: parse, analyze, collect operands.

    Returns (main_rungs, collection, nick_map, subroutines, structured_map).

    When *validate* is True, rung analysis raises ``ValueError`` for any source
    contact that reaches no output (a dropped condition — see
    ``analyzer._analyze_single_rung``); otherwise such drops only warn.
    """
    if nickname_csv is not None and nicknames is not None:
        raise ValueError("Provide nickname_csv or nicknames, not both.")

    nick_map: dict[str, str] | None = None
    structured_map: TagMap | None = None
    if nickname_csv is not None:
        from pyrung.click.tag_map import TagMap as _TagMap

        structured_map = _TagMap.from_nickname_file(Path(nickname_csv))
        nick_map = {
            slot.hardware_address: slot.logical_name
            for slot in structured_map.mapped_slots()
            if slot.source == "user"
        }
    elif nicknames is not None:
        nick_map = nicknames

    from pyrung.click.ladder.types import LadderBundle as _LadderBundle

    if isinstance(source, _LadderBundle):
        raw_rungs = _parse_rows(source.main_rows)
        call_names = _find_call_names(raw_rungs)
        subroutines = _parse_subroutines_from_bundle(source, call_names, validate=validate)
    elif isinstance(source, (str, Path)):
        csv_path = Path(source)
        if csv_path.is_dir():
            main_path = csv_path / "main.csv"
            if not main_path.exists():
                raise ValueError(f"main.csv not found in {csv_path}")
            dir_path = csv_path
        else:
            main_path = csv_path
            dir_path = csv_path.parent

        raw_rungs = _parse_csv(main_path)
        call_names = _find_call_names(raw_rungs)
        subroutines = (
            _parse_subroutines(dir_path, call_names, validate=validate) if call_names else []
        )
    else:
        raise TypeError(
            f"source must be a path (str/Path) or LadderBundle, got {type(source).__name__}"
        )

    analyzed = _analyze_rungs(raw_rungs, validate=validate, source_name="Main program")

    all_analyzed = list(analyzed)
    for sub in subroutines:
        all_analyzed.extend(sub.analyzed)
    collection = _collect_operands(all_analyzed, nick_map, structured_map=structured_map)

    if subroutines:
        collection.has_subroutine = True

    return analyzed, collection, nick_map, subroutines, structured_map


def ladder_to_pyrung(
    source: str | Path | LadderBundle,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
    output_path: str | Path | None = None,
    validate: bool = True,
) -> str:
    """Convert Click ladder data to pyrung Python source code.

    Args:
        source: A file path (to main.csv or a directory containing main.csv
            and optional ``subroutines/*.csv`` files), or a
            :class:`LadderBundle` for in-memory round-trip without disk I/O.
        nickname_csv: Optional path to a Click nickname CSV file (Address.csv).
            Read via ``pyclickplc.read_csv()``, extracts ``{display_address: nickname}``
            pairs for variable name substitution.
        nicknames: Optional pre-parsed ``{operand: nickname}`` dict. Alternative
            to ``nickname_csv``; useful when the caller already has the map.
        output_path: Optional path to write the generated Python file.
            If ``None``, the code is returned as a string only.
        validate: When *True* (default), run codegen self-checks: a decl-level
            identity check that the generated structure defaults reconstruct
            every source per-slot value (raises :class:`CodegenIdentityError`),
            and a rung-analysis check that no source contact is silently dropped
            for lack of wiring into an output (raises ``ValueError``). When
            *False*, a dropped contact only warns instead of raising.

    Returns:
        The generated Python source code as a string.

    Raises:
        ValueError: If both ``nickname_csv`` and ``nicknames`` are provided,
            if required subroutine CSV files are missing, if the CSV format is
            invalid, or if ``validate`` is *True* and a rung drops a source
            contact that reaches no output.
        TypeError: If ``source`` is not a supported type.
        CodegenIdentityError: If ``validate`` is *True* and the generated code
            would not reconstruct the source project's per-slot values.
    """
    analyzed, collection, nick_map, subroutines, structured_map = _prepare_codegen(
        source, nickname_csv=nickname_csv, nicknames=nicknames, validate=validate
    )

    if validate:
        _verify_codegen_identity(collection, structured_map)

    code = _generate_code(
        analyzed, collection, nick_map, subroutines=subroutines, structured_map=structured_map
    )

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")

    return code


def _parse_subroutines_from_bundle(
    bundle: LadderBundle,
    call_names: dict[str, str],
    *,
    validate: bool = False,
) -> list:
    """Parse subroutine rows from a LadderBundle (in-memory, no disk I/O)."""
    from pyrung.click.codegen.models import _SubroutineInfo
    from pyrung.click.codegen.utils import _slugify

    subs = []
    for subroutine_name, rows in bundle.subroutine_rows:
        slug = _slugify(subroutine_name)
        name = call_names.get(slug, subroutine_name)
        raw = _parse_rows(rows)
        analyzed = _analyze_rungs(
            raw,
            validate=validate,
            source_name=f'Subroutine "{name}"',
        )
        subs.append(_SubroutineInfo(name=name, analyzed=analyzed))
    return subs


def ladder_to_pyrung_project(
    source: str | Path | LadderBundle,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
    output_dir: str | Path | None = None,
    index: bool = False,
    overwrite: bool = False,
    machine_name: str = "PLC",
    validate: bool = True,
) -> dict[str, str]:
    """Convert Click ladder data to a multi-file pyrung project.

    Generates an installable ``src/plc`` project with separate ``tags.py``,
    ``main.py``, and ``subroutines/*.py`` files suitable for simulation,
    testing, or editing.

    Args:
        source: A file path (to main.csv or a directory containing main.csv
            and optional ``subroutines/*.csv`` files), or a
            :class:`LadderBundle` for in-memory round-trip without disk I/O.
        nickname_csv: Optional path to a Click nickname CSV file (Address.csv).
        nicknames: Optional pre-parsed ``{operand: nickname}`` dict.
        output_dir: Optional directory to write the project files into.
            If ``None``, files are returned as strings only.
        overwrite: When *False* (default), scaffolding files (pyproject.toml,
            README.md, .vscode/) are skipped if they already exist on disk.
            Logic files under src/plc/ are always written.
        machine_name: Human-readable machine name for AGENTS.md
            header (e.g. from the .ckp filename).
        validate: When *True* (default), run codegen self-checks (see
            :func:`ladder_to_pyrung`): a structure-default identity check
            (raises :class:`CodegenIdentityError`) and a dropped-contact check
            (raises ``ValueError``). When *False*, dropped contacts only warn.

    Returns:
        A dict mapping relative file paths to their content, e.g.
        ``{"src/plc/main.py": "...", "src/plc/tags.py": "..."}``.
    """
    from pyrung.click.codegen.project_emitter import _SCAFFOLDING_FILES, _generate_project

    analyzed, collection, nick_map, subroutines, structured_map = _prepare_codegen(
        source, nickname_csv=nickname_csv, nicknames=nicknames, validate=validate
    )

    if validate:
        _verify_codegen_identity(collection, structured_map)

    files = _generate_project(
        analyzed,
        collection,
        nick_map,
        subroutines,
        structured_map=structured_map,
        index=index,
        machine_name=machine_name,
    )

    # Include nickname CSV in output for round-trip support
    if nickname_csv is not None:
        nick_path = Path(nickname_csv)
        if nick_path.exists():
            files["nicknames.csv"] = nick_path.read_text(encoding="utf-8")

    if output_dir is not None:
        out_dir = Path(output_dir)
        for rel_path, content in files.items():
            fpath = out_dir / rel_path
            if not overwrite and rel_path in _SCAFFOLDING_FILES and fpath.exists():
                continue
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

    return files
