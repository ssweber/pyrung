"""Parameterized codegen tests driven by CSV-with-comment fixtures.

Each CSV in ``tests/fixtures/user_shapes/`` carries the expected pyrung
rung body in its first rung's comment rows.  The test runs
``ladder_to_pyrung`` on the CSV, strips boilerplate, and compares.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from pyrung.click import ladder_to_pyrung
from tests.click.helpers import load_fixtures, normalize_pyrung, strip_pyrung_boilerplate

_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "user_shapes"


@pytest.mark.parametrize("fixture", load_fixtures(_FIXTURE_DIR), ids=lambda f: f.name)
def test_user_fixture(fixture):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        code = ladder_to_pyrung(fixture.csv_path)
    body = strip_pyrung_boilerplate(code)
    assert body == normalize_pyrung(fixture.expected)
