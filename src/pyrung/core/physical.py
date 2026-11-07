"""Physical feedback declarations and the table of their keys.

A ``Physical`` says how a real device answers its command. It takes flat keywords, one per
value, and each keyword is also the key a CLICK nickname comment uses::

    Physical("MotorFb", on_delay="2s", off_delay="500ms")
    Physical("Encoder", flasher_on="8ms", flasher_off="8ms")
    Physical("Horn", timed="200ms", maintained=True)
    Physical("Lamp", toggle=True)
    Physical("Temp", ramp_on=0.8, ramp_off=-0.05)
    Physical("Oven", approach_to="Oven_SP", approach_rate=0.3)

``PHYSICAL_KINDS`` lists every kind and its keys. It drives ``Physical()``, the comment
parser, the comment writer and clicknick's annotation dialogs, so a key is described once.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, overload

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|min|s|m|h|d)")

_UNIT_TO_MS: dict[str, float] = {
    "ms": 1.0,
    "s": 1_000.0,
    "min": 60_000.0,
    "m": 60_000.0,
    "h": 3_600_000.0,
    "d": 86_400_000.0,
}


def parse_duration(text: str) -> int:
    """Parse a compound duration string into milliseconds.

    Accepts strings like ``"2s"``, ``"500ms"``, ``"2s50ms"``,
    ``"1h30min"``.  Tokens are summed left-to-right.

    Raises ``ValueError`` for empty or unparseable strings.
    """
    stripped = text.strip()
    if stripped.startswith("T#"):
        stripped = stripped[2:].strip()
    if not stripped:
        raise ValueError("empty duration string")

    total = 0.0
    pos = 0
    found = False

    for match in _DURATION_TOKEN.finditer(stripped):
        if match.start() != pos:
            bad = stripped[pos : match.start()].strip()
            if bad:
                raise ValueError(f"unexpected '{bad}' in duration '{text}'")
        value = float(match.group(1))
        unit = match.group(2)
        total += value * _UNIT_TO_MS[unit]
        pos = match.end()
        found = True

    if not found:
        raise ValueError(f"no duration tokens in '{text}'")

    trailing = stripped[pos:].strip()
    if trailing:
        raise ValueError(f"unexpected '{trailing}' in duration '{text}'")

    return int(total)


def parse_number(text: str) -> float:
    """A rate as a comment writes it. Raises ``ValueError`` unless it is a finite number."""
    try:
        value = float(text.strip())
    except ValueError:
        raise ValueError(f"expected a number, got {text!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"expected a finite number, got {text!r}")
    return value


def parse_number_or_tag(text: str) -> float | str:
    """A target as a comment writes it: numeric text is a number, other text a tag name."""
    stripped = text.strip()
    if not stripped:
        raise ValueError("expected a number or a tag name, got nothing")
    try:
        return parse_number(stripped)
    except ValueError:
        return stripped


def format_number(value: float) -> str:
    """Comment text for a number. A whole number drops its ``.0``, and others keep every digit."""
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


FeedbackType = Literal["bool", "analog"]
#: How a key's value is written: a time with units, a number, a number or a tag name, or a
#: bare flag.
ValueType = Literal["duration", "number", "number_or_tag", "flag"]


@dataclass(frozen=True)
class PhysicalKey:
    """One ``Physical`` keyword, which is also its comment key.

    ``default`` is the value when the key is left out (``None`` means nothing). ``suggested``
    is the comment text a dialog starts with.
    """

    name: str
    kind: str
    value_type: ValueType
    label: str
    help: str
    unit: str = ""
    required: bool = True
    default: float | bool | None = None
    suggested: str | None = None

    @property
    def is_flag(self) -> bool:
        return self.value_type == "flag"

    def parse(self, text: str) -> Any:
        """The Python value for comment text. Raises ``ValueError`` naming the key."""
        try:
            if self.value_type == "duration":
                parse_duration(text)
                return text.strip()
            if self.value_type == "number":
                return parse_number(text)
            if self.value_type == "number_or_tag":
                return parse_number_or_tag(text)
        except ValueError as exc:
            raise ValueError(f"{self.name}: {exc}") from None
        raise ValueError(f"{self.name} is a bare flag. Write {self.name} without a value.")

    def check(self, value: Any, *, owner: str) -> Any:
        """The stored Python value for a keyword argument, or raise naming *owner* and the key."""
        where = f"Physical {owner!r}: {self.name}"
        if self.value_type == "flag":
            if not isinstance(value, bool):
                raise TypeError(f"{where} takes True or False (got {value!r}).")
            return value
        if self.value_type == "duration":
            if not isinstance(value, str):
                raise TypeError(
                    f"{where} takes a time with units, such as '500ms' or '2s' (got {value!r})."
                )
            try:
                parse_duration(value)
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from None
            return value
        if isinstance(value, str):
            if self.value_type == "number":
                raise TypeError(f"{where} takes a number (got {value!r}).")
            stripped = value.strip()
            if not stripped:
                raise ValueError(f"{where} needs a number or a tag name.")
            try:
                float(stripped)
            except ValueError:
                return stripped
            raise ValueError(
                f"{where}={value!r} is text that looks like a number. "
                f"Write {self.name}={stripped} for a number, or give a tag name."
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{where} takes a number (got {value!r}).")
        if not math.isfinite(value):
            raise ValueError(f"{where} must be a finite number (got {value!r}).")
        return float(value)

    def text(self, value: Any) -> str:
        """Comment text for a stored value. A flag's text is its key."""
        if self.value_type == "flag":
            return self.name
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return format_number(float(value))
        return str(value)

    def token(self, value: Any) -> str:
        """The comment token: ``key=text``, or the bare key for a flag."""
        if self.value_type == "flag":
            return self.name
        return f"{self.name}={self.text(value)}"


