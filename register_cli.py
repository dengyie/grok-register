"""CLI wrapper for grok_register_ttk — multi-thread register + async CPA mint pipeline.

Architecture:
  Register workers (R)  →  accounts_cli + mint_queue
  Mint workers (M)      →  cpa_auths/xai-*.json + optional hotload

Browser lifecycle:
  - One Chromium per register worker, reused via TabPool.clear_session
  - Full recycle every N accounts or on error
  - Register browser released BEFORE mint (mint always standalone Chromium)
  - Peak browsers ≈ R + M (not 2×R)
  - Startup: kill PPID=1 orphan Drission Chromes left by crashed runs
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import traceback
from typing import Any

# 强制走本目录的 grok_register_ttk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grok_register_ttk as reg  # noqa: E402


# Linux 适配: DrissionPage 默认找 'chrome', 我们装的是 chromium
# 保留原版 slim flags + proxy，再补 chromium 路径与 turnstilePatch。
_orig_create_browser_options = reg.create_browser_options


def _patched_create_browser_options(browser_proxy=None):
    # Prefer original factory (proxy bridge + CHROMIUM_SLIM_FLAGS + extension)
    try:
        opts = _orig_create_browser_options(browser_proxy=browser_proxy)
    except TypeError:
        # older signature without browser_proxy kw
        try:
            opts = _orig_create_browser_options()
        except Exception:
            from DrissionPage import ChromiumOptions

            opts = ChromiumOptions()
            opts.auto_port()
            opts.set_timeouts(base=1)
            for flag in getattr(reg, "CHROMIUM_SLIM_FLAGS", ()) or ():
                try:
                    opts.set_argument(flag)
                except Exception:
                    pass
    except Exception:
        from DrissionPage import ChromiumOptions

        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=1)
        for flag in getattr(reg, "CHROMIUM_SLIM_FLAGS", ()) or ():
            try:
                opts.set_argument(flag)
            except Exception:
                pass
        if browser_proxy:
            try:
                opts.set_argument(f"--proxy-server={browser_proxy}")
            except Exception:
                pass

    try:
        opts.auto_port()
    except Exception:
        pass
    try:
        opts.set_timeouts(base=1)
    except Exception:
        pass

    for cand in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(cand):
            try:
                opts.set_browser_path(cand)
            except Exception:
                pass
            break

    ext_path = os.path.join(os.path.dirname(os.path.abspath(reg.__file__)), "turnstilePatch")
    if os.path.isdir(ext_path):
        try:
            opts.add_extension(ext_path)
        except Exception:
            pass
    return opts


reg.create_browser_options = _patched_create_browser_options


# ── 线程安全日志 ──

_log_queue: queue.Queue = queue.Queue()


def _log_writer():
    while True:
        msg = _log_queue.get()
        if msg is None:
            break
        print(msg, flush=True)


def log(worker_id: int | str, msg: str) -> None:
    _log_queue.put(f"[{time.strftime('%H:%M:%S')}] [W{worker_id}] {msg}")


# ── 统计 ──

_stats_lock = threading.Lock()
_stats = {
    "reg_success": 0,
    "reg_fail": 0,
    "mint_success": 0,
    "mint_fail": 0,
    "mint_skip": 0,
    "remote_inject_ok": 0,
    "remote_inject_fail": 0,
    "remote_inject_skip": 0,
}


def _inc(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


# forever 任务索引
_next_idx_lock = threading.Lock()
_next_idx = [1]

# mint 队列结束哨兵
_MINT_STOP = object()

# 不可恢复错误：别名耗尽 / 凭证缺失等 → 全进程停止，禁止空转重试
_fatal_stop = threading.Event()
_fatal_reason_lock = threading.Lock()
_fatal_reason: list[str] = [""]


class FatalRegisterError(Exception):
    """Unrecoverable resource/config error — stop the whole batch immediately."""


def request_fatal_stop(reason: str) -> None:
    """Signal all workers to exit; first reason wins."""
    text = str(reason or "fatal").strip() or "fatal"
    with _fatal_reason_lock:
        if not _fatal_reason[0]:
            _fatal_reason[0] = text
    _fatal_stop.set()


def fatal_stop_reason() -> str:
    with _fatal_reason_lock:
        return _fatal_reason[0]


def is_fatal_register_error(msg: str) -> bool:
    """Hard blockers that must stop the job (no retry / no empty loop)."""
    text = str(msg or "")
    markers = (
        "可用别名已耗尽",
        "账号文件不存在",
        "账号文件无有效记录",
        "Cloudflare API Base 未配置",
        "CloudMail 需要在 defaultDomains",
        "CloudMail 配置不完整",
        "YYDS API Key 或 JWT 未配置",
        "YYDS 没有返回任何可用域名",
        "YYDS 无已验证域名可用",
        "DuckMail 没有返回任何可用域名",
        "DuckMail 无已验证域名可用",
        "获取 DuckMail token 失败",
        "获取 YYDS token 失败",
    )
    return any(m in text for m in markers)


def resolve_mint_workers(
    *,
    cli_value: int,
    threads: int,
    config: dict,
    inline_mint: bool,
) -> int:
    """Resolve mint worker count.

    Priority: --inline-mint > CLI --mint-workers (>=0) > config cpa_mint_workers > auto.
    auto (-1): min(threads, 4) when CPA export enabled, else 0.
    0: inline mint on register threads.
    """
    if inline_mint:
        return 0
    if cli_value >= 0:
        return max(0, min(int(cli_value), 10))
    cfg_v = config.get("cpa_mint_workers", -1)
    try:
        cfg_v = int(cfg_v)
    except Exception:
        cfg_v = -1
    if cfg_v >= 0:
        return max(0, min(cfg_v, 10))
    # auto
    if config.get("cpa_export_enabled", True):
        return max(1, min(int(threads), 4))
    return 0


def resolve_mint_queue_max(config: dict, mint_workers: int, cli_value: int | None = None) -> int:
    if cli_value is not None and cli_value >= 0:
        return int(cli_value)
    try:
        v = int(config.get("cpa_mint_queue_max", 0) or 0)
    except Exception:
        v = 0
    if v > 0:
        return v
    # default backpressure: 2 × mint workers (0 if no mint pool)
    return max(0, mint_workers * 2) if mint_workers > 0 else 0


class DummyStop:
    def __call__(self) -> bool:
        return False


def _is_hotmail_provider() -> bool:
    try:
        provider = reg.get_email_provider()
    except Exception:
        provider = (getattr(reg, "config", {}) or {}).get("email_provider", "")
    return str(provider or "").strip().lower() in {"hotmail", "outlook", "outlookmail", "microsoft"}


def _mark_email_stage_error(email: str, reason: str) -> None:
    """Persist failed Hotmail/Outlook aliases so the next run does not reuse them."""
    if not email or not _is_hotmail_provider():
        return
    try:
        reg.mark_error(email, reason=str(reason)[:120])
    except Exception:
        pass


def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle."""
    if force_recycle:
        try:
            reg.stop_browser()
        except Exception:
            pass
    if reg.TabPool.get_browser() is None:
        reg.start_browser(log_callback=lambda m: log(worker_id, m))


