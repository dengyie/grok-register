"""Project-owned mihomo mini-core for protocol proxies (VLESS/SS/…).

Why this exists
---------------
``nodes.json`` only stores dialable HTTP/SOCKS URLs for curl_cffi.
Clash YAML nodes (vless/vmess/trojan/ss/hysteria/…) need a protocol core.
This module runs a **project-local** mihomo (Meta) process against
``.nodes/config/runtime.yaml`` and exposes ``http://127.0.0.1:<mixed-port>``.

It does **not** require Clash Verge UI / system TUN / system proxy.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DIR = _ROOT / ".nodes"
_DEFAULT_BIN = _DEFAULT_DIR / "bin" / "mihomo"
_DEFAULT_CONFIG = _DEFAULT_DIR / "config" / "runtime.yaml"
_DEFAULT_META = _DEFAULT_DIR / "config" / "proxy-names.json"
_DEFAULT_PID = _DEFAULT_DIR / "runtime" / "mihomo.pid"
_DEFAULT_LOG = _DEFAULT_DIR / "runtime" / "mihomo.log"

DEFAULT_MIXED_PORT = 17897
DEFAULT_CONTROLLER = "127.0.0.1:19097"
DEFAULT_GROUP = "REGISTER"


def nodes_home() -> Path:
    env = (os.environ.get("REGISTER_NODES_HOME") or os.environ.get("NODES_HOME") or "").strip()
    return Path(os.path.expanduser(env)).resolve() if env else _DEFAULT_DIR.resolve()


def core_bin() -> Path:
    env = (os.environ.get("REGISTER_MIHOMO_BIN") or os.environ.get("MIHOMO_BIN") or "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    return (nodes_home() / "bin" / "mihomo").resolve()


def core_config() -> Path:
    env = (os.environ.get("REGISTER_CORE_CONFIG") or os.environ.get("CORE_CONFIG") or "").strip()
    if env:
        return Path(os.path.expanduser(env)).resolve()
    return (nodes_home() / "config" / "runtime.yaml").resolve()


def core_meta_path() -> Path:
    return (nodes_home() / "config" / "proxy-names.json").resolve()


def pid_path() -> Path:
    return (nodes_home() / "runtime" / "mihomo.pid").resolve()


def log_path() -> Path:
    return (nodes_home() / "runtime" / "mihomo.log").resolve()


def load_meta() -> dict[str, Any]:
    p = core_meta_path()
    if not p.is_file():
        return {
            "group": DEFAULT_GROUP,
            "mixed_port": DEFAULT_MIXED_PORT,
            "controller": DEFAULT_CONTROLLER,
            "proxy_url": f"http://127.0.0.1:{DEFAULT_MIXED_PORT}",
            "names": [],
        }
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {
            "group": DEFAULT_GROUP,
            "mixed_port": DEFAULT_MIXED_PORT,
            "controller": DEFAULT_CONTROLLER,
            "proxy_url": f"http://127.0.0.1:{DEFAULT_MIXED_PORT}",
            "names": [],
        }


def proxy_url() -> str:
    meta = load_meta()
    return str(meta.get("proxy_url") or f"http://127.0.0.1:{int(meta.get('mixed_port') or DEFAULT_MIXED_PORT)}")


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid() -> int | None:
    p = pid_path()
    if not p.is_file():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_running() -> bool:
    meta = load_meta()
    port = int(meta.get("mixed_port") or DEFAULT_MIXED_PORT)
    if _port_open("127.0.0.1", port):
        return True
    pid = _read_pid()
    return bool(pid and _pid_alive(pid))


def status() -> dict[str, Any]:
    meta = load_meta()
    bin_p = core_bin()
    cfg_p = core_config()
    pid = _read_pid()
    port = int(meta.get("mixed_port") or DEFAULT_MIXED_PORT)
    ctl = str(meta.get("controller") or DEFAULT_CONTROLLER)
    selected = None
    try:
        selected = get_selected()
    except Exception:
        selected = None
    return {
        "running": is_running(),
        "pid": pid if pid and _pid_alive(pid) else None,
        "bin": str(bin_p),
        "bin_exists": bin_p.is_file(),
        "config": str(cfg_p),
        "config_exists": cfg_p.is_file(),
        "mixed_port": port,
        "controller": ctl,
        "proxy_url": proxy_url(),
        "group": meta.get("group") or DEFAULT_GROUP,
        "proxy_count": len(meta.get("names") or []),
        "selected": selected,
        "log": str(log_path()),
    }


def start(*, wait_s: float = 8.0) -> dict[str, Any]:
    """Start project mihomo if not already running."""
    if is_running():
        return {"ok": True, "already": True, **status()}

    bin_p = core_bin()
    cfg_p = core_config()
    if not bin_p.is_file():
        return {
            "ok": False,
            "error": f"mihomo binary missing: {bin_p} (run scripts/bootstrap_nodes_core.sh)",
        }
    if not cfg_p.is_file():
        return {
            "ok": False,
            "error": f"runtime config missing: {cfg_p} (import Clash YAML first)",
        }

    runtime_dir = pid_path().parent
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_p = log_path()
    # mihomo -d workdir -f config
    workdir = nodes_home()
    workdir.mkdir(parents=True, exist_ok=True)
    log_f = open(log_p, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [str(bin_p), "-d", str(workdir), "-f", str(cfg_p)],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=str(workdir),
            start_new_session=True,
        )
    except Exception as exc:
        log_f.close()
        return {"ok": False, "error": f"spawn failed: {exc}"}

    pid_path().write_text(str(proc.pid), encoding="utf-8")
    deadline = time.time() + max(1.0, wait_s)
    meta = load_meta()
    port = int(meta.get("mixed_port") or DEFAULT_MIXED_PORT)
    while time.time() < deadline:
        if proc.poll() is not None:
            log_f.close()
            return {
                "ok": False,
                "error": f"mihomo exited early code={proc.returncode}; see {log_p}",
                "pid": proc.pid,
            }
        if _port_open("127.0.0.1", port, timeout=0.3):
            log_f.close()
            return {"ok": True, "already": False, **status()}
        time.sleep(0.15)
    log_f.close()
    return {
        "ok": False,
        "error": f"timeout waiting for mixed-port {port}; see {log_p}",
        "pid": proc.pid,
        **status(),
    }


def stop() -> dict[str, Any]:
    pid = _read_pid()
    stopped = False
    if pid and _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
        # wait
        for _ in range(30):
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        if pid_path().is_file():
            pid_path().unlink()
    except Exception:
        pass
    return {"ok": True, "stopped": stopped, "running": is_running()}


def _controller_base() -> str:
    meta = load_meta()
    ctl = str(meta.get("controller") or DEFAULT_CONTROLLER).strip()
    if not ctl.startswith("http"):
        ctl = f"http://{ctl}"
    return ctl.rstrip("/")


def _api(method: str, path: str, body: dict | None = None, timeout: float = 5.0) -> Any:
    url = f"{_controller_base()}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"controller HTTP {e.code}: {detail}") from e


def list_proxy_names() -> list[str]:
    meta = load_meta()
    names = meta.get("names") or []
    if names:
        return [str(x) for x in names]
    # fallback to controller
    if not is_running():
        return []
    try:
        data = _api("GET", "/proxies")
        proxies = (data or {}).get("proxies") or {}
        group = str(meta.get("group") or DEFAULT_GROUP)
        g = proxies.get(group) or {}
        return list(g.get("all") or [])
    except Exception:
        return []


def get_selected() -> str | None:
    if not is_running():
        return None
    meta = load_meta()
    group = str(meta.get("group") or DEFAULT_GROUP)
    try:
        data = _api("GET", f"/proxies/{urllib.parse.quote(group)}")
        return (data or {}).get("now")
    except Exception:
        return None


def select(name: str) -> dict[str, Any]:
    """Switch REGISTER group to a named proxy inside project core."""
    if not is_running():
        started = start()
        if not started.get("ok"):
            return {"ok": False, "error": started.get("error") or "core not running"}
    meta = load_meta()
    group = str(meta.get("group") or DEFAULT_GROUP)
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "error": "empty proxy name"}
    try:
        _api("PUT", f"/proxies/{urllib.parse.quote(group)}", {"name": name})
    except Exception as exc:
        return {"ok": False, "error": str(exc), "group": group, "name": name}
    return {"ok": True, "group": group, "selected": name, "proxy_url": proxy_url()}


def ensure_proxy_url(*, start_core: bool = True) -> str:
    """Return local mixed-port URL, starting core if needed."""
    if start_core and not is_running():
        res = start()
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "failed to start mihomo core")
    return proxy_url()
