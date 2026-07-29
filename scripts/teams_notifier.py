#!/usr/bin/env python3
"""Microsoft Teams webhook notifications for DICOMpress.

Sends a Legacy-MessageCard alert when archiving fails (TEAMS_WEBHOOK_ERROR)
and an optional success summary per archived study (TEAMS_WEBHOOK_LOG).
Standard library only (urllib) — the receiver venv gains no new dependency.

Webhook URLs resolve in this order (first hit per key wins):
  1. the process environment,
  2. a .env file next to this script (a repo checkout / /usr/local/bin),
  3. ~/.config/dicompress/.env (same config dir as config.json).

archive_study.py normally runs headless (storescp --exec-on-eostudy under a
cron-launched service account), so check_and_prompt_teams_webhooks() only
prompts when stdin is a terminal; otherwise missing URLs just leave
notifications disabled. Interactively entered URLs are persisted to a .env
file (mode 600 — a webhook URL is a post-to-channel credential, same trust
model as config.json) so later headless runs pick them up.

A failure inside the notifier must never break archiving: send_teams_alert
catches everything and reports on the console only.
"""

import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

ERROR_VAR = "TEAMS_WEBHOOK_ERROR"
LOG_VAR = "TEAMS_WEBHOOK_LOG"

ENV_PATHS = (
    Path(__file__).resolve().parent / ".env",
    Path.home() / ".config" / "dicompress" / ".env",
)


def _read_env_file(path):
    """Parse KEY=VALUE lines; blanks, comments and malformed lines are skipped."""
    values = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _save_env_values(new_values):
    """Merge new values into the first writable .env candidate, mode 600."""
    last_err = None
    for path in ENV_PATHS:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            merged = _read_env_file(path)
            merged.update(new_values)
            path.write_text("".join(f"{k}={v}\n" for k, v in merged.items()))
            path.chmod(0o600)
            return path
        except OSError as e:
            last_err = e
    print(f"Teams notifier: could not save .env ({last_err}); URLs will not persist.")
    return None


def check_and_prompt_teams_webhooks():
    """Resolve webhook URLs into os.environ; offer interactive setup on a TTY.

    Headless runs (storescp / cron: stdin is not a terminal) never block on
    input() — missing URLs simply leave that notification channel disabled.
    """
    file_values = {}
    for path in ENV_PATHS:
        for key, value in _read_env_file(path).items():
            file_values.setdefault(key, value)

    entered = {}
    for var, purpose in ((ERROR_VAR, "Errors"), (LOG_VAR, "Logs")):
        if os.environ.get(var):
            continue
        if file_values.get(var):
            os.environ[var] = file_values[var]
            continue
        if not sys.stdin.isatty():
            continue
        url = input(
            f"{var} is not set. Enter Microsoft Teams Webhook URL for {purpose} "
            f"(or press Enter to skip): "
        ).strip()
        if not url:
            continue
        if not url.startswith("https://"):
            print(f"Teams notifier: {var} must be an https:// URL; skipping.")
            continue
        os.environ[var] = url
        entered[var] = url

    if entered:
        saved = _save_env_values(entered)
        if saved:
            print(f"Teams notifier: saved webhook URL(s) to {saved}")


def send_teams_alert(message, level="error"):
    """POST a MessageCard to the error or log webhook. Never raises.

    Returns True when Teams accepted the post, False otherwise (including
    "no URL configured", which is a normal state, reported quietly).
    """
    var = ERROR_VAR if level == "error" else LOG_VAR
    url = os.environ.get(var)
    if not url:
        return False

    host = socket.gethostname()
    if level == "error":
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "FF0000",
            "summary": "DICOMpress Error Alert",
            "text": (
                "🚨 **DICOMpress Error Alert**\n\n"
                f"**Host:** `{host}`\n**Details:** `{message}`"
            ),
        }
    else:
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "00FF00",
            "summary": "DICOMpress Transfer Log",
            "text": (
                "🟢 **DICOMpress Success**\n\n"
                f"**Host:** `{host}`\n**Summary:** `{message}`"
            ),
        }

    req = urllib.request.Request(
        url,
        data=json.dumps(card).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace").strip()
        # Legacy Office-365 connectors answer 200 with body "1" on success and
        # 200 with an error string otherwise; Power-Automate workflow URLs
        # answer 202 with an empty body. Treat anything else as a failure.
        if resp.status == 202 or (resp.status == 200 and body == "1"):
            return True
        print(f"Teams notifier: webhook rejected {level} card (HTTP {resp.status}: {body[:200]})")
        return False
    except Exception as e:  # noqa: BLE001 — the notifier must never crash archiving
        print(f"Teams notifier: failed to send {level} notification: {e}")
        return False
