"""
PreTradeCheck.py  -  night-before (T-1) health check + NIFTY homework, sent as ONE Telegram message.

Checks
  1. Dhan login (client id + PIN + TOTP)  -> fresh token works
  2. Dhan profile                         -> Data API plan active + days left
  3. Option-data recorder                 -> last trading day's file exists and is not empty
  4. Windows Task Scheduler               -> ProTrader tasks: enabled, last result OK, next run time
  5. Expiry                               -> nearest NIFTY expiry from Dhan (is tomorrow pre-expiry / expiry?)
  6. Index homework (NIFTY/BANKNIFTY/SENSEX) -> prev-day H/L/C, Pivot + CPR + R1/S1/R2/S2,
                                             max CE / PE OI strikes and PCR for the nearest expiry

Run:   python PreTradeCheck.py              (uses pretradecheck_config.json next to this file)
       python PreTradeCheck.py --dry-run    (prints the report, does not send to Telegram)
"""

import argparse
import csv
import datetime as dt
import glob
import io
import json
import os
import subprocess
import sys
import time
import traceback

import requests

try:
    import pyotp
except ImportError:  # reported as a failed check instead of crashing
    pyotp = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "pretradecheck_config.json")

AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
API = "https://api.dhan.co/v2"
NIFTY_SECURITY_ID = 13          # NIFTY 50 index
NIFTY_SEG = "IDX_I"

OK, WARN, FAIL = "✅", "⚠️", "❌"


# ----------------------------------------------------------------------------- helpers
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Optional: read Dhan credentials from your existing credentials file
    cred_file = cfg.get("dhan", {}).get("credentials_file")
    if cred_file and os.path.exists(cred_file):
        with open(cred_file, "r", encoding="utf-8") as f:
            if cred_file.lower().endswith(".json"):
                creds = json.load(f)
            else:  # simple KEY=VALUE file (.env style)
                creds = {}
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        creds[k.strip()] = v.strip().strip('"').strip("'")
        keymap = cfg["dhan"].get("credential_keys", {})
        for field in ("client_id", "pin", "totp_secret"):
            src_key = keymap.get(field, field)
            if src_key in creds:
                cfg["dhan"][field] = str(creds[src_key])
    return cfg


NSE_HOME = "https://www.nseindia.com"
NSE_HOLIDAY_API = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/resources/exchange-communication-holidays",
}


def parse_nse_holidays(data, segment="FO"):
    """NSE returns {"CM":[...], "FO":[...], ...}; each item has tradingDate like '26-Jan-2026'."""
    items = data.get(segment) or data.get("CM") or []
    out = {}
    for it in items:
        raw = str(it.get("tradingDate", "")).strip()
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d"):
            try:
                d = dt.datetime.strptime(raw, fmt).date()
                out[d.isoformat()] = str(it.get("description", "")).strip()
                break
            except ValueError:
                continue
    return dict(sorted(out.items()))


def fetch_nse_holidays(segment="FO"):
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    s.get(NSE_HOME, timeout=20)          # NSE needs its cookies before the API answers
    time.sleep(1)
    r = s.get(NSE_HOLIDAY_API, timeout=20)
    r.raise_for_status()
    hol = parse_nse_holidays(r.json(), segment)
    if not hol:
        raise RuntimeError("NSE returned no holidays")
    return hol


def update_holidays_in_config(cfg, config_path):
    """Fetch NSE holidays and save them into the config file. Returns (status, line)."""
    try:
        hol = fetch_nse_holidays(cfg.get("holiday_segment", "FO"))
    except Exception as e:
        n = len(cfg.get("holidays", []))
        return WARN, f"{WARN} NSE holiday update failed ({str(e)[:80]}); using saved list ({n} dates)"
    # re-read the raw file so merged credentials are never written back
    with open(config_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    old = set(raw.get("holidays", []))
    raw["holidays"] = list(hol.keys())
    raw["holiday_names"] = hol
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    cfg["holidays"], cfg["holiday_names"] = raw["holidays"], hol
    added = len(set(hol) - old)
    return OK, f"{OK} NSE holidays updated: {len(hol)} dates" + (f" ({added} new)" if added else "")


def today_ist():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()


def is_trading_day(d, holidays):
    return d.weekday() < 5 and d.isoformat() not in holidays


def next_trading_day(d, holidays):
    n = d + dt.timedelta(days=1)
    while not is_trading_day(n, holidays):
        n += dt.timedelta(days=1)
    return n


def last_trading_day(d, holidays, include_today=True):
    n = d if include_today else d - dt.timedelta(days=1)
    while not is_trading_day(n, holidays):
        n -= dt.timedelta(days=1)
    return n


def send_telegram(token, chat_id, text):
    # Telegram limit is 4096 chars per message
    for i in range(0, len(text), 4000):
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text[i:i + 4000]},
            timeout=20,
        )
        if not r.ok:
            try:
                reason = r.json().get("description", r.text)
            except ValueError:
                reason = r.text
            raise RuntimeError(f"HTTP {r.status_code}: {reason}")


