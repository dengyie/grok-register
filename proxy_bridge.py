"""Local HTTP proxy bridge so Chromium can use user:pass upstream proxies.

Chromium --proxy-server cannot embed credentials. This module starts a
127.0.0.1 bridge that injects Proxy-Authorization toward the real upstream.
No-auth proxies are returned unchanged (no bridge process).
"""

from __future__ import annotations

import base64
import select
import socket
import socketserver
import ssl
import threading
import urllib.parse
from typing import Optional, Tuple


def parse_proxy_url(proxy: str | None) -> Optional[urllib.parse.SplitResult]:
    raw = str(proxy or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        return urllib.parse.urlsplit(raw)
    except Exception:
        return None


def safe_proxy_port(parsed: urllib.parse.SplitResult | None) -> Optional[int]:
    if parsed is None:
        return None
    try:
        return parsed.port
    except Exception:
        return None


def proxy_has_auth(proxy: str | None) -> bool:
    parsed = parse_proxy_url(proxy)
    return bool(parsed and parsed.hostname and (parsed.username is not None or parsed.password is not None))


def strip_proxy_auth(proxy: str | None) -> str:
    raw = str(proxy or "").strip()
    parsed = parse_proxy_url(raw)
    if not parsed or not parsed.hostname:
        return raw
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = safe_proxy_port(parsed)
    netloc = f"{host}:{port}" if port else host
    stripped = urllib.parse.urlunsplit(
        (parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment)
    )
    if "://" not in raw:
        return stripped.split("://", 1)[1]
    return stripped


def proxy_log_label(proxy: str | None) -> str:
    p = str(proxy or "").strip()
    if not p:
        return ""
    try:
        u = parse_proxy_url(p)
        if not u or not u.hostname:
            return "(proxy)"
        host = u.hostname
        port = safe_proxy_port(u)
        auth = "user:***@" if u.username else ""
        return f"{u.scheme or 'http'}://{auth}{host}{(':' + str(port)) if port else ''}"
    except Exception:
        return "(proxy)"


class _ReusableThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _recv_until_headers(sock: socket.socket, timeout: float = 20, limit: int = 65536) -> bytes:
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _relay(left: socket.socket, right: socket.socket, timeout: float = 60) -> None:
    left.settimeout(timeout)
    right.settimeout(timeout)
    sockets = [left, right]
    while True:
        readable, _, _ = select.select(sockets, [], [], timeout)
        if not readable:
            return
        for sock in readable:
            data = sock.recv(65536)
            if not data:
                return
            peer = right if sock is left else left
            peer.sendall(data)


class _LocalAuthProxyBridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        bridge: LocalAuthProxyBridge = self.server.bridge  # type: ignore[attr-defined]
        upstream = None
        try:
            initial = _recv_until_headers(self.request, timeout=bridge.timeout)
            if not initial:
                return
            first_line = initial.split(b"\r\n", 1)[0].decode("latin1", "ignore")
            if first_line.upper().startswith("CONNECT "):
                target = first_line.split()[1]
                upstream = bridge.open_upstream()
                req = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
                if bridge.auth_header:
                    req.append(f"Proxy-Authorization: Basic {bridge.auth_header}")
                upstream.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin1"))
                response = _recv_until_headers(upstream, timeout=bridge.timeout)
                if response:
                    self.request.sendall(response)
                status = response.split(b"\r\n", 1)[0] if response else b""
                if b" 200 " not in status:
                    return
                _relay(self.request, upstream, timeout=bridge.relay_timeout)
            else:
                upstream = bridge.open_upstream()
                upstream.sendall(bridge.inject_proxy_auth(initial))
                _relay(self.request, upstream, timeout=bridge.relay_timeout)
        except Exception:
            return
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except Exception:
                    pass


class LocalAuthProxyBridge:
    """127.0.0.1 bridge that authenticates to an upstream HTTP(S) proxy for Chromium."""

    def __init__(self, proxy_url: str):
        parsed = parse_proxy_url(proxy_url)
        if not parsed or not parsed.hostname:
            raise ValueError("invalid proxy url for auth bridge")
        scheme = (parsed.scheme or "http").lower()
        if scheme not in ("http", "https"):
            raise ValueError("auth proxy bridge only supports http/https upstream")
        self.upstream_scheme = scheme
        self.upstream_host = parsed.hostname
        self.upstream_port = safe_proxy_port(parsed) or (443 if scheme == "https" else 80)
        username = urllib.parse.unquote(parsed.username or "")
        password = urllib.parse.unquote(parsed.password or "")
        raw_auth = f"{username}:{password}".encode("utf-8")
        self.auth_header = base64.b64encode(raw_auth).decode("ascii") if (username or password) else ""
        self.timeout = 20
        self.relay_timeout = 90
        self.server: _ReusableThreadingTCPServer | None = None
        self.thread: threading.Thread | None = None
        self.local_proxy = ""

    def open_upstream(self) -> socket.socket:
        sock = socket.create_connection((self.upstream_host, self.upstream_port), timeout=self.timeout)
        if self.upstream_scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=self.upstream_host)
        sock.settimeout(self.timeout)
        return sock

    def inject_proxy_auth(self, data: bytes) -> bytes:
        if not self.auth_header or b"\r\n\r\n" not in data:
            return data
        if b"\r\nproxy-authorization:" in data.lower():
            return data
        head, body = data.split(b"\r\n\r\n", 1)
        auth_line = f"Proxy-Authorization: Basic {self.auth_header}".encode("latin1")
        return head + b"\r\n" + auth_line + b"\r\n\r\n" + body

    def start(self) -> str:
        self.server = _ReusableThreadingTCPServer(("127.0.0.1", 0), _LocalAuthProxyBridgeHandler)
        self.server.bridge = self  # type: ignore[attr-defined]
        port = self.server.server_address[1]
        self.local_proxy = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="proxy-auth-bridge")
        self.thread.start()
        return self.local_proxy

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None
        self.thread = None
        self.local_proxy = ""


def resolve_browser_proxy(proxy: str | None) -> Tuple[str, Optional[LocalAuthProxyBridge]]:
    """Map config proxy → Chromium --proxy-server value + optional bridge.

    Returns:
      ("", None)                       — no proxy
      (proxy_url, None)                — no-auth or non-http scheme (auth stripped)
      (local_bridge_url, bridge)       — auth http(s) proxy via local bridge
    """
    raw = str(proxy or "").strip()
    if not raw:
        return "", None
    if not proxy_has_auth(raw):
        # Ensure scheme for Chromium
        if "://" not in raw:
            return f"http://{raw}", None
        return raw, None
    parsed = parse_proxy_url(raw)
    scheme = (parsed.scheme or "http").lower() if parsed else ""
    if scheme in ("http", "https"):
        bridge = LocalAuthProxyBridge(raw)
        local = bridge.start()
        return local, bridge
    # socks etc.: Chromium cannot use user:pass; strip and hope
    return strip_proxy_auth(raw), None
