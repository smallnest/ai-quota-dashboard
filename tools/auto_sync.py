"""Automatic loop: fetch Magpie gateway stats -> update data/quota.json -> POST
to the dashboard, which writes the file and triggers a BLE push to the Wio
Terminal.

All numbers come from the local Magpie gateway (default
http://127.0.0.1:3425, override with MAGPIE_URL). The device shows four pages
(Overview / Requests / Sessions / Context); the five-way key switches pages.
Run the dashboard server with BLE first:

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

# Local Magpie gateway (reference: peterroe/magpie-usage-display). It exposes
# read-only stats for every provider it proxies, so the dashboard can show
# gateway-wide usage instead of one provider's plan snapshot.
MAGPIE_URL = os.environ.get("MAGPIE_URL", "http://127.0.0.1:3425")
MAGPIE_TIMEOUT = 8


def _get_json(url: str, timeout: int = MAGPIE_TIMEOUT) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unexpected response from {url}")
    return data


def fetch_magpie() -> dict | None:
    """Collect quotas / usage / latest request from the local Magpie gateway.

    Returns the three raw payloads or None when Magpie is not running or any
    endpoint fails; the caller then keeps the previous snapshot, so the device
    keeps its last numbers while the gateway restarts.
    """
    try:
        root = _get_json(MAGPIE_URL + "/")
        if "magpie" not in json.dumps(root.get("name", "")) and "/v1/magpie/quotas" not in (root.get("apis") or []):
            print(f"[magpie] {MAGPIE_URL} is not a Magpie gateway", file=sys.stderr)
            return None
        quotas = _get_json(MAGPIE_URL + "/v1/magpie/quotas")
        usage = _get_json(MAGPIE_URL + "/v1/magpie/usage")
        requests = _get_json(MAGPIE_URL + "/v1/magpie/usage/requests?limit=1")
        return {"quotas": quotas, "usage": usage, "requests": requests}
    except Exception as error:
        print(f"[magpie] fetch failed: {error}", file=sys.stderr)
        return None


def _bucket_tokens(bucket: dict) -> int:
    return sum(
        int(bucket.get(key) or 0)
        for key in ("input", "output", "reasoning", "cache_read", "cache_write")
    )


def _most_constrained_window(quotas: dict) -> dict:
    # Magpie aggregates several providers; the reference project highlights the
    # window closest to exhaustion, which is what the user must act on first.
    windows = [
        window
        for provider in quotas.get("data") or []
        for window in provider.get("windows") or []
        if not window.get("unlimited") and isinstance(window.get("remaining"), (int, float))
    ]
    return min(windows, key=lambda w: w["remaining"], default=None)


def build_magpie_block(raw: dict, hhmm: str) -> dict:
    """Turn raw Magpie payloads into the four device pages."""
    quotas = raw.get("quotas") or {}
    usage = raw.get("usage") or {}
    requests = raw.get("requests") or {}

    series = usage.get("series") or []
    today = series[-1] if series else {}
    calls_today = int(today.get("calls") or 0)
    errors_today = min(calls_today, int(today.get("errors") or 0))
    success_pct = round((calls_today - errors_today) * 100 / calls_today) if calls_today else 100
    tokens_today = _bucket_tokens(today)
    tokens_7d = sum(_bucket_tokens(b) for b in series[-7:])
    timed_today = int(today.get("timed") or 0)
    avg_ttft_ms = int(today.get("ttft_ms") or 0) // timed_today if timed_today else 0

    latest = (requests.get("rows") or [{}])[0]
    latest_status = int(latest.get("status") or 0)
    latency_ms = int(latest.get("ms") or 0)
    latest_model = str(latest.get("served") or latest.get("model") or "--")

    total_calls = int(usage.get("calls") or 0)
    total_tokens = _bucket_tokens(usage)
    cost_30d = float(usage.get("cost") or 0)
    sessions = int(usage.get("sessionCount") or 0)
    models = usage.get("models") or []
    top_model = max(models, key=lambda m: int(m.get("calls") or 0), default={})

    cache_read = int(usage.get("cache_read") or 0)
    input_total = int(usage.get("input") or 0)
    cache_base = cache_read + input_total
    cache_hit_pct = round(cache_read * 100 / cache_base) if cache_base else 0

    min_window = _most_constrained_window(quotas)
    if min_window:
        min_left = f"{round(float(min_window['remaining']))}%"
        min_name = str(min_window.get("name") or "--")
    else:
        min_left, min_name = "--", "--"

    pages = [
        {
            "t": "Overview",
            "v": f"{success_pct}%",
            "l": "today success",
            "b": -1,
            "r": [
                ["calls", str(calls_today)],
                ["errors", str(errors_today)],
                ["min left", min_left],
                ["window", min_name[:14]],
            ],
        },
        {
            "t": "Requests",
            "v": short_tokens(tokens_today),
            "l": "today tokens",
            "b": -1,
            "r": [
                ["latest", latest_model[:14]],
                ["status", str(latest_status) if latest_status else "--"],
                ["latency", f"{latency_ms / 1000:.1f}s" if latency_ms else "--"],
                ["avg ttft", f"{avg_ttft_ms}ms" if avg_ttft_ms else "--"],
            ],
        },
        {
            "t": "Sessions",
            "v": f"${cost_30d:.2f}",
            "l": "30d cost",
            "b": -1,
            "r": [
                ["sessions", str(sessions)],
                ["calls", str(total_calls)],
                ["tokens 30d", short_tokens(total_tokens)],
                ["top model", str(top_model.get("name") or "--")[:14]],
            ],
        },
        {
            "t": "Context",
            "v": f"{cache_hit_pct}%",
            "l": "cache hit",
            "b": cache_hit_pct,
            "r": [
                ["input", short_tokens(input_total)],
                ["output", short_tokens(int(usage.get("output") or 0))],
                ["cache read", short_tokens(cache_read)],
                ["reasoning", short_tokens(int(usage.get("reasoning") or 0))],
            ],
        },
    ]
    return {
        "footer": {"brand": "Magpie", "tokens": f"{short_tokens(total_tokens)} 30d", "time": hhmm},
        "pages": pages,
    }


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
    # All numbers come from the local Magpie gateway, which already aggregates
    # every provider the user's agents go through; no provider-specific API
    # keys are needed here.
    tz = timezone(timedelta(hours=args.tz))
    now = datetime.now(tz)
    hhmm = now.strftime("%H:%M")

    try:
        quota = json.loads(cu.DATA_FILE.read_text("utf-8"))
    except Exception:
        quota = {}
    quota.pop("platforms", None)  # per-platform Anthropic/OpenAI quota is unused
    quota.pop("plan", None)
    quota.pop("today", None)

    raw = fetch_magpie()
    if raw:
        quota["magpie"] = build_magpie_block(raw, hhmm)
    quota["updatedAt"] = hhmm
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
    magpie = quota.get("magpie") or {}
    pages = magpie.get("pages") or []
    stamp = datetime.now().strftime("%H:%M:%S")
    if pages:
        overview = pages[0].get("r") or []
        print(
            f"[{stamp}] magpie today={pages[0].get('v', '--')} "
            f"calls={overview[0][1] if len(overview) > 0 else '--'} "
            f"errors={overview[1][1] if len(overview) > 1 else '--'}  -> {pushed}",
            flush=True,
        )
    else:
        print(f"[{stamp}] magpie unavailable, kept last snapshot -> {pushed}", flush=True)


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
