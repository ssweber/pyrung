"""Record which conformance cases have passed on a real CLICK, as they are now.

A run against a real CLICK records each case that passed, keyed ``group/id``.  The record
holds the hash of the case's exported rows (``GroupBuild.case_hashes``) and the date.  The
run drops each case that failed.  A case counts as confirmed while its recorded hash
matches the hash it runs with.  If you rewrite a case (its sentence, expectation, ladder
or slot), it stays unconfirmed until it passes on a CLICK again.

An in-process-only case is recorded with an empty hash.  No CLICK runs it, so its entry
says that someone checked it another way.

The record is a TOML file that the suite keeps under version control::

    [cases]
    "rungs/branch_sees_rung_entry" = { hash = "af0402ff4ba8", on = "2026-10-30" }
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pyrung.conformance._case import CaseResult

_HEADER = """\
# Cases that passed on a real CLICK, written by `make test-conformance PLC=...`.
# hash: the case's exported rows when it passed; a case whose rows have changed
# since counts as unconfirmed until it passes again.  hash = "" marks an
# in-process-only case confirmed some other way.
"""


@dataclass(frozen=True)
class Confirmation:
    hash: str
    on: str


def load_confirmations(path: str | Path) -> dict[str, Confirmation]:
    """Return the record at *path* (empty if there is none yet), keyed ``group/id``."""
    path = Path(path)
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        key: Confirmation(hash=str(entry.get("hash", "")), on=str(entry.get("on", "")))
        for key, entry in data.get("cases", {}).items()
    }


def is_confirmed(result: CaseResult, confirmations: Mapping[str, Confirmation]) -> bool:
    """Return whether *result*'s case has passed on a CLICK as it is now.

    A result from an in-process run has no hash.  For such a result, the presence of its
    entry is enough.
    """
    entry = confirmations.get(f"{result.group}/{result.case.id}")
    if entry is None:
        return False
    return not entry.hash or not result.case_hash or entry.hash == result.case_hash


def record_confirmations(
    path: str | Path, results: Iterable[CaseResult], *, on: str
) -> tuple[list[str], list[str]]:
    """Record the wire *results* of a run against a real CLICK at *path*.

    Write each case that passed with its hash and the date *on*.  Drop each case that
    failed.  Leave stale and skipped cases as they are.

    Returns:
        ``(added, dropped)``: the keys that became confirmed and the keys that
        stopped being confirmed.
    """
    record = load_confirmations(path)
    added: list[str] = []
    dropped: list[str] = []
    for r in results:
        if r.stale or r.skipped is not None or not r.case_hash:
            continue
        key = f"{r.group}/{r.case.id}"
        if r.passed and not r.xfail:
            if record.get(key, Confirmation("", "")).hash != r.case_hash:
                added.append(key)
            record[key] = Confirmation(hash=r.case_hash, on=on)
        elif key in record:
            dropped.append(key)
            del record[key]
    write_confirmations(path, record)
    return added, dropped


def write_confirmations(path: str | Path, record: Mapping[str, Confirmation]) -> None:
    """Write *record* to *path*, sorted by key."""
    lines = [_HEADER, "[cases]"]
    for key in sorted(record):
        entry = record[key]
        lines.append(f'"{key}" = {{ hash = "{entry.hash}", on = "{entry.on}" }}')
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
