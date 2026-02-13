"""Asynchronous acustom() callback example for Click-style email behavior."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pyrung.core.tag import Tag, TagType

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from pyrung.core.context import ScanContext


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


def _clear_status(ctx: ScanContext, *, sending: Tag, success: Tag, error: Tag, error_code: Tag) -> None:
    ctx.set_tags(
        {
            sending.name: False,
            success.name: False,
            error.name: False,
            error_code.name: 0,
        }
    )


def _cancel_pending_request(pending: Future[_EmailResult] | None) -> None:
    if pending is None:
        return
    pending.cancel()


def email_instruction(
    *,
    subject_tag: Tag,
    body_tag: Tag,
    sending: Tag,
    success: Tag,
    error: Tag,
    error_code: Tag,
    smtp_host: str,
    smtp_port: int,
    recipients: Iterable[str],
) -> Callable[[ScanContext, bool], None]:
    """Factory for an acustom callback that behaves like a Click email instruction."""
    if sending.type != TagType.BOOL:
        raise TypeError(f"sending tag '{sending.name}' must be BOOL")
    if success.type != TagType.BOOL:
        raise TypeError(f"success tag '{success.name}' must be BOOL")
    if error.type != TagType.BOOL:
        raise TypeError(f"error tag '{error.name}' must be BOOL")
    if error_code.type not in {TagType.INT, TagType.DINT}:
        raise TypeError(f"error_code tag '{error_code.name}' must be INT or DINT")

    recipients_tuple = tuple(recipients)
    base = f"_custom:email:{sending.name}"
    pending_key = f"{base}:pending"
    prev_enabled_key = f"{base}:prev_enabled"
    attempt_key = f"{base}:attempt"

    def _execute(ctx: ScanContext, enabled: bool) -> None:
        pending = ctx.get_memory(pending_key, None)
        if pending is not None and not isinstance(pending, Future):
            pending = None
        prev_enabled = bool(ctx.get_memory(prev_enabled_key, False))
        attempt = int(ctx.get_memory(attempt_key, 0))

        if not enabled:
            _cancel_pending_request(pending)
            ctx.set_memory(pending_key, None)
            ctx.set_memory(prev_enabled_key, False)
            _clear_status(
                ctx,
                sending=sending,
                success=success,
                error=error,
                error_code=error_code,
            )
            return

        ctx.set_memory(prev_enabled_key, True)
        rising = not prev_enabled

        if rising and pending is None:
            subject = str(ctx.get_tag(subject_tag.name, subject_tag.default))
            body = str(ctx.get_tag(body_tag.name, body_tag.default))
            pending = _submit_email_request(
                subject=subject,
                body=body,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                recipients=recipients_tuple,
            )
            ctx.set_memory(pending_key, pending)
            ctx.set_memory(attempt_key, attempt + 1)
            ctx.set_tags(
                {
                    sending.name: True,
                    success.name: False,
                    error.name: False,
                    error_code.name: 0,
                }
            )
            return

        if pending is None:
            return

        ctx.set_tag(sending.name, True)
        if not pending.done():
            return

        try:
            result = pending.result()
        except Exception:
            result = _EmailResult(ok=False, error_code=0)

        ctx.set_memory(pending_key, None)
        if result.ok:
            ctx.set_tags(
                {
                    sending.name: False,
                    success.name: True,
                    error.name: False,
                    error_code.name: 0,
                }
            )
        else:
            ctx.set_tags(
                {
                    sending.name: False,
                    success.name: False,
                    error.name: True,
                    error_code.name: int(result.error_code),
                }
            )

    return _execute


__all__ = ["email_instruction"]
