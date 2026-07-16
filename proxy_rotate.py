"""Register-scoped proxy / egress rotation.

Modes
-----
off   — no rotation
list  — rotate explicit proxy URLs; only Chromium / register HTTP use them
        (other apps untouched)
clash — **domain-scoped Clash rotation** (recommended with local 7897):
        ensure a dedicated selector group (default ``GROK-REG``) + DOMAIN-SUFFIX
        rules for xAI/Grok hosts pointing at that group; rotate **only that
        group's node**. Never touch the main profile selector (e.g. 宝可梦).

Whole-machine impact
--------------------
- list: none outside register browser / its HTTP clients
- clash: only traffic matching the injected domain rules uses the rotated node
  (TUN/system-proxy clients hitting those domains share it; everything else
  keeps the original policy group). On process exit we restore the GROK-REG
  node to the pre-session value.
"""

from __future__ import annotations

import atexit
import http.client
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from proxy_bridge import proxy_log_label

LogFn = Optional[Callable[[str], None]]

DEFAULT_CLASH_GROUP = "GROK-REG"
DEFAULT_CLASH_DONOR_GROUP = "宝可梦"
DEFAULT_CLASH_API = "unix:///tmp/verge/verge-mihomo.sock"
DEFAULT_CLASH_CONFIG_PATH = (
    "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"
)
DEFAULT_CLASH_PROFILES_DIR = (
    "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles"
)
DEFAULT_CLASH_PROFILES_YAML = (
    "~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/profiles.yaml"
)

DEFAULT_GROK_DOMAINS = (
    "x.ai",
    "grok.com",
    "grok.x.ai",
    "assets.grok.com",
)

_DEFAULT_CLASH_EXCLUDE = (
    r"剩余|距离|套餐|建议|官网|DIRECT|REJECT|自动选择|故障转移|"
    r"Pass|Compatible|REJECT-DROP|GLOBAL|GROK-REG"
)

_SELECTOR_TYPES = frozenset(
    {"Selector", "URLTest", "Fallback", "LoadBalance", "Relay"}
)
_SKIP_NODE_TYPES = frozenset(
    {
        "Selector",
        "URLTest",
        "Fallback",
        "LoadBalance",
        "Relay",
        "Direct",
        "Reject",
        "Compatible",
        "Pass",
        "RejectDrop",
    }
)


def _log(log: LogFn, msg: str) -> None:
    if log:
        try:
            log(msg)
        except Exception:
            pass


