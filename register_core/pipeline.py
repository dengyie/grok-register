"""Orchestrator: provider × count with fail-fast, optional verify + sink."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from register_core.contracts import RegisterJob, RegisterResult, VerifyResult
from register_core.email.base import EmailSource
from register_core.email.mail_proxy import resolve_mail_proxy
from register_core.email.registry import get_email_source
from register_core.errors import FailFastError, MailMissError, RegisterCoreError
from register_core.providers.base import RegisterProvider
from register_core.providers.registry import get_provider
from register_core.sink.base import ResultSink
from register_core.strategy.engine import StrategyEngine
from register_core.verify.base import Verifier
from register_core.verify.registry import get_verifier

log = logging.getLogger("register_core.pipeline")

# Shell adapters that historically owned mail internally; M3/M4 consume EmailSource
# via FIXED_EMAIL (+ optional OTP bridge). Kept only for docs / legacy messages.
_SHELL_PROVIDERS = frozenset({"grok", "mimo", "xai", "xiaomi", "mimo-tts"})


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
        strategy: StrategyEngine | None = None,
    ) -> None:
        self.provider = provider
        self.email_source = email_source
        self.verifier = verifier
        self.sink = sink
        self.fail_fast = fail_fast
        self.on_result = on_result
        self.strategy = strategy

    @classmethod
    def from_job(cls, job: RegisterJob, *, sink: ResultSink | None = None) -> Pipeline:
        provider = get_provider(job.provider, **(job.extra or {}))
        email = cls._resolve_email_source(job)
        verifier = cls._resolve_verifier(job)
        strategy = StrategyEngine.from_extra(
            job.extra,
            fail_fast=job.fail_fast,
            log_fn=lambda m: log.info("%s", m),
        )
        return cls(
            provider,
            email_source=email,
            verifier=verifier,
            sink=sink,
            fail_fast=job.fail_fast,
            strategy=strategy,
        )

    @classmethod
    def from_profile(
        cls,
        profile: Any,
        *,
        sink: ResultSink | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Pipeline:
        """Build pipeline from a register.v1 RegisterProfile.

        Injects CompositeEmailSource (mailbox + decode) for all providers that
        can consume FIXED_EMAIL / EMAIL_PROVIDER inject (ChatGPT, MiMo, Grok).
        """
        from register_core.config.loader import build_composite_email, profile_to_job

        job = profile_to_job(profile, overrides=overrides)
        provider = get_provider(job.provider, **(job.extra or {}))
        composite = None
        try:
            composite = build_composite_email(profile)
        except Exception as exc:
            # provider-internal mail returns None; other errors propagate
            from register_core.config.loader import ProfileLoadError

            if isinstance(exc, ProfileLoadError):
                raise ValueError(str(exc)) from exc
            raise
        if composite is None:
            email = cls._resolve_email_source(job)
        else:
            email = composite  # type: ignore[assignment]
        verifier = cls._resolve_verifier(job)
        strategy = StrategyEngine.from_extra(
            job.extra,
            fail_fast=job.fail_fast,
            log_fn=lambda m: log.info("%s", m),
        )
        return cls(
            provider,
            email_source=email,
            verifier=verifier,
            sink=sink,
            fail_fast=job.fail_fast,
            strategy=strategy,
        )

    @staticmethod
    def _resolve_email_source(job: RegisterJob) -> EmailSource | None:
        # Profile loader may stash a pre-built composite on extra.
        pre = (job.extra or {}).get("_email_source_obj")
        if pre is not None:
            return pre  # type: ignore[return-value]
        name = (job.email_source or "provider").strip().lower()
        if name in ("", "provider", "none", "internal"):
            return None
        # Mail path must never inherit register egress (PROXY_LIST / attempt proxy).
        mail_proxy = resolve_mail_proxy(job.extra)
        kw: dict[str, Any] = {}
        if mail_proxy:
            kw["proxy"] = mail_proxy
        else:
            # Explicit direct when source accepts proxy kw (tinyhost / duckmail).
            kw["proxy"] = None
        domain = str((job.extra or {}).get("email_domain") or "").strip()
        if domain and name in ("tinyhost", "auto"):
            kw["domain"] = domain
        if name == "auto":
            return get_email_source("auto", **kw)
        return get_email_source(name, **kw)

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
        base_extra = dict(extra or {})
        strategy = self.strategy or StrategyEngine.from_extra(
            base_extra,
            fail_fast=self.fail_fast,
            log_fn=lambda m: log.info("%s", m),
        )
        self.strategy = strategy

        # Gate: probe project nodes before any register attempt (list/auto).
        # Dead catalog must not enter the registration hot path.
        # Stash provider so L2 business-domain targets resolve from the map.
        try:
            if self.provider and getattr(self.provider, "name", None):
                base_extra.setdefault("provider", self.provider.name)
                base_extra.setdefault("_provider", self.provider.name)
        except Exception:
            pass
        try:
            from register_core.util.proxy import preflight_nodes_for_register

            base_extra = preflight_nodes_for_register(
                base_extra,
                log_fn=lambda m: log.info("%s", m),
            )
            pf = base_extra.get("_nodes_preflight") if isinstance(base_extra, dict) else None
            if isinstance(pf, dict):
                stats.nodes_preflight = {
                    k: pf.get(k)
                    for k in (
                        "skipped",
                        "reason",
                        "ok",
                        "fail",
                        "healthy",
                        "probed",
                        "path",
                        "probe_targets",
                        "l2_enabled",
                    )
                    if k in pf
                }
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
            log.error("fail-fast stop (nodes preflight): %s", exc)
            return stats

        for i in range(1, n + 1):
            log.info("pipeline attempt %s/%s provider=%s", i, n, self.provider.name)
            # Self-controlled egress: rotate proxy_list (or clash group) per attempt.
            # List mode never depends on Clash UI selecting a node.
            try:
                from register_core.util.proxy import inject_attempt_proxy

                attempt_extra = inject_attempt_proxy(
                    base_extra,
                    log_fn=lambda m: log.info("%s", m),
                )
            except Exception as exc:
                # Hard fail when operator required rotation (fail-fast egress).
                raw_req = base_extra.get("proxy_rotate_required")
                if raw_req is None:
                    raw_req = os.environ.get("PROXY_ROTATE_REQUIRED") or os.environ.get(
                        "CHATGPT_PROXY_ROTATE_REQUIRED"
                    )
                if isinstance(raw_req, bool):
                    required = raw_req
                else:
                    required = str(raw_req or "").strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                if required:
                    raise
                log.warning("proxy rotation skipped: %s", exc)
                attempt_extra = dict(base_extra)

            # Strategy precheck: burned egress / cooling IP → skip attempt & rotate.
            pre = strategy.precheck_egress(attempt_extra)
            if pre.should_stop:
                result = RegisterResult(
                    ok=False,
                    provider=self.provider.name,
                    error=pre.stop_reason,
                    error_kind="fatal",
                    secret_kind="none",
                    artifacts={
                        "strategy_precheck": pre.action,
                        "skip_attempt": bool(pre.skip_attempt),
                    },
                )
                stats.results.append(result)
                stats.fail += 1
                self._emit(result)
                if pre.skip_attempt:
                    log.warning(
                        "strategy precheck skip attempt (rotate): %s",
                        pre.stop_reason,
                    )
                    continue
                stats.stopped_reason = f"strategy: {pre.stop_reason}"
                log.error("strategy precheck stop: %s", pre.stop_reason)
                break

            # Domain hard-burn after mailbox allocate is fail-fast for fixed domains.
            # Adapters allocate inside register_one; precheck uses forced_domain when known.
            forced_dom = ""
            if self.email_source is not None:
                forced_dom = str(
                    getattr(self.email_source, "forced_domain", None)
                    or getattr(self.email_source, "domain", None)
                    or ""
                ).strip()
            if forced_dom:
                dpre = strategy.precheck_domain(forced_dom)
                if dpre.should_stop:
                    result = RegisterResult(
                        ok=False,
                        provider=self.provider.name,
                        error=dpre.stop_reason,
                        error_kind="unsupported_email",
                        secret_kind="none",
                        artifacts={"strategy_precheck": dpre.action},
                    )
                    stats.results.append(result)
                    stats.fail += 1
                    self._emit(result)
                    stats.stopped_reason = f"strategy: {dpre.stop_reason}"
                    log.error("strategy domain precheck stop: %s", dpre.stop_reason)
                    break

            try:
                result = self.provider.register_one(
                    email_source=self.email_source,
                    extra=attempt_extra,
                )
            except FailFastError as exc:
                result = RegisterResult(
                    ok=False,
                    provider=self.provider.name,
                    error=str(exc),
                    error_kind="fatal",
                    secret_kind="none",
                )
                self._feedback_all(attempt_extra, result, strategy)
                stats.results.append(result)
                stats.fail += 1
                self._emit(result)
                stats.stopped_reason = f"fail_fast: {exc}"
                log.error("fail-fast stop: %s", exc)
                break
            except MailMissError as exc:
                arts: dict[str, Any] = {}
                diag = getattr(exc, "diagnostics", None)
                if diag is not None:
                    try:
                        arts["otp_wait"] = (
                            asdict(diag)
                            if hasattr(diag, "__dataclass_fields__")
                            else dict(diag)  # type: ignore[arg-type]
                        )
                    except Exception:
                        arts["otp_wait"] = {"notes": "diagnostics_serialize_failed"}
                result = RegisterResult(
                    ok=False,
                    provider=self.provider.name,
                    error=str(exc),
                    error_kind="mail_miss",
                    secret_kind="none",
                    artifacts=arts,
                )
                # mail_miss is not a dead proxy — still report so soft-cool path
                # classifies as non_proxy_failure (no quarantine / network cool).
                try:
                    self._feedback_all(attempt_extra, result, strategy)
                except FailFastError as ff:
                    stats.results.append(result)
                    stats.fail += 1
                    self._emit(result)
                    stats.stopped_reason = f"fail_fast: {ff}"
                    log.error("fail-fast stop: %s", ff)
                    break
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
                try:
                    self._feedback_all(attempt_extra, result, strategy)
                except FailFastError as ff:
                    stats.results.append(result)
                    stats.fail += 1
                    self._emit(result)
                    stats.stopped_reason = f"fail_fast: {ff}"
                    log.error("fail-fast stop: %s", ff)
                    break
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
                try:
                    self._feedback_all(attempt_extra, result, strategy)
                except FailFastError as ff:
                    stats.results.append(result)
                    stats.fail += 1
                    self._emit(result)
                    stats.stopped_reason = f"fail_fast: {ff}"
                    log.error("fail-fast stop: %s", ff)
                    break
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

            # Feedback node health + strategy burn/cool.
            try:
                sfb = self._feedback_all(attempt_extra, result, strategy)
            except FailFastError as ff:
                stats.results.append(result)
                if result.ok:
                    stats.ok += 1
                else:
                    stats.fail += 1
                self._emit(result)
                stats.stopped_reason = f"fail_fast: {ff}"
                log.error("fail-fast stop: %s", ff)
                break

            stats.results.append(result)
            if result.ok:
                stats.ok += 1
            else:
                stats.fail += 1
            self._emit(result)

            # Strategy fail_fast_kinds may stop even when pipeline.fail_fast is soft
            # on other kinds; prefer strategy decision when present.
            if not result.ok:
                if sfb and sfb.should_stop:
                    stats.stopped_reason = sfb.stop_reason or result.error or result.error_kind
                    log.error("strategy fail-fast: %s", stats.stopped_reason)
                    break
                if self.fail_fast:
                    stats.stopped_reason = result.error or result.error_kind or "failed"
                    log.error("fail-fast after failure: %s", stats.stopped_reason)
                    break

        return stats

    def _feedback_all(
        self,
        attempt_extra: dict[str, Any] | None,
        result: RegisterResult,
        strategy: StrategyEngine | None = None,
    ):
        """Proxy catalog feedback + StrategyEngine burn/cool."""
        self._feedback_proxy(attempt_extra, result)
        eng = strategy or self.strategy
        if eng is None:
            return None
        try:
            return eng.on_result(result, attempt_extra)
        except Exception as exc:
            log.debug("strategy feedback skipped: %s", exc)
            return None

    def _feedback_proxy(self, attempt_extra: dict[str, Any] | None, result: RegisterResult) -> None:
        """Mark catalog node success/failure and drop quarantined URLs from rotator."""
        try:
            from register_core.util.proxy import report_attempt_proxy_result

            report_attempt_proxy_result(
                attempt_extra,
                ok=bool(result.ok),
                error=str(result.error or ""),
                error_kind=str(result.error_kind or ""),
                log_fn=lambda m: log.info("%s", m),
            )
        except FailFastError:
            raise
        except Exception as exc:
            log.debug("proxy feedback skipped: %s", exc)

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
    nodes_preflight: dict[str, Any] = field(default_factory=dict)
