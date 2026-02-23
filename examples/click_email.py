"""Threaded run_enabled_function() example for Click-style email behavior."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

_EMAIL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pyrung-email")


@dataclass(frozen=True)
class _EmailResult:
    ok: bool
    error_code: int = 0


def _send_email_stub(
    *,
    subject: str,
    body: str,
    smtp_host: str,
    smtp_port: int,
    recipients: tuple[str, ...],
) -> _EmailResult:
    _ = (subject, body, smtp_host, smtp_port)
    if not recipients:
        return _EmailResult(ok=False, error_code=1)
    return _EmailResult(ok=True, error_code=0)


def _submit_email_request(
    *,
    subject: str,
    body: str,
    smtp_host: str,
    smtp_port: int,
    recipients: tuple[str, ...],
) -> Future[_EmailResult]:
    return _EMAIL_EXECUTOR.submit(
        _send_email_stub,
        subject=subject,
        body=body,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        recipients=recipients,
    )


def _cancel_pending_request(pending: Future[_EmailResult] | None) -> None:
    if pending is not None:
        pending.cancel()


class EmailInstruction:
    """Synchronous callable object for run_enabled_function()."""

    def __init__(self, *, smtp_host: str, smtp_port: int, recipients: tuple[str, ...]):
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._recipients = recipients
        self._pending: Future[_EmailResult] | None = None

    def __call__(self, enabled: bool, subject: Any, body: Any) -> dict[str, Any]:
        if not enabled:
            _cancel_pending_request(self._pending)
            self._pending = None
            return {"sending": False, "success": False, "error": False, "error_code": 0}

        if self._pending is None:
            self._pending = _submit_email_request(
                subject=str(subject),
                body=str(body),
                smtp_host=self._smtp_host,
                smtp_port=self._smtp_port,
                recipients=self._recipients,
            )
            return {"sending": True, "success": False, "error": False, "error_code": 0}

        if not self._pending.done():
            return {"sending": True, "success": False, "error": False, "error_code": 0}

        try:
            result = self._pending.result()
        except Exception:
            result = _EmailResult(ok=False, error_code=0)

        self._pending = None
        if result.ok:
            return {"sending": False, "success": True, "error": False, "error_code": 0}
        return {
            "sending": False,
            "success": False,
            "error": True,
            "error_code": int(result.error_code),
        }


__all__ = ["EmailInstruction"]
