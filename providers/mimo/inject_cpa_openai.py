#!/usr/bin/env python3
"""Inject MiMo API keys into CPA openai-compatibility (OpenAI provider path).

MiMo keys are NOT xai auth-dir JSON. They go under config.yaml:

  openai-compatibility:
    - name: xiaomimimo
      base-url: https://api.xiaomimimo.com/v1
      api-key-entries:
        - api-key: sk-...
      models:
        - name: mimo-v2.5-tts
          ...

Idempotent: existing keys are left alone. Backs up config before write.
Does not SIGHUP CPA (CLIProxyAPI watches config via fsnotify).

Usage:
  python inject_cpa_openai.py --key sk-...
  python inject_cpa_openai.py --from-file /personal/mimo-register/output/success_keys.txt
  python inject_cpa_openai.py --from-jsonl /personal/mimo-register/output/accounts.jsonl
  # remote via ssh host (BatchMode):
  python inject_cpa_openai.py --ssh tebi-tunnel --from-jsonl ...
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_CHANNEL = "xiaomimimo"
DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MODELS = (
    "mimo-v2.5-tts",
    "mimo-v2.5-tts-voiceclone",
    "mimo-v2.5-tts-voicedesign",
)
DEFAULT_CONFIG = "/personal/cpa/config.yaml"
# Allow hyphenated vendor keys (sk-hyper-..., sk-existing-...) while keeping sk- prefix.
_KEY_RE = re.compile(r"(sk-[A-Za-z0-9][A-Za-z0-9_-]*)")


def _redact(key: str) -> str:
    k = key.strip()
    if len(k) <= 14:
        return "***"
    return f"{k[:10]}...{k[-4:]}"


def extract_keys_from_text(text: str) -> list[str]:
    found: list[str] = []
    for m in _KEY_RE.finditer(text or ""):
        k = m.group(1)
        if k not in found:
            found.append(k)
    return found


def extract_keys_from_jsonl(path: Path) -> list[str]:
    found: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            found.extend(extract_keys_from_text(line))
            continue
        for field in ("apiKey", "api_key", "key", "secret"):
            v = obj.get(field)
            if isinstance(v, str) and v.startswith("sk-") and v not in found:
                found.append(v.strip())
    return found


def _channel_block_span(text: str, channel: str) -> tuple[int, int] | None:
    """Return [start, end) of the openai-compatibility list item for channel."""
    m = re.search(r"(?m)^openai-compatibility:\s*$", text)
    if not m:
        return None
    start_block = m.end()
    next_top = re.search(r"(?m)^[A-Za-z0-9_-]+:\s*(?:#.*)?$", text[start_block:])
    end_block = start_block + (next_top.start() if next_top else len(text) - start_block)
    block = text[start_block:end_block]

    # list items at indent 2: "  - name:"
    item_starts = [m.start() for m in re.finditer(r"(?m)^  - name:\s*", block)]
    if not item_starts:
        return None
    for i, rel in enumerate(item_starts):
        abs_start = start_block + rel
        abs_end = start_block + (item_starts[i + 1] if i + 1 < len(item_starts) else len(block))
        head = text[abs_start : abs_start + 120]
        hm = re.match(r"  - name:\s*[\"']?([^\"'\n#]+?)[\"']?\s*(?:#.*)?$", head.splitlines()[0])
        if not hm:
            continue
        name = hm.group(1).strip()
        if name == channel:
            return abs_start, abs_end
    return None


def list_keys_in_entry(entry: str) -> list[str]:
    return extract_keys_from_text(entry)


def ensure_channel_entry(
    text: str,
    *,
    channel: str,
    base_url: str,
    models: tuple[str, ...] | list[str],
    priority: int = 100,
) -> tuple[str, bool]:
    """Ensure xiaomimimo-style entry exists. Returns (new_text, created)."""
    span = _channel_block_span(text, channel)
    if span is not None:
        return text, False

    models_yaml = "\n".join(
        f'      - name: {m}\n        alias: ""' for m in models
    )
    entry = (
        f"  - name: {channel}\n"
        f"    priority: {priority}\n"
        f"    base-url: {base_url}\n"
        f"    api-key-entries:\n"
        f"      # injected by inject_cpa_openai.py\n"
        f"{models_yaml}\n"
    )
    # Prefer insert just after openai-compatibility:
    m = re.search(r"(?m)^openai-compatibility:\s*\n", text)
    if not m:
        raise SystemExit("config missing openai-compatibility: block")
    insert_at = m.end()
    return text[:insert_at] + entry + text[insert_at:], True


def append_keys_to_entry(entry: str, keys: list[str]) -> tuple[str, list[str]]:
    """Append missing api-key lines under api-key-entries. Returns (entry, added)."""
    existing = set(list_keys_in_entry(entry))
    to_add = [k for k in keys if k not in existing]
    if not to_add:
        return entry, []

    lines = entry.splitlines(keepends=True)
    # Find api-key-entries: line
    ake_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^    api-key-entries:\s*(?:#.*)?$", line):
            ake_idx = i
            break
    if ake_idx is None:
        # insert before models: or at end of entry (before trailing blank)
        models_idx = None
        for i, line in enumerate(lines):
            if re.match(r"^    models:\s*(?:#.*)?$", line):
                models_idx = i
                break
        insert_at = models_idx if models_idx is not None else len(lines)
        block = ["    api-key-entries:\n"] + [f"      - api-key: {k}\n" for k in to_add]
        lines[insert_at:insert_at] = block
        return "".join(lines), to_add

    # Find last api-key under this section (indent 6: "      - api-key")
    last = ake_idx
    for j in range(ake_idx + 1, len(lines)):
        line = lines[j]
        if re.match(r"^      - ", line) or re.match(r"^        ", line) or re.match(r"^\s*#", line) or line.strip() == "":
            # still in list / comments / blank inside section
            if re.match(r"^      - api-key:", line) or re.match(r"^      - ", line) or line.strip().startswith("#") or line.strip() == "":
                last = j
                continue
            # deeper nested under entry item
            if re.match(r"^        ", line):
                last = j
                continue
        # next sibling field at indent 4 (models/priority/base-url)
        if re.match(r"^    [a-zA-Z0-9_-]+:", line):
            break
        # next list item at indent 2
        if re.match(r"^  - ", line):
            break
        last = j

    insert_lines = [f"      - api-key: {k}\n" for k in to_add]
    lines[last + 1 : last + 1] = insert_lines
    return "".join(lines), to_add


def inject_local(
    config_path: Path,
    keys: list[str],
    *,
    channel: str = DEFAULT_CHANNEL,
    base_url: str = DEFAULT_BASE_URL,
    models: tuple[str, ...] = DEFAULT_MODELS,
    priority: int = 100,
    dry_run: bool = False,
) -> dict:
    if not keys:
        raise SystemExit("no keys to inject")
    if not config_path.is_file():
        raise SystemExit(f"config not found: {config_path}")

    original = config_path.read_text(encoding="utf-8")
    text, created = ensure_channel_entry(
        original, channel=channel, base_url=base_url, models=models, priority=priority
    )
    span = _channel_block_span(text, channel)
    if span is None:
        raise SystemExit(f"channel {channel!r} still missing after ensure")
    start, end = span
    entry = text[start:end]
    new_entry, added = append_keys_to_entry(entry, keys)
    if not added and not created:
        return {
            "ok": True,
            "changed": False,
            "channel": channel,
            "added": [],
            "existing": [_redact(k) for k in list_keys_in_entry(entry)],
            "config": str(config_path),
        }

    new_text = text[:start] + new_entry + text[end:]
    if dry_run:
        return {
            "ok": True,
            "changed": False,
            "dry_run": True,
            "channel": channel,
            "would_add": [_redact(k) for k in added],
            "created_channel": created,
            "config": str(config_path),
        }

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.bak-mimo-{ts}")
    shutil.copy2(config_path, backup)

    # atomic replace
    fd, tmp_name = tempfile.mkstemp(
        prefix=config_path.name + ".",
        suffix=".tmp",
        dir=str(config_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, config_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    # verify
    verify = config_path.read_text(encoding="utf-8")
    vspan = _channel_block_span(verify, channel)
    if vspan is None:
        raise SystemExit("post-write: channel missing (restoring not auto — check backup)")
    vkeys = set(list_keys_in_entry(verify[vspan[0] : vspan[1]]))
    missing = [k for k in keys if k not in vkeys]
    if missing:
        raise SystemExit(f"post-write: keys not present: {[ _redact(k) for k in missing ]}")

    return {
        "ok": True,
        "changed": True,
        "channel": channel,
        "added": [_redact(k) for k in added],
        "created_channel": created,
        "backup": str(backup),
        "config": str(config_path),
        "key_count": len(vkeys),
    }


def inject_via_ssh(
    ssh_host: str,
    keys: list[str],
    *,
    config: str = DEFAULT_CONFIG,
    channel: str = DEFAULT_CHANNEL,
    base_url: str = DEFAULT_BASE_URL,
    models: tuple[str, ...] = DEFAULT_MODELS,
    priority: int = 100,
    dry_run: bool = False,
) -> dict:
    """Ship this script + keys to remote python (keys via temp file, not argv)."""
    self_path = Path(__file__).resolve()
    remote_script = f"/tmp/inject_cpa_openai_{os.getpid()}.py"
    remote_keys = f"/tmp/mimo_keys_{os.getpid()}.txt"
    keys_blob = "\n".join(keys) + "\n"
    ssh_base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", ssh_host]

    up1 = subprocess.run(
        [
            "scp",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=20",
            str(self_path),
            f"{ssh_host}:{remote_script}",
        ],
        capture_output=True,
        text=True,
    )
    if up1.returncode != 0:
        raise SystemExit(f"scp script failed: {up1.stderr or up1.stdout}")

    up2 = subprocess.run(
        ssh_base + [f"cat > {remote_keys} && chmod 600 {remote_keys}"],
        input=keys_blob,
        capture_output=True,
        text=True,
    )
    if up2.returncode != 0:
        raise SystemExit(f"upload keys failed: {up2.stderr or up2.stdout}")

    parts = [
        "python3",
        remote_script,
        "--config",
        config,
        "--channel",
        channel,
        "--base-url",
        base_url,
        "--priority",
        str(priority),
        "--from-file",
        remote_keys,
    ]
    for m in models:
        parts.extend(["--model", m])
    if dry_run:
        parts.append("--dry-run")
    remote_cmd = " ".join(parts) + f"; ec=$?; rm -f {remote_keys} {remote_script}; exit $ec"
    proc = subprocess.run(
        ssh_base + [remote_cmd],
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise SystemExit(f"remote inject failed exit={proc.returncode}: {out[-2000:]}")
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"ok": True, "raw": out[-1500:]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Inject MiMo sk- keys into CPA openai-compatibility")
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--channel", default=DEFAULT_CHANNEL)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--priority", type=int, default=100)
    p.add_argument("--model", action="append", dest="models")
    p.add_argument("--key", action="append", dest="keys")
    p.add_argument("--from-file", type=Path)
    p.add_argument("--from-jsonl", type=Path)
    p.add_argument("--ssh", help="ssh host alias (e.g. tebi-tunnel); runs inject on remote")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    keys: list[str] = []
    for k in args.keys or []:
        keys.extend(extract_keys_from_text(k))
    if args.from_file:
        keys.extend(extract_keys_from_text(args.from_file.read_text(encoding="utf-8", errors="replace")))
    if args.from_jsonl:
        keys.extend(extract_keys_from_jsonl(args.from_jsonl))
    # de-dupe preserve order
    uniq: list[str] = []
    for k in keys:
        if k not in uniq:
            uniq.append(k)
    keys = uniq
    if not keys:
        print("no sk- keys found", file=sys.stderr)
        return 2

    models = tuple(args.models) if args.models else DEFAULT_MODELS
    if args.ssh:
        result = inject_via_ssh(
            args.ssh,
            keys,
            config=args.config,
            channel=args.channel,
            base_url=args.base_url,
            models=models,
            priority=args.priority,
            dry_run=args.dry_run,
        )
    else:
        result = inject_local(
            Path(args.config),
            keys,
            channel=args.channel,
            base_url=args.base_url,
            models=models,
            priority=args.priority,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
