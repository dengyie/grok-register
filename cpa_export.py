"""Register-machine hook: mint CPA xai auth after successful registration.

OIDC package lives at ./cpa_xai (bundled with this project).
Optional override: config `api_reverse_tools` / env `API_REVERSE_TOOLS`
points at a directory that *contains* the `cpa_xai` package.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

_REG_DIR = Path(__file__).resolve().parent
_DEFAULT_OUT = _REG_DIR / "cpa_auths"
_DEFAULT_CPA = Path("")  # empty = do not assume a machine-local CPA path


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
    """SCP a minted xai-*.json into remote CPA auth-dir (default: tebi /personal/cpa/auths).

    Uses ssh/scp (optionally via sshpass). Default host alias is `tebi-tunnel`
    from ~/.ssh/config so Bohrium gateway rate-limit is avoided.
    """
    import shutil as _shutil
    import subprocess

    cfg = config or {}
    log = log_callback or (lambda m: print(m, flush=True))
    if not _config_bool(cfg.get("cpa_remote_inject"), default=False):
        return {"ok": False, "skipped": True, "reason": "disabled"}

    src = Path(local_path).expanduser()
    if not src.is_file():
        msg = f"local auth missing: {src}"
        log(f"[cpa] remote inject failed: {msg}")
        return {"ok": False, "error": msg}

    host = (cfg.get("cpa_remote_ssh_host") or "tebi-tunnel").strip()
    remote_dir = (cfg.get("cpa_remote_auth_dir") or "/personal/cpa/auths").strip().rstrip("/")
    user = (cfg.get("cpa_remote_ssh_user") or "").strip()
    target_host = f"{user}@{host}" if user and "@" not in host else host
    remote_path = f"{target_host}:{remote_dir}/{src.name}"
    timeout = float(cfg.get("cpa_remote_inject_timeout_sec", 60) or 60)

    password = _resolve_remote_ssh_password(cfg)
    sshpass = _shutil.which("sshpass")
    if password and not sshpass:
        msg = "sshpass not found (brew install sshpass / apt install sshpass)"
        log(f"[cpa] remote inject failed: {msg}")
        return {"ok": False, "error": msg}

    env = os.environ.copy()
    # Never inherit broken Bohrium proxy for local tunnel ssh
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    if password:
        env["SSHPASS"] = password

    ssh_base = [
        "ssh",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
    ]
    scp_base = [
        "scp",
        "-o",
        "BatchMode=no",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=20",
        "-o",
        "PreferredAuthentications=password",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-p",
    ]

    def _wrap(cmd: list[str]) -> list[str]:
        if password and sshpass:
            return [sshpass, "-e", *cmd]
        return cmd

    def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
        log(f"[cpa] remote inject {what}: {' '.join(cmd[:6])}...")
        return subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    try:
        mkdir_cmd = _wrap([*ssh_base, target_host, f"mkdir -p {remote_dir} && chmod 700 {remote_dir}"])
        r1 = _run(mkdir_cmd, "mkdir")
        if r1.returncode != 0:
            err = (r1.stderr or r1.stdout or "").strip()[:300]
            raise RuntimeError(f"mkdir failed rc={r1.returncode}: {err}")

        scp_cmd = _wrap([*scp_base, str(src), remote_path])
        r2 = _run(scp_cmd, "scp")
        if r2.returncode != 0:
            err = (r2.stderr or r2.stdout or "").strip()[:300]
            raise RuntimeError(f"scp failed rc={r2.returncode}: {err}")

        # harden remote perms (scp -p keeps local mode; still enforce 600)
        chmod_cmd = _wrap(
            [
                *ssh_base,
                target_host,
                f"chmod 600 {remote_dir}/{src.name} && test -s {remote_dir}/{src.name} && ls -la {remote_dir}/{src.name}",
            ]
        )
        r3 = _run(chmod_cmd, "chmod")
        if r3.returncode != 0:
            err = (r3.stderr or r3.stdout or "").strip()[:300]
            raise RuntimeError(f"chmod/verify failed rc={r3.returncode}: {err}")

        detail = (r3.stdout or "").strip().splitlines()[-1] if (r3.stdout or "").strip() else src.name
        log(f"[cpa] remote inject ok -> {remote_path} ({detail})")
        return {
            "ok": True,
            "remote_path": remote_path,
            "remote_dir": remote_dir,
            "host": host,
            "name": src.name,
        }
    except subprocess.TimeoutExpired:
        msg = f"timeout after {timeout}s"
        log(f"[cpa] remote inject failed: {msg}")
        return {"ok": False, "error": msg}
    except Exception as e:  # noqa: BLE001
        log(f"[cpa] remote inject failed: {e}")
        return {"ok": False, "error": str(e)}


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

    if not cfg.get("cpa_export_enabled", True):
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
    headless = bool(cfg.get("cpa_headless", False))
    probe = bool(cfg.get("cpa_probe_after_write", True))
    probe_chat = bool(cfg.get("cpa_probe_chat", False))
    timeout = float(cfg.get("cpa_mint_timeout_sec", 240))
    base_url = cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1"
    force_standalone = bool(cfg.get("cpa_force_standalone", True))
    cookie_inject = bool(cfg.get("cpa_mint_cookie_inject", True))
    reuse_browser = bool(cfg.get("cpa_mint_browser_reuse", True))
    recycle_every = int(cfg.get("cpa_mint_browser_recycle_every", 15) or 0)
    # Protocol (pure HTTP SSO device flow) first; browser only on failure.
    prefer_protocol = bool(cfg.get("cpa_prefer_protocol", True))
    protocol_only = bool(cfg.get("cpa_protocol_only", False))
    protocol_poll_timeout = float(cfg.get("cpa_protocol_poll_timeout_sec", 90) or 90)

    # cookies: explicit arg > page export > none
    use_cookies = cookies
    if use_cookies is None and cookie_inject and page is not None:
        use_cookies = export_cookies_from_page(page)
    if not cookie_inject:
        use_cookies = None
    else:
        # Always attach SSO cookie clones — register cookies alone often miss accounts.x.ai host
        sso_val = (sso or "").strip()
        if not sso_val and isinstance(use_cookies, list):
            for c in use_cookies:
                if isinstance(c, dict) and c.get("name") in ("sso", "sso-rw") and c.get("value"):
                    sso_val = str(c.get("value"))
                    break
        if sso_val:
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

    sso_val = (sso or "").strip()
    if not sso_val and isinstance(use_cookies, list):
        for c in use_cookies:
            if isinstance(c, dict) and c.get("name") in ("sso", "sso-rw") and c.get("value"):
                sso_val = str(c.get("value"))
                break

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
        log=_log,
    )
    if result.get("mint_method"):
        log(f"[cpa] mint_method={result.get('mint_method')}")

    # By default, a failed post-write probe is only a warning: the CPA auth file
    # has already been minted and written. Set cpa_probe_required=true to make
    # missing /models grok-4.5 fail the export.
    if (
        not result.get("ok")
        and result.get("path")
        and str(result.get("error") or "").startswith("token ok but grok-4.5 not listed")
        and not cfg.get("cpa_probe_required", False)
    ):
        result["ok"] = True
        result["probe_warning"] = result.pop("error", "probe failed")
        log(f"[cpa] probe warning ignored (file already written): {result.get('probe_warning')}")

    if result.get("ok") and result.get("path") and cfg.get("cpa_copy_to_hotload", False) and cpa_dir:
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

    # failure log under register dir
    if not result.get("ok"):
        fail_path = out_dir / "cpa_auth_failed.txt"
        with open(fail_path, "a", encoding="utf-8") as f:
            f.write(f"{email}----{result.get('error') or 'unknown'}----{int(time.time())}\n")
        if cfg.get("cpa_mint_required", False):
            raise RuntimeError(f"CPA mint required but failed: {result.get('error')}")

    return result