def _truthy(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if not s:
        return default
    return s in {"1", "true", "yes", "y", "on"}


def _as_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except Exception:
        return default


def parse_proxy_list(raw: Any) -> list[str]:
    """Parse proxy pool from list / JSON array / comma|newline|semicolon / file path."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    if ("\n" not in text and "," not in text and ";" not in text) and (
        text.endswith(".txt")
        or text.endswith(".list")
        or os.path.isfile(os.path.expanduser(text))
    ):
        try:
            p = Path(text).expanduser()
            if p.is_file():
                lines = []
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    lines.append(line)
                return lines
        except Exception:
            pass
    parts = re.split(r"[\n,;]+", text)
    return [p.strip() for p in parts if p.strip()]


def parse_domain_list(raw: Any, default: tuple[str, ...] = DEFAULT_GROK_DOMAINS) -> list[str]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return list(default)
    if isinstance(raw, (list, tuple)):
        out = [str(x).strip().lstrip(".") for x in raw if str(x).strip()]
        return out or list(default)
    text = str(raw).strip()
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                out = [str(x).strip().lstrip(".") for x in data if str(x).strip()]
                return out or list(default)
        except Exception:
            pass
    parts = re.split(r"[\n,;|\s]+", text)
    out = [p.strip().lstrip(".") for p in parts if p.strip()]
    return out or list(default)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, unix_path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = unix_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


def clash_request(
    api: str,
    method: str,
    path: str,
    *,
    body: Any = None,
    secret: str = "",
    timeout: float = 8.0,
) -> tuple[int, Any]:
    """HTTP(S) or unix-socket request against Clash external controller."""
    raw = str(api or "").strip()
    if not raw:
        raise ValueError("clash_api empty")
    method = method.upper()
    if not path.startswith("/"):
        path = "/" + path

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if secret:
        headers["Authorization"] = f"Bearer {secret}"

    if raw.startswith("unix://") or raw.startswith("unix:"):
        sock_path = (
            raw.split("unix://", 1)[-1] if "unix://" in raw else raw.split("unix:", 1)[-1]
        )
        sock_path = sock_path.strip()
        if not sock_path:
            raise ValueError("unix clash_api missing path")
        conn = _UnixHTTPConnection(sock_path, timeout=timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw_body = resp.read()
            status = int(resp.status)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    else:
        if "://" not in raw:
            raw = "http://" + raw
        url = raw.rstrip("/") + path
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = int(resp.status)
                raw_body = resp.read()
        except urllib.error.HTTPError as e:
            status = int(e.code)
            raw_body = e.read() if e.fp else b""

    if not raw_body:
        return status, None
    try:
        return status, json.loads(raw_body.decode("utf-8"))
    except Exception:
        return status, raw_body.decode("utf-8", "replace")


def clash_get_proxies(api: str, *, secret: str = "", timeout: float = 8.0) -> dict[str, Any]:
    status, data = clash_request(api, "GET", "/proxies", secret=secret, timeout=timeout)
    if status >= 400 or not isinstance(data, dict):
        raise RuntimeError(f"clash GET /proxies failed status={status} body={data!r}")
    proxies = data.get("proxies") or {}
    if not isinstance(proxies, dict):
        raise RuntimeError("clash /proxies missing proxies map")
    return proxies


def clash_list_nodes(
    api: str,
    group: str,
    *,
    secret: str = "",
    exclude_re: str = _DEFAULT_CLASH_EXCLUDE,
    include_re: str = "",
    timeout: float = 8.0,
    proxies: dict[str, Any] | None = None,
) -> tuple[list[str], str, dict[str, Any]]:
    """Return (usable_node_names, current_now, group_info)."""
    if proxies is None:
        proxies = clash_get_proxies(api, secret=secret, timeout=timeout)
    g = proxies.get(group)
    if not isinstance(g, dict):
        selectors = [
            k
            for k, v in proxies.items()
            if isinstance(v, dict) and v.get("type") in _SELECTOR_TYPES
        ]
        raise RuntimeError(f"clash group not found: {group!r}; selectors={selectors[:20]}")
    all_names = list(g.get("all") or [])
    now = str(g.get("now") or "")
    ex = re.compile(exclude_re) if exclude_re else None
    inc = re.compile(include_re) if include_re else None
    usable: list[str] = []
    for name in all_names:
        if not name:
            continue
        if name == group:
            continue
        if ex and ex.search(name):
            continue
        if inc and not inc.search(name):
            continue
        meta = proxies.get(name) if isinstance(proxies.get(name), dict) else {}
        ntype = str((meta or {}).get("type") or "")
        if ntype in _SKIP_NODE_TYPES:
            continue
        usable.append(name)
    return usable, now, g


def clash_switch_node(
    api: str,
    group: str,
    node: str,
    *,
    secret: str = "",
    flush: bool = True,
    timeout: float = 8.0,
) -> None:
    enc_group = urllib.parse.quote(group, safe="")
    status, body = clash_request(
        api,
        "PUT",
        f"/proxies/{enc_group}",
        body={"name": node},
        secret=secret,
        timeout=timeout,
    )
    if status >= 400:
        raise RuntimeError(
            f"clash switch failed status={status} body={body!r} group={group!r} node={node!r}"
        )
    if flush:
        try:
            clash_request(api, "DELETE", "/connections", secret=secret, timeout=timeout)
        except Exception:
            pass


def _leaf_nodes_from_donor(
    proxies: dict[str, Any],
    donor_group: str,
    *,
    exclude_re: str,
) -> list[str]:
    donor = proxies.get(donor_group) if isinstance(proxies.get(donor_group), dict) else None
    names: list[str] = []
    if donor and isinstance(donor.get("all"), list):
        names = list(donor.get("all") or [])
    else:
        # fallback: all non-selector leaves
        for name, meta in proxies.items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("type") or "") in _SKIP_NODE_TYPES:
                continue
            names.append(name)
    ex = re.compile(exclude_re) if exclude_re else None
    out: list[str] = []
    seen = set()
    for name in names:
        if not name or name in seen:
            continue
        if ex and ex.search(name):
            continue
        meta = proxies.get(name) if isinstance(proxies.get(name), dict) else {}
        ntype = str((meta or {}).get("type") or "")
        if ntype in _SKIP_NODE_TYPES:
            continue
        seen.add(name)
        out.append(name)
    return out


def _yaml_quote(s: str) -> str:
    """Minimal YAML double-quote for node names with emoji / special chars."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _build_group_yaml(group: str, nodes: list[str]) -> str:
    lines = [
        f"- name: {_yaml_quote(group)}",
        "  type: select",
        "  proxies:",
    ]
    for n in nodes:
        lines.append(f"    - {_yaml_quote(n)}")
    return "\n".join(lines) + "\n"


def _build_domain_rules(domains: list[str], group: str) -> list[str]:
    rules = []
    for d in domains:
        d = d.strip().lstrip(".")
        if not d:
            continue
        rules.append(f"DOMAIN-SUFFIX,{d},{group}")
    return rules


def autodetect_verge_enhancement_files(
    profiles_yaml_path: str = DEFAULT_CLASH_PROFILES_YAML,
) -> tuple[str, str]:
    """Read Clash Verge profiles.yaml → (groups_file, rules_file) for current profile.

    Returns empty strings if not found. Best-effort; never raises.
    """
    try:
        p = Path(profiles_yaml_path).expanduser()
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", ""
    m = re.search(r"(?m)^current:\s*(\S+)", text)
    if not m:
        return "", ""
    uid = m.group(1).strip()
    start = text.find(f"- uid: {uid}")
    if start < 0:
        return "", ""
    end = text.find("\n- uid:", start + 1)
    block = text[start : end if end > 0 else start + 2000]
    groups_uid = re.search(r"(?m)^\s*groups:\s*(\S+)\s*$", block)
    rules_uid = re.search(r"(?m)^\s*rules:\s*(\S+)\s*$", block)
    g = (groups_uid.group(1).strip() + ".yaml") if groups_uid else ""
    r = (rules_uid.group(1).strip() + ".yaml") if rules_uid else ""
    return g, r


def _inject_group_and_rules_into_runtime_yaml(
    config_path: Path,
    *,
    group: str,
    nodes: list[str],
    domains: list[str],
) -> bool:
    """Inject GROK-REG group + domain rules into clash-verge.yaml if missing.

    Returns True if file was modified.
    """
    path = config_path.expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"clash config not found: {path}")
    text = path.read_text(encoding="utf-8")
    original = text
    group_marker = f"name: {group}"
    group_marker_q = f'name: "{group}"'
    has_group = (group_marker in text) or (group_marker_q in text)

    if not has_group:
        block = _build_group_yaml(group, nodes)
        # insert after `proxy-groups:` line
        m = re.search(r"(?m)^proxy-groups:\s*\n", text)
        if not m:
            raise RuntimeError("clash config missing proxy-groups:")
        text = text[: m.end()] + block + text[m.end() :]

    # domain rules — prepend after `rules:` if any missing
    wanted = _build_domain_rules(domains, group)
    missing = [r for r in wanted if r not in text]
    if missing:
        m = re.search(r"(?m)^rules:\s*\n", text)
        if not m:
            raise RuntimeError("clash config missing rules:")
        inject = "".join(f"- {r}\n" for r in missing)
        text = text[: m.end()] + inject + text[m.end() :]

    if text == original:
        return False
    # backup once per process dir
    bak = path.with_suffix(path.suffix + ".grok-reg.bak")
    if not bak.exists():
        try:
            bak.write_text(original, encoding="utf-8")
        except Exception:
            pass
    path.write_text(text, encoding="utf-8")
    return True


