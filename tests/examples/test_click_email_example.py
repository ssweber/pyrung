"""Tests for click_email run_enabled_function example."""

from __future__ import annotations

from concurrent.futures import Future

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, run_enabled_function


def test_click_email_scan_state_machine(monkeypatch: pytest.MonkeyPatch):
    import pyrung.examples.click_email as click_email

    submissions: list[dict[str, object]] = []
    futures: list[Future[click_email._EmailResult]] = []

    def fake_submit(**kwargs: object) -> Future[click_email._EmailResult]:
        fut: Future[click_email._EmailResult] = Future()
        submissions.append(dict(kwargs))
        futures.append(fut)
        return fut

    monkeypatch.setattr(click_email, "_submit_email_request", fake_submit)

    Enable = Bool("Enable")
    Subject = Int("Subject")
    Body = Int("Body")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")
    ErrorCode = Int("ErrorCode")
    email = click_email.EmailInstruction(
        smtp_host="127.0.0.1",
        smtp_port=25,
        recipients=("test@example.com",),
    )

    with Program() as logic:
        with Rung(Enable):
            run_enabled_function(
                email,
                ins={"subject": Subject, "body": Body},
                outs={
                    "sending": Sending,
                    "success": Success,
                    "error": Error,
                    "error_code": ErrorCode,
                },
            )

    runner = PLCRunner(logic=logic)
    runner.patch(
        {
            "Enable": True,
            "Subject": 123,
            "Body": 456,
            "Sending": False,
            "Success": False,
            "Error": False,
            "ErrorCode": 0,
        }
    )
    runner.step()

    assert len(submissions) == 1
    assert runner.current_state.tags["Sending"] is True
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ErrorCode"] == 0

    runner.patch({"Enable": True})
    runner.step()

    assert len(submissions) == 1
    assert runner.current_state.tags["Sending"] is True

    futures[0].set_result(click_email._EmailResult(ok=True, error_code=0))
    runner.patch({"Enable": True})
    runner.step()

    assert len(submissions) == 1
    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is True
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ErrorCode"] == 0

    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ErrorCode"] == 0

    runner.patch({"Enable": True})
    runner.step()
    assert len(submissions) == 2
    assert runner.current_state.tags["Sending"] is True
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is False


def test_click_email_disable_clears_and_cancels_pending(monkeypatch: pytest.MonkeyPatch):
    import pyrung.examples.click_email as click_email

    class TrackingFuture(Future[click_email._EmailResult]):
        def __init__(self):
            super().__init__()
            self.cancel_calls = 0

        def cancel(self) -> bool:
            self.cancel_calls += 1
            return super().cancel()

    pending = TrackingFuture()

    def fake_submit(**kwargs: object) -> Future[click_email._EmailResult]:
        _ = kwargs
        return pending

    monkeypatch.setattr(click_email, "_submit_email_request", fake_submit)

    Enable = Bool("Enable")
    Subject = Int("Subject")
    Body = Int("Body")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")
    ErrorCode = Int("ErrorCode")
    email = click_email.EmailInstruction(
        smtp_host="127.0.0.1",
        smtp_port=25,
        recipients=("test@example.com",),
    )

    with Program() as logic:
        with Rung(Enable):
            run_enabled_function(
                email,
                ins={"subject": Subject, "body": Body},
                outs={
                    "sending": Sending,
                    "success": Success,
                    "error": Error,
                    "error_code": ErrorCode,
                },
            )

    runner = PLCRunner(logic=logic)
    runner.patch(
        {
            "Enable": True,
            "Subject": 1,
            "Body": 2,
            "Sending": False,
            "Success": False,
            "Error": False,
            "ErrorCode": 0,
        }
    )
    runner.step()
    assert runner.current_state.tags["Sending"] is True

    runner.patch({"Enable": False})
    runner.step()

    assert pending.cancel_calls == 1
    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is False
    assert runner.current_state.tags["ErrorCode"] == 0
