"""GCP billing health checker — runs daily on GitHub Actions.

Sends a Slack #alerts notification if:
  - the project's billing is disabled, or
  - the linked billing account is closed.

Both conditions cause Cloud Run / Functions to stop serving with 503 the
same way cocorondo-bot died on 2026-05-25 (silent until end users notice).
"""

import json
import os
import subprocess
import sys
import urllib.request

PROJECT_ID = "metal-density-488308-b6"
PROJECT_LABEL = "cocorondo-bot (metal-density-488308-b6)"
BILLING_ACCOUNT = "0186B9-681C7A-5FFFDC"
SLACK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")


def slack(text: str) -> None:
    if not SLACK_URL:
        print("WARN: SLACK_WEBHOOK_URL is not set", file=sys.stderr)
        return
    req = urllib.request.Request(
        SLACK_URL,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def gcloud(args: list[str]) -> str:
    r = subprocess.run(["gcloud", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gcloud failed: {' '.join(args)}\nstderr: {r.stderr.strip()}")
    return r.stdout.strip()


def main() -> int:
    problems: list[str] = []

    billing_enabled = gcloud([
        "beta", "billing", "projects", "describe", PROJECT_ID,
        "--format=value(billingEnabled)",
    ])
    if billing_enabled.lower() != "true":
        problems.append(
            f":rotating_light: *GCP billing DISABLED on project*\n"
            f"  project: `{PROJECT_ID}` ({PROJECT_LABEL})\n"
            f"  billingEnabled: `{billing_enabled}`\n"
            f"  → Cloud Run will return 503. Re-link a billing account immediately."
        )

    account_open = gcloud([
        "billing", "accounts", "describe", f"billingAccounts/{BILLING_ACCOUNT}",
        "--format=value(open)",
    ])
    if account_open.lower() != "true":
        problems.append(
            f":warning: *GCP billing account CLOSED*\n"
            f"  account: `{BILLING_ACCOUNT}`\n"
            f"  open: `{account_open}`\n"
            f"  → Free trial may have ended or payment failed."
        )

    if problems:
        slack("\n\n".join(problems))
        for p in problems:
            print(p)
        return 1

    print(f"OK: project={PROJECT_ID} billingEnabled=True, account={BILLING_ACCOUNT} open=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())