def _write_verge_enhancement(
    profiles_dir: Path,
    groups_uid_file: str,
    rules_uid_file: str,
    *,
    group: str,
    nodes: list[str],
    domains: list[str],
) -> None:
    """Best-effort: persist into Clash Verge enhancement templates."""
    profiles_dir = profiles_dir.expanduser()
    if not profiles_dir.is_dir():
        return
    gpath = profiles_dir / groups_uid_file
    rpath = profiles_dir / rules_uid_file
    # groups prepend
    group_entry_lines = [
        f"  - name: {_yaml_quote(group)}",
        "    type: select",
        "    proxies:",
    ]
    for n in nodes:
        group_entry_lines.append(f"      - {_yaml_quote(n)}")
    groups_yaml = (
        "# Profile Enhancement Groups Template for Clash Verge\n"
        "# Managed by grok-register proxy_rotate (GROK-REG domain isolation)\n\n"
        "prepend:\n"
        + "\n".join(group_entry_lines)
        + "\n\nappend: []\n\ndelete: []\n"
    )
    rules_lines = ["prepend:"]
    for r in _build_domain_rules(domains, group):
        rules_lines.append(f"  - {_yaml_quote(r)}")
    rules_yaml = (
        "# Profile Enhancement Rules Template for Clash Verge\n"
        "# Managed by grok-register proxy_rotate (xAI/Grok -> GROK-REG only)\n\n"
        + "\n".join(rules_lines)
        + "\n\nappend: []\n\ndelete: []\n"
    )
    try:
        if groups_uid_file:
            gpath.write_text(groups_yaml, encoding="utf-8")
        if rules_uid_file:
            rpath.write_text(rules_yaml, encoding="utf-8")
    except Exception:
        pass


