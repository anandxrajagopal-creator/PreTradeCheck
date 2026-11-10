"""
global_cues.py  -  GLOBAL CUES for the 6:30 AM morning report (MorningCues.py).

  • GIFT Nifty    : NSE market-status feed (same source as ProTrader preopen_fno.py)
  • US markets    : Dow, S&P 500, Nasdaq (last session close)  +  S&P / Nasdaq futures (live)
  • Asian markets : Nikkei, Kospi, ASX 200, Taiwan, Hang Seng, Shanghai, Straits Times
                    -> 'live' if trading has started today, otherwise previous close
  • European mkts : FTSE 100, DAX, CAC 40, Euro Stoxx 50 (previous close)
  • Macro         : Brent, WTI, USD/INR, Dollar index, US 10Y yield, Gold, US VIX
  • US events     : HIGH-impact US data / Fed events in the next 24 h, in IST

Data: Yahoo Finance via yfinance;  economic calendar: ForexFactory weekly JSON feed.
Test on its own:   python global_cues.py
"""

import datetime as dt
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
ET = ZoneInfo("America/New_York")
OK, WARN = "✅", "⚠️"

US_INDICES = [("Dow", "^DJI"), ("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC")]
US_FUTURES = [("S&P fut", "ES=F"), ("Nasdaq fut", "NQ=F")]
ASIA = [("Nikkei", "^N225", "Asia/Tokyo"), ("Kospi", "^KS11", "Asia/Seoul"),
        ("ASX 200", "^AXJO", "Australia/Sydney"), ("Taiwan", "^TWII", "Asia/Taipei"),
        ("Hang Seng", "^HSI", "Asia/Hong_Kong"), ("Shanghai", "000001.SS", "Asia/Shanghai"),
        ("Straits Times", "^STI", "Asia/Singapore")]
EUROPE = [("FTSE 100", "^FTSE"), ("DAX", "^GDAXI"), ("CAC 40", "^FCHI"), ("Euro Stoxx 50", "^STOXX50E")]
MACRO = [("Brent", "BZ=F", "$"), ("WTI", "CL=F", "$"), ("USD/INR", "INR=X", "₹"),
         ("Dollar idx", "DX-Y.NYB", ""), ("US 10Y", "^TNX", "%"), ("Gold", "GC=F", "$"),
         ("US VIX", "^VIX", "")]

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NSE_HOME = "https://www.nseindia.com"
NSE_STATUS = "https://www.nseindia.com/api/marketStatus"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


# ----------------------------------------------------------------------------- helpers
def _download(tickers):
    import yfinance as yf
    df = yf.download(tickers, period="7d", interval="1d", progress=False,
                     group_by="ticker", auto_adjust=False, threads=True)
    out = {}
    for t in tickers:
        try:
            s = df[t]["Close"].dropna()
            if len(s) >= 2:
                out[t] = s
        except Exception:
            pass
    return out


def _pct(a, b):
    return (a / b - 1) * 100 if b else float("nan")


def _arrow(p):
    return "🟢" if p > 0.05 else ("🔴" if p < -0.05 else "⚪")


def _fmt(val, unit=""):
    if unit == "%":
        return f"{val:.2f}%"
    if unit == "₹":
        return f"₹{val:.2f}"
    if unit == "$":
        return f"${val:,.2f}"
    return f"{val:,.2f}" if val < 1000 else f"{val:,.0f}"


def close_lines(data, items):
    """Last value vs previous close."""
    lines = []
    for item in items:
        name, t = item[0], item[1]
        unit = item[2] if len(item) > 2 else ""
        s = data.get(t)
        if s is None:
            lines.append(f"{WARN} {name}: n/a")
            continue
        p = _pct(s.iloc[-1], s.iloc[-2])
        lines.append(f"{_arrow(p)} {name}: {_fmt(s.iloc[-1], unit)} ({p:+.2f}%)")
    return lines


def asia_lines(data, now):
    """'live' when today's bar exists in the exchange's own date, else previous close."""
    lines = []
    for name, t, tz in ASIA:
        s = data.get(t)
        if s is None:
            lines.append(f"{WARN} {name}: n/a")
            continue
        local_today = now.astimezone(ZoneInfo(tz)).date()
        live = s.index[-1].date() == local_today
        p = _pct(s.iloc[-1], s.iloc[-2])
        tag = "live" if live else "prev close"
        lines.append(f"{_arrow(p)} {name}: {_fmt(s.iloc[-1])} ({p:+.2f}%) {tag}")
    return lines