def classify_email_stage_failure(msg: str) -> str:
    """Classify email/code stage failure for retry policy.

    Returns:
      fatal         — resource/config exhausted; stop whole batch (no retry)
      progress_fail — code filled but profile not reached (do not swap mailbox as mail-miss)
      mail_miss     — verification code not received / IMAP path
      other         — navigation/form/browser hard failure
    """
    text = str(msg or "")
    if is_fatal_register_error(text):
        return "fatal"
    if ("未进入资料页" in text) or ("验证码已填写" in text):
        return "progress_fail"
    if (
        ("未收到验证码" in text)
        or ("获取验证码失败" in text)
        or (
            ("验证码" in text)
            and any(
                k in text
                for k in (
                    "未收到",
                    "自动填写/提交失败",
                    "IMAP",
                )
            )
        )
    ):
        return "mail_miss"
    return "other"


def _soft_recycle_browser(worker_id: int) -> None:
    """Prefer clear_session; only full restart when reuse is impossible."""
    mode = _resolved_recycle_mode()
    if mode == "hard":
        _hard_recycle_browser(worker_id)
        return
    try:
        if reg.TabPool.get_browser() is not None:
            if reg.TabPool.clear_session(log_callback=lambda m: log(worker_id, m)):
                log(worker_id, f"[*] 软回收：会话已清理，复用浏览器进程 (mode={mode})")
                return
    except Exception as exc:
        log(worker_id, f"[Debug] clear_session 失败，改完整重启: {exc}")
    try:
        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
    except Exception:
        pass