def clash_force_reload(api: str, config_path: str, *, secret: str = "", timeout: float = 15.0) -> None:
    path = str(Path(config_path).expanduser())
    status, body = clash_request(
        api,
        "PUT",
        "/configs?force=true",
        body={"path": path},
        secret=secret,
        timeout=timeout,
    )
    if status >= 400:
        raise RuntimeError(f"clash force reload failed status={status} body={body!r}")


def ensure_clash_domain_group(
    *,
    api: str,
    group: str,
    donor_group: str,
    domains: list[str],
    config_path: str,
    secret: str = "",
    exclude_re: str = _DEFAULT_CLASH_EXCLUDE,
    profiles_dir: str = "",
    verge_groups_file: str = "",
    verge_rules_file: str = "",
    log: LogFn = None,
) -> dict[str, Any]:
    """Ensure dedicated selector + domain rules exist; reload if we patched YAML."""
    proxies = clash_get_proxies(api, secret=secret)
    if group in proxies and str((proxies.get(group) or {}).get("type") or "") in _SELECTOR_TYPES:
        # still ensure domain rules present in runtime yaml
        nodes, now, _ = clash_list_nodes(
            api, group, secret=secret, exclude_re=exclude_re, proxies=proxies
        )
        if not nodes:
            # group exists but empty — refill from donor
            nodes = _leaf_nodes_from_donor(proxies, donor_group, exclude_re=exclude_re)
        changed = _inject_group_and_rules_into_runtime_yaml(
            Path(config_path), group=group, nodes=nodes or ["DIRECT"], domains=domains
        )
        if changed:
            _log(log, f"[*] Clash 已补充域名规则 -> {group}，强制重载配置…")
            clash_force_reload(api, config_path, secret=secret)
            proxies = clash_get_proxies(api, secret=secret)
        return {
            "group": group,
            "existed": True,
            "nodes": len(nodes),
            "now": str((proxies.get(group) or {}).get("now") or now),
            "reloaded": changed,
        }

    nodes = _leaf_nodes_from_donor(proxies, donor_group, exclude_re=exclude_re)
    if not nodes:
        raise RuntimeError(
            f"无法从策略组 {donor_group!r} 收集可用节点以创建 {group!r}"
        )
    _log(
        log,
        f"[*] 创建 Clash 专用策略组 {group}（{len(nodes)} 节点）+ 域名规则 {domains}；"
        f"不修改主组 {donor_group}",
    )
    _inject_group_and_rules_into_runtime_yaml(
        Path(config_path), group=group, nodes=nodes, domains=domains
    )
    if profiles_dir and (verge_groups_file or verge_rules_file):
        _write_verge_enhancement(
            Path(profiles_dir),
            verge_groups_file,
            verge_rules_file,
            group=group,
            nodes=nodes,
            domains=domains,
        )
    clash_force_reload(api, config_path, secret=secret)
    proxies2 = clash_get_proxies(api, secret=secret)
    if group not in proxies2:
        raise RuntimeError(
            f"重载后仍找不到策略组 {group!r}；请检查 clash 配置写入权限 / API"
        )
    return {
        "group": group,
        "existed": False,
        "nodes": len(nodes),
        "now": str((proxies2.get(group) or {}).get("now") or ""),
        "reloaded": True,
    }