def us_events(now_ist, until_ist, impacts=("High",)):
    r = requests.get(FF_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0 MorningCues"})
    r.raise_for_status()
    events = []
    for e in r.json():
        if e.get("country") != "USD" or e.get("impact") not in impacts:
            continue
        try:
            t = dt.datetime.fromisoformat(e["date"]).astimezone(IST)
        except Exception:
            continue
        if now_ist - dt.timedelta(hours=1) <= t <= until_ist:
            events.append((t, e.get("title", "?"), e.get("forecast", ""), e.get("previous", "")))
    events.sort()
    lines = []
    for t, title, fc, prev in events:
        extra = ", ".join(x for x in (f"fc {fc}" if fc else "", f"prev {prev}" if prev else "") if x)
        lines.append(f"🔔 {t:%a %H:%M} IST  {title}" + (f"  ({extra})" if extra else ""))
    return lines


# ----------------------------------------------------------------------------- GIFT Nifty
def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def gift_nifty(tries=3):
    """GIFT Nifty from NSE's market-status feed (same source as preopen_fno.py).
    Returns (last, pct_change, timestamp_text)."""
    import time
    last_err = None
    for k in range(1, tries + 1):
        try:
            s = requests.Session()
            s.headers.update(NSE_HEADERS)
            s.get(NSE_HOME, timeout=20)          # NSE needs its cookies first
            time.sleep(1)
            r = s.get(NSE_STATUS, timeout=20)
            r.raise_for_status()
            g = r.json().get("giftnifty") or {}
            last, pct = _num(g.get("LASTPRICE")), _num(g.get("PERCHANGE"))
            if not last:
                raise RuntimeError("no GIFT Nifty price in NSE feed")
            return last, pct, str(g.get("TIMESTMP") or "")
        except Exception as e:
            last_err = e
            time.sleep(3 * k)
    raise RuntimeError(str(last_err)[:80])


def gift_line():
    last, pct, ts = gift_nifty()
    when = ""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            when = f" · as of {dt.datetime.strptime(ts, fmt):%d-%b %H:%M}"
            break
        except ValueError:
            continue
    pct_txt = f" ({pct:+.2f}%)" if pct is not None else ""
    return f"{_arrow(pct or 0)} GIFT Nifty: {last:,.1f}{pct_txt}{when}"


# ----------------------------------------------------------------------------- main block
def morning_cues(cfg=None, now=None):
    """Returns (lines, status) for the 6:30 AM report."""
    cfg = cfg or {}
    gc = cfg.get("global_cues", {})
    now = (now or dt.datetime.now(IST)).astimezone(IST)
    out, st = [], OK

    if gc.get("gift_nifty", True):
        try:
            out += ["— GIFT NIFTY —", gift_line(), ""]
        except Exception as e:
            st = WARN; out += ["— GIFT NIFTY —", f"{WARN} GIFT Nifty not available: {str(e)[:60]}", ""]

    try:
        tickers = ([t for _, t in US_INDICES] + [t for _, t in US_FUTURES] + [a[1] for a in ASIA]
                   + [t for _, t in EUROPE] + [m[1] for m in MACRO])
        data = _download(tickers)
        if not data:
            raise RuntimeError("no data from Yahoo")
        out += ["— US MARKETS (overnight close) —"] + close_lines(data, US_INDICES)
        out += close_lines(data, US_FUTURES)
        out += ["", "— ASIAN MARKETS —"] + asia_lines(data, now)
        out += ["", "— EUROPEAN MARKETS (prev close) —"] + close_lines(data, EUROPE)
        out += ["", "— CRUDE · CURRENCY · BONDS —"] + close_lines(data, MACRO)
        if any("n/a" in x for x in out):
            st = WARN
    except Exception as e:
        st = WARN; out.append(f"{WARN} Prices: {str(e)[:80]}")

    if gc.get("us_calendar", True):
        try:
            hours = gc.get("calendar_hours", 24)
            ev = us_events(now, now + dt.timedelta(hours=hours), tuple(gc.get("calendar_impacts", ["High"])))
            out += ["", f"— US EVENTS (next {hours}h, IST) —"]
            out += ev if ev else ["No high-impact US events"]
        except Exception as e:
            st = WARN; out += ["", f"{WARN} US calendar: {str(e)[:80]}"]
    return out, st


if __name__ == "__main__":
    lines, st = morning_cues()
    print("\n".join(lines))
    print("status:", st)
