"""Register-machine hook: mint CPA xai auth after successful registration.

OIDC package lives at ./cpa_xai (bundled with this project).
Optional override: config `api_reverse_tools` / env `API_REVERSE_TOOLS`
points at a directory that *contains* the `cpa_xai` package.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

_REG_DIR = Path(__file__).resolve().parent
_DEFAULT_OUT = _REG_DIR / "cpa_auths"
_DEFAULT_CPA = Path("")  # empty = do not assume a machine-local CPA path
_REMOTE_DIR_READY: set[str] = set()


def _ensure_cpa_xai_on_path(tools_dir: str | Path | None = None) -> Path:
    """Put the parent of `cpa_xai` on sys.path. Default: this project root."""
    if tools_dir:
        tools = Path(tools_dir).expanduser().resolve()
    else:
        env = (os.environ.get("API_REVERSE_TOOLS") or "").strip()
        tools = Path(env).expanduser().resolve() if env else _REG_DIR
    # If user pointed at .../cpa_xai itself, use its parent
    if tools.name == "cpa_xai" and (tools / "__init__.py").is_file():
        tools = tools.parent
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    return tools


def export_cookies_from_page(page: Any) -> list[dict]:
    """Best-effort export of cookies from a DrissionPage tab/browser."""
    if page is None:
        return []
    cookies = None
    for getter in (
        lambda: page.cookies(all_domains=True, all_info=True),
        lambda: page.cookies(all_domains=True),
        lambda: page.cookies(),
    ):
        try:
            cookies = getter()
            if cookies:
                break
        except TypeError:
            continue
        except Exception:
            continue
    if not cookies:
        try:
            browser = getattr(page, "browser", None)
            if browser is not None:
                cookies = browser.cookies()
        except Exception:
            cookies = None
    if isinstance(cookies, list):
        return [c for c in cookies if isinstance(c, dict)]
    return []



def _config_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on", "y"}:
        return True
    if s in {"0", "false", "no", "off", "n", ""}:
        return False
    return default


def _config_priority(cfg: dict | None, default: int = 1000) -> int:
    """CPA auth-file routing weight (CLIProxyAPI priority field)."""
    raw = (cfg or {}).get("cpa_auth_priority", default)
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        return default
    try:
        return int(raw)
    except Exception:
        return default


DEFAULT_LIVE_REMOTE_AUTH_DIR = "/root/.cli-proxy-api"
DEFAULT_INVENTORY_REMOTE_AUTH_DIR = "/personal/cpa/auths"


def resolve_live_remote_auth_dir(cfg: dict | None = None) -> str:
    """Canonical live CPA auth-dir (one-click success gate)."""
    cfg = cfg or {}
    raw = str(cfg.get("cpa_remote_live_dir") or DEFAULT_LIVE_REMOTE_AUTH_DIR).strip().rstrip("/")
    return raw or DEFAULT_LIVE_REMOTE_AUTH_DIR


def is_live_remote_auth_dir(path: str | None, cfg: dict | None = None) -> bool:
    return str(path or "").strip().rstrip("/") == resolve_live_remote_auth_dir(cfg)


def resolve_remote_auth_dirs(cfg: dict | None) -> list[str]:
    """Remote CPA auth-dirs to inject into (live first, then inventory).

    Config priority:
      1) cpa_remote_auth_dirs — list or comma-separated (explicit wins)
      2) if cpa_remote_inject: default live+inventory
         /root/.cli-proxy-api,/personal/cpa/auths
      3) cpa_remote_auth_dir — legacy single dir (only when inject is off
         or as the sole explicit choice via cpa_remote_auth_dirs)

    One-click CPA requires the live pool; inventory-only is not enough.
    To inject a single custom dir, set cpa_remote_auth_dirs to that path only.
    """
    cfg = cfg or {}
    live = resolve_live_remote_auth_dir(cfg)
    raw = cfg.get("cpa_remote_auth_dirs")
    dirs: list[str] = []
    if isinstance(raw, (list, tuple)):
        dirs = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str) and raw.strip():
        dirs = [p.strip() for p in raw.split(",") if p.strip()]
    if not dirs:
        if _config_bool(cfg.get("cpa_remote_inject"), default=False):
            # One-click: always live + inventory unless multi dirs explicitly set.
            dirs = [live, DEFAULT_INVENTORY_REMOTE_AUTH_DIR]
        else:
            single = str(cfg.get("cpa_remote_auth_dir") or "").strip()
            if single:
                dirs = [single]
    # de-dup preserve order
    seen: set[str] = set()
    out: list[str] = []
    for d in dirs:
        d = d.rstrip("/")
        if not d or d in seen:
            continue
        seen.add(d)
        out.append(d)
    return out


def evaluate_remote_inject_gate(
    result: dict | None = None,
    cfg: dict | None = None,
    *,
    auth_path: str | Path | None = None,
) -> dict:
    """Product hard-gate for remote CPA inject: only chat-usable free Build.

    Returns:
      allow, reason, import_gate, chat_ok, entitlement_denied, usable

    Rules (defaults = production one-click):
      - always refuse entitlement_denied
      - when ``cpa_remote_inject_require_chat_ok`` (default True): require chat_ok is True
      - refuse usable is False when chat gate is on
      - may load stamps from local auth JSON when result lacks chat fields
    """
    cfg = cfg or {}
    r: dict = dict(result or {})
    path = auth_path or r.get("path")
    if path and (
        r.get("chat_ok") is None
        or "entitlement_denied" not in r
        or "usable" not in r
        or not r.get("import_gate")
    ):
        try:
            p = Path(path).expanduser()
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k in (
                        "chat_ok",
                        "entitlement_denied",
                        "usable",
                        "chat_retryable",
                        "fail_reason",
                        "import_gate",
                        "chat_error_code",
                    ):
                        if k not in r or r.get(k) is None:
                            if k in data:
                                r[k] = data.get(k)
        except Exception:
            pass

    require_chat = _config_bool(cfg.get("cpa_remote_inject_require_chat_ok"), default=True)
    probe_chat = _config_bool(cfg.get("cpa_probe_chat"), default=True)
    # Soft-pass local write must never become a live inject bypass.
    if not require_chat:
        # Operator explicitly disabled chat inject gate — still refuse hard entitlement.
        if r.get("entitlement_denied") is True:
            return {
                "allow": False,
                "reason": "entitlement_denied",
                "import_gate": "entitlement_denied",
                "chat_ok": False,
                "entitlement_denied": True,
                "usable": False,
            }
        return {
            "allow": True,
            "reason": "chat_gate_disabled",
            "import_gate": str(r.get("import_gate") or "chat_gate_disabled"),
            "chat_ok": r.get("chat_ok"),
            "entitlement_denied": bool(r.get("entitlement_denied")),
            "usable": r.get("usable"),
        }

    if r.get("entitlement_denied") is True:
        return {
            "allow": False,
            "reason": "entitlement_denied",
            "import_gate": "entitlement_denied",
            "chat_ok": False,
            "entitlement_denied": True,
            "usable": False,
        }
    if r.get("skip_remote_inject") is True and r.get("chat_ok") is not True:
        reason = str(r.get("remote_inject_skip_reason") or r.get("fail_reason") or "skip_remote_inject")
        return {
            "allow": False,
            "reason": reason,
            "import_gate": reason,
            "chat_ok": bool(r.get("chat_ok")),
            "entitlement_denied": False,
            "usable": False,
        }
    if probe_chat and r.get("chat_ok") is not True:
        reason = str(r.get("fail_reason") or r.get("import_gate") or "chat_not_ok")
        if reason in ("", "None"):
            reason = "chat_not_ok"
        return {
            "allow": False,
            "reason": reason,
            "import_gate": reason if reason != "chat_ok" else "chat_not_ok",
            "chat_ok": False,
            "entitlement_denied": False,
            "usable": False,
        }
    if r.get("usable") is False:
        reason = str(r.get("fail_reason") or "unusable")
        return {
            "allow": False,
            "reason": reason,
            "import_gate": reason,
            "chat_ok": bool(r.get("chat_ok")),
            "entitlement_denied": bool(r.get("entitlement_denied")),
            "usable": False,
        }
    return {
        "allow": True,
        "reason": "chat_ok",
        "import_gate": "chat_ok",
        "chat_ok": True,
        "entitlement_denied": False,
        "usable": True if r.get("usable") is not False else False,
    }


def apply_multi_remote_inject(
    result: dict,
    cfg: dict | None = None,
    *,
    log_callback: Callable[[str], None] | None = None,
    inject_fn: Callable[..., dict] | None = None,
) -> dict:
    """Inject minted auth into all resolved remote dirs; enforce live success gate.

    Mutates and returns ``result``. Product rule:
      - **chat_ok hard-gate** (default): refuse inject unless free Build chat probe passed.
      - when live dir is among targets and ``cpa_remote_live_required`` (default true),
        live failure hard-fails export even if inventory succeeded.
      - ``cpa_remote_inject_required`` still means *all* dirs must succeed.
    """
    log = log_callback or (lambda _m: None)
    cfg = cfg or {}
    if not result.get("ok") or not result.get("path"):
        return result
    if not _config_bool(cfg.get("cpa_remote_inject"), default=False):
        return result

    gate = evaluate_remote_inject_gate(result, cfg)
    result["import_gate"] = gate.get("import_gate") or result.get("import_gate")
    if not gate.get("allow"):
        reason = str(gate.get("reason") or "chat_not_ok")
        result["remote_inject_skipped"] = True
        result["remote_inject_skip_reason"] = reason
        result["remote_live_ok"] = False
        result["remote_inject"] = {
            "ok": False,
            "skipped": True,
            "reason": reason,
        }
        log(f"[cpa] skip remote inject (gate={reason}): {result.get('email') or result.get('path')}")
        return result

    inject = inject_fn or inject_cpa_auth_remote
    remote_dirs = resolve_remote_auth_dirs(cfg)
    remote_results: list[dict] = []
    any_ok = False
    errors: list[str] = []
    for rdir in remote_dirs:
        inj_cfg = dict(cfg)
        inj_cfg["cpa_remote_inject"] = True
        inj_cfg["cpa_remote_auth_dir"] = rdir
        # prevent recursive multi-expand inside single-dir injector
        inj_cfg["cpa_remote_auth_dirs"] = [rdir]
        remote_res = inject(
            result["path"],
            config=inj_cfg,
            log_callback=log,
        )
        remote_results.append({"dir": rdir, **remote_res})
        if remote_res.get("ok"):
            any_ok = True
        else:
            errors.append(
                f"{rdir}:{remote_res.get('error') or remote_res.get('reason') or 'fail'}"
            )

    result["remote_injects"] = remote_results
    live_dir = resolve_live_remote_auth_dir(cfg)
    live_attempts = [r for r in remote_results if is_live_remote_auth_dir(r.get("dir"), cfg)]
    inv_attempts = [r for r in remote_results if not is_live_remote_auth_dir(r.get("dir"), cfg)]
    live_ok = any(bool(r.get("ok")) for r in live_attempts) if live_attempts else None
    inv_ok = any(bool(r.get("ok")) for r in inv_attempts) if inv_attempts else None
    all_ok = bool(remote_results) and all(
        bool(r.get("ok") or r.get("skipped")) for r in remote_results
    )
    result["remote_live_dir"] = live_dir
    result["remote_live_ok"] = live_ok
    result["remote_inventory_ok"] = inv_ok
    result["remote_inject_all_ok"] = all_ok

    # Product summary prefers live success; inventory-only is not one-click success.
    if live_attempts:
        summary = next((r for r in live_attempts if r.get("ok")), live_attempts[-1])
    else:
        summary = next(
            (r for r in remote_results if r.get("ok")),
            remote_results[-1] if remote_results else {"ok": False, "error": "no remote dirs"},
        )
    result["remote_inject"] = summary

    ok_paths = [r.get("remote_path") for r in remote_results if r.get("ok")]
    if ok_paths:
        live_paths = [
            r.get("remote_path")
            for r in live_attempts
            if r.get("ok") and r.get("remote_path")
        ]
        result["remote_path"] = live_paths[0] if live_paths else ok_paths[0]
        result["remote_paths"] = ok_paths
    if errors:
        result["remote_inject_partial_errors"] = errors
        log(f"[cpa] remote inject partial: ok={len(ok_paths)} fail={len(errors)}")

    live_required = _config_bool(cfg.get("cpa_remote_live_required"), default=True)
    inject_required = _config_bool(cfg.get("cpa_remote_inject_required"), default=False)
    fail_reasons: list[str] = []
    if live_attempts and not live_ok:
        fail_reasons.append(
            f"live inject failed ({live_dir}): "
            + "; ".join(
                f"{r.get('dir')}:{r.get('error') or r.get('reason') or 'fail'}"
                for r in live_attempts
                if not r.get("ok")
            )
        )
    if not any_ok:
        fail_reasons.append("; ".join(errors) if errors else "remote inject failed")
    if inject_required and not all_ok:
        fail_reasons.append("cpa_remote_inject_required: not all remote dirs succeeded")

    if fail_reasons:
        seen_fr: set[str] = set()
        uniq_fr: list[str] = []
        for fr in fail_reasons:
            if fr not in seen_fr:
                seen_fr.add(fr)
                uniq_fr.append(fr)
        result["remote_inject_error"] = "; ".join(uniq_fr)
        hard_fail = bool(
            (live_attempts and not live_ok and live_required)
            or (inject_required and not all_ok)
            or (not any_ok and inject_required)
        )
        if hard_fail:
            result["ok"] = False
            result["error"] = (
                f"remote inject required but failed: {result['remote_inject_error']}"
            )
            log(f"[cpa] remote inject hard-fail: {result['remote_inject_error']}")
        else:
            log(f"[cpa] remote inject soft-fail: {result['remote_inject_error']}")
    elif live_attempts and live_ok:
        log(f"[cpa] remote live inject ok: {result.get('remote_path')}")
    return result


def ensure_auth_file_priority(
    path: str | Path,
    *,
    priority: int = 1000,
    log_callback: Callable[[str], None] | None = None,
) -> int:
    """Ensure local xai-*.json has priority field; return applied value."""
    log = log_callback or (lambda _m: None)
    src = Path(path)
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] priority ensure read failed: {e}")
        return priority
    try:
        want = int(priority)
    except Exception:
        want = 1000
    if data.get("priority") == want:
        return want
    data["priority"] = want
    try:
        src.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.chmod(src, 0o600)
        except Exception:
            pass
        log(f"[cpa] priority set {want} on {src.name}")
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] priority ensure write failed: {e}")
    return want


def _resolve_remote_ssh_password(cfg: dict) -> str:
    """Resolve password for tebi/Bohrium SSH without printing it.

    Priority:
      1) env CPA_REMOTE_SSHPASS / SSHPASS
      2) cfg.cpa_remote_ssh_password (not recommended)
      3) credentials file (default ~/.ssh/bohrium_credentials)
         format: alias|host|user|port|password
    """
    for key in ("CPA_REMOTE_SSHPASS", "SSHPASS"):
        v = (os.environ.get(key) or "").strip()
        if v:
            return v
    inline = str(cfg.get("cpa_remote_ssh_password") or "").strip()
    if inline:
        return inline

    cred_file = (cfg.get("cpa_remote_credentials_file") or "~/.ssh/bohrium_credentials").strip()
    path = Path(cred_file).expanduser()
    if not path.is_file():
        return ""
    alias = str(cfg.get("cpa_remote_credential_alias") or "tebi").strip().lower()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    candidates: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        a = parts[0].strip().lower()
        pwd = parts[4].strip()
        if not pwd:
            continue
        if a == alias or a.startswith(alias) or alias in a:
            return pwd
        candidates.append(pwd)
    # fallback: first credential line if only one useful entry
    return candidates[0] if len(candidates) == 1 else ""


def inject_cpa_auth_remote(
    local_path: str | Path,
    *,
    config: dict | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Upload minted xai-*.json into remote CPA auth-dir (default tebi).

    Real-run lesson: tebi-tunnel multi-hop mkdir+scp+chmod ≈ 24s.
    Prefer ONE ssh hop: `cat > file` from stdin + chmod/stat in the same remote shell.
    ControlMaster reuses the tunnel for subsequent accounts.
    """
    import shlex
    import shutil as _shutil
    import subprocess

    global _REMOTE_DIR_READY

    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))
    if not _config_bool(cfg.get("cpa_remote_inject"), default=False):
        return {"ok": False, "skipped": True, "reason": "disabled"}

    src = Path(local_path).expanduser()
    if not src.is_file():
        msg = f"local auth missing: {src}"
        log(f"[cpa] remote inject failed: {msg}")
        return {"ok": False, "error": msg}

    # Defense-in-depth: direct callers cannot upload non-chat_ok auths.
    gate = evaluate_remote_inject_gate({"path": str(src)}, cfg, auth_path=src)
    if not gate.get("allow"):
        reason = str(gate.get("reason") or "chat_not_ok")
        log(f"[cpa] remote inject refused (gate={reason}): {src.name}")
        return {
            "ok": False,
            "skipped": True,
            "reason": reason,
            "import_gate": gate.get("import_gate") or reason,
        }

    host = (cfg.get("cpa_remote_ssh_host") or "tebi-tunnel").strip()
    remote_dir = (cfg.get("cpa_remote_auth_dir") or "/personal/cpa/auths").strip().rstrip("/")
    user = (cfg.get("cpa_remote_ssh_user") or "").strip()
    target_host = f"{user}@{host}" if user and "@" not in host else host
    remote_file = f"{remote_dir}/{src.name}"
    remote_path = f"{target_host}:{remote_file}"
    timeout = float(cfg.get("cpa_remote_inject_timeout_sec", 60) or 60)

    password = _resolve_remote_ssh_password(cfg)
    sshpass = _shutil.which("sshpass")
    if password and not sshpass:
        msg = "sshpass not found (brew install sshpass / apt install sshpass)"
        log(f"[cpa] remote inject failed: {msg}")
        return {"ok": False, "error": msg}

    env = os.environ.copy()
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    if password:
        env["SSHPASS"] = password

    # Short ControlPath (macOS AF_UNIX path limit ~104 bytes)
    control_path = "/tmp/grcm-%C"
    ssh_common = [
        "-o", "BatchMode=no",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=15",
        "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=2",
        "-o", "PreferredAuthentications=password",
        "-o", "PubkeyAuthentication=no",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={control_path}",
        "-o", "ControlPersist=180",
    ]

    def _wrap(cmd: list[str]) -> list[str]:
        if password and sshpass:
            return [sshpass, "-e", *cmd]
        return cmd

    def _decode(data: bytes | None) -> str:
        return (data or b"").decode("utf-8", "replace")

    def _run(
        cmd: list[str],
        what: str,
        *,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        log(f"[cpa] remote inject {what}: {' '.join(cmd[:8])}...")
        return subprocess.run(
            cmd,
            env=env,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    try:
        try:
            os.chmod(src, 0o600)
        except Exception:
            pass
        ensure_auth_file_priority(
            src,
            priority=_config_priority(cfg),
            log_callback=log,
        )
        payload = src.read_bytes()
        local_size = len(payload)

        # Single remote shell: mkdir, atomic write via tmp, chmod, print size/mode.
        # Quote paths for remote sh.
        rd = shlex.quote(remote_dir)
        rf = shlex.quote(remote_file)
        rt = shlex.quote(remote_file + ".tmp")
        remote_sh = (
            f"mkdir -p {rd} && chmod 700 {rd} && "
            f"cat > {rt} && chmod 600 {rt} && mv -f {rt} {rf} && "
            f"stat -c '%s %a' {rf} 2>/dev/null || stat -f '%z %Lp' {rf}"
        )
        cmd = _wrap(["ssh", *ssh_common, target_host, remote_sh])
        r = _run(cmd, "pipe", input_bytes=payload)
        if r.returncode != 0:
            err = (_decode(r.stderr) + _decode(r.stdout)).strip()[:300]
            raise RuntimeError(f"pipe upload failed rc={r.returncode}: {err}")

        out = _decode(r.stdout).strip()
        detail = out.splitlines()[-1] if out else f"size={local_size}"
        try:
            remote_size = int(str(detail).split()[0])
            if remote_size != local_size:
                raise RuntimeError(f"size mismatch local={local_size} remote={remote_size}")
        except (ValueError, IndexError):
            # stat format unexpected — still accept if exit 0
            pass

        _REMOTE_DIR_READY.add(f"{target_host}:{remote_dir}")
        log(f"[cpa] remote inject ok -> {remote_path} ({detail})")
        return {
            "ok": True,
            "remote_path": remote_path,
            "remote_dir": remote_dir,
            "host": host,
            "name": src.name,
            "size": local_size,
            "detail": detail,
            "method": "ssh-pipe",
        }
    except subprocess.TimeoutExpired:
        msg = f"timeout after {timeout}s"
        log(f"[cpa] remote inject failed: {msg}")
        return {"ok": False, "error": msg}
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] remote inject pipe failed, fallback scp: {e}")
        try:
            mkdir_cmd = _wrap(
                ["ssh", *ssh_common, target_host, f"mkdir -p {shlex.quote(remote_dir)} && chmod 700 {shlex.quote(remote_dir)}"]
            )
            r1 = _run(mkdir_cmd, "mkdir")
            if r1.returncode != 0:
                err = (_decode(r1.stderr) + _decode(r1.stdout)).strip()[:300]
                raise RuntimeError(f"mkdir failed rc={r1.returncode}: {err}")
            scp_cmd = _wrap(["scp", *ssh_common, "-p", str(src), remote_path])
            r2 = _run(scp_cmd, "scp")
            if r2.returncode != 0:
                err = (_decode(r2.stderr) + _decode(r2.stdout)).strip()[:300]
                raise RuntimeError(f"scp failed rc={r2.returncode}: {err}")
            detail = f"size={src.stat().st_size}"
            vcmd = _wrap(
                [
                    "ssh",
                    *ssh_common,
                    target_host,
                    (
                        f"chmod 600 {shlex.quote(remote_file)} && "
                        f"stat -c '%s %a' {shlex.quote(remote_file)} 2>/dev/null || "
                        f"stat -f '%z %Lp' {shlex.quote(remote_file)}"
                    ),
                ]
            )
            r3 = _run(vcmd, "verify")
            if r3.returncode == 0:
                lines = _decode(r3.stdout).strip().splitlines()
                if lines:
                    detail = lines[-1]
            log(f"[cpa] remote inject ok -> {remote_path} ({detail})")
            return {
                "ok": True,
                "remote_path": remote_path,
                "remote_dir": remote_dir,
                "host": host,
                "name": src.name,
                "size": src.stat().st_size,
                "detail": detail,
                "method": "scp-fallback",
            }
        except subprocess.TimeoutExpired:
            msg = f"timeout after {timeout}s"
            log(f"[cpa] remote inject failed: {msg}")
            return {"ok": False, "error": msg}
        except Exception as e2:  # noqa: BLE001
            log(f"[cpa] remote inject failed: {e2}")
            return {"ok": False, "error": str(e2)}


def finalize_probe_and_gate(
    result: dict,
    cfg: dict | None = None,
    *,
    email: str = "",
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Apply product success rules after mint_and_export.

    Table (probe_chat default on, probe_chat_required default on):
      entitlement_denied     → ok=False, non_retryable, skip inject (caller)
      chat transient/other   → ok=False when required; keep chat_retryable
      chat fail + !required  → soft-pass ok=True + warning (never for entitlement)
      models miss + !chat    → legacy soft-pass when cpa_probe_required=false
    """
    log = log_callback or (lambda _m: None)
    cfg = cfg or {}
    probe_chat = _config_bool(cfg.get("cpa_probe_chat"), default=True)
    probe_chat_required = _config_bool(cfg.get("cpa_probe_chat_required"), default=True)
    probe_required = _config_bool(cfg.get("cpa_probe_required"), default=False)

    err_s = str(result.get("error") or "")
    is_models_only_miss = err_s.startswith("token ok but grok-4.5 not listed")
    is_chat_fail = bool(
        result.get("entitlement_denied")
        or result.get("fail_reason")
        in (
            "entitlement_denied",
            "chat_failed",
            "auth_or_protocol",
            "transient",
            "models_missing_grok_45",
        )
        or err_s.startswith("chat probe failed")
        or err_s.startswith("chat entitlement denied")
    )

    # Surface chat fields for CLI stats.
    if "chat_ok" not in result and probe_chat:
        ch = result.get("probe_chat") or {}
        if isinstance(ch, dict) and ch:
            result["chat_ok"] = bool(ch.get("ok"))
            result.setdefault("entitlement_denied", bool(ch.get("entitlement_denied")))
            result.setdefault("chat_retryable", bool(ch.get("retryable")))

    # Entitlement always hard-fails product success.
    if result.get("entitlement_denied"):
        result["ok"] = False
        result["non_retryable"] = True
        result["chat_ok"] = False
        result["chat_retryable"] = False
        result["usable"] = False
        result["fail_reason"] = "entitlement_denied"
        result["skip_remote_inject"] = True
        if not result.get("error"):
            result["error"] = "chat entitlement denied (permission-denied)"
        log(
            f"[cpa] FAIL-FAST entitlement_denied for {email or result.get('email')}: "
            f"{result.get('error')} (not product-usable free Build)"
        )
        return result

    # Models-only soft-pass (legacy): only when chat probe is off.
    if (
        not result.get("ok")
        and result.get("path")
        and is_models_only_miss
        and not is_chat_fail
        and not probe_chat
        and not probe_required
    ):
        result["ok"] = True
        result["probe_warning"] = result.pop("error", "probe failed")
        log(f"[cpa] probe warning ignored (file already written): {result.get('probe_warning')}")
        return result

    if not result.get("ok") and is_chat_fail and probe_chat:
        if probe_chat_required:
            # Transient stays failed but retryable — do not remint-spin as entitlement.
            result["non_retryable"] = not bool(result.get("chat_retryable"))
            if result.get("chat_retryable"):
                log(
                    f"[cpa] chat probe transient-fail for {email or result.get('email')}: "
                    f"{result.get('error') or result.get('fail_reason')} "
                    "(local auth stamped chat_retryable; remint may re-probe)"
                )
            else:
                log(
                    f"[cpa] chat probe hard-fail for {email or result.get('email')}: "
                    f"{result.get('error') or result.get('fail_reason')}"
                )
            result["skip_remote_inject"] = True
        else:
            # Soft-pass non-entitlement chat fail when operator disables required.
            if result.get("path"):
                result["ok"] = True
                result["probe_chat_warning"] = result.pop("error", "chat probe failed")
                result["chat_ok"] = False
                log(
                    f"[cpa] chat probe warning ignored (cpa_probe_chat_required=false): "
                    f"{result.get('probe_chat_warning')}"
                )
    return result


def export_cpa_xai_for_account(
    email: str,
    password: str,
    *,
    page: Any | None = None,
    cookies: Any | None = None,
    sso: str | None = None,
    config: dict | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """Mint OIDC + write xai-<email>.json under register cpa_auths (and optional CPA auth-dir)."""
    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))

    if not _config_bool(cfg.get("cpa_export_enabled"), default=True):
        log("[cpa] export disabled")
        return {"ok": False, "skipped": True, "reason": "disabled"}

    tools_dir = cfg.get("api_reverse_tools") or cfg.get("cpa_xai_parent") or None
    _ensure_cpa_xai_on_path(tools_dir)

    try:
        from cpa_xai import mint_and_export  # type: ignore
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] import cpa_xai failed: {e}")
        return {"ok": False, "error": f"import: {e}"}

    out_dir = Path(cfg.get("cpa_auth_dir") or _DEFAULT_OUT).expanduser()
    if not out_dir.is_absolute():
        out_dir = (_REG_DIR / out_dir).resolve()

    hotload_raw = (cfg.get("cpa_hotload_dir") or "").strip()
    cpa_dir = Path(hotload_raw).expanduser() if hotload_raw else None
    if cpa_dir and not cpa_dir.is_absolute():
        cpa_dir = (_REG_DIR / cpa_dir).resolve()

    # Priority: cpa_proxy > proxy > env. Config must beat shell https_proxy.
    proxy = (cfg.get("cpa_proxy") or cfg.get("proxy") or "").strip()
    if not proxy:
        proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()
    # Default headed: headless is frequently Cloudflare-blocked on accounts.x.ai
    headless = _config_bool(cfg.get("cpa_headless"), default=False)
    probe = _config_bool(cfg.get("cpa_probe_after_write"), default=True)
    # Product default ON: models-only is not free-Build success (chat 403 common).
    probe_chat = _config_bool(cfg.get("cpa_probe_chat"), default=True)
    # When chat probe runs, deny soft-pass unless explicitly disabled.
    probe_chat_required = _config_bool(cfg.get("cpa_probe_chat_required"), default=True)
    timeout = float(cfg.get("cpa_mint_timeout_sec", 240))
    base_url = cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1"
    force_standalone = _config_bool(cfg.get("cpa_force_standalone"), default=True)
    cookie_inject = _config_bool(cfg.get("cpa_mint_cookie_inject"), default=True)
    reuse_browser = _config_bool(cfg.get("cpa_mint_browser_reuse"), default=True)
    recycle_every = int(cfg.get("cpa_mint_browser_recycle_every", 15) or 0)
    # Protocol (pure HTTP SSO device flow) first; browser only on failure.
    prefer_protocol = _config_bool(cfg.get("cpa_prefer_protocol"), default=True)
    protocol_only = _config_bool(cfg.get("cpa_protocol_only"), default=False)
    protocol_poll_timeout = float(cfg.get("cpa_protocol_poll_timeout_sec", 90) or 90)
    # PKCE authorization-code flow (default) yields chat-usable tokens; legacy
    # device-code flow is known to produce /models-ok-but-chat-403 tokens.
    protocol_flow = (str(cfg.get("cpa_protocol_flow") or "pkce")).strip().lower() or "pkce"
    # Default true: PKCE CreateCookieSetterLink often fails; device-flow still local-mints.
    # chat entitlement_denied remains hard-gated for remote inject (not remint-spin).
    allow_device_flow_fallback = _config_bool(
        cfg.get("cpa_allow_device_flow_fallback"), default=True
    )
    auth_priority = _config_priority(cfg)

    from cpa_xai.accounts import normalize_sso_cookie

    def _resolve_sso_val(raw_sso: str | None, cookie_list: Any) -> str:
        val = normalize_sso_cookie(raw_sso)
        if val:
            return val
        if isinstance(cookie_list, list):
            for c in cookie_list:
                if isinstance(c, dict) and c.get("name") in ("sso", "sso-rw") and c.get("value"):
                    val = normalize_sso_cookie(str(c.get("value")))
                    if val:
                        return val
        return ""

    # cookies: explicit arg > page export > none
    use_cookies = cookies
    if use_cookies is None and cookie_inject and page is not None:
        use_cookies = export_cookies_from_page(page)
    if not cookie_inject:
        use_cookies = None

    sso_val = _resolve_sso_val(sso, use_cookies)
    if cookie_inject and sso_val:
        # Always attach SSO cookie clones — register cookies alone often miss accounts.x.ai host
        base = list(use_cookies) if isinstance(use_cookies, list) else []
        for name in ("sso", "sso-rw"):
            for dom in (".x.ai", "accounts.x.ai", ".accounts.x.ai", "auth.x.ai", "grok.com", ".grok.com"):
                base.append({
                    "name": name,
                    "value": sso_val,
                    "domain": dom,
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                })
        use_cookies = base

    out_dir.mkdir(parents=True, exist_ok=True)
    log(
        f"[cpa] mint OIDC for {email} -> {out_dir} proxy={proxy or '(none)'} "
        f"cookies={len(use_cookies) if isinstance(use_cookies, list) else (1 if use_cookies else 0)} "
        f"reuse={reuse_browser} protocol={prefer_protocol}"
        f"{' only' if protocol_only else ''} sso={'yes' if sso_val else 'no'}"
    )

    def _log(msg: str) -> None:
        log(f"[cpa] {msg}")

    result = mint_and_export(
        email=email,
        password=password,
        auth_dir=out_dir,
        page=None if force_standalone else page,
        proxy=proxy or None,
        headless=headless,
        base_url=base_url,
        probe=probe,
        probe_chat=probe_chat,
        browser_timeout_sec=timeout,
        force_standalone=force_standalone,
        cookies=use_cookies,
        sso=sso_val or None,
        reuse_browser=reuse_browser,
        recycle_every=recycle_every,
        prefer_protocol=prefer_protocol,
        protocol_only=protocol_only,
        protocol_poll_timeout_sec=protocol_poll_timeout,
        protocol_flow=protocol_flow,
        allow_device_flow_fallback=allow_device_flow_fallback,
        priority=auth_priority,
        log=_log,
    )
    if result.get("mint_method"):
        log(f"[cpa] mint_method={result.get('mint_method')}")

    finalize_probe_and_gate(result, cfg, email=email, log_callback=log)

    if result.get("ok") and result.get("path"):
        ensure_auth_file_priority(
            result["path"],
            priority=auth_priority,
            log_callback=log,
        )
        result["priority"] = auth_priority

    # Product hard-gate: only free Build chat_ok may enter remote live/inventory.
    # evaluate_remote_inject_gate is also enforced inside apply_multi_remote_inject
    # so ops scripts cannot bypass by calling inject helpers directly.
    gate = evaluate_remote_inject_gate(result, cfg)
    result["import_gate"] = gate.get("import_gate") or result.get("import_gate")
    skip_inject = not bool(gate.get("allow"))
    if skip_inject:
        result["skip_remote_inject"] = True
        result["remote_inject_skipped"] = True
        result["remote_inject_skip_reason"] = str(gate.get("reason") or "chat_not_ok")
        if result.get("entitlement_denied"):
            log(f"[cpa] skip remote inject (entitlement_denied): {email}")
            try:
                from cpa_xai.writer import record_entitlement_denied

                record_entitlement_denied(
                    out_dir,
                    email,
                    extra={
                        "path": result.get("path"),
                        "chat_error_code": result.get("chat_error_code"),
                    },
                )
            except Exception as e:  # noqa: BLE001
                log(f"[cpa] entitlement ledger write failed: {e}")
        else:
            log(
                f"[cpa] skip remote inject ({result.get('remote_inject_skip_reason')}): {email}"
            )

    # Re-stamp full chat fields after finalize/gate (not only import_gate).
    # mint stamps pre-finalize; finalize may flip entitlement/ok/skip flags.
    if result.get("path"):
        try:
            from cpa_xai.writer import stamp_auth_chat_fields

            stamped = stamp_auth_chat_fields(result["path"], result)
            if stamped.get("import_gate"):
                result["import_gate"] = stamped["import_gate"]
        except Exception as e:  # noqa: BLE001
            log(f"[cpa] stamp chat fields failed: {e}")

    if (
        result.get("ok")
        and result.get("path")
        and _config_bool(cfg.get("cpa_copy_to_hotload"), default=False)
        and cpa_dir
        and not skip_inject
    ):
        try:
            cpa_dir.mkdir(parents=True, exist_ok=True)
            src = Path(result["path"])
            dst = cpa_dir / src.name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            result["cpa_path"] = str(dst)
            log(f"[cpa] hotload copy -> {dst}")
        except Exception as e:  # noqa: BLE001
            log(f"[cpa] hotload copy failed: {e}")
            result["cpa_copy_error"] = str(e)

    # Optional: auto inject minted auth into remote CPA live + inventory dirs.
    # One-click product gate: live dir success is required by default.
    # Never inject dead-entitlement / unconfirmed chat tokens into live pool.
    if not skip_inject:
        apply_multi_remote_inject(result, cfg, log_callback=log)
    else:
        result["remote_live_ok"] = False
        result["remote_inject"] = {
            "ok": False,
            "skipped": True,
            "reason": result.get("remote_inject_skip_reason") or "chat_gate",
        }

    # Project-local backup of accounts + cpa auth (gitignored backups/)
    if result.get("ok") and result.get("path"):
        try:
            import account_backup as _ab

            bres = _ab.backup_after_success(
                email,
                root=_REG_DIR,
                cpa_path=result.get("path"),
                log_callback=log,
            )
            result["local_backup"] = bres
        except Exception as e:  # noqa: BLE001
            log(f"[cpa] local backup failed: {e}")
            result["local_backup_error"] = str(e)

    # failure log under register dir
    if not result.get("ok"):
        fail_path = out_dir / "cpa_auth_failed.txt"
        with open(fail_path, "a", encoding="utf-8") as f:
            f.write(f"{email}----{result.get('error') or 'unknown'}----{int(time.time())}\n")
        if _config_bool(cfg.get("cpa_mint_required"), default=False):
            raise RuntimeError(f"CPA mint required but failed: {result.get('error')}")

    return result
