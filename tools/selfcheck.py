#!/usr/bin/env python3
"""Standalone connectivity self-check for a running SnowLuma WebUI.

This intentionally uses only the standard library so it can be run on a host
where the plugin's dependencies are not installed:

    python tools/selfcheck.py --base http://127.0.0.1:5099 --password '******'

It verifies the same endpoints the plugin depends on and prints a compact
report. It never writes anything: the OneBot config is only read.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

TOKEN_HEADER = "X-SnowLuma-Token"


def request(base: str, method: str, path: str, token: str = "", body=None):
    url = base.rstrip("/") + path
    data = None
    headers = {"User-Agent": "snowluma-go-selfcheck"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers[TOKEN_HEADER] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except urllib.error.URLError as exc:
        return 0, f"connection failed: {exc.reason}"


def main() -> int:
    parser = argparse.ArgumentParser(description="SnowLuma WebUI self-check")
    parser.add_argument("--base", default="http://127.0.0.1:5099")
    parser.add_argument("--password", required=True)
    parser.add_argument("--totp", default="")
    args = parser.parse_args()

    failures = 0
    print(f"base: {args.base}")

    status, payload = request(args.base, "GET", "/api/status")
    print(f"[status]  unauthenticated -> HTTP {status}")

    login_body = {"password": args.password}
    if args.totp:
        login_body["totp"] = args.totp
    status, payload = request(args.base, "POST", "/api/login", body=login_body)
    if status != 200 or not isinstance(payload, dict) or not payload.get("token"):
        print(f"[login]   FAILED -> HTTP {status} {payload}")
        return 1
    token = payload["token"]
    print(f"[login]   ok (mustChangePassword={payload.get('mustChangePassword')})")

    checks = [
        ("status", "GET", "/api/status", None),
        ("agreements", "GET", "/api/agreements", None),
        ("qq-list", "GET", "/api/qq-list", None),
        ("connections", "GET", "/api/connections", None),
        ("processes", "GET", "/api/processes", None),
        ("system", "GET", "/api/system", None),
        ("logs", "GET", "/api/logs?limit=5", None),
        ("update", "GET", "/api/update/check", None),
    ]
    for name, method, path, body in checks:
        status, payload = request(args.base, method, path, token, body)
        ok = status == 200
        failures += 0 if ok else 1
        detail = ""
        if isinstance(payload, dict):
            if name == "qq-list":
                detail = f"{len(payload.get('list') or [])} account(s)"
            elif name == "processes":
                detail = f"{len(payload.get('list') or [])} process(es)"
            elif name == "agreements":
                detail = f"consentRequired={payload.get('consentRequired')} version={payload.get('version')}"
            elif name == "system":
                detail = f"{payload.get('platform')}/{payload.get('arch')}"
            elif name == "update":
                detail = str(payload)[:120]
            elif name == "status":
                detail = str(payload.get("status"))
        print(f"[{name:9s}] HTTP {status} {detail}")

    status, payload = request(args.base, "GET", "/api/qq-list", token)
    accounts = (payload or {}).get("list") if isinstance(payload, dict) else None
    if accounts:
        uin = str(accounts[0].get("uin"))
        status, config = request(args.base, "GET", f"/api/config/{uin}", token)
        ok = status == 200
        failures += 0 if ok else 1
        clients = (((config or {}).get("config") or {}).get("networks") or {}).get("wsClients") or []
        names = [c.get("name") for c in clients if isinstance(c, dict)]
        print(f"[config]  HTTP {status} uin={uin} wsClients={names}")
    else:
        print("[config]  skipped (no online account)")

    print("result:", "OK" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
