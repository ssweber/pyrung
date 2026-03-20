from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pyrung.click.codegen.analyzer import _analyze_rungs
from pyrung.click.codegen.collector import _collect_operands
from pyrung.click.codegen.emitter import _generate_code
from pyrung.click.codegen.parser import _find_call_names, _parse_csv, _parse_subroutines

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap


def csv_to_pyrung(
    csv_path: str | Path,
    *,
    nickname_csv: str | Path | None = None,
    nicknames: dict[str, str] | None = None,
    output_path: str | Path | None = None,
) -> str:
    """Convert a Click ladder CSV (v2) to pyrung Python source code.

    Args:
        csv_path: Path to the CSV file (main.csv) or a directory containing
            main.csv and optional sub_*.csv subroutine files.
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
            or if the CSV format is invalid.
    """
    if nickname_csv is not None and nicknames is not None:
        raise ValueError("Provide nickname_csv or nicknames, not both.")

    csv_path = Path(csv_path)

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

    # Determine if csv_path is a directory or a file
    if csv_path.is_dir():
        main_path = csv_path / "main.csv"
        if not main_path.exists():
            raise ValueError(f"main.csv not found in {csv_path}")
        dir_path = csv_path
    else:
        main_path = csv_path
        dir_path = csv_path.parent

    # Phase 1: Parse main CSV
    raw_rungs = _parse_csv(main_path)

    # Phase 1b: Parse subroutine CSVs (if any sub_*.csv files exist)
    call_names = _find_call_names(raw_rungs)
    subroutines = _parse_subroutines(dir_path, call_names) if call_names else []

    # Phase 2: Analyze topology
    analyzed = _analyze_rungs(raw_rungs)

    # Phase 3: Collect operands (from main + subroutines)
    all_analyzed = list(analyzed)
    for sub in subroutines:
        all_analyzed.extend(sub.analyzed)
    collection = _collect_operands(all_analyzed, nick_map, structured_map=structured_map)

    # Mark subroutine usage if we have subroutines
    if subroutines:
        collection.has_subroutine = True

    # Phase 4: Generate code
    code = _generate_code(
        analyzed, collection, nick_map, subroutines=subroutines, structured_map=structured_map
    )

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(code, encoding="utf-8")

    return code
