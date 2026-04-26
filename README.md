# service-uptime-monitor

GitHub Actions上で5分おきに各サービスのHTTP死活を確認し、状態変化（down/recovery）のときだけ Slack `#alerts` に通知します。PCがオフでも稼働。

## 監視対象
`monitors.json` を編集して追加・削除。形式:
```json
{ "name": "表示名", "url": "https://...", "expect": [200, 307] }
```
`expect` に含まれない status code、または connection error / timeout を `down` 扱い。

## 通知ロジック
- 前回状態（`.state.json`、Actions cacheで永続化）と今回を比較
- `up→down` または `down→up` のときだけ Slack に飛ぶ
- 連続スパム抑止のため、ダウン中は何度チェックしても通知は1回のみ
- 復旧したら復旧通知1回

## 通知先
Slack `#alerts` (poyo-co.slack.com)
Webhook は GitHub Secrets `SLACK_ALERTS_WEBHOOK_URL`

## 手動実行
```bash
gh workflow run uptime.yml
gh workflow run uptime.yml -f test_notify=true   # 状態変化なくても疎通テスト送信
```

## ローカル実行
```bash
SLACK_WEBHOOK_URL="https://hooks.slack.com/..." python3 check.py
SLACK_WEBHOOK_URL="..." python3 check.py --test-notify
```

## 設計メモ
- 5分間隔は public repo 前提（private は無料 Actions 枠で足りない）
- リポジトリには webhook も鍵も含まない（Secrets のみ）
- Python 標準ライブラリのみ（依存なし）
