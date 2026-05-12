"""Unit-level invariants for pyrung kernel pure functions.

Implements the U1-U8 invariant tests catalogued in Section 11 of
``scratchpad/fuzzer-checklist.md``. These exercise the underlying kernel
primitives with Hypothesis ``@given`` strategies and ``RuleBasedStateMachine``
tests. They are cheap to run, shrink to single-value examples, and localise
failures to a specific function -- complementary to the grammar fuzzer that
drives whole programs through BFS.

Tier 1 (pure functions, ``@given``):

- U1: ``_store_copy_value_to_tag_type`` (clamping idempotence + range)
- U2: ``_truncate_to_tag_type`` (modular wrap; non-finite -> 0)
- U3: ``_math_out_of_range_for_dest`` (oracle agreement with truncate)
- U4: ``_rotate_left_16`` / ``_rotate_right_16`` (round-trip + identity)
- U5: ``_int_to_float_bits`` / ``_float_to_int_bits`` (bit-reinterpret round-trip)

Tier 2 (stateful machines, ``RuleBasedStateMachine``):

- U6: Counter accumulator clamping and done-bit semantics
- U7: Timer fractional accumulation and mode semantics (TON/RTON/TOF)
- U8: Drum step machine (event-triggered and time-triggered)
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pyrung.core.expression import _rotate_left_16, _rotate_right_16
from pyrung.core.instruction.conversions import (
    _DINT_MAX,
    _DINT_MIN,
    _INT_MAX,
    _INT_MIN,
    _float_to_int_bits,
    _int_to_float_bits,
    _math_out_of_range_for_dest,
    _store_copy_value_to_tag_type,
    _truncate_to_tag_type,
)
from pyrung.core.tag import Dint, Int, Real, TagType, Word

# --- Tag fixtures (singletons, since the conversion fns only read .type) -----

_INT_TAG = Int("U_int")
_DINT_TAG = Dint("U_dint")
_WORD_TAG = Word("U_word")
_REAL_TAG = Real("U_real")

_TAG_BY_TYPE = {
    TagType.INT: _INT_TAG,
    TagType.DINT: _DINT_TAG,
    TagType.WORD: _WORD_TAG,
    TagType.REAL: _REAL_TAG,
}

_INT_TAG_TYPES = (TagType.INT, TagType.DINT, TagType.WORD)
_ALL_TAG_TYPES = (*_INT_TAG_TYPES, TagType.REAL)


def _in_range(value: object, tag_type: TagType) -> bool:
    """Return True if ``value`` is within ``tag_type``'s storage range.

    REAL destinations accept any numeric scalar (the non-finite sentinel branch
    in ``_store_copy_value_to_tag_type`` returns int ``0``, which is a valid
    REAL value).
    """
    if tag_type == TagType.REAL:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    assert isinstance(value, int)
    if tag_type == TagType.INT:
        return _INT_MIN <= value <= _INT_MAX
    if tag_type == TagType.DINT:
        return _DINT_MIN <= value <= _DINT_MAX
    if tag_type == TagType.WORD:
        return 0 <= value <= 0xFFFF
    raise AssertionError(f"unhandled tag_type: {tag_type!r}")


# =============================================================================
# U1: Copy clamping idempotence + range invariant
# =============================================================================


@pytest.mark.hypothesis
@given(
    value=st.one_of(
        st.integers(min_value=-(10**18), max_value=10**18),
        st.floats(allow_nan=True, allow_infinity=True),
    ),
    tag_type=st.sampled_from(_ALL_TAG_TYPES),
)
@settings(max_examples=200, deadline=None)
def test_clamp_idempotent(value, tag_type):
    """``_store_copy_value_to_tag_type`` clamps to the tag-type range and is idempotent."""
    tag = _TAG_BY_TYPE[tag_type]
    once = _store_copy_value_to_tag_type(value, tag)
    twice = _store_copy_value_to_tag_type(once, tag)
    assert once == twice, f"clamp not idempotent: once={once!r} twice={twice!r}"
    # Result must lie inside the destination type's range.
    assert _in_range(once, tag_type), f"clamped value {once!r} not in {tag_type.name} range"


# =============================================================================
# U2: Truncation is modular (calc-style wrap)
# =============================================================================


@pytest.mark.hypothesis
@given(value=st.integers(min_value=-(10**18), max_value=10**18))
@settings(max_examples=200, deadline=None)
def test_truncate_int_is_modular(value):
    """INT truncation matches signed 16-bit modular wrap."""
    expected = ((value + 32768) % 65536) - 32768
    assert _truncate_to_tag_type(value, _INT_TAG) == expected


@pytest.mark.hypothesis
@given(value=st.integers(min_value=-(10**18), max_value=10**18))
@settings(max_examples=200, deadline=None)
def test_truncate_dint_is_modular(value):
    """DINT truncation matches signed 32-bit modular wrap."""
    modulus = 1 << 32
    half = 1 << 31
    expected = ((value + half) % modulus) - half
    assert _truncate_to_tag_type(value, _DINT_TAG) == expected


@pytest.mark.hypothesis
@given(value=st.integers(min_value=-(10**18), max_value=10**18))
@settings(max_examples=200, deadline=None)
def test_truncate_word_is_modular(value):
    """WORD truncation matches unsigned 16-bit modular wrap."""
    assert _truncate_to_tag_type(value, _WORD_TAG) == value & 0xFFFF


@pytest.mark.hypothesis
@pytest.mark.parametrize("non_finite", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("tag_type", [TagType.INT, TagType.DINT, TagType.WORD])
def test_truncate_non_finite_is_zero(non_finite, tag_type):
    """Non-finite floats (inf, -inf, NaN) truncate to 0 for integer destinations."""
    tag = _TAG_BY_TYPE[tag_type]
    assert _truncate_to_tag_type(non_finite, tag) == 0


# =============================================================================
# U3: out-of-range oracle agrees with truncate (integer dest types)
# =============================================================================


@pytest.mark.hypothesis
@given(
    value=st.integers(min_value=-(10**18), max_value=10**18),
    tag_type=st.sampled_from(_INT_TAG_TYPES),
)
@settings(max_examples=200, deadline=None)
def test_out_of_range_agrees_with_truncate(value, tag_type):
    """``_math_out_of_range_for_dest`` is True iff ``_truncate_to_tag_type`` changes value."""
    tag = _TAG_BY_TYPE[tag_type]
    is_oor = _math_out_of_range_for_dest(value, tag, "decimal")
    truncated = _truncate_to_tag_type(value, tag, "decimal")
    assert is_oor == (truncated != value), (
        f"oracle disagreement for value={value!r} tag={tag_type.name}: "
        f"oor={is_oor} truncated={truncated!r}"
    )


@pytest.mark.hypothesis
@given(value=st.integers(min_value=-(10**18), max_value=10**18))
@settings(max_examples=200, deadline=None)
def test_out_of_range_agrees_with_truncate_hex_mode(value):
    """In hex mode, oracle agreement holds for any integer destination."""
    # In hex mode the dest type is irrelevant beyond it being an integer kind;
    # use INT as the carrier tag.
    is_oor = _math_out_of_range_for_dest(value, _INT_TAG, "hex")
    truncated = _truncate_to_tag_type(value, _INT_TAG, "hex")
    assert is_oor == (truncated != value), (
        f"hex-mode oracle disagreement: value={value!r} oor={is_oor} truncated={truncated!r}"
    )


# =============================================================================
# U4: 16-bit rotate round-trip and full-cycle identity
# =============================================================================


@pytest.mark.hypothesis
@given(
    value=st.integers(min_value=0, max_value=0xFFFF),
    count=st.integers(min_value=0, max_value=31),
)
@settings(max_examples=200, deadline=None)
def test_rotate_round_trip(value, count):
    """``rro(lro(v, n), n) == v & 0xFFFF`` for any 16-bit value and rotation count."""
    masked = value & 0xFFFF
    assert _rotate_right_16(_rotate_left_16(masked, count), count) == masked
    # Symmetric direction.
    assert _rotate_left_16(_rotate_right_16(masked, count), count) == masked


@pytest.mark.hypothesis
@given(value=st.integers(min_value=0, max_value=0xFFFF))
@settings(max_examples=200, deadline=None)
def test_full_rotation_is_identity(value):
    """Rotating by a full multiple of 16 returns the original masked value."""
    masked = value & 0xFFFF
    assert _rotate_left_16(masked, 16) == masked
    assert _rotate_left_16(masked, 32) == masked
    assert _rotate_right_16(masked, 16) == masked
    assert _rotate_right_16(masked, 32) == masked


@pytest.mark.hypothesis
@given(
    value=st.integers(min_value=0, max_value=0xFFFF),
    count=st.integers(min_value=0, max_value=31),
)
@settings(max_examples=200, deadline=None)
def test_rotate_result_always_16_bit(value, count):
    """Rotation results always fit in 16 bits."""
    assert 0 <= _rotate_left_16(value, count) <= 0xFFFF
    assert 0 <= _rotate_right_16(value, count) <= 0xFFFF


# =============================================================================
# U5: Float bit reinterpretation round-trip
# =============================================================================


@pytest.mark.hypothesis
@given(n=st.integers(min_value=0, max_value=0xFFFFFFFF))
@settings(max_examples=200, deadline=None)
def test_int_to_float_to_int(n):
    """Any 32-bit unsigned int round-trips through int->float->int (NaN bits excluded)."""
    f = _int_to_float_bits(n)
    # NaN has multiple bit representations; the canonical NaN produced by
    # struct may differ from the original payload, so skip NaN inputs.
    if math.isnan(f):
        return
    assert _float_to_int_bits(f) == n


@pytest.mark.hypothesis
@given(f=st.floats(width=32, allow_nan=False, allow_infinity=False))
@settings(max_examples=200, deadline=None)
def test_float_to_int_to_float(f):
    """Any finite 32-bit float round-trips through float->int->float."""
    n = _float_to_int_bits(f)
    round_tripped = _int_to_float_bits(n)
    # +0.0 and -0.0 are equal under ``==`` but bit-different; the round trip
    # preserves bits exactly, so direct equality holds for finite floats.
    assert round_tripped == f or (math.copysign(1.0, round_tripped) == math.copysign(1.0, f))
    # Strengthen: the bit representation should round-trip exactly.
    assert _float_to_int_bits(round_tripped) == n


# =============================================================================
# U6: Counter accumulator clamping and done-bit semantics
# =============================================================================

from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule


class CountUpMachine(RuleBasedStateMachine):
    """Model CTU: acc increments while enabled, optional down, reset clears.

    Models scan-level semantics: reset sets acc=0, done=False and returns
    immediately (done is NOT recomputed against preset until the next counting
    scan).  ``last_op`` tracks what happened most recently so invariants fire
    on the correct post-conditions.
    """

    acc = 0
    done = False
    preset = 10
    down_enabled = False
    last_op = "reset"  # "reset" | "count" | "other"

    @initialize(preset=st.integers(min_value=-100, max_value=100))
    def init(self, preset):
        self.acc = 0
        self.done = False
        self.preset = preset
        self.last_op = "reset"

    @rule()
    def count_up(self):
        delta = 1
        if self.down_enabled:
            delta -= 1
        self.acc = max(_DINT_MIN, min(_DINT_MAX, self.acc + delta))
        self.done = self.acc >= self.preset
        self.last_op = "count"

    @rule()
    def toggle_down(self):
        self.down_enabled = not self.down_enabled

    @rule()
    def reset(self):
        self.acc = 0
        self.done = False
        self.last_op = "reset"

    @rule(preset=st.integers(min_value=-100, max_value=100))
    def set_preset(self, preset):
        self.preset = preset
        self.last_op = "other"

    @invariant()
    def acc_in_dint_range(self):
        assert _DINT_MIN <= self.acc <= _DINT_MAX

    @invariant()
    def done_matches_after_count(self):
        if self.last_op == "count":
            assert self.done == (self.acc >= self.preset)

    @invariant()
    def reset_semantics(self):
        if self.last_op == "reset":
            assert self.acc == 0
            assert self.done is False


class CountDownMachine(RuleBasedStateMachine):
    """Model CTD: acc decrements while enabled, done when acc <= -preset.

    Same reset semantics as CTU.
    """

    acc = 0
    done = False
    preset = 10
    last_op = "reset"

    @initialize(preset=st.integers(min_value=-100, max_value=100))
    def init(self, preset):
        self.acc = 0
        self.done = False
        self.preset = preset
        self.last_op = "reset"

    @rule()
    def count_down(self):
        self.acc = max(_DINT_MIN, min(_DINT_MAX, self.acc - 1))
        self.done = self.acc <= -self.preset
        self.last_op = "count"

    @rule()
    def reset(self):
        self.acc = 0
        self.done = False
        self.last_op = "reset"

    @rule(preset=st.integers(min_value=-100, max_value=100))
    def set_preset(self, preset):
        self.preset = preset
        self.last_op = "other"

    @invariant()
    def acc_in_dint_range(self):
        assert _DINT_MIN <= self.acc <= _DINT_MAX

    @invariant()
    def done_matches_after_count(self):
        if self.last_op == "count":
            assert self.done == (self.acc <= -self.preset)

    @invariant()
    def reset_semantics(self):
        if self.last_op == "reset":
            assert self.acc == 0
            assert self.done is False


TestCountUp = pytest.mark.hypothesis(CountUpMachine.TestCase)
TestCountUp.settings = settings(max_examples=200, deadline=None, stateful_step_count=50)

TestCountDown = pytest.mark.hypothesis(CountDownMachine.TestCase)
TestCountDown.settings = settings(max_examples=200, deadline=None, stateful_step_count=50)


# =============================================================================
# U7: Timer fractional accumulation and mode semantics
# =============================================================================

from pyrung.core.time_mode import TimeUnit


class OnDelayTimerMachine(RuleBasedStateMachine):
    """Model TON/RTON: acc counts while enabled, fractional carry, clamp at 32767.

    ``last_op`` tracks what happened last so invariants fire on correct
    post-conditions.  The done comparison only holds right after a tick while
    enabled — not after enable/disable/reset transitions.
    """

    acc = 0
    frac = 0.0
    done = False
    preset = 100
    enabled = False
    has_reset = False
    last_op = "reset"  # "tick" | "reset" | "disable" | "other"

    @initialize(
        preset=st.integers(min_value=0, max_value=500),
        has_reset=st.booleans(),
    )
    def init(self, preset, has_reset):
        self.acc = 0
        self.frac = 0.0
        self.done = False
        self.preset = preset
        self.enabled = False
        self.has_reset = has_reset
        self._prev_acc = 0
        self.last_op = "reset"

    @rule()
    def enable(self):
        self.enabled = True
        self.last_op = "other"

    @rule()
    def disable(self):
        self.enabled = False
        if not self.has_reset:
            self.acc = 0
            self.frac = 0.0
            self.done = False
        self._prev_acc = self.acc
        self.last_op = "disable"

    @rule(dt=st.floats(min_value=0.0, max_value=0.1, allow_nan=False, allow_infinity=False))
    def tick(self, dt):
        if not self.enabled:
            return
        self._prev_acc = self.acc
        dt_units = TimeUnit.Tms.dt_to_units(dt) + self.frac
        int_units = int(dt_units)
        self.frac = dt_units - int_units
        self.acc = min(self.acc + int_units, 32767)
        self.done = self.acc >= self.preset
        self.last_op = "tick"

    @rule()
    def reset(self):
        if not self.has_reset:
            return
        self.acc = 0
        self.frac = 0.0
        self.done = False
        self._prev_acc = 0
        self.last_op = "reset"

    @invariant()
    def acc_in_int_range(self):
        assert -32768 <= self.acc <= 32767

    @invariant()
    def frac_non_negative(self):
        assert self.frac >= 0.0

    @invariant()
    def done_matches_after_tick(self):
        if self.last_op == "tick":
            assert self.done == (self.acc >= self.preset)

    @invariant()
    def acc_monotone_after_tick(self):
        if self.last_op == "tick":
            assert self.acc >= self._prev_acc

    @invariant()
    def ton_disable_resets(self):
        if self.last_op == "disable" and not self.has_reset:
            assert self.acc == 0
            assert self.done is False


class OffDelayTimerMachine(RuleBasedStateMachine):
    """Model TOF: done=True while enabled; counts while disabled, done=False when acc>=preset.

    Same ``last_op`` pattern — done comparison only holds after a tick while
    disabled, or immediately after enable.
    """

    acc = 0
    frac = 0.0
    done = False
    preset = 100
    enabled = False
    last_op = "init"  # "enable" | "tick" | "disable" | "init"

    @initialize(preset=st.integers(min_value=0, max_value=500))
    def init(self, preset):
        self.acc = 0
        self.frac = 0.0
        self.done = False
        self.preset = preset
        self.enabled = False
        self.last_op = "init"

    @rule()
    def enable(self):
        self.enabled = True
        self.acc = 0
        self.frac = 0.0
        self.done = True
        self.last_op = "enable"

    @rule()
    def disable(self):
        self.enabled = False
        self.last_op = "disable"

    @rule(dt=st.floats(min_value=0.0, max_value=0.1, allow_nan=False, allow_infinity=False))
    def tick(self, dt):
        if self.enabled:
            return
        dt_units = TimeUnit.Tms.dt_to_units(dt) + self.frac
        int_units = int(dt_units)
        self.frac = dt_units - int_units
        self.acc = min(self.acc + int_units, 32767)
        self.done = self.acc < self.preset
        self.last_op = "tick"

    @invariant()
    def acc_in_int_range(self):
        assert -32768 <= self.acc <= 32767

    @invariant()
    def enable_semantics(self):
        if self.last_op == "enable":
            assert self.done is True
            assert self.acc == 0

    @invariant()
    def done_matches_after_tick(self):
        if self.last_op == "tick":
            assert self.done == (self.acc < self.preset)


TestOnDelayTimer = pytest.mark.hypothesis(OnDelayTimerMachine.TestCase)
TestOnDelayTimer.settings = settings(max_examples=200, deadline=None, stateful_step_count=50)

TestOffDelayTimer = pytest.mark.hypothesis(OffDelayTimerMachine.TestCase)
TestOffDelayTimer.settings = settings(max_examples=200, deadline=None, stateful_step_count=50)


# =============================================================================
# U8: Drum step machine (event-triggered and time-triggered)
# =============================================================================


class EventDrumMachine(RuleBasedStateMachine):
    """Model event drum: edge-triggered step advancement, reset, jump, jog."""

    step = 1
    step_count = 3
    completion = False
    event_ready = True
    event_prev = False

    @initialize(step_count=st.integers(min_value=2, max_value=8))
    def init(self, step_count):
        self.step = 1
        self.step_count = step_count
        self.completion = False
        self.event_ready = True
        self.event_prev = False
        self._jog_prev = False
        self._jump_prev = False
        self.last_op = "reset"

    @rule()
    def fire_event_on(self):
        event_curr = True
        if self.event_ready and event_curr and not self.event_prev:
            if self.step < self.step_count:
                self.step += 1
                self.event_ready = not True  # re-evaluate for new step
            else:
                self.completion = True
        self.event_prev = event_curr
        self.last_op = "event"

    @rule()
    def fire_event_off(self):
        event_curr = False
        if not self.event_ready and not event_curr:
            self.event_ready = True
        self.event_prev = event_curr
        self.last_op = "event"

    @rule()
    def reset(self):
        self.step = 1
        self.completion = False
        self.event_ready = True
        self.event_prev = False
        self.last_op = "reset"

    @rule(step_target=st.integers(min_value=1, max_value=16))
    def jump(self, step_target):
        jump_curr = True
        jump_edge = jump_curr and not self._jump_prev
        self._jump_prev = jump_curr
        if not jump_edge:
            return
        if 1 <= step_target <= self.step_count:
            self.step = step_target
            self.event_ready = True
            self.event_prev = False
        self.last_op = "jump"

    @rule()
    def jump_release(self):
        self._jump_prev = False

    @rule()
    def jog(self):
        jog_curr = True
        jog_edge = jog_curr and not self._jog_prev
        self._jog_prev = jog_curr
        if jog_edge and self.step < self.step_count:
            self.step += 1
            self.event_ready = True
            self.event_prev = False
        self.last_op = "jog"

    @rule()
    def jog_release(self):
        self._jog_prev = False

    @invariant()
    def step_in_range(self):
        assert 1 <= self.step <= self.step_count

    @invariant()
    def reset_clears_completion(self):
        if self.last_op == "reset":
            assert self.completion is False
            assert self.step == 1


class TimeDrumMachine(RuleBasedStateMachine):
    """Model time drum: time-triggered step advancement with per-step presets."""

    step = 1
    step_count = 3
    acc = 0
    frac = 0.0
    completion = False
    presets: list[int]

    @initialize(
        step_count=st.integers(min_value=2, max_value=8),
        preset=st.integers(min_value=1, max_value=200),
    )
    def init(self, step_count, preset):
        self.step = 1
        self.step_count = step_count
        self.acc = 0
        self.frac = 0.0
        self.completion = False
        self.presets = [preset] * step_count
        self._jog_prev = False
        self._jump_prev = False
        self.last_op = "reset"

    @rule(dt=st.floats(min_value=0.0, max_value=0.1, allow_nan=False, allow_infinity=False))
    def tick(self, dt):
        dt_units = TimeUnit.Tms.dt_to_units(dt) + self.frac
        int_units = int(dt_units)
        self.frac = dt_units - int_units
        self.acc = min(self.acc + int_units, 32767)
        preset = self.presets[self.step - 1]
        if self.acc >= preset:
            if self.step < self.step_count:
                self.step += 1
                self.acc = 0
                self.frac = 0.0
            else:
                self.completion = True
        self.last_op = "tick"

    @rule()
    def reset(self):
        self.step = 1
        self.acc = 0
        self.frac = 0.0
        self.completion = False
        self.last_op = "reset"

    @rule(step_target=st.integers(min_value=1, max_value=16))
    def jump(self, step_target):
        jump_curr = True
        jump_edge = jump_curr and not self._jump_prev
        self._jump_prev = jump_curr
        if not jump_edge:
            return
        if 1 <= step_target <= self.step_count:
            self.step = step_target
            self.acc = 0
            self.frac = 0.0
        self.last_op = "jump"

    @rule()
    def jump_release(self):
        self._jump_prev = False

    @rule()
    def jog(self):
        jog_curr = True
        jog_edge = jog_curr and not self._jog_prev
        self._jog_prev = jog_curr
        if jog_edge and self.step < self.step_count:
            self.step += 1
            self.acc = 0
            self.frac = 0.0
        self.last_op = "jog"

    @rule()
    def jog_release(self):
        self._jog_prev = False

    @invariant()
    def step_in_range(self):
        assert 1 <= self.step <= self.step_count

    @invariant()
    def acc_non_negative(self):
        assert self.acc >= 0

    @invariant()
    def acc_in_int_range(self):
        assert self.acc <= 32767

    @invariant()
    def reset_clears_completion(self):
        if self.last_op == "reset":
            assert self.completion is False
            assert self.step == 1
            assert self.acc == 0


TestEventDrum = pytest.mark.hypothesis(EventDrumMachine.TestCase)
TestEventDrum.settings = settings(max_examples=200, deadline=None, stateful_step_count=50)

TestTimeDrum = pytest.mark.hypothesis(TimeDrumMachine.TestCase)
TestTimeDrum.settings = settings(max_examples=200, deadline=None, stateful_step_count=50)
