"""The Send and Receive family: send() and receive().

laddercodec's Send, Receive, ModbusTcpTarget, ModbusRtuTarget and ModbusAddress check them.
Neither the target nor the remote start is a tag. An omitted status bit is replaced by a
hidden tag. So the family overrides ``operands`` and ``call_text``.
"""

from __future__ import annotations

from typing import Any

from laddercodec.csv.ast import AfCall
from laddercodec.csv.token_parser import parse_af_token
from laddercodec.instructions.send_receive import parse_af_call

from pyrung.core.instruction.send_receive import ModbusTcpTarget, is_modbus_status_tag
from pyrung.core.tag import Tag
from pyrung.core.validation.render import Call, call_of, operand_name

from ..fields import Family, Operand, Unbuildable, _address_text, refusals_of, render_operand
from ..resolve import _ResolutionCache

_STATUS_BITS = ("success", "error", "exception_response")

# The argument of the pyrung call that holds each field of laddercodec's Send, Receive and
# target classes. The target holds its own fields, and the remote start holds the address
# and the function code.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    **{
        f"{target}.{name}": "target"
        for target, names in {
            "ModbusTcpTarget": ("name", "ip", "port", "device_id", "com_port"),
            "ModbusRtuTarget": ("name", "com_port", "device_id"),
        }.items()
        for name in names
    },
    "ModbusAddress.address": "remote_start",
    "ModbusAddress.register_type": "remote_start",
    **{
        f"{cls}.{name}": argument
        for cls, local, busy in (("Send", "source", "sending"), ("Receive", "dest", "receiving"))
        for name, argument in {
            "target": "target",
            "remote_start": "remote_start",
            local: local,
            "quantity": local,
            busy: busy,
            "success": "success",
            "error": "error",
            "exception_response": "exception_response",
            "word_swap": "word_swap",
            "function_code": "remote_start",
        }.items()
    },
}

# pyrung's advice for a refusal of a field, where the advice names pyrung's own calls or
# arguments.
_ADVICE: dict[tuple[str, str], str] = {}


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    return _ADVICE.get((code, field))


class _RemoteStart:
    """A refusal of the remote start, worded without the names of local tags.

    The remote start is an address on the remote device. A local tag that is mapped to the
    same CLICK address must not name it.
    """

    def __init__(self, refusal: Any, address: str) -> None:
        self._refusal = refusal
        self._address = address

    def __getattr__(self, name: str) -> Any:
        return getattr(self._refusal, name)

    def _bare(self, names: Any) -> dict[str, str]:
        return {key: value for key, value in names.items() if key != self._address}

    def message(self, names: Any) -> str:
        return self._refusal.message(self._bare(names))

    def short(self, names: Any) -> str:
        return self._refusal.short(self._bare(names))

    def detail(self, names: Any) -> str:
        return self._refusal.detail(self._bare(names))


def _names(instruction: Any) -> tuple[str, str]:
    """Return the keyword of the local operand and of the busy flag of the call."""
    if type(instruction).__name__ == "ModbusSendInstruction":
        return "source", "sending"
    return "dest", "receiving"


def _operands(instruction: Any) -> list[Operand]:
    """Return each argument of the call that holds a tag the author wrote."""
    local, busy = _names(instruction)
    word = "source" if local == "source" else "destination"
    operands = [Operand(local, word, ((local, getattr(instruction, local)),))]
    device_id = instruction.device_id
    if isinstance(device_id, Tag):
        operands.append(Operand("target", "device id", (("device_id", device_id),)))
    for name in (busy, *_STATUS_BITS):
        bit = getattr(instruction, name)
        # A status bit that the author left off holds a hidden tag, which CLICK never sees.
        if not is_modbus_status_tag(bit.name):
            operands.append(Operand(name, f"{name.replace('_', ' ')} flag", ((name, bit),)))
    return operands


