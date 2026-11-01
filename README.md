# PreTradeCheck

Night-before trading health check. Runs at 8 PM (Sun–Thu) via Task Scheduler and sends one
Telegram report through **ProTraderSetupBot**:

- **Systems** – NSE holiday auto-update, Dhan login (TOTP), Data API plan validity,
  option recorder output, every task in the Task Scheduler `\ProTrader\` folder
- **Index homework** – NIFTY, BANKNIFTY, SENSEX: nearest expiry, previous-day H/L/C,
  pivots, CPR, bull/bear levels, max CE/PE OI strikes, PCR
- **Tomorrow's rules** – your own risk rules from the config

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy pretradecheck_config.example.json pretradecheck_config.json
notepad pretradecheck_config.json
```

Fill in Dhan credentials (or `credentials_file`), Telegram `bot_token` / `chat_id`,
and the recorder folder. `pretradecheck_config.json` is git-ignored — never commit it.

## Run

```
python PreTradeCheck.py --dry-run     # print only
python PreTradeCheck.py               # print + send to Telegram
python PreTradeCheck.py --test-totp   # debug Dhan TOTP login
```

Schedule: right-click `setup_task.bat` → Run as administrator
(creates `\ProTrader\ProTrader_PreTradeCheck`, headless via pythonw).
Every run is appended to `PreTradeCheck.log`.
