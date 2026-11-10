"""
MorningCues.py  -  6:30 AM global-cues report on ProTraderSetupBot (you + friends).

Uses the same pretradecheck_config.json as PreTradeCheck (telegram + holidays).
Skips weekends and NSE holidays automatically.

Run:   python MorningCues.py              send to Telegram
       python MorningCues.py --dry-run    print only
       python MorningCues.py --force      run even on a holiday/weekend (testing)
"""

import argparse
import datetime as dt
import json
import os
import traceback

import requests

from global_cues import IST, morning_cues

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "pretradecheck_config.json")
LOG = os.path.join(HERE, "MorningCues.log")

DEFAULT_DISCLAIMER = ("ℹ️ Not a SEBI-registered adviser. For educational purposes only, "
                      "not buy/sell advice. F&O trading carries high risk - always use a stop-loss.")


def log(text):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {dt.datetime.now(IST):%Y-%m-%d %H:%M:%S} =====\n{text}\n")
    except Exception:
        pass


def send(token, chat_id, text):
    for i in range(0, len(text), 4000):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text[i:i + 4000]}, timeout=20)
        r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    now = dt.datetime.now(IST)
    holidays = set(cfg.get("holidays", []))
    if not args.force and (now.weekday() >= 5 or now.date().isoformat() in holidays):
        msg = f"{now:%a %d-%b}: weekend / NSE holiday - morning cues skipped"
        print(msg); log(msg)
        return

    try:
        lines, _ = morning_cues(cfg, now)
        disclaimer = cfg.get("telegram", {}).get("friends_disclaimer", DEFAULT_DISCLAIMER)
        report = "\n".join([f"🌅 GLOBAL CUES · {now:%a %d-%b-%Y %H:%M} IST", ""] + lines
                           + (["", disclaimer] if disclaimer else []))
    except Exception:
        report = "❌ MorningCues crashed:\n" + traceback.format_exc()[-1500:]

    print(report)
    log(report)
    if args.dry_run:
        return

    tg = cfg["telegram"]
    as_list = lambda v: [str(c).strip() for c in (v if isinstance(v, list) else [v]) if str(c).strip()]
    me = as_list(tg.get("chat_id", []))
    crashed = report.startswith("❌")
    friends = [] if crashed else [c for c in as_list(tg.get("friend_chat_ids", [])) if c not in me]
    for cid in me + friends:
        try:
            send(tg["bot_token"], cid, report)
        except Exception as e:
            print(f"Telegram send to {cid} failed: {e}"); log(f"Telegram send to {cid} failed: {e}")


if __name__ == "__main__":
    main()
