"""Integration test for CLICK send/receive across two localhost soft PLCs."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass

import pytest
from pyclickplc.server import ClickServer

from pyrung.click import ClickDataProvider, TagMap, c, dd, df, dh, ds, receive, send, txt
from pyrung.core import (
    Block,
    Bool,
    Char,
    Dint,
    Int,
    PLCRunner,
    Program,
    Real,
    Rung,
    Tag,
    TagType,
    TimeMode,
    Word,
)

pytestmark = pytest.mark.integration

_EXCHANGE_TIMEOUT_SECONDS = 8.0
_OFFLINE_OBSERVATION_TIMEOUT_SECONDS = 1.5
_RECOVERY_TIMEOUT_SECONDS = 8.0


async def _yield_event_loop() -> None:
    await asyncio.sleep(0)
    await asyncio.to_thread(lambda: None)


@dataclass(frozen=True)
class _StatusTags:
    busy: Tag
    success: Tag
    error: Tag
    exception: Tag


@dataclass(frozen=True)
class _NodeConfig:
    program: Program
    mapping: TagMap
    initial_patch: dict[str, bool | int | float | str]
    success_tags: tuple[str, ...]
    error_tags: tuple[str, ...]


@dataclass(frozen=True)
class _OutageNodeAConfig:
    program: Program
    initial_patch: dict[str, bool | int | float | str]
    send_status: _StatusTags
    recv_status: _StatusTags
    recv_dest_tag: str
    send_value: int
    recv_sentinel: int


@dataclass(frozen=True)
class _OutageNodeBConfig:
    program: Program
    mapping: TagMap
    initial_patch: dict[str, bool | int | float | str]
    recv_sink_tag: str
    send_value: int


def _status(prefix: str, *, busy_kind: str) -> _StatusTags:
    return _StatusTags(
        busy=Bool(f"{prefix}_{busy_kind}"),
        success=Bool(f"{prefix}_Success"),
        error=Bool(f"{prefix}_Error"),
        exception=Int(f"{prefix}_Ex"),
    )


def _find_unused_port(*, exclude: set[int] | None = None) -> int:
    excluded = exclude or set()
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port not in excluded:
            return port


def _build_node_a(port_b: int) -> _NodeConfig:
    enable = Bool("A_Enable")
    a_bool_src = Block("A_BoolSrc", TagType.BOOL, 1, 3)
    a_int_src = Block("A_IntSrc", TagType.INT, 1, 2)
    a_dint_src = Dint("A_DintSrc")
    a_real_src = Real("A_RealSrc")
    a_word_src = Word("A_WordSrc")
    a_char_src = Char("A_CharSrc")

    a_bool_rx = Block("A_BoolRx", TagType.BOOL, 1, 2)
    a_int_rx = Block("A_IntRx", TagType.INT, 1, 2)
    a_bool_sink = Block("A_BoolSink", TagType.BOOL, 1, 2)
    a_int_sink = Block("A_IntSink", TagType.INT, 1, 2)

    statuses = {
        "send_bool": _status("A_SendBool", busy_kind="Sending"),
        "send_int": _status("A_SendInt", busy_kind="Sending"),
        "send_dint": _status("A_SendDint", busy_kind="Sending"),
        "send_real": _status("A_SendReal", busy_kind="Sending"),
        "send_word": _status("A_SendWord", busy_kind="Sending"),
        "send_char": _status("A_SendChar", busy_kind="Sending"),
        "recv_bool": _status("A_RecvBool", busy_kind="Receiving"),
        "recv_int": _status("A_RecvInt", busy_kind="Receiving"),
    }

    with Program() as logic:
        with Rung(enable):
            send(
                host="127.0.0.1",
                port=port_b,
                remote_start="C101",
                source=a_bool_src.select(1, 3),
                sending=statuses["send_bool"].busy,
                success=statuses["send_bool"].success,
                error=statuses["send_bool"].error,
                exception_response=statuses["send_bool"].exception,
            )
            send(
                host="127.0.0.1",
                port=port_b,
                remote_start="DS101",
                source=a_int_src.select(1, 2),
                sending=statuses["send_int"].busy,
                success=statuses["send_int"].success,
                error=statuses["send_int"].error,
                exception_response=statuses["send_int"].exception,
            )
            send(
                host="127.0.0.1",
                port=port_b,
                remote_start="DD101",
                source=a_dint_src,
                sending=statuses["send_dint"].busy,
                success=statuses["send_dint"].success,
                error=statuses["send_dint"].error,
                exception_response=statuses["send_dint"].exception,
            )
            send(
                host="127.0.0.1",
                port=port_b,
                remote_start="DF101",
                source=a_real_src,
                sending=statuses["send_real"].busy,
                success=statuses["send_real"].success,
                error=statuses["send_real"].error,
                exception_response=statuses["send_real"].exception,
            )
            send(
                host="127.0.0.1",
                port=port_b,
                remote_start="DH101",
                source=a_word_src,
                sending=statuses["send_word"].busy,
                success=statuses["send_word"].success,
                error=statuses["send_word"].error,
                exception_response=statuses["send_word"].exception,
            )
            send(
                host="127.0.0.1",
                port=port_b,
                remote_start="TXT101",
                source=a_char_src,
                sending=statuses["send_char"].busy,
                success=statuses["send_char"].success,
                error=statuses["send_char"].error,
                exception_response=statuses["send_char"].exception,
            )
            receive(
                host="127.0.0.1",
                port=port_b,
                remote_start="C201",
                dest=a_bool_rx.select(1, 2),
                receiving=statuses["recv_bool"].busy,
                success=statuses["recv_bool"].success,
                error=statuses["recv_bool"].error,
                exception_response=statuses["recv_bool"].exception,
            )
            receive(
                host="127.0.0.1",
                port=port_b,
                remote_start="DS201",
                dest=a_int_rx.select(1, 2),
                receiving=statuses["recv_int"].busy,
                success=statuses["recv_int"].success,
                error=statuses["recv_int"].error,
                exception_response=statuses["recv_int"].exception,
            )

    mapping = TagMap(
        [
            a_bool_src.map_to(c.select(1, 3)),
            a_int_src.map_to(ds.select(1, 2)),
            a_dint_src.map_to(dd[1]),
            a_real_src.map_to(df[1]),
            a_word_src.map_to(dh[1]),
            a_char_src.map_to(txt[1]),
            a_bool_sink.map_to(c.select(301, 302)),
            a_int_sink.map_to(ds.select(301, 302)),
        ]
    )

    return _NodeConfig(
        program=logic,
        mapping=mapping,
        initial_patch={
            "A_Enable": True,
            "A_BoolSrc1": True,
            "A_BoolSrc2": False,
            "A_BoolSrc3": True,
            "A_IntSrc1": 123,
            "A_IntSrc2": -45,
            "A_DintSrc": 123456789,
            "A_RealSrc": 12.5,
            "A_WordSrc": 0xBEEF,
            "A_CharSrc": "Z",
        },
        success_tags=tuple(status.success.name for status in statuses.values()),
        error_tags=tuple(status.error.name for status in statuses.values()),
    )


def _build_node_b(port_a: int) -> _NodeConfig:
    enable = Bool("B_Enable")
    b_bool_rx = Block("B_BoolRx", TagType.BOOL, 1, 3)
    b_int_rx = Block("B_IntRx", TagType.INT, 1, 2)
    b_dint_rx = Dint("B_DintRx")
    b_real_rx = Real("B_RealRx")
    b_word_rx = Word("B_WordRx")
    b_char_rx = Char("B_CharRx")

    b_bool_src = Block("B_BoolSrc", TagType.BOOL, 1, 2)
    b_int_src = Block("B_IntSrc", TagType.INT, 1, 2)

    b_bool_sink = Block("B_BoolSink", TagType.BOOL, 1, 3)
    b_int_sink = Block("B_IntSink", TagType.INT, 1, 2)
    b_dint_sink = Dint("B_DintSink")
    b_real_sink = Real("B_RealSink")
    b_word_sink = Word("B_WordSink")
    b_char_sink = Char("B_CharSink")

    statuses = {
        "recv_bool": _status("B_RecvBool", busy_kind="Receiving"),
        "recv_int": _status("B_RecvInt", busy_kind="Receiving"),
        "recv_dint": _status("B_RecvDint", busy_kind="Receiving"),
        "recv_real": _status("B_RecvReal", busy_kind="Receiving"),
        "recv_word": _status("B_RecvWord", busy_kind="Receiving"),
        "recv_char": _status("B_RecvChar", busy_kind="Receiving"),
        "send_bool": _status("B_SendBool", busy_kind="Sending"),
        "send_int": _status("B_SendInt", busy_kind="Sending"),
    }

    with Program() as logic:
        with Rung(enable):
            receive(
                host="127.0.0.1",
                port=port_a,
                remote_start="C1",
                dest=b_bool_rx.select(1, 3),
                receiving=statuses["recv_bool"].busy,
                success=statuses["recv_bool"].success,
                error=statuses["recv_bool"].error,
                exception_response=statuses["recv_bool"].exception,
            )
            receive(
                host="127.0.0.1",
                port=port_a,
                remote_start="DS1",
                dest=b_int_rx.select(1, 2),
                receiving=statuses["recv_int"].busy,
                success=statuses["recv_int"].success,
                error=statuses["recv_int"].error,
                exception_response=statuses["recv_int"].exception,
            )
            receive(
                host="127.0.0.1",
                port=port_a,
                remote_start="DD1",
                dest=b_dint_rx,
                receiving=statuses["recv_dint"].busy,
                success=statuses["recv_dint"].success,
                error=statuses["recv_dint"].error,
                exception_response=statuses["recv_dint"].exception,
            )
            receive(
                host="127.0.0.1",
                port=port_a,
                remote_start="DF1",
                dest=b_real_rx,
                receiving=statuses["recv_real"].busy,
                success=statuses["recv_real"].success,
                error=statuses["recv_real"].error,
                exception_response=statuses["recv_real"].exception,
            )
            receive(
                host="127.0.0.1",
                port=port_a,
                remote_start="DH1",
                dest=b_word_rx,
                receiving=statuses["recv_word"].busy,
                success=statuses["recv_word"].success,
                error=statuses["recv_word"].error,
                exception_response=statuses["recv_word"].exception,
            )
            receive(
                host="127.0.0.1",
                port=port_a,
                remote_start="TXT1",
                dest=b_char_rx,
                receiving=statuses["recv_char"].busy,
                success=statuses["recv_char"].success,
                error=statuses["recv_char"].error,
                exception_response=statuses["recv_char"].exception,
            )
            send(
                host="127.0.0.1",
                port=port_a,
                remote_start="C301",
                source=b_bool_src.select(1, 2),
                sending=statuses["send_bool"].busy,
                success=statuses["send_bool"].success,
                error=statuses["send_bool"].error,
                exception_response=statuses["send_bool"].exception,
            )
            send(
                host="127.0.0.1",
                port=port_a,
                remote_start="DS301",
                source=b_int_src.select(1, 2),
                sending=statuses["send_int"].busy,
                success=statuses["send_int"].success,
                error=statuses["send_int"].error,
                exception_response=statuses["send_int"].exception,
            )

    mapping = TagMap(
        [
            b_bool_src.map_to(c.select(201, 202)),
            b_int_src.map_to(ds.select(201, 202)),
            b_bool_sink.map_to(c.select(101, 103)),
            b_int_sink.map_to(ds.select(101, 102)),
            b_dint_sink.map_to(dd[101]),
            b_real_sink.map_to(df[101]),
            b_word_sink.map_to(dh[101]),
            b_char_sink.map_to(txt[101]),
        ]
    )

    return _NodeConfig(
        program=logic,
        mapping=mapping,
        initial_patch={
            "B_Enable": True,
            "B_BoolSrc1": False,
            "B_BoolSrc2": True,
            "B_IntSrc1": 77,
            "B_IntSrc2": -88,
        },
        success_tags=tuple(status.success.name for status in statuses.values()),
        error_tags=tuple(status.error.name for status in statuses.values()),
    )


def _float_matches(value: object, expected: float, *, epsilon: float = 1e-6) -> bool:
    if not isinstance(value, float):
        return False
    return abs(value - expected) <= epsilon


def _assert_status(
    tags: Mapping[str, object],
    status: _StatusTags,
    *,
    busy: bool,
    success: bool,
    error: bool,
    exception: int,
) -> None:
    assert tags.get(status.busy.name) is busy
    assert tags.get(status.success.name) is success
    assert tags.get(status.error.name) is error
    assert tags.get(status.exception.name) == exception


def _build_outage_node_a(port_b: int) -> _OutageNodeAConfig:
    enable = Bool("A_Enable")
    send_source = Int("A_SendSrc")
    recv_dest = Int("A_RecvDest")
    send_status = _status("A_OutageSend", busy_kind="Sending")
    recv_status = _status("A_OutageRecv", busy_kind="Receiving")

    send_value = 314
    recv_sentinel = -999

    with Program() as logic:
        with Rung(enable):
            send(
                host="127.0.0.1",
                port=port_b,
                remote_start="DS101",
                source=send_source,
                sending=send_status.busy,
                success=send_status.success,
                error=send_status.error,
                exception_response=send_status.exception,
            )
            receive(
                host="127.0.0.1",
                port=port_b,
                remote_start="DS201",
                dest=recv_dest,
                receiving=recv_status.busy,
                success=recv_status.success,
                error=recv_status.error,
                exception_response=recv_status.exception,
            )

    return _OutageNodeAConfig(
        program=logic,
        initial_patch={
            "A_Enable": True,
            "A_SendSrc": send_value,
            "A_RecvDest": recv_sentinel,
        },
        send_status=send_status,
        recv_status=recv_status,
        recv_dest_tag=recv_dest.name,
        send_value=send_value,
        recv_sentinel=recv_sentinel,
    )


def _build_outage_node_b() -> _OutageNodeBConfig:
    recv_sink = Int("B_RecvSink")
    send_source = Int("B_SendSrc")
    send_value = 678

    with Program() as logic:
        pass

    mapping = TagMap([recv_sink.map_to(ds[101]), send_source.map_to(ds[201])])
    return _OutageNodeBConfig(
        program=logic,
        mapping=mapping,
        initial_patch={"B_SendSrc": send_value},
        recv_sink_tag=recv_sink.name,
        send_value=send_value,
    )


async def _run_two_node_exchange() -> list[str]:
    port_a = _find_unused_port()
    port_b = _find_unused_port(exclude={port_a})

    node_a = _build_node_a(port_b)
    node_b = _build_node_b(port_a)

    runner_a = PLCRunner(logic=node_a.program)
    runner_b = PLCRunner(logic=node_b.program)
    runner_a.set_time_mode(TimeMode.FIXED_STEP, dt=0.01)
    runner_b.set_time_mode(TimeMode.FIXED_STEP, dt=0.01)

    server_a = ClickServer(
        ClickDataProvider(runner=runner_a, tag_map=node_a.mapping),
        host="127.0.0.1",
        port=port_a,
    )
    server_b = ClickServer(
        ClickDataProvider(runner=runner_b, tag_map=node_b.mapping),
        host="127.0.0.1",
        port=port_b,
    )

    await server_a.start()
    await server_b.start()

    logs: list[str] = [f"servers started on ports A={port_a}, B={port_b}"]
    seen_a_success: set[str] = set()
    seen_b_success: set[str] = set()
    error_snapshots: list[str] = []
    seen_data = {
        "b_sink_from_a_send": False,
        "b_receive_from_a": False,
        "a_sink_from_b_send": False,
        "a_receive_from_b": False,
    }

    try:
        runner_a.patch(node_a.initial_patch)
        runner_b.patch(node_b.initial_patch)

        deadline = time.monotonic() + _EXCHANGE_TIMEOUT_SECONDS
        scan = 0
        while time.monotonic() < deadline:
            scan += 1
            runner_a.step()
            runner_b.step()

            tags_a = runner_a.current_state.tags
            tags_b = runner_b.current_state.tags

            seen_a_success.update(name for name in node_a.success_tags if tags_a.get(name, False))
            seen_b_success.update(name for name in node_b.success_tags if tags_b.get(name, False))

            a_errors = [name for name in node_a.error_tags if tags_a.get(name, False)]
            b_errors = [name for name in node_b.error_tags if tags_b.get(name, False)]
            if a_errors and len(error_snapshots) < 8:
                error_snapshots.append(f"scan {scan} A errors: {a_errors}")
            if b_errors and len(error_snapshots) < 8:
                error_snapshots.append(f"scan {scan} B errors: {b_errors}")

            if (
                not seen_data["b_sink_from_a_send"]
                and tags_b.get("B_BoolSink1") is True
                and tags_b.get("B_BoolSink2") is False
                and tags_b.get("B_BoolSink3") is True
                and tags_b.get("B_IntSink1") == 123
                and tags_b.get("B_IntSink2") == -45
                and tags_b.get("B_DintSink") == 123456789
                and _float_matches(tags_b.get("B_RealSink"), 12.5)
                and tags_b.get("B_WordSink") == 0xBEEF
                and tags_b.get("B_CharSink") == "Z"
            ):
                seen_data["b_sink_from_a_send"] = True
                logs.append(f"scan {scan}: A send() updated B mapped sinks for all datatypes")

            if (
                not seen_data["b_receive_from_a"]
                and tags_b.get("B_BoolRx1") is True
                and tags_b.get("B_BoolRx2") is False
                and tags_b.get("B_BoolRx3") is True
                and tags_b.get("B_IntRx1") == 123
                and tags_b.get("B_IntRx2") == -45
                and tags_b.get("B_DintRx") == 123456789
                and _float_matches(tags_b.get("B_RealRx"), 12.5)
                and tags_b.get("B_WordRx") == 0xBEEF
                and tags_b.get("B_CharRx") == "Z"
            ):
                seen_data["b_receive_from_a"] = True
                logs.append(f"scan {scan}: B receive() captured all datatypes from A")

            if (
                not seen_data["a_sink_from_b_send"]
                and tags_a.get("A_BoolSink1") is False
                and tags_a.get("A_BoolSink2") is True
                and tags_a.get("A_IntSink1") == 77
                and tags_a.get("A_IntSink2") == -88
            ):
                seen_data["a_sink_from_b_send"] = True
                logs.append(f"scan {scan}: B send() updated A mapped sinks")

            if (
                not seen_data["a_receive_from_b"]
                and tags_a.get("A_BoolRx1") is False
                and tags_a.get("A_BoolRx2") is True
                and tags_a.get("A_IntRx1") == 77
                and tags_a.get("A_IntRx2") == -88
            ):
                seen_data["a_receive_from_b"] = True
                logs.append(f"scan {scan}: A receive() captured reverse data from B")

            if (
                len(seen_a_success) == len(node_a.success_tags)
                and len(seen_b_success) == len(node_b.success_tags)
                and all(seen_data.values())
                and not a_errors
                and not b_errors
            ):
                logs.append(f"complete after {scan} scans")
                return logs

            await _yield_event_loop()

        missing_a = sorted(set(node_a.success_tags) - seen_a_success)
        missing_b = sorted(set(node_b.success_tags) - seen_b_success)
        missing_data = [name for name, ready in seen_data.items() if not ready]
        final_a = dict(runner_a.current_state.tags)
        final_b = dict(runner_b.current_state.tags)
        sink_snapshot = {
            "B_BoolSink": (
                final_b.get("B_BoolSink1"),
                final_b.get("B_BoolSink2"),
                final_b.get("B_BoolSink3"),
            ),
            "B_IntSink": (final_b.get("B_IntSink1"), final_b.get("B_IntSink2")),
            "B_DintSink": final_b.get("B_DintSink"),
            "B_RealSink": final_b.get("B_RealSink"),
            "B_WordSink": final_b.get("B_WordSink"),
            "B_CharSink": final_b.get("B_CharSink"),
            "A_BoolSink": (final_a.get("A_BoolSink1"), final_a.get("A_BoolSink2")),
            "A_IntSink": (final_a.get("A_IntSink1"), final_a.get("A_IntSink2")),
        }
        raise AssertionError(
            "Two-node CLICK exchange did not converge.\n"
            f"missing A success tags: {missing_a}\n"
            f"missing B success tags: {missing_b}\n"
            f"missing data checks: {missing_data}\n"
            f"errors: {error_snapshots}\n"
            f"sink snapshot: {sink_snapshot}\n"
            f"logs: {logs}"
        )
    finally:
        runner_a.patch({"A_Enable": False})
        runner_b.patch({"B_Enable": False})
        for _ in range(3):
            runner_a.step()
            runner_b.step()
            await _yield_event_loop()
        await server_b.stop()
        await server_a.stop()


def test_two_local_click_programs_exchange_send_receive_across_datatypes():
    logs = asyncio.run(_run_two_node_exchange())
    assert logs


async def _run_transient_peer_outage_auto_recovery() -> list[str]:
    port_b = _find_unused_port()
    node_a = _build_outage_node_a(port_b)
    node_b = _build_outage_node_b()

    runner_a = PLCRunner(logic=node_a.program)
    runner_a.set_time_mode(TimeMode.FIXED_STEP, dt=0.01)
    runner_b = PLCRunner(logic=node_b.program)
    runner_b.set_time_mode(TimeMode.FIXED_STEP, dt=0.01)

    logs: list[str] = [f"node A started with B offline on port {port_b}"]
    scan = 0
    server_b: ClickServer | None = None

    try:
        runner_a.patch(node_a.initial_patch)

        scan += 1
        runner_a.step()
        tags_a = runner_a.current_state.tags
        _assert_status(
            tags_a,
            node_a.send_status,
            busy=True,
            success=False,
            error=False,
            exception=0,
        )
        _assert_status(
            tags_a,
            node_a.recv_status,
            busy=True,
            success=False,
            error=False,
            exception=0,
        )
        assert tags_a.get(node_a.recv_dest_tag) == node_a.recv_sentinel
        logs.append(f"scan {scan}: requests submitted while B offline")

        observed_offline_failure = False
        observed_inflight_retry = False
        offline_observation_deadline = time.monotonic() + _OFFLINE_OBSERVATION_TIMEOUT_SECONDS
        while time.monotonic() < offline_observation_deadline:
            scan += 1
            runner_a.step()
            tags_a = runner_a.current_state.tags
            assert tags_a.get(node_a.recv_dest_tag) == node_a.recv_sentinel
            assert tags_a.get(node_a.send_status.success.name) is False
            assert tags_a.get(node_a.recv_status.success.name) is False
            send_failed = (
                tags_a.get(node_a.send_status.busy.name) is False
                and tags_a.get(node_a.send_status.success.name) is False
                and tags_a.get(node_a.send_status.error.name) is True
            )
            recv_failed = (
                tags_a.get(node_a.recv_status.busy.name) is False
                and tags_a.get(node_a.recv_status.success.name) is False
                and tags_a.get(node_a.recv_status.error.name) is True
            )
            send_inflight = (
                tags_a.get(node_a.send_status.busy.name) is True
                and tags_a.get(node_a.send_status.success.name) is False
                and tags_a.get(node_a.send_status.error.name) is False
            )
            recv_inflight = (
                tags_a.get(node_a.recv_status.busy.name) is True
                and tags_a.get(node_a.recv_status.success.name) is False
                and tags_a.get(node_a.recv_status.error.name) is False
            )

            if send_failed and recv_failed:
                observed_offline_failure = True
                _assert_status(
                    tags_a,
                    node_a.send_status,
                    busy=False,
                    success=False,
                    error=True,
                    exception=0,
                )
                _assert_status(
                    tags_a,
                    node_a.recv_status,
                    busy=False,
                    success=False,
                    error=True,
                    exception=0,
                )
                logs.append(f"scan {scan}: offline failure latched with exception_response=0")
            if send_inflight and recv_inflight:
                observed_inflight_retry = True
            await _yield_event_loop()
        if not (observed_offline_failure or observed_inflight_retry):
            raise AssertionError(
                "Did not observe a valid offline request state before timeout.\n"
                f"final tags: {dict(runner_a.current_state.tags)}\n"
                f"logs: {logs}"
            )
        if observed_inflight_retry and not observed_offline_failure:
            logs.append("offline state stayed inflight/retrying without latching an error")

        scan += 1
        runner_a.step()
        tags_a = runner_a.current_state.tags
        assert tags_a.get(node_a.send_status.success.name) is False
        assert tags_a.get(node_a.recv_status.success.name) is False
        assert tags_a.get(node_a.recv_dest_tag) == node_a.recv_sentinel
        logs.append(f"scan {scan}: requests remained active without toggling A_Enable")

        runner_b.patch(node_b.initial_patch)
        runner_b.step()
        server_b = ClickServer(
            ClickDataProvider(runner=runner_b, tag_map=node_b.mapping),
            host="127.0.0.1",
            port=port_b,
        )
        await server_b.start()
        logs.append("node B started without toggling A_Enable")

        send_success_seen = False
        recv_success_seen = False
        recovery_deadline = time.monotonic() + _RECOVERY_TIMEOUT_SECONDS
        while time.monotonic() < recovery_deadline:
            scan += 1
            runner_a.step()
            runner_b.step()
            tags_a = runner_a.current_state.tags
            tags_b = runner_b.current_state.tags

            if (
                tags_a.get(node_a.send_status.success.name) is True
                and tags_a.get(node_a.send_status.error.name) is False
                and tags_a.get(node_a.send_status.exception.name) == 0
            ):
                send_success_seen = True
            if (
                tags_a.get(node_a.recv_status.success.name) is True
                and tags_a.get(node_a.recv_status.error.name) is False
                and tags_a.get(node_a.recv_status.exception.name) == 0
            ):
                recv_success_seen = True

            if (
                tags_b.get(node_b.recv_sink_tag) == node_a.send_value
                and tags_a.get(node_a.recv_dest_tag) == node_b.send_value
                and send_success_seen
                and recv_success_seen
            ):
                logs.append(
                    f"scan {scan}: recovered with B_RecvSink={node_a.send_value}, "
                    f"A_RecvDest={node_b.send_value}"
                )
                return logs
            await _yield_event_loop()

        raise AssertionError(
            "Did not auto-recover after B came online.\n"
            f"A tags: {dict(runner_a.current_state.tags)}\n"
            f"B tags: {dict(runner_b.current_state.tags)}\n"
            f"logs: {logs}"
        )
    finally:
        runner_a.patch({"A_Enable": False})
        for _ in range(3):
            runner_a.step()
            runner_b.step()
            await _yield_event_loop()
        if server_b is not None:
            await server_b.stop()


def test_transient_peer_outage_auto_recovers_without_manual_reenable():
    logs = asyncio.run(_run_transient_peer_outage_auto_recovery())
    assert logs
