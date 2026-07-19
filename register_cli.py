"""CLI wrapper for grok_register_ttk — multi-thread register + async CPA mint pipeline.

Architecture:
  Register workers (R)  →  accounts_cli + mint_queue
  Mint workers (M)      →  cpa_auths/xai-*.json + optional hotload

Browser lifecycle:
  - One Chromium per register worker, reused via TabPool.clear_session
  - Full recycle every N accounts or on error
  - Register browser released BEFORE mint (mint always standalone Chromium)
  - Peak browsers ≈ R + M (not 2×R)
  - Startup: kill PPID=1 orphan Drission Chromes + empty Xvfb left by crashed runs
  - Process-level flock (default) prevents dual register_cli device-page stalls
"""
from __future__ import annotations

import argparse
import atexit
import json
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
from proxy_rotate import (  # noqa: E402
    configure_proxy_rotation,
    maybe_rotate_proxy,
    restore_proxy_rotation,
)


# Linux 适配: DrissionPage 默认找 'chrome', 我们装的是 chromium
# 保留原版 slim flags + proxy，再补 chromium 路径与 turnstilePatch。
_orig_create_browser_options = reg.create_browser_options


def _patched_create_browser_options(browser_proxy=None, *, apply_config_proxy=True):
    # Prefer original factory (proxy bridge + CHROMIUM_SLIM_FLAGS + extension).
    # Must forward apply_config_proxy so mint path can set proxy exactly once.
    try:
        opts = _orig_create_browser_options(
            browser_proxy=browser_proxy,
            apply_config_proxy=apply_config_proxy,
        )
    except TypeError:
        # older signature without browser_proxy / apply_config_proxy
        try:
            opts = _orig_create_browser_options(browser_proxy=browser_proxy)
        except TypeError:
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

    # pxed/k8s / Xvfb: force sandbox-less flags even if upstream options omitted them
    for flag in (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",
    ):
        try:
            opts.set_argument(flag)
        except Exception:
            pass

    # Prefer Playwright CFT chrome on pxed, then common system paths
    for cand in (
        "/personal/browsers/ms-playwright/chromium-1228/chrome-linux64/chrome",
        "/usr/bin/google-chrome",
        "/usr/local/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(cand):
            try:
                opts.set_browser_path(cand)
            except Exception:
                pass
            break

    # Never leave --disable-images on: Turnstile widget needs image/cdn assets
    try:
        args = getattr(opts, "arguments", None)
        if isinstance(args, (list, tuple)):
            cleaned = [a for a in args if a != "--disable-images" and not str(a).startswith("--disable-images=")]
            if len(cleaned) != len(args):
                try:
                    opts._arguments = cleaned  # type: ignore[attr-defined]
                except Exception:
                    pass
    except Exception:
        pass

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
    # mint_token_ok: OIDC tokens written (token_ok=True). Product-usable free Build
    # is chat_ok / remote_live_ok — not this counter.
    "mint_token_ok": 0,
    # mint_success: product ok from export (probes resolved). Not the same as chat_ok.
    "mint_success": 0,
    "mint_fail": 0,
    "mint_skip": 0,
    "chat_ok": 0,
    "chat_denied": 0,
    "chat_fail": 0,
    "remote_inject_ok": 0,
    "remote_inject_fail": 0,
    "remote_inject_skip": 0,
    "remote_live_ok": 0,
    "remote_live_fail": 0,
    # mint path counters (observability; not product gates)
    "mint_method_pkce": 0,
    "mint_method_protocol": 0,
    "mint_method_protocol_device": 0,
    "mint_method_browser": 0,
    "mint_method_other": 0,
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

# Process-level single-instance lock (ad-hoc CLI / smoke). Bulk supervisor has its own.
# Path overridable via GROK_REGISTER_CLI_LOCK. SKIP_REGISTER_CLI_LOCK=1 disables.
_DEFAULT_CLI_LOCK_PATH = "/tmp/grok_register_cli.lock"
_cli_lock_fd: int | None = None
_cli_lock_path: str = ""
# Last mint fail taxonomy for SUMMARY_JSON (best-effort, not a product gate).
_last_mint_fail_reason: list[str] = [""]
_last_mint_fail_phase: list[str] = [""]
_last_mint_fail_lock = threading.Lock()


def _note_mint_fail(reason: str, phase: str = "") -> None:
    with _last_mint_fail_lock:
        _last_mint_fail_reason[0] = (reason or "")[:200]
        _last_mint_fail_phase[0] = (phase or "")[:80]


def _classify_mint_fail(result: dict[str, Any] | None) -> tuple[str, str]:
    """Derive (mint_fail_reason, mint_fail_phase) from export/mint result dict."""
    if not isinstance(result, dict):
        return ("mint_error", "")
    explicit = str(result.get("mint_fail_reason") or "").strip()
    phase = str(result.get("mint_fail_phase") or "").strip()
    if explicit:
        return (explicit[:200], phase[:80])
    err = str(result.get("error") or result.get("fail_reason") or "").strip()
    low = err.lower()
    if "device_click_stall" in low:
        return ("device_click_stall", phase or "device")
    if "browser confirm timeout" in low or "timeout phase=" in low:
        # phase=device|consent|password|email ...
        p = phase
        if not p and "phase=" in low:
            try:
                p = low.split("phase=", 1)[1].split(None, 1)[0].strip()
            except Exception:
                p = ""
        return ("browser_timeout", p or "device")
    if "auth failed" in low or "turnstile" in low:
        return ("auth_failed", phase or "password")
    if "chromium" in low or "browser connection" in low:
        return ("browser_boot", phase or "boot")
    if err:
        return ("mint_error", phase)
    return ("mint_error", phase)


def _release_cli_lock() -> None:
    """Best-effort unlock + close flock fd (atexit / early return)."""
    global _cli_lock_fd, _cli_lock_path
    fd = _cli_lock_fd
    path = _cli_lock_path
    _cli_lock_fd = None
    _cli_lock_path = ""
    if fd is None:
        return
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    # Leave lock file on disk (path marker); do not unlink — other waiter may open it.


def acquire_register_cli_lock(
    *,
    lock_path: str | None = None,
    skip: bool | None = None,
) -> tuple[bool, str]:
    """Non-blocking exclusive flock for single-instance ad-hoc register_cli.

    Returns (ok, message). ok=False → another process holds the lock (caller exit 1).
    skip=True / env SKIP_REGISTER_CLI_LOCK=1 → always ok (bulk supervisor path).
    """
    global _cli_lock_fd, _cli_lock_path
    if skip is None:
        skip = str(os.environ.get("SKIP_REGISTER_CLI_LOCK") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if skip:
        return True, "lock skipped (SKIP_REGISTER_CLI_LOCK)"
    path = (lock_path or os.environ.get("GROK_REGISTER_CLI_LOCK") or _DEFAULT_CLI_LOCK_PATH).strip()
    if not path:
        path = _DEFAULT_CLI_LOCK_PATH
    try:
        import fcntl
    except ImportError:
        # Windows / non-POSIX: best-effort no-op (production is Linux pxed).
        return True, "lock skipped (no fcntl)"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        return False, f"lock open failed path={path}: {e}"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            os.close(fd)
        except Exception:
            pass
        holder = ""
        try:
            with open(f"{path}.pid", encoding="utf-8") as pf:
                holder = (pf.read() or "").strip()
        except Exception:
            pass
        msg = f"another register_cli holds {path}"
        if holder:
            msg = f"{msg} pid={holder}"
        return False, msg
    except OSError as e:
        try:
            os.close(fd)
        except Exception:
            pass
        return False, f"lock flock failed path={path}: {e}"
    _cli_lock_fd = fd
    _cli_lock_path = path
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
    except Exception:
        pass
    try:
        with open(f"{path}.pid", "w", encoding="utf-8") as pf:
            pf.write(f"{os.getpid()}\n")
    except Exception:
        pass
    atexit.register(_release_cli_lock)
    return True, f"lock acquired path={path} pid={os.getpid()}"


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


def is_turnstile_headless_upgradeable(msg: str) -> bool:
    """Turnstile stuck with empty token while headless — may recover once headed.

    Evidence from live runs: headless often token_len=0; same env headed
    passes. Upgrade is allowed once per process, never loops.
    """
    text = str(msg or "")
    if "Turnstile 卡住 fail-fast" not in text and "Turnstile 获取 token 失败" not in text:
        return False
    if "token_len=0" in text or "token_len = 0" in text:
        return True
    # pre-submit retry exhaustion without token is the same class
    return "pre-submit retries exhausted" in text or "retries exhausted" in text


def headed_display_ready() -> bool:
    """True when switching to headed Chromium will not fail on missing X display.

    Prefer shared ``tab_pool.display_available`` so register + mint agree.
    Fallback: macOS/Windows always ok; Linux requires non-empty DISPLAY.
    """
    try:
        from tab_pool import display_available

        return bool(display_available())
    except Exception:
        if sys.platform == "darwin" or sys.platform.startswith("win"):
            return True
        return bool((os.environ.get("DISPLAY") or "").strip())



def product_batch_success(stats: dict, cfg: dict | None = None) -> bool:
    """True when this batch delivered the configured product criterion.

    - Always require reg_success > 0.
    - Pure register mode (cpa_export_enabled=false): reg_success alone is enough.
    - disk-first: mint_token_ok is product success when chat probe off
      (CPA_PROBE_CHAT=false / cpa_probe_chat=false). Models-only or chat probe
      stay separate; import still filters healthy later.
    - When cpa_export_enabled and chat probe on: require at least one chat_ok.
    - When cpa_remote_inject also on: require at least one remote_live_ok
      (one-click live pool success). Inventory-only inject without live does not count.
    """
    cfg = cfg or {}
    if int(stats.get("reg_success", 0) or 0) <= 0:
        return False
    cpa_on = True
    raw = cfg.get("cpa_export_enabled", True)
    if isinstance(raw, bool):
        cpa_on = raw
    else:
        s = str(raw).strip().lower()
        if s in {"0", "false", "no", "off", "n", ""}:
            cpa_on = False
    if not cpa_on:
        return True

    def _cfg_bool(val, default: bool = False) -> bool:
        if isinstance(val, bool):
            return val
        if val is None:
            return default
        return str(val).strip().lower() in {"1", "true", "yes", "on", "y"}

    # disk-first export: token write is the success criterion when chat is not probed.
    probe_chat = _cfg_bool(cfg.get("cpa_probe_chat"), default=True)
    if not probe_chat:
        return int(stats.get("mint_token_ok", 0) or 0) > 0

    if int(stats.get("chat_ok", 0) or 0) <= 0:
        return False
    inj_on = _cfg_bool(cfg.get("cpa_remote_inject"), default=False)
    if inj_on and int(stats.get("remote_live_ok", 0) or 0) <= 0:
        return False
    return True


def is_fatal_register_error(msg: str) -> bool:
    """Hard blockers that must stop the job (no retry / no empty loop)."""
    text = str(msg or "")
    markers = (
        "可用别名已耗尽",
        "plus-alias 已禁用",
        "Hotmail plus-alias 已禁用",
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
        "Gmail 模式需要",
        "Gmail catch-all 需要在 defaultDomains",
        "Gmail 无法生成可用域名邮箱",
        "Gmail IMAP 凭证未配置",
        "Gmail IMAP 认证失败",
        "AUTHENTICATIONFAILED",
        "Invalid credentials",
        # Xvfb/datacenter 上 Turnstile 恒 0 时，整批空转无意义；单号失败后停止本批
        # (after optional one-shot headless→headed upgrade)
        "Turnstile 卡住 fail-fast",
        # Bare --headless on Linux without DISPLAY: refuse headed upgrade and stop batch
        "Turnstile headless 失败且无可用 DISPLAY",
        "headed 需要 DISPLAY/xvfb-run",
    )
    return any(m in text for m in markers)


# Process-wide: only one headless→headed Turnstile upgrade attempt.
_turnstile_headed_upgrade_lock = threading.Lock()
_turnstile_headed_upgrade_done = False


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


def _should_persist_email_stage_error() -> bool:
    """Persist failed registration addresses for non-disposable pools (Hotmail/Gmail/CloudMail)."""
    try:
        provider = reg.get_email_provider()
    except Exception:
        provider = (getattr(reg, "config", {}) or {}).get("email_provider", "")
    return str(provider or "").strip().lower() in {
        "hotmail",
        "outlook",
        "outlookmail",
        "microsoft",
        "gmail",
        "google",
        "googlemail",
        "cloudmail",
    }


def _mark_email_stage_error(email: str, reason: str) -> None:
    """Persist failed addresses so the next run does not reuse them."""
    if not email or not _should_persist_email_stage_error():
        return
    try:
        reg.mark_error(email, reason=str(reason)[:120])
    except Exception:
        pass


def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle.

    Also runs proxy rotation (when enabled) before starting the browser so the
    new account attempt uses the rotated egress. List mode rewrites
    reg.config["proxy"]; clash mode only switches the dedicated GROK-REG
    selector (main profile group untouched).
    """
    if force_recycle:
        try:
            reg.stop_browser()
        except Exception:
            pass
    # Rotate egress before (re)creating the browser so --proxy-server picks it up.
    try:
        maybe_rotate_proxy(log=lambda m: log(worker_id, m), config=reg.config)
    except Exception as exc:
        log(worker_id, f"[!] 代理轮换失败(继续用当前出口): {exc}")
    if reg.TabPool.get_browser() is None:
        reg.start_browser(log_callback=lambda m: log(worker_id, m))


def classify_email_stage_failure(msg: str) -> str:
    """Classify email/code stage failure for retry policy.

    Returns:
      fatal         — resource/config exhausted; stop whole batch (no retry)
      progress_fail — code filled but profile not reached (do not swap mailbox as mail-miss)
      mail_miss     — verification code not received / IMAP path
      browser_boot  — Chromium start / chrome-error interstitial / connection fails
      other         — navigation/form/browser hard failure
    """
    text = str(msg or "")
    low = text.lower()
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
    # Chromium boot / interstitial / proxy path dead: recycle browser + force rotate.
    # Do not burn mailbox quota as if signup UI logic failed.
    if (
        "browser_boot" in low
        or "chrome error page" in low
        or "chrome-error://" in low
        or "the browser connection fails" in low
        or "browser connection fails" in low
        or "standalone chromium start failed" in low
        or "浏览器启动失败" in text
        or "devtoolsactiveport" in low
        or "user data directory is already in use" in low
        # Dead Clash / proxy path (observed batch50: ERR_CONNECTION_CLOSED on accounts.x.ai)
        or "err_connection_closed" in low
        or "err_connection_reset" in low
        or "err_connection_refused" in low
        or "err_connection_aborted" in low
        or "err_connection_timed_out" in low
        or "err_timed_out" in low
        or "err_tunnel_connection_failed" in low
        or "err_proxy_connection_failed" in low
        or "err_socks_connection_failed" in low
        or "err_name_not_resolved" in low
        or "err_network_changed" in low
        or "err_internet_disconnected" in low
        or "err_address_unreachable" in low
        or "err_ssl_protocol_error" in low
        or "net::err_" in low
        or "connection closed" in low
        or "connection reset" in low
        or "connection refused" in low
        or "proxy connect" in low
        or "tunnel connection failed" in low
        # Pre-email SPA stuck on「您正在登录」after「使用邮箱注册」— browser recycle +
        # slot retry (not reg_fail/other). Post-profile SSO mid-state uses different
        # wording (final-page-no-submit) and must NOT match here.
        or "signup_spa_stuck" in low
        or (
            "您正在登录" in text
            and (
                "邮箱表单未挂载" in text
                or "未挂载输入框" in text
                or "邮箱注册按钮点击后" in text
                or "未找到邮箱表单" in text
            )
        )
        or (
            "signing in" in low
            and (
                "signup_spa_stuck" in low
                or "email form" in low
                or "email input" in low
            )
        )
    ):
        return "browser_boot"
    return "other"



def _force_rotate_path(worker_id: int, reason: str) -> dict:
    """Immediately switch Clash/list egress — do not spin on a dead node.

    Used on browser_boot / connection-closed. force=True bypasses rotate_every.
    Safe no-op when mode=off. Failures are logged; never raise into register loop.
    """
    try:
        result = maybe_rotate_proxy(
            force=True,
            log=lambda m: log(worker_id, m),
            config=getattr(reg, "config", None),
        )
        rotated = bool((result or {}).get("rotated"))
        label = (result or {}).get("label") or (result or {}).get("node") or "-"
        prev = (result or {}).get("prev") or "-"
        mode = (result or {}).get("mode") or "?"
        log(
            worker_id,
            f"[*] fail-fast 换路 reason={reason!r} rotated={rotated} "
            f"mode={mode} {prev} -> {label}",
        )
        return result if isinstance(result, dict) else {"rotated": False}
    except Exception as exc:  # noqa: BLE001
        log(worker_id, f"[!] fail-fast 换路失败(继续回收浏览器): {exc}")
        return {"rotated": False, "error": str(exc)}


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
    """Full Chromium quit+create for stuck pages / unknown failures.

    Also kills PPID=1 Drission leftovers so the next start does not race
    auto_port against orphan processes from a previous crash.
    """
    try:
        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
    except Exception:
        pass
    try:
        from tab_pool import TabPool

        protect = set()
        try:
            protect |= TabPool.tracked_pids()
        except Exception:
            pass
        cres = TabPool.cleanup_orphans(
            log_callback=lambda m: log(worker_id, m),
            protect_pids=protect,
            only_ppid_init=True,
        )
        if cres.get("killed"):
            log(
                worker_id,
                f"[*] hard recycle orphan cleanup: killed={cres['killed']} pids={cres.get('pids')}",
            )
    except Exception as exc:  # noqa: BLE001
        log(worker_id, f"[Debug] hard recycle orphan cleanup skipped: {exc}")


def _resolved_recycle_mode() -> str:
    """Delegate to grok_register_ttk so CLI and library share one source of truth."""
    fn = getattr(reg, "_resolved_recycle_mode", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:
            pass
    mode = str(
        (getattr(reg, "PERF_FLAGS", {}) or {}).get("browser_recycle_mode")
        or (getattr(reg, "config", {}) or {}).get("browser_recycle_mode")
        or "soft"
    ).strip().lower()
    if mode not in ("soft", "hybrid", "hard"):
        return "soft"
    return mode


def _account_slot_retry_limit(config: dict | None = None) -> int:
    """Parse account_slot_retry; 0 is valid (disable). Never treat 0 as missing."""
    cfg = config if isinstance(config, dict) else (getattr(reg, "config", {}) or {})
    raw = cfg.get("account_slot_retry", 3)
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return 3
    try:
        n = int(raw)
    except Exception:
        return 3
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

    Returns:
      success dict, or failure dict with ok=False and optional flags:
        - slot_exhausted: True when AccountRetryNeeded budget used up
      None only for browser-start hard fail (legacy).

    Raises FatalRegisterError on unrecoverable resource/config errors.
    AccountRetryNeeded is handled internally (slot retry); not re-raised to worker.
    """
    AccountRetryNeeded = reg.AccountRetryNeeded

    email = ""
    dev_token = ""
    try:
        max_mail_retry = max(1, int((getattr(reg, "config", {}) or {}).get("mail_retry_count", 3) or 3))
    except Exception:
        max_mail_retry = 3
    cancel = DummyStop()
    max_slot_retry = _account_slot_retry_limit()
    slot_retry = 0
    last_slot_email = ""

    while True:
        email = ""
        dev_token = ""
        try:
            _ensure_browser(worker_id, force_recycle=False)
        except Exception as exc:
            msg = str(exc)
            log(worker_id, f"! 浏览器启动失败: {exc}")
            # Missing DISPLAY / headed config is not a flaky boot — stop batch.
            if is_fatal_register_error(msg) or (
                "headed 需要 DISPLAY" in msg
                or ("DISPLAY" in msg and "xvfb" in msg.lower())
            ):
                fatal_msg = (
                    f"浏览器启动致命失败（headed 需要 DISPLAY/xvfb-run）: {msg}"
                )
                log(worker_id, f"! 致命错误，停止整批（不空转）: {fatal_msg}")
                _inc("reg_fail")
                request_fatal_stop(fatal_msg)
                raise FatalRegisterError(fatal_msg) from exc
            # Non-fatal boot flake / proxy path: switch egress before returning fail
            # so supervisor's next sub does not sit on the same dead Clash node.
            _force_rotate_path(worker_id, reason=f"browser_start:{msg[:80]}")
            return {"ok": False, "error": f"browser start: {exc}", "idx": idx}

        mail_ok = False

        def _clear_mail_provider_bind() -> None:
            try:
                if hasattr(reg, "clear_email_provider_bind"):
                    reg.clear_email_provider_bind()
            except Exception:
                pass

        def _reset_mail_provider_attempt_state() -> None:
            # New account (or fresh mail stage): failover index must not leak
            # from a previous account that already advanced through the pool.
            try:
                if hasattr(reg, "reset_email_provider_failover"):
                    reg.reset_email_provider_failover()
            except Exception:
                pass
            _clear_mail_provider_bind()

        def _advance_mail_provider_on_miss() -> None:
            # Multi-select: release bind so next try picks next channel
            # (RR/random) or next failover member.
            try:
                if hasattr(reg, "advance_email_provider_failover"):
                    reg.advance_email_provider_failover()
            except Exception:
                pass
            _clear_mail_provider_bind()

        _reset_mail_provider_attempt_state()
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
                try:
                    provider_now = reg.get_email_provider()
                except Exception:
                    provider_now = "?"
                log(worker_id, f"邮箱: {email} (provider={provider_now})")
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
            except AccountRetryNeeded:
                _clear_mail_provider_bind()
                raise
            except Exception as exc:
                msg = str(exc)
                kind = classify_email_stage_failure(msg)
                if kind == "fatal":
                    log(worker_id, f"! 致命错误，停止整批（不空转）: {msg}")
                    _inc("reg_fail")
                    _clear_mail_provider_bind()
                    request_fatal_stop(msg)
                    raise FatalRegisterError(msg) from exc
                if kind == "mail_miss" and mail_try < max_mail_retry:
                    log(worker_id, f"! 本邮箱未取到验证码，换邮箱重试: {msg}")
                    _mark_email_stage_error(email, msg)
                    _advance_mail_provider_on_miss()
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
                    _clear_mail_provider_bind()
                    # 页面可能卡在中间态，强制完整回收
                    _hard_recycle_browser(worker_id)
                    return {"ok": False, "error": msg, "idx": idx, "kind": "progress_fail"}
                if kind == "browser_boot":
                    # Chromium connection / chrome-error interstitial / dead Clash path:
                    # force-rotate egress immediately, then slot retry (do not burn mailbox
                    # quota as if signup UI logic failed; do not spin on same dead node).
                    log(worker_id, f"! 浏览器启动/错误页/连接断开({kind}): {msg}")
                    _mark_email_stage_error(email, msg)
                    _clear_mail_provider_bind()
                    _force_rotate_path(worker_id, reason=f"browser_boot:{msg[:80]}")
                    raise AccountRetryNeeded(f"browser_boot: {msg}") from exc
                log(worker_id, f"! 邮箱阶段失败({kind}): {msg}")
                _mark_email_stage_error(email, msg)
                traceback.print_exc()
                _inc("reg_fail")
                _clear_mail_provider_bind()
                _hard_recycle_browser(worker_id)
                return {"ok": False, "error": msg, "idx": idx, "kind": kind}

        _clear_mail_provider_bind()
        if not mail_ok:
            return {"ok": False, "error": "mail stage failed", "idx": idx}

        try:
            log(worker_id, "4. 填写资料")
            try:
                profile_timeout = int(reg.config.get("profile_timeout", 120) or 120)
            except Exception:
                profile_timeout = 120
            try:
                profile = reg.fill_profile_and_submit(
                    timeout=profile_timeout,
                    log_callback=lambda m: log(worker_id, m),
                    cancel_callback=cancel,
                )
            except Exception as profile_exc:
                msg = str(profile_exc)
                # headless Turnstile token_len=0 → one-shot upgrade to headed, then retry slot
                # ONLY when DISPLAY/Xvfb is ready; otherwise fail-fast (no browser_boot spin).
                if (
                    is_turnstile_headless_upgradeable(msg)
                    and bool((getattr(reg, "config", {}) or {}).get("browser_headless", False))
                    and bool(
                        (getattr(reg, "config", {}) or {}).get(
                            "turnstile_auto_headed_on_fail", True
                        )
                    )
                ):
                    if not headed_display_ready():
                        fatal_msg = (
                            "Turnstile headless 失败且无可用 DISPLAY/"
                            "xvfb-run（headed 升级会必挂 browser_boot）。"
                            "请用默认 --no-headless + xvfb-run，或不要设 HEADLESS_FLAG=--headless。"
                            f" 原错误: {msg[:160]}"
                        )
                        log(worker_id, f"! 致命错误，停止整批（不空转）: {fatal_msg}")
                        _inc("reg_fail")
                        request_fatal_stop(fatal_msg)
                        raise FatalRegisterError(fatal_msg) from profile_exc
                    global _turnstile_headed_upgrade_done
                    do_upgrade = False
                    with _turnstile_headed_upgrade_lock:
                        if not _turnstile_headed_upgrade_done:
                            _turnstile_headed_upgrade_done = True
                            do_upgrade = True
                    if do_upgrade:
                        log(
                            worker_id,
                            "[!] Turnstile headless 失败，自动切 headed 重试一次（不空转）"
                            f" DISPLAY={os.environ.get('DISPLAY', '')!r}",
                        )
                        reg.config["browser_headless"] = False
                        try:
                            reg.stop_browser()
                        except Exception:
                            pass
                        _hard_recycle_browser(worker_id)
                        # Re-enter outer while as slot-style retry without burning fatal
                        if email:
                            try:
                                reg.mark_error(
                                    email, reason=f"turnstile-headed-upgrade:{msg[:80]}"
                                )
                            except Exception:
                                pass
                        slot_retry += 1
                        if slot_retry <= max(max_slot_retry, 1):
                            reg.sleep_with_cancel(1.0, cancel)
                            continue
                        # slot budget exhausted after upgrade — fall through fatal
                # Turnstile/datacenter hard stuck: stop whole batch, do not slot-retry spin
                if is_fatal_register_error(msg):
                    log(worker_id, f"! 致命错误，停止整批（不空转）: {profile_exc}")
                    _inc("reg_fail")
                    request_fatal_stop(msg)
                    raise FatalRegisterError(msg) from profile_exc
                raise
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
                "ok": True,
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
        except AccountRetryNeeded as exc:
            # Mark the stuck attempt's email so slot retry does not burn alias budget silently.
            if email:
                try:
                    reg.mark_error(email, reason=f"slot-retry:{str(exc)[:100]}")
                except Exception:
                    _mark_email_stage_error(email, str(exc))
                last_slot_email = email
            slot_retry += 1
            # Always switch path on stuck (even when slot budget is 0) so the *next*
            # account / next process does not inherit a dead Clash node.
            _force_rotate_path(worker_id, reason=f"slot_retry:{str(exc)[:80]}")
            if slot_retry <= max_slot_retry:
                log(
                    worker_id,
                    f"[!] 当前账号流程卡住，已换路，slot 重试 {slot_retry}/{max_slot_retry}: {exc}",
                )
                _hard_recycle_browser(worker_id)
                reg.sleep_with_cancel(1.0, cancel)
                continue
            log(worker_id, f"! slot 重试耗尽 ({max_slot_retry}): {exc}")
            traceback.print_exc()
            _inc("reg_fail")
            _hard_recycle_browser(worker_id)
            return {
                "ok": False,
                "error": str(exc),
                "idx": idx,
                "slot_exhausted": True,
                "email": last_slot_email or email,
            }
        except Exception as exc:
            log(worker_id, f"! 注册失败: {exc}")
            reg.mark_error(email or "", reason=str(exc)[:120])
            traceback.print_exc()
            _inc("reg_fail")
            try:
                reg.restart_browser(log_callback=lambda m: log(worker_id, m))
            except Exception:
                pass
            return {"ok": False, "error": str(exc), "idx": idx, "email": email}


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
        # Chat entitlement stats (product: models-only is not free Build success).
        if result.get("entitlement_denied"):
            _inc("chat_denied")
        elif result.get("chat_ok") is True:
            _inc("chat_ok")
        elif result.get("chat_ok") is False or (
            isinstance(result.get("probe_chat"), dict)
            and result.get("probe_chat")
            and not result["probe_chat"].get("ok")
        ):
            _inc("chat_fail")

        # Honesty: token write vs product ok are separate.
        # token_ok=True after OIDC write even when chat/models fail product ok.
        if result.get("token_ok") is True:
            _inc("mint_token_ok")
            mm = str(result.get("mint_method") or "").strip().lower()
            if mm in ("pkce",):
                _inc("mint_method_pkce")
            elif mm in ("protocol_device", "device_residual"):
                _inc("mint_method_protocol_device")
            elif mm in ("protocol", "device"):
                _inc("mint_method_protocol")
            elif mm in ("browser", "browser_device", "device_browser"):
                _inc("mint_method_browser")
            elif mm:
                _inc("mint_method_other")

        def _inj_bool(val, default: bool = False) -> bool:
            if isinstance(val, bool):
                return val
            if val is None:
                return default
            return str(val).strip().lower() in {"1", "true", "yes", "on", "y"}

        inject_on = _inj_bool(config.get("cpa_remote_inject"), default=False)
        if result.get("ok"):
            log(worker_id, f"+ CPA auth (product ok): {result.get('path')}")
            _inc("mint_success")
            # Inject counters only when remote inject is part of this run.
            # inject=false (disk-first) must not inflate skip/fail from export payload.
            if inject_on and not result.get("remote_inject_disabled"):
                multi = result.get("remote_injects")
                remote = result.get("remote_inject") or {}
                live_ok = result.get("remote_live_ok")
                if live_ok is True:
                    _inc("remote_live_ok")
                elif live_ok is False:
                    _inc("remote_live_fail")
                if result.get("remote_inject_skipped") or remote.get("skipped"):
                    _inc("remote_inject_skip")
                if isinstance(multi, list) and multi:
                    ok_n = sum(1 for r in multi if r.get("ok"))
                    fail_n = sum(
                        1 for r in multi if not r.get("ok") and not r.get("skipped")
                    )
                    skip_n = sum(1 for r in multi if r.get("skipped"))
                    # Product success = live ok when live was targeted; else any ok.
                    product_ok = live_ok if live_ok is not None else bool(ok_n)
                    if product_ok:
                        _inc("remote_inject_ok")
                        paths = result.get("remote_paths") or [
                            r.get("remote_path") or r.get("dir")
                            for r in multi
                            if r.get("ok")
                        ]
                        log(
                            worker_id,
                            f"+ tebi inject x{ok_n}"
                            f"{' (live ok)' if live_ok is True else ''}: {paths}",
                        )
                    if fail_n or live_ok is False:
                        _inc("remote_inject_fail")
                        log(
                            worker_id,
                            f"! tebi inject 部分/全部失败"
                            f"{' (live fail)' if live_ok is False else ''}: "
                            f"{result.get('remote_inject_error') or result.get('remote_inject_partial_errors')}",
                        )
                    if skip_n and not ok_n and not fail_n:
                        _inc("remote_inject_skip")
                elif remote.get("ok"):
                    _inc("remote_inject_ok")
                    log(
                        worker_id,
                        f"+ tebi inject: {remote.get('remote_path') or result.get('remote_path')}",
                    )
                elif remote.get("skipped"):
                    _inc("remote_inject_skip")
                elif result.get("remote_inject_error") or (
                    remote and not remote.get("disabled")
                ):
                    _inc("remote_inject_fail")
                    log(
                        worker_id,
                        f"! tebi inject 失败: "
                        f"{result.get('remote_inject_error') or remote.get('error') or remote}",
                    )
                else:
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
            # Product not ok. Only count mint_fail when tokens were NOT written.
            # token_ok + chat fail is a product gate miss, not a mint write failure.
            if result.get("token_ok") is not True:
                _inc("mint_fail")
                reason, phase = _classify_mint_fail(result)
                _note_mint_fail(reason, phase)
                result.setdefault("mint_fail_reason", reason)
                if phase:
                    result.setdefault("mint_fail_phase", phase)
            if result.get("entitlement_denied"):
                log(
                    worker_id,
                    f"! CPA chat entitlement_denied（token已写={bool(result.get('token_ok'))}，"
                    f"不可重试/勿 remint）: {result.get('error') or result}",
                )
            elif result.get("token_ok") is True:
                log(
                    worker_id,
                    f"! CPA product not ok（token已写 path={result.get('path')} "
                    f"chat_ok={result.get('chat_ok')}）："
                    f"{result.get('error') or result.get('fail_reason') or result}",
                )
            else:
                mfr = result.get("mint_fail_reason") or ""
                log(
                    worker_id,
                    f"! CPA auth 未成功"
                    f"{f' reason={mfr}' if mfr else ''}: "
                    f"{result.get('error') or result}",
                )
            if inject_on and not result.get("remote_inject_disabled"):
                if result.get("remote_inject_skipped"):
                    _inc("remote_inject_skip")
                elif result.get("remote_inject_error"):
                    _inc("remote_inject_fail")
        return result
    except Exception as exc:
        _inc("mint_fail")
        _note_mint_fail("mint_exception", "")
        log(worker_id, f"! CPA export 异常: {exc}")
        traceback.print_exc()
        return {
            "ok": False,
            "error": str(exc),
            "email": email,
            "mint_fail_reason": "mint_exception",
        }


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

        # Worker outer retry is intentionally limited and does NOT re-run after
        # AccountRetryNeeded slot exhaustion (register_one already spent that budget).
        # Avoid slot×worker multiplicative alias burn.
        try:
            result = register_one(
                worker_id,
                idx,
                total,
                accounts_file,
                do_mint_inline=do_mint_inline,
                mint_queue=mint_queue,
            )
            if isinstance(result, dict) and result.get("ok") is False:
                # Failure already counted/recycled inside register_one.
                if result.get("slot_exhausted"):
                    log(
                        worker_id,
                        f"[skip-outer-retry] slot 已耗尽 idx={idx} email={result.get('email') or ''}",
                    )
            # success dict / None both end this task; no outer full re-run
        except FatalRegisterError as exc:
            # 不可恢复：不重试、不换号空转，直接停本 worker（全局 stop 已 set）
            log(worker_id, f"[stop] FatalRegisterError: {exc}")
        except Exception:
            # Unexpected throw: one hard recycle, no second full registration attempt.
            log(worker_id, f"[error] 账号 {idx} 未捕获异常（不外层重跑整号）")
            traceback.print_exc()
            try:
                reg.restart_browser(log_callback=lambda m: log(worker_id, m))
            except Exception:
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
    parser.add_argument(
        "--proxy-rotate",
        choices=("off", "list", "clash"),
        default="",
        help="代理/出口 IP 轮换：off=不轮换；list=轮换 proxy_list(仅注册浏览器)；"
        "clash=在 Clash 专用策略组 GROK-REG 上轮换(域名规则命中才走该组，主策略组不动)",
    )
    parser.add_argument(
        "--proxy-rotate-every",
        type=int,
        default=-1,
        help="每 N 次注册轮换一次出口（-1=用 config/env）",
    )
    parser.add_argument(
        "--proxy-list",
        default="",
        help="list 模式代理池（逗号/分号/换行分隔，或 .txt/.list 文件路径）",
    )
    parser.add_argument(
        "--clash-group",
        default="",
        help="clash 模式专用策略组名（默认 GROK-REG，绝不写主组名）",
    )
    parser.add_argument(
        "--clash-domains",
        default="",
        help="clash 模式命中域名（逗号分隔，默认 x.ai,grok.com,grok.x.ai,assets.grok.com）",
    )
    parser.add_argument(
        "--no-cli-lock",
        action="store_true",
        help="跳过单实例 flock（调试/测试用；生产 smoke/CLI 默认加锁）",
    )
    args = parser.parse_args()

    # Single-instance flock early: dual register_cli → dual Chromium/Xvfb →
    # device page stall (visible 继续 never clicked) for ~7min then timeout.
    lock_ok, lock_msg = acquire_register_cli_lock(skip=bool(args.no_cli_lock))
    if not lock_ok:
        print(f"[!] {lock_msg}; exit 1", flush=True)
        return 1
    print(f"[*] {lock_msg}", flush=True)

    reg.load_config()
    cfg0 = getattr(reg, "config", {}) or {}

    # Proxy rotation: CLI > config > env (env already overlaid into cfg0).
    if args.proxy_rotate:
        cfg0["proxy_rotate_mode"] = args.proxy_rotate
    if args.proxy_rotate_every >= 1:
        cfg0["proxy_rotate_every"] = int(args.proxy_rotate_every)
    if args.proxy_list:
        cfg0["proxy_list"] = args.proxy_list
    if args.clash_group:
        # hard guard: refuse main-group names
        if args.clash_group in {"GLOBAL", "宝可梦", cfg0.get("clash_donor_group")}:
            print(f"[!] --clash-group 不能用主策略组 {args.clash_group!r}，已忽略改回 GROK-REG", flush=True)
        else:
            cfg0["clash_proxy_group"] = args.clash_group
    if args.clash_domains:
        cfg0["clash_rule_domains"] = args.clash_domains
    try:
        configure_proxy_rotation(cfg0, log=lambda m: print(m, flush=True))
    except Exception as exc:
        print(f"[!] 代理轮换配置失败: {exc}", flush=True)

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
        sleep_scale=0.1 if fast else 1.0,  # 1/10 human pace (Grok)
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

    # Crashed prior runs leave Drission Chrome / empty Xvfb reparented to init.
    # Clean those before starting workers; never touch live children of this process.
    try:
        from tab_pool import cleanup_orphan_drission_chromes, cleanup_orphan_xvfb

        cres = cleanup_orphan_drission_chromes(
            log_callback=lambda m: print(m, flush=True),
            only_ppid_init=True,
        )
        if cres.get("killed"):
            print(
                f"[*] 启动清理孤儿浏览器: killed={cres['killed']} pids={cres.get('pids')}",
                flush=True,
            )
        xres = cleanup_orphan_xvfb(
            log_callback=lambda m: print(m, flush=True),
            only_ppid_init=True,
            require_no_children=True,
        )
        if xres.get("killed") or xres.get("tmp_removed"):
            print(
                f"[*] 启动清理孤儿 Xvfb: killed={xres.get('killed')} "
                f"tmp_removed={xres.get('tmp_removed')} pids={xres.get('pids')}",
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 孤儿资源清理跳过: {exc}", flush=True)

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

    # Restore dedicated Clash group node (never touches main selector).
    try:
        restore_proxy_rotation(log=lambda m: print(m, flush=True))
    except Exception as exc:
        print(f"[!] 恢复 Clash 专用组节点失败: {exc}", flush=True)

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
        f"CPA token写入 {s.get('mint_token_ok', 0)}, CPA产品OK {s.get('mint_success', 0)}, "
        f"CPA写失败 {s.get('mint_fail', 0)}, CPA跳过 {s.get('mint_skip', 0)}, "
        f"chat可用 {s.get('chat_ok', 0)}, chat无权限 {s.get('chat_denied', 0)}, "
        f"chat其它失败 {s.get('chat_fail', 0)}, "
        f"tebi注入成功 {s.get('remote_inject_ok', 0)}, tebi注入失败 {s.get('remote_inject_fail', 0)}, "
        f"tebi注入跳过 {s.get('remote_inject_skip', 0)}, "
        f"live成功 {s.get('remote_live_ok', 0)}, live失败 {s.get('remote_live_fail', 0)} ===",
        flush=True,
    )
    # Product exit: free Build 成功语义（chat_ok / live），不是仅注册进程成功。
    cfg_exit = {}
    try:
        cfg_exit = dict(getattr(reg, "config", {}) or {})
    except Exception:
        cfg_exit = {}
    if _fatal_stop.is_set():
        exit_code = 2
        reason = fatal_stop_reason()
        print(f"[!] 致命错误已停止任务（不空转）: {reason}", flush=True)
    elif product_batch_success(s, cfg_exit):
        exit_code = 0
    else:
        exit_code = 1
        probe_chat_on = True
        try:
            raw_pc = cfg_exit.get("cpa_probe_chat", True)
            if isinstance(raw_pc, bool):
                probe_chat_on = raw_pc
            else:
                probe_chat_on = str(raw_pc).strip().lower() not in {
                    "0", "false", "no", "off", "n", ""
                }
        except Exception:
            probe_chat_on = True
        if probe_chat_on:
            criterion = "cpa_export 开启且 probe_chat 时需 chat_ok；remote_inject 还需 live"
        else:
            criterion = "disk-first (probe_chat=off) 需 mint_token_ok≥1（complete+refresh）"
        print(
            "[!] 本批未达到当前产品标准"
            f"（reg={s.get('reg_success', 0)} mint_token_ok={s.get('mint_token_ok', 0)} "
            f"chat_ok={s.get('chat_ok', 0)} live={s.get('remote_live_ok', 0)}；"
            f"{criterion}）",
            flush=True,
        )
    # Machine-readable fixed summary (ops/log parsers). Keep keys stable.
    try:
        summary = {
            "event": "register_cli_summary",
            "exit": exit_code,
            "reg_success": int(s.get("reg_success", 0) or 0),
            "reg_fail": int(s.get("reg_fail", 0) or 0),
            # Additive: token write honesty (stable keys keep mint_success).
            "mint_token_ok": int(s.get("mint_token_ok", 0) or 0),
            "mint_success": int(s.get("mint_success", 0) or 0),
            "mint_fail": int(s.get("mint_fail", 0) or 0),
            "mint_skip": int(s.get("mint_skip", 0) or 0),
            "chat_ok": int(s.get("chat_ok", 0) or 0),
            "chat_denied": int(s.get("chat_denied", 0) or 0),
            "chat_fail": int(s.get("chat_fail", 0) or 0),
            "remote_inject_ok": int(s.get("remote_inject_ok", 0) or 0),
            "remote_inject_fail": int(s.get("remote_inject_fail", 0) or 0),
            "remote_inject_skip": int(s.get("remote_inject_skip", 0) or 0),
            "remote_live_ok": int(s.get("remote_live_ok", 0) or 0),
            "remote_live_fail": int(s.get("remote_live_fail", 0) or 0),
            "mint_method_pkce": int(s.get("mint_method_pkce", 0) or 0),
            "mint_method_protocol": int(s.get("mint_method_protocol", 0) or 0),
            "mint_method_protocol_device": int(s.get("mint_method_protocol_device", 0) or 0),
            "mint_method_browser": int(s.get("mint_method_browser", 0) or 0),
            "mint_method_other": int(s.get("mint_method_other", 0) or 0),
            "proxy_rotate_mode": str(cfg_exit.get("proxy_rotate_mode") or "off"),
            "clash_pin_node": str(
                cfg_exit.get("clash_pin_node") or os.environ.get("GROK_NODE") or ""
            ),
            "cpa_probe_via": str(cfg_exit.get("cpa_probe_via") or "hybrid"),
            "cpa_protocol_flow": str(cfg_exit.get("cpa_protocol_flow") or "pkce"),
            "cpa_allow_device_flow_fallback": bool(
                cfg_exit.get("cpa_allow_device_flow_fallback", True)
            ),
            "fatal": bool(_fatal_stop.is_set()),
            "fatal_reason": fatal_stop_reason() if _fatal_stop.is_set() else "",
            "product_ok": bool(exit_code == 0),
            # Last mint write failure taxonomy (observability; empty on clean success).
            "mint_fail_reason": _last_mint_fail_reason[0] if s.get("mint_fail") else "",
            "mint_fail_phase": _last_mint_fail_phase[0] if s.get("mint_fail") else "",
        }
        print(
            "SUMMARY_JSON "
            + json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
    except Exception as exc:
        print(f"[!] SUMMARY_JSON emit failed: {exc}", flush=True)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
