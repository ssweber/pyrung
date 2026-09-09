"""Shared project configuration for static checks; library checks never read cwd implicitly."""

from __future__ import annotations

import os
import tempfile
import tomllib
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyrung.core.validation.registry import resolve_rules


@dataclass(frozen=True)
class CheckConfig:
    select: tuple[str, ...] | None = None
    extend_select: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, values: object) -> CheckConfig:
        if not isinstance(values, dict):
            raise ValueError("tool.pyrung.check must be a table")
        unknown = set(values) - {"select", "extend-select", "ignore"}
        if unknown:
            raise ValueError(f"Unknown check settings: {', '.join(sorted(map(str, unknown)))}")
        validated: dict[str, tuple[str, ...]] = {}
        for key, value in values.items():
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"Check setting {key!r} must be an array of rule selectors")
            validated[str(key)] = tuple(str(item) for item in value)
        return cls(
            select=validated.get("select"),
            extend_select=validated.get("extend-select", ()),
            ignore=validated.get("ignore", ()),
        )

    def as_mapping(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if self.select is not None:
            values["select"] = list(self.select)
        if self.extend_select:
            values["extend-select"] = list(self.extend_select)
        if self.ignore:
            values["ignore"] = list(self.ignore)
        return values

    def resolve(self) -> frozenset[str]:
        return resolve_rules(
            set(self.select) if self.select is not None else None,
            set(self.ignore),
            set(self.extend_select),
        )


def load_check_config(path: Path) -> CheckConfig | None:
    """Read one pyproject; missing files or check sections have no project policy."""
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    tool = data.get("tool", {})
    pyrung = tool.get("pyrung", {}) if isinstance(tool, dict) else {}
    if not isinstance(pyrung, dict) or "check" not in pyrung:
        return None
    return CheckConfig.from_mapping(pyrung["check"])


def find_check_config(start: Path) -> tuple[CheckConfig, Path | None]:
    """Find the closest ancestor pyproject containing tool.pyrung.check."""
    start = start.resolve()
    if start.is_file():
        start = start.parent
    for directory in (start, *start.parents):
        path = directory / "pyproject.toml"
        config = load_check_config(path)
        if config is not None:
            return config, path
    return CheckConfig(), None


def save_check_config(path: Path, config: CheckConfig) -> None:
    """Update just the check settings, preserving other TOML values and comments."""
    import tomlkit

    config.resolve()
    document = (
        tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    )
    tool = document.setdefault("tool", tomlkit.table())
    if not isinstance(tool, MutableMapping):
        raise ValueError("tool must be a table")
    pyrung = tool.setdefault("pyrung", tomlkit.table())
    if not isinstance(pyrung, MutableMapping):
        raise ValueError("tool.pyrung must be a table")
    check = pyrung.setdefault("check", tomlkit.table())
    if not isinstance(check, MutableMapping):
        raise ValueError("tool.pyrung.check must be a table")
    values = config.as_mapping()
    for key in ("select", "extend-select", "ignore"):
        if key in values:
            check[key] = values[key]
        elif key in check:
            del check[key]
    path.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            staging = Path(stream.name)
            stream.write(tomlkit.dumps(document))
        os.replace(staging, path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)
