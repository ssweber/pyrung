"""The contact family: the contacts and compares of a rung, a branch or a pin.

laddercodec's Contact checks a contact, such as ``X1``, ``~C2`` or ``rise(C3)``. Its
CompareContact checks a compare, such as ``DS1 > 5``. Each leaf of a condition is one
contact. :mod:`pyrung.click.validation.conditions` finds the leaves, and validation asks
this family about each one.
"""

from __future__ import annotations

from typing import Any

from laddercodec import CompareContact, Contact

from pyrung.core.condition import CompareEq, CompareNe
from pyrung.core.tag import TagType
from pyrung.core.validation.render import Call, operand_name

from ..conditions import COMPARES, CONTACTS, INDIRECT_COMPARES, leaf_call
from ..fields import Family, Operand, Place, Unbuildable, default_place, memory_types, refusals_of
from ..resolve import _ResolutionCache

# Each field of laddercodec's classes, and the argument of the leaf's call that holds it.
# conditions.leaf_call names these arguments.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Contact.operand": "operand",
    "Contact.immediate": "contact",
    "CompareContact.op": "contact",
    "CompareContact.left": "left",
    "CompareContact.right": "right",
}

_ADVICE: dict[str, str] = {
    "immediate-contact-not-x": "Map it to an X input, or use a plain contact.",
    "text-order-compare": (
        "Compare text with == or !=, or copy the characters into Int tags "
        "(convert=to_ascii) and compare those."
    ),
}
_HEX_MIX_ADVICE = (
    "Copy the Word into a DS tag first and compare that, or compare it with a DH tag or a constant."
)
_TEXT_MIX_ADVICE = (
    "Compare text with text. To compare a character's code, copy the character into an "
    "Int tag first (convert=to_ascii)."
)


def _sides(leaf: Any) -> tuple[Any, Any]:
    """Return the left and the right side of a compare."""
    left = leaf.tag if isinstance(leaf, COMPARES) else leaf.indirect_ref
    return left, leaf.value


def _bit_contact(leaf: Any) -> str | None:
    """Return the contact that does what a compare of a bit does, such as ``~C2``.

    Return None for a compare that is not ``==`` or ``!=`` of a bit with True, False, 1 or 0.
    """
    if not isinstance(leaf, (CompareEq, CompareNe)) or leaf.tag.type is not TagType.BOOL:
        return None
    if type(leaf.value) not in (bool, int) or leaf.value not in (0, 1):
        return None
    closed = bool(leaf.value) == isinstance(leaf, CompareNe)
    name = operand_name(leaf.tag)
    return f"~{name}" if closed else name


def _advice(code: str, field: str, leaf: Any, resolver: _ResolutionCache) -> str | None:
    if code == "bank-not-allowed" and field == "CompareContact.left":
        contact = _bit_contact(leaf)
        if contact is not None:
            return f"Write the contact {contact} in place of the compare."
        return None
    if code == "compare-mixed-types":
        sides = _sides(leaf)
        if any(isinstance(side, str) or "TXT" in memory_types(side, resolver) for side in sides):
            return _TEXT_MIX_ADVICE
        return _HEX_MIX_ADVICE
    return _ADVICE.get(code)


def _build(leaf: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the cell that the export writes for *leaf*."""
    from pyrung.click.ladder.types import _RenderError

    try:
        token = exporter._condition_leaf_token(leaf, path="condition")
    except _RenderError as exc:
        # pyrung's own checks report a leaf that the export cannot write. An immediate
        # rise() or fall() gets CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED. A compare of two
        # texts that the CSV reads two ways gets invalid-text-constant.
        raise Unbuildable from exc
    parse = Contact if isinstance(leaf, CONTACTS) else CompareContact
    return refusals_of(lambda: parse.from_csv_token(token))


def _operands(leaf: Any) -> list[Operand]:
    if isinstance(leaf, CONTACTS):
        return [Operand("operand", "contact", (("tag", leaf.tag),))]
    left_attribute = "tag" if isinstance(leaf, COMPARES) else "indirect_ref"
    left, right = _sides(leaf)
    return [
        Operand("left", "comparison", ((left_attribute, left),)),
        Operand("right", "comparison", (("value", right),)),
    ]


def _call(leaf: Any) -> Call:
    return leaf_call(leaf)


def _place(refusal: Any) -> Place | None:
    # laddercodec names the left side when the two sides do not mix. The fault is the
    # pair, so the caret marks the whole compare.
    if refusal.code == "compare-mixed-types":
        return Place("contact")
    return default_place(refusal.field, _FIELD_ARGUMENTS)


_CONTACT_WORDS = {"tag": "contact"}
_COMPARE_WORDS = {"tag": "comparison", "value": "comparison"}
_INDIRECT_WORDS = {"indirect_ref": "comparison", "value": "comparison"}

FAMILY = Family(
    name="contacts",
    classes={
        **{cls.__name__: _CONTACT_WORDS for cls in CONTACTS},
        **{cls.__name__: _COMPARE_WORDS for cls in COMPARES},
        **{cls.__name__: _INDIRECT_WORDS for cls in INDIRECT_COMPARES},
    },
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    replaced_codes={
        "CLK_IMMEDIATE_CONTACT_MUST_BE_X": "immediate-contact-not-x",
        "CLK_TEXT_ORDER_COMPARE": "text-order-compare",
        "CLK_COMPARE_HEX_MIX": "compare-mixed-types",
    },
    operands=_operands,
    call_text=_call,
    place=_place,
)