def _hard_recycle_browser(worker_id: int) -> None:
    """Full Chromium quit+create for stuck pages / unknown failures."""
    try:
        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
    except Exception:
        pass


def _resolved_recycle_mode() -> str:
    mode = str(
        (getattr(reg, "PERF_FLAGS", {}) or {}).get("browser_recycle_mode")
        or (getattr(reg, "config", {}) or {}).get("browser_recycle_mode")
        or "soft"
    ).strip().lower()
    if mode not in ("soft", "hybrid", "hard"):
        return "soft"
    return mode


def _account_slot_retry_limit() -> int:
    try:
        n = int((getattr(reg, "config", {}) or {}).get("account_slot_retry", 3) or 3)
    except Exception:
        n = 3
    return max(0, min(10, n))


def register_one(
    worker_id: int,
    idx: int,
    total: int,
    accounts_file: str,
    *,
    do_mint_inline: bool = False,
    mint_queue: queue.Queue | None = None,
) -> dict | None:
    """Run one registration. Enqueue CPA mint (default) instead of blocking.

    Returns dict(email, sso, profile) or None.
    Raises FatalRegisterError on unrecoverable resource/config errors.
    Raises AccountRetryNeeded when final-page/SSO stuck and slot retries remain
    (caller handles count); after exhaustion returns None.
    """
    AccountRetryNeeded = getattr(reg, "AccountRetryNeeded", None)
    if AccountRetryNeeded is None:
        class AccountRetryNeeded(Exception):  # type: ignore[no-redef]
            pass

    email = ""
    dev_token = ""
    try:
        max_mail_retry = max(1, int((getattr(reg, "config", {}) or {}).get("mail_retry_count", 3) or 3))
    except Exception:
        max_mail_retry = 3
    cancel = DummyStop()
    max_slot_retry = _account_slot_retry_limit()
    slot_retry = 0

    while True:
        email = ""
        dev_token = ""
        try:
            _ensure_browser(worker_id, force_recycle=False)
        except Exception as exc:
            log(worker_id, f"! 浏览器启动失败: {exc}")
            return None

        mail_ok = False
        for mail_try in range(1, max_mail_retry + 1):
            email = ""
            dev_token = ""
            try:
                log(
                    worker_id,
                    f"--- 第 {idx}/{total} 个账号, 邮箱尝试 {mail_try}/{max_mail_retry}"
                    f"{f', slot重试 {slot_retry}/{max_slot_retry}' if slot_retry else ''} ---",
                )
                log(worker_id, "1. 打开注册页")
                reg.open_signup_page(log_callback=lambda m: log(worker_id, m), cancel_callback=cancel)
                log(worker_id, "2. 创建邮箱并提交")
                email, dev_token = reg.fill_email_and_submit(
                    log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
                )
                log(worker_id, f"邮箱: {email}")
                log(worker_id, "3. 拉取验证码")
                code = reg.fill_code_and_submit(
                    email,
                    dev_token,
                    log_callback=lambda m: log(worker_id, m),
                    cancel_callback=cancel,
                )
                log(worker_id, f"验证码: {code}")
                mail_ok = True
                break
            except Exception as exc:
                if AccountRetryNeeded is not None and isinstance(exc, AccountRetryNeeded):
                    raise
                msg = str(exc)
                kind = classify_email_stage_failure(msg)
                if kind == "fatal":
                    log(worker_id, f"! 致命错误，停止整批（不空转）: {msg}")
                    _inc("reg_fail")
                    request_fatal_stop(msg)
                    raise FatalRegisterError(msg) from exc
                if kind == "mail_miss" and mail_try < max_mail_retry:
                    log(worker_id, f"! 本邮箱未取到验证码，换邮箱重试: {msg}")
                    _mark_email_stage_error(email, msg)
                    # 收码失败通常不是浏览器崩溃；优先软回收避免进程爆炸
                    _soft_recycle_browser(worker_id)
                    reg.sleep_with_cancel(1, cancel)
                    continue
                if kind == "progress_fail":
                    log(
                        worker_id,
                        f"! 验证码阶段推进失败(不换邮箱当 mail-miss): {msg}",
                    )
                    _mark_email_stage_error(email, msg)
                    traceback.print_exc()
                    _inc("reg_fail")
                    # 页面可能卡在中间态，强制完整回收
                    _hard_recycle_browser(worker_id)
                    return None
                log(worker_id, f"! 邮箱阶段失败({kind}): {msg}")
                _mark_email_stage_error(email, msg)
                traceback.print_exc()
                _inc("reg_fail")
                _hard_recycle_browser(worker_id)
                return None

        if not mail_ok:
            return None

        try:
            log(worker_id, "4. 填写资料")
            try:
                profile_timeout = int(reg.config.get("profile_timeout", 120) or 120)
            except Exception:
                profile_timeout = 120
            profile = reg.fill_profile_and_submit(
                timeout=profile_timeout,
                log_callback=lambda m: log(worker_id, m),
                cancel_callback=cancel,
            )
            log(worker_id, f"资料已填: {profile.get('given_name')} {profile.get('family_name')}")
            log(worker_id, "5. 等待 sso cookie")
            sso = reg.wait_for_sso_cookie(
                log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
            )
            from cpa_xai.accounts import format_account_line, normalize_sso_cookie

            sso = normalize_sso_cookie(sso)
            password = profile.get("password", "") or ""
            line = format_account_line(email, password, sso)
            with open(accounts_file, "a", encoding="utf-8") as f:
                f.write(line)
            log(worker_id, f"+ 注册成功: {email}")
            reg.mark_used(email, password)
            try:
                import account_backup as _ab

                _ab.backup_after_success(
                    email,
                    root=os.path.dirname(os.path.abspath(__file__)),
                    log_callback=lambda m: log(worker_id, m),
                )
            except Exception as _be:
                log(worker_id, f"[backup] 注册后备份失败: {_be}")

            # Capture cookies BEFORE releasing browser (for mint cookie inject)
            page = reg._get_page()
            cookies = []
            try:
                import cpa_export as _cpa_exp

                cookies = _cpa_exp.export_cookies_from_page(page) if page is not None else []
            except Exception:
                cookies = []
            if cookies:
                log(worker_id, f"[*] 导出 cookie {len(cookies)} 条供 mint 注入")

            if page and reg.PERF_FLAGS.get("cookie_snapshot", True):
                try:
                    reg.save_cookies_snapshot(page, "success", email)
                except Exception:
                    pass
            try:
                reg.add_token_to_grok2api_pools(
                    sso, email=email, log_callback=lambda m: log(worker_id, m)
                )
            except Exception as exc:
                log(worker_id, f"[Debug] grok2api: {exc}")

            # Release / recycle register browser BEFORE mint so peak browsers ≈ R+M
            try:
                reg.prepare_browser_for_next_account(log_callback=lambda m: log(worker_id, m))
            except Exception:
                try:
                    reg.stop_browser()
                except Exception:
                    pass

            job = {
                "email": email,
                "password": password,
                "sso": sso,
                "profile": profile,
                "idx": idx,
                "cookies": cookies,
            }

            if do_mint_inline:
                _run_mint_job(f"R{worker_id}", job, getattr(reg, "config", {}) or {})
            elif mint_queue is not None:
                # backpressure: wait while queue is saturated
                qmax = int(getattr(mint_queue, "_reg_qmax", 0) or 0)
                while qmax > 0 and mint_queue.qsize() >= qmax:
                    log(worker_id, f"[cpa] mint 队列背压 qsize={mint_queue.qsize()}≥{qmax}，等待...")
                    time.sleep(1.0)
                mint_queue.put(job)
                log(worker_id, f"[cpa] enqueued mint for {email} (queue≈{mint_queue.qsize()})")
            else:
                log(worker_id, "[cpa] mint skipped (no queue / inline)")

            _inc("reg_success")
            return job
        except Exception as exc:
            if AccountRetryNeeded is not None and isinstance(exc, AccountRetryNeeded):
                slot_retry += 1
                if slot_retry <= max_slot_retry:
                    log(
                        worker_id,
                        f"[!] 当前账号流程卡住，slot 重试 {slot_retry}/{max_slot_retry}: {exc}",
                    )
                    # hard recycle so next attempt is clean; do NOT mark email error yet
                    _hard_recycle_browser(worker_id)
                    reg.sleep_with_cancel(1.5, cancel)
                    continue
                log(worker_id, f"! slot 重试耗尽 ({max_slot_retry}): {exc}")
                reg.mark_error(email or "", reason=str(exc)[:120])
                traceback.print_exc()
                _inc("reg_fail")
                _hard_recycle_browser(worker_id)
                return None
            log(worker_id, f"! 注册失败: {exc}")
            reg.mark_error(email or "", reason=str(exc)[:120])
            traceback.print_exc()
            _inc("reg_fail")
            try:
                reg.restart_browser(log_callback=lambda m: log(worker_id, m))
            except Exception:
                pass
            return None


