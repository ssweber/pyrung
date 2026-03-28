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

if TYPE_CHECKING:
    from pyrung.click.codegen.models import _AnalyzedRung, _OperandCollection, _SubroutineInfo
    from pyrung.click.ladder.types import LadderBundle
    from pyrung.click.tag_map import TagMap


def _prepare_codegen(
    source: str | Path | LadderBundle,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
) -> tuple[
    list[_AnalyzedRung],
    _OperandCollection,
    dict[str, str] | None,
    list[_SubroutineInfo],
    TagMap | None,
]:
    """Shared pipeline: parse, analyze, collect operands.

    Returns (main_rungs, collection, nick_map, subroutines, structured_map).
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
        subroutines = _parse_subroutines_from_bundle(source, call_names)
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
        subroutines = _parse_subroutines(dir_path, call_names) if call_names else []
    else:
        raise TypeError(
            f"source must be a path (str/Path) or LadderBundle, got {type(source).__name__}"
        )

    analyzed = _analyze_rungs(raw_rungs)

    all_analyzed = list(analyzed)
    for sub in subroutines:
        all_analyzed.extend(sub.analyzed)
    collection = _collect_operands(all_analyzed, nick_map, structured_map=structured_map)

    if subroutines:
        collection.has_subroutine = True

    return analyzed, collection, nick_map, subroutines, structured_map


def to_pyrung(
    source: str | Path | LadderBundle,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
    output_path: str | Path | None = None,
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

    Returns:
        The generated Python source code as a string.

    Raises:
        ValueError: If both ``nickname_csv`` and ``nicknames`` are provided,
            if required subroutine CSV files are missing, or if the CSV
            format is invalid.
        TypeError: If ``source`` is not a supported type.
    """
    analyzed, collection, nick_map, subroutines, structured_map = _prepare_codegen(
        source, nickname_csv=nickname_csv, nicknames=nicknames
    )

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
) -> list:
    """Parse subroutine rows from a LadderBundle (in-memory, no disk I/O)."""
    from pyrung.click.codegen.models import _SubroutineInfo
    from pyrung.click.codegen.utils import _slugify

    subs = []
    for subroutine_name, rows in bundle.subroutine_rows:
        slug = _slugify(subroutine_name)
        name = call_names.get(slug, subroutine_name)
        raw = _parse_rows(rows)
        analyzed = _analyze_rungs(raw)
        subs.append(_SubroutineInfo(name=name, analyzed=analyzed))
    return subs


def to_pyrung_project(
    source: str | Path | LadderBundle,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
    output_dir: str | Path | None = None,
) -> dict[str, str]:
    """Convert Click ladder data to a multi-file pyrung project.

    Generates a project layout with separate ``tags.py``, ``main.py``, and
    ``subroutines/*.py`` files suitable for simulation, testing, or editing.

    Args:
        source: A file path (to main.csv or a directory containing main.csv
            and optional ``subroutines/*.csv`` files), or a
            :class:`LadderBundle` for in-memory round-trip without disk I/O.
        nickname_csv: Optional path to a Click nickname CSV file (Address.csv).
        nicknames: Optional pre-parsed ``{operand: nickname}`` dict.
        output_dir: Optional directory to write the project files into.
            If ``None``, files are returned as strings only.

    Returns:
        A dict mapping relative file paths to their content, e.g.
        ``{"main.py": "...", "tags.py": "...", "subroutines/startup.py": "..."}``.
    """
    from pyrung.click.codegen.project_emitter import _generate_project

    analyzed, collection, nick_map, subroutines, structured_map = _prepare_codegen(
        source, nickname_csv=nickname_csv, nicknames=nicknames
    )

    files = _generate_project(
        analyzed,
        collection,
        nick_map,
        subroutines,
        structured_map=structured_map,
    )

    if output_dir is not None:
        out_dir = Path(output_dir)
        for rel_path, content in files.items():
            fpath = out_dir / rel_path
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

    return files


# Backwards-compatible alias
csv_to_pyrung = to_pyrung