@dataclass(frozen=True)
class PhysicalKind:
    """One way a device answers its command, and the keys that describe it.

    ``needs`` is ``"any"`` when at least one key must be given, and ``"all"`` when every
    required key must be. ``runs_as`` names the CLICK instruction the kind behaves like.
    """

    name: str
    feedback: FeedbackType
    label: str
    description: str
    runs_as: str
    keys: tuple[PhysicalKey, ...]
    needs: Literal["any", "all"] = "all"

    @property
    def key_names(self) -> tuple[str, ...]:
        return tuple(key.name for key in self.keys)

    @property
    def flags(self) -> tuple[PhysicalKey, ...]:
        return tuple(key for key in self.keys if key.is_flag)

    def missing(self, given: Iterable[str]) -> tuple[str, ...]:
        """The keys this kind still needs when *given* are set."""
        present = set(given)
        if self.needs == "any":
            return () if present & set(self.key_names) else self.key_names
        return tuple(k.name for k in self.keys if k.required and k.name not in present)


_DURATION_UNIT = "ms, s, min, h or d"
_RATE_UNIT = "per second"


def _duration(
    name: str, kind: str, label: str, help: str, suggested: str, *, required: bool = True
) -> PhysicalKey:
    return PhysicalKey(
        name,
        kind,
        "duration",
        label,
        help,
        unit=_DURATION_UNIT,
        required=required,
        suggested=suggested,
    )


