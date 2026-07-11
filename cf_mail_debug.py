#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests


def extract_code(text: str, subject: str = "") -> Optional[str]:
    if subject:
        m = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", subject, re.IGNORECASE)
        if m:
            return m.group(1)
    m = re.search(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    for p in [
        r"verification\s+code[:\s]+(\d{4,8})",
        r"your\s+code[:\s]+(\d{4,8})",
        r"confirm(?:ation)?\s+code[:\s]+(\d{4,8})",
    ]:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def json_or_text(resp: requests.Response) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        data = resp.json()
        return data, ""
    except Exception:
        return None, (resp.text or "")[:400]


def create_address(api_base: str) -> Tuple[str, str]:
    resp = requests.post(
        f"{api_base.rstrip('/')}/api/new_address",
        json={},
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    resp.raise_for_status()
    data, raw = json_or_text(resp)
    if not data:
        raise RuntimeError(f"/api/new_address 非JSON: {raw}")
    address = str(data.get("address", "")).strip()
    jwt = str(data.get("jwt", "")).strip()
    if not address or not jwt:
        raise RuntimeError(f"/api/new_address 缺少 address/jwt: {data}")
    return address, jwt


def fetch_box(api_base: str, jwt: str, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    resp = requests.get(
        f"{api_base.rstrip('/')}{path}",
        params=params,
        headers={"Authorization": f"Bearer {jwt}"},
        timeout=20,
    )
    if resp.status_code >= 400:
        return []
    data, _ = json_or_text(resp)
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("results"), list):
        return data["results"]
    if isinstance(data.get("data"), list):
        return data["data"]
    if isinstance(data.get("messages"), list):
        return data["messages"]
    return []


def probe_all_boxes(api_base: str, jwt: str) -> List[Tuple[str, List[Dict[str, Any]]]]:
    probes = [
        ("/api/mails", {"limit": 20, "offset": 0}),
        ("/api/sendbox", {"limit": 20, "offset": 0}),
        ("/api/mails", {"limit": 20, "offset": 0, "box": "trash"}),
        ("/api/mails", {"limit": 20, "offset": 0, "folder": "trash"}),
        ("/api/mails", {"limit": 20, "offset": 0, "deleted": "1"}),
        ("/api/mails", {"limit": 20, "offset": 0, "status": "deleted"}),
    ]
    out: List[Tuple[str, List[Dict[str, Any]]]] = []
    for path, params in probes:
        mails = fetch_box(api_base, jwt, path, params)
        out.append((f"{path}?{params}", mails))
    return out


def get_detail(api_base: str, jwt: str, mail_id: Any) -> Dict[str, Any]:
    for url in [
        f"{api_base.rstrip('/')}/api/mail/{mail_id}",
        f"{api_base.rstrip('/')}/api/mails/{mail_id}",
    ]:
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {jwt}"}, timeout=20)
            if resp.status_code >= 400:
                continue
            data, _ = json_or_text(resp)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def flatten_mail_text(item: Dict[str, Any], detail: Dict[str, Any]) -> Tuple[str, str]:
    subject = str(item.get("subject") or detail.get("subject") or "")
    parts: List[str] = []
    for src in (item, detail):
        for k in ("text", "raw", "content", "intro", "body", "snippet"):
            v = src.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v)
        html_val = src.get("html")
        if isinstance(html_val, str):
            html_val = [html_val]
        if isinstance(html_val, list):
            for h in html_val:
                if isinstance(h, str):
                    parts.append(re.sub(r"<[^>]+>", " ", h))
    return subject, "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", required=True)
    ap.add_argument("--address", default="")
    ap.add_argument("--credential", default="")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--interval", type=int, default=3)
    args = ap.parse_args()

    address = args.address.strip()
    credential = args.credential.strip()
    if not credential:
        address, credential = create_address(args.api_base)
        print(f"[NEW] address={address}")
        print(f"[NEW] credential(jwt)={credential}")
    else:
        print(f"[USE] address={address or '(unknown, from credential)'}")

    deadline = time.time() + max(args.timeout, 1)
    seen_ids = set()
    while time.time() < deadline:
        boxes = probe_all_boxes(args.api_base, credential)
        total = 0
        for name, mails in boxes:
            if mails:
                print(f"[BOX] {name} -> {len(mails)}")
            total += len(mails)
            for m in mails:
                mail_id = m.get("id") or m.get("mail_id")
                if not mail_id or mail_id in seen_ids:
                    continue
                seen_ids.add(mail_id)
                detail = get_detail(args.api_base, credential, mail_id)
                subj, text = flatten_mail_text(m, detail)
                code = extract_code(text, subj)
                print(f"[MAIL] id={mail_id} subject={subj!r} code={code!r}")
                if code:
                    print(f"[FOUND] {code}")
                    return
        if total == 0:
            print("[INFO] no mails yet")
        time.sleep(max(args.interval, 1))
    print("[TIMEOUT] no code found")


if __name__ == "__main__":
    main()