def _run_mint_job(worker_id: int | str, job: dict[str, Any], config: dict) -> dict:
    """Standalone CPA mint (own Chromium). Never reuses register browser."""
    email = job.get("email") or ""
    password = job.get("password") or ""
    if not email or not password:
        _inc("mint_fail")
        return {"ok": False, "error": "missing email/password", "email": email}
    if not config.get("cpa_export_enabled", True):
        _inc("mint_skip")
        log(worker_id, f"[cpa] export disabled, skip {email}")
        return {"ok": False, "skipped": True, "email": email}
    try:
        import cpa_export

        # page=None always — force standalone path inside export
        result = cpa_export.export_cpa_xai_for_account(
            email,
            password,
            page=None,
            cookies=job.get("cookies"),
            sso=job.get("sso") or "",
            config=config,
            log_callback=lambda m: log(worker_id, m),
        )
        if result.get("ok"):
            log(worker_id, f"+ CPA auth: {result.get('path')}")
            _inc("mint_success")
            remote = result.get("remote_inject") or {}
            if remote.get("ok"):
                _inc("remote_inject_ok")
                log(worker_id, f"+ tebi inject: {remote.get('remote_path') or result.get('remote_path')}")
            elif remote.get("skipped"):
                _inc("remote_inject_skip")
            elif result.get("remote_inject_error") or remote:
                _inc("remote_inject_fail")
                log(
                    worker_id,
                    f"! tebi inject 失败: {result.get('remote_inject_error') or remote.get('error') or remote}",
                )
            elif config.get("cpa_remote_inject"):
                # flag on but no remote_inject payload — wiring bug / old code path
                _inc("remote_inject_fail")
                log(worker_id, "! tebi inject 未执行（export 未返回 remote_inject）")
            try:
                import account_backup as _ab

                _ab.backup_after_success(
                    email,
                    root=os.path.dirname(os.path.abspath(__file__)),
                    cpa_path=result.get("path"),
                    log_callback=lambda m: log(worker_id, m),
                )
            except Exception as _be:
                log(worker_id, f"[backup] mint 后备份失败: {_be}")
        elif result.get("skipped"):
            _inc("mint_skip")
            log(worker_id, f"[cpa] skipped: {result.get('reason')}")
        else:
            _inc("mint_fail")
            log(worker_id, f"! CPA auth 未成功: {result.get('error') or result}")
            if result.get("remote_inject_error"):
                _inc("remote_inject_fail")
        return result
    except Exception as exc:
        _inc("mint_fail")
        log(worker_id, f"! CPA export 异常: {exc}")
        traceback.print_exc()
        return {"ok": False, "error": str(exc), "email": email}


