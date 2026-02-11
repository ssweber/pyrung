"""Tests for CLICK-specific send/receive DSL instructions."""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from pyrung.core import Block, Bool, Int, PLCRunner, Program, Rung, TagType


def test_send_starts_reports_success_then_restarts(monkeypatch: pytest.MonkeyPatch):
    import pyrung.click.send_receive as click_send_receive

    submissions: list[tuple[dict[str, object], Future[click_send_receive._RequestResult]]] = []

    def fake_submit(**kwargs: object) -> Future[click_send_receive._RequestResult]:
        fut: Future[click_send_receive._RequestResult] = Future()
        submissions.append((kwargs, fut))
        return fut

    monkeypatch.setattr(click_send_receive, "_submit_send_request", fake_submit)

    Enable = Bool("Enable")
    Source = Int("Source")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")

    with Program() as logic:
        with Rung(Enable):
            click_send_receive.send(
                host="127.0.0.1",
                port=502,
                remote_start="DS1",
                source=Source,
                sending=Sending,
                success=Success,
                error=Error,
                exception_response=ExCode,
                device_id=17,
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Source": 123})
    runner.step()

    assert len(submissions) == 1
    assert submissions[0][0]["device_id"] == 17
    assert runner.current_state.tags["Sending"] is True
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ExCode"] == 0

    submissions[0][1].set_result(click_send_receive._RequestResult(ok=True, exception_code=0))
    runner.patch({"Enable": True})
    runner.step()

    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is True
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ExCode"] == 0

    # Enabled rung auto-restarts request on following scan.
    runner.patch({"Enable": True})
    runner.step()
    assert len(submissions) == 2
    assert runner.current_state.tags["Sending"] is True
    assert runner.current_state.tags["Success"] is False


def test_send_rung_false_discards_pending_result_and_clears_outputs(
    monkeypatch: pytest.MonkeyPatch,
):
    import pyrung.click.send_receive as click_send_receive

    class _UncancellableFuture(Future[click_send_receive._RequestResult]):
        def cancel(self) -> bool:
            return False

    futures: list[Future[click_send_receive._RequestResult]] = []

    def fake_submit(**kwargs: object) -> Future[click_send_receive._RequestResult]:
        _ = kwargs
        fut: Future[click_send_receive._RequestResult] = _UncancellableFuture()
        futures.append(fut)
        return fut

    monkeypatch.setattr(click_send_receive, "_submit_send_request", fake_submit)

    Enable = Bool("Enable")
    Source = Int("Source")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")

    with Program() as logic:
        with Rung(Enable):
            click_send_receive.send(
                host="127.0.0.1",
                port=502,
                remote_start="DS1",
                source=Source,
                sending=Sending,
                success=Success,
                error=Error,
                exception_response=ExCode,
                device_id=1,
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Source": 1})
    runner.step()
    assert runner.current_state.tags["Sending"] is True
    assert len(futures) == 1

    runner.patch({"Enable": False})
    runner.step()

    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ExCode"] == 0

    futures[0].set_result(click_send_receive._RequestResult(ok=True, exception_code=0))
    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ExCode"] == 0


def test_receive_applies_values_and_sets_success(monkeypatch: pytest.MonkeyPatch):
    import pyrung.click.send_receive as click_send_receive

    future: Future[click_send_receive._RequestResult] = Future()

    def fake_submit(**kwargs: object) -> Future[click_send_receive._RequestResult]:
        _ = kwargs
        return future

    monkeypatch.setattr(click_send_receive, "_submit_receive_request", fake_submit)

    Enable = Bool("Enable")
    Receiving = Bool("Receiving")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")
    Local = Block("Local", TagType.INT, 1, 2)

    with Program() as logic:
        with Rung(Enable):
            click_send_receive.receive(
                host="127.0.0.1",
                port=502,
                remote_start="DS1",
                dest=Local.select(1, 2),
                receiving=Receiving,
                success=Success,
                error=Error,
                exception_response=ExCode,
                device_id=3,
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Receiving"] is True

    future.set_result(click_send_receive._RequestResult(ok=True, exception_code=0, values=(11, 22)))
    runner.patch({"Enable": True})
    runner.step()

    assert runner.current_state.tags["Receiving"] is False
    assert runner.current_state.tags["Success"] is True
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ExCode"] == 0
    assert runner.current_state.tags["Local1"] == 11
    assert runner.current_state.tags["Local2"] == 22


def test_receive_error_sets_exception_code(monkeypatch: pytest.MonkeyPatch):
    import pyrung.click.send_receive as click_send_receive

    future: Future[click_send_receive._RequestResult] = Future()

    def fake_submit(**kwargs: object) -> Future[click_send_receive._RequestResult]:
        _ = kwargs
        return future

    monkeypatch.setattr(click_send_receive, "_submit_receive_request", fake_submit)

    Enable = Bool("Enable")
    Receiving = Bool("Receiving")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")
    Dest = Int("Dest")

    with Program() as logic:
        with Rung(Enable):
            click_send_receive.receive(
                host="127.0.0.1",
                port=502,
                remote_start="DS1",
                dest=Dest,
                receiving=Receiving,
                success=Success,
                error=Error,
                exception_response=ExCode,
                device_id=1,
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True})
    runner.step()

    future.set_result(click_send_receive._RequestResult(ok=False, exception_code=6))
    runner.patch({"Enable": True})
    runner.step()

    assert runner.current_state.tags["Receiving"] is False
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is True
    assert runner.current_state.tags["ExCode"] == 6


def test_send_count_mismatch_raises():
    import pyrung.click.send_receive as click_send_receive

    Enable = Bool("Enable")
    Source = Int("Source")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")

    with Program() as logic:
        with Rung(Enable):
            click_send_receive.send(
                host="127.0.0.1",
                port=502,
                remote_start="DS1",
                source=Source,
                sending=Sending,
                success=Success,
                error=Error,
                exception_response=ExCode,
                device_id=1,
                count=2,
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Source": 1})
    with pytest.raises(ValueError, match="count mismatch"):
        runner.step()


def test_receive_count_mismatch_raises():
    import pyrung.click.send_receive as click_send_receive

    Enable = Bool("Enable")
    Dest = Int("Dest")
    Receiving = Bool("Receiving")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")

    with Program() as logic:
        with Rung(Enable):
            click_send_receive.receive(
                host="127.0.0.1",
                port=502,
                remote_start="DS1",
                dest=Dest,
                receiving=Receiving,
                success=Success,
                error=Error,
                exception_response=ExCode,
                device_id=1,
                count=2,
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True})
    with pytest.raises(ValueError, match="count mismatch"):
        runner.step()


def test_receive_validates_status_tag_types():
    import pyrung.click.send_receive as click_send_receive

    Enable = Bool("Enable")
    Dest = Int("Dest")
    BadReceiving = Int("BadReceiving")
    Success = Bool("Success")
    Error = Bool("Error")
    ExCode = Int("ExCode")

    with Program():
        with Rung(Enable):
            with pytest.raises(TypeError, match="must be BOOL"):
                click_send_receive.receive(
                    host="127.0.0.1",
                    port=502,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=BadReceiving,
                    success=Success,
                    error=Error,
                    exception_response=ExCode,
                    device_id=1,
                )


def test_receive_validates_exception_response_type():
    import pyrung.click.send_receive as click_send_receive

    Enable = Bool("Enable")
    Dest = Int("Dest")
    Receiving = Bool("Receiving")
    Success = Bool("Success")
    Error = Bool("Error")
    BadExCode = Bool("BadExCode")

    with Program():
        with Rung(Enable):
            with pytest.raises(TypeError, match="must be INT or DINT"):
                click_send_receive.receive(
                    host="127.0.0.1",
                    port=502,
                    remote_start="DS1",
                    dest=Dest,
                    receiving=Receiving,
                    success=Success,
                    error=Error,
                    exception_response=BadExCode,
                    device_id=1,
                )


def test_run_send_request_sparse_writes_are_split_by_gap(monkeypatch: pytest.MonkeyPatch):
    import pyrung.click.send_receive as click_send_receive

    writes: list[tuple[str, object]] = []

    class _FakeAddr:
        async def write(self, address: str, payload: object) -> None:
            writes.append((address, payload))

    class _FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            _ = args, kwargs
            self.addr = _FakeAddr()

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            _ = exc_type, exc, tb

    monkeypatch.setattr(click_send_receive, "ClickClient", _FakeClient)

    addresses = click_send_receive._addresses_for_count("X", 1, 17)
    values = tuple(range(17))
    result = click_send_receive._run_send_request(
        "127.0.0.1",
        502,
        1,
        "X",
        tuple(addresses),
        values,
    )

    assert result.ok is True
    assert writes == [("X001", list(range(16))), ("X021", 16)]
