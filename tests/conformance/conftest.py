"""Fixtures for the conformance suite."""

from __future__ import annotations

import os

import pytest

from pyrung.conformance import Endpoint, parse_endpoint

#: Environment fallback for ``--conformance-plc``.
ENDPOINT_ENV = "PYRUNG_CONFORMANCE_PLC"
#: Environment fallback for ``--conformance-group``.
GROUP_ENV = "PYRUNG_CONFORMANCE_GROUP"


@pytest.fixture(scope="session")
def conformance_endpoint(request: pytest.FixtureRequest) -> Endpoint | None:
    """``host[:port]`` of the PLC under test, or ``None`` for pyrung's local soft PLC."""
    raw = request.config.getoption("conformance_plc", default=None) or os.environ.get(ENDPOINT_ENV)
    return parse_endpoint(raw) if raw else None


@pytest.fixture(scope="session")
def conformance_groups(request: pytest.FixtureRequest) -> list[str] | None:
    """Group names to run over Modbus, or ``None`` for every group."""
    raw = request.config.getoption("conformance_group", default=None) or os.environ.get(GROUP_ENV)
    if not raw:
        return None
    return [name.strip() for name in raw.split(",") if name.strip()]