# ----------------------------------------------------------------------------- Dhan
class Dhan:
    def __init__(self, client_id):
        self.client_id = client_id
        self.token = None

    def login(self, pin, totp_secret):
        if pyotp is None:
            raise RuntimeError("pyotp not installed (pip install pyotp)")
        if not str(totp_secret).strip() or not str(pin).strip():
            raise RuntimeError("PIN or TOTP secret is EMPTY - check credentials_file / credential_keys in config")
        totp = pyotp.TOTP(str(totp_secret).replace(" ", "").upper()).now()
        r = requests.post(
            AUTH_URL,
            params={"dhanClientId": self.client_id, "pin": pin, "totp": totp},
            timeout=20,
        )
        data = r.json() if r.content else {}
        tok = data.get("accessToken") or data.get("access_token")
        if not tok:
            raise RuntimeError(f"login failed: HTTP {r.status_code} {str(data)[:200]}")
        self.token = tok
        return data

    def _h(self):
        return {"access-token": self.token, "client-id": self.client_id,
                "Content-Type": "application/json", "Accept": "application/json"}

    def get(self, path):
        r = requests.get(API + path, headers=self._h(), timeout=20)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = requests.post(API + path, headers=self._h(), json=body, timeout=30)
        r.raise_for_status()
        return r.json()


# ----------------------------------------------------------------------------- checks
def check_dhan_profile(dhan):
    p = dhan.get("/profile")
    plan = str(p.get("dataPlan", "?"))
    validity = str(p.get("dataValidity", "")).strip()
    lines, status = [], OK
    if plan.lower() != "active":
        status = FAIL
    days_left = None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            days_left = (dt.datetime.strptime(validity, fmt).date() - today_ist()).days
            break
        except ValueError:
            continue
    if days_left is not None and days_left <= 3 and status == OK:
        status = WARN
    txt = f"{status} Data API plan: {plan}"
    if validity:
        txt += f" (valid till {validity}" + (f", {days_left}d left)" if days_left is not None else ")")
    lines.append(txt)
    return status, lines


def check_recorder(cfg, holidays):
    rc = cfg.get("recorder", {})
    folder, pattern = rc.get("folder"), rc.get("glob", "*")
    min_kb = rc.get("min_size_kb", 1)
    if not folder:
        return WARN, [f"{WARN} Recorder check skipped (set recorder.folder in config)"]
    if not os.path.isdir(folder):
        return FAIL, [f"{FAIL} Recorder folder not found: {folder}"]
    ltd = last_trading_day(today_ist(), holidays, include_today=True)
    files = glob.glob(os.path.join(folder, "**", pattern), recursive=True)
    todays = []
    for f in files:
        mdate = dt.datetime.fromtimestamp(os.path.getmtime(f)).date()
        if mdate >= ltd:
            todays.append(f)
    if not todays:
        newest = max(files, key=os.path.getmtime) if files else None
        when = dt.datetime.fromtimestamp(os.path.getmtime(newest)).strftime("%d-%b %H:%M") if newest else "never"
        return FAIL, [f"{FAIL} Recorder: no file saved for {ltd:%a %d-%b} (newest file: {when})"]
    size_kb = sum(os.path.getsize(f) for f in todays) / 1024
    if size_kb < min_kb:
        return FAIL, [f"{FAIL} Recorder: {len(todays)} file(s) for {ltd:%a %d-%b} but only {size_kb:.1f} KB"]
    return OK, [f"{OK} Recorder: {len(todays)} file(s) for {ltd:%a %d-%b}, {size_kb/1024:.1f} MB"]


