# service-uptime-monitor

GitHub Actions上で5分おきに各サービスのHTTP死活を確認し、状態変化（down/recovery）のときだけ Slack `#alerts` に通知します。PCがオフでも稼働。

## 監視対象
`monitors.json` を編集して追加・削除。形式:
```json
{ "name": "表示名", "url": "https://...", "expect": [200, 307] }
```
`expect` に含まれない status code、または connection error / timeout を `down` 扱い。

## 偽陽性抑制（2段階）
1. **同一run内リトライ**: 1回失敗したら 3 秒待って再試行、2回連続失敗で初めて「この run は失敗」と判定（ランナー側の瞬断対策）
2. **連続失敗しきい値** (`CONSEC_FAIL_THRESHOLD = 2`): 2 run連続で失敗した時のみ `down` 扱いにし通知。1 run だけの失敗は status を維持し通知しない（共有ホスティングなどの30秒級の瞬断対策）。`up→down` 通知は実害発生から最大1 cron間隔（約5分）遅れる代わりに、日次のフラッピングを抑える。

## 通知ロジック
- 前回状態（`.state.json`、Actions cacheで永続化）と今回を比較
- `up→down` または `down→up` のときだけ Slack に飛ぶ
- 連続スパム抑止のため、ダウン中は何度チェックしても通知は1回のみ
- 復旧したら復旧通知1回（復旧は1回成功で即時通知。streak は不要）

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
