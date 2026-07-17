"""Node health probe — uses curl_cffi when available (matches ChatGPT path).

Layers:
  L1 (probe_node / DEFAULT_PROBE_URL): require HTTP 2xx; mutates catalog last_ok.
  L2 (probe_reachable / business URL): any HTTP status = transport success; no stamp.
  L1∧L2 (probe_node_layered): registration pool gate when target domains are known.
"""

from __future__ import annotations

import json
import time
from typing import Any, Sequence

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


def probe_reachable(
    proxy_url: str,
    target_url: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """L2 transport probe: any HTTP response (incl. 3xx/4xx/5xx) is success.

    RST / tunnel timeout / empty / connect errors are failures. Does **not**
    mutate Node catalog fields — filter-only for registration pool seeding.
    """
    t0 = time.time()
    ok = False
    err = ""
    status: int | None = None
    try:
        _body, status = _http_get(proxy_url, target_url, timeout=timeout)
        # Transport success: we received an HTTP status line (any code).
        if status is not None:
            ok = True
        else:
            err = "empty_status"
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:200]
    ms = int((time.time() - t0) * 1000)
    return {
        "ok": ok,
        "status": status,
        "ms": ms,
        "error": "" if ok else err,
        "target": target_url,
    }


def probe_node_layered(
    node: Node,
    *,
    probe_urls: Sequence[str] | None = None,
    l1_url: str = DEFAULT_PROBE_URL,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """L1 egress (2xx) then L2 business targets (any status).

    - Always runs L1 via ``probe_node`` (mutates last_ok).
    - When ``probe_urls`` empty: layered ok == L1 ok (legacy).
    - When L2 set: ok only if L1 and every target is transport-reachable.
    - L2 failures do **not** flip last_ok / fail_count if L1 already passed —
      catalog stamp stays L1-true; ``last_error`` may note ``l2_fail …`` for
      ops + smart_order deprioritization; pool filter uses ok / pool_ready.
    - Remaining L2 targets short-circuit after the first miss.
    """
    targets = [str(u).strip() for u in (probe_urls or []) if str(u).strip()]
    l1 = probe_node(node, probe_url=l1_url, timeout=timeout)
    result: dict[str, Any] = {
        **l1,
        "l1_ok": bool(l1.get("ok")),
        "l2_ok": True if not targets else False,
        "l2": [],
        "pool_ready": bool(l1.get("ok")),
        "probe_targets": list(targets),
    }
    if not l1.get("ok"):
        result["ok"] = False
        result["pool_ready"] = False
        return result
    if not targets:
        result["ok"] = True
        result["pool_ready"] = True
        return result

    l2_results: list[dict[str, Any]] = []
    all_l2 = True
    for target in targets:
        r = probe_reachable(node.url, target, timeout=timeout)
        l2_results.append(r)
        if not r.get("ok"):
            all_l2 = False
            break  # short-circuit remaining L2 targets
    result["l2"] = l2_results
    result["l2_ok"] = all_l2
    result["pool_ready"] = all_l2
    # Public "ok" for preflight healthy pool = L1∧L2 when targets present.
    result["ok"] = all_l2
    if not all_l2:
        failed = next((x for x in l2_results if not x.get("ok")), None)
        detail = (failed or {}).get("error") or "l2_unreachable"
        tgt = (failed or {}).get("target") or (targets[0] if targets else "")
        err = f"l2_fail target={tgt}: {detail}"[:200]
        result["error"] = err
        # Ops visibility only: do not flip last_ok / fail_count (no hard quarantine
        # for missing a business path). Pool gate still uses ok / pool_ready.
        node.last_error = err
    else:
        # Clear prior L2 annotation when dual-pass succeeds.
        if (node.last_error or "").startswith("l2_fail"):
            node.last_error = ""
    return result


def _http_get(proxy: str, url: str, *, timeout: float) -> tuple[str, int]:
    proxy = (proxy or "").strip()
    # Prefer curl_cffi — same stack as ChatGPT provider.
    try:
        from curl_cffi import requests as creq

        r = creq.get(url, proxy=proxy or None, impersonate="chrome", timeout=timeout)
        return (r.text or ""), int(r.status_code)
    except ImportError:
        pass

    import urllib.error
    import urllib.request

    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "register-machine-node-probe/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace"), int(resp.status)
    except urllib.error.HTTPError as e:
        # L2 contract: any HTTP status = transport OK (match curl_cffi).
        # HTTPError is a valid response with .code; do not raise into probe_reachable.
        try:
            body = e.read().decode("utf-8", "replace") if e.fp is not None else ""
        except Exception:
            body = ""
        return body, int(e.code)


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