SCHTASK_RESULTS = {"0": "OK", "1": "error 1", "267009": "running", "267011": "never run",
                   "267014": "terminated", "2147750687": "already running"}


def check_scheduler(cfg):
    sc = cfg.get("scheduler", {})
    folders = [f.strip("\\").lower() for f in sc.get("folders", ["ProTrader"])]
    names = [n.lower() for n in sc.get("extra_tasks", [])]
    if os.name != "nt":
        return WARN, [f"{WARN} Task Scheduler check skipped (not Windows)"]
    out = subprocess.run(["schtasks", "/query", "/fo", "CSV", "/v"], capture_output=True,
                         text=True, encoding="mbcs", errors="replace",
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    rows = [r for r in csv.DictReader(io.StringIO(out)) if r.get("TaskName") and r["TaskName"] != "TaskName"]
    picked, seen = [], set()
    for r in rows:
        tn = r["TaskName"]
        parts = tn.strip("\\").lower().split("\\")
        if (len(parts) > 1 and parts[0] in folders) or parts[-1] in names:
            if tn not in seen:
                seen.add(tn)
                picked.append(r)
    if not picked:
        return FAIL, [f"{FAIL} No scheduled tasks found in {sc.get('folders')} / {sc.get('extra_tasks')}"]
    worst, lines = OK, []
    for r in picked:
        name = r["TaskName"].strip("\\").split("\\")[-1]
        state = (r.get("Scheduled Task State") or r.get("Status") or "").strip()
        res = str(r.get("Last Result", "")).strip()
        res_txt = SCHTASK_RESULTS.get(res, f"code {res}")
        nxt = (r.get("Next Run Time") or "").strip()
        st = OK
        if state.lower() == "disabled":
            st = FAIL
        elif res not in ("0", "267009", "267011"):
            st = FAIL
        elif res == "267011" or nxt.upper() in ("N/A", ""):
            st = WARN
        worst = FAIL if FAIL in (worst, st) else (WARN if WARN in (worst, st) else OK)
        lines.append(f"{st} {name}: last={res_txt}, next={nxt or 'N/A'}" + (" [DISABLED]" if state.lower() == "disabled" else ""))
    return worst, lines


def index_expiry(dhan, sec_id, next_day):
    data = dhan.post("/optionchain/expirylist", {"UnderlyingScrip": sec_id, "UnderlyingSeg": NIFTY_SEG})
    exps = sorted(dt.date.fromisoformat(e) for e in data.get("data", []))
    upcoming = [e for e in exps if e >= next_day]
    if not upcoming:
        raise RuntimeError("no upcoming expiry returned")
    return upcoming[0]


def prev_day_ohlc(dhan, sec_id):
    to_d = today_ist() + dt.timedelta(days=1)
    from_d = to_d - dt.timedelta(days=15)
    d = dhan.post("/charts/historical", {
        "securityId": str(sec_id), "exchangeSegment": NIFTY_SEG, "instrument": "INDEX",
        "expiryCode": 0, "oi": False, "fromDate": from_d.isoformat(), "toDate": to_d.isoformat()})
    i = len(d["close"]) - 1
    day = dt.datetime.fromtimestamp(d["timestamp"][i], dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
    return day, float(d["high"][i]), float(d["low"][i]), float(d["close"][i])


def pivots(h, l, c):
    p = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * p - bc
    return {"P": p, "TC": max(tc, bc), "BC": min(tc, bc),
            "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + (h - l), "S2": p - (h - l),
            "CPR_width_pct": abs(tc - bc) / p * 100}


def oi_summary(chain):
    oc = chain.get("data", {}).get("oc", {})
    spot = chain.get("data", {}).get("last_price")
    ce = {float(k): (v.get("ce") or {}).get("oi", 0) or 0 for k, v in oc.items()}
    pe = {float(k): (v.get("pe") or {}).get("oi", 0) or 0 for k, v in oc.items()}
    if not ce:
        raise RuntimeError("empty option chain")
    tot_ce, tot_pe = sum(ce.values()), sum(pe.values())
    top = lambda d: [kv for kv in sorted(d.items(), key=lambda kv: kv[1], reverse=True) if kv[1] > 0][:2]
    if spot:  # resistance = calls AT/ABOVE spot, support = puts AT/BELOW spot
        ce = {k: v for k, v in ce.items() if k >= spot}
        pe = {k: v for k, v in pe.items() if k <= spot}
    return {"spot": spot, "ce_top": top(ce), "pe_top": top(pe),
            "pcr": (tot_pe / tot_ce) if tot_ce else float("nan")}


DEFAULT_INDICES = [
    {"name": "NIFTY", "security_id": 13},
    {"name": "BANKNIFTY", "security_id": 25},
    {"name": "SENSEX", "security_id": 51},
]


def index_block(dhan, idx, nd, holidays):
    """Homework lines for one index. Returns (lines, worst_status)."""
    name, sid = idx["name"], int(idx["security_id"])
    out, st = ["", f"— {name} HOMEWORK —"], OK
    expiry = None
    try:
        expiry = index_expiry(dhan, sid, nd)
        if expiry == nd:
            tag = "‼️ EXPIRY DAY"
        elif expiry == next_trading_day(nd, holidays):
            tag = "⚠️ day before expiry - heavy theta on OTM"
        else:
            tag = f"{(expiry - nd).days} days to expiry"
        out.append(f"Nearest expiry: {expiry:%a %d-%b}  ({tag})")
    except Exception as e:
        st = WARN; out.append(f"{WARN} Expiry: {e}")

    pv = None
    try:
        time.sleep(1)
        day, h, l, c = prev_day_ohlc(dhan, sid)
        pv = pivots(h, l, c)
        out.append(f"Prev day {day:%d-%b}: H {h:.0f} | L {l:.0f} | C {c:.0f} | Range {h-l:.0f}")
        out.append(f"R2 {pv['R2']:.0f} | R1 {pv['R1']:.0f} | P {pv['P']:.0f} | S1 {pv['S1']:.0f} | S2 {pv['S2']:.0f}")
        cpr_kind = "NARROW → trending day likely" if pv["CPR_width_pct"] < 0.2 else (
            "WIDE → sideways day likely" if pv["CPR_width_pct"] > 0.5 else "normal")
        out.append(f"CPR {pv['BC']:.0f} – {pv['TC']:.0f} ({pv['CPR_width_pct']:.2f}%, {cpr_kind})")
        pos = "above CPR (mild bullish)" if c > pv["TC"] else ("below CPR (mild bearish)" if c < pv["BC"] else "inside CPR (neutral)")
        out.append(f"Prev close is {pos}")
        bull = sorted({round(pv["TC"]), round(h), round(pv["R1"]), round(pv["R2"])})
        bear = sorted({round(pv["BC"]), round(l), round(pv["S1"]), round(pv["S2"])}, reverse=True)
        out.append("📈 Bull above: " + " → ".join(str(x) for x in bull))
        out.append("📉 Bear below: " + " → ".join(str(x) for x in bear))
    except Exception as e:
        st = WARN; out.append(f"{WARN} Prev-day levels: {e}")

    if expiry:
        try:
            time.sleep(3.1)  # Dhan option-chain rate limit: 1 request / 3 s
            chain = dhan.post("/optionchain", {"UnderlyingScrip": sid,
                                               "UnderlyingSeg": NIFTY_SEG, "Expiry": expiry.isoformat()})
            s = oi_summary(chain)
            fmt = lambda lst: ", ".join(f"{k:.0f} ({v/1e5:.1f}L)" for k, v in lst)
            out.append(f"Max CE OI (resistance): {fmt(s['ce_top'])}")
            out.append(f"Max PE OI (support):    {fmt(s['pe_top'])}")
            bias = "bullish tilt" if s["pcr"] > 1.2 else ("bearish tilt" if s["pcr"] < 0.8 else "neutral")
            out.append(f"PCR {s['pcr']:.2f} ({bias})")
        except Exception as e:
            st = WARN; out.append(f"{WARN} Option chain: {e}")
    return out, st


# ----------------------------------------------------------------------------- main
def build_report(cfg, holiday_line=None):
    holidays = set(cfg.get("holidays", []))
    t = today_ist()
    nd = next_trading_day(t, holidays)
    out, worst = [], OK

    def mark(st):
        nonlocal worst
        if st == FAIL or (st == WARN and worst == OK):
            worst = st

    out.append(f"🗓 PRE-TRADE CHECK for {nd:%a %d-%b-%Y}")
    names = cfg.get("holiday_names", {})
    upcoming = [d for d in sorted(holidays)
                if d > t.isoformat() and dt.date.fromisoformat(d).weekday() < 5]
    if upcoming:
        h = dt.date.fromisoformat(upcoming[0])
        nm = names.get(upcoming[0], "")
        out.append(f"Next NSE holiday: {h:%a %d-%b}" + (f" ({nm})" if nm else ""))
    out.append("")
    out.append("— SYSTEMS —")
    if holiday_line:
        mark(holiday_line[0]); out.append(holiday_line[1])

    dcfg = cfg["dhan"]
    dhan = Dhan(dcfg["client_id"])
    logged_in = False
    try:
        dhan.login(dcfg["pin"], dcfg["totp_secret"])
        logged_in = True
        out.append(f"{OK} Dhan login (TOTP) OK")
    except Exception as e:
        mark(FAIL)
        out.append(f"{FAIL} Dhan login: {e}")

    if logged_in:
        try:
            st, lines = check_dhan_profile(dhan); mark(st); out += lines
        except Exception as e:
            mark(WARN); out.append(f"{WARN} Profile check: {e}")

    for fn in (lambda: check_recorder(cfg, holidays), lambda: check_scheduler(cfg)):
        try:
            st, lines = fn(); mark(st); out += lines
        except Exception as e:
            mark(WARN); out.append(f"{WARN} {e}")

    if logged_in:
        for i, idx in enumerate(cfg.get("indices", DEFAULT_INDICES)):
            if i:
                time.sleep(3.1)  # Dhan option-chain rate limit: 1 request / 3 s
            lines, st = index_block(dhan, idx, nd, holidays)
            mark(st)
            out += lines

    rules = cfg.get("risk_rules", [])
    if rules:
        out.append("")
        out.append("— TOMORROW'S RULES —")
        out += [f"• {r}" for r in rules]

    out.append("")
    verdict = {OK: "✅ ALL GOOD - ready for tomorrow",
               WARN: "⚠️ CHECK THE WARNINGS ABOVE",
               FAIL: "❌ FIX BEFORE MARKET OPEN"}[worst]
    out.insert(1, verdict)
    return "\n".join(out), worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only, don't send to Telegram")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--test-totp", action="store_true", help="show the TOTP code being generated, then exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.test_totp:
        d = cfg["dhan"]
        sec = str(d.get("totp_secret", "")).replace(" ", "").upper()
        print(f"client_id   : {d.get('client_id') or '(EMPTY)'}")
        print(f"pin         : {'*' * len(str(d.get('pin', ''))) or '(EMPTY)'}")
        print(f"totp_secret : {(sec[:4] + '...' + sec[-2:]) if sec else '(EMPTY)'}  ({len(sec)} chars)")
        if sec and pyotp:
            print(f"TOTP now    : {pyotp.TOTP(sec).now()}   <- compare with your authenticator app")
        print(f"PC time     : {dt.datetime.now():%H:%M:%S}")
        return
    holiday_line = None
    if cfg.get("auto_update_holidays", True):
        holiday_line = update_holidays_in_config(cfg, args.config)
    try:
        report, worst = build_report(cfg, holiday_line)
    except Exception:
        report, worst = "❌ PreTradeCheck crashed:\n" + traceback.format_exc()[-1500:], FAIL

    print(report)
    try:  # keep a log, because a headless (pythonw) run has no window
        with open(os.path.join(HERE, "PreTradeCheck.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} =====\n{report}\n")
    except Exception:
        pass
    if not args.dry_run:
        tg = cfg["telegram"]
        ids = tg["chat_id"] if isinstance(tg["chat_id"], list) else [tg["chat_id"]]
        for cid in [str(c).strip() for c in ids if str(c).strip()]:
            try:
                send_telegram(tg["bot_token"], cid, report)
            except Exception as e:
                print(f"Telegram send to {cid} failed: {e}")
                try:
                    with open(os.path.join(HERE, "PreTradeCheck.log"), "a", encoding="utf-8") as f:
                        f.write(f"Telegram send to {cid} failed: {e}\n")
                except Exception:
                    pass
    sys.exit(2 if report.startswith("❌ PreTradeCheck crashed") else 0)


if __name__ == "__main__":
    main()