def _register_worker(
    worker_id: int,
    task_queue: queue.Queue,
    total: int,
    accounts_file: str,
    mint_queue: queue.Queue | None,
    forever: bool,
    do_mint_inline: bool,
):
    while True:
        if _fatal_stop.is_set():
            log(worker_id, f"[stop] 致命错误已触发，退出 worker: {fatal_stop_reason()}")
            break
        try:
            idx = task_queue.get_nowait()
        except queue.Empty:
            if not forever or _fatal_stop.is_set():
                break
            with _next_idx_lock:
                nxt = _next_idx[0]
                _next_idx[0] = nxt + 5
            for i in range(nxt, nxt + 5):
                task_queue.put(i)
            continue

        retry = 0
        while retry < 2 and not _fatal_stop.is_set():
            try:
                result = register_one(
                    worker_id,
                    idx,
                    total,
                    accounts_file,
                    do_mint_inline=do_mint_inline,
                    mint_queue=mint_queue,
                )
                if result:
                    break
                # register_one 内部已在失败路径上 restart 过浏览器；
                # worker 层不再重复 restart，避免 quit+create 链叠加导致僵尸进程堆积。
                retry += 1
                if retry < 2 and not _fatal_stop.is_set():
                    log(worker_id, f"[retry] 账号 {idx} 失败，重试 {retry}/1")
            except FatalRegisterError as exc:
                # 不可恢复：不重试、不换号空转，直接停本 worker（全局 stop 已 set）
                log(worker_id, f"[stop] FatalRegisterError: {exc}")
                retry = 2
                break
            except Exception:
                # 只有 register_one 抛出未捕获异常时，worker 才需要 recycle 浏览器。
                retry += 1
                if retry < 2 and not _fatal_stop.is_set():
                    log(worker_id, f"[retry] 账号 {idx} 异常，重试 {retry}/1")
                    traceback.print_exc()
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass

        if retry >= 2:
            # register_one 已在自己的失败路径上计 reg_fail；worker 不再重复计。
            pass
        if _fatal_stop.is_set():
            break

    # worker exit: free browser
    try:
        reg.stop_browser()
    except Exception:
        pass
    log(worker_id, "register worker exit")


