#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Grok 注册机 - TTK GUI 版本
整合 DrissionPage_example.py, openai_register.py, batch_open_nsfw.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import datetime
import time
import os
import sys
import queue
import secrets
import struct
import random
import re
import string
import json

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
from curl_cffi import requests

from proxy_bridge import resolve_browser_proxy


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULT_CONFIG = {
    "duckmail_api_key": "",
    "cloudflare_api_base": "",
    "cloudflare_api_key": "",
    "cloudflare_auth_mode": "bearer",
    "cloudflare_path_domains": "/domains",
    "cloudflare_path_accounts": "/accounts",
    "cloudflare_path_token": "/token",
    "cloudflare_path_messages": "/messages",
    "proxy": "http://127.0.0.1:7890",
    "browser_headless": False,
    "enable_nsfw": True,
    "register_count": 1,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "grok2api_auto_add_local": True,
    "grok2api_local_token_file": "",
    "grok2api_pool_name": "ssoBasic",
    "grok2api_auto_add_remote": False,
    "grok2api_remote_base": "",
    "grok2api_remote_app_key": "",
    "register_threads": 1,
    "thread_start_interval": 0.8,
    "show_tutorial_on_start": True,
    "cloudmail_url": "",
    "cloudmail_admin_email": "",
    "cloudmail_password": "",
    "cpa_gui_close_mint_browser": True,
    "hotmail_accounts_file": "mail_credentials.txt",
    "hotmail_alias_mode": "random",
    "hotmail_alias_random_length": 8,
    "hotmail_alias_random_max_attempts": 200,
    "hotmail_max_aliases_per_account": 5,
    "hotmail_poll_interval": 2,
    "hotmail_recent_seconds": 900,
    "hotmail_mail_fetch_modes": "rest,imap",
    "hotmail_imap_hosts": "outlook.office365.com,imap-mail.outlook.com",
    "hotmail_imap_last_n": 30,
    "hotmail_require_recipient_match": True,
    "hotmail_manual_code": False,
    # browser recycle: soft=clear_session; hard=always quit; hybrid=soft then periodic hard
    "browser_recycle_mode": "soft",
    "browser_recycle_every": 25,
    # per-account soft retry when final page / SSO path is stuck (does not override fatal)
    "account_slot_retry": 3,
    "final_page_no_submit_timeout": 45,
}

config = DEFAULT_CONFIG.copy()
_cf_domain_index = 0
# CloudMail 公开 token 单例（多线程共享，避免并发覆盖）
_cloudmail_public_token = None
_cloudmail_public_token_lock = threading.Lock()
_cpa_gui_export_lock = threading.Lock()
_hotmail_accounts_cache = None
_hotmail_accounts_mtime = None
_hotmail_accounts_lock = threading.Lock()
_hotmail_selection_lock = threading.Lock()
_hotmail_reserved_aliases = set()
_hotmail_token_map = {}
_hotmail_refresh_locks = {}
_hotmail_refresh_locks_lock = threading.Lock()
# Per-thread Chromium auth-proxy bridge (user:pass upstream)
_browser_proxy_bridge_local = threading.local()
_browser_proxy_bridge_all: list = []
_browser_proxy_bridge_lock = threading.Lock()



# ── 邮箱追踪 ──

_EMAILS_USED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emails_used.txt")
_EMAILS_ERROR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emails_error.txt")
_email_track_lock = threading.Lock()


def mark_used(email: str, password: str = ""):
    """记录成功注册的邮箱，防止重复使用。"""
    with _email_track_lock:
        with open(_EMAILS_USED_FILE, "a", encoding="utf-8") as f:
            f.write(f"{email}----{password}----ok\n")
    try:
        _hotmail_release_alias(email)
    except Exception:
        pass


def mark_error(email: str, password: str = "", reason: str = ""):
    """记录失败邮箱及原因，避免重试烂邮箱。"""
    with _email_track_lock:
        with open(_EMAILS_ERROR_FILE, "a", encoding="utf-8") as f:
            f.write(f"{email}----{password}----{reason}\n")
    try:
        _hotmail_release_alias(email)
    except Exception:
        pass


def is_email_used(email: str) -> bool:
    """检查邮箱是否已被使用或标记为失败。"""
    email_lower = email.strip().lower()
    for fpath in (_EMAILS_USED_FILE, _EMAILS_ERROR_FILE):
        if os.path.exists(fpath):
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split("----")
                        if parts and parts[0].strip().lower() == email_lower:
                            return True
    return False


# ── 页面状态快照 ──

# ── CLI / batch performance knobs (register_cli may mutate) ──
PERF_FLAGS = {
    "fast": False,           # scale down human_sleep
    "sleep_scale": 1.0,      # multiply all human_sleep means
    "skip_debug_io": False,  # skip dump_state / take_screenshot
    "cookie_snapshot": True, # save_cookies_snapshot
    "async_side_effects": True,  # grok2api / cookie snapshot in background
    "browser_reuse": True,   # clear_session instead of quit between accounts
    "browser_recycle_every": 25,  # full quit+recreate after N successful reuses
    # soft | hybrid | hard — hybrid = soft until recycle_every then hard
    "browser_recycle_mode": "soft",
}

_side_effect_pool = None


def _get_side_effect_pool():
    global _side_effect_pool
    if _side_effect_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _side_effect_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sidefx")
    return _side_effect_pool


def configure_perf(**kwargs):
    """Update PERF_FLAGS from CLI. Unknown keys ignored."""
    for k, v in kwargs.items():
        if k in PERF_FLAGS:
            PERF_FLAGS[k] = v


_SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")


def dump_state(page, tag: str = ""):
    """打印当前页面状态：URL、可见按钮文本、输入框类型。"""
    if PERF_FLAGS.get("skip_debug_io"):
        return
    try:
        info = page.run_js("""
            const btns = [...document.querySelectorAll('button')]
                .map(b => b.innerText.trim())
                .filter(t => t)
                .slice(0, 20);
            const inputs = [...document.querySelectorAll('input,textarea')]
                .map(i => (i.type || 'text') + '/' + (i.placeholder || i.name || ''))
                .slice(0, 15);
            return {url: location.href, btns: btns, inputs: inputs};
        """)
        if not info:
            print(f"  [state:{tag}] page context not ready (None)")
            return
        print(f"  [state:{tag}] url: {info.get('url', '?')}")
        print(f"  [state:{tag}] btns: {info.get('btns', [])}")
        print(f"  [state:{tag}] inputs: {info.get('inputs', [])}")
    except Exception as e:
        print(f"  [state:{tag}] dump_state err: {e}")


def take_screenshot(page, tag: str = ""):
    """捕获当前页面截图并保存到 screenshots/ 目录。"""
    if PERF_FLAGS.get("skip_debug_io"):
        return
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%H%M%S")
        path = os.path.join(_SCREENSHOT_DIR, f"{ts}_{tag}.png")
        page.get_screenshot(path=path)
        print(f"  [screenshot] saved: {path}")
    except Exception as e:
        print(f"  [screenshot] err: {e}")


# ── 超时守卫 ──

REGISTER_TIMEOUT = 180  # 单次注册总超时（秒）


class TimeoutError(Exception):
    pass


def check_timeout(start_time: float):
    """检查是否超过总超时时间。"""
    elapsed = time.time() - start_time
    if elapsed > REGISTER_TIMEOUT:
        raise TimeoutError(f"注册超时 ({REGISTER_TIMEOUT}s, 已用 {elapsed:.0f}s)")


# ── 全量 cookie 保存 ──

_COOKIE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies", "grok")


def save_cookies_snapshot(page, tag: str = "", email: str = ""):
    """保存当前浏览器上下文的全量 cookie 快照。"""
    if not PERF_FLAGS.get("cookie_snapshot", True):
        return
    try:
        browser = _get_browser()
        if not browser:
            return
        os.makedirs(_COOKIE_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        cookies = browser.cookies()
        data = {
            "ts": ts,
            "tag": tag,
            "email": email,
            "url": page.url if page else "",
            "cookies": cookies,
        }
        path = os.path.join(_COOKIE_DIR, f"full_{ts}_{tag}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"  [cookies] saved: {path} ({len(cookies)} cookies)")
    except Exception as e:
        print(f"  [cookies] save err: {e}")


# ── .env 加载 ──


def load_env():
    """从 .env 文件加载环境变量（零依赖）。
    只在 os.environ 中尚未设置该 KEY 时填入（真实环境变量优先）。
    """
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


class RegistrationCancelled(Exception):
    pass


class AccountRetryNeeded(Exception):
    """Soft stuck-state: retry the same account slot without consuming fatal stop."""

    pass


def load_config():
    load_env()
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # Allow "// comment" keys in config.example.json / config.json templates.
            if isinstance(loaded, dict):
                loaded = {
                    k: v
                    for k, v in loaded.items()
                    if not (isinstance(k, str) and (k.startswith("//") or k.startswith("#")))
                }
            config = {**DEFAULT_CONFIG, **loaded}
        except Exception:
            config = DEFAULT_CONFIG.copy()
    return config


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置失败: {e}")


def ensure_stable_python_runtime():
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(
            f"[*] 检测到 Python {sys.version.split()[0]}，自动切换到更稳定的解释器: {candidate}"
        )
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    if sys.version_info >= (3, 14):
        print(
            "[提示] 当前 Python 为 3.14+；若出现 Mail.tm TLS 异常，建议改用 Python 3.12 或 3.13。"
        )


ensure_stable_python_runtime()
warn_runtime_compatibility()

load_config()

EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "turnstilePatch")
)


DUCKMAIL_API_BASE = "https://api.duckmail.sbs"


def get_proxies():
    proxy = config.get("proxy", "")
    if proxy:
        return {"http": proxy, "https": proxy}
    return {}


def get_configured_proxy():
    return str(config.get("proxy", "") or "").strip()


def _thread_proxy_bridge():
    return getattr(_browser_proxy_bridge_local, "bridge", None)


def _set_thread_proxy_bridge(bridge) -> None:
    old = getattr(_browser_proxy_bridge_local, "bridge", None)
    if old is not None and old is not bridge:
        try:
            old.stop()
        except Exception:
            pass
        with _browser_proxy_bridge_lock:
            try:
                _browser_proxy_bridge_all[:] = [b for b in _browser_proxy_bridge_all if b is not old]
            except Exception:
                pass
    _browser_proxy_bridge_local.bridge = bridge
    if bridge is not None:
        with _browser_proxy_bridge_lock:
            if bridge not in _browser_proxy_bridge_all:
                _browser_proxy_bridge_all.append(bridge)


def stop_browser_proxy_bridge() -> None:
    """Stop the current-thread auth proxy bridge (if any)."""
    _set_thread_proxy_bridge(None)


def stop_all_browser_proxy_bridges() -> None:
    """Stop every known bridge (process shutdown)."""
    with _browser_proxy_bridge_lock:
        bridges = list(_browser_proxy_bridge_all)
        _browser_proxy_bridge_all.clear()
    for b in bridges:
        try:
            b.stop()
        except Exception:
            pass
    try:
        _browser_proxy_bridge_local.bridge = None
    except Exception:
        pass


def prepare_browser_proxy(use_proxy: bool = True, log_callback=None):
    """Resolve config proxy for Chromium; start local auth bridge when needed.

    Returns (browser_proxy_url, bridge_or_None).
    """
    if not use_proxy:
        return "", None
    proxy = get_configured_proxy()
    if not proxy:
        return "", None
    try:
        browser_proxy, bridge = resolve_browser_proxy(proxy)
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] 代理桥启动失败，浏览器将不走代理: {exc}")
        return "", None
    if bridge is not None and log_callback:
        log_callback(f"[*] 已为 Chromium 启动本地认证代理桥: {browser_proxy}")
    elif browser_proxy and log_callback:
        log_callback(f"[*] Chromium 代理: {browser_proxy}")
    return browser_proxy, bridge


def get_duckmail_api_key():
    return config.get("duckmail_api_key", "")


def get_cloudflare_api_base():
    return str(config.get("cloudflare_api_base", "") or "").rstrip("/")


def get_cloudflare_api_key():
    return config.get("cloudflare_api_key", "")


def get_cloudflare_auth_mode():
    return str(config.get("cloudflare_auth_mode", "bearer") or "bearer").lower()


def get_cloudflare_path(key, default_path):
    raw = str(config.get(key, default_path) or default_path).strip()
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw


def cloudflare_build_headers(content_type=False):
    headers = {"Content-Type": "application/json"} if content_type else {}
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key:
        if mode == "x-api-key":
            headers["X-API-Key"] = key
        elif mode != "none":
            headers["Authorization"] = f"Bearer {key}"
    return headers


def cloudflare_apply_auth_params(params=None):
    merged = dict(params or {})
    key = get_cloudflare_api_key()
    mode = get_cloudflare_auth_mode()
    if key and mode == "query-key":
        merged["key"] = key
    return merged


def _pick_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if isinstance(data.get("results"), list):
            return data.get("results")
        if isinstance(data.get("hydra:member"), list):
            return data.get("hydra:member")
        if isinstance(data.get("data"), list):
            return data.get("data")
        if isinstance(data.get("messages"), list):
            return data.get("messages")
        if isinstance(data.get("data"), dict):
            nested = data.get("data")
            if isinstance(nested.get("messages"), list):
                return nested.get("messages")
    return []


def cloudflare_create_temp_address(api_base):
    """适配 cloudflare_temp_email v1.8.x: POST /api/new_address -> {address,jwt}"""
    global _cf_domain_index
    url = f"{api_base}/api/new_address"
    payload = {}
    try:
        # 在多个域名之间轮换，降低单域偶发不收件导致的失败率
        domains = [x.strip() for x in re.split(r"[,，\s]+", str(config.get("defaultDomains", "") or "")) if x.strip()]
        if domains:
            payload["domain"] = domains[_cf_domain_index % len(domains)]
            _cf_domain_index += 1
    except Exception:
        pass
    resp = http_post(url, json=payload, headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare /api/new_address 返回非JSON: {resp.text[:300]}")
    address = data.get("address")
    jwt = data.get("jwt")
    if not address or not jwt:
        raise Exception(f"Cloudflare /api/new_address 缺少 address/jwt: {data}")
    return address, jwt


def get_user_agent():
    return config.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    )


def resolve_grok2api_local_token_file():
    configured = str(config.get("grok2api_local_token_file", "") or "").strip()
    if configured:
        return configured
    return r"D:\注册机\3255d5ee6e702db9220a897df64635a1ec9df644\vendor\grok2api\data\token.json"


def _normalize_sso_token(raw_token):
    token = str(raw_token or "").strip()
    if token.startswith("sso="):
        token = token[4:]
    return token


def add_token_to_grok2api_local_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    token_file = resolve_grok2api_local_token_file()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip()
    if not pool_name:
        pool_name = "ssoBasic"
    os.makedirs(os.path.dirname(token_file), exist_ok=True)
    data = {}
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    pool = data.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token in existing:
        if log_callback:
            log_callback(f"[*] grok2api 本地池已存在 token: {pool_name}")
        return True
    entry = {"token": token, "tags": ["auto-register"], "note": email}
    pool.append(entry)
    data[pool_name] = pool
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 本地池: {pool_name} ({token_file})")
    return True


def add_token_to_grok2api_remote_pool(raw_token, email="", log_callback=None):
    token = _normalize_sso_token(raw_token)
    if not token:
        return False
    base = str(config.get("grok2api_remote_base", "") or "").strip().rstrip("/")
    app_key = str(os.environ.get("GROK2API_APP_KEY") or config.get("grok2api_remote_app_key", "") or "").strip()
    pool_name = str(config.get("grok2api_pool_name", "ssoBasic") or "ssoBasic").strip() or "ssoBasic"
    if not base or not app_key:
        if log_callback:
            log_callback("[Debug] grok2api 远端未配置 base/app_key，跳过")
        return False
    headers = {"Content-Type": "application/json"}
    query = {"app_key": app_key, "auto_nsfw": "true"}
    pool_map = {"ssoBasic": "basic", "ssoSuper": "super"}
    remote_pool = pool_map.get(pool_name, "basic")
    # 优先使用 add 接口，避免全量覆盖远端池
    try:
        add_payload = {"tokens": [token], "pool": remote_pool, "tags": ["auto-register"]}
        resp_add = http_post(
            f"{base}/tokens/add",
            headers=headers,
            params=query,
            json=add_payload,
            timeout=8,
            proxies={},
        )
        resp_add.raise_for_status()
        if log_callback:
            log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({base}/tokens/add)")
        return True
    except Exception as add_exc:
        if log_callback:
            log_callback(f"[Debug] /tokens/add 写入失败，尝试 /tokens 全量模式: {add_exc}")

    # 兜底：旧版全量保存接口
    current = {}
    try:
        resp = http_get(f"{base}/tokens", headers=headers, params=query, timeout=6, proxies={})
        if resp.status_code == 200:
            payload = resp.json()
            current = payload.get("tokens", {}) if isinstance(payload, dict) else {}
    except Exception:
        current = {}
    if not isinstance(current, dict):
        current = {}
    pool = current.get(pool_name)
    if not isinstance(pool, list):
        pool = []
    existing = set()
    for item in pool:
        if isinstance(item, str):
            existing.add(_normalize_sso_token(item))
        elif isinstance(item, dict):
            existing.add(_normalize_sso_token(item.get("token", "")))
    if token not in existing:
        pool.append({"token": token, "tags": ["auto-register"], "note": email})
    current[pool_name] = pool
    resp2 = http_post(f"{base}/tokens", headers=headers, params=query, json=current, timeout=8, proxies={})
    resp2.raise_for_status()
    if log_callback:
        log_callback(f"[+] 已写入 grok2api 远端池: {pool_name} ({base}/tokens)")
    return True


