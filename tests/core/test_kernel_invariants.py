"""Tier 1 unit-level invariants for pyrung kernel pure functions.

Implements the U1-U5 invariant tests catalogued in Section 11 of
``scratchpad/fuzzer-checklist.md``. These exercise the underlying kernel
primitives with Hypothesis ``@given`` strategies. They are cheap to run,
shrink to single-value examples, and localise failures to a specific
function -- complementary to the grammar fuzzer that drives whole programs
through BFS.

Targets:

- U1: ``_store_copy_value_to_tag_type`` (clamping idempotence + range)
- U2: ``_truncate_to_tag_type`` (modular wrap; non-finite -> 0)
- U3: ``_math_out_of_range_for_dest`` (oracle agreement with truncate)
- U4: ``_rotate_left_16`` / ``_rotate_right_16`` (round-trip + identity)
- U5: ``_int_to_float_bits`` / ``_float_to_int_bits`` (bit-reinterpret round-trip)
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
