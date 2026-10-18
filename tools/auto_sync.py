"""Automatic loop: fetch real OpenCode Go quota -> update data/quota.json -> POST
to the dashboard, which writes the file and triggers a BLE push to the Wio
Terminal.

The plan row comes from the official OpenCode Go usage endpoint
(GET https://opencode.ai/inference/go/v1/usage, keyed by the OPENCODE_GO_API_KEY
environment variable). token_usage.py still supplies today's token totals from
local session logs. Run the dashboard server with BLE first:

    py server.py --ble --ble-interval 15

Then, in another terminal:

    py tools/auto_sync.py --interval 60        # refresh + push every 60s
    py tools/auto_sync.py --once               # do it a single time

The POST to /api/quota makes the server rewrite quota.json and immediately
trigger a BLE sync, so device updates happen as soon as new data is fetched.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import codex_usage as cu
import claude_usage as clu
import token_usage as tu

# USD per token. Codex CLI uses gpt-5.5 (standard short-context tier):
#   input $5.00 / cached input $0.50 / output $30.00 per 1M tokens.
PRICING = {
    "codex": {"input": 5.00 / 1e6, "cached": 0.50 / 1e6, "output": 30.00 / 1e6},
    # Claude pricing placeholder (Sonnet-class); only used once Claude logs exist.
    "claude": {"input": 3.00 / 1e6, "cached": 0.30 / 1e6, "output": 15.00 / 1e6},
}


def estimate_cost(bucket: dict, price: dict) -> float:
    cached = int(bucket.get("cache") or 0)
    non_cached_input = max(0, int(bucket.get("input") or 0) - cached)
    output = int(bucket.get("output") or 0)
    return (
        non_cached_input * price["input"]
        + cached * price["cached"]
        + output * price["output"]
    )


def human_tokens(n: int) -> str:
    n = int(n or 0)
    for unit, size in (("T", 10**12), ("B", 10**9), ("M", 10**6), ("K", 10**3)):
        if n >= size:
            return f"{n / size:.2f}{unit}Token"
    return f"{n}Token"


def short_tokens(n: int) -> str:
    # Compact form for the device's headline number: "13.3M", "1.8B", "0".
    n = int(n or 0)
    for unit, size in (("T", 10**12), ("B", 10**9), ("M", 10**6), ("K", 10**3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(n)


# Official OpenCode Go usage endpoint (see console/api/inference.mdx and the
# inference OpenAPI spec). The key comes from OPENCODE_GO_API_KEY, the key the
# user copies out of the OpenCode Console after subscribing to Go / Go Plus.
GO_USAGE_URL = "https://opencode.ai/inference/go/v1/usage"
GO_USAGE_TIMEOUT = 20


def fetch_plan() -> dict | None:
    """Best-effort OpenCode Go plan usage snapshot via the official usage API.

    Returns {"rolling": {"pct", "resetAt"}, "weekly": ..., "monthly": ...}
    or None if OPENCODE_GO_API_KEY is missing / expired / the query fails. The
    caller keeps the previous snapshot on failure, so the device never shows
    blanks while the key is out of date.

    Response shape (inference OpenAPI spec, GET /go/v1/usage):
        {"usage": {"rolling": {"status", "percent", "resetsAt"}, "weekly": ...,
         "monthly": ...}}
    The three windows map 1:1 onto the display: rolling (5-hour) / weekly /
    monthly used-percent, same as the old Volcano Coding Plan buckets.
    """
    api_key = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    if not api_key:
        print("[plan] OPENCODE_GO_API_KEY not set; keeping previous plan", file=sys.stderr)
        return None

    request = urllib.request.Request(
        GO_USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "wio-ai-quota-dashboard/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=GO_USAGE_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace").strip()[:160]
        print(f"[plan] Go usage HTTP {error.code}: {detail}", file=sys.stderr)
        return None
    except Exception as error:
        print(f"[plan] Go usage fetch failed: {error}", file=sys.stderr)
        return None

    usage = data.get("usage") or {}
    periods = {}
    for label in ("rolling", "weekly", "monthly"):
        window = usage.get(label) or {}
        if "percent" not in window:
            continue
        periods[label] = {
            "pct": round(float(window["percent"])),
            "resetAt": window.get("resetsAt", ""),
        }
    if periods:
        return periods
    print(f"[plan] Go usage response had no percent fields: {str(data)[:160]}", file=sys.stderr)
    return None


def build_quota(args) -> dict:
    # Actual usage always comes from the local session logs (Claude Code and
    # Codex run against OpenCode Go, so the official Anthropic / OpenAI quota
    # endpoints don't apply).
    tz = timezone(timedelta(hours=args.tz))
    now = datetime.now(tz)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_week = now - timedelta(days=7)
    cx_today, cx_week, _ = tu.collect_codex(start_today, start_week)
    cc_today, cc_week, _ = tu.collect_claude(start_today, start_week)

    today_total = cx_today["total"] + cc_today["total"]
    week_total = cx_week["total"] + cc_week["total"]
    today_cost = estimate_cost(cx_today, PRICING["codex"]) + estimate_cost(cc_today, PRICING["claude"])

    try:
        quota = json.loads(cu.DATA_FILE.read_text("utf-8"))
    except Exception:
        quota = {}
    quota.pop("platforms", None)  # per-platform Anthropic/OpenAI quota is unused
    # Migrate a stale Volcano-era plan snapshot (key "session") to the OpenCode
    # Go schema ("rolling") so the 5h window bar keeps rendering while the new
    # fetch is unavailable.
    old_plan = quota.get("plan") or {}
    if "session" in old_plan and "rolling" not in old_plan:
        old_plan["rolling"] = old_plan.pop("session")

    hhmm = now.strftime("%H:%M")
    quota["updatedAt"] = hhmm
    footer = quota.setdefault("footer", {})
    footer["time"] = hhmm
    footer["tokens"] = human_tokens(today_total)
    footer["cost"] = f"${today_cost:.2f}"

    # OpenCode Go quota (best-effort): keep the previous snapshot when the API
    # key is missing, expired, or the endpoint is unreachable.
    plan = fetch_plan()
    if plan:
        quota["plan"] = plan

    quota["today"] = {
        "tokens": human_tokens(today_total),
        "tokensShort": short_tokens(today_total),
        "cost": f"${today_cost:.2f}",
        "weekTokens": human_tokens(week_total),
        "weekShort": short_tokens(week_total),
    }
    return quota


def post(quota: dict, url: str) -> None:
    data = json.dumps(quota, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def cycle(args) -> None:
    quota = build_quota(args)
    try:
        post(quota, args.dashboard_url)
        pushed = "posted+BLE"
    except Exception as error:
        cu.DATA_FILE.write_text(json.dumps(quota, ensure_ascii=False, indent=2) + "\n", "utf-8")
        pushed = f"file-only (server down? {error})"
    plan = quota.get("plan") or {}
    today = quota.get("today") or {}
    stamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"[{stamp}] go week={plan.get('weekly', {}).get('pct', '--')}% "
        f"rolling={plan.get('rolling', {}).get('pct', '--')}%  "
        f"today={today.get('cost', '--')} tokens={today.get('tokensShort', '--')}  -> {pushed}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto loop: fetch quota -> quota.json -> BLE push")
    parser.add_argument("--interval", type=int, default=60, help="seconds between refreshes")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--tz", type=float, default=8.0, help="UTC offset for day boundary / clock")
    parser.add_argument("--no-refresh", action="store_true", help="never refresh the OAuth token")
    parser.add_argument("--no-claude", action="store_true", help="skip the Claude usage probe")
    parser.add_argument("--no-codex", action="store_true", help="skip the Codex usage probe")
    parser.add_argument("--dashboard-url", default=cu.DASHBOARD_URL)
    args = parser.parse_args()

    if args.once:
        try:
            cycle(args)
            return 0
        except Exception as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    print(f"auto-sync every {args.interval}s -> {args.dashboard_url} (Ctrl+C to stop)", flush=True)
    while True:
        try:
            cycle(args)
        except Exception as error:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] error: {error}", file=sys.stderr, flush=True)
        time.sleep(max(15, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