def _add_token_to_grok2api_pools_sync(raw_token, email="", log_callback=None):
    # SSO 账本只写 accounts_cli.txt；不再本地备份 tokens/grok/
    if config.get("grok2api_auto_add_local", True):
        try:
            add_token_to_grok2api_local_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 本地池失败: {exc}")
    if config.get("grok2api_auto_add_remote", False):
        try:
            add_token_to_grok2api_remote_pool(raw_token, email=email, log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 写入 grok2api 远端池失败: {exc}")


def add_token_to_grok2api_pools(raw_token, email="", log_callback=None):
    """Push SSO into grok2api pools. Async by default so register path never blocks on dead :8000."""
    if PERF_FLAGS.get("async_side_effects", True):
        def _job():
            try:
                _add_token_to_grok2api_pools_sync(raw_token, email=email, log_callback=log_callback)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] grok2api side-effect 异常: {exc}")
        try:
            _get_side_effect_pool().submit(_job)
            if log_callback:
                log_callback("[*] grok2api 池写入已异步提交")
            return
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 异步提交失败，同步写入: {exc}")
    _add_token_to_grok2api_pools_sync(raw_token, email=email, log_callback=log_callback)


CHROMIUM_SLIM_FLAGS = [
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-images",
    "--mute-audio",
    "--disable-background-networking",
    "--no-first-run",
]


