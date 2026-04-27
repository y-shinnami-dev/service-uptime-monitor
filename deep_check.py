"""
Playwright 深層監視: 主要ページがブラウザ視点で正しく描画されているかを確認。
HTTPステータスは正常でも、CSS/JSバグで本文が見えない事故を検知する。

検査項目:
  - HTTP 200
  - <main>/<header>/<footer> 要素の存在
  - main の高さ >= min_main_height
  - body innerText の文字数 >= min_body_text
  - <main> 配下の <section> で opacity < 0.5 のものがないか
  - JSコンソールエラー（無視リスト除外）が無いか
  - フォームページなら <form> 要素が存在するか
  - 必須テキストが含まれているか

異常検知時は Slack に通知。状態変化（OK→NG, NG→OK）のときだけ送る。
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".deep_state.json"
CONFIG_FILE = ROOT / "deep_monitors.json"


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def slack_notify(webhook, text):
    if not webhook:
        print("(no SLACK_WEBHOOK_URL set; would have sent: " + text + ")")
        return
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        print(f"slack send failed: {e}")


PROBE_JS = """() => {
    const m = document.querySelector('main');
    const h = document.querySelector('header');
    const f = document.querySelector('footer');
    const hidden = [];
    if (m) {
        m.querySelectorAll('section').forEach(s => {
            const op = parseFloat(getComputedStyle(s).opacity);
            const vis = getComputedStyle(s).visibility;
            const disp = getComputedStyle(s).display;
            if (op < 0.5 || vis === 'hidden' || disp === 'none') {
                hidden.push((s.className || s.tagName) + ' (op=' + op + ',vis=' + vis + ',disp=' + disp + ')');
            }
        });
    }
    return {
        hasMain: !!m, mainHeight: m ? m.offsetHeight : 0,
        hasHeader: !!h, hasFooter: !!f,
        bodyText: document.body.innerText.length,
        hiddenSections: hidden,
        formCount: document.querySelectorAll('form').length,
        bodyHTML: document.body.innerHTML.length,
    };
}"""


def check_page(page, target, defaults):
    rules = {**defaults, **(target.get("rules") or {})}
    url = target["url"]
    label = target["label"]
    errors = []
    console_errors = []

    def on_console(msg):
        if msg.type == "error":
            text = (msg.text or "")[:300]
            if not any(p in text for p in defaults.get("ignore_console_patterns", [])):
                console_errors.append(text)

    def on_pageerror(err):
        text = str(err)[:300]
        if not any(p in text for p in defaults.get("ignore_console_patterns", [])):
            console_errors.append("PAGEERROR: " + text)

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)

    try:
        resp = page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1200)
    except Exception as e:
        return [f"goto failed: {e}"]

    if resp is None:
        errors.append("no response")
    elif resp.status != 200:
        errors.append(f"HTTP {resp.status}")

    try:
        info = page.evaluate(PROBE_JS)
    except Exception as e:
        errors.append(f"probe failed: {e}")
        return errors

    if not info["hasMain"]: errors.append("no <main>")
    if not info["hasHeader"]: errors.append("no <header>")
    if not info["hasFooter"]: errors.append("no <footer>")
    if info["mainHeight"] < rules.get("min_main_height", 800):
        errors.append(f"main too small ({info['mainHeight']}px < {rules['min_main_height']}px)")
    if info["bodyText"] < rules.get("min_body_text", 600):
        errors.append(f"body text too short ({info['bodyText']} chars < {rules['min_body_text']})")
    if len(info["hiddenSections"]) > rules.get("max_hidden_sections", 0):
        errors.append(f"hidden sections: {info['hiddenSections'][:5]}")

    if rules.get("must_have_form") and info["formCount"] == 0:
        errors.append("missing <form>")

    must_text = rules.get("must_have_text") or []
    if must_text:
        try:
            html = page.content()
            for t in must_text:
                if t not in html:
                    errors.append(f"missing required text: {t!r}")
        except Exception:
            pass

    if console_errors:
        errors.append(f"JS errors ({len(console_errors)}): " + " / ".join(console_errors[:3]))

    return errors


def main():
    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    defaults = config["default_rules"]
    targets = config["targets"]

    test_notify = "--test-notify" in sys.argv
    webhook = os.environ.get("SLACK_WEBHOOK_URL")

    state = load_state()
    new_state = {}
    transitions = []  # (label, prev, now, errors)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for t in targets:
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 800},
                user_agent="Mozilla/5.0 (compatible; corems-deep-monitor/1.0)",
            )
            page = ctx.new_page()
            errors = check_page(page, t, defaults)
            ctx.close()

            label = t["label"]
            now = "OK" if not errors else "NG"
            prev = state.get(label, {}).get("status")
            new_state[label] = {"status": now, "errors": errors[:5], "url": t["url"]}
            print(f"[{now}] {label}  errors={errors}")
            # 初回（prev=None）かつ OK は通知しない（ノイズ削減）
            if prev != now and not (prev is None and now == "OK"):
                transitions.append((label, prev, now, errors))

        browser.close()

    save_state(new_state)

    # Slack notify (transitions or test)
    if test_notify:
        ng_lines = [f"- {label}: {' / '.join(errs)}" for label, _, _, errs in transitions if errs] or ["(no transitions)"]
        msg = ":mag: *core-ms deep monitor TEST*\n" + "\n".join(ng_lines)
        slack_notify(webhook, msg)
        return

    if not transitions:
        print("no transitions; nothing to notify")
        return

    lines = []
    for label, prev, now, errs in transitions:
        if now == "NG":
            url = next(t["url"] for t in targets if t["label"] == label)
            lines.append(f":rotating_light: *{label}* OK→NG  {url}\n  " + "\n  ".join(errs[:5]))
        else:
            lines.append(f":white_check_mark: *{label}* NG→OK  recovered")
    msg = "*core-ms deep monitor*\n" + "\n".join(lines)
    slack_notify(webhook, msg)


if __name__ == "__main__":
    main()
