"""What a piece of generated code reads, writes and compares, recorded where the codegen writes it.

The codegen and the analysis both import this module, and it imports neither.  The emit
helpers build each record from the same values they write into the text, so a record and
its code agree on the operator, the operands and the stored value.

A normal render records nothing (``CodegenContext.recorder`` is None).  A recording render
wraps each recorded piece of text as ``_r(<id>, <text>)``.  Nothing runs that source.  The
reader parses it to find each record's place and the ``if`` tests around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias


@dataclass(frozen=True)
class TagValue:
    """A tag's value where the code reads it."""

    name: str


@dataclass(frozen=True)
class Updated:
    """The value an instruction computes for a tag on this scan, before it stores it."""

    name: str


@dataclass(frozen=True)
class Memory:
    """An instruction memory cell."""

    key: str


@dataclass(frozen=True)
class Const:
    value: Any


@dataclass(frozen=True)
class Setting:
    """The number an instruction holds in its field *field*, such as a preset of 100.

    The record names the field, not the number, so one record serves every preset.
    """

    field: str


Term: TypeAlias = TagValue | Updated | Memory | Const | Setting


@dataclass(frozen=True)
class Read:
    """The code reads a tag or a memory cell, which gives *default* while it is unwritten."""

    term: TagValue | Memory
    default: Any = None


@dataclass(frozen=True)
class Compare:
    left: Term
    op: str
    right: Term


@dataclass(frozen=True)
class Rise:
    """True on the scan an input turns on.  The cell *key* holds the input as last stored."""

    key: str


@dataclass(frozen=True)
class Store:
    """The code stores *value* into the tag *dst* or into a memory cell.

    A *value* of None is one this record does not describe, such as a status bit's formula.
    """

    dst: str | Memory
    value: Term | Compare | None


@dataclass(frozen=True)
class Step:
    """The code moves the tag *name* by *delta*, held within *low* and *high*.

    A *delta* of None is a timer's elapsed time.  A bound of None is no bound.
    """

    name: str
    delta: int | None
    low: int | None
    high: int | None


Record: TypeAlias = Read | Compare | Rise | Store | Step


class Recorder:
    """Collects the records of one recording render, in the order the codegen writes them."""

    def __init__(self) -> None:
        self.records: list[Record] = []

    def mark(self, record: Record, text: str) -> str:
        """*text* wrapped as ``_r(<id>, <text>)``, with *record* kept under that id."""
        self.records.append(record)
        return f"_r({len(self.records) - 1}, {text})"
