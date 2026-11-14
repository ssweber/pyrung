"""Compare the CLICK project that is open now with its conformance export.

Usage (from the pyrung repo root):

    python -I devtools/ckp_check.py            # newest CLICK temp dir, part found from DS1
    python -I devtools/ckp_check.py boot       # name the part
    python -I devtools/ckp_check.py --diff     # also print each difference
    python -I devtools/ckp_check.py --dir "C:/Users/Sam/AppData/Local/Temp/CLICK (00F50E42)"

ClickNick writes the open project as CSV to <CLICK temp dir>/pyrung_project/csv. The script compares that with
build/conformance-release/exports/<part>. It ignores only rung numbers (R1 vs R), CR line ends, and the system
nicknames (SC/SD rows) that CLICK adds to its own list. Everything else must match the export text exactly.
A `raw(...)` box in the project is reported as RAW. It means laddercodec could not decode an instruction, which is a
laddercodec bug to report. Run ClickNick on laddercodec dev from 2026-11-10 or later. Earlier builds decoded a timed,
debounce or flasher coil in Tms as raw and wrote `for(N,oneshot=0)`.
ClickNick also writes legacy_encodings.json beside the CSVs when laddercodec warned about a legacy encoding. Each
finding prints as a STALE line under its file. The project's text can match the export and still hold an instruction
CLICK runs differently, so a STALE file counts as a problem until it is pasted again.
Exit 0 when every file matches with no RAW box and no STALE line, 1 otherwise.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXPORTS = REPO / "build" / "conformance-release" / "exports"
TEMP = Path.home() / "AppData" / "Local" / "Temp"

_ROW_NUM = re.compile(r"^R\d+,")


def _norm_ladder(text: str) -> list[str]:
    out = [_ROW_NUM.sub("R,", line) for line in text.replace("\r", "").split("\n")]
    while out and not out[-1]:
        out.pop()
    return out


def _norm_nicknames(ckp: str, export: str) -> tuple[list[str], list[str]]:
    exp = [ln for ln in export.replace("\r", "").split("\n") if ln]
    exp_set = set(exp)
    got = [
        ln
        for ln in ckp.replace("\r", "").split("\n")
        if ln and (ln in exp_set or not re.match(r"^S[CD]\d+,", ln))
    ]
    return got, exp


def _read_legacy(src: Path) -> dict[str, list[str]]:
    """Map each file name to the lines describing its legacy encodings, from ClickNick's sidecar."""
    path = src / "legacy_encodings.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("not a list")
    except (OSError, ValueError) as exc:
        print(f"  note: could not read {path.name} ({exc}), so legacy encodings are not checked")
        return {}
    found: dict[str, list[str]] = {}
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            continue
        rung = item.get("rung")
        where = f"rung {rung}" if isinstance(rung, int) else "whole file"
        what = f"{item.get('instruction', '')} {item.get('token', '')}".strip()
        detail = item.get("detail", "")
        found.setdefault(item["file"], []).append(f"{where}: {what}, {detail} Paste it again.")
    return found


def _newest_click_dir() -> Path:
    dirs = [d for d in TEMP.glob("CLICK (*)") if (d / "pyrung_project" / "csv").is_dir()]
    if not dirs:
        sys.exit("No open CLICK project with a ClickNick export found in " + str(TEMP))
    return max(dirs, key=lambda d: (d / "pyrung_project" / "csv" / "main.csv").stat().st_mtime)


def _layouts() -> dict[str, str]:
    found = {}
    for txt in EXPORTS.glob("*.export.txt"):
        m = re.search(r"layout (\d+) in DS1", txt.read_text(encoding="utf-8"))
        if m:
            found[m.group(1)] = txt.name.removesuffix(".export.txt")
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("part", nargs="?")
    ap.add_argument("--dir", type=Path)
    ap.add_argument("--diff", action="store_true")
    args = ap.parse_args()

    src = (args.dir or _newest_click_dir()) / "pyrung_project" / "csv"
    main_csv = (src / "main.csv").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"copy\((\d+),DS1\)", main_csv)
    ckp_layout = m.group(1) if m else None
    part = args.part or _layouts().get(ckp_layout or "")
    if not part:
        sys.exit(
            f"Open project writes layout {ckp_layout} to DS1, which matches no export. Name the part."
        )
    exp_dir = EXPORTS / part
    exp_match = re.search(
        r"layout (\d+)", (EXPORTS / f"{part}.export.txt").read_text(encoding="utf-8")
    )
    exp_layout = exp_match.group(1) if exp_match else None
    print(f"{src.parent.parent.name}  part {part}")
    print(
        f"layout: project {ckp_layout}, export {exp_layout}"
        + ("" if ckp_layout == exp_layout else "  <- main differs")
    )

    names = {"main.csv", "nicknames.csv"}
    names |= {f"subroutines/{p.name}" for p in (exp_dir / "subroutines").glob("*.csv")}
    names |= {f"subroutines/{p.name}" for p in (src / "subroutines").glob("*.csv")}
    bad = 0
    legacy = _read_legacy(src)
    for name in sorted(names, key=lambda n: (n.startswith("sub"), n)):
        a, b = src / name, exp_dir / name
        if not a.exists():
            print(f"  MISSING   {name}  (paste it)")
            bad += 1
            continue
        if not b.exists():
            print(f"  EXTRA     {name}  (not in the export: delete it from the project)")
            bad += 1
            continue
        ta = a.read_text(encoding="utf-8", errors="replace")
        tb = b.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(ta.replace("\r", "").split("\n"), 1):
            if '"raw(' in line or ",raw(" in line:
                print(
                    f"  RAW       {name}:{i}  {line.split(',')[0]}  {line[line.find('raw(') :][:90]}"
                )
                bad += 1
        if name == "nicknames.csv":
            got, exp = _norm_nicknames(ta, tb)
        else:
            got, exp = _norm_ladder(ta), _norm_ladder(tb)
        stale = legacy.get(name, [])
        counted = False
        if got == exp:
            print(f"  same      {name}")
        else:
            diff = [
                d for d in difflib.unified_diff(got, exp, "project", "export", n=0, lineterm="")
            ]
            nlines = sum(1 for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---"))
            tag = "nicknames" if name == "nicknames.csv" else "re-paste"
            print(f"  DIFF      {name}  ({nlines} lines, {tag})")
            if name != "nicknames.csv":
                bad += 1
                counted = True
            if args.diff:
                for d in diff:
                    print("      " + d[:240])
        for line in stale:
            print(f"  STALE     {name}  {line}")
        if stale and not counted:
            bad += 1
    print(
        "all current"
        if not bad
        else f"{bad} problem(s): paste each DIFF, MISSING or STALE file, fix each RAW box"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
