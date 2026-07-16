#!/usr/bin/env python3
"""TabPool — per-thread Chromium with proper lifecycle.

Interface:
    TabPool.init(options_factory) → save options factory (no browser yet)
    TabPool.get_tab()             → get/create current thread browser tab
    TabPool.clear_session()       → wipe cookies/storage; keep process warm
    TabPool.release_tab()         → quit current thread browser + drop registry
    TabPool.shutdown()            → quit all known browsers
    cleanup_orphan_drission_chromes() → kill PPID=1 leftover Drission Chromes

Notes:
    - One Chromium per worker thread (cookie isolation).
    - Prefer clear_session() between accounts; release_tab() only on errors / GC.
    - _all_browsers is pruned on release to avoid zombie list growth.
    - Success path reuses the process; orphans usually come from crashed runs
      where quit() never ran and Chrome was reparented to init/launchd (PPID=1).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

LogFn = Callable[[str], None]

# Serialize Chromium process boots across register TabPool + mint standalone.
# Concurrent auto_port / user-data-dir allocation races produce
# "The browser connection fails" and PPID=1 orphans under load.
_chromium_start_lock = threading.Lock()


def chromium_start_lock() -> threading.Lock:
    """Process-wide lock held only while constructing Chromium(...)."""
    return _chromium_start_lock


def display_available() -> bool:
    """True if headed Chromium is likely to connect without Xvfb missing.

    macOS/Windows: always True (no X11 DISPLAY required).
    Linux/other: require non-empty ``DISPLAY`` (e.g. real desktop or ``xvfb-run``).
    Bare ``--headless`` on servers leaves DISPLAY empty; auto headless→headed
    upgrades must refuse rather than spin on "The browser connection fails".
    """
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool((os.environ.get("DISPLAY") or "").strip())


def is_drission_chrome_cmdline(cmd: str) -> bool:
    """True for Drission/register Chrome mains (not Helpers / unrelated Chrome)."""
    if not cmd or "Helper" in cmd:
        return False
    if "remote-debugging-port" not in cmd:
        return False
    return ("autoPortData" in cmd) or ("DrissionPage" in cmd) or ("turnstilePatch" in cmd)


def parse_ps_chrome_rows(ps_text: str) -> list[tuple[int, int, str]]:
    """Parse ``ps -ax -o pid=,ppid=,command=`` style lines → (pid, ppid, cmd)."""
    rows: list[tuple[int, int, str]] = []
    for line in (ps_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2]
        if is_drission_chrome_cmdline(cmd):
            rows.append((pid, ppid, cmd))
    return rows


def cleanup_orphan_drission_chromes(
    *,
    log_callback: LogFn | None = None,
    protect_pids: set[int] | None = None,
    only_ppid_init: bool = True,
    dry_run: bool = False,
    term_grace_sec: float = 1.0,
) -> dict[str, Any]:
    """Kill leftover Drission Chromium mains that were reparented to init/launchd.

    Safety:
      - Only matches Drission/register Chrome mains (autoPortData / turnstilePatch
        + remote-debugging-port), never generic user Chrome profiles.
      - Default only_ppid_init=True → only PPID in {0, 1} (orphans). Live children
        of a healthy register_cli (PPID=python) are left alone.
      - Never signals protect_pids, os.getpid(), or this process's parent.
      - SIGTERM first, then SIGKILL after a short grace.

    Returns dict: scanned, matched, killed, protected_skipped, errors, pids.
    """
    log = log_callback or (lambda _m: None)
    protect = set(protect_pids or ())
    protect.add(os.getpid())
    try:
        protect.add(os.getppid())
    except Exception:
        pass

    result: dict[str, Any] = {
        "scanned": 0,
        "matched": 0,
        "killed": 0,
        "protected_skipped": 0,
        "errors": [],
        "pids": [],
        "dry_run": dry_run,
    }

    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        ps_text = proc.stdout or ""
    except Exception as e:  # noqa: BLE001
        result["errors"].append(f"ps failed: {e}")
        log(f"[browser] orphan cleanup: ps failed: {e}")
        return result

    rows = parse_ps_chrome_rows(ps_text)
    result["scanned"] = len(rows)
    init_ppids = {0, 1}

    for pid, ppid, cmd in rows:
        if only_ppid_init and ppid not in init_ppids:
            continue
        result["matched"] += 1
        if pid in protect or ppid in protect:
            result["protected_skipped"] += 1
            continue
        # Extra: never kill if this pid is our direct child (live session)
        if ppid == os.getpid():
            result["protected_skipped"] += 1
            continue
        port = ""
        if "remote-debugging-port=" in cmd:
            try:
                port = cmd.split("remote-debugging-port=", 1)[1].split(None, 1)[0]
            except Exception:
                port = "?"
        if dry_run:
            log(f"[browser] orphan would kill pid={pid} ppid={ppid} port={port}")
            result["pids"].append(pid)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as e:
            result["errors"].append(f"pid={pid}: {e}")
            continue
        except OSError as e:
            result["errors"].append(f"pid={pid}: {e}")
            continue

        deadline = time.time() + max(0.1, float(term_grace_sec))
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        result["killed"] += 1
        result["pids"].append(pid)
        log(f"[browser] killed orphan Drission Chrome pid={pid} ppid={ppid} port={port}")

    if result["matched"] or result["killed"]:
        log(
            f"[browser] orphan cleanup: matched={result['matched']} "
            f"killed={result['killed']} skipped={result['protected_skipped']}"
        )
    return result


class TabPool:
    """Per-thread Chromium instance manager."""

    _options_factory = None
    _options_lock = threading.Lock()
    _thread_local = threading.local()
    _all_browsers: list[Any] = []
    _all_browsers_lock = threading.Lock()

    # ── public ──

    @classmethod
    def init(cls, browser_options_or_factory, log_callback=None):
        """Save options object or factory. Callable → fresh options each create."""
        with cls._options_lock:
            if callable(browser_options_or_factory):
                cls._options_factory = browser_options_or_factory
            else:
                # Shared options object: auto_port will NOT re-allocate.
                cls._options_factory = lambda: browser_options_or_factory
        if log_callback:
            log_callback("[*] TabPool 已初始化浏览器选项模板")

    @classmethod
    def _create_browser(cls):
        from DrissionPage import Chromium

        with cls._options_lock:
            factory = cls._options_factory
        if factory is None:
            return None
        options = factory()
        # Hold global start lock so mint standalone and other workers cannot
        # race auto_port / debugging-port allocation during Chromium() boot.
        with _chromium_start_lock:
            browser = Chromium(options)
        with cls._all_browsers_lock:
            cls._all_browsers.append(browser)
        return browser

    @classmethod
    def _unregister(cls, browser) -> None:
        if browser is None:
            return
        with cls._all_browsers_lock:
            try:
                cls._all_browsers = [b for b in cls._all_browsers if b is not browser]
            except Exception:
                pass

    @classmethod
    def _try_kill(cls, browser) -> None:
        """Hard-kill the Chromium OS process if quit() failed.

        browser.process_id is the OS PID; SIGKILL guarantees cleanup.
        """
        if browser is None:
            return
        pid = getattr(browser, "process_id", None)
        if pid is None or pid <= 0:
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, PermissionError):
            pass

    @classmethod
    def get_tab(cls, url=None):
        """Return current thread tab; create Chromium on first use."""
        tab = getattr(cls._thread_local, "tab", None)
        if tab is not None:
            return tab
        browser = cls._create_browser()
        if browser is None:
            raise RuntimeError("TabPool not initialized — call init() first")
        tab_ids = browser.tab_ids
        if tab_ids:
            tab = browser.get_tab(tab_ids[0])
        else:
            tab = browser.new_tab()
        cls._thread_local.browser = browser
        cls._thread_local.tab = tab
        cls._thread_local.served = 0
        return tab

    @classmethod
    def sync_tab(cls):
        """Point thread-local tab at the browser's latest tab."""
        browser = getattr(cls._thread_local, "browser", None)
        if browser is None:
            return
        tabs = browser.tab_ids
        if tabs:
            cls._thread_local.tab = browser.get_tab(tabs[-1])

    @classmethod
    def clear_session(cls, log_callback=None) -> bool:
        """Clear cookies/storage and blank the page; keep Chromium process.

        Returns True if session was cleared on a live browser; False if no browser.
        """
        browser = getattr(cls._thread_local, "browser", None)
        tab = getattr(cls._thread_local, "tab", None)
        if browser is None:
            return False
        ok = True
        try:
            if tab is not None:
                try:
                    tab.get("about:blank")
                except Exception:
                    pass
                for js in (
                    "try{localStorage.clear()}catch(e){}",
                    "try{sessionStorage.clear()}catch(e){}",
                    "try{indexedDB.databases&&indexedDB.databases().then(ds=>ds.forEach(d=>indexedDB.deleteDatabase(d.name)))}catch(e){}",
                ):
                    try:
                        tab.run_js(js)
                    except Exception:
                        pass
            # Best-effort cookie wipe (API varies by DrissionPage version)
            cleared = False
            for target in (tab, browser):
                if target is None or cleared:
                    continue
                for attr_path in (
                    ("set", "cookies", "clear"),
                    ("cookies", "clear"),
                ):
                    try:
                        obj = target
                        for name in attr_path[:-1]:
                            obj = getattr(obj, name)
                        fn = getattr(obj, attr_path[-1])
                        fn()
                        cleared = True
                        break
                    except Exception:
                        continue
            if not cleared:
                try:
                    # Fallback: drop all cookies via CDP-ish helper if present
                    cks = browser.cookies()
                    if isinstance(cks, list):
                        for c in cks:
                            try:
                                browser.set.cookies.remove(c)  # type: ignore[attr-defined]
                            except Exception:
                                pass
                except Exception:
                    ok = False
            # Prefer a single clean tab
            try:
                tabs = list(browser.tab_ids or [])
                if len(tabs) > 1:
                    keep = tabs[0]
                    for tid in tabs[1:]:
                        try:
                            browser.get_tab(tid).close()
                        except Exception:
                            pass
                    cls._thread_local.tab = browser.get_tab(keep)
                elif tabs:
                    cls._thread_local.tab = browser.get_tab(tabs[0])
            except Exception:
                cls.sync_tab()
            if log_callback:
                served = int(getattr(cls._thread_local, "served", 0) or 0)
                log_callback(f"[*] 浏览器会话已清理（复用进程, served={served}）")
            return ok
        except Exception as exc:
            if log_callback:
                log_callback(f"[!] clear_session 失败: {exc}")
            return False

    @classmethod
    def mark_served(cls) -> int:
        n = int(getattr(cls._thread_local, "served", 0) or 0) + 1
        cls._thread_local.served = n
        return n

    @classmethod
    def served_count(cls) -> int:
        return int(getattr(cls._thread_local, "served", 0) or 0)

    @classmethod
    def release_tab(cls):
        """Quit current thread Chromium and unregister it.

        On quit() failure, hard-kill the OS process to prevent zombie pile-up.
        """
        browser = getattr(cls._thread_local, "browser", None)
        if browser is not None:
            quit_ok = True
            try:
                browser.quit(del_data=True)
            except TypeError:
                try:
                    browser.quit()
                except Exception:
                    quit_ok = False
            except Exception:
                quit_ok = False
            if not quit_ok:
                cls._try_kill(browser)
            cls._unregister(browser)
        cls._thread_local.browser = None
        cls._thread_local.tab = None
        cls._thread_local.served = 0

    @classmethod
    def refresh_tab(cls):
        """Full recycle: quit + new browser."""
        cls.release_tab()
        return cls.get_tab()

    @classmethod
    def shutdown(cls):
        """Quit every browser we still track."""
        cls.release_tab()
        with cls._all_browsers_lock:
            browsers = list(cls._all_browsers)
            cls._all_browsers.clear()
        for b in browsers:
            try:
                b.quit(del_data=True)
            except TypeError:
                try:
                    b.quit()
                except Exception:
                    cls._try_kill(b)
            except Exception:
                cls._try_kill(b)

    @classmethod
    def live_count(cls) -> int:
        with cls._all_browsers_lock:
            return len(cls._all_browsers)

    @classmethod
    def get_browser(cls):
        return getattr(cls._thread_local, "browser", None)

    @classmethod
    def tracked_pids(cls) -> set[int]:
        """OS PIDs of Chromium instances currently tracked by TabPool."""
        out: set[int] = set()
        with cls._all_browsers_lock:
            browsers = list(cls._all_browsers)
        for b in browsers:
            pid = getattr(b, "process_id", None)
            if isinstance(pid, int) and pid > 0:
                out.add(pid)
        local = getattr(cls._thread_local, "browser", None)
        pid = getattr(local, "process_id", None) if local is not None else None
        if isinstance(pid, int) and pid > 0:
            out.add(pid)
        return out

    @classmethod
    def cleanup_orphans(cls, log_callback: LogFn | None = None, **kwargs: Any) -> dict[str, Any]:
        """Kill PPID=1 Drission leftovers; never touch tracked/live children."""
        protect = set(kwargs.pop("protect_pids", None) or ())
        protect |= cls.tracked_pids()
        return cleanup_orphan_drission_chromes(
            log_callback=log_callback,
            protect_pids=protect,
            **kwargs,
        )