PHYSICAL_KINDS: tuple[PhysicalKind, ...] = (
    PhysicalKind(
        "delay",
        "bool",
        "On/off delay",
        "Turns on after the command has held for a time, and off a time after it drops.",
        "debounce",
        (
            _duration(
                "on_delay",
                "delay",
                "On delay",
                "How long the command must hold before the feedback turns on. Left out, 0.",
                "500ms",
                required=False,
            ),
            _duration(
                "off_delay",
                "delay",
                "Off delay",
                "How long the feedback stays on after the command drops. Left out, 0.",
                "200ms",
                required=False,
            ),
        ),
        needs="any",
    ),
    PhysicalKind(
        "flasher",
        "bool",
        "Flasher",
        "Blinks while the command is on, like a pulse sensor.",
        "flasher",
        (
            _duration("flasher_on", "flasher", "On time", "How long each blink stays on.", "500ms"),
            _duration(
                "flasher_off",
                "flasher",
                "Off time",
                "How long it stays off between blinks.",
                "500ms",
            ),
        ),
    ),
    PhysicalKind(
        "timed",
        "bool",
        "Timed",
        "Turns on for a set time on each command.",
        "timed",
        (
            _duration(
                "timed", "timed", "On time", "How long the feedback stays on per command.", "200ms"
            ),
            PhysicalKey(
                "maintained",
                "timed",
                "flag",
                "Maintained",
                "Stays on for at least the on time, then follows the command.",
                required=False,
                default=False,
            ),
        ),
    ),
    PhysicalKind(
        "toggle",
        "bool",
        "Toggle",
        "Each command flips the feedback (push-on, push-off).",
        "toggle",
        (
            PhysicalKey(
                "toggle",
                "toggle",
                "flag",
                "Toggle",
                "Each new command flips the feedback on or off.",
            ),
        ),
    ),
    PhysicalKind(
        "ramp",
        "analog",
        "Ramp",
        "An analog value that moves at a steady rate.",
        "math",
        (
            PhysicalKey(
                "ramp_on",
                "ramp",
                "number",
                "Rate on",
                "Change per second while the command is on. Negative moves down.",
                unit=_RATE_UNIT,
                suggested="1.0",
            ),
            PhysicalKey(
                "ramp_off",
                "ramp",
                "number",
                "Rate off",
                "Change per second while the command is off. 0 holds the value.",
                unit=_RATE_UNIT,
                required=False,
                default=0.0,
                suggested="-0.5",
            ),
        ),
    ),
    PhysicalKind(
        "approach",
        "analog",
        "Approach",
        "An analog value that closes on a target, fast at first and slower near it.",
        "math",
        (
            PhysicalKey(
                "approach_to",
                "approach",
                "number_or_tag",
                "Target",
                "The value it moves toward while the command is on: a number or a tag name.",
                suggested="100",
            ),
            PhysicalKey(
                "approach_rate",
                "approach",
                "number",
                "Rate",
                "Share of the remaining gap closed per second. It holds while the command is off.",
                unit=_RATE_UNIT,
                suggested="0.3",
            ),
        ),
    ),
)

#: Every response key by name, in the order a comment writes them.
PHYSICAL_KEYS: dict[str, PhysicalKey] = {
    key.name: key for kind in PHYSICAL_KINDS for key in kind.keys
}
_KINDS_BY_NAME: dict[str, PhysicalKind] = {kind.name: kind for kind in PHYSICAL_KINDS}


def physical_kind(name: str) -> PhysicalKind:
    """The kind called *name* (``"delay"``, ``"flasher"``, ...)."""
    try:
        return _KINDS_BY_NAME[name]
    except KeyError:
        raise ValueError(
            f"Unknown physical kind {name!r}. Kinds: {', '.join(_KINDS_BY_NAME)}."
        ) from None


def _kinds_hint() -> str:
    return ", ".join("/".join(kind.key_names) for kind in PHYSICAL_KINDS)


def kind_of_keys(keys: Iterable[str], *, owner: str | None = None) -> PhysicalKind | None:
    """The kind the response keys *keys* belong to, or ``None`` for no keys.

    Raises ``ValueError`` for a name that is no response key, for keys of two kinds, and for
    a kind missing a key it needs (``maintained`` without ``timed``).
    """
    given = list(keys)
    prefix = f"Physical {owner!r}: " if owner is not None else ""
    unknown = [key for key in given if key not in PHYSICAL_KEYS]
    if unknown:
        raise ValueError(f"{prefix}unknown key {unknown[0]!r}. Keys: {_kinds_hint()}.")
    kinds: dict[str, list[str]] = {}
    for key in given:
        kinds.setdefault(PHYSICAL_KEYS[key].kind, []).append(key)
    if not kinds:
        return None
    if len(kinds) > 1:
        (first, first_keys), (second, second_keys) = list(kinds.items())[:2]
        raise ValueError(
            f"{prefix}{first_keys[0]} ({first}) and {second_keys[0]} ({second}) are keys "
            "of two kinds. A feedback has one kind."
        )
    kind = _KINDS_BY_NAME[next(iter(kinds))]
    missing = kind.missing(given)
    if missing:
        raise ValueError(f"{prefix}{', '.join(given)} needs {' or '.join(missing)}.")
    return kind