class ProxyRotator:
    """Process-wide proxy / Clash domain-group rotator (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.mode = "off"
        self.every = 1
        self.rotate_on_start = False
        self.rotate_required = False
        self.list_pool: list[str] = []
        self.list_index = 0
        self.accounts_on_current = 0
        self.current_proxy = ""
        self.current_label = ""
        self.clash_api = DEFAULT_CLASH_API
        self.clash_secret = ""
        self.clash_group = DEFAULT_CLASH_GROUP
        self.clash_donor_group = DEFAULT_CLASH_DONOR_GROUP
        self.clash_domains: list[str] = list(DEFAULT_GROK_DOMAINS)
        self.clash_config_path = DEFAULT_CLASH_CONFIG_PATH
        self.clash_profiles_dir = DEFAULT_CLASH_PROFILES_DIR
        self.clash_verge_groups_file = ""
        self.clash_verge_rules_file = ""
        self.clash_exclude = _DEFAULT_CLASH_EXCLUDE
        self.clash_include = ""
        self.clash_flush = True
        self.clash_restore_on_exit = True
        self.clash_pin_node = ""
        self.clash_setup_done = False
        self.update_cpa_proxy = True
        self._original_clash_node: str = ""
        self._clash_dirty = False
        self._started = False
        self._last_error = ""
        self._atexit_registered = False

    def configure(self, cfg: dict | None) -> None:
        cfg = cfg if isinstance(cfg, dict) else {}
        with self._lock:
            mode = str(cfg.get("proxy_rotate_mode") or "off").strip().lower()
            if mode in {"none", "disabled", "0", "false", ""}:
                mode = "off"
            # aliases
            if mode in {"clash_domain", "domain", "rules"}:
                mode = "clash"
            if mode not in {"off", "list", "clash"}:
                mode = "off"
            self.mode = mode
            self.every = max(1, _as_int(cfg.get("proxy_rotate_every"), 1))
            default_on_start = mode in {"list", "clash"}
            self.rotate_on_start = _truthy(
                cfg.get("proxy_rotate_on_start"), default=default_on_start
            )
            self.rotate_required = _truthy(cfg.get("proxy_rotate_required"), False)
            self.update_cpa_proxy = _truthy(cfg.get("proxy_rotate_update_cpa"), True)

            base_proxy = str(cfg.get("proxy") or "").strip()
            pool = parse_proxy_list(cfg.get("proxy_list"))
            if not pool:
                pool = parse_proxy_list(cfg.get("proxy_pool"))
            self.list_pool = pool
            self.list_index = 0
            if mode == "list" and pool:
                self.current_proxy = pool[0]
            else:
                self.current_proxy = base_proxy
            self.current_label = (
                proxy_log_label(self.current_proxy) or self.current_proxy or "(none)"
            )

            self.clash_api = str(
                cfg.get("clash_api") or cfg.get("clash_controller") or DEFAULT_CLASH_API
            ).strip() or DEFAULT_CLASH_API
            self.clash_secret = str(cfg.get("clash_secret") or "").strip()
            # Dedicated group — NEVER default to main profile group
            self.clash_group = str(
                cfg.get("clash_proxy_group") or cfg.get("clash_group") or DEFAULT_CLASH_GROUP
            ).strip() or DEFAULT_CLASH_GROUP
            self.clash_donor_group = str(
                cfg.get("clash_donor_group") or DEFAULT_CLASH_DONOR_GROUP
            ).strip() or DEFAULT_CLASH_DONOR_GROUP
            if self.clash_group in {self.clash_donor_group, "GLOBAL", "全球", "宝可梦"}:
                # hard guard: refuse to use main group as rotate target
                self.clash_group = DEFAULT_CLASH_GROUP
            self.clash_domains = parse_domain_list(cfg.get("clash_rule_domains"))
            self.clash_config_path = str(
                cfg.get("clash_config_path") or DEFAULT_CLASH_CONFIG_PATH
            ).strip()
            self.clash_profiles_dir = str(
                cfg.get("clash_profiles_dir") or DEFAULT_CLASH_PROFILES_DIR
            ).strip()
            self.clash_verge_groups_file = str(
                cfg.get("clash_verge_groups_file") or ""
            ).strip()
            self.clash_verge_rules_file = str(
                cfg.get("clash_verge_rules_file") or ""
            ).strip()
            if self.mode == "clash" and (
                not self.clash_verge_groups_file or not self.clash_verge_rules_file
            ):
                g, r = autodetect_verge_enhancement_files()
                if not self.clash_verge_groups_file and g:
                    self.clash_verge_groups_file = g
                if not self.clash_verge_rules_file and r:
                    self.clash_verge_rules_file = r
            self.clash_exclude = (
                str(cfg.get("clash_node_exclude") or _DEFAULT_CLASH_EXCLUDE).strip()
                or _DEFAULT_CLASH_EXCLUDE
            )
            self.clash_include = str(cfg.get("clash_node_include") or "").strip()
            self.clash_flush = _truthy(cfg.get("clash_flush_connections"), True)
            self.clash_restore_on_exit = _truthy(cfg.get("clash_restore_on_exit"), True)
            # Prefer config pin; fall back to GROK_NODE env (start-clash-for-grok.sh).
            pin = str(
                cfg.get("clash_pin_node") or cfg.get("grok_node") or os.environ.get("GROK_NODE") or ""
            ).strip()
            self.clash_pin_node = pin

            self.accounts_on_current = 0
            self._original_clash_node = ""
            self._clash_dirty = False
            self._started = False
            self.clash_setup_done = False
            self._last_error = ""

            if mode == "clash" and self.clash_restore_on_exit and not self._atexit_registered:
                atexit.register(_atexit_restore_clash)
                self._atexit_registered = True

    def apply_to_config(self, cfg: dict) -> None:
        if not isinstance(cfg, dict):
            return
        if self.mode != "list":
            return
        proxy = self.current_proxy
        if not proxy:
            return
        prev = str(cfg.get("proxy") or "").strip()
        cfg["proxy"] = proxy
        if self.update_cpa_proxy:
            cpa = str(cfg.get("cpa_proxy") or "").strip()
            if not cpa or cpa == prev or cpa == str(cfg.get("_proxy_rotate_prev") or ""):
                cfg["cpa_proxy"] = proxy
        cfg["_proxy_rotate_prev"] = proxy

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode,
                "every": self.every,
                "accounts_on_current": self.accounts_on_current,
                "current_proxy": proxy_log_label(self.current_proxy) or self.current_proxy,
                "current_label": self.current_label,
                "list_size": len(self.list_pool),
                "clash_api": self.clash_api,
                "clash_group": self.clash_group,
                "clash_donor_group": self.clash_donor_group,
                "clash_domains": list(self.clash_domains),
                "clash_dirty": self._clash_dirty,
                "original_clash_node": self._original_clash_node,
                "last_error": self._last_error,
            }

    def _due_locked(self, *, force: bool) -> bool:
        if self.mode == "off":
            return False
        if force:
            return True
        if not self._started and self.rotate_on_start:
            return True
        if not self._started:
            return False
        return self.accounts_on_current >= self.every

    def _ensure_clash_setup_locked(self, log: LogFn = None) -> None:
        if self.clash_setup_done:
            return
        ensure_clash_domain_group(
            api=self.clash_api,
            group=self.clash_group,
            donor_group=self.clash_donor_group,
            domains=self.clash_domains,
            config_path=self.clash_config_path,
            secret=self.clash_secret,
            exclude_re=self.clash_exclude,
            profiles_dir=self.clash_profiles_dir,
            verge_groups_file=self.clash_verge_groups_file,
            verge_rules_file=self.clash_verge_rules_file,
            log=log,
        )
        self.clash_setup_done = True
        # snapshot original node of dedicated group only
        try:
            _nodes, now, _ = clash_list_nodes(
                self.clash_api,
                self.clash_group,
                secret=self.clash_secret,
                exclude_re=self.clash_exclude,
                include_re=self.clash_include,
            )
            if not self._original_clash_node:
                self._original_clash_node = now
        except Exception:
            pass

    def _rotate_list_locked(self) -> dict[str, Any]:
        if not self.list_pool:
            raise RuntimeError("proxy_rotate_mode=list 但 proxy_list 为空")
        if self._started or self.accounts_on_current > 0:
            self.list_index = (self.list_index + 1) % len(self.list_pool)
        proxy = self.list_pool[self.list_index]
        prev = self.current_proxy
        self.current_proxy = proxy
        self.current_label = proxy_log_label(proxy) or proxy
        return {
            "rotated": True,
            "mode": "list",
            "proxy": proxy,
            "label": self.current_label,
            "index": self.list_index,
            "pool_size": len(self.list_pool),
            "prev": proxy_log_label(prev) or prev,
            "scope": "browser_only",
        }

    def _rotate_clash_locked(self, log: LogFn = None) -> dict[str, Any]:
        self._ensure_clash_setup_locked(log=log)
        # hard guard again
        if self.clash_group in {self.clash_donor_group, "GLOBAL"}:
            raise RuntimeError(
                f"拒绝轮换主策略组 {self.clash_group!r}；请使用专用组 {DEFAULT_CLASH_GROUP}"
            )
        nodes, now, _info = clash_list_nodes(
            self.clash_api,
            self.clash_group,
            secret=self.clash_secret,
            exclude_re=self.clash_exclude,
            include_re=self.clash_include,
        )
        if not nodes:
            raise RuntimeError(f"clash group {self.clash_group!r} 无可用节点")
        if not self._original_clash_node:
            self._original_clash_node = now

        # First due rotate (rotate_on_start): claim current / pin — do NOT advance off
        # pre-pinned GROK_NODE (smoke bug: TUIC → AnyTLS on worker start).
        if not self._started:
            pin = self.clash_pin_node
            if pin and pin in nodes and pin != now:
                clash_switch_node(
                    self.clash_api,
                    self.clash_group,
                    pin,
                    secret=self.clash_secret,
                    flush=self.clash_flush,
                )
                self._clash_dirty = True
                self.current_label = pin
                return {
                    "rotated": True,
                    "mode": "clash",
                    "label": pin,
                    "node": pin,
                    "prev": now,
                    "reason": "pin_current_on_start",
                    "group": self.clash_group,
                    "pool_size": len(nodes),
                    "domains": list(self.clash_domains),
                    "proxy": proxy_log_label(self.current_proxy) or self.current_proxy,
                    "scope": "domain_rules_only",
                    "will_restore": self.clash_restore_on_exit,
                    "original": self._original_clash_node,
                }
            # Keep current node (symmetric to list mode using pool[0] first).
            claim = pin if pin and pin == now else now
            if claim not in nodes:
                claim = now if now in nodes else nodes[0]
            self.current_label = claim
            return {
                "rotated": False,
                "mode": "clash",
                "label": claim,
                "node": claim,
                "reason": "on_start_keep_current",
                "group": self.clash_group,
                "pool_size": len(nodes),
                "domains": list(self.clash_domains),
                "proxy": proxy_log_label(self.current_proxy) or self.current_proxy,
                "scope": "domain_rules_only",
                "will_restore": self.clash_restore_on_exit,
                "original": self._original_clash_node,
            }

        try:
            idx = nodes.index(now)
            nxt = nodes[(idx + 1) % len(nodes)]
        except ValueError:
            nxt = nodes[0]
            if nxt == now and len(nodes) > 1:
                nxt = nodes[1]
        if nxt == now:
            self.current_label = now
            return {
                "rotated": False,
                "mode": "clash",
                "label": now,
                "node": now,
                "reason": "single_or_same_node",
                "group": self.clash_group,
                "scope": "domain_rules_only",
            }
        clash_switch_node(
            self.clash_api,
            self.clash_group,
            nxt,
            secret=self.clash_secret,
            flush=self.clash_flush,
        )
        self._clash_dirty = True
        self.current_label = nxt
        return {
            "rotated": True,
            "mode": "clash",
            "label": nxt,
            "node": nxt,
            "prev": now,
            "group": self.clash_group,
            "pool_size": len(nodes),
            "domains": list(self.clash_domains),
            "proxy": proxy_log_label(self.current_proxy) or self.current_proxy,
            "scope": "domain_rules_only",
            "will_restore": self.clash_restore_on_exit,
            "original": self._original_clash_node,
        }

    def maybe_rotate(
        self,
        *,
        force: bool = False,
        log: LogFn = None,
        config: dict | None = None,
    ) -> dict[str, Any]:
        """Call once before each account attempt (before browser start)."""
        with self._lock:
            if self.mode == "off":
                self._started = True
                self.accounts_on_current += 1
                return {"rotated": False, "mode": "off", "label": self.current_label}

            due = self._due_locked(force=force)
            if not due:
                self._started = True
                self.accounts_on_current += 1
                return {
                    "rotated": False,
                    "mode": self.mode,
                    "label": self.current_label,
                    "accounts_on_current": self.accounts_on_current,
                    "every": self.every,
                }

            try:
                if self.mode == "list":
                    result = self._rotate_list_locked()
                else:
                    result = self._rotate_clash_locked(log=log)
                self._last_error = ""
            except Exception as exc:
                self._last_error = str(exc)
                _log(log, f"[!] 代理轮换失败: {exc}")
                if self.rotate_required:
                    raise
                self._started = True
                self.accounts_on_current += 1
                return {
                    "rotated": False,
                    "mode": self.mode,
                    "error": str(exc),
                    "label": self.current_label,
                }

            self._started = True
            self.accounts_on_current = 1
            if config is not None and self.mode == "list":
                self.apply_to_config(config)
            if result.get("rotated"):
                if result.get("mode") == "clash":
                    _log(
                        log,
                        f"[*] 代理轮换(仅 {result.get('group')} / 域名 {','.join(self.clash_domains)}): "
                        f"{result.get('prev') or '-'} -> {result.get('label')}"
                        f" (pool={result.get('pool_size', '?')}; 主策略组不动)",
                    )
                    if result.get("will_restore"):
                        _log(
                            log,
                            f"[*] 会话结束将恢复 {result.get('group')} -> "
                            f"{result.get('original') or '(unknown)'}",
                        )
                else:
                    _log(
                        log,
                        f"[*] 代理轮换(仅注册浏览器): "
                        f"{result.get('prev') or '-'} -> {result.get('label')}"
                        f" (pool={result.get('pool_size', '?')})",
                    )
            return result

    def restore_clash(self, log: LogFn = None) -> bool:
        """Restore dedicated GROK-REG node only (never touches main group)."""
        with self._lock:
            if not self._clash_dirty:
                return False
            if not self.clash_restore_on_exit:
                return False
            if not self.clash_api or not self.clash_group or not self._original_clash_node:
                return False
            if self.clash_group in {self.clash_donor_group, "GLOBAL"}:
                _log(log, f"[!] 拒绝恢复：group={self.clash_group} 疑似主组")
                return False
            target = self._original_clash_node
            try:
                clash_switch_node(
                    self.clash_api,
                    self.clash_group,
                    target,
                    secret=self.clash_secret,
                    flush=self.clash_flush,
                )
                self._clash_dirty = False
                self.current_label = target
                _log(log, f"[*] 已恢复专用组节点: {self.clash_group} -> {target}")
                return True
            except Exception as exc:
                _log(log, f"[!] 恢复专用组节点失败: {exc}")
                return False


_rotator = ProxyRotator()
_rotator_lock = threading.Lock()


def _atexit_restore_clash() -> None:
    try:
        _rotator.restore_clash(log=lambda m: print(m, flush=True))
    except Exception:
        pass


def get_rotator() -> ProxyRotator:
    return _rotator


def configure_proxy_rotation(cfg: dict | None, log: LogFn = None) -> ProxyRotator:
    with _rotator_lock:
        _rotator.configure(cfg)
        if cfg is not None:
            _rotator.apply_to_config(cfg)
        st = _rotator.status()
        if st["mode"] == "list":
            _log(
                log,
                f"[*] 代理轮换已启用(仅注册浏览器): mode=list every={st['every']} "
                f"pool={st['list_size']} current={st['current_label'] or '-'}",
            )
        elif st["mode"] == "clash":
            _log(
                log,
                f"[*] 代理轮换已启用(域名规则专用组): mode=clash every={st['every']} "
                f"group={st['clash_group']} domains={','.join(st['clash_domains'])} "
                f"donor={st['clash_donor_group']}（不改主策略组）",
            )
        return _rotator


def maybe_rotate_proxy(
    *,
    force: bool = False,
    log: LogFn = None,
    config: dict | None = None,
) -> dict[str, Any]:
    return _rotator.maybe_rotate(force=force, log=log, config=config)


def restore_proxy_rotation(log: LogFn = None) -> bool:
    return _rotator.restore_clash(log=log)


def current_proxy_override() -> str:
    with _rotator._lock:
        if _rotator.mode == "list" and _rotator.current_proxy:
            return _rotator.current_proxy
        return ""
