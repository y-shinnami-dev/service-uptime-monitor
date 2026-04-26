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
JST = timezone(timedelta(hours=9))


def check_one(monitor):
    name = monitor["name"]
    url = monitor["url"]
    expected = set(monitor.get("expect", [200]))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "service-uptime-monitor/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception as e:
        return name, url, None, False, str(e)
    is_up = code in expected
    err = None if is_up else f"unexpected status {code}"
    return name, url, code, is_up, err


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


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    monitors = load_json(MONITORS_PATH, [])
    prev_state = load_json(STATE_PATH, {})
    test_notify = "--test-notify" in sys.argv

    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    print("=== Service Uptime Check ===")
    print(f"実行: {now_jst}")
    print(f"監視対象: {len(monitors)} services")

    new_state = {}
    results = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(check_one, m): m for m in monitors}
        for fut in as_completed(futures):
            name, url, code, is_up, err = fut.result()
            results[name] = (url, code, is_up, err)
            new_state[name] = "up" if is_up else "down"

    for name in sorted(results):
        url, code, is_up, err = results[name]
        mark = "✓" if is_up else "✗"
        code_str = str(code) if code is not None else "ERR"
        print(f"  {mark} {name:25s} {code_str:>5}  {url}")

    new_down = []
    recovered = []
    for name, status in new_state.items():
        prev = prev_state.get(name, "up")
        if prev == "up" and status == "down":
            url, code, _, err = results[name]
            new_down.append((name, url, code, err))
        elif prev == "down" and status == "up":
            url, code, _, _ = results[name]
            recovered.append((name, url, code))

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