def create_browser_options(browser_proxy: str | None = None):
    """Build ChromiumOptions. browser_proxy is the --proxy-server value (already de-authed or local bridge)."""
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    for flag in CHROMIUM_SLIM_FLAGS:
        options.set_argument(flag)
    # headless: Turnstile 在无头下成功率可能更低；默认有头。
    headless = bool(config.get("browser_headless", False))
    if headless:
        try:
            options.headless(True)
        except Exception:
            try:
                options.set_argument("--headless=new")
            except Exception:
                options.set_argument("--headless")
        # 无头下仍尽量伪装桌面 UA，降低被拦概率
        ua = (config.get("user_agent") or "").strip()
        if ua:
            try:
                options.set_user_agent(ua)
            except Exception:
                pass
        try:
            options.set_argument("--window-size=1280,900")
        except Exception:
            pass
    if os.path.exists(EXTENSION_PATH):
        # 传统 headless 不支持扩展；headless=new 下尽量加载 turnstilePatch
        try:
            options.add_extension(EXTENSION_PATH)
        except Exception:
            pass
    # Prefer explicit browser_proxy (local auth bridge or stripped URL).
    # start_browser pins the URL on thread-local for the TabPool factory.
    if browser_proxy is not None:
        proxy_arg = str(browser_proxy or "").strip()
    else:
        proxy_arg = str(getattr(_browser_proxy_bridge_local, "proxy_url", "") or "").strip()
    if not proxy_arg:
        # Last-resort no-auth only (auth proxies need prepare_browser_proxy / bridge).
        raw = get_configured_proxy()
        if raw:
            try:
                from urllib.parse import urlparse

                u = urlparse(raw if "://" in raw else f"http://{raw}")
                if u.hostname and not u.username and not u.password:
                    port = u.port or (443 if (u.scheme or "http") == "https" else 80)
                    proxy_arg = f"{u.scheme or 'http'}://{u.hostname}:{port}"
            except Exception:
                proxy_arg = ""
    if proxy_arg:
        try:
            options.set_argument(f"--proxy-server={proxy_arg}")
        except Exception as e:
            print(f"  [proxy] set browser proxy failed: {e}")
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    try:
        return requests.get(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        # 代理不可用时自动回退为直连，避免整个流程直接失败
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.get(url, **_build_request_kwargs(**retry_kwargs))
        raise


def http_post(url, **kwargs):
    try:
        return requests.post(url, **_build_request_kwargs(**kwargs))
    except Exception as exc:
        err = str(exc)
        if "127.0.0.1 port 7890" in err or "Could not connect to server" in err:
            retry_kwargs = dict(kwargs)
            retry_kwargs["proxies"] = {}
            return requests.post(url, **_build_request_kwargs(**retry_kwargs))
        raise


def raise_if_cancelled(cancel_callback=None):
    if cancel_callback and cancel_callback():
        raise RegistrationCancelled("用户停止注册")


def sleep_with_cancel(seconds, cancel_callback=None):
    deadline = time.time() + max(seconds, 0)
    while True:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def human_sleep(mean_seconds, cancel_callback=None, min_seconds=0.0):
    """高斯分布人类化延迟，sigma=mean*0.3，clamp [mean*0.5, mean*2.0]。

    PERF_FLAGS sleep_scale / fast 可压缩批量注册等待。
    min_seconds: 关键步骤下限（不受 fast 压到近 0），用于 Turnstile/表单 settle。
    """
    scale = float(PERF_FLAGS.get("sleep_scale", 1.0) or 1.0)
    if PERF_FLAGS.get("fast"):
        scale = min(scale, 0.15)
    mean_seconds = max(0.0, float(mean_seconds) * scale)
    floor = max(0.0, float(min_seconds or 0.0))
    if mean_seconds < floor:
        mean_seconds = floor
    if mean_seconds <= 0.01:
        raise_if_cancelled(cancel_callback)
        return
    try:
        delay = random.gauss(mean_seconds, mean_seconds * 0.3)
    except Exception:
        delay = mean_seconds
    delay = max(mean_seconds * 0.5, min(mean_seconds * 2.0, delay))
    if delay < floor:
        delay = floor
    sleep_with_cancel(delay, cancel_callback)


def _page_form_snapshot(page):
    """轻量页面快照，供诊断日志使用（不受 skip_debug_io 影响）。"""
    try:
        info = page.run_js(
            r"""
const isVisible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const inputs = Array.from(document.querySelectorAll('input,textarea,select'))
  .filter(isVisible)
  .slice(0, 20)
  .map((i) => {
    const key = i.getAttribute('data-testid') || i.name || i.id || i.placeholder || i.type || i.tagName;
    return String(key || '').slice(0, 40);
  });
const buttons = Array.from(document.querySelectorAll('button,[role="button"]'))
  .filter(isVisible)
  .slice(0, 12)
  .map((b) => String((b.innerText || b.textContent || '').replace(/\s+/g, ' ')).trim().slice(0, 30))
  .filter(Boolean);
const hasProfile = !!(
  document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[name="firstName"], input[autocomplete="first-name"]')
  && document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[name="lastName"], input[autocomplete="family-name"]')
  && document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]')
);
const hasCode = Array.from(document.querySelectorAll(
  'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
)).some((n) => isVisible(n));
const hasCf = !!(
  document.querySelector('input[name="cf-turnstile-response"]')
  || document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey]')
);
return {
  url: location.href,
  inputs,
  buttons,
  hasProfile: !!hasProfile,
  hasCode: !!hasCode,
  hasCf: !!hasCf,
  title: String(document.title || '').slice(0, 80),
};
            """
        )
        return info if isinstance(info, dict) else {"raw": str(info)}
    except Exception as exc:
        return {"error": str(exc)}



def get_domains(api_key=None):
    headers = {}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    resp = http_get(f"{DUCKMAIL_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def create_account(address, password, api_key=None, expires_in=0):
    headers = {"Content-Type": "application/json"}
    key = api_key or get_duckmail_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = {"address": address, "password": password, "expiresIn": expires_in}
    resp = http_post(f"{DUCKMAIL_API_BASE}/accounts", json=data, headers=headers)
    resp.raise_for_status()
    return resp.json()


def get_token(address, password):
    data = {"address": address, "password": password}
    resp = http_post(f"{DUCKMAIL_API_BASE}/token", json=data)
    resp.raise_for_status()
    return resp.json().get("token")


def get_messages(token):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages", headers=headers)
    resp.raise_for_status()
    return resp.json().get("hydra:member", [])


def get_message_detail(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    resp = http_get(f"{DUCKMAIL_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_domains(api_base, api_key=None):
    headers = cloudflare_build_headers(content_type=False)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_domains", "/domains")
    params = cloudflare_apply_auth_params()
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    return _pick_list_payload(resp.json())


def cloudflare_create_account(api_base, address, password, api_key=None, expires_in=0):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    payload = {"address": address, "password": password, "expiresIn": expires_in}
    path = get_cloudflare_path("cloudflare_path_accounts", "/accounts")
    params = cloudflare_apply_auth_params()
    resp = http_post(f"{api_base}{path}", json=payload, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def cloudflare_get_token(api_base, address, password, api_key=None):
    headers = cloudflare_build_headers(content_type=True)
    if api_key and "Authorization" in headers:
        headers["Authorization"] = f"Bearer {api_key}"
    if api_key and "X-API-Key" in headers:
        headers["X-API-Key"] = api_key
    path = get_cloudflare_path("cloudflare_path_token", "/token")
    resp = http_post(
        f"{api_base}{path}",
        json={"address": address, "password": password},
        headers=headers,
        params=cloudflare_apply_auth_params(),
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("token"):
            return data.get("token")
        if isinstance(data.get("data"), dict) and data["data"].get("token"):
            return data["data"].get("token")
    return None


def cloudflare_get_messages(api_base, token):
    headers = {"Authorization": f"Bearer {token}"}
    path = get_cloudflare_path("cloudflare_path_messages", "/messages")
    params = {"limit": 20, "offset": 0}
    params = cloudflare_apply_auth_params(params)
    resp = http_get(f"{api_base}{path}", headers=headers, params=params)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        raise Exception(f"Cloudflare messages 返回非JSON: {resp.text[:300]}")
    return _pick_list_payload(data)


def cloudflare_get_message_detail(api_base, token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    candidates = [
        f"{api_base}/api/mail/{message_id}",
        f"{api_base}{get_cloudflare_path('cloudflare_path_messages', '/messages')}/{message_id}",
    ]
    last_err = None
    for url in candidates:
        try:
            resp = http_get(
                url,
                headers=headers,
                params=cloudflare_apply_auth_params(),
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                return data["data"]
            return data
        except Exception as exc:
            last_err = exc
            continue
    raise Exception(f"Cloudflare 获取邮件详情失败: {last_err}")


YYDS_API_BASE = "https://maliapi.215.im/v1"


def get_yyds_api_key():
    return config.get("yyds_api_key", "")


def get_yyds_jwt():
    return config.get("yyds_jwt", "")


def yyds_get_domains(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/domains", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data.get("data", []) if data.get("success") else []


def yyds_create_account(address=None, domain=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    payload = {}
    if address:
        payload["address"] = address
    if domain:
        payload["domain"] = domain
    elif key or token:
        payload["autoDomainStrategy"] = "prefer_owned"
    resp = http_post(f"{YYDS_API_BASE}/accounts", json=payload, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 创建邮箱失败: {data}")


def yyds_get_token(address, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_post(
        f"{YYDS_API_BASE}/token", json={"address": address}, headers=headers
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("token")
    raise Exception(f"YYDS 获取token失败: {data}")


def yyds_get_messages(address, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(
        f"{YYDS_API_BASE}/messages",
        params={"address": address},
        headers=headers,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {}).get("messages", [])
    return []


def yyds_get_message_detail(message_id, token=None, api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    temp_token = token or jwt or get_yyds_jwt()
    headers = {}
    if temp_token:
        headers["Authorization"] = f"Bearer {temp_token}"
    elif key:
        headers["X-API-Key"] = key
    resp = http_get(f"{YYDS_API_BASE}/messages/{message_id}", headers=headers)
    resp.raise_for_status()
    data = resp.json()
    if data.get("success"):
        return data.get("data", {})
    raise Exception(f"YYDS 获取邮件详情失败: {data}")


def yyds_generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def yyds_pick_domain(api_key=None, jwt=None):
    domains = yyds_get_domains(api_key=api_key, jwt=jwt)
    if not domains:
        raise Exception("YYDS 没有返回任何可用域名")
    private = [d for d in domains if d.get("isVerified") and not d.get("isPublic")]
    if private:
        return private[0]["domain"]
    public = [d for d in domains if d.get("isVerified") and d.get("isPublic")]
    if public:
        return public[0]["domain"]
    verified = [d for d in domains if d.get("isVerified")]
    if verified:
        return verified[0]["domain"]
    raise Exception("YYDS 无已验证域名可用")


def yyds_get_email_and_token(api_key=None, jwt=None):
    key = api_key or get_yyds_api_key()
    token = jwt or get_yyds_jwt()
    if not token and not key:
        raise Exception("YYDS API Key 或 JWT 未配置")
    domain = yyds_pick_domain(api_key=key, jwt=token)
    username = yyds_generate_username(10)
    result = yyds_create_account(
        address=username, domain=domain, api_key=key, jwt=token
    )
    address = result.get("address") or f"{username}@{domain}"
    temp_token = result.get("token")
    if not temp_token:
        temp_token = yyds_get_token(address, api_key=key, jwt=token)
    if not temp_token:
        raise Exception("获取 YYDS token 失败")
    print(f"[*] 已创建 YYDS 邮箱: {address}")
    return address, temp_token


def yyds_get_oai_code(
    token,
    address,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    jwt=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = yyds_get_messages(address, token=token, jwt=jwt)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] YYDS 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            to_addrs = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if address.lower() not in to_addrs:
                continue
            try:
                detail = yyds_get_message_detail(msg_id, token=token, jwt=jwt)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] YYDS 获取邮件详情失败: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] YYDS 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] YYDS 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"YYDS 在 {timeout}s 内未收到验证码邮件")


def generate_username(length=10):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def pick_domain(api_key=None):
    domains = get_domains(api_key=api_key)
    if not domains:
        raise Exception("DuckMail 没有返回任何可用域名")
    private = [d for d in domains if d.get("ownerId")]
    verified_private = [d for d in private if d.get("isVerified")]
    if verified_private:
        return verified_private[0]["domain"]
    public = [d for d in domains if d.get("isVerified")]
    if public:
        return public[0]["domain"]
    raise Exception("DuckMail 无已验证域名可用")


# ──────────────────────── CloudMail (maillab/cloud-mail) ────────────────────────
# API 前缀: /api/（所有接口均挂载在 /api/ 下）
# 认证格式: Authorization: <token>（不带 Bearer 前缀）
# 公开 token 通过 /api/public/genToken 获取（需管理员账号）

def get_cloudmail_url():
    return str(os.environ.get("CLOUDMAIL_URL") or config.get("cloudmail_url", "") or "").rstrip("/")


def get_cloudmail_password():
    return os.environ.get("CLOUDMAIL_PASSWORD") or config.get("cloudmail_password", "")


def get_cloudmail_admin_email():
    return str(os.environ.get("CLOUDMAIL_ADMIN_EMAIL") or config.get("cloudmail_admin_email", "") or "").strip()


def cloudmail_login(url, email, password):
    """POST /api/login -> JWT string"""
    resp = http_post(
        f"{url}/api/login",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            jwt = token_data.get("token")
            if jwt:
                return jwt
    raise Exception(f"CloudMail 登录失败: {str(data)[:200]}")


def cloudmail_register(url, email, password, turnstile_token=""):
    """POST /api/register -> 注册用户+账号"""
    payload = {"email": email, "password": password}
    if turnstile_token:
        payload["token"] = turnstile_token
    resp = http_post(
        f"{url}/api/register",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") != 200:
        raise Exception(f"CloudMail 注册失败: {data.get('message', str(data))}")
    return data


def cloudmail_gen_public_token(url, admin_email, admin_password):
    """POST /api/public/genToken -> 公开 API token (UUID)"""
    resp = http_post(
        f"{url}/api/public/genToken",
        json={"email": admin_email, "password": admin_password},
        headers={"Content-Type": "application/json"},
        proxies={},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code") == 200:
        token_data = data.get("data", {})
        if isinstance(token_data, dict):
            return token_data.get("token")
    raise Exception(f"CloudMail 获取公开 token 失败: {str(data)[:200]}")


def cloudmail_public_email_list(url, public_token, to_email="", size=20):
    """POST /api/public/emailList -> 公开邮件查询（需公开 token，Authorization: <token>）"""
    payload = {"size": size}
    if to_email:
        payload["toEmail"] = to_email
    resp = http_post(
        f"{url}/api/public/emailList",
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": public_token,
        },
        proxies={},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        if data.get("code") == 200:
            return data.get("data", [])
        raise Exception(f"CloudMail 邮件查询失败: {data.get('message', str(data))}")
    return []


def _cloudmail_get_shared_token(force_refresh=False):
    """获取或刷新共享的公开 token（线程安全单例）"""
    global _cloudmail_public_token
    with _cloudmail_public_token_lock:
        if _cloudmail_public_token and not force_refresh:
            return _cloudmail_public_token
        url = get_cloudmail_url()
        admin_email = get_cloudmail_admin_email()
        admin_password = get_cloudmail_password()
        if not url or not admin_email or not admin_password:
            raise Exception("CloudMail 配置不完整")
        token = cloudmail_gen_public_token(url, admin_email, admin_password)
        if not token:
            raise Exception("CloudMail 公开 token 为空")
        _cloudmail_public_token = token
        return token


def cloudmail_get_oai_code(
    dev_token,
    email,
    timeout=300,
    poll_interval=None,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    # 使用配置的 mail_poll_interval，默认 0.3s
    if poll_interval is None:
        poll_interval = max(0.1, float(config.get("mail_poll_interval", 0.3) or 0.3))
    url = get_cloudmail_url()
    if not url:
        raise Exception("CloudMail URL 未配置")
    # 获取共享公开 token（所有线程共用同一个，避免并发覆盖）
    try:
        public_token = _cloudmail_get_shared_token()
    except Exception as exc:
        raise Exception(f"CloudMail 获取公开 token 失败: {exc}")
    if log_callback:
        log_callback("[Debug] CloudMail 公开 token 获取成功")
    deadline = time.time() + timeout
    seen_attempts = {}
    next_resend_at = time.time() + 60
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 60
        # 统一使用 poll_interval（0.3s 短轮询，无需前加速）
        current_interval = poll_interval
        # 用完整邮箱地址查询（公开 API 的 toEmail 需要完整地址）
        try:
            messages = cloudmail_public_email_list(url, public_token, to_email=email, size=20)
        except Exception as exc:
            err_msg = str(exc)
            if log_callback:
                log_callback(f"[Debug] CloudMail 邮件查询失败: {err_msg}")
            # token 失效时，刷新共享 token（加锁，多线程只刷新一次）
            if "token" in err_msg.lower() or "401" in err_msg:
                try:
                    public_token = _cloudmail_get_shared_token(force_refresh=True)
                    if log_callback:
                        log_callback("[Debug] CloudMail 公开 token 已刷新")
                except Exception:
                    pass
            sleep_with_cancel(current_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] CloudMail 本轮邮件数量: {len(messages)}")
        for msg in messages:
            msg_id = msg.get("emailId") or msg.get("id") or msg.get("messageId")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            # 提取邮件内容（公开接口返回 content 字段，为完整 HTML）
            parts = []
            for field in ("content", "text", "textContent", "text_content", "body", "snippet", "intro"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_val = msg.get("html") or msg.get("htmlContent") or msg.get("html_content")
            if isinstance(html_val, str):
                parts.append(re.sub(r"<[^>]+>", " ", html_val))
            elif isinstance(html_val, list):
                for h in html_val:
                    if isinstance(h, str):
                        parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            if log_callback:
                log_callback(f"[Debug] CloudMail 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] CloudMail 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(current_interval, cancel_callback)
    raise Exception(f"CloudMail 在 {timeout}s 内未收到验证码邮件")


# ──────────────────────── Hotmail / Outlook OAuth2 IMAP ────────────────────────
# 导入格式兼容 grok-register：邮箱----密码----ClientID----Token
# 其中 Token 为 Microsoft OAuth2 refresh_token，验证码通过 outlook.office365.com XOAUTH2 IMAP 拉取。

HOTMAIL_TOKEN_ENDPOINTS = [
    # IMAP XOAUTH2 needs an Outlook resource token. Graph Mail.Read tokens can
    # refresh successfully but then fail at IMAP with "authenticated but not connected".
    (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        {"scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"},
    ),
    (
        "https://login.live.com/oauth20_token.srf",
        {"scope": "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"},
    ),
    # Fallbacks for existing refresh tokens that were issued with legacy/default scopes.
    ("https://login.live.com/oauth20_token.srf", {}),
    (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        {
            "scope": (
                "offline_access https://graph.microsoft.com/Mail.Read "
                "https://graph.microsoft.com/User.Read"
            )
        },
    ),
]


def _config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
    return default


def _resolve_project_path(path_value, default_name="mail_credentials.txt"):
    raw = str(path_value or default_name).strip()
    if not raw:
        raw = default_name
    if os.path.isabs(raw):
        return raw
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), raw)


def get_hotmail_accounts_file():
    return _resolve_project_path(config.get("hotmail_accounts_file", "mail_credentials.txt"))


def _hotmail_release_alias(email):
    if not email:
        return
    with _hotmail_selection_lock:
        _hotmail_reserved_aliases.discard(email.strip().lower())


def _hotmail_split_credential_line(line):
    parts = line.rstrip("\n").split("----", 3)
    if len(parts) < 4:
        return None
    email_addr = parts[0].strip()
    password = parts[1].strip()
    client_id = parts[2].strip()
    refresh_token = parts[3].strip()
    if not email_addr or "@" not in email_addr or not client_id or not refresh_token:
        return None
    return {
        "email": email_addr,
        "password": password,
        "client_id": client_id,
        "refresh_token": refresh_token,
    }


def _hotmail_load_accounts(force=False):
    global _hotmail_accounts_cache, _hotmail_accounts_mtime
    path = get_hotmail_accounts_file()
    if not os.path.exists(path):
        raise Exception(f"Hotmail/Outlook 账号文件不存在: {path}")
    mtime = os.path.getmtime(path)
    with _hotmail_accounts_lock:
        if (
            not force
            and _hotmail_accounts_cache is not None
            and _hotmail_accounts_mtime == mtime
        ):
            return _hotmail_accounts_cache
        accounts = []
        seen_emails = set()
        with open(path, "r", encoding="utf-8-sig") as f:
            for line_no, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("//"):
                    continue
                item = _hotmail_split_credential_line(raw)
                if not item:
                    print(f"[Hotmail] 跳过无效账号行 {line_no}: {line[:80]}")
                    continue
                email_key = item["email"].strip().lower()
                if email_key in seen_emails:
                    print(f"[Hotmail] 跳过重复主邮箱行 {line_no}: {item['email']}")
                    continue
                seen_emails.add(email_key)
                item["line_no"] = line_no
                accounts.append(item)
        if not accounts:
            raise Exception(f"Hotmail/Outlook 账号文件无有效记录: {path}")
        _hotmail_accounts_cache = accounts
        _hotmail_accounts_mtime = mtime
        return _hotmail_accounts_cache


def _hotmail_split_email_addr(email_addr):
    raw = str(email_addr or "").strip().lower()
    if "@" not in raw:
        return "", ""
    local, domain = raw.rsplit("@", 1)
    return local, domain


def _hotmail_is_alias_of_main(email_addr, main_email):
    local, domain = _hotmail_split_email_addr(email_addr)
    main_local, main_domain = _hotmail_split_email_addr(main_email)
    if not local or not main_local or domain != main_domain:
        return False
    return local == main_local or local.startswith(main_local + "+")


def _hotmail_iter_tracked_emails():
    """Yield emails already persisted in success/error ledgers."""
    for fpath in (_EMAILS_USED_FILE, _EMAILS_ERROR_FILE):
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    email_addr = line.split("----", 1)[0].strip()
                    if email_addr:
                        yield email_addr
        except Exception:
            continue


def _hotmail_count_consumed_for_main(main_email):
    """Count used/failed/reserved aliases belonging to one Hotmail main mailbox."""
    consumed = set()
    for email_addr in _hotmail_iter_tracked_emails():
        if _hotmail_is_alias_of_main(email_addr, main_email):
            consumed.add(email_addr.strip().lower())
    for email_addr in _hotmail_reserved_aliases:
        if _hotmail_is_alias_of_main(email_addr, main_email):
            consumed.add(email_addr.strip().lower())
    return len(consumed)


def _hotmail_alias_available(alias_email):
    alias_key = alias_email.strip().lower()
    return alias_key and alias_key not in _hotmail_reserved_aliases and not is_email_used(alias_email)


def _hotmail_random_suffix(main_local):
    try:
        configured_len = int(config.get("hotmail_alias_random_length", 8) or 8)
    except Exception:
        configured_len = 8
    # Outlook local-part max is 64 chars; keep suffix valid even for long usernames.
    max_len = max(1, 64 - len(str(main_local or "")) - 1)
    length = max(1, min(configured_len, max_len))
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _hotmail_make_alias(main_email, alias_index, *, randomize=False):
    if alias_index <= 0:
        return main_email
    local, domain = main_email.split("@", 1)
    if randomize:
        return f"{local}+{_hotmail_random_suffix(local)}@{domain}"
    return f"{local}+{alias_index}@{domain}"


def hotmail_get_email_and_token():
    accounts = _hotmail_load_accounts()
    try:
        max_aliases = int(config.get("hotmail_max_aliases_per_account", 5) or 5)
    except Exception:
        max_aliases = 5
    max_aliases = max(1, max_aliases)
    alias_mode = str(config.get("hotmail_alias_mode", "random") or "random").strip().lower()
    random_mode = alias_mode in ("random", "rand", "随机")
    try:
        random_max_attempts = int(config.get("hotmail_alias_random_max_attempts", 200) or 200)
    except Exception:
        random_max_attempts = 200
    random_max_attempts = max(10, random_max_attempts)

    with _hotmail_selection_lock:
        for acc in accounts:
            main_email = acc["email"].strip()
            if "@" not in main_email:
                continue
            if _hotmail_count_consumed_for_main(main_email) >= max_aliases:
                continue

            candidate = None
            # 原邮箱仍优先尝试一次；之后 random 模式使用随机 plus alias。
            if _hotmail_alias_available(main_email):
                candidate = main_email
            elif random_mode:
                for _ in range(random_max_attempts):
                    if _hotmail_count_consumed_for_main(main_email) >= max_aliases:
                        break
                    alias_email = _hotmail_make_alias(main_email, 1, randomize=True)
                    if _hotmail_alias_available(alias_email):
                        candidate = alias_email
                        break
            else:
                for alias_index in range(1, max_aliases):
                    alias_email = _hotmail_make_alias(main_email, alias_index)
                    if _hotmail_alias_available(alias_email):
                        candidate = alias_email
                        break

            if not candidate:
                continue

            alias_key = candidate.lower()
            _hotmail_reserved_aliases.add(alias_key)
            token_key = "hotmail:" + secrets.token_urlsafe(18)
            _hotmail_token_map[token_key] = {
                "account": acc,
                "email": candidate,
                "created_at": time.time(),
            }
            return candidate, token_key
    raise Exception(
        "Hotmail/Outlook 可用别名已耗尽：请增加 hotmail_max_aliases_per_account、"
        "补充 mail_credentials.txt，或清理 emails_used.txt / emails_error.txt"
    )


def _hotmail_get_refresh_lock(email_addr):
    key = email_addr.strip().lower()
    with _hotmail_refresh_locks_lock:
        lock = _hotmail_refresh_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _hotmail_refresh_locks[key] = lock
        return lock


def _hotmail_update_refresh_token_file(email_addr, new_refresh_token, log_callback=None):
    path = get_hotmail_accounts_file()
    if not new_refresh_token or not os.path.exists(path):
        return
    with _hotmail_accounts_lock:
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()
            changed = False
            out_lines = []
            for raw in lines:
                item = _hotmail_split_credential_line(raw)
                if item and item["email"].lower() == email_addr.lower():
                    newline = (
                        f"{item['email']}----{item['password']}----"
                        f"{item['client_id']}----{new_refresh_token}\n"
                    )
                    out_lines.append(newline)
                    changed = True
                else:
                    out_lines.append(raw)
            if changed:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(out_lines)
                global _hotmail_accounts_mtime
                _hotmail_accounts_mtime = os.path.getmtime(path)
                if log_callback:
                    log_callback(f"[*] Hotmail refresh_token 已回写: {email_addr}")
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Hotmail refresh_token 回写失败: {exc}")


def hotmail_refresh_access_token(account, log_callback=None):
    email_addr = account["email"]
    lock = _hotmail_get_refresh_lock(email_addr)
    with lock:
        refresh_token = account.get("refresh_token", "")
        last_error = None
        for url, extra in HOTMAIL_TOKEN_ENDPOINTS:
            try:
                data = {
                    "client_id": account["client_id"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    **extra,
                }
                resp = http_post(url, data=data, timeout=30)
                try:
                    token_data = resp.json()
                except Exception:
                    token_data = {}
                access_token = token_data.get("access_token")
                if access_token:
                    new_refresh = token_data.get("refresh_token") or refresh_token
                    if new_refresh and new_refresh != refresh_token:
                        account["refresh_token"] = new_refresh
                        _hotmail_update_refresh_token_file(
                            email_addr, new_refresh, log_callback=log_callback
                        )
                    if log_callback:
                        log_callback(f"[*] Hotmail OAuth2 access_token 刷新成功: {email_addr}")
                    return access_token
                last_error = token_data.get("error_description") or token_data.get("error") or resp.text[:200]
            except Exception as exc:
                last_error = exc
                continue
        raise Exception(f"Hotmail OAuth2 refresh 失败: {last_error}")


def _hotmail_decode_header(value):
    if not value:
        return ""
    try:
        from email.header import decode_header, make_header

        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _hotmail_message_body(msg):
    import html as html_lib
    import re as re_lib

    def decode_part(part):
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="ignore")

    if msg.is_multipart():
        text_body = ""
        html_body = ""
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" and not text_body:
                text_body = decode_part(part)
            elif content_type == "text/html" and not html_body:
                html_body = decode_part(part)
        if text_body:
            return text_body
        return re_lib.sub(r"\s+", " ", re_lib.sub(r"<[^>]+>", " ", html_lib.unescape(html_body))).strip()
    return decode_part(msg)


def _hotmail_get_imap_hosts():
    raw = config.get("hotmail_imap_hosts", "outlook.office365.com,imap-mail.outlook.com")
    if isinstance(raw, (list, tuple)):
        hosts = [str(x).strip() for x in raw if str(x).strip()]
    else:
        hosts = [x.strip() for x in re.split(r"[,，\s]+", str(raw or "")) if x.strip()]
    out = []
    for host in hosts or ["outlook.office365.com", "imap-mail.outlook.com"]:
        if host not in out:
            out.append(host)
    return out


def _hotmail_mail_fetch_modes():
    """收码通道顺序。默认 rest,imap：REST 可绕开 'authenticated but not connected'。"""
    raw = config.get("hotmail_mail_fetch_modes", "rest,imap")
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip().lower() for x in raw if str(x).strip()]
    else:
        # 注意: 用 \s 表示空白；不要写成 \\s（会把字母 s 当分隔符，rest 被拆成 re/t）
        items = [x.strip().lower() for x in re.split(r"[,，\s]+", str(raw or "")) if x.strip()]
    allowed = {"rest", "outlook_rest", "imap"}
    out = []
    for item in items or ["rest", "imap"]:
        if item == "outlook_rest":
            item = "rest"
        if item in allowed and item not in out:
            out.append(item)
    return out or ["rest", "imap"]


def _hotmail_parse_iso_ts(value):
    """Parse Outlook ReceivedDateTime (…Z or +00:00) to epoch ms; 0 on failure."""
    if not value:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        # 2026-07-11T16:47:54Z
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # datetime.fromisoformat handles +00:00
        dt = datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _hotmail_rest_get_code(mailbox_email, target_email, access_token, log_callback=None):
    """通过 Outlook Office REST 拉最近邮件并提取验证码。

    很多 consumer Hotmail 账号 OAuth 能过 IMAP AUTH，但 SELECT 报
    'User is authenticated but not connected'；REST 通常仍可用。
    """
    try:
        recent_seconds = int(config.get("hotmail_recent_seconds", 900) or 900)
    except Exception:
        recent_seconds = 900
    try:
        last_n = int(config.get("hotmail_imap_last_n", 30) or 30)
    except Exception:
        last_n = 30
    last_n = max(5, min(50, last_n))
    require_recipient = _config_bool(
        config.get("hotmail_require_recipient_match", True), default=True
    )
    filter_after_ts = int((time.time() - max(60, recent_seconds)) * 1000)
    target_lower = (target_email or "").strip().lower()
    keywords = ["x.ai", "xai", "grok", "verification", "code", "confirm", "验证码", "确认", "confirmation"]

    # $orderby 含空格，必须编码；否则 urllib/部分客户端会拒收
    query = (
        f"$top={last_n}"
        f"&$select=Subject,ReceivedDateTime,ToRecipients,CcRecipients,BodyPreview,Body,From"
        f"&$orderby=ReceivedDateTime%20desc"
    )
    bases = (
        "https://outlook.office.com/api/v2.0/me/messages",
        "https://outlook.office.com/api/v2.0/me/mailfolders/inbox/messages",
        "https://outlook.office365.com/api/v2.0/me/messages",
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    last_error = None
    messages = []
    for base in bases:
        url = f"{base}?{query}"
        try:
            if log_callback:
                log_callback(f"[Debug] Hotmail/Outlook REST 拉取: {base.split('/')[2]} user={mailbox_email}")
            resp = http_get(url, headers=headers, timeout=30)
            if resp.status_code == 401:
                raise Exception(f"Outlook REST 401 unauthorized: {(resp.text or '')[:160]}")
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}: {(resp.text or '')[:160]}"
                if log_callback:
                    log_callback(f"[Debug] Hotmail/Outlook REST 失败: {last_error}")
                continue
            try:
                payload = resp.json()
            except Exception as exc:
                last_error = f"invalid json: {exc}"
                continue
            messages = payload.get("value") or []
            if messages:
                break
        except Exception as exc:
            last_error = str(exc)
            if log_callback:
                log_callback(f"[Debug] Hotmail/Outlook REST 异常: {exc}")
            continue

    if not messages:
        if last_error:
            raise Exception(f"Outlook REST 无可用邮件: {last_error}")
        return None

    for msg in messages:
        recv_ms = _hotmail_parse_iso_ts(msg.get("ReceivedDateTime"))
        if recv_ms and recv_ms < filter_after_ts:
            continue

        subject = str(msg.get("Subject") or "")
        sender = ""
        try:
            sender = str(((msg.get("From") or {}).get("EmailAddress") or {}).get("Address") or "")
        except Exception:
            sender = ""
        recipients = []
        for key in ("ToRecipients", "CcRecipients"):
            for item in msg.get(key) or []:
                try:
                    addr = str(((item or {}).get("EmailAddress") or {}).get("Address") or "").strip()
                except Exception:
                    addr = ""
                if addr:
                    recipients.append(addr.lower())
        recipient_blob = " ".join(recipients)
        recipient_matched = (not target_lower) or (target_lower in recipient_blob)
        if require_recipient and not recipient_matched:
            continue

        body_obj = msg.get("Body") or {}
        body_content = str(body_obj.get("Content") or "")
        if str(body_obj.get("ContentType") or "").lower() == "html":
            body_content = re.sub(r"<[^>]+>", " ", body_content)
        preview = str(msg.get("BodyPreview") or "")
        combined = f"{subject}\n{sender}\n{recipient_blob}\n{preview}\n{body_content}"
        combined_lower = combined.lower()
        if not any(kw in combined_lower for kw in keywords):
            # subject 本身常带 "ABC-DEF xAI confirmation code"
            if "xai" not in subject.lower() and "confirmation" not in subject.lower():
                continue
        code = extract_verification_code(combined, subject)
        if code:
            if log_callback:
                log_callback(
                    f"[*] Hotmail/Outlook REST 提取到验证码: {code} "
                    f"(to={target_email or mailbox_email})"
                )
            return code
    return None


def _hotmail_manual_prompt(mailbox_email, target_email, log_callback=None, prompted=False):
    """给那些 IMAP 拉不到码的账号提供的人工兜底：stdin 读你贴的内容/码。

    你在浏览器登录 mailbox 邮箱后,看到 x.ai 发给 target_email(alias) 的验证码邮件,
    把整封邮件正文或单把码(4-8 位数字 / XXX-XXX)贴进来回车即可。
    返回提取出的验证码或你输入的原始非空串(当码用);空/EOF 返回 ""。
    """
    try:
        prefix = "[*] " if not prompted else "[!] "
        sys.stdout.write(
            f"\n{prefix}=== 人工收码 ===\n"
            f"  邮箱: {mailbox_email}\n"
            f"  本次注册使用别名: {target_email}\n"
            f"  请去 https://outlook.live.com 用该邮箱登录,\n"
            f"  找到 x.ai / xAI / Grok 发来的验证码邮件,\n"
            f"  把整封邮件内容或验证码本身粘贴到这里后回车:\n"
            f"> "
        )
        sys.stdout.flush()
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return ""
    if line is None:
        return ""
    line = line.strip()
    if not line:
        return ""
    code = extract_verification_code(line)
    if code:
        if log_callback:
            log_callback(f"[*] manual 提取到验证码: {code}")
        return code
    if log_callback:
        log_callback(f"[*] manual 未匹配正则,把你输入当码用: {line[:40]}")
    return line


def _hotmail_imap_get_code(mailbox_email, target_email, access_token, log_callback=None, host=None):
    import email as email_lib
    import imaplib
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    try:
        recent_seconds = int(config.get("hotmail_recent_seconds", 900) or 900)
    except Exception:
        recent_seconds = 900
    try:
        last_n = int(config.get("hotmail_imap_last_n", 30) or 30)
    except Exception:
        last_n = 30
    require_recipient = _config_bool(
        config.get("hotmail_require_recipient_match", True), default=True
    )
    filter_after_ts = int((time.time() - max(60, recent_seconds)) * 1000)
    target_lower = (target_email or "").strip().lower()
    keywords = ["x.ai", "xai", "grok", "verification", "code", "confirm", "验证码", "确认"]

    host = host or "outlook.office365.com"
    if log_callback:
        log_callback(f"[Debug] Hotmail/Outlook IMAP 连接: host={host} user={mailbox_email}")
    imap = imaplib.IMAP4_SSL(host, 993, timeout=45)
    auth_string = f"user={mailbox_email}\x01auth=Bearer {access_token}\x01\x01"
    imap.authenticate("XOAUTH2", lambda _: auth_string.encode())
    try:
        imap.select("INBOX")
        status, data = imap.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return None
        msg_ids = data[0].split()[-max(1, last_n):]
        for mid in reversed(msg_ids):
            _, msg_data = imap.fetch(mid, "(RFC822)")
            if not msg_data or not msg_data[0] or not isinstance(msg_data[0][1], bytes):
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])

            date_str = msg.get("Date")
            if date_str:
                try:
                    dt = parsedate_to_datetime(date_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if int(dt.timestamp() * 1000) < filter_after_ts:
                        continue
                except Exception:
                    pass

            subject = _hotmail_decode_header(msg.get("Subject", ""))
            sender = _hotmail_decode_header(msg.get("From", ""))
            recipient_blob = " ".join(
                _hotmail_decode_header(msg.get(h, ""))
                for h in (
                    "To",
                    "Cc",
                    "Delivered-To",
                    "X-Original-To",
                    "Original-Recipient",
                    "Envelope-To",
                )
            ).lower()
            recipient_matched = not target_lower or target_lower in recipient_blob
            if require_recipient and not recipient_matched:
                continue

            body = _hotmail_message_body(msg)
            combined = f"{subject}\n{sender}\n{recipient_blob}\n{body}"
            combined_lower = combined.lower()
            if not any(kw in combined_lower for kw in keywords):
                continue
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Hotmail/Outlook 从邮件中提取到验证码: {code}")
                return code
        return None
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass


def hotmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    token_info = _hotmail_token_map.get(dev_token)
    if not token_info:
        raise Exception("Hotmail/Outlook dev_token 无效或已过期")
    account = token_info["account"]
    mailbox_email = account["email"]
    try:
        configured_interval = float(config.get("hotmail_poll_interval", 2) or 2)
    except Exception:
        configured_interval = 2.0
    # 实跑：验证码常在 5–8s 到达；首轮立刻查，之后从 poll 起步并小幅退避
    base_interval = max(0.8, configured_interval or float(poll_interval or 2) or 2.0)
    current_interval = 0.0  # first loop: immediate REST pull
    empty_rounds = 0
    deadline = time.time() + timeout
    access_token = None
    next_resend_at = time.time() + 60
    # 默认关人工；REST 可用时不应阻塞 stdin
    manual_mode = _config_bool(config.get("hotmail_manual_code"), default=False)
    manual_prompted = False
    fetch_modes = _hotmail_mail_fetch_modes()
    consecutive_transport_fails = 0

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 60
        try:
            if not access_token:
                access_token = hotmail_refresh_access_token(account, log_callback=log_callback)
            code = None
            transport_errors = []
            transport_ok = False

            for mode in fetch_modes:
                if mode == "rest":
                    try:
                        code = _hotmail_rest_get_code(
                            mailbox_email,
                            email,
                            access_token,
                            log_callback=log_callback,
                        )
                        transport_ok = True
                        if code:
                            break
                        # REST 成功但本轮无匹配码，不必再打 IMAP（省一轮 SSL/AUTH 失败）
                        break
                    except Exception as rest_exc:
                        transport_errors.append(f"rest: {rest_exc}")
                        if log_callback:
                            log_callback(f"[Debug] Hotmail/Outlook REST 失败: {rest_exc}")
                        continue

                if mode == "imap":
                    host_errors = []
                    for imap_host in _hotmail_get_imap_hosts():
                        try:
                            code = _hotmail_imap_get_code(
                                mailbox_email,
                                email,
                                access_token,
                                log_callback=log_callback,
                                host=imap_host,
                            )
                            transport_ok = True
                            # 成功连接但本轮未找到码，不必再换同邮箱另一个 host 重扫。
                            break
                        except Exception as host_exc:
                            host_errors.append(f"{imap_host}: {host_exc}")
                            if log_callback:
                                log_callback(
                                    f"[Debug] Hotmail/Outlook IMAP host 失败: {imap_host}: {host_exc}"
                                )
                            continue
                    if host_errors and not transport_ok:
                        transport_errors.append("imap: " + "; ".join(host_errors))
                    if code:
                        break
                    if transport_ok:
                        break

            if code:
                return code

            if transport_ok:
                consecutive_transport_fails = 0
                if log_callback:
                    log_callback(f"[Debug] Hotmail/Outlook 本轮未找到验证码: {email}")
            else:
                consecutive_transport_fails += 1
                # 全部通道挂掉：manual 兜底；否则抛出让外层刷新 token 后重试
                if manual_mode:
                    code = _hotmail_manual_prompt(
                        mailbox_email,
                        email,
                        log_callback=log_callback,
                        prompted=manual_prompted,
                    )
                    manual_prompted = True
                    if code:
                        return code
                    if log_callback:
                        log_callback(f"[Debug] manual 输入为空,本轮继续等: {email}")
                elif consecutive_transport_fails >= 3:
                    raise Exception(
                        "Hotmail/Outlook 收码通道连续失败: "
                        + ("; ".join(transport_errors) if transport_errors else "unknown")
                    )
                else:
                    # 刷新 token 再试
                    access_token = None
                    if log_callback and transport_errors:
                        log_callback(
                            f"[Debug] Hotmail/Outlook 收码通道失败，将重试: "
                            f"{'; '.join(transport_errors)[:240]}"
                        )
        except Exception as exc:
            # OAuth/通道临时失败时下一轮重新 refresh access_token。
            access_token = None
            if log_callback:
                log_callback(f"[Debug] Hotmail/Outlook 拉取验证码失败: {exc}")
        # 自适应轮询：首轮 0s 立刻查；空轮从 base 起步，上限 5s（邮件通常 <10s 到达）
        empty_rounds += 1
        if empty_rounds <= 1:
            current_interval = base_interval
        else:
            current_interval = min(5.0, base_interval + 0.5 * (empty_rounds - 1))
        sleep_with_cancel(current_interval, cancel_callback)
    raise Exception(f"Hotmail/Outlook 在 {timeout}s 内未收到验证码邮件: {email}")


# ──────────────────────── 公共邮箱工具 ────────────────────────

def get_email_provider():
    return str(config.get("email_provider", "duckmail") or "duckmail").strip().lower()


def get_email_and_token(api_key=None):
    provider = get_email_provider()
    if provider in ("hotmail", "outlook", "outlookmail", "microsoft"):
        return hotmail_get_email_and_token()
    if provider == "yyds":
        return yyds_get_email_and_token(api_key=api_key, jwt=get_yyds_jwt())
    if provider == "cloudmail":
        # CloudMail catch-all 模式：直接生成随机邮箱，无需注册
        # Cloudflare Email Routing 会自动将所有该域名的邮件路由到 Worker
        # 支持英文逗号、中文逗号、空格分隔
        raw = str(config.get("defaultDomains", "") or "")
        domains = [x.strip() for x in re.split(r"[,，\s]+", raw) if x.strip()]
        if not domains:
            raise Exception("CloudMail 需要在 defaultDomains 中配置可用域名")
        global _cf_domain_index
        domain = domains[_cf_domain_index % len(domains)]
        _cf_domain_index += 1
        username = generate_username(10)
        address = f"{username}@{domain}"
        # 返回占位 token（实际不用于邮件查询，邮件查询走公开 API）
        return address, "cloudmail_catch_all"
    if provider == "cloudflare":
        api_base = get_cloudflare_api_base()
        if not api_base:
            raise Exception("Cloudflare API Base 未配置")
        try:
            # cloudflare_temp_email 专用模式
            return cloudflare_create_temp_address(api_base)
        except Exception as primary_exc:
            # 兜底回退到 Mail.tm 风格
            key = api_key or get_cloudflare_api_key()
            domains = cloudflare_get_domains(api_base, api_key=key)
            if not domains:
                raise Exception(f"Cloudflare 创建邮箱失败: {primary_exc}")
            verified = [d for d in domains if d.get("isVerified")]
            target = verified[0] if verified else domains[0]
            domain = target.get("domain")
            if not domain:
                raise Exception("Cloudflare 域名数据格式错误，缺少 domain 字段")
            username = generate_username(10)
            address = f"{username}@{domain}"
            password = secrets.token_urlsafe(12)
            cloudflare_create_account(
                api_base, address, password, api_key=key, expires_in=0
            )
            token = cloudflare_get_token(api_base, address, password, api_key=key)
            if not token:
                raise Exception("获取 Cloudflare 邮箱 token 失败")
            return address, token
    key = api_key or get_duckmail_api_key()
    domain = pick_domain(api_key=key)
    username = generate_username(10)
    address = f"{username}@{domain}"
    password = secrets.token_urlsafe(12)
    create_account(address, password, api_key=key, expires_in=0)
    token = get_token(address, password)
    if not token:
        raise Exception("获取 DuckMail token 失败")
    return address, token


def get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    provider = get_email_provider()
    if provider in ("hotmail", "outlook", "outlookmail", "microsoft"):
        return hotmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "yyds":
        return yyds_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            jwt=get_yyds_jwt(),
            cancel_callback=cancel_callback,
        )
    if provider == "cloudmail":
        return cloudmail_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    if provider == "cloudflare":
        return cloudflare_get_oai_code(
            dev_token,
            email,
            timeout=timeout,
            poll_interval=poll_interval,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=resend_callback,
        )
    return duckmail_get_oai_code(
        dev_token,
        email,
        timeout=timeout,
        poll_interval=poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
    )


def extract_verification_code(text, subject=""):
    if subject:
        match = re.search(r"^([A-Z0-9]{3}-[A-Z0-9]{3})\s+xAI", subject, re.IGNORECASE)
        if match:
            return match.group(1)
    match = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if match:
        return match.group(1)
    patterns = [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def duckmail_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
):
    deadline = time.time() + timeout
    seen_ids = set()
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            messages = get_messages(dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            if email.lower() not in recipients:
                continue
            try:
                detail = get_message_detail(dev_token, msg_id)
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 获取邮件详情失败: {exc}")
                continue
            parts = []
            text_body = detail.get("text") or ""
            if text_body:
                parts.append(text_body)
            html_list = detail.get("html") or []
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            combined = "\n".join(parts)
            subject = detail.get("subject", "")
            if log_callback:
                log_callback(f"[Debug] 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] 从邮件中提取到验证码: {code}")
                return code
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"在 {timeout}s 内未收到验证码邮件")


def cloudflare_get_oai_code(
    dev_token,
    email,
    timeout=180,
    poll_interval=3,
    log_callback=None,
    cancel_callback=None,
    resend_callback=None,
):
    api_base = get_cloudflare_api_base()
    if not api_base:
        raise Exception("Cloudflare API Base 未配置")
    deadline = time.time() + timeout
    # 同一封邮件正文可能延迟可读，允许多次重试解析，避免偶发漏码
    seen_attempts = {}
    next_resend_at = time.time() + 35
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if resend_callback and time.time() >= next_resend_at:
            try:
                resend_callback()
                if log_callback:
                    log_callback("[*] 已触发重新发送验证码")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] 触发重发验证码失败: {exc}")
            next_resend_at = time.time() + 35
        try:
            messages = cloudflare_get_messages(api_base, dev_token)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] Cloudflare 拉取邮件列表失败: {exc}")
            sleep_with_cancel(poll_interval, cancel_callback)
            continue
        if log_callback:
            log_callback(f"[Debug] Cloudflare 本轮邮件数量: {len(messages)}")

        for msg in messages:
            msg_id = msg.get("id") or msg.get("msgid")
            if not msg_id:
                continue
            attempt = int(seen_attempts.get(msg_id, 0))
            if attempt >= 5:
                continue
            seen_attempts[msg_id] = attempt + 1
            recipients = [t.get("address", "").lower() for t in (msg.get("to") or [])]
            msg_addr = str(msg.get("address", "")).lower()
            # 优先匹配目标邮箱；若结构不一致也允许继续解析，避免接口字段漂移导致漏码
            address_matched = True
            if recipients:
                address_matched = email.lower() in recipients
            elif msg_addr:
                address_matched = msg_addr == email.lower()
            if not address_matched and log_callback:
                log_callback(f"[Debug] 跳过疑似非目标邮件 id={msg_id} address={msg_addr} to={recipients}")
                continue
            parts = []
            # 先直接从列表项取内容，避免 detail 接口差异导致漏码
            for field in ("text", "raw", "content", "intro", "body", "snippet"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    parts.append(value)
            html_list = msg.get("html") or []
            if isinstance(html_list, str):
                html_list = [html_list]
            for h in html_list:
                parts.append(re.sub(r"<[^>]+>", " ", h))
            subject = str(msg.get("subject", "") or "")
            combined = "\n".join(parts)
            # 再尝试 detail 接口补全内容
            try:
                detail = cloudflare_get_message_detail(api_base, dev_token, msg_id)
                for field in ("text", "raw", "content", "intro", "body", "snippet"):
                    value = detail.get(field)
                    if isinstance(value, str) and value.strip():
                        combined += "\n" + value
                html_list2 = detail.get("html") or []
                if isinstance(html_list2, str):
                    html_list2 = [html_list2]
                for h in html_list2:
                    combined += "\n" + re.sub(r"<[^>]+>", " ", h)
                if not subject:
                    subject = str(detail.get("subject", "") or "")
            except Exception as exc:
                if log_callback:
                    log_callback(f"[Debug] Cloudflare detail接口失败，改用列表内容解析: {exc}")
            if log_callback:
                log_callback(f"[Debug] Cloudflare 收到邮件: {subject}")
            code = extract_verification_code(combined, subject)
            if code:
                if log_callback:
                    log_callback(f"[*] Cloudflare 从邮件中提取到验证码: {code}")
                return code
            elif log_callback:
                log_callback(f"[Debug] 邮件已解析但未提取到验证码 id={msg_id} attempt={seen_attempts[msg_id]}")
        sleep_with_cancel(poll_interval, cancel_callback)
    raise Exception(f"Cloudflare 在 {timeout}s 内未收到验证码邮件")


def generate_random_birthdate():
    import datetime as dt

    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def set_birth_date(session, log_callback=None):
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(
                f"[Debug] set_birth_date status: {res.status_code}, body: {res.text[:200]}"
            )
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False


def set_tos_accepted(session, log_callback=None):
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False


def encode_grpc_nsfw_settings():
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] update_nsfw status: {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False


def enable_nsfw_for_token(token, cf_clearance="", log_callback=None):
    proxies = get_proxies()
    user_agent = get_user_agent()
    try:
        with requests.Session(impersonate="chrome120", proxies=proxies) as session:
            session.headers.update(
                {
                    "user-agent": user_agent,
                    "cookie": f"sso={token}; sso-rw={token}; cf_clearance={cf_clearance}",
                }
            )
            if not set_tos_accepted(session, log_callback):
                return False, "set_tos_accepted 失败!"
            if not set_birth_date(session, log_callback):
                return False, "set_birth_date 失败!"
            if not update_nsfw_settings(session, log_callback):
                return False, "update_nsfw_settings 失败!"
            return True, "成功开启 NSFW"
    except Exception as e:
        return False, f"异常: {str(e)}"


SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_thread_ctx = threading.local()

from tab_pool import TabPool


def _get_browser():
    return TabPool.get_browser()


def _set_browser(value):
    pass  # TabPool 管理 browser，外部 setter 为 no-op


def _get_page():
    if TabPool.get_browser() is None:
        return None
    return TabPool.get_tab()


def _set_page(value):
    pass  # TabPool 管理 tab，外部 setter 为 no-op


def start_browser(log_callback=None, use_proxy: bool = True):
    last_exc = None
    for attempt in range(1, 5):
        bridge = None
        try:
            browser_proxy, bridge = prepare_browser_proxy(use_proxy=use_proxy, log_callback=log_callback)
            # pin proxy URL for TabPool options factory (callable reuses this URL)
            _browser_proxy_bridge_local.proxy_url = browser_proxy or ""
            _set_thread_proxy_bridge(bridge)
            bridge = None  # ownership transferred to thread-local

            def _factory():
                return create_browser_options(browser_proxy=_browser_proxy_bridge_local.proxy_url or None)

            TabPool.init(_factory, log_callback=log_callback)
            page = TabPool.get_tab()
            if log_callback and attempt > 1:
                log_callback(f"[*] 浏览器第 {attempt} 次启动成功")
            return TabPool.get_browser(), page
        except Exception as exc:
            last_exc = exc
            if bridge is not None:
                try:
                    bridge.stop()
                except Exception:
                    pass
            stop_browser_proxy_bridge()
            if log_callback:
                log_callback(f"[Debug] 浏览器启动失败(第{attempt}/4次): {exc}")
            # 每线程独立浏览器，shutdown 只影响当前线程
            try:
                TabPool.release_tab()
            except Exception:
                pass
            human_sleep(min(1.5 * attempt, 4))
    raise Exception(f"浏览器启动失败，已重试4次: {last_exc}")


def stop_browser():
    """Quit current-thread Chromium (full process exit + del_data)."""
    TabPool.release_tab()
    stop_browser_proxy_bridge()
    try:
        _browser_proxy_bridge_local.proxy_url = ""
    except Exception:
        pass


def _resolved_recycle_mode() -> str:
    mode = str(
        PERF_FLAGS.get("browser_recycle_mode")
        or config.get("browser_recycle_mode")
        or "soft"
    ).strip().lower()
    if mode not in ("soft", "hybrid", "hard"):
        return "soft"
    return mode


def prepare_browser_for_next_account(log_callback=None, force_recycle: bool = False):
    """Between accounts: soft clear_session, hybrid periodic hard, or always hard.

    Modes (PERF_FLAGS.browser_recycle_mode / config):
      soft   — clear_session while reuse allowed; hard only when reuse fails/force
      hybrid — soft until browser_recycle_every, then full quit+recreate
      hard   — full quit+recreate every account

    Returns (browser, page).
    """
    mode = _resolved_recycle_mode()
    reuse_flag = bool(PERF_FLAGS.get("browser_reuse", True)) and not force_recycle and mode != "hard"
    every_raw = PERF_FLAGS.get("browser_recycle_every")
    if every_raw is None:
        every_raw = config.get("browser_recycle_every", 25)
    try:
        every = int(every_raw if every_raw is not None else 25)
    except Exception:
        every = 25
    served = TabPool.served_count()
    # hybrid: force hard recycle when served reaches every (every<=0 → never by count)
    if mode == "hybrid" and every > 0 and served >= every:
        force_recycle = True
        reuse_flag = False
    can_soft = (
        reuse_flag
        and TabPool.get_browser() is not None
        and not force_recycle
        and (
            mode == "soft"
            or mode == "hybrid"  # hybrid only reaches here when served < every
        )
    )
    if can_soft:
        if TabPool.clear_session(log_callback=log_callback):
            TabPool.mark_served()
            return TabPool.get_browser(), _get_page()
    # full recycle
    if log_callback:
        log_callback(
            f"[*] 浏览器完整回收（mode={mode}, reuse={reuse_flag}, served={served}, every={every}）"
        )
    stop_browser()
    return start_browser(log_callback=log_callback)


def shutdown_browser():
    """Quit all tracked Chromium instances."""
    TabPool.shutdown()
    stop_all_browser_proxy_bridges()


def restart_browser(log_callback=None):
    stop_browser()
    return start_browser(log_callback=log_callback)


def refresh_active_page():
    if TabPool.get_browser() is None:
        restart_browser()
    try:
        browser = TabPool.get_browser()
        tabs = browser.tab_ids
        if tabs:
            page = browser.get_tab(tabs[-1])
        else:
            page = browser.new_tab()
        page.refresh()
        TabPool.sync_tab()
    except Exception:
        restart_browser()
    return _get_page()


def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
    page = _get_page()
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if log_callback:
            log_callback("[Debug] 尝试查找“使用邮箱注册”按钮...")

        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text.includes('使用邮箱注册') ||
        lower.includes('signupwithemail') ||
        lower.includes('continuewithemail') ||
        lower.includes('email')
    );
});
if (!target) {
    return false;
}
target.click();
return true;
        """)

        if clicked:
            if log_callback:
                log_callback("[*] 已点击「使用邮箱注册」按钮")
            human_sleep(2, cancel_callback)
            return True

        if log_callback:
            current_url = page.url if page else "none"
            log_callback(f"[Debug] 当前URL: {current_url}")

        human_sleep(1, cancel_callback)

    if log_callback:
        page_html = page.html[:500] if page else "no page"
        log_callback(f"[Debug] 页面内容片段: {page_html}")

    raise Exception("未找到「使用邮箱注册」按钮")


def open_signup_page(log_callback=None, cancel_callback=None):
    """打开注册页。release/restart 后必须重建 browser，禁止对 None page 调 get。"""
    raise_if_cancelled(cancel_callback)
    browser = _get_browser()
    page = _get_page()
    if browser is None or page is None:
        browser, page = start_browser(log_callback=log_callback)
        if log_callback:
            log_callback("[*] 浏览器已启动")

    def _goto_signup(active_page):
        if active_page is None:
            raise RuntimeError("page is None before signup navigation")
        active_page.get(SIGNUP_URL)
        return active_page

    try:
        page = _get_page()
        page = _goto_signup(page)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        try:
            # release_tab 后 _get_page() 会返回 None，必须 start_browser 重建
            TabPool.release_tab()
            browser, page = start_browser(log_callback=log_callback)
            page = _goto_signup(page)
        except Exception as e2:
            if log_callback:
                log_callback(f"[Debug] 重建浏览器后打开失败: {e2}")
            browser, page = restart_browser(log_callback=log_callback)
            page = _get_page()
            if page is None:
                raise Exception(f"打开注册页失败: {e2}") from e2
            page = _goto_signup(page)

    page = _get_page()
    if page is None:
        raise Exception("打开注册页失败: 页面未就绪")
    page.wait.doc_loaded()
    dump_state(page, "signup-loaded")
    take_screenshot(page, "signup")
    # 实跑页已稳定；2s 可压到 1s（保留 min 避免 fast 模式过冲）
    human_sleep(1.0, cancel_callback, min_seconds=0.5)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    click_email_signup_button(
        log_callback=log_callback, cancel_callback=cancel_callback
    )
    dump_state(page, "after-email-signup-click")


# 中间页推进：优先 submit，排除 resend/cancel；与最终注册提交文案分离
_JS_CLICK_INTERMEDIATE_CTA = r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function btnText(node) {
  return (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
}
function isExcluded(t) {
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送') ||
         t.includes('取消') || t.includes('cancel') || t.includes('返回') || t.includes('back') ||
         t.includes('跳过') || t.includes('skip');
}
function isPrimaryAdvance(t) {
  return t.includes('继续') || t.includes('下一步') || t.includes('continue') || t.includes('next') ||
         t.includes('确认邮箱') || t.includes('confirm');
}
function isSoftAdvance(t) {
  return t.includes('同意') || t.includes('agree') || t.includes('accept') || t.includes('确认');
}
const nodes = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"]')).filter((node) => {
  return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const ranked = [];
for (const node of nodes) {
  const t = btnText(node);
  if (!t || isExcluded(t)) continue;
  const isSubmit = String(node.getAttribute('type') || '').toLowerCase() === 'submit';
  if (isPrimaryAdvance(t)) ranked.push([isSubmit ? 0 : 1, t, node]);
  else if (isSoftAdvance(t)) ranked.push([isSubmit ? 2 : 3, t, node]);
}
ranked.sort((a, b) => a[0] - b[0]);
if (!ranked.length) return '';
const hit = ranked[0];
hit[2].focus();
hit[2].click();
return hit[1];
"""

_JS_CLICK_PROFILE_SUBMIT = r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function btnText(node) {
  return (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
}
function isExcluded(t) {
  return t.includes('重新发送') || t.includes('resend') || t.includes('取消') || t.includes('cancel') ||
         t.includes('返回') || t.includes('back');
}
function isFinalSubmit(t) {
  return t.includes('完成注册') || t.includes('创建账户') || t.includes('创建帐号') ||
         t.includes('signup') || t.includes('sign up') || t.includes('createaccount') ||
         t.includes('create account') || t === '注册' || t.includes('create');
}
function isFallbackAdvance(t) {
  return t.includes('继续') || t.includes('下一步') || t.includes('continue') || t.includes('next');
}
const nodes = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
  return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
let fallback = null;
for (const node of nodes) {
  const t = btnText(node);
  if (!t || isExcluded(t)) continue;
  if (isFinalSubmit(t)) {
    node.focus();
    node.click();
    return 'submitted:' + t;
  }
  if (!fallback && isFallbackAdvance(t)) fallback = [node, t];
}
if (fallback) {
  fallback[0].focus();
  fallback[0].click();
  return 'submitted-fallback:' + fallback[1];
}
return 'no-submit-button';
"""


def has_profile_form(log_callback=None):
    """检测资料表单是否已渲染。不刷新页面，避免冲掉当前步骤状态。"""
    page = _get_page()
    if page is None:
        return False
    try:
        return bool(
            page.run_js(
                """
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function pick(sel) {
  return Array.from(document.querySelectorAll(sel)).find((n) => isVisible(n) && !n.disabled) || null;
}
const givenInput = pick('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[name="firstName"], input[autocomplete="first-name"], input[name="first_name"]');
const familyInput = pick('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[name="lastName"], input[name="last_name"]');
const passwordInput = pick('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');
return !!(givenInput && familyInput && passwordInput);
            """
            )
        )
    except Exception:
        return False


def wait_for_profile_form(timeout=20, log_callback=None, cancel_callback=None):
    """验证码提交后等待资料页出现；超时返回 False 并打诊断。"""
    page = _get_page()
    if page is None:
        if log_callback:
            log_callback("[Debug] 等待资料页失败: 页面未就绪")
        return False
    deadline = time.time() + max(2.0, float(timeout or 20))
    last_log = 0.0
    last_click_at = 0.0
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if has_profile_form(log_callback=log_callback):
            if log_callback:
                log_callback("[*] 已进入资料填写页")
            return True
        # 中间页推进：2s 冷却，避免连点
        now = time.time()
        if now - last_click_at >= 2.0:
            try:
                clicked = page.run_js(_JS_CLICK_INTERMEDIATE_CTA)
                if clicked:
                    last_click_at = now
                    if log_callback:
                        log_callback(f"[*] 资料页前置中间步骤已点击: {clicked}")
            except Exception:
                pass
        if log_callback and now - last_log >= 4:
            snap = _page_form_snapshot(page)
            log_callback(
                f"[Debug] 等待资料页... url={snap.get('url','?')} inputs={snap.get('inputs')} "
                f"btns={snap.get('buttons')} hasCode={snap.get('hasCode')} hasCf={snap.get('hasCf')}"
            )
            last_log = now
        human_sleep(0.6, cancel_callback, min_seconds=0.25)
    if log_callback:
        snap = _page_form_snapshot(page)
        log_callback(f"[Debug] 等待资料页超时: {snap}")
    return False


def fill_email_and_submit(timeout=15, log_callback=None, cancel_callback=None):
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    check_timeout(time.time())
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("获取邮箱失败")
    if page is None:
        raise Exception("页面未就绪，无法填写邮箱")
    if log_callback:
        log_callback(f"[*] 已创建邮箱: {email}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        filled = page.run_js(
            """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input) return 'not-ready';
input.focus(); input.click();
// 清空并设置值
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email); else input.value = email;
// 完整事件序列，确保 React 受控组件同步
input.dispatchEvent(new Event('focus', { bubbles: true }));
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
// 验证：值已写入即可（不依赖 checkValidity，部分站点自定义校验会导致误判）
const current = (input.value || '').trim();
if (current === email) return 'filled';
// 兜底：尝试逐字符输入
input.value = '';
input.dispatchEvent(new Event('input', { bubbles: true }));
for (const ch of email) {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
    input.value += ch;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
}
input.dispatchEvent(new Event('change', { bubbles: true }));
if ((input.value || '').trim() === email) return 'filled';
return input.value;
            """,
            email,
        )
        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if filled != "filled":
            if log_callback:
                log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue
        human_sleep(0.8, cancel_callback)
        clicked = page.run_js(
            r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
if (!input || !input.checkValidity() || !(input.value || '').trim()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next')
    );
});
if (!submitButton || submitButton.disabled) return false;
submitButton.click();
return true;
            """
        )
        if clicked:
            if log_callback:
                log_callback(f"[*] 已填写邮箱并点击注册: {email}")
            dump_state(page, "email-submitted")
            take_screenshot(page, "email-submitted")
            return email, dev_token
        human_sleep(0.5, cancel_callback)
    raise Exception("未找到邮箱输入框或注册按钮")


def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
    page = _get_page()
    check_timeout(time.time())
    dump_state(page, "wait-code")
    take_screenshot(page, "wait-code")
    def _resend_code():
        page.run_js(
            r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('resend') || t.includes('再次发送');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """
        )

    try:
        mail_timeout = int(config.get("mail_timeout", timeout) or timeout)
    except Exception:
        mail_timeout = timeout
    try:
        mail_poll_interval = float(config.get("mail_poll_interval", 3) or 3)
    except Exception:
        mail_poll_interval = 3

    code = get_oai_code(
        dev_token,
        email,
        timeout=mail_timeout,
        poll_interval=mail_poll_interval,
        log_callback=log_callback,
        cancel_callback=cancel_callback,
        resend_callback=_resend_code,
    )
    if not code:
        raise Exception("获取验证码失败")
    clean_code = str(code).replace("-", "").strip()
    try:
        code_form_timeout = int(config.get("code_form_timeout", timeout) or timeout)
    except Exception:
        code_form_timeout = timeout
    # 填码+提交阶段用 code_form_timeout；与收信 mail_timeout 分离
    deadline = time.time() + max(20, int(code_form_timeout or timeout or 180))
    code_filled_once = False

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        filled = page.run_js(
            """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);

if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});

if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}

return 'not-ready';
            """,
            clean_code,
        )

        if filled == "not-ready":
            human_sleep(0.5, cancel_callback)
            continue
        if "failed" in str(filled):
            if log_callback:
                log_callback(f"[Debug] 验证码填写失败: {filled}")
            human_sleep(0.5, cancel_callback)
            continue

        # 填表与提交之间等待 React 状态 settle：轮询直到出现可点的提交按钮，
        # 或验证码步骤已自动推进。no-button 不再等同于成功。
        submit_deadline = min(deadline, time.time() + 6)
        outcome = "pending"
        while time.time() < submit_deadline:
            raise_if_cancelled(cancel_callback)
            outcome = page.run_js(
                r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const buttons = Array.from(document.querySelectorAll('button[type=\"submit\"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});

const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\\s+/g, '').toLowerCase();
    return (
        t.includes('确认邮箱') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next')
    );
});

if (btn) {
    btn.focus();
    btn.click();
    return 'clicked';
}

// 没有可点的提交按钮：判断验证码步骤是否已推进（自动提交场景）
const codeInputs = Array.from(document.querySelectorAll(
  'input[data-input-otp=\"true\"], input[name=\"code\"], input[autocomplete=\"one-time-code\"], input[inputmode=\"numeric\"], input[inputmode=\"text\"]'
)).filter((n) => isVisible(n) && !n.disabled && Number(n.maxLength || 6) > 1);
const otpBoxes = Array.from(document.querySelectorAll('input')).filter((n) => {
    if (!isVisible(n) || n.disabled) return false;
    const ml = Number(n.maxLength || 0);
    return ml === 1 || String(n.autocomplete || '').toLowerCase() === 'one-time-code';
});
const hasCodeInput = codeInputs.length > 0 || otpBoxes.length > 0;
if (!hasCodeInput) return 'advanced';

// 仍停留在验证码步骤且无按钮：对已填输入按 Enter 兜底触发提交
const target = otpBoxes.length ? otpBoxes[otpBoxes.length - 1] : codeInputs[codeInputs.length - 1];
if (target) {
    target.focus();
    for (const type of ['keydown', 'keyup']) {
        target.dispatchEvent(new KeyboardEvent(type, { bubbles: true, key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }));
    }
}
return 'pending';
                """
            )
            if outcome in ("clicked", "advanced"):
                break
            human_sleep(0.4, cancel_callback)

        code_filled_once = True
        # wait 预算绑定外层 deadline，避免多次假 advanced 吃光超时
        wait_budget = max(2.0, min(8.0, deadline - time.time() - 1.0))
        if wait_budget < 2.0:
            break

        if outcome == "clicked":
            if log_callback:
                log_callback(f"[*] 已填写验证码并点击提交按钮: {code}")
            human_sleep(1.5, cancel_callback, min_seconds=0.8)
            if wait_for_profile_form(timeout=wait_budget, log_callback=log_callback, cancel_callback=cancel_callback):
                return code
            if log_callback:
                log_callback("[Debug] 验证码已点提交，但资料页尚未出现，继续确认...")
            continue
        if outcome == "advanced":
            if log_callback:
                log_callback(f"[*] 验证码已填写，步骤疑似自动推进: {code}")
            human_sleep(1.0, cancel_callback, min_seconds=0.6)
            if wait_for_profile_form(timeout=wait_budget, log_callback=log_callback, cancel_callback=cancel_callback):
                return code
            if log_callback:
                log_callback("[Debug] advanced 未确认到资料页，当作未推进继续重试")
            human_sleep(0.5, cancel_callback, min_seconds=0.3)
            continue

        if log_callback:
            log_callback(f"[Debug] 验证码已填写但提交未确认(outcome={outcome})，重试")
        human_sleep(0.5, cancel_callback, min_seconds=0.25)

    snap = _page_form_snapshot(page)
    if code_filled_once:
        # 码已写入页面，失败属于推进/资料页，不是“没拿到验证码”
        raise Exception(
            f"验证码已填写，但未进入资料页: code={code} snap={snap}"
        )
    raise Exception(f"验证码已获取，但自动填写/提交失败: code={code} snap={snap}")


def getTurnstileToken(log_callback=None, cancel_callback=None):
    page = _get_page()
    if page is None:
        raise Exception("页面未就绪，无法执行 Turnstile")

    try:
        page.run_js(
            "try { if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset(); } catch(e) {}"
        )
    except Exception:
        pass

    for _ in range(0, 20):
        raise_if_cancelled(cancel_callback)
        try:
            token = page.run_js(
                """
try {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) return byInput;
  if (window.turnstile && typeof turnstile.getResponse === 'function') {
    return String(turnstile.getResponse() || '').trim();
  }
  return '';
} catch(e) { return ''; }
                """
            )
            token = str(token or "").strip()
            if len(token) >= 80:
                if log_callback:
                    log_callback(f"[*] Turnstile 已通过，token长度={len(token)}")
                return token

            challenge_input = page.ele("@name=cf-turnstile-response")
            if challenge_input:
                wrapper = challenge_input.parent()
                iframe = None
                try:
                    iframe = wrapper.shadow_root.ele("tag:iframe")
                except Exception:
                    iframe = None
                if iframe:
                    try:
                        iframe.run_js(
                            """
window.dtp = 1;
function getRandomInt(min, max) { return Math.floor(Math.random() * (max - min + 1)) + min; }
let sx = getRandomInt(800, 1200);
let sy = getRandomInt(400, 700);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: sx });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: sy });
                            """
                        )
                    except Exception:
                        pass
                    try:
                        body_sr = iframe.ele("tag:body").shadow_root
                        btn = body_sr.ele("tag:input")
                        if btn:
                            btn.click()
                    except Exception:
                        pass
            else:
                # 兜底：尝试触发页面上可见的 Turnstile 容器
                page.run_js(
                    """
const nodes = Array.from(document.querySelectorAll('div,span,iframe')).filter((n) => {
  const txt = (n.className || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute?.('src') || '');
  return String(txt).toLowerCase().includes('turnstile');
});
if (nodes.length && typeof nodes[0].click === 'function') nodes[0].click();
                    """
                )
        except Exception:
            pass
        human_sleep(1, cancel_callback)

    raise Exception("Turnstile 获取 token 失败")


def build_profile():
    given_name_pool = [
        "Neo", "Ethan", "Liam", "Noah", "Lucas", "Mason", "Ryan", "Leo",
        "Owen", "Aiden", "Elio", "Aron", "Ivan", "Nolan", "Evan", "Kai",
        "Caleb", "Adam", "Ezra", "Miles", "Logan", "Carter", "Hunter", "Jason",
        "Brian", "Dylan", "Alex", "Colin", "Blake", "Gavin", "Henry", "Julian",
        "Kevin", "Louis", "Marcus", "Nathan", "Oscar", "Peter", "Quinn", "Robin",
        "Simon", "Tristan", "Victor", "Wesley", "Xavier", "Yuri", "Zane", "Felix",
        "Aaron", "Damian",
    ]
    family_name_pool = [
        "Lin", "Wang", "Zhao", "Liu", "Chen", "Zhang", "Xu", "Sun",
        "Guo", "He", "Yang", "Wu", "Zhou", "Tang", "Qin", "Shi",
        "Fang", "Peng", "Cao", "Deng", "Fan", "Fu", "Gao", "Han",
        "Hu", "Jiang", "Kong", "Lu", "Ma", "Nie", "Pan", "Qiao",
        "Ren", "Shao", "Tian", "Xie", "Yan", "Yao", "Yu", "Zeng",
        "Bai", "Duan", "Hou", "Jin", "Kang", "Luo", "Mao", "Song",
        "Wei", "Xiong",
    ]
    given_name = random.choice(given_name_pool)
    family_name = random.choice(family_name_pool)
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def fill_profile_and_submit(timeout=None, log_callback=None, cancel_callback=None):
    page = _get_page()
    check_timeout(time.time())
    try:
        cfg_timeout = int(config.get("profile_timeout", 120) or 120)
    except Exception:
        cfg_timeout = 120
    # 显式 timeout 优先生效，便于调试缩短；未传则用 config
    if timeout is None:
        timeout = cfg_timeout
    else:
        timeout = int(timeout)
    timeout = max(30, int(timeout or 120))

    # 先确认资料页已渲染；验证码误判推进时这里会继续等/点中间页
    if not has_profile_form(log_callback=log_callback):
        if log_callback:
            log_callback("[*] 资料表单未就绪，等待页面推进...")
        wait_budget = min(30, max(5, timeout // 4))
        if not wait_for_profile_form(timeout=wait_budget, log_callback=log_callback, cancel_callback=cancel_callback):
            snap = _page_form_snapshot(page)
            raise Exception(f"最终注册页资料填写失败: 未进入资料页 {snap}")

    dump_state(page, "profile-form")
    take_screenshot(page, "profile-form")
    given_name, family_name, password = build_profile()
    # 预热 Turnstile：等 iframe 初始化；fast 模式下仍保留下限
    if log_callback:
        log_callback("[*] 预热 Turnstile...")
    # 有头路径 iframe 约 1s 就绪；过长预热不提升成功率
    human_sleep(1.2, cancel_callback, min_seconds=0.8)
    deadline = time.time() + timeout
    form_filled_once = False
    wait_cf_since = None
    last_cf_retry_at = 0.0
    last_diag_at = 0.0
    not_ready_count = 0
    last_filled = None

    profile_fill_js = r"""
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}

function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.blur();
    return String(input.value || '').trim() === String(value || '').trim();
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"], input[name="firstName"], input[autocomplete="first-name"], input[name="first_name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"], input[name="lastName"], input[name="last_name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]');

if (!givenInput || !familyInput || !passwordInput) {
    const visible = Array.from(document.querySelectorAll('input,textarea,select'))
      .filter(isVisible)
      .slice(0, 12)
      .map((i) => (i.getAttribute('data-testid') || i.name || i.id || i.placeholder || i.type || i.tagName));
    return 'not-ready:' + visible.join('|');
}

const ok1 = setInputValue(givenInput, givenName);
const ok2 = setInputValue(familyInput, familyName);
const ok3 = setInputValue(passwordInput, password);

if (!ok1 || !ok2 || !ok3) return 'fill-failed:' + [ok1, ok2, ok3].join(',');

function btnText(node) {
    return (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
}
function isExcluded(t) {
    return t.includes('重新发送') || t.includes('resend') || t.includes('取消') || t.includes('cancel') ||
           t.includes('返回') || t.includes('back');
}
function isFinalSubmit(t) {
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('创建帐号') ||
           t.includes('signup') || t.includes('sign up') || t.includes('createaccount') ||
           t.includes('create account') || t === '注册' || t.includes('create');
}
function isFallbackAdvance(t) {
    return t.includes('继续') || t.includes('下一步') || t.includes('continue') || t.includes('next');
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
let hasFinal = false;
let hasFallback = false;
for (const node of buttons) {
    const t = btnText(node);
    if (!t || isExcluded(t)) continue;
    if (isFinalSubmit(t)) hasFinal = true;
    else if (isFallbackAdvance(t)) hasFallback = true;
}

// 必须等待 Cloudflare 校验通过后再提交
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    let token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80 && window.turnstile && typeof turnstile.getResponse === 'function') {
        try { token = String(turnstile.getResponse() || '').trim(); } catch (e) {}
    }
    const solvedByToken = token.length >= 80;
    if (!solvedByToken) return 'wait-cloudflare:' + token.length;
}

if (hasFinal || hasFallback) {
    return 'ready-to-submit';
}
return 'filled-no-submit';
"""

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not form_filled_once:
            try:
                filled = page.run_js(
                    profile_fill_js,
                    given_name,
                    family_name,
                    password,
                )
            except Exception as js_exc:
                if log_callback:
                    log_callback(f"[Debug] 资料页 fill JS 异常: {js_exc}")
                human_sleep(0.6, cancel_callback, min_seconds=0.3)
                continue

            last_filled = filled
            filled_s = str(filled or "")

            if isinstance(filled, str) and filled.startswith("wait-cloudflare"):
                form_filled_once = True
                if log_callback:
                    token_len = filled.split(":", 1)[1] if ":" in filled else "0"
                    log_callback(f"[*] 资料已填写，等待 Cloudflare 人机验证通过... 当前token长度={token_len}")
                now = time.time()
                if wait_cf_since is None:
                    wait_cf_since = now
                # 卡住后自动二次复用 Turnstile 组件
                if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                    if log_callback:
                        log_callback("[*] Cloudflare 验证卡住，开始二次复用 Turnstile...")
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            synced = page.run_js(
                                """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                """,
                                token,
                            )
                            if log_callback:
                                log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                    last_cf_retry_at = now
                human_sleep(0.8, cancel_callback, min_seconds=0.4)
                continue

            if filled in ("ready-to-submit", "filled-no-submit"):
                form_filled_once = True
                if log_callback:
                    log_callback(f"[*] 资料字段已写入 ({filled})")
            elif isinstance(filled, str) and filled.startswith("fill-failed"):
                if log_callback:
                    log_callback(f"[Debug] 资料输入失败，重试中... {filled}")
                human_sleep(0.5, cancel_callback, min_seconds=0.25)
                continue
            elif filled_s.startswith("not-ready") or filled in (None, "", "not-ready"):
                not_ready_count += 1
                now = time.time()
                snap = _page_form_snapshot(page)
                if log_callback and (not_ready_count <= 2 or now - last_diag_at >= 5):
                    detail = filled_s.split(":", 1)[1] if ":" in filled_s else ""
                    log_callback(
                        f"[Debug] 资料表单 not-ready#{not_ready_count} detail={detail!r} "
                        f"url={snap.get('url','?')} inputs={snap.get('inputs')} btns={snap.get('buttons')} "
                        f"hasCode={snap.get('hasCode')} hasProfile={snap.get('hasProfile')} hasCf={snap.get('hasCf')}"
                    )
                    last_diag_at = now
                # 仍停在验证码/中间页：尝试继续点提交（复用收紧后的 CTA 选择器）
                try:
                    clicked = page.run_js(_JS_CLICK_INTERMEDIATE_CTA)
                    if clicked and log_callback:
                        log_callback(f"[Debug] not-ready 中间页点击: {clicked}")
                except Exception:
                    pass
                human_sleep(0.6, cancel_callback, min_seconds=0.3)
                continue
            else:
                if log_callback:
                    log_callback(f"[Debug] 资料页未知状态: {filled!r}")
                human_sleep(0.5, cancel_callback, min_seconds=0.25)
                continue

        # 提交前再确认 CF token
        try:
            cf_state = page.run_js(
                r"""
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (!cfPresent) return 'no-cf';
let token = String((cfInput && cfInput.value) || '').trim();
if (token.length < 80 && window.turnstile && typeof turnstile.getResponse === 'function') {
  try { token = String(turnstile.getResponse() || '').trim(); } catch (e) {}
}
if (token.length >= 80) return 'ok:' + token.length;
return 'wait-cloudflare:' + token.length;
                """
            )
        except Exception as cf_exc:
            if log_callback:
                log_callback(f"[Debug] CF 状态检查异常: {cf_exc}")
            human_sleep(0.5, cancel_callback, min_seconds=0.25)
            continue

        if isinstance(cf_state, str) and cf_state.startswith("wait-cloudflare"):
            if log_callback:
                token_len = cf_state.split(":", 1)[1] if ":" in cf_state else "0"
                log_callback(f"[*] 等待 Cloudflare 人机验证通过后再提交... 当前token长度={token_len}")
            now = time.time()
            if wait_cf_since is None:
                wait_cf_since = now
            if now - wait_cf_since >= 12 and now - last_cf_retry_at >= 8:
                if log_callback:
                    log_callback("[*] 提交前仍卡住，自动再次复用 Turnstile...")
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        synced = page.run_js(
                            """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                            """,
                            token,
                        )
                        if log_callback:
                            log_callback(f"[*] Turnstile 二次复用完成，回填长度={synced}")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 二次复用失败: {cf_exc}")
                last_cf_retry_at = now
            human_sleep(0.8, cancel_callback, min_seconds=0.4)
            continue

        try:
            submit_state = page.run_js(_JS_CLICK_PROFILE_SUBMIT)
        except Exception as sub_exc:
            if log_callback:
                log_callback(f"[Debug] 资料提交点击异常: {sub_exc}")
            human_sleep(0.5, cancel_callback, min_seconds=0.25)
            continue

        if isinstance(submit_state, str) and submit_state.startswith("submitted"):
            if log_callback:
                log_callback(f"[*] 已填写注册资料并提交: {given_name} {family_name} ({submit_state})")
            return {"given_name": given_name, "family_name": family_name, "password": password}
        wait_cf_since = None
        if submit_state == "no-submit-button" and log_callback:
            log_callback("[Debug] 未找到提交按钮，继续等待页面稳定...")

        human_sleep(0.5, cancel_callback, min_seconds=0.25)

    snap = _page_form_snapshot(page)
    raise Exception(
        f"最终注册页资料填写失败: last={last_filled!r} not_ready={not_ready_count} snap={snap}"
    )


# ── NSFW 自动开启（从 AaronL725 移植） ──


def generate_random_birthdate():
    """生成随机生日（20-40 岁）。"""
    import datetime as dt
    today = dt.date.today()
    age = random.randint(20, 40)
    birth_year = today.year - age
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    return f"{birth_year}-{birth_month:02d}-{birth_day:02d}T16:00:00.000Z"


def set_birth_date(session, log_callback=None):
    """设置生日。"""
    url = "https://grok.com/rest/auth/set-birth-date"
    new_headers = {
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    payload = {"birthDate": generate_random_birthdate()}
    try:
        res = session.post(url, json=payload, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_birth_date status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"set_birth_date HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_birth_date] 异常: {e}")
        return False, f"set_birth_date 异常: {e}"


def set_tos_accepted(session, log_callback=None):
    """同意 TOS。"""
    url = "https://accounts.x.ai/auth_mgmt.AuthManagement/SetTosAcceptedVersion"
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    data = b"\x00" + struct.pack(">I", len(payload)) + payload
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "connect-es/2.1.1",
        "origin": "https://accounts.x.ai",
        "referer": "https://accounts.x.ai/accept-tos",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] set_tos_accepted status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"set_tos_accepted HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[set_tos_accepted] 异常: {e}")
        return False, f"set_tos_accepted 异常: {e}"


def encode_grpc_nsfw_settings():
    """编码 NSFW 设置 gRPC 请求体。"""
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = b"always_show_nsfw_content"
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def update_nsfw_settings(session, log_callback=None):
    """更新 NSFW 设置。"""
    url = "https://grok.com/auth_mgmt.AuthManagement/UpdateUserFeatureControls"
    data = encode_grpc_nsfw_settings()
    new_headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "origin": "https://grok.com",
        "referer": "https://grok.com/",
    }
    try:
        res = session.post(url, data=data, headers=new_headers, timeout=15)
        if log_callback:
            log_callback(f"[Debug] update_nsfw status: {res.status_code}")
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"update_nsfw_settings HTTP {res.status_code}"
    except Exception as e:
        if log_callback:
            log_callback(f"[update_nsfw] 异常: {e}")
        return False, f"update_nsfw_settings 异常: {e}"


def enable_nsfw(sso_cookie, log_callback=None):
    """使用 sso cookie 自动开启 NSFW（生日 + TOS + NSFW 设置）。"""
    from curl_cffi import requests as cf_requests
    session = cf_requests.Session()
    session.cookies.set("sso", sso_cookie, domain="grok.com")
    session.cookies.set("sso", sso_cookie, domain="accounts.x.ai")

    results = {}

    # 1. 设置生日
    ok, msg = set_birth_date(session, log_callback=log_callback)
    results["set_birth_date"] = {"ok": ok, "msg": msg}

    # 2. 同意 TOS
    ok, msg = set_tos_accepted(session, log_callback=log_callback)
    results["set_tos_accepted"] = {"ok": ok, "msg": msg}

    # 3. 开启 NSFW
    ok, msg = update_nsfw_settings(session, log_callback=log_callback)
    results["update_nsfw_settings"] = {"ok": ok, "msg": msg}

    if log_callback:
        all_ok = all(r["ok"] for r in results.values())
        log_callback(f"[*] NSFW 设置: {'全部成功' if all_ok else results}")

    return results


# ── wait_for_sso_cookie ──


def wait_for_sso_cookie(timeout=120, log_callback=None, cancel_callback=None):
    deadline = time.time() + timeout
    last_seen_names = set()
    last_submit_retry = 0.0
    last_cf_retry_at = 0.0
    final_no_submit_state = ""
    final_no_submit_since = None
    try:
        final_no_submit_timeout = float(
            (config.get("final_page_no_submit_timeout") if isinstance(config, dict) else None) or 45
        )
    except Exception:
        final_no_submit_timeout = 45.0
    final_no_submit_timeout = max(15.0, final_no_submit_timeout)

    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        try:
            page = _get_page()
            if page is None:
                human_sleep(1, cancel_callback)
                continue

            # 仍停留在“完成注册”页时，若 Cloudflare 已通过，周期性重试点击提交
            now = time.time()
            if now - last_submit_retry >= 2.5:
                retried = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const titleHit = !!Array.from(document.querySelectorAll('h1,h2,div,span')).find((el) => {
    const t = (el.textContent || '').replace(/\s+/g, '');
    const lower = t.toLowerCase();
    return t.includes('完成注册') || lower.includes('completeyoursignup') || lower.includes('completesignup');
});
if (!titleHit) return 'not-final-page';

const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput
  || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    const solved = token.length >= 80;
    if (!solved) return 'final-page-wait-cf:' + token.length;
}

function buttonText(node) {
    return [
        node.innerText,
        node.textContent,
        node.getAttribute('value'),
        node.getAttribute('aria-label'),
        node.getAttribute('title'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitBtn = buttons.find((node) => {
    const t = buttonText(node).replace(/\s+/g, '').toLowerCase();
    return t.includes('完成注册') || t.includes('创建账户') || t.includes('signup') || t.includes('createaccount') || t.includes('sign up');
});
if (!submitBtn) {
    const visibleTexts = buttons.map(buttonText).filter(Boolean).slice(0, 8).join(' | ');
    return 'final-page-no-submit:' + visibleTexts;
}
submitBtn.focus();
submitBtn.click();
return 'final-page-clicked-submit';
                    """
                )
                last_submit_retry = now
                if log_callback and (
                    retried == "final-page-clicked-submit"
                    or (isinstance(retried, str) and retried.startswith("final-page-no-submit"))
                ):
                    log_callback(f"[Debug] 最终页状态: {retried}")
                if isinstance(retried, str) and retried.startswith("final-page-no-submit"):
                    if retried != final_no_submit_state:
                        final_no_submit_state = retried
                        final_no_submit_since = now
                    elif final_no_submit_since and now - final_no_submit_since >= final_no_submit_timeout:
                        raise AccountRetryNeeded(
                            f"最终注册页状态 {int(final_no_submit_timeout)}s 未变化且未找到提交按钮，重试当前账号: {retried}"
                        )
                else:
                    final_no_submit_state = ""
                    final_no_submit_since = None
                if log_callback and isinstance(retried, str) and retried.startswith("final-page-wait-cf"):
                    token_len = retried.split(":", 1)[1] if ":" in retried else "0"
                    log_callback(f"[Debug] 最终页状态: final-page-wait-cf, token长度={token_len}")
                    if now - last_cf_retry_at >= 10:
                        if log_callback:
                            log_callback("[*] 最终页 Cloudflare 卡住，自动二次复用 Turnstile...")
                        try:
                            token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                            if token:
                                synced = page.run_js(
                                    """
const token = String(arguments[0] || '').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (!cfInput || !token) return false;
const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
if (nativeSetter) nativeSetter.call(cfInput, token);
else cfInput.value = token;
cfInput.dispatchEvent(new Event('input', { bubbles: true }));
cfInput.dispatchEvent(new Event('change', { bubbles: true }));
return String(cfInput.value || '').trim().length;
                                    """,
                                    token,
                                )
                                if log_callback:
                                    log_callback(f"[*] 最终页 Turnstile 二次复用完成，回填长度={synced}")
                        except Exception as cf_exc:
                            if log_callback:
                                log_callback(f"[Debug] 最终页 Turnstile 二次复用失败: {cf_exc}")
                        last_cf_retry_at = now

            cookies = page.cookies(all_domains=True, all_info=True) or []
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    value = str(item.get("value", "")).strip()
                else:
                    name = str(getattr(item, "name", "")).strip()
                    value = str(getattr(item, "value", "")).strip()

                if name:
                    last_seen_names.add(name)

                if name == "sso" and value:
                    if log_callback:
                        log_callback("[*] 已获取到 sso cookie")
                    return value
        except PageDisconnectedError:
            refresh_active_page()
        except AccountRetryNeeded:
            raise
        except Exception:
            pass

        human_sleep(1, cancel_callback)

    raise Exception(
        f"等待超时：未获取到 sso cookie。已看到 cookies: {sorted(last_seen_names)}"
    )


# ── 登录（非注册）获取 sso ──

LOGIN_URL = "https://accounts.x.ai/login?redirect=grok-com"


def open_login_page(log_callback=None, cancel_callback=None):
    """打开 xAI 登录页，点击「使用邮箱登录」。"""
    browser = _get_browser()
    page = _get_page()
    raise_if_cancelled(cancel_callback)
    if browser is None:
        browser, page = start_browser()
        if log_callback:
            log_callback("[*] 浏览器已启动")
    try:
        page = _get_page()
        page.get(LOGIN_URL)
    except Exception as e:
        if log_callback:
            log_callback(f"[Debug] 打开URL异常: {e}")
        restart_browser()
        page = _get_page()
        page.get(LOGIN_URL)
    page.wait.doc_loaded()
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback(f"[*] 当前URL: {page.url}")
    # 点击「使用邮箱登录」
    clicked = page.run_js("""
const btn = document.querySelector('button[data-testid="continue-with-email"]');
if (btn) { btn.click(); return 'clicked'; }
return 'not-found';
""")
    if clicked != 'clicked':
        raise Exception("未找到「使用邮箱登录」按钮")
    human_sleep(2, cancel_callback)
    if log_callback:
        log_callback("[*] 已点击「使用邮箱登录」")


def fill_login_and_submit(email, password, timeout=120, log_callback=None, cancel_callback=None):
    """两步登录：1.填邮箱点下一步 2.填密码处理Turnstile点登录。"""
    page = _get_page()
    deadline = time.time() + timeout
    last_cf_retry = 0.0

    # ── 步骤1：填邮箱，点「下一步」 ──
    email_submitted = False
    while time.time() < deadline and not email_submitted:
        raise_if_cancelled(cancel_callback)
        state = page.run_js("""
const emailInput = document.querySelector('input[data-testid="email"]');
if (!emailInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = emailInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(emailInput, arguments[0]); else emailInput.value = arguments[0];
emailInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
emailInput.dispatchEvent(new Event('change', {bubbles:true}));
emailInput.blur();
if (String(emailInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-btn';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""", email)
        if state == 'submitted':
            email_submitted = True
            if log_callback:
                log_callback(f"[*] 已填写邮箱并提交: {email}")
        elif state == 'not-ready':
            human_sleep(0.5, cancel_callback)
        elif state == 'btn-disabled':
            human_sleep(0.5, cancel_callback)
        else:
            human_sleep(0.5, cancel_callback)
    if not email_submitted:
        raise Exception("邮箱提交超时")

    # 等密码框出现
    human_sleep(2, cancel_callback)

    # ── 步骤2：填密码，处理 Turnstile，点「登录」 ──
    pw_filled = False
    while time.time() < deadline:
        raise_if_cancelled(cancel_callback)
        if not pw_filled:
            filled = page.run_js("""
const pwInput = document.querySelector('input[data-testid="password"]');
if (!pwInput) return 'not-ready';
const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
const tracker = pwInput._valueTracker;
if (tracker) tracker.setValue('');
if (ns) ns.call(pwInput, arguments[0]); else pwInput.value = arguments[0];
pwInput.dispatchEvent(new InputEvent('input', {bubbles:true, data:arguments[0], inputType:'insertText'}));
pwInput.dispatchEvent(new Event('change', {bubbles:true}));
pwInput.blur();
if (String(pwInput.value||'').trim() !== String(arguments[0]||'').trim()) return 'fill-failed';
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
return 'ready';
""", password)
            if isinstance(filled, str) and filled.startswith('wait-cf'):
                pw_filled = True
                if log_callback:
                    token_len = filled.split(':',1)[1] if ':' in filled else '0'
                    log_callback(f"[*] 已填密码，等待 Turnstile... token长度={token_len}")
                now = time.time()
                if now - last_cf_retry >= 8:
                    try:
                        token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                        if token:
                            page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                            if log_callback:
                                log_callback("[*] Turnstile 已通过，回填完成")
                    except Exception as cf_exc:
                        if log_callback:
                            log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                    last_cf_retry = now
                human_sleep(1, cancel_callback)
                continue
            elif filled == 'ready':
                pw_filled = True
                if log_callback:
                    log_callback("[*] 密码已填写，准备提交")
            elif filled == 'not-ready':
                human_sleep(0.5, cancel_callback)
                continue
            elif filled == 'fill-failed':
                human_sleep(0.5, cancel_callback)
                continue

        # 提交
        state = page.run_js("""
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
const cfPresent = !!cfInput || !!document.querySelector('iframe[src*="turnstile"], div.cf-turnstile');
if (cfPresent) {
    const token = String((cfInput && cfInput.value) || '').trim();
    if (token.length < 80) return 'wait-cf:' + token.length;
}
const btn = document.querySelector('button[data-testid="sign-in-submit"]');
if (!btn) return 'no-submit';
if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'btn-disabled';
btn.click();
return 'submitted';
""")
        if isinstance(state, str) and state.startswith('wait-cf'):
            if log_callback:
                token_len = state.split(':',1)[1] if ':' in state else '0'
                log_callback(f"[*] 等待 Turnstile 通过后再提交... token长度={token_len}")
            now = time.time()
            if now - last_cf_retry >= 8:
                try:
                    token = getTurnstileToken(log_callback=log_callback, cancel_callback=cancel_callback)
                    if token:
                        page.run_js("""
const token = String(arguments[0]||'').trim();
const cfInput = document.querySelector('input[name="cf-turnstile-response"]');
if (cfInput && token) {
    const ns = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (ns) ns.call(cfInput, token); else cfInput.value = token;
    cfInput.dispatchEvent(new Event('input', {bubbles:true}));
    cfInput.dispatchEvent(new Event('change', {bubbles:true}));
}
""", token)
                        if log_callback:
                            log_callback("[*] Turnstile 二次复用完成")
                except Exception as cf_exc:
                    if log_callback:
                        log_callback(f"[Debug] Turnstile 复用失败: {cf_exc}")
                last_cf_retry = now
            human_sleep(1, cancel_callback)
            continue
        elif state == 'submitted':
            if log_callback:
                log_callback("[*] 已点击登录，等待 sso cookie...")
            return
        elif state == 'btn-disabled':
            human_sleep(1, cancel_callback)
            continue
        human_sleep(1, cancel_callback)
    raise Exception("登录提交超时")

def login_and_get_sso(email, password, log_callback=None, cancel_callback=None):
    """完整登录流程：打开页 → 填邮箱密码 → Turnstile → 等 sso cookie。"""
    open_login_page(log_callback=log_callback, cancel_callback=cancel_callback)
    fill_login_and_submit(email, password, log_callback=log_callback, cancel_callback=cancel_callback)
    sso = wait_for_sso_cookie(timeout=120, log_callback=log_callback, cancel_callback=cancel_callback)
    return sso


def export_cpa_after_success(email, password, sso, page=None, log_callback=None):
    """GUI 成功注册后的 CPA xai-*.json 导出 hook。"""
    log = log_callback or (lambda m: print(m, flush=True))
    if not config.get("cpa_export_enabled", True):
        log("[cpa] export disabled, skip")
        return {"ok": False, "skipped": True, "reason": "disabled"}
    if not email or not password:
        log("[cpa] 缺少 email/password，跳过 CPA 导出")
        return {"ok": False, "error": "missing email/password"}
    try:
        import cpa_export
    except Exception as exc:
        log(f"[cpa] 导入 cpa_export 失败: {exc}")
        return {"ok": False, "error": f"import: {exc}"}

    cookies = []
    try:
        cookies = cpa_export.export_cookies_from_page(page) if page is not None else []
    except Exception as exc:
        log(f"[cpa] cookie 导出失败，继续用邮箱密码 mint: {exc}")
        cookies = []
    if cookies:
        log(f"[cpa] 已导出 cookie {len(cookies)} 条供 OIDC mint 注入")

    cpa_cfg = dict(config)
    if _config_bool(config.get("cpa_gui_close_mint_browser", True), default=True):
        # GUI 下优先不残留额外 Chromium：CPA mint 成功后也立即 quit。
        # CLI 仍可通过 cpa_mint_browser_reuse 保持流水线复用。
        cpa_cfg["cpa_mint_browser_reuse"] = False

    # GUI 可多线程注册；CPA mint 使用独立有头浏览器，串行可避免多个 consent 窗口互相抢焦点。
    with _cpa_gui_export_lock:
        try:
            result = cpa_export.export_cpa_xai_for_account(
                email,
                password,
                page=page,
                cookies=cookies,
                sso=sso,
                config=cpa_cfg,
                log_callback=log,
            )
        except Exception as exc:
            log(f"[cpa] CPA 导出异常: {exc}")
            if config.get("cpa_mint_required", False):
                raise
            return {"ok": False, "error": str(exc)}

    if result.get("ok"):
        log(f"[cpa] CPA 格式已导出: {result.get('path')}")
    else:
        log(f"[cpa] CPA 导出失败: {result.get('error') or result}")
    return result


class GrokRegisterGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Grok 注册机")
        self.root.geometry("980x860")
        self.root.minsize(900, 760)
        self.is_running = False
        self.batch_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        self.stop_requested = False
        self.ui_queue = queue.Queue()
        self.accounts_output_file = ""
        self.stats_lock = threading.Lock()
        self._tutorial_window = None
        self.setup_ui()
        self.root.after(200, self._maybe_show_tutorial_on_start)

    def setup_ui(self):
        load_config()
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        config_frame = ttk.LabelFrame(main_frame, text="配置", padding=10)
        config_frame.pack(fill=tk.X, pady=5)
        ttk.Label(config_frame, text="邮箱服务商:").grid(row=0, column=0, sticky=tk.W)
        self.email_provider_var = tk.StringVar(value=config.get("email_provider", "duckmail"))
        self.email_provider_combo = ttk.Combobox(config_frame, textvariable=self.email_provider_var, values=["duckmail", "yyds", "cloudflare", "cloudmail", "hotmail", "outlookmail"], width=12, state="readonly")
        self.email_provider_combo.grid(row=0, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="注册数量:").grid(row=0, column=2, sticky=tk.W, padx=10)
        self.count_var = tk.StringVar(value=str(config.get("register_count", 1)))
        self.count_spinbox = ttk.Spinbox(config_frame, from_=1, to=100, width=8, textvariable=self.count_var)
        self.count_spinbox.grid(row=0, column=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="并发线程:").grid(row=1, column=2, sticky=tk.W, padx=10)
        self.thread_var = tk.StringVar(value=str(config.get("register_threads", 1)))
        self.thread_spinbox = ttk.Spinbox(config_frame, from_=1, to=10, width=8, textvariable=self.thread_var)
        self.thread_spinbox.grid(row=1, column=3, sticky=tk.W, padx=5)
        self.nsfw_var = tk.BooleanVar(value=config.get("enable_nsfw", True))
        self.nsfw_check = ttk.Checkbutton(config_frame, text="注册后开启 NSFW", variable=self.nsfw_var)
        self.nsfw_check.grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=5)
        ttk.Label(config_frame, text="代理（可选）:").grid(row=2, column=0, sticky=tk.W)
        self.proxy_var = tk.StringVar(value=config.get("proxy", ""))
        self.proxy_entry = ttk.Entry(config_frame, textvariable=self.proxy_var, width=30)
        self.proxy_entry.grid(row=2, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="DuckMail API Key:").grid(row=3, column=0, sticky=tk.W)
        self.api_key_var = tk.StringVar(value=config.get("duckmail_api_key", ""))
        self.api_key_entry = ttk.Entry(config_frame, textvariable=self.api_key_var, width=30)
        self.api_key_entry.grid(row=3, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Base:").grid(row=4, column=0, sticky=tk.W)
        self.cloudflare_api_base_var = tk.StringVar(value=config.get("cloudflare_api_base", ""))
        self.cloudflare_api_base_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_base_var, width=30)
        self.cloudflare_api_base_entry.grid(row=4, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare API Key:").grid(row=5, column=0, sticky=tk.W)
        self.cloudflare_api_key_var = tk.StringVar(value=config.get("cloudflare_api_key", ""))
        self.cloudflare_api_key_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_api_key_var, width=30)
        self.cloudflare_api_key_entry.grid(row=5, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="Cloudflare 鉴权模式:").grid(row=6, column=0, sticky=tk.W)
        self.cloudflare_auth_mode_var = tk.StringVar(value=config.get("cloudflare_auth_mode", "bearer"))
        self.cloudflare_auth_mode_combo = ttk.Combobox(
            config_frame,
            textvariable=self.cloudflare_auth_mode_var,
            values=["query-key", "bearer", "x-api-key", "none"],
            width=12,
            state="readonly",
        )
        self.cloudflare_auth_mode_combo.grid(row=6, column=1, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="CF 路径(domains/accounts/token/messages):").grid(row=7, column=0, sticky=tk.W)
        self.cloudflare_paths_var = tk.StringVar(
            value=",".join(
                [
                    config.get("cloudflare_path_domains", "/domains"),
                    config.get("cloudflare_path_accounts", "/accounts"),
                    config.get("cloudflare_path_token", "/token"),
                    config.get("cloudflare_path_messages", "/messages"),
                ]
            )
        )
        self.cloudflare_paths_entry = ttk.Entry(config_frame, textvariable=self.cloudflare_paths_var, width=30)
        self.cloudflare_paths_entry.grid(row=7, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail URL:").grid(row=8, column=0, sticky=tk.W)
        self.cloudmail_url_var = tk.StringVar(value=str(config.get("cloudmail_url", "")))
        self.cloudmail_url_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_url_var, width=30)
        self.cloudmail_url_entry.grid(row=8, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员邮箱:").grid(row=9, column=0, sticky=tk.W)
        self.cloudmail_admin_email_var = tk.StringVar(value=str(config.get("cloudmail_admin_email", "")))
        self.cloudmail_admin_email_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_admin_email_var, width=30)
        self.cloudmail_admin_email_entry.grid(row=9, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="CloudMail 管理员密码:").grid(row=10, column=0, sticky=tk.W)
        self.cloudmail_password_var = tk.StringVar(value=str(config.get("cloudmail_password", "")))
        self.cloudmail_password_entry = ttk.Entry(config_frame, textvariable=self.cloudmail_password_var, width=30, show="*")
        self.cloudmail_password_entry.grid(row=10, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地自动入池:").grid(row=11, column=0, sticky=tk.W)
        self.grok2api_local_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_local", True)))
        self.grok2api_local_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_local_auto_var)
        self.grok2api_local_auto_check.grid(row=11, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 本地 token.json:").grid(row=12, column=0, sticky=tk.W)
        self.grok2api_local_file_var = tk.StringVar(value=str(config.get("grok2api_local_token_file", "")))
        self.grok2api_local_file_entry = ttk.Entry(config_frame, textvariable=self.grok2api_local_file_var, width=30)
        self.grok2api_local_file_entry.grid(row=12, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 池名:").grid(row=13, column=0, sticky=tk.W)
        self.grok2api_pool_name_var = tk.StringVar(value=str(config.get("grok2api_pool_name", "ssoBasic")))
        self.grok2api_pool_name_combo = ttk.Combobox(
            config_frame,
            textvariable=self.grok2api_pool_name_var,
            values=["ssoBasic", "ssoSuper"],
            width=12,
            state="readonly",
        )
        self.grok2api_pool_name_combo.grid(row=13, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端自动入池:").grid(row=14, column=0, sticky=tk.W)
        self.grok2api_remote_auto_var = tk.BooleanVar(value=bool(config.get("grok2api_auto_add_remote", False)))
        self.grok2api_remote_auto_check = ttk.Checkbutton(config_frame, variable=self.grok2api_remote_auto_var)
        self.grok2api_remote_auto_check.grid(row=14, column=1, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 Base:").grid(row=15, column=0, sticky=tk.W)
        self.grok2api_remote_base_var = tk.StringVar(value=str(config.get("grok2api_remote_base", "")))
        self.grok2api_remote_base_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_base_var, width=30)
        self.grok2api_remote_base_entry.grid(row=15, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="grok2api 远端 app_key:").grid(row=16, column=0, sticky=tk.W)
        self.grok2api_remote_key_var = tk.StringVar(value=str(config.get("grok2api_remote_app_key", "")))
        self.grok2api_remote_key_entry = ttk.Entry(config_frame, textvariable=self.grok2api_remote_key_var, width=30)
        self.grok2api_remote_key_entry.grid(row=16, column=1, columnspan=3, sticky=tk.W, padx=5)
        ttk.Label(config_frame, text="默认域名(defaultDomains):").grid(row=17, column=0, sticky=tk.W)
        self.default_domains_var = tk.StringVar(value=str(config.get("defaultDomains", "")))
        self.default_domains_entry = ttk.Entry(config_frame, textvariable=self.default_domains_var, width=30)
        self.default_domains_entry.grid(row=17, column=1, columnspan=3, sticky=tk.W, padx=5)

        ttk.Label(config_frame, text="Hotmail账号文件:").grid(row=18, column=0, sticky=tk.W)
        self.hotmail_accounts_file_var = tk.StringVar(value=str(config.get("hotmail_accounts_file", "mail_credentials.txt")))
        self.hotmail_accounts_file_entry = ttk.Entry(config_frame, textvariable=self.hotmail_accounts_file_var, width=30)
        self.hotmail_accounts_file_entry.grid(row=18, column=1, columnspan=3, sticky=tk.W, padx=5)
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)
        self.start_btn = ttk.Button(btn_frame, text="开始注册", command=self.start_registration)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_registration, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.clear_btn = ttk.Button(btn_frame, text="清空日志", command=self.clear_log)
        self.clear_btn.pack(side=tk.LEFT, padx=5)
        self.help_btn = ttk.Button(btn_frame, text="教程", command=self.show_tutorial)
        self.help_btn.pack(side=tk.LEFT, padx=5)
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_frame, text="状态: ").pack(side=tk.LEFT)
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var, foreground="green")
        self.status_label.pack(side=tk.LEFT)
        self.stats_var = tk.StringVar(value="成功: 0 | 失败: 0")
        ttk.Label(status_frame, textvariable=self.stats_var).pack(side=tk.RIGHT)
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding=5)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=60)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        # 仅当用户当前就在底部时自动跟随，避免手动上滑后被强制拉回底部
        yview = self.log_text.yview()
        at_bottom = bool(yview) and yview[1] >= 0.999
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        if at_bottom:
            self.log_text.see(tk.END)

    def clear_log(self):
        self.log_text.delete(1.0, tk.END)

    def update_stats(self):
        self.stats_var.set(f"成功: {self.success_count} | 失败: {self.fail_count}")

    def _set_running_ui(self, running):
        self.is_running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        self.status_var.set("运行中..." if running else "就绪")
        self.status_label.config(foreground="blue" if running else "green")

    def _maybe_show_tutorial_on_start(self):
        if bool(config.get("show_tutorial_on_start", True)):
            self.show_tutorial()

    def _tutorial_text(self):
        return """欢迎使用 Grok 注册机。建议按下面顺序填写（从最关键到可选）：

【第一步：先确定邮箱后端信息从哪里来】
如果你使用 cloudflare 模式（你当前主要是这套），先去你的临时邮箱服务配置接口查信息：
- 常见接口: /open_api/settings、/api/settings、/health_check
- 重点字段:
  - api_base（对应本工具的 Cloudflare API Base）
  - domains / defaultDomains（可用域名）
  - needAuth（是否需要鉴权）
  - admin_password 或 api_key（需要鉴权时使用）
  - provider.type（应为 cloudflare_temp_email）

【第二步：先填最小可运行配置】
1) 邮箱服务商
- duckmail: 需要 DuckMail API Key
- yyds: 需要 YYDS API Key 或 JWT
- cloudflare: 需要 Cloudflare API Base（cloudflare_temp_email 临时邮箱）
- cloudmail: 需要 CloudMail URL + 密码 + defaultDomains（maillab/cloud-mail 完整邮箱）
- hotmail/outlookmail: 需要 Hotmail账号文件，格式为 邮箱----密码----ClientID----Token

2) Cloudflare API Base（cloudflare 模式必填）
- 示例: https://xxxx.pages.dev
- 填写规则: 与 settings 接口中的 api_base 保持一致

3) 默认域名(defaultDomains)
- 填写你要优先使用的域名
- 支持单域名或逗号分隔多域名轮换
- 示例: a.com,b.com

4) CF 路径(domains/accounts/token/messages)
- 必须与后端真实路由一致
- 常见新路径:
  - /api/domains,/api/new_address,/api/token,/api/mails
- 常见旧路径:
  - /domains,/accounts,/token,/messages

5) Cloudflare API Key / 鉴权模式
- needAuth=false: 通常鉴权模式选 none，key 可留空
- needAuth=true: 按后端要求填 key，并选择 bearer/x-api-key/query-key

6) CloudMail 模式配置（maillab/cloud-mail 部署）
- CloudMail URL: 你的 Worker 地址，如 https://mail.xxx.workers.dev
- CloudMail 管理员邮箱: 管理员账号，如 admin@yourdomain.com
- CloudMail 管理员密码: 管理员密码（用于获取公开 API token 查询邮件）
- defaultDomains: 必须填写可用域名，如 yourdomain.com
- 前提: CloudMail 管理面板需关闭注册验证码（Turnstile），或确保注册接口可用
- 邮件获取: 通过 /api/public/emailList 公开接口查询，自动刷新 token

7) Hotmail/Outlook 模式配置
- Hotmail账号文件默认: mail_credentials.txt
- 每行格式: your@hotmail.com----mailPassword----client-id----refresh-token
- 默认先用原邮箱，后续使用随机 plus alias（如 name+k8s2p9qa@domain）
- 成功、失败、当前运行占用的 alias 都会去重，并通过 outlook.office365.com XOAUTH2 IMAP 收验证码

【第三步：并发与稳定性】
6) 注册数量
- 本次要注册的总账号数

7) 并发线程
- 建议先 3-6 稳定后再升到 10

8) 代理（可选）
- 不填=直连
- 示例: http://127.0.0.1:7890
- 代理不稳会影响验证码和注册稳定性

9) 注册后开启 NSFW
- 勾选后成功账号会自动调用接口开启对应设置

【第四步：grok2api 入池（可选）】
10) grok2api 本地自动入池
- 开启后把成功 sso 自动写入本地池
- 本地 token.json 填 grok2api 的 token.json 路径

11) grok2api 池名
- ssoBasic 或 ssoSuper

12) grok2api 远端自动入池
- 开启后调用远端管理接口自动加 token
- 远端 Base 示例: https://xxx/admin/api
- app_key 按远端服务配置填写

【最后：快速自检】
1) 先设置: 注册数量=1，并发线程=1
2) 点开始后看日志是否出现：
- 已创建邮箱: xxx@你的域名
- Cloudflare/CloudMail 本轮邮件数量: ...
- 从邮件中提取到验证码: ...
3) 若第一步就失败：
- cloudflare 模式: 检查 API Base / CF 路径 / 鉴权模式
- cloudmail 模式: 检查 URL / 密码 / defaultDomains / 注册接口是否可用

提示:
- 点“开始注册”会自动保存当前配置到 config.json。
- 如果关闭了启动教程，可随时点主界面的“教程”按钮重新打开。"""

    def show_tutorial(self):
        if self._tutorial_window is not None and self._tutorial_window.winfo_exists():
            self._tutorial_window.lift()
            self._tutorial_window.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._tutorial_window = win
        win.title("使用教程")
        win.geometry("760x620")
        win.minsize(680, 520)
        win.transient(self.root)

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        txt = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=26)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.insert("1.0", self._tutorial_text())
        txt.config(state=tk.DISABLED)

        footer = ttk.Frame(frame)
        footer.pack(fill=tk.X, pady=(8, 0))

        dont_show_var = tk.BooleanVar(value=not bool(config.get("show_tutorial_on_start", True)))
        chk = ttk.Checkbutton(
            footer,
            text="以后不再自动显示本教程",
            variable=dont_show_var,
        )
        chk.pack(side=tk.LEFT)

        def on_close():
            config["show_tutorial_on_start"] = not bool(dont_show_var.get())
            save_config()
            try:
                win.destroy()
            except Exception:
                pass

        close_btn = ttk.Button(footer, text="关闭", command=on_close)
        close_btn.pack(side=tk.RIGHT, padx=5)
        win.protocol("WM_DELETE_WINDOW", on_close)

    def should_stop(self):
        return self.stop_requested or not self.is_running

    def start_registration(self):
        if self.is_running:
            self.log("[!] 当前已有任务在运行")
            return

        config["email_provider"] = self.email_provider_var.get().strip() or "duckmail"
        config["proxy"] = self.proxy_var.get().strip()
        config["duckmail_api_key"] = self.api_key_var.get().strip()
        config["cloudflare_api_base"] = self.cloudflare_api_base_var.get().strip()
        config["cloudflare_api_key"] = self.cloudflare_api_key_var.get().strip()
        config["cloudflare_auth_mode"] = self.cloudflare_auth_mode_var.get().strip() or "bearer"
        config["cloudmail_url"] = self.cloudmail_url_var.get().strip()
        config["cloudmail_admin_email"] = self.cloudmail_admin_email_var.get().strip()
        config["cloudmail_password"] = self.cloudmail_password_var.get().strip()
        config["grok2api_auto_add_local"] = bool(self.grok2api_local_auto_var.get())
        config["grok2api_local_token_file"] = self.grok2api_local_file_var.get().strip()
        config["grok2api_pool_name"] = self.grok2api_pool_name_var.get().strip() or "ssoBasic"
        config["grok2api_auto_add_remote"] = bool(self.grok2api_remote_auto_var.get())
        config["grok2api_remote_base"] = self.grok2api_remote_base_var.get().strip()
        config["grok2api_remote_app_key"] = self.grok2api_remote_key_var.get().strip()
        config["defaultDomains"] = self.default_domains_var.get().strip()
        config["hotmail_accounts_file"] = self.hotmail_accounts_file_var.get().strip() or "mail_credentials.txt"
        try:
            config["register_threads"] = max(1, min(10, int(self.thread_var.get())))
        except Exception:
            config["register_threads"] = 1
        raw_paths = [x.strip() for x in self.cloudflare_paths_var.get().split(",") if x.strip()]
        if len(raw_paths) >= 4:
            config["cloudflare_path_domains"] = raw_paths[0] if raw_paths[0].startswith("/") else ("/" + raw_paths[0])
            config["cloudflare_path_accounts"] = raw_paths[1] if raw_paths[1].startswith("/") else ("/" + raw_paths[1])
            config["cloudflare_path_token"] = raw_paths[2] if raw_paths[2].startswith("/") else ("/" + raw_paths[2])
            config["cloudflare_path_messages"] = raw_paths[3] if raw_paths[3].startswith("/") else ("/" + raw_paths[3])
        save_config()
        if config["email_provider"] == "cloudflare" and not config["cloudflare_api_base"]:
            self.log("[!] Cloudflare 模式需要先填写 Cloudflare API Base")
            return
        if config["email_provider"] == "cloudmail":
            if not config.get("cloudmail_url"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail URL")
                return
            if not config.get("cloudmail_admin_email"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员邮箱")
                return
            if not config.get("cloudmail_password"):
                self.log("[!] CloudMail 模式需要先填写 CloudMail 管理员密码")
                return
        if config["email_provider"] in ("hotmail", "outlook", "outlookmail", "microsoft"):
            hotmail_path = get_hotmail_accounts_file()
            if not os.path.exists(hotmail_path):
                self.log(f"[!] Hotmail/Outlook 模式账号文件不存在: {hotmail_path}")
                return
        try:
            count = int(self.count_var.get())
        except Exception:
            self.log("[!] 注册数量无效")
            return
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.results = []
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.accounts_output_file = os.path.join(
            os.path.dirname(__file__), f"accounts_{now}.txt"
        )
        self.update_stats()
        self._set_running_ui(True)
        worker_count = max(1, min(config.get("register_threads", 1), count))
        self.log(f"[*] 配置已保存，开始执行。目标数量: {count}，并发线程: {worker_count}")
        self.log(f"[*] 成功账号将实时保存到: {self.accounts_output_file}")
        threading.Thread(
            target=self.run_registration,
            args=(count, worker_count),
            daemon=True,
        ).start()

    def stop_registration(self):
        self.stop_requested = True
        self.log("[!] 用户停止注册")

    def _run_single_registration(self, idx, total, logf):
        email = ""
        dev_token = ""
        code = ""
        mail_ok = False
        max_mail_retry = 3
        for mail_try in range(1, max_mail_retry + 1):
            logf(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
            open_signup_page(log_callback=logf, cancel_callback=self.should_stop)
            logf("[*] 2. 创建邮箱并提交")
            email, dev_token = fill_email_and_submit(log_callback=logf, cancel_callback=self.should_stop)
            logf(f"[*] 邮箱: {email}")
            if get_email_provider() not in ("hotmail", "outlook", "outlookmail", "microsoft"):
                try:
                    with open(os.path.join(os.path.dirname(__file__), "created_mailboxes.txt"), "a", encoding="utf-8") as f:
                        f.write(f"{email}\t{dev_token}\n")
                except Exception:
                    pass
            logf("[*] 3. 拉取验证码")
            try:
                code = fill_code_and_submit(email, dev_token, log_callback=logf, cancel_callback=self.should_stop)
                mail_ok = True
                break
            except Exception as mail_exc:
                msg = str(mail_exc)
                if email:
                    try:
                        mark_error(email, reason=msg[:120])
                    except Exception:
                        pass
                progress_fail = (
                    "未进入资料页" in msg
                    or "验证码已填写" in msg
                )
                mail_miss = (not progress_fail) and (
                    "未收到验证码" in msg
                    or "获取验证码失败" in msg
                    or (
                        "验证码" in msg
                        and any(
                            k in msg
                            for k in (
                                "未收到",
                                "自动填写/提交失败",
                                "IMAP",
                            )
                        )
                    )
                )
                if mail_miss and mail_try < max_mail_retry:
                    logf(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
                    continue
                raise
        if not mail_ok:
            raise Exception("验证码阶段失败，已达到最大重试次数")
        logf(f"[*] 验证码: {code}")
        try:
            logf("[*] 4. 填写资料")
            profile = fill_profile_and_submit(log_callback=logf, cancel_callback=self.should_stop)
            logf(f"[*] 资料已填: {profile.get('given_name')} {profile.get('family_name')}")
            logf("[*] 5. 等待 sso cookie")
            sso = wait_for_sso_cookie(log_callback=logf, cancel_callback=self.should_stop)
            try:
                from cpa_xai.accounts import normalize_sso_cookie

                sso = normalize_sso_cookie(sso)
            except Exception:
                raw = (sso or "").strip()
                sso = raw[1:] if raw.startswith("-") and "eyJ" in raw[:8] else raw
        except Exception as flow_exc:
            if email:
                try:
                    mark_error(email, reason=str(flow_exc)[:120])
                except Exception:
                    pass
            raise
        password = profile.get("password", "") or ""
        result_record = {"email": email, "sso": sso, "profile": profile}
        with self.stats_lock:
            self.results.append(result_record)
            self.success_count += 1
            try:
                from cpa_xai.accounts import format_account_line

                line = format_account_line(email, password, sso)
            except Exception:
                line = f"{email}----{password}----{sso}\n"
            try:
                with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as file_exc:
                logf(f"[Debug] 保存账号文件失败: {file_exc}")
        try:
            mark_used(email, password)
        except Exception:
            pass
        logf(f"[+] 注册成功: {email}")
        add_token_to_grok2api_pools(sso, email=email, log_callback=logf)
        try:
            page = _get_page()
        except Exception:
            page = None
        logf("[cpa] 开始自动导出 CPA xai 认证文件")
        cpa_result = export_cpa_after_success(
            email,
            password,
            sso,
            page=page,
            log_callback=logf,
        )
        result_record["cpa"] = cpa_result

    def _worker_loop(self, worker_id, total, task_queue):
        prefix = f"[T{worker_id}]"
        logf = lambda m: self.log(f"{prefix} {m}")
        try:
            start_browser(log_callback=logf)
            logf("[*] 浏览器已启动")
            while not self.should_stop():
                try:
                    idx = task_queue.get_nowait()
                except queue.Empty:
                    break
                logf(f"--- 开始第 {idx}/{total} 个账号 ---")
                try:
                    self._run_single_registration(idx, total, logf)
                except RegistrationCancelled:
                    logf("[!] 注册被用户停止")
                    break
                except Exception as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    logf(f"[-] 注册失败: {exc}")
                finally:
                    self.update_stats()
                    if self.should_stop():
                        break
                    restart_browser(log_callback=logf)
                    sleep_with_cancel(1, self.should_stop)
        except Exception as exc:
            logf(f"[!] 线程异常: {exc}")
        finally:
            stop_browser()

    def run_registration(self, count, worker_count):
        task_queue = queue.Queue()
        for i in range(1, count + 1):
            task_queue.put(i)
        workers = []
        try:
            start_interval = float(config.get("thread_start_interval", 0.8))
        except Exception:
            start_interval = 0.8
        if start_interval < 0:
            start_interval = 0.0
        for wid in range(1, worker_count + 1):
            t = threading.Thread(target=self._worker_loop, args=(wid, count, task_queue), daemon=True)
            workers.append(t)
            t.start()
            if wid < worker_count and start_interval > 0:
                sleep_with_cancel(start_interval, self.should_stop)
        for t in workers:
            t.join()
        self._set_running_ui(False)
        self.log("[*] 任务结束")

def main():
    root = tk.Tk()
    app = GrokRegisterGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
