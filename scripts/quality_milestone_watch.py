#!/usr/bin/env python3
"""Watch accounts_cli growth; every +100 accounts run quality sample (--live).

Designed to run beside register_cli. State file tracks last milestone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
STATE = _ROOT / "logs" / "quality_watch_state.json"
ACCOUNTS = _ROOT / "accounts_cli.txt"
PY = _ROOT / ".venv" / "bin" / "python"
if not PY.is_file():
    PY = Path(sys.executable)


def _count() -> int:
    if not ACCOUNTS.is_file():
        return 0
    n = 0
    with ACCOUNTS.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip() and not line.lstrip().startswith("#"):
                n += 1
    return n


def _load_state() -> dict:
    if STATE.is_file():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_milestone": 0, "runs": []}


def _save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run_quality(milestone: int) -> int:
    out = _ROOT / "logs" / "quality" / f"milestone_{milestone}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(PY),
        "-u",
        str(_ROOT / "scripts" / "quality_sample_accounts.py"),
        "--sample",
        "3",
        "--live",
        "--milestone",
        str(milestone),
        "--out",
        str(out),
    ]
    print(f"[watch] run quality milestone={milestone}", flush=True)
    p = subprocess.run(cmd, cwd=str(_ROOT))
    return int(p.returncode)


def main() -> int:
    step = int(os.environ.get("QUALITY_STEP", "100"))
    target = int(os.environ.get("QUALITY_TARGET", "10000"))
    poll = float(os.environ.get("QUALITY_POLL_SEC", "30"))
    st = _load_state()
    last = int(st.get("last_milestone") or 0)
    # align last to floor of current if unset
    cur0 = _count()
    if last <= 0:
        last = (cur0 // step) * step
        st["last_milestone"] = last
        _save_state(st)
    print(
        f"[watch] start accounts={cur0} last_milestone={last} step={step} target={target}",
        flush=True,
    )
    while True:
        n = _count()
        # next milestone strictly greater than last
        next_m = last + step
        while next_m <= n:
            rc = _run_quality(next_m)
            st.setdefault("runs", []).append(
                {"milestone": next_m, "accounts": n, "rc": rc, "ts": int(time.time())}
            )
            st["last_milestone"] = next_m
            last = next_m
            _save_state(st)
            print(f"[watch] milestone {next_m} done rc={rc} accounts={n}", flush=True)
            next_m = last + step
        if n >= target:
            print(f"[watch] target {target} reached (accounts={n}); exit", flush=True)
            return 0
        # stop if register meta says dead for long? keep watching until target
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