def _target_text(instruction: Any) -> str:
    """Return the target as the author wrote it."""
    target = instruction.raw_target
    if target is None:
        return repr(instruction.target_name)
    extras: list[str] = []
    if isinstance(target, ModbusTcpTarget):
        text = f"ModbusTcpTarget({target.name!r}, {target.ip!r}"
        if target.port != 502:
            extras.append(f"port={target.port}")
    else:
        text = f"ModbusRtuTarget({target.name!r}"
    if isinstance(target.device_id, Tag):
        extras.append(f"device_id={operand_name(target.device_id)}")
    elif target.device_id != 1:
        extras.append(f"device_id={target.device_id}")
    default_port = "ethernet" if isinstance(target, ModbusTcpTarget) else "cpu2"
    if target.com_port != default_port:
        extras.append(f"com_port={target.com_port!r}")
    return "".join([text, *(f", {extra}" for extra in extras), ")"])


def _remote_start_text(instruction: Any) -> str:
    """Return the remote start as the author wrote it."""
    address = instruction.remote_address
    if address is None:
        return repr(f"{instruction.bank}{instruction.start}")
    number = f"{address.address:X}h" if address.notation == "hex" else address.address
    return f"ModbusAddress({number!r}, RegisterType.{address.register_type.name})"


def _call(instruction: Any) -> Call:
    """Return the call as the author wrote it."""
    arguments = [
        ("target", _target_text(instruction), "keyword"),
        ("remote_start", _remote_start_text(instruction), "keyword"),
    ]
    for operand in _operands(instruction):
        if operand.argument == "target":
            continue
        ((_attribute, value),) = operand.values
        arguments.append((str(operand.argument), operand_name(value), "keyword"))
    if instruction.word_swap:
        arguments.append(("word_swap", "True", "keyword"))
    verb = "send" if _names(instruction)[0] == "source" else "receive"
    return call_of(verb, arguments)


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Send or Receive that the export writes.

    The export writes the call as a CSV token, and laddercodec reads the token. So this
    builds the same token and reads it the same way.
    """
    if instruction.raw_target is None:
        # A target that is only a name is a placeholder for code generation. The export
        # has no CLICK target to write for it.
        raise Unbuildable
    from pyrung.click.ladder.instructions import _render_remote_start
    from pyrung.click.ladder.types import _RenderError

    local, busy = _names(instruction)
    verb = "send" if local == "source" else "receive"
    try:
        kwargs = {
            "target": exporter._render_modbus_target(instruction, "instruction"),
            "remote_start": _render_remote_start(instruction),
            local: render_operand(exporter, getattr(instruction, local), local, instruction),
        }
    except _RenderError as exc:
        raise Unbuildable from exc
    for name in (busy, *_STATUS_BITS):
        bit = getattr(instruction, name)
        if not is_modbus_status_tag(bit.name):
            kwargs[name] = render_operand(exporter, bit, name, instruction)
    if instruction.word_swap:
        kwargs["word_swap"] = "1"
    call = parse_af_token(exporter._fn(verb, **kwargs))
    if not isinstance(call, AfCall):
        raise Unbuildable
    refusals = refusals_of(lambda: parse_af_call(call))
    address = _remote_address(instruction)
    return [
        _RemoteStart(refusal, address) if str(refusal.field).endswith(".remote_start") else refusal
        for refusal in refusals
    ]


def _remote_address(instruction: Any) -> str:
    """Return the remote start in CLICK's form, such as ``DS1``, as laddercodec names it."""
    if instruction.remote_address is None:
        return _address_text(f"{instruction.bank}{instruction.start}")
    return ""


FAMILY = Family(
    name="send_receive",
    classes={
        "ModbusSendInstruction": {
            "source": "source",
            "device_id": "device id",
            "sending": "sending flag",
            "success": "success flag",
            "error": "error flag",
            "exception_response": "exception response flag",
        },
        "ModbusReceiveInstruction": {
            "dest": "destination",
            "device_id": "device id",
            "receiving": "receiving flag",
            "success": "success flag",
            "error": "error flag",
            "exception_response": "exception response flag",
        },
    },
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    ranges={"ModbusSendInstruction": ("source",), "ModbusReceiveInstruction": ("dest",)},
    operands=_operands,
    call_text=_call,
)
