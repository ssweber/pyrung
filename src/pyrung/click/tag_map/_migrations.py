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
from dataclasses import dataclass

from ._parsers import _OLD_FORM_TOKENS, _TAG_META_GROUP_RE, _is_tag_meta

#: One token in a ``[...]`` group, with the spaces around it kept apart from its text.
_TOKEN_RE = re.compile(r"(?:(?<=,)|^)(\s*)([^,]*?)(\s*)(?=,|$)")


def _tokens(group: str) -> list[str]:
    return [token.strip() for token in group.split(",") if token.strip()]


def _params(text: str) -> dict[str, str] | None:
    """``a=1|b=2`` as a dict, as older pyrung read it, or ``None`` when a part has no ``=``."""
    values: dict[str, str] = {}
    for pair in text.split("|"):
        key, sep, value = pair.partition("=")
        if sep != "=":
            return None
        values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class CommentMigration:
    """One numbered rewrite of an old ``profile=<kind>:a=..|b=..`` token into new keys.

    It reads what older pyrung read. Spaces around the kind and the values are allowed, and
    a parameter older pyrung ignored is dropped.
    """

    number: int
    title: str
    #: The old ``profile=`` kind this migration rewrites, such as ``pulse``.
    kind: str
    #: Old parameter -> new key, in the order the new keys are written.
    renames: tuple[tuple[str, str], ...]
    #: Old parameters a token must have for older pyrung to have read it.
    required: tuple[str, ...]

    def rewrite_token(self, token: str) -> str | None:
        """The new tokens for one old *token*, or ``None`` when this migration can't read it."""
        head, _, value = token.partition("=")
        if head != "profile":
            return None
        kind, sep, params = value.partition(":")
        if sep != ":" or kind.strip() != self.kind:
            return None
        values = _params(params)
        if values is None or not all(values.get(name) for name in self.required):
            return None
        return ", ".join(f"{new}={values[old]}" for old, new in self.renames if values.get(old))

    def apply(self, comment: str) -> str:
        """*comment* with this migration's old tokens rewritten, every other character kept."""

        def group(match: re.Match[str]) -> str:
            content = match.group()[1:-1]
            if not _is_tag_meta(_tokens(content)):
                return match.group()

            def token(m: re.Match[str]) -> str:
                new = self.rewrite_token(m.group(2))
                return m.group(0) if new is None else f"{m.group(1)}{new}{m.group(3)}"

            return f"[{_TOKEN_RE.sub(token, content)}]"

        return _TAG_META_GROUP_RE.sub(group, comment)

    def applies(self, comment: str) -> bool:
        """True when this migration would change *comment*."""
        return self.apply(comment) != comment


#: Every migration, in the order they run.
MIGRATIONS: tuple[CommentMigration, ...] = (
    CommentMigration(
        1,
        "profile=pulse:on_dwell=..|off_dwell=.. -> flasher_on=.., flasher_off=..",
        "pulse",
        (("on_dwell", "flasher_on"), ("off_dwell", "flasher_off")),
        ("on_dwell", "off_dwell"),
    ),
    CommentMigration(
        2,
        "profile=ramp:up=..|down=.. -> ramp_on=.., ramp_off=..",
        "ramp",
        (("up", "ramp_on"), ("down", "ramp_off")),
        ("up",),
    ),
    CommentMigration(
        3,
        "profile=approach:toward=..|rate=.. -> approach_to=.., approach_rate=..",
        "approach",
        (("toward", "approach_to"), ("rate", "approach_rate")),
        ("toward", "rate"),
    ),
)


def old_form_tokens(comment: str) -> tuple[str, ...]:
    """Every old token in *comment*'s metadata groups. The parser refuses each one.

    ``old_form_tokens(upgrade_comment(c))`` lists the ones no migration can rewrite, so the
    user can fix them by hand.
    """
    found: list[str] = []
    for match in _TAG_META_GROUP_RE.finditer(comment):
        tokens = _tokens(match.group()[1:-1])
        if _is_tag_meta(tokens):
            found += [token for token in tokens if token.startswith(_OLD_FORM_TOKENS)]
    return tuple(found)


def comment_needs_upgrade(comment: str) -> bool:
    """True when *comment* holds an old token, whether or not a migration can rewrite it."""
    return bool(old_form_tokens(comment))


def pending_migrations(comment: str) -> tuple[CommentMigration, ...]:
    """The migrations that would change *comment*, in the order they run."""
    pending: list[CommentMigration] = []
    for migration in MIGRATIONS:
        upgraded = migration.apply(comment)
        if upgraded != comment:
            pending.append(migration)
            comment = upgraded
    return tuple(pending)


def upgrade_comment(comment: str) -> str:
    """*comment* with every migration run in order. A current comment comes back unchanged."""
    for migration in MIGRATIONS:
        comment = migration.apply(comment)
    return comment


__all__ = [
    "MIGRATIONS",
    "CommentMigration",
    "comment_needs_upgrade",
    "old_form_tokens",
    "pending_migrations",
    "upgrade_comment",
]
