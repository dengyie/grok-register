"""Import pipeline: parse → validate → split → pack (light, opt-in)."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from register_core.nodes.convert.parsers import ParseError, parse_file, parse_text
from register_core.nodes.convert.types import (
    DEFAULT_CONTROLLER,
    DEFAULT_GROUP,
    DEFAULT_MIXED_PORT,
    DIALABLE_TYPES,
)
from register_core.nodes.convert.validate import ProfileReport, ValidationIssue, validate_proxy_list
from register_core.nodes.models import Node

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

_ROOT = Path(__file__).resolve().parents[3]


def load_nodes_for_plan(nodes_json: Path | str | None = None) -> list[Node]:
    """Load existing catalog for merge preview (empty if missing)."""
    from register_core.nodes.catalog import load_nodes

    path = Path(nodes_json) if nodes_json else (_ROOT / "nodes.json")
    try:
        return load_nodes(path)
    except Exception:
        return []


def _auth_url(scheme: str, server: str, port, username: str = "", password: str = "") -> str:
    host = f"{server}:{int(port)}"
    user = (username or "").strip()
    pwd = (password or "").strip()
    if user or pwd:
        return f"{scheme}://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}"
    return f"{scheme}://{host}"


def node_from_dialable(proxy: dict[str, Any], source: str = "") -> Node | None:
    t = str(proxy.get("type") or "").lower().strip()
    if t not in DIALABLE_TYPES:
        return None
    server = str(proxy.get("server") or "").strip()
    port = proxy.get("port")
    if not server or not port:
        return None
    name = str(proxy.get("name") or f"{server}:{port}").strip()
    user = str(proxy.get("username") or proxy.get("user") or "")
    pwd = str(proxy.get("password") or "")
    scheme = "socks5" if t.startswith("socks") else "http"
    url = _auth_url(scheme, server, port, user, pwd)
    nid = hashlib.sha1(f"{scheme}|{server}|{port}|{user}".encode()).hexdigest()[:12]
    tags = [t, "imported"]
    if source:
        tags.append(source[:24])
    return Node(
        url=url,
        id=f"imp-{nid}",
        label=name[:80],
        tags=tags,
        enabled=True,
    )


def _sig(p: dict[str, Any]) -> str:
    return hashlib.sha1(
        f"{p.get('type')}|{p.get('server')}|{p.get('port')}|{p.get('uuid') or p.get('password') or ''}|{p.get('name')}".encode()
    ).hexdigest()


@dataclass
class MergePlan:
    """How dialable nodes will land in nodes.json."""

    mode: str = "merge"  # merge | replace
    existing: int = 0
    incoming: int = 0
    added: int = 0
    updated: int = 0
    kept: int = 0
    dropped: int = 0
    final: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "existing": self.existing,
            "incoming": self.incoming,
            "added": self.added,
            "updated": self.updated,
            "kept": self.kept,
            "dropped": self.dropped,
            "final": self.final,
        }


@dataclass
class ImportResult:
    ok: bool
    format: str = ""
    dialable: list[Node] = field(default_factory=list)
    protocol: list[dict[str, Any]] = field(default_factory=list)
    reports: list[ProfileReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    runtime_path: str | None = None
    nodes_path: str | None = None
    meta_path: str | None = None
    types: dict[str, int] = field(default_factory=dict)
    needs_core: bool = False
    merge: MergePlan | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "format": self.format,
            "http_socks": len(self.dialable),
            "protocol": len(self.protocol),
            "needs_core": self.needs_core,
            "types": self.types,
            "runtime": self.runtime_path,
            "nodes_json": self.nodes_path,
            "meta": self.meta_path,
            "errors": self.errors,
            "reports": [r.to_dict() for r in self.reports],
            "dialable_labels": [n.label for n in self.dialable[:20]],
            "protocol_names": [str(p.get("name") or "") for p in self.protocol[:20]],
            "merge": self.merge.to_dict() if self.merge else None,
        }


def merge_dialable(
    existing: list[Node],
    incoming: list[Node],
    *,
    replace: bool = False,
) -> tuple[list[Node], MergePlan]:
    """Merge by URL. Default keeps unknown existing entries; replace drops them."""
    plan = MergePlan(
        mode="replace" if replace else "merge",
        existing=len(existing),
        incoming=len(incoming),
    )
    if replace:
        plan.added = len(incoming)
        plan.dropped = len(existing)
        plan.final = len(incoming)
        return list(incoming), plan

    by_url: dict[str, Node] = {}
    order: list[str] = []
    for n in existing:
        if not n.url:
            continue
        if n.url not in by_url:
            order.append(n.url)
        by_url[n.url] = n
    existing_urls = set(by_url)

    for n in incoming:
        if not n.url:
            continue
        if n.url in by_url:
            old = by_url[n.url]
            # refresh label/tags/id toward pure import identity; keep health counters
            new_id = n.id or old.id
            if str(old.id or "").startswith("clash-") or not old.id:
                new_id = n.id or old.id
            # strip legacy tag when rewriting
            tags = list(n.tags or [])
            if not tags:
                tags = [t for t in (old.tags or []) if t != "from-clash"]
            merged = Node(
                url=n.url,
                id=new_id,
                label=n.label or old.label,
                tags=tags,
                enabled=bool(old.enabled) if old.enabled is not None else n.enabled,
                last_ok=old.last_ok,
                last_ip=old.last_ip,
                last_ms=old.last_ms,
                last_error=old.last_error,
                last_checked_at=old.last_checked_at,
                fail_count=int(old.fail_count or 0),
                # Preserve soft-cool so re-import does not re-arm burned egress.
                cooldown_until=old.cooldown_until,
                cooldown_reason=old.cooldown_reason,
            )
            by_url[n.url] = merged
            plan.updated += 1
        else:
            by_url[n.url] = n
            order.append(n.url)
            plan.added += 1

    plan.kept = len(existing_urls - {n.url for n in incoming if n.url})
    plan.final = len(order)
    return [by_url[u] for u in order if u in by_url], plan


def convert_text(
    text: str,
    *,
    source: str = "stdin",
    format_hint: str = "",
) -> ImportResult:
    """Parse+validate one blob; no disk write."""
    result = ImportResult(ok=False)
    try:
        fmt, proxies = parse_text(text, source=source, format_hint=format_hint)
    except ParseError as exc:
        result.errors.append(str(exc))
        result.reports.append(
            ProfileReport(source=source, format=getattr(exc, "format", "unknown"), issues=[
                ValidationIssue(level="error", code="parse", message=str(exc), name="")
            ])
        )
        return result
    result.format = fmt
    accepted, issues = validate_proxy_list(proxies)
    report = ProfileReport(
        source=source,
        format=fmt,
        total=len(proxies),
        accepted=len(accepted),
        rejected=len(proxies) - len(accepted),
        issues=issues,
    )
    result.reports.append(report)
    _split_into(result, accepted, source=Path(source).stem or "stdin")
    result.ok = bool(result.dialable or result.protocol)
    if not result.ok and not result.errors:
        result.errors.append("all proxies rejected by validation")
    return result


def convert_paths(
    paths: list[Path | str],
    *,
    format_hint: str = "",
    max_profile_proxies: int = 400,
) -> ImportResult:
    """Parse+validate many files; no disk write until pack_result()."""
    result = ImportResult(ok=False)
    formats: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            files = sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml")) + sorted(path.glob("*.json")) + sorted(path.glob("*.txt"))
        else:
            files = [path]
        for f in files:
            if not f.is_file():
                result.errors.append(f"missing file: {f}")
                continue
            try:
                fmt, proxies = parse_file(f, format_hint=format_hint)
            except ParseError as exc:
                result.errors.append(f"{f.name}: {exc}")
                result.reports.append(
                    ProfileReport(
                        source=str(f),
                        format=getattr(exc, "format", "unknown"),
                        issues=[ValidationIssue(level="error", code="parse", message=str(exc))],
                    )
                )
                continue
            formats.append(fmt)
            large = len(proxies) > int(max_profile_proxies)
            accepted, issues = validate_proxy_list(proxies)
            if large:
                issues.append(
                    ValidationIssue(
                        level="warn",
                        code="large_profile",
                        message=f"profile has {len(proxies)} proxies; only dialable HTTP/SOCKS packed into core path skipped for bulk",
                    )
                )
            report = ProfileReport(
                source=str(f),
                format=fmt,
                total=len(proxies),
                accepted=len(accepted),
                rejected=len(proxies) - len(accepted),
                issues=issues,
            )
            result.reports.append(report)
            # for mega free lists: still take dialable; protocol only if not large
            if large:
                dialable_only = [
                    p
                    for p in accepted
                    if str(p.get("type") or "").lower() in DIALABLE_TYPES
                ]
                _split_into(result, dialable_only, source=f.stem, allow_protocol=False)
            else:
                _split_into(result, accepted, source=f.stem, allow_protocol=True)
    if formats:
        result.format = formats[0] if len(set(formats)) == 1 else "mixed"
    result.ok = bool(result.dialable or result.protocol)
    if not result.ok and not result.errors:
        result.errors.append("no usable proxies after validation")
    return result


def _split_into(
    result: ImportResult,
    proxies: list[dict[str, Any]],
    *,
    source: str,
    allow_protocol: bool = True,
) -> None:
    seen_http = {n.url for n in result.dialable}
    seen_sig = {_sig(p) for p in result.protocol}
    seen_name = {str(p.get("name") or "") for p in result.protocol}
    type_counter: Counter[str] = Counter(result.types)

    for p in proxies:
        t = str(p.get("type") or "").lower().strip()
        type_counter[t] += 1
        n = node_from_dialable(p, source=source)
        if n and n.url not in seen_http:
            seen_http.add(n.url)
            result.dialable.append(n)
        if not allow_protocol:
            continue
        if t in DIALABLE_TYPES:
            # already in dialable; also keep in core pack so select works if desired
            pass
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        s = _sig(p)
        if s in seen_sig:
            continue
        if name in seen_name:
            name = f"{name}#{s[:6]}"
            p = dict(p)
            p["name"] = name
        seen_sig.add(s)
        seen_name.add(name)
        # protocol pack includes dialable too when allow_protocol — useful for unified core
        # but dialable-only lists often huge; only add non-dialable + modest dialable
        if t in DIALABLE_TYPES:
            continue  # core group focuses protocol; list path uses nodes.json
        result.protocol.append(p if p.get("name") == name else {**p, "name": name})

    result.types = dict(type_counter)
    result.needs_core = bool(result.protocol)


def build_runtime_dict(
    protocol_proxies: list[dict[str, Any]],
    *,
    mixed_port: int = DEFAULT_MIXED_PORT,
    controller: str = DEFAULT_CONTROLLER,
    group: str = DEFAULT_GROUP,
) -> dict[str, Any]:
    names = [str(p.get("name") or "") for p in protocol_proxies]
    return {
        "mixed-port": int(mixed_port),
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "info",
        "ipv6": True,
        "external-controller": str(controller),
        "secret": "",
        "unified-delay": True,
        "tcp-concurrent": True,
        "find-process-mode": "off",
        "dns": {
            "enable": True,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["8.8.8.8", "1.1.1.1"],
            "fallback": ["8.8.4.4", "1.0.0.1"],
        },
        "proxies": protocol_proxies,
        "proxy-groups": [
            {"name": group, "type": "select", "proxies": names + ["DIRECT"]},
        ],
        "rules": [f"MATCH,{group}"],
    }


def pack_result(
    result: ImportResult,
    *,
    nodes_home: Path | None = None,
    nodes_json: Path | None = None,
    mixed_port: int = DEFAULT_MIXED_PORT,
    controller: str = DEFAULT_CONTROLLER,
    write_nodes: bool = True,
    write_runtime: bool = True,
    archive_sources: list[Path] | None = None,
    replace_nodes: bool = False,
) -> ImportResult:
    """Write nodes.json + runtime.yaml + meta.

    Dialable catalog defaults to **merge by URL** (keeps existing entries).
    Pass ``replace_nodes=True`` to overwrite nodes.json with only this import.
    Protocol runtime.yaml is always replaced when protocol proxies are present.
    """
    home = (nodes_home or (_ROOT / ".nodes")).expanduser().resolve()
    cfg_dir = home / "config"
    imp = home / "profiles" / "import"
    runtime_dir = home / "runtime"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    imp.mkdir(parents=True, exist_ok=True)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    if archive_sources:
        for src in archive_sources:
            src = Path(src)
            if src.is_file():
                dest = imp / f"profile-{src.name}"
                if src.resolve() != dest.resolve():
                    shutil.copy2(src, dest)

    nodes_path = (nodes_json or (_ROOT / "nodes.json")).expanduser().resolve()
    if write_nodes and result.dialable:
        from register_core.nodes.catalog import load_nodes, save_nodes

        # Always load existing for merge plan stats (replace still reports dropped).
        existing = load_nodes(nodes_path)
        merged, plan = merge_dialable(existing, result.dialable, replace=replace_nodes)
        result.merge = plan
        save_nodes(merged, nodes_path)
        result.nodes_path = str(nodes_path)
        result.dialable = merged
        try:
            nodes_path.chmod(0o600)
        except Exception:
            pass
    elif write_nodes and not result.dialable and replace_nodes:
        from register_core.nodes.catalog import load_nodes, save_nodes

        existing = load_nodes(nodes_path)
        result.merge = MergePlan(
            mode="replace",
            existing=len(existing),
            incoming=0,
            dropped=len(existing),
            final=0,
        )
        save_nodes([], nodes_path)
        result.nodes_path = str(nodes_path)

    if write_runtime and result.protocol:
        if yaml is None:
            result.errors.append("PyYAML required to write runtime.yaml")
            result.ok = bool(result.dialable)
            return result
        runtime = build_runtime_dict(
            result.protocol, mixed_port=mixed_port, controller=controller
        )
        runtime_path = cfg_dir / "runtime.yaml"
        runtime_path.write_text(
            yaml.safe_dump(runtime, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        try:
            runtime_path.chmod(0o600)
        except Exception:
            pass
        result.runtime_path = str(runtime_path)

        meta = {
            "group": DEFAULT_GROUP,
            "mixed_port": int(mixed_port),
            "controller": str(controller),
            "proxy_url": f"http://127.0.0.1:{int(mixed_port)}",
            "names": [str(p.get("name") or "") for p in result.protocol],
            "types": dict(Counter(str(p.get("type")) for p in result.protocol)),
            "http_socks_imported": len(result.dialable),
            "needs_core": True,
        }
        meta_path = cfg_dir / "proxy-names.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result.meta_path = str(meta_path)
    elif write_runtime and not result.protocol and result.dialable:
        # list-only import: leave a small meta note that core is not needed
        meta = {
            "group": DEFAULT_GROUP,
            "mixed_port": int(mixed_port),
            "controller": str(controller),
            "proxy_url": f"http://127.0.0.1:{int(mixed_port)}",
            "names": [],
            "types": {},
            "http_socks_imported": len(result.dialable),
            "needs_core": False,
        }
        meta_path = cfg_dir / "proxy-names.json"
        # do not overwrite a rich protocol meta with empty if exists and has names
        if meta_path.is_file():
            try:
                old = json.loads(meta_path.read_text(encoding="utf-8"))
                if old.get("names"):
                    meta_path = cfg_dir / "proxy-names.list-only.json"
            except Exception:
                pass
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result.meta_path = str(meta_path)

    result.ok = bool(result.dialable or result.protocol) and not any(
        e.startswith("PyYAML") for e in result.errors
    )
    result.needs_core = bool(result.protocol)
    return result