def _mint_worker(worker_id: str, mint_queue: queue.Queue, config: dict):
    while True:
        job = mint_queue.get()
        try:
            if job is _MINT_STOP:
                break
            if not isinstance(job, dict):
                continue
            _run_mint_job(worker_id, job, config)
        finally:
            mint_queue.task_done()
    try:
        from cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
    log(worker_id, "mint worker exit")


def main() -> int:
    parser = argparse.ArgumentParser(description="CLI runner for grok_register_ttk (pipelined).")
    parser.add_argument("--count", type=int, default=1, help="账号总数目标（0=不限；含已有）")
    parser.add_argument(
        "--extra",
        type=int,
        default=0,
        help="在已有 accounts 基础上再新注册 N 个",
    )
    parser.add_argument("--threads", type=int, default=1, help="注册并发线程数（1-10）")
    parser.add_argument(
        "--mint-workers",
        type=int,
        default=-1,
        help="CPA mint 并发：-1=用 config/auto；0=内联；1-10=固定。覆盖 config.cpa_mint_workers",
    )
    parser.add_argument(
        "--mint-queue-max",
        type=int,
        default=-1,
        help="mint 队列背压上限：-1=用 config/auto(2×workers)；0=不限制",
    )
    parser.add_argument("--accounts-file", default=os.path.join(os.path.dirname(__file__), "accounts_cli.txt"))
    parser.add_argument("--fast", action="store_true", default=True, help="快速模式（默认开）：压缩 sleep、关截图")
    parser.add_argument("--no-fast", action="store_true", help="关闭快速模式")
    parser.add_argument("--no-browser-reuse", action="store_true", help="每号强制 quit 浏览器")
    parser.add_argument("--browser-recycle-every", type=int, default=-1, help="复用 N 次后完整回收；-1=用 config")
    parser.add_argument(
        "--browser-recycle-mode",
        choices=("soft", "hybrid", "hard"),
        default="",
        help="浏览器回收策略 soft|hybrid|hard（默认 config / soft）",
    )
    parser.add_argument(
        "--account-slot-retry",
        type=int,
        default=-1,
        help="最终页/SSO 卡住时同号重试次数；-1=用 config（默认 3）",
    )
    parser.add_argument("--cookie-snapshot", action="store_true", help="注册成功写 cookie 快照（默认关，fast）")
    parser.add_argument("--inline-mint", action="store_true", help="强制注册线程内联 mint（调试用）")
    parser.add_argument("--headless", action="store_true", help="无头 Chromium 注册（覆盖 config.browser_headless）")
    parser.add_argument("--no-headless", action="store_true", help="强制有头浏览器")
    args = parser.parse_args()

    reg.load_config()
    cfg0 = getattr(reg, "config", {}) or {}
    threads = max(1, min(args.threads, 10))
    fast = bool(args.fast) and not bool(args.no_fast)
    if getattr(args, "headless", False):
        reg.config["browser_headless"] = True
    if getattr(args, "no_headless", False):
        reg.config["browser_headless"] = False
    print(f"[*] browser_headless = {bool(reg.config.get('browser_headless'))}", flush=True)

    # recycle mode / every / slot retry from CLI or config
    recycle_mode = (args.browser_recycle_mode or str(cfg0.get("browser_recycle_mode") or "soft")).strip().lower()
    if recycle_mode not in ("soft", "hybrid", "hard"):
        recycle_mode = "soft"
    if args.no_browser_reuse:
        recycle_mode = "hard"
    if args.browser_recycle_every >= 0:
        recycle_every = max(1, int(args.browser_recycle_every))
    else:
        try:
            recycle_every = max(1, int(cfg0.get("browser_recycle_every", 25) or 25))
        except Exception:
            recycle_every = 25
    if args.account_slot_retry >= 0:
        reg.config["account_slot_retry"] = max(0, min(10, int(args.account_slot_retry)))
    reg.config["browser_recycle_mode"] = recycle_mode
    reg.config["browser_recycle_every"] = recycle_every
    print(
        f"[*] browser_recycle_mode={recycle_mode} every={recycle_every} "
        f"account_slot_retry={reg.config.get('account_slot_retry', 3)}",
        flush=True,
    )

    mint_workers = resolve_mint_workers(
        cli_value=args.mint_workers,
        threads=threads,
        config=cfg0,
        inline_mint=bool(args.inline_mint),
    )
    do_mint_inline = mint_workers == 0
    mint_qmax = resolve_mint_queue_max(
        cfg0,
        mint_workers,
        cli_value=(None if args.mint_queue_max < 0 else args.mint_queue_max),
    )

    # perf knobs
    reg.configure_perf(
        fast=fast,
        sleep_scale=0.15 if fast else 1.0,
        skip_debug_io=fast,
        cookie_snapshot=bool(args.cookie_snapshot) or not fast,
        async_side_effects=True,
        browser_reuse=(recycle_mode != "hard"),
        browser_recycle_every=recycle_every,
        browser_recycle_mode=recycle_mode,
    )

    # 断点续跑
    done_count = 0
    if os.path.exists(args.accounts_file):
        with open(args.accounts_file) as f:
            done_count = sum(1 for line in f if line.strip())

    if args.extra and args.extra > 0:
        target_total = done_count + args.extra
        remaining = args.extra
        print(
            f"[*] 配置加载完成，额外新注册 {args.extra} 个（当前已有 {done_count} → 目标 {target_total}），"
            f"注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
        args.count = target_total
    elif args.count == 0:
        remaining = None
        print(
            f"[*] 配置加载完成，不限数量，注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    else:
        remaining = max(0, args.count - done_count)
        print(
            f"[*] 配置加载完成，目标 {args.count} 个账号，注册线程={threads} "
            f"mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    print(f"[*] accounts_file = {args.accounts_file}", flush=True)
    if done_count > 0:
        print(f"[*] 断点续跑：已完成 {done_count}", flush=True)
    if remaining is not None and remaining <= 0:
        print("[*] 所有账号已完成，无需继续（可用 --extra N 再注册）", flush=True)
        return 0

    log_thread = threading.Thread(target=_log_writer, daemon=True)
    log_thread.start()

    # Crashed prior runs can leave Drission Chrome reparented to launchd (PPID=1).
    # Clean those before starting workers; never touch live children of this process.
    try:
        from tab_pool import cleanup_orphan_drission_chromes

        cres = cleanup_orphan_drission_chromes(
            log_callback=lambda m: print(m, flush=True),
            only_ppid_init=True,
        )
        if cres.get("killed"):
            print(
                f"[*] 启动清理孤儿浏览器: killed={cres['killed']} pids={cres.get('pids')}",
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 孤儿浏览器清理跳过: {exc}", flush=True)

    try:
        # Factory only; real start_browser pins proxy bridge per worker thread.
        reg.TabPool.init(reg.create_browser_options, log_callback=lambda m: log(0, m))
    except Exception as exc:
        print(f"[!] 浏览器初始化失败: {exc}", flush=True)
        return 1

    task_queue: queue.Queue = queue.Queue()
    mint_queue: queue.Queue | None = queue.Queue() if not do_mint_inline else None
    if mint_queue is not None:
        mint_queue._reg_qmax = mint_qmax  # type: ignore[attr-defined]
    global _next_idx
    _next_idx[0] = done_count + 1
    if remaining is not None:
        for i in range(done_count + 1, args.count + 1):
            task_queue.put(i)
    else:
        for i in range(done_count + 1, done_count + threads * 5 + 1):
            task_queue.put(i)
        _next_idx[0] = done_count + threads * 5 + 1

    forever = remaining is None
    cfg = getattr(reg, "config", {}) or {}

    # mint workers first (so queue consumers ready)
    mint_threads: list[threading.Thread] = []
    if mint_queue is not None and mint_workers > 0:
        for i in range(1, mint_workers + 1):
            wid = f"M{i}"
            t = threading.Thread(
                target=_mint_worker,
                args=(wid, mint_queue, cfg),
                daemon=True,
                name=f"mint-{i}",
            )
            t.start()
            mint_threads.append(t)

    reg_threads: list[threading.Thread] = []
    for wid in range(1, threads + 1):
        t = threading.Thread(
            target=_register_worker,
            args=(wid, task_queue, args.count, args.accounts_file, mint_queue, forever, do_mint_inline),
            daemon=True,
            name=f"reg-{wid}",
        )
        t.start()
        reg_threads.append(t)

    try:
        for t in reg_threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[!] 用户中断", flush=True)
        request_fatal_stop("KeyboardInterrupt")

    # drain mint queue (skip long wait if fatal — still flush in-flight)
    if mint_queue is not None:
        if _fatal_stop.is_set():
            log(
                0,
                f"[cpa] 致命停止，尽快清空 mint 队列（qsize≈{mint_queue.qsize()}）...",
            )
        else:
            log(0, f"[cpa] 等待 mint 队列清空（qsize≈{mint_queue.qsize()}）...")
        mint_queue.join()
        for _ in mint_threads:
            mint_queue.put(_MINT_STOP)
        for t in mint_threads:
            t.join(timeout=120 if _fatal_stop.is_set() else 600)

    try:
        reg.shutdown_browser()
    except Exception:
        pass

    # stop side-effect pool
    try:
        pool = getattr(reg, "_side_effect_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    _log_queue.put(None)
    log_thread.join(timeout=2)

    with _stats_lock:
        s = dict(_stats)
    # Final timestamped project backup after batch (accounts + cpa_auths)
    try:
        import account_backup as _ab

        snap = _ab.snapshot_registered_accounts(
            root=os.path.dirname(os.path.abspath(__file__)),
            reason="batch_complete",
            make_timestamped=True,
            log_callback=lambda m: print(m, flush=True),
        )
        print(
            f"[backup] final snapshot accounts={snap.get('account_count')} "
            f"cpa={snap.get('cpa_count')} -> {snap.get('stamped') or snap.get('latest')}",
            flush=True,
        )
    except Exception as exc:
        print(f"[backup] final snapshot failed: {exc}", flush=True)
    print(
        f"=== 完成: 注册成功 {s.get('reg_success', 0)}, 注册失败 {s.get('reg_fail', 0)}, "
        f"CPA成功 {s.get('mint_success', 0)}, CPA失败 {s.get('mint_fail', 0)}, "
        f"CPA跳过 {s.get('mint_skip', 0)}, "
        f"tebi注入成功 {s.get('remote_inject_ok', 0)}, tebi注入失败 {s.get('remote_inject_fail', 0)}, "
        f"tebi注入跳过 {s.get('remote_inject_skip', 0)} ===",
        flush=True,
    )
    if _fatal_stop.is_set():
        reason = fatal_stop_reason()
        print(f"[!] 致命错误已停止任务（不空转）: {reason}", flush=True)
        return 2
    return 0 if s.get("reg_success", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
