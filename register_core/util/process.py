"""Subprocess helpers: process-group kill on timeout (avoid orphan browsers)."""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(slots=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run_command(
    cmd: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 900,
) -> CmdResult:
    """Run command; on timeout kill the whole process group (Unix)."""
    use_session = os.name != "nt"
    popen_kwargs: dict = {
        "args": list(cmd),
        "cwd": cwd,
        "env": dict(env) if env is not None else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if use_session:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(**popen_kwargs)
    timed_out = False
    stdout = ""
    stderr = ""
    try:
        stdout, stderr = proc.communicate(timeout=max(1.0, float(timeout_s)))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except Exception:
            stdout, stderr = stdout or "", stderr or ""
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                more_out, more_err = proc.communicate(timeout=5)
                stdout = (stdout or "") + (more_out or "")
                stderr = (stderr or "") + (more_err or "")
            except Exception:
                pass

    return CmdResult(
        returncode=int(proc.returncode if proc.returncode is not None else -1),
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=timed_out,
    )


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
            return
        except Exception:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass
        return
    try:
        proc.kill()
    except Exception:
        pass


_OTP_LINE = re.compile(r"(验证码|otp|one[- ]time|\bcode)\s*[:：]\s*\d{4,8}", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SK = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")


def redact_log_tail(text: str, *, limit: int = 1200) -> str:
    """Strip OTP / JWT / API keys before storing artifacts."""
    if not text:
        return ""
    lines: list[str] = []
    for line in text.splitlines():
        s = line
        if _OTP_LINE.search(s):
            s = re.sub(r"\d{4,8}", "******", s)
        s = _JWT.sub("eyJ***", s)
        s = _SK.sub("sk-***", s)
        lines.append(s)
    joined = "\n".join(lines)
    return joined[-limit:] if len(joined) > limit else joined
