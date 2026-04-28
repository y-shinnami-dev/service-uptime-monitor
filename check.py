#!/usr/bin/env python3
"""
Service uptime monitor.

Reads monitors.json, pings each URL in parallel, compares with previous
state in .state.json, and posts to Slack only when state transitions
occur (up→down or down→up).
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
MONITORS_PATH = os.path.join(REPO_DIR, "monitors.json")
STATE_PATH = os.path.join(REPO_DIR, ".state.json")
TIMEOUT = 15
RETRY_BACKOFF = 3
MAX_ATTEMPTS = 2
# Number of CONSECUTIVE failed runs required before flipping a service to "down"
# and posting an alert. Suppresses single-point flaps (e.g., 30s blips on
# Lolipop shared hosting) without slowing genuine outage detection more than
# one cron interval (~5min).
CONSEC_FAIL_THRESHOLD = 2
JST = timezone(timedelta(hours=9))


def _probe(url):
    req = urllib.request.Request(url, headers={"User-Agent": "service-uptime-monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return None, str(e)


def check_one(monitor):
    name = monitor["name"]
    url = monitor["url"]
    expected = set(monitor.get("expect", [200]))
    code, err_msg = None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        code, err_msg = _probe(url)
        is_up = code in expected
        if is_up:
            if attempt > 1:
                print(f"  ↻ {name} recovered on attempt {attempt}", file=sys.stderr)
            return name, url, code, True, None
        if attempt < MAX_ATTEMPTS:
            print(f"  ↻ {name} attempt {attempt} failed (code={code} err={err_msg}), retrying in {RETRY_BACKOFF}s", file=sys.stderr)
            time.sleep(RETRY_BACKOFF)
    if code is None:
        return name, url, None, False, err_msg
    return name, url, code, False, f"unexpected status {code}"


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def post_slack(webhook, text):
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except Exception as e:
        print(f"Slack post failed: {e}", file=sys.stderr)
        return None


def _normalize_prev(raw):
    """Migrate old state format ({name: 'up'|'down'}) to new
    ({name: {'status': ..., 'fail_streak': N}})."""
    out = {}
    for name, val in (raw or {}).items():
        if isinstance(val, dict):
            out[name] = {
                "status": val.get("status", "up"),
                "fail_streak": int(val.get("fail_streak", 0)),
            }
        else:
            out[name] = {"status": val, "fail_streak": 0}
    return out


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    monitors = load_json(MONITORS_PATH, [])
    prev_state = _normalize_prev(load_json(STATE_PATH, {}))
    test_notify = "--test-notify" in sys.argv

    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    print("=== Service Uptime Check ===")
    print(f"実行: {now_jst}")
    print(f"監視対象: {len(monitors)} services")
    print(f"連続失敗しきい値: {CONSEC_FAIL_THRESHOLD} runs")

    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(check_one, m): m for m in monitors}
        for fut in as_completed(futures):
            name, url, code, is_up, err = fut.result()
            results[name] = (url, code, is_up, err)

    for name in sorted(results):
        url, code, is_up, err = results[name]
        mark = "✓" if is_up else "✗"
        code_str = str(code) if code is not None else "ERR"
        print(f"  {mark} {name:25s} {code_str:>5}  {url}")

    new_down = []
    recovered = []
    new_state = {}
    for name, (url, code, is_up, err) in results.items():
        prev = prev_state.get(name, {"status": "up", "fail_streak": 0})
        prev_status = prev["status"]
        prev_streak = prev["fail_streak"]

        if is_up:
            new_status = "up"
            new_streak = 0
            if prev_status == "down":
                recovered.append((name, url, code))
        else:
            new_streak = prev_streak + 1
            if prev_status == "up":
                if new_streak >= CONSEC_FAIL_THRESHOLD:
                    new_status = "down"
                    new_down.append((name, url, code, err))
                else:
                    new_status = "up"  # still tentative — suppress alert
                    print(f"  ⚠ {name} failed (streak {new_streak}/{CONSEC_FAIL_THRESHOLD}) — not yet alerting")
            else:
                new_status = "down"

        new_state[name] = {"status": new_status, "fail_streak": new_streak}

    save_state(new_state)

    print("---")
    print(f"new_down: {len(new_down)} / recovered: {len(recovered)}")

    if not new_down and not recovered and not test_notify:
        print("変化なし。Slack通知はスキップ")
        return 0

    if not webhook:
        print("WARN: SLACK_WEBHOOK_URL not set — skipping notification", file=sys.stderr)
        return 0

    lines = []
    if new_down:
        lines.append("🚨 *サービス監視 — ダウン検知*")
        for name, url, code, err in new_down:
            code_str = str(code) if code is not None else "connection error"
            lines.append(f"  🔴 *{name}* — {code_str}")
            lines.append(f"     {url}")
            if err and code is None:
                lines.append(f"     `{err}`")

    if recovered:
        if lines:
            lines.append("")
        lines.append("✅ *サービス監視 — 復旧*")
        for name, url, code in recovered:
            lines.append(f"  🟢 *{name}* — {code}")
            lines.append(f"     {url}")

    if test_notify and not new_down and not recovered:
        lines.append("🧪 *疎通テスト — 全サービスUP*")
        lines.append(f"  {len(monitors)} services 監視中、すべて正常応答")

    lines.append("")
    lines.append(f"_(チェック: {now_jst})_")

    text = "\n".join(lines)
    print("Posting to Slack:")
    print(text)
    post_slack(webhook, text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
