"""Machine-readable grammar for the DAP console commands.

The console's ``usage=`` strings are written for humans. Tools that complete or
validate console input (clicknick's REPL, editor integrations) need the same
information as *data* — which slot takes a tag, which takes a free expression,
which may repeat.

This module is that contract. :func:`command_grammar` returns one
:class:`CommandGrammar` per registered verb, with slots derived from the usage
string, or declared outright via ``register(slots=...)`` when the prose is
ambiguous (see ``how``).

Consumers should read this instead of parsing ``usage`` themselves: the
derivation is heuristic, and keeping it here means a reworded usage string
breaks a test in this repo rather than silently degrading a downstream tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

SlotKind = Literal["tag", "expression", "value", "choices", "flag", "text"]
"""What a slot accepts.

``tag`` — a single tag name.  ``expression`` — tags mixed with operators
(``Motor & ~Pump``, ``State == 3``); a completer should offer tag names here
too.  ``value`` — a literal (``true``, ``42``).  ``choices`` — one of
:attr:`Slot.choices`.  ``flag`` — a ``--flag``.  ``text`` — freeform (a note, a
filepath); nothing to complete.
"""


@dataclass(frozen=True)
class Slot:
    """One argument slot in a console command."""

    kind: SlotKind
    label: str = ""
    required: bool = True
    #: The slot may be given more than once (``get <tag> [tag2 ...]``).
    repeat: bool = False
    #: What separates repeats: a space, or "," for comma-separated conjuncts
    #: (``how A, B`` — every target must hold; ``avoid A, B`` — avoid either).
    #: A completer must treat this as a token boundary *within* one slot.
    separator: str = " "
    #: A literal word that introduces this slot (``how X avoid Y`` — the ``avoid``
    #: slot's keyword). The keyword itself is a completion candidate, and it marks
    #: where the preceding slot ends; positional slots leave this empty.
    keyword: str = ""
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandGrammar:
    """The parsed grammar of a single console command."""

    verb: str
    slots: tuple[Slot, ...] = ()
    group: str = ""
    hint: str = ""
    usage: str = ""


_PAREN_RE = re.compile(r"\s*\(.*?\)\s*$")
_EXPR_RE = re.compile(r"\bexpr(ession)?\b", re.IGNORECASE)
_WORD_RE = re.compile(r"^[\w-]+$")
_TAG_WORDS = {"tag", "tag2"}
_VALUE_WORDS = {"value"}


def _tokenize_usage(text: str) -> list[str]:
    """Split a usage remainder into bracket-aware tokens.

    Keeps ``<...>`` and ``[...]`` groups together, including nested brackets
    (``[avoid <expression>[, <expression>...]]`` is one token).
    """
    tokens: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in " \t":
            i += 1
            continue
        if ch in "<[":
            close = ">" if ch == "<" else "]"
            depth = 0
            end = i
            while end < len(text):
                if text[end] == ch:
                    depth += 1
                elif text[end] == close:
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            if end >= len(text):  # unbalanced — take the rest
                end = len(text) - 1
            tokens.append(text[i : end + 1])
            i = end + 1
        else:
            end = i
            while end < len(text) and text[end] not in " \t<[":
                end += 1
            tokens.append(text[i:end])
            i = end
    return tokens


def _classify(token: str) -> Slot | None:
    """Classify one usage token into a :class:`Slot`."""
    if token == "...":
        return None

    optional = token.startswith("[")
    inner = token.strip("<>[]").strip()
    if not inner:
        return None

    repeat = "..." in token
    # A comma before the repeated element means the repeats are comma-separated:
    # "[, <expression>...]" and "[avoid <expression>[, <expression>...]]".
    separator = "," if repeat and re.search(r",\s*[<\[]", token) else " "

    if inner.startswith("--"):
        return Slot(kind="flag", label=inner, required=False, choices=(inner.split()[0],))

    base = inner.replace("...", "").strip().lstrip(",").strip()

    # Expression before tag: "avoid <expression>" is an expression slot, not a bare tag.
    if _EXPR_RE.search(base):
        return Slot(
            kind="expression",
            label=inner,
            required=not optional,
            repeat=repeat,
            separator=separator,
        )

    # "<tag>", "[tag2 ...]", "<tag>[@scan|:value]"
    tag_part = base.split("[")[0].split("@")[0].strip()
    if tag_part.lower() in _TAG_WORDS:
        return Slot(
            kind="tag",
            label=inner,
            required=not optional,
            repeat=repeat,
            separator=separator,
        )

    if base.lower() in _VALUE_WORDS:
        return Slot(kind="value", label=inner, required=not optional)

    # Literal alternatives: "always|never", "<install|remove|status>". A spaced pipe
    # ("N | duration") is descriptive prose, not a choice list.
    if "|" in inner and " | " not in inner:
        parts = [p.strip() for p in inner.split("|")]
        if all(_WORD_RE.match(p) for p in parts):
            return Slot(
                kind="choices",
                label=inner,
                required=not optional,
                choices=tuple(parts),
            )

    # A bare optional word is a literal switch: "[clear]", "[off]", "[list]".
    # "[N]" is a count, not a literal.
    if optional and _WORD_RE.match(inner) and not inner.isdigit() and inner != "N":
        return Slot(kind="choices", label=inner, required=False, choices=(inner,))

    return Slot(kind="text", label=inner, required=not optional)


def parse_usage(verb: str, usage: str) -> tuple[Slot, ...]:
    """Derive a command's slots from its ``usage`` string."""
    remainder = usage.strip()
    if remainder.lower().startswith(verb):
        remainder = remainder[len(verb) :].strip()
    remainder = _PAREN_RE.sub("", remainder).strip()
    if not remainder:
        return ()

    # Alternative forms ("record <action> | record stop") — grammar the first.
    if f" | {verb} " in remainder:
        remainder = remainder.split("|")[0].strip()
    elif remainder.startswith("| "):
        return ()

    slots = [s for s in (_classify(t) for t in _tokenize_usage(remainder)) if s is not None]
    return tuple(slots)


def command_grammar() -> dict[str, CommandGrammar]:
    """Return the grammar of every registered console command, keyed by verb.

    Importing this module is not enough to see every verb — the console commands
    live in several modules that register on import, so this imports them all
    before reading the registry.
    """
    from pyrung.dap import (  # noqa: F401
        bounds_console,
        capture,
        harness_console,
        miner_console,
        reload_console,
        spec_console,
    )
    from pyrung.dap.console import _REGISTRY

    out: dict[str, CommandGrammar] = {}
    for verb, entry in _REGISTRY.items():
        slots = entry.slots if entry.slots is not None else parse_usage(verb, entry.usage)
        out[verb] = CommandGrammar(
            verb=verb,
            slots=slots,
            group=entry.group,
            hint=entry.hint,
            usage=entry.usage,
        )
    return out
