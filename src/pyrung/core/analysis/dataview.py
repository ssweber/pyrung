"""Chainable query API over a program's tag dependency graph."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from pyrung.core.analysis.pdg import ProgramGraph, TagRole


@dataclass(frozen=True)
class TagDetail:
    """Rich metadata for a single tag in a DataView."""

    name: str
    type: str
    role: str
    retentive: bool = False
    readonly: bool = False
    external: bool = False
    final: bool = False
    public: bool = False
    physical: str | None = None
    link: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    uom: str | None = None
    choices: int | None = None
    structure_kind: str | None = None
    structure_name: str | None = None
    structure_field: str | None = None


@dataclass(frozen=True)
class StructureInfo:
    """Summary of a UDT or named_array definition."""

    name: str
    kind: str
    count: int
    fields: tuple[StructureFieldInfo, ...] = ()
    stride: int | None = None
    base_type: str | None = None


@dataclass(frozen=True)
class StructureFieldInfo:
    """Per-field metadata within a structure."""

    name: str
    type: str
    readonly: bool = False
    external: bool = False
    final: bool = False
    public: bool = False
    physical: str | None = None
    link: str | None = None
    min: int | float | None = None
    max: int | float | None = None
    uom: str | None = None
    choices: int | None = None


class TagNameMatcher:
    """Abbreviation-aware tag name matching.

    Ported from clicknick's ``ContainsPlusFilter``.  Precomputes an
    abbreviation index so that ``filter()`` can match needles like
    ``"cmd"`` against tag names like ``CommandRun``.
    """

    _WORD_SPLIT = re.compile(r"[_\s]+|(?<=[a-z])(?=[A-Z])")
    _VOWELS = frozenset("aeiou")

    def __init__(self, tag_names: frozenset[str]) -> None:
        self._index: dict[str, tuple[str, ...]] = {
            name: self._generate_tokens(name) for name in tag_names
        }

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def filter(self, tag_names: frozenset[str], needle: str) -> frozenset[str]:
        """Return the subset of *tag_names* matching *needle*."""
        if not needle:
            return tag_names

        words = self._split_words(needle, min_len=1)
        if not words:
            needle_lower = needle.lower()
            return frozenset(name for name in tag_names if needle_lower in name.lower())

        if len(words) == 1:
            return self._filter_single(tag_names, words[0])
        return self._filter_multi(tag_names, words)

    # ------------------------------------------------------------------
    # Single / multi word filtering
    # ------------------------------------------------------------------

    def _filter_single(self, tag_names: frozenset[str], word: str) -> frozenset[str]:
        needle_lower = word.lower()
        variants = self._needle_variants(word)
        matched: set[str] = set()
        for name in tag_names:
            if needle_lower in name.lower():
                matched.add(name)
                continue
            tokens = self._index.get(name, ())
            if any(tok.startswith(v) for tok in tokens for v in variants):
                matched.add(name)
        return frozenset(matched)

    def _filter_multi(self, tag_names: frozenset[str], words: list[str]) -> frozenset[str]:
        remaining = set(tag_names)
        words = list(words)
        words.sort(key=len, reverse=True)
        for word in words:
            if not remaining:
                break
            needle_lower = word.lower()
            variants = self._needle_variants(word)
            word_matches: set[str] = set()
            for name in remaining:
                if needle_lower in name.lower():
                    word_matches.add(name)
                    continue
                tokens = self._index.get(name, ())
                if any(tok.startswith(v) for tok in tokens for v in variants):
                    word_matches.add(name)
            remaining &= word_matches
        return frozenset(remaining)

    # ------------------------------------------------------------------
    # Word splitting
    # ------------------------------------------------------------------

    @classmethod
    def _split_words(cls, text: str, *, min_len: int = 2) -> list[str]:
        return [w for w in cls._WORD_SPLIT.split(text) if len(w) >= min_len]

    # ------------------------------------------------------------------
    # Abbreviation generation
    # ------------------------------------------------------------------

    @classmethod
    def _special_case(cls, word: str) -> str | None:
        if len(set(word)) <= 1:
            return word
        lower = word.lower()
        if len(lower) <= 3:
            return lower
        if not cls._VOWELS & set(lower[1:]):
            return lower
        return None

    @classmethod
    def _consonants_abbr(cls, word: str) -> str:
        lower = word.lower()
        result = [lower[0]]
        for ch in lower[1:]:
            if ch not in cls._VOWELS:
                result.append(ch)
        final = [result[0]]
        for ch in result[1:]:
            if ch != final[-1]:
                final.append(ch)
        return "".join(final)

    @classmethod
    def _reduced_consonants_abbr(cls, word: str) -> str:
        lower = word.lower()
        result = [lower[0]]
        for i in range(len(lower)):
            ch = lower[i]
            if ch in cls._VOWELS:
                continue
            if (
                lower[i - 1] in cls._VOWELS
                and i + 1 < len(lower)
                and lower[i + 1] not in cls._VOWELS
            ):
                continue
            result.append(ch)
        final = [result[0]]
        for ch in result[1:]:
            if ch != final[-1]:
                final.append(ch)
        return "".join(final)

    @classmethod
    def _abbreviations(cls, word: str) -> list[str]:
        abbr = cls._special_case(word)
        if abbr is not None:
            return [abbr]
        variants: list[str] = []
        c = cls._consonants_abbr(word)
        if len(c) >= 2:
            variants.append(c)
            r = cls._reduced_consonants_abbr(word)
            if len(r) >= 2:
                variants.append(r)
        return variants

    @classmethod
    def _generate_tokens(cls, text: str) -> tuple[str, ...]:
        words = cls._split_words(text)
        tokens: list[str] = []
        for w in words:
            if len(w) >= 4:
                tokens.append(w.lower())
            tokens.extend(cls._abbreviations(w))
        return tuple(sorted(set(tokens)))

    @classmethod
    def _needle_variants(cls, needle: str) -> tuple[str, ...]:
        lower = needle.lower()
        variants = [lower]
        variants.extend(cls._abbreviations(lower))
        return tuple(dict.fromkeys(variants))


@dataclass(frozen=True)
class DataView:
    """Lazy, chainable query over a program's tag dependency graph."""

    _graph: ProgramGraph
    _tags: frozenset[str]
    _matcher: TagNameMatcher

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_graph(cls, graph: ProgramGraph) -> DataView:
        """Create a root view covering all classified tags."""
        all_tags = frozenset(graph.tag_roles)
        return cls(_graph=graph, _tags=all_tags, _matcher=TagNameMatcher(all_tags))

    def _narrow(self, tags: frozenset[str]) -> DataView:
        return DataView(_graph=self._graph, _tags=tags, _matcher=self._matcher)

    # ------------------------------------------------------------------
    # Role filters
    # ------------------------------------------------------------------

    def inputs(self) -> DataView:
        """Tags with role INPUT."""
        return self._narrow(self._tags & self._by_role(TagRole.INPUT))

    def pivots(self) -> DataView:
        """Tags with role PIVOT."""
        return self._narrow(self._tags & self._by_role(TagRole.PIVOT))

    def terminals(self) -> DataView:
        """Tags with role TERMINAL."""
        return self._narrow(self._tags & self._by_role(TagRole.TERMINAL))

    def isolated(self) -> DataView:
        """Tags with role ISOLATED."""
        return self._narrow(self._tags & self._by_role(TagRole.ISOLATED))

    # ------------------------------------------------------------------
    # Physicality filters
    # ------------------------------------------------------------------

    def physical_inputs(self) -> DataView:
        """Tags backed by physical input hardware."""
        return self._narrow(frozenset(t for t in self._tags if self._graph.is_physical_input(t)))

    def physical_outputs(self) -> DataView:
        """Tags backed by physical output hardware."""
        return self._narrow(frozenset(t for t in self._tags if self._graph.is_physical_output(t)))

    def internal(self) -> DataView:
        """Tags that are neither physical inputs nor outputs."""
        return self._narrow(
            frozenset(
                t
                for t in self._tags
                if not self._graph.is_physical_input(t) and not self._graph.is_physical_output(t)
            )
        )

    # ------------------------------------------------------------------
    # Name matching
    # ------------------------------------------------------------------

    def contains(self, needle: str) -> DataView:
        """Filter to tags matching *needle* (contains + abbreviation)."""
        return self._narrow(self._matcher.filter(self._tags, needle))

    # ------------------------------------------------------------------
    # Graph slicing
    # ------------------------------------------------------------------

    def upstream(self, tag: str) -> DataView:
        """Upstream dependency cone of *tag*, intersected with current view."""
        return self._narrow(self._tags & self._graph.upstream_slice(tag))

    def downstream(self, tag: str) -> DataView:
        """Downstream dependency cone of *tag*, intersected with current view."""
        return self._narrow(self._tags & self._graph.downstream_slice(tag))

    # ------------------------------------------------------------------
    # Iteration / inspection
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._tags))

    def __len__(self) -> int:
        return len(self._tags)

    def __contains__(self, item: object) -> bool:
        return item in self._tags

    def __bool__(self) -> bool:
        return bool(self._tags)

    @property
    def tags(self) -> frozenset[str]:
        """The current narrowed tag set."""
        return self._tags

    def roles(self) -> dict[str, TagRole]:
        """Return ``{tag: role}`` for tags in the current view."""
        tr = self._graph.tag_roles
        return {t: tr[t] for t in self._tags if t in tr}

    def details(self) -> dict[str, TagDetail]:
        """Return ``{tag: TagDetail}`` with full metadata for tags in the view."""
        tr = self._graph.tag_roles
        tags_dict = self._graph.tags
        result: dict[str, TagDetail] = {}
        for name in self._tags:
            if name not in tr:
                continue
            tag = tags_dict.get(name)
            if tag is None:
                result[name] = TagDetail(name=name, type="bool", role=tr[name].value)
                continue

            physical_name = tag.physical.name if tag.physical is not None else None
            choices_count = len(tag.choices) if tag.choices else None

            struct_kind = getattr(tag, "_pyrung_structure_kind", None)
            struct_name = getattr(tag, "_pyrung_structure_name", None)
            struct_field = getattr(tag, "_pyrung_structure_field", None)

            result[name] = TagDetail(
                name=name,
                type=tag.type.value,
                role=tr[name].value,
                retentive=tag.retentive,
                readonly=tag.readonly,
                external=tag.external,
                final=tag.final,
                public=tag.public,
                physical=physical_name,
                link=tag.link,
                min=tag.min,
                max=tag.max,
                uom=tag.uom,
                choices=choices_count,
                structure_kind=struct_kind,
                structure_name=struct_name,
                structure_field=struct_field,
            )
        return result

    def structures(self) -> list[StructureInfo]:
        """Return definitions for all UDTs and named_arrays found in the view."""
        seen_ids: set[int] = set()
        runtimes: list[object] = []
        for name in self._tags:
            tag = self._graph.tags.get(name)
            if tag is None:
                continue
            rt = getattr(tag, "_pyrung_structure_runtime", None)
            if rt is None:
                continue
            rt_id = id(rt)
            if rt_id in seen_ids:
                continue
            seen_ids.add(rt_id)
            runtimes.append(rt)

        result: list[StructureInfo] = []
        for rt in runtimes:
            kind = getattr(rt, "_structure_kind", "udt")
            name = getattr(rt, "name", "?")
            count = getattr(rt, "count", 1)
            field_order: tuple[str, ...] = getattr(rt, "_field_order", ())
            blocks: dict[str, object] = getattr(rt, "_blocks", {})

            fields: list[StructureFieldInfo] = []
            for fname in field_order:
                block = blocks.get(fname)
                if block is None:
                    continue
                ftype = getattr(block, "type", None)
                ftype_str = ftype.value if ftype is not None else "?"
                phys = getattr(block, "_pyrung_field_physical", None)
                ch = getattr(block, "_pyrung_field_choices", None)
                fields.append(
                    StructureFieldInfo(
                        name=fname,
                        type=ftype_str,
                        readonly=getattr(block, "_pyrung_field_readonly", False),
                        external=getattr(block, "_pyrung_field_external", False),
                        final=getattr(block, "_pyrung_field_final", False),
                        public=getattr(block, "_pyrung_field_public", False),
                        physical=phys.name if phys is not None else None,
                        link=getattr(block, "_pyrung_field_link", None),
                        min=getattr(block, "_pyrung_field_min", None),
                        max=getattr(block, "_pyrung_field_max", None),
                        uom=getattr(block, "_pyrung_field_uom", None),
                        choices=len(ch) if ch else None,
                    )
                )

            stride = getattr(rt, "stride", None)
            base_type_attr = getattr(rt, "type", None)
            base_type = base_type_attr.value if base_type_attr is not None else None

            result.append(
                StructureInfo(
                    name=name,
                    kind=kind,
                    count=count,
                    fields=tuple(fields),
                    stride=stride if kind == "named_array" else None,
                    base_type=base_type if kind == "named_array" else None,
                )
            )

        result.sort(key=lambda s: (s.kind, s.name))
        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _by_role(self, role: TagRole) -> frozenset[str]:
        return frozenset(t for t, r in self._graph.tag_roles.items() if r is role)
