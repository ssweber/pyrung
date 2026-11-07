"""Numbered rewrites of old nickname-comment annotations into the current form.

The comment parser reads only the current form and refuses an old one. Each migration here
rewrites one old form into the next, so old knowledge lives in this file alone. clicknick's
"Upgrade project" runs them over every comment through ``upgrade_comment``::

    upgrade_comment("[link=Run, profile=pulse:on_dwell=8ms|off_dwell=8ms] Encoder")
    # -> "[link=Run, flasher_on=8ms, flasher_off=8ms] Encoder"

A migration changes only the old token. The rest of the comment keeps its exact text.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ._parsers import _TAG_META_GROUP_RE, _starts_tag_meta


def _params(text: str) -> dict[str, str] | None:
    """``a=1|b=2`` as a dict, or ``None`` when a part has no ``=``."""
    values: dict[str, str] = {}
    for pair in text.split("|"):
        key, sep, value = pair.partition("=")
        if sep != "=" or not key.strip() or not value.strip():
            return None
        values[key.strip()] = value.strip()
    return values


def _renamer(
    renames: tuple[tuple[str, str], ...], required: tuple[str, ...]
) -> Callable[[str], str | None]:
    """Rewrite old ``a=1|b=2`` parameters as new ``x=1, y=2`` tokens, in *renames* order."""

    def rewrite(text: str) -> str | None:
        values = _params(text)
        if values is None or set(values) - {old for old, _new in renames}:
            return None
        if any(name not in values for name in required):
            return None
        return ", ".join(f"{new}={values[old]}" for old, new in renames if old in values)

    return rewrite


@dataclass(frozen=True)
class CommentMigration:
    """One numbered rewrite of an old ``[...]`` token into the next form."""

    number: int
    title: str
    #: The old token's prefix, such as ``profile=pulse:``.
    prefix: str
    #: The old token's text after the prefix -> the new tokens, or ``None`` if it can't.
    rewrite_value: Callable[[str], str | None]

    def _token_re(self) -> re.Pattern[str]:
        return re.compile(r"(?:(?<=,)|^)(\s*)" + re.escape(self.prefix) + r"([^,]*?)(\s*)(?=,|$)")

    def _rewrite_group(self, content: str) -> str:
        tokens = [token.strip() for token in content.split(",") if token.strip()]
        if not tokens or not _starts_tag_meta(tokens[0]):
            return content

        def replace(match: re.Match[str]) -> str:
            new = self.rewrite_value(match.group(2).strip())
            if new is None:
                return match.group(0)
            return f"{match.group(1)}{new}{match.group(3)}"

        return self._token_re().sub(replace, content)

    def apply(self, comment: str) -> str:
        """*comment* with this migration's old tokens rewritten, every other character kept."""
        return _TAG_META_GROUP_RE.sub(
            lambda m: f"[{self._rewrite_group(m.group()[1:-1])}]", comment
        )

    def applies(self, comment: str) -> bool:
        """True when this migration would change *comment*."""
        return self.apply(comment) != comment


#: Every migration, in the order they run.
MIGRATIONS: tuple[CommentMigration, ...] = (
    CommentMigration(
        1,
        "profile=pulse:on_dwell=..|off_dwell=.. -> flasher_on=.., flasher_off=..",
        "profile=pulse:",
        _renamer((("on_dwell", "flasher_on"), ("off_dwell", "flasher_off")), ("on_dwell", "off_dwell")),
    ),
    CommentMigration(
        2,
        "profile=ramp:up=..|down=.. -> ramp_on=.., ramp_off=..",
        "profile=ramp:",
        _renamer((("up", "ramp_on"), ("down", "ramp_off")), ("up",)),
    ),
    CommentMigration(
        3,
        "profile=approach:toward=..|rate=.. -> approach_to=.., approach_rate=..",
        "profile=approach:",
        _renamer((("toward", "approach_to"), ("rate", "approach_rate")), ("toward", "rate")),
    ),
)


def pending_migrations(comment: str) -> tuple[CommentMigration, ...]:
    """The migrations that would change *comment*, in the order they run."""
    pending: list[CommentMigration] = []
    for migration in MIGRATIONS:
        upgraded = migration.apply(comment)
        if upgraded != comment:
            pending.append(migration)
            comment = upgraded
    return tuple(pending)


def comment_needs_upgrade(comment: str) -> bool:
    """True when some migration would rewrite *comment*."""
    return bool(pending_migrations(comment))


def upgrade_comment(comment: str) -> str:
    """*comment* with every migration run in order. A current comment comes back unchanged."""
    for migration in MIGRATIONS:
        comment = migration.apply(comment)
    return comment


__all__ = [
    "MIGRATIONS",
    "CommentMigration",
    "comment_needs_upgrade",
    "pending_migrations",
    "upgrade_comment",
]
