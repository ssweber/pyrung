"""Physical feedback declarations.

A ``Physical`` describes the real-world response characteristics of a
feedback signal — how long a bool feedback takes to assert/deassert, or
which declarative profile (``Ramp`` / ``Approach`` / ``Pulse``) drives it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Literal

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


FeedbackType = Literal["bool", "analog"]


def _fmt_rate(value: float) -> str:
    """Serialize a rate for a comment token (repr round-trips exactly for floats)."""
    return repr(float(value))


def _parse_number_or_tag(raw: str) -> float | str:
    """A spec parameter that is either a numeric constant or a tag-name reference."""
    text = raw.strip()
    try:
        return float(text)
    except ValueError:
        return text


def _fmt_number_or_tag(value: float | str) -> str:
    return _fmt_rate(value) if isinstance(value, (int, float)) else value


def _split_spec_params(params: str, *, kind: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in params.split("|"):
        key, sep, raw = pair.partition("=")
        if sep != "=":
            raise ValueError(f"Invalid {kind} profile parameter {pair!r}.")
        values[key.strip()] = raw.strip()
    return values


@dataclass(frozen=True)
class Ramp:
    """Linear analog plant response — a constant-slope ramp.

    ``Fb`` moves ``up`` units per second while the linked enable is active and
    ``down`` units per second otherwise (``down`` is typically negative — an
    ambient decay or bleed-down; ``0`` means "hold on enable fall").  Rates are
    per **second**, so they are stable across scan periods.

    Lowered by :func:`pyrung.core.synthesis.analog_feedback_rungs` to an arm
    latch plus two guarded ``calc`` plant rungs reading ``sys.dt`` — real,
    inspectable rungs that fold and trace natively (no Python tick, no io-gap).

    The declaration is pure data, so it round-trips through a Click nickname
    comment as ``profile=ramp:up=<up>|down=<down>`` (see :meth:`to_token`).
    """

    up: float
    down: float = 0.0

    kind: ClassVar[str] = "ramp"

    def to_token(self) -> str:
        """The comment-field serialization: ``ramp:up=<up>|down=<down>``."""
        return f"{self.kind}:up={_fmt_rate(self.up)}|down={_fmt_rate(self.down)}"

    @classmethod
    def _from_params(cls, params: str) -> Ramp:
        values = _split_spec_params(params, kind=cls.kind)
        if "up" not in values:
            raise ValueError(f"Ramp profile requires 'up' (got {params!r}).")
        return cls(up=float(values["up"]), down=float(values.get("down", "0.0")))


@dataclass(frozen=True)
class Approach:
    """First-order (exponential) analog plant response.

    ``Fb`` moves toward ``toward`` by fraction ``rate`` per second while the
    linked enable is active — ``Fb += rate*(toward - Fb)*dt`` — and holds on
    enable fall.  ``toward`` is a constant setpoint (a number) or a tag name (a
    live setpoint register).  Lowered to a single guarded ``calc`` plant rung
    reading ``sys.dt``.

    The slope depends on the current value, so ``how()`` measures the coast
    empirically (fork-and-run) rather than analytically.  Round-trips through a
    Click comment as ``profile=approach:toward=<toward>|rate=<rate>``.
    """

    toward: float | str
    rate: float

    kind: ClassVar[str] = "approach"

    def to_token(self) -> str:
        """The comment-field serialization: ``approach:toward=<toward>|rate=<rate>``."""
        return f"{self.kind}:toward={_fmt_number_or_tag(self.toward)}|rate={_fmt_rate(self.rate)}"

    @classmethod
    def _from_params(cls, params: str) -> Approach:
        values = _split_spec_params(params, kind=cls.kind)
        if "toward" not in values or "rate" not in values:
            raise ValueError(f"Approach profile requires 'toward' and 'rate' (got {params!r}).")
        return cls(toward=_parse_number_or_tag(values["toward"]), rate=float(values["rate"]))


@dataclass(frozen=True)
class Pulse:
    """Bool pulse-train feedback — an astable oscillation while enabled.

    ``Fb`` cycles high for ``on_dwell`` then low for ``off_dwell`` (compound
    duration strings, e.g. ``"0.5s"``) as long as the linked enable is active,
    and rests low when disabled.  This is the declarative form of a discrete
    pulse sensor (a shaft encoder, a flow-meter pulse output) — lowered by
    :func:`pyrung.core.synthesis.pulse_feedback_rungs` to a self-resetting
    on-delay pair plus two toggle rungs (the classic ladder "flasher").

    Round-trips through a Click comment as
    ``profile=pulse:on_dwell=<on_dwell>|off_dwell=<off_dwell>``.
    """

    on_dwell: str
    off_dwell: str

    kind: ClassVar[str] = "pulse"

    def __post_init__(self) -> None:
        parse_duration(self.on_dwell)
        parse_duration(self.off_dwell)

    @property
    def on_dwell_ms(self) -> int:
        return parse_duration(self.on_dwell)

    @property
    def off_dwell_ms(self) -> int:
        return parse_duration(self.off_dwell)

    def to_token(self) -> str:
        """The comment-field serialization: ``pulse:on_dwell=<..>|off_dwell=<..>``."""
        return f"{self.kind}:on_dwell={self.on_dwell}|off_dwell={self.off_dwell}"

    @classmethod
    def _from_params(cls, params: str) -> Pulse:
        values = _split_spec_params(params, kind=cls.kind)
        if "on_dwell" not in values or "off_dwell" not in values:
            raise ValueError(f"Pulse profile requires 'on_dwell' and 'off_dwell' (got {params!r}).")
        return cls(on_dwell=values["on_dwell"], off_dwell=values["off_dwell"])


#: A declarative analog plant response — lowered to transparent plant rungs.
AnalogSpec = Ramp | Approach
#: Any declarative feedback profile: analog (``Ramp`` / ``Approach``) or the bool
#: pulse train (``Pulse``).  All lower to transparent plant rungs.
ProfileSpec = Ramp | Approach | Pulse

_SPEC_KINDS: dict[str, type[ProfileSpec]] = {
    Ramp.kind: Ramp,
    Approach.kind: Approach,
    Pulse.kind: Pulse,
}


def parse_profile_spec(text: str) -> ProfileSpec:
    """Parse a ``profile=`` comment value into a feedback spec.

    ``"ramp:up=0.8|down=-0.05"`` → ``Ramp(...)``,
    ``"approach:toward=180|rate=0.3"`` → ``Approach(...)``,
    ``"pulse:on_dwell=0.5s|off_dwell=0.5s"`` → ``Pulse(...)``.  A value without a
    recognized ``<kind>:`` prefix raises — the legacy bare-name (Python
    ``@profile``) form is no longer supported.
    """
    kind, sep, params = text.partition(":")
    spec_cls = _SPEC_KINDS.get(kind.strip()) if sep == ":" else None
    if spec_cls is None:
        raise ValueError(
            f"Unknown profile spec {text!r}. Use a declarative spec such as "
            f"ramp:up=..|down=.., approach:toward=..|rate=.., or "
            f"pulse:on_dwell=..|off_dwell=.."
        )
    return spec_cls._from_params(params)


def profile_to_token(profile: ProfileSpec) -> str:
    """Serialize a feedback spec back to its comment-field value."""
    return profile.to_token()


@dataclass(frozen=True)
class Physical:
    """Declares physical feedback characteristics for a tag or field.

    Bool dwell feedback (has timing)::

        motor_fb = Physical("MotorFb", on_delay="2s", off_delay="500ms")

    Profile-driven feedback (has a declarative spec)::

        temp    = Physical("TempSensor", profile=Ramp(up=0.5, down=-0.05))
        oven    = Physical("Oven",       profile=Approach(toward=180.0, rate=0.3))
        encoder = Physical("Encoder",    profile=Pulse(on_dwell="8ms", off_dwell="8ms"))

    The ``system`` field groups related feedback for reporting.
    """

    name: str
    on_delay: str | None = None
    off_delay: str | None = None
    profile: ProfileSpec | None = None
    system: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Physical name must be non-empty")

        has_timing = self.on_delay is not None or self.off_delay is not None
        has_profile = self.profile is not None

        if has_timing and has_profile:
            raise ValueError(
                f"Physical '{self.name}' has both timing and profile; "
                f"a feedback is either bool (on_delay/off_delay) or "
                f"analog (profile), not both"
            )

        if not has_timing and not has_profile:
            raise ValueError(
                f"Physical '{self.name}' has neither timing nor profile; "
                f"provide on_delay/off_delay for bool feedback or "
                f"profile for analog feedback"
            )

        if self.on_delay is not None:
            parse_duration(self.on_delay)
        if self.off_delay is not None:
            parse_duration(self.off_delay)

    @property
    def feedback_type(self) -> FeedbackType:
        # Timing dwell and a Pulse profile are both Bool feedback; an analog
        # Ramp/Approach profile drives a Real register.
        if self.on_delay is not None or self.off_delay is not None:
            return "bool"
        if isinstance(self.profile, Pulse):
            return "bool"
        return "analog"

    @property
    def on_delay_ms(self) -> int | None:
        if self.on_delay is None:
            return None
        return parse_duration(self.on_delay)

    @property
    def off_delay_ms(self) -> int | None:
        if self.off_delay is None:
            return None
        return parse_duration(self.off_delay)
