"""Node health probe — uses curl_cffi when available (matches ChatGPT path)."""

from __future__ import annotations

import json
import time
from typing import Any

from register_core.nodes.models import Node

DEFAULT_PROBE_URL = "https://api.ipify.org?format=json"


def probe_node(
    node: Node,
    *,
    probe_url: str = DEFAULT_PROBE_URL,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Probe one node; mutates node last_* fields. Returns public result dict."""
    t0 = time.time()
    ok = False
    ip = ""
    err = ""
    status: int | None = None
    try:
        body, status = _http_get(node.url, probe_url, timeout=timeout)
        if status is not None and 200 <= int(status) < 300:
            ok = True
            ip = _extract_ip(body)
        else:
            err = f"http_status={status}"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:200]

    ms = int((time.time() - t0) * 1000)
    node.last_ok = ok
    node.last_ip = ip
    node.last_ms = ms
    node.last_error = "" if ok else err
    node.last_checked_at = time.time()
    if ok:
        node.fail_count = 0
    else:
        node.fail_count = int(node.fail_count or 0) + 1

    return {
        "id": node.id,
        "label": node.label,
        "ok": ok,
        "ip": ip,
        "ms": ms,
        "status": status,
        "error": node.last_error,
        "url_label": node.label,
    }


def _http_get(proxy: str, url: str, *, timeout: float) -> tuple[str, int]:
    proxy = (proxy or "").strip()
    # Prefer curl_cffi — same stack as ChatGPT provider.
    try:
        from curl_cffi import requests as creq

        r = creq.get(url, proxy=proxy or None, impersonate="chrome", timeout=timeout)
        return (r.text or ""), int(r.status_code)
    except ImportError:
        pass

    import urllib.request

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "register-machine-node-probe/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace"), int(resp.status)


def _extract_ip(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for k in ("ip", "origin", "query"):
                if data.get(k):
                    return str(data[k]).split(",")[0].strip()
    except Exception:
        pass
    # plain IP
    if text and all(c.isdigit() or c == "." or c == ":" for c in text[:64]):
        return text.split()[0][:64]
    return ""