#: Names that left the public API, and what to write instead.
REMOVED_NAMES: dict[str, str] = {
    "Pulse": "Physical(name, flasher_on='8ms', flasher_off='8ms')",
    "Ramp": "Physical(name, ramp_on=0.8, ramp_off=-0.05)",
    "Approach": "Physical(name, approach_to=180, approach_rate=0.3)",
}


def removed_name_error(module: str, name: str) -> ImportError:
    """The error for an old feedback class, naming the keywords that replace it."""
    return ImportError(
        f"{module}.{name} was removed. Give its values to Physical as keywords: "
        f"{REMOVED_NAMES[name]}."
    )


def __getattr__(name: str) -> Any:
    if name in REMOVED_NAMES:
        raise removed_name_error(__name__, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _is_set(value: Any) -> bool:
    return value is not None and value is not False


@dataclass(frozen=True, init=False, repr=False)
class Physical:
    """Declares how a feedback answers its linked command.

    Each kind takes its own keywords, and a feedback has one kind::

        Physical("MotorFb", on_delay="2s", off_delay="500ms")
        Physical("Encoder", flasher_on="8ms", flasher_off="8ms")
        Physical("Horn", timed="200ms", maintained=True)
        Physical("Lamp", toggle=True)
        Physical("Temp", ramp_on=0.8, ramp_off=-0.05)
        Physical("Oven", approach_to="Oven_SP", approach_rate=0.3)

    Times are strings with units. Rates are numbers per second. ``approach_to`` is a number
    or a tag name. ``system`` groups related feedback for reporting.
    """

    name: str
    on_delay: str | None
    off_delay: str | None
    flasher_on: str | None
    flasher_off: str | None
    timed: str | None
    maintained: bool
    toggle: bool
    ramp_on: float | None
    ramp_off: float | None
    approach_to: float | str | None
    approach_rate: float | None
    system: str | None

    @overload
    def __init__(
        self,
        name: str,
        *,
        on_delay: str | None = None,
        off_delay: str | None = None,
        system: str | None = None,
    ) -> None: ...
    @overload
    def __init__(
        self, name: str, *, flasher_on: str, flasher_off: str, system: str | None = None
    ) -> None: ...
    @overload
    def __init__(
        self, name: str, *, timed: str, maintained: bool = False, system: str | None = None
    ) -> None: ...
    @overload
    def __init__(self, name: str, *, toggle: Literal[True], system: str | None = None) -> None: ...
    @overload
    def __init__(
        self, name: str, *, ramp_on: float, ramp_off: float = 0.0, system: str | None = None
    ) -> None: ...
    @overload
    def __init__(
        self,
        name: str,
        *,
        approach_to: float | str,
        approach_rate: float,
        system: str | None = None,
    ) -> None: ...
    def __init__(
        self,
        name: str,
        *,
        on_delay: str | None = None,
        off_delay: str | None = None,
        flasher_on: str | None = None,
        flasher_off: str | None = None,
        timed: str | None = None,
        maintained: bool = False,
        toggle: bool = False,
        ramp_on: float | None = None,
        ramp_off: float | None = None,
        approach_to: float | str | None = None,
        approach_rate: float | None = None,
        system: str | None = None,
        **removed: Any,
    ) -> None:
        if removed:
            key = next(iter(removed))
            if key == "profile":
                raise TypeError(
                    "Physical() no longer takes profile=. Give the keywords of one kind: "
                    f"{_kinds_hint()}."
                )
            raise TypeError(f"Physical() got an unexpected keyword argument {key!r}")
        if not name:
            raise ValueError("Physical name must be non-empty")
        given = {
            "on_delay": on_delay,
            "off_delay": off_delay,
            "flasher_on": flasher_on,
            "flasher_off": flasher_off,
            "timed": timed,
            "maintained": maintained,
            "toggle": toggle,
            "ramp_on": ramp_on,
            "ramp_off": ramp_off,
            "approach_to": approach_to,
            "approach_rate": approach_rate,
        }
        values: dict[str, Any] = {}
        for key_name, value in given.items():
            key = PHYSICAL_KEYS[key_name]
            # A flag left False is a flag not given.
            if value is None or (key.is_flag and value is False):
                continue
            values[key_name] = key.check(value, owner=name)
        kind = kind_of_keys(values, owner=name)
        if kind is None:
            raise ValueError(
                f"Physical {name!r} has no response. Give the keywords of one kind: "
                f"{_kinds_hint()}."
            )
        # A key left out takes its default, so Python and a comment that omits it are equal.
        for key in kind.keys:
            if key.name not in values and key.default is not None:
                values[key.name] = key.default
        object.__setattr__(self, "name", name)
        for key_name, key in PHYSICAL_KEYS.items():
            object.__setattr__(self, key_name, values.get(key_name, False if key.is_flag else None))
        object.__setattr__(self, "system", system)
        # Not a field, so equality and hashing read only the keys.
        object.__setattr__(self, "_kind", kind.name)

    def __repr__(self) -> str:
        args = [repr(self.name)]
        args += [f"{key}={value!r}" for key, value in self.keywords().items()]
        if self.system is not None:
            args.append(f"system={self.system!r}")
        return f"Physical({', '.join(args)})"

    @classmethod
    def from_text(
        cls, name: str, values: Mapping[str, str | bool], *, system: str | None = None
    ) -> Physical:
        """Build from comment text: ``{"flasher_on": "8ms", ...}``, with a flag as ``True``.

        Raises ``ValueError`` for an unknown key, a bad value, or keys that make no one kind.
        """
        kwargs: dict[str, Any] = {}
        for key_name, raw in values.items():
            key = PHYSICAL_KEYS.get(key_name)
            if key is None:
                raise ValueError(f"Physical {name!r}: unknown key {key_name!r}.")
            if key.is_flag:
                if raw is not True:
                    raise ValueError(f"{key_name} is a bare flag. Write {key_name} alone.")
                kwargs[key_name] = True
            elif not isinstance(raw, str):
                raise ValueError(f"{key_name} takes text, got {raw!r}.")
            else:
                kwargs[key_name] = key.parse(raw)
        return cls(name, system=system, **kwargs)

    @property
    def kind_spec(self) -> PhysicalKind:
        """The row of ``PHYSICAL_KINDS`` this feedback belongs to."""
        return _KINDS_BY_NAME[self.__dict__["_kind"]]

    @property
    def kind(self) -> str:
        """``delay``, ``flasher``, ``timed``, ``toggle``, ``ramp`` or ``approach``."""
        return self.kind_spec.name

    @property
    def feedback_type(self) -> FeedbackType:
        return self.kind_spec.feedback

    def keywords(self) -> dict[str, Any]:
        """The response keywords set on this feedback, leaving out a key at its default.

        ``Physical(p.name, **p.keywords(), system=p.system) == p``.
        """
        out: dict[str, Any] = {}
        for key in self.kind_spec.keys:
            value = getattr(self, key.name)
            if _is_set(value) and value != key.default:
                out[key.name] = value
        return out

    def comment_text(self) -> dict[str, str | bool]:
        """The response keys as a comment writes them, with a flag as ``True``."""
        return {
            name: True if PHYSICAL_KEYS[name].is_flag else PHYSICAL_KEYS[name].text(value)
            for name, value in self.keywords().items()
        }

    def tokens(self) -> list[str]:
        """The comment tokens for the response: ``["flasher_on=8ms", "flasher_off=8ms"]``."""
        return [PHYSICAL_KEYS[name].token(value) for name, value in self.keywords().items()]

    @property
    def on_delay_ms(self) -> int | None:
        return None if self.on_delay is None else parse_duration(self.on_delay)

    @property
    def off_delay_ms(self) -> int | None:
        return None if self.off_delay is None else parse_duration(self.off_delay)

    @property
    def flasher_on_ms(self) -> int | None:
        return None if self.flasher_on is None else parse_duration(self.flasher_on)

    @property
    def flasher_off_ms(self) -> int | None:
        return None if self.flasher_off is None else parse_duration(self.flasher_off)

    @property
    def timed_ms(self) -> int | None:
        return None if self.timed is None else parse_duration(self.timed)
