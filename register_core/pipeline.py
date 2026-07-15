"""Orchestrator: provider × count with fail-fast, optional verify + sink."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from register_core.contracts import RegisterJob, RegisterResult, VerifyResult
from register_core.email.base import EmailSource
from register_core.email.registry import get_email_source
from register_core.errors import FailFastError, MailMissError, RegisterCoreError
from register_core.providers.base import RegisterProvider
from register_core.providers.registry import get_provider
from register_core.sink.base import ResultSink
from register_core.verify.base import Verifier
from register_core.verify.registry import get_verifier

log = logging.getLogger("register_core.pipeline")

# Black-box adapters that cannot consume EmailSource today.
_BLACKBOX_PROVIDERS = frozenset({"grok", "mimo", "xai", "xiaomi", "mimo-tts"})


class Pipeline:
    def __init__(
        self,
        provider: RegisterProvider,
        *,
        email_source: EmailSource | None = None,
        verifier: Verifier | None = None,
        sink: ResultSink | None = None,
        fail_fast: bool = True,
        on_result: Callable[[RegisterResult], None] | None = None,
    ) -> None:
        self.provider = provider
        self.email_source = email_source
        self.verifier = verifier
        self.sink = sink
        self.fail_fast = fail_fast
        self.on_result = on_result

    @classmethod
    def from_job(cls, job: RegisterJob, *, sink: ResultSink | None = None) -> Pipeline:
        provider = get_provider(job.provider, **(job.extra or {}))
        email = cls._resolve_email_source(job)
        verifier = cls._resolve_verifier(job)
        return cls(
            provider,
            email_source=email,
            verifier=verifier,
            sink=sink,
            fail_fast=job.fail_fast,
        )

    @staticmethod
    def _resolve_email_source(job: RegisterJob) -> EmailSource | None:
        name = (job.email_source or "provider").strip().lower()
        if name in ("", "provider", "none", "internal"):
            return None
        prov = (job.provider or "").strip().lower()
        if prov in _BLACKBOX_PROVIDERS:
            raise ValueError(
                f"provider {job.provider!r} is a black-box runner and cannot use "
                f"--email-source={job.email_source!r}; use email_source=provider "
                f"(adapter-internal mail) or an in-process provider"
            )
        if name == "auto":
            return get_email_source("auto")
        return get_email_source(name)

    @staticmethod
    def _resolve_verifier(job: RegisterJob) -> Verifier:
        if not job.verify:
            return get_verifier("noop")
        try:
            return get_verifier(job.provider)
        except KeyError:
            log.warning("no verifier for %s; using noop", job.provider)
            return get_verifier("noop")

    def run(self, count: int = 1, *, extra: dict[str, Any] | None = None) -> "PipelineStats":
        stats = PipelineStats()
        n = max(1, int(count))
        for i in range(1, n + 1):
            log.info("pipeline attempt %s/%s provider=%s", i, n, self.provider.name)
            try:
                result = self.provider.register_one(
                    email_source=self.email_source,
                    extra=extra or {},
                )
            except FailFastError as exc:
                result = RegisterResult(
                    ok=False,
                    provider=self.provider.name,
                    error=str(exc),
                    error_kind="fatal",
                    secret_kind="none",
                )
                stats.results.append(result)
                stats.fail += 1
                self._emit(result)
                stats.stopped_reason = f"fail_fast: {exc}"
                log.error("fail-fast stop: %s", exc)
                break
            except MailMissError as exc:
                result = RegisterResult(
                    ok=False,
                    provider=self.provider.name,
                    error=str(exc),
                    error_kind="mail_miss",
                    secret_kind="none",
                )
                stats.results.append(result)
                stats.fail += 1
                self._emit(result)
                if self.fail_fast:
                    stats.stopped_reason = f"mail_miss: {exc}"
                    break
                continue
            except RegisterCoreError as exc:
                result = RegisterResult(
                    ok=False,
                    provider=self.provider.name,
                    error=str(exc),
                    error_kind="provider",
                    secret_kind="none",
                )
                stats.results.append(result)
                stats.fail += 1
                self._emit(result)
                if self.fail_fast:
                    stats.stopped_reason = str(exc)
                    break
                continue
            except Exception as exc:
                result = RegisterResult(
                    ok=False,
                    provider=self.provider.name,
                    error=f"unexpected: {exc}",
                    error_kind="other",
                    secret_kind="none",
                )
                stats.results.append(result)
                stats.fail += 1
                self._emit(result)
                if self.fail_fast:
                    stats.stopped_reason = f"unexpected: {exc}"
                    break
                continue

            if self.verifier and result.ok:
                try:
                    vr = self.verifier.verify(result)
                    stats.verifies.append(vr)
                    if not vr.ok:
                        result.ok = False
                        result.error = result.error or vr.detail
                        result.error_kind = result.error_kind or "verify"
                except Exception as exc:
                    stats.verifies.append(
                        VerifyResult(ok=False, provider=result.provider, detail=str(exc))
                    )
                    # Verify failure always invalidates the result.
                    result.ok = False
                    result.error = f"verify: {exc}"
                    result.error_kind = "verify"

            stats.results.append(result)
            if result.ok:
                stats.ok += 1
            else:
                stats.fail += 1
            self._emit(result)

            if not result.ok and self.fail_fast:
                stats.stopped_reason = result.error or result.error_kind or "failed"
                log.error("fail-fast after failure: %s", stats.stopped_reason)
                break

        return stats

    def _emit(self, result: RegisterResult) -> None:
        if self.sink:
            try:
                self.sink.write(result)
            except Exception as exc:
                log.warning("sink write failed: %s", exc)
        if self.on_result:
            try:
                self.on_result(result)
            except Exception as exc:
                log.warning("on_result failed: %s", exc)


@dataclass
class PipelineStats:
    ok: int = 0
    fail: int = 0
    results: list[RegisterResult] = field(default_factory=list)
    verifies: list[VerifyResult] = field(default_factory=list)
    stopped_reason: str = ""
