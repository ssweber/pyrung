"""Comment migrations: each rewrites one old annotation form and keeps the rest of the comment."""

from __future__ import annotations

import pytest

from pyrung.click.tag_map import (
    MIGRATIONS,
    comment_needs_upgrade,
    parse_tag_meta,
    pending_migrations,
    upgrade_comment,
)
from pyrung.core.physical import Physical

OLD_AND_NEW = [
    (
        "[link=Run, profile=pulse:on_dwell=8ms|off_dwell=12ms] Encoder",
        "[link=Run, flasher_on=8ms, flasher_off=12ms] Encoder",
        1,
    ),
    (
        "[link=Heat, profile=ramp:up=0.8|down=-0.05, min=0, max=200] Oven temp",
        "[link=Heat, ramp_on=0.8, ramp_off=-0.05, min=0, max=200] Oven temp",
        2,
    ),
    ("[link=Heat, profile=ramp:up=2.5]", "[link=Heat, ramp_on=2.5]", 2),
    (
        "[link=Pump, profile=approach:toward=SetPt|rate=0.3, uom=psi]",
        "[link=Pump, approach_to=SetPt, approach_rate=0.3, uom=psi]",
        3,
    ),
    (
        "[profile=approach:toward=180|rate=0.3, link=Burner]",
        "[approach_to=180, approach_rate=0.3, link=Burner]",
        3,
    ),
]


@pytest.mark.parametrize(("old", "new", "number"), OLD_AND_NEW)
def test_each_migration_rewrites_its_form(old, new, number):
    assert comment_needs_upgrade(old)
    assert [m.number for m in pending_migrations(old)] == [number]
    assert upgrade_comment(old) == new
    assert not comment_needs_upgrade(new)
    assert upgrade_comment(new) == new


@pytest.mark.parametrize(("old", "new", "number"), OLD_AND_NEW)
def test_the_parser_refuses_the_old_form_and_reads_the_new(old, new, number):
    with pytest.raises(ValueError, match='Run "Upgrade project" in clicknick') as caught:
        parse_tag_meta(old)
    assert "upgrade_comment()" in str(caught.value)
    assert str(caught.value).isascii()
    meta, _rest = parse_tag_meta(new)
    assert meta is not None and meta.physical_kind is not None


def test_the_rest_of_the_comment_keeps_its_exact_text():
    old = (
        "Zone 3 °C  [schedule] [readonly,  profile=ramp:up=1|down=-0.5 ,lock]"
        "  café [note, profile=ramp:up=1]"
    )
    new = (
        "Zone 3 °C  [schedule] [readonly,  ramp_on=1, ramp_off=-0.5 ,lock]"
        "  café [note, profile=ramp:up=1]"
    )
    # The last group starts with text, so it is a note and stays as written.
    assert upgrade_comment(old) == new


def test_the_upgraded_comment_gives_the_old_physical():
    old = "[link=Run, profile=pulse:on_dwell=8ms|off_dwell=8ms]"
    meta, _rest = parse_tag_meta(upgrade_comment(old))
    assert meta is not None
    physical = Physical.from_text("Encoder", meta.physical_values())
    assert physical == Physical("Encoder", flasher_on="8ms", flasher_off="8ms")


@pytest.mark.parametrize(
    "comment",
    [
        "[link=Run, profile=pulse:on_dwell=8ms]",  # off_dwell missing
        "[link=Run, profile=ramp:rise=1]",  # unknown parameter
        "[link=Run, profile=approach:toward=1|rate=0.3|gain=2]",
        "[link=Run, profile=bogus:x=1]",
    ],
)
def test_an_old_form_no_migration_reads_is_left_alone(comment):
    assert upgrade_comment(comment) == comment
    assert not comment_needs_upgrade(comment)
    with pytest.raises(ValueError, match="Upgrade project"):
        parse_tag_meta(comment)


def test_a_current_comment_needs_nothing():
    for comment in ("", "plain text", "[link=Run, on_delay=1s] Motor", "[timed=1s, maintained]"):
        assert upgrade_comment(comment) == comment
        assert pending_migrations(comment) == ()


def test_migrations_are_numbered_in_order():
    assert [m.number for m in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))
    for migration in MIGRATIONS:
        assert migration.title.isascii()
