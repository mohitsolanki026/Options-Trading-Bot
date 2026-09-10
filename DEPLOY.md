# Deploying the Trading Bot to GCP

This bot is a **long-running, stateful process**: background threads (5-min monitor,
1-sec tick monitor, WebSocket feed, web dashboard) plus a daily scheduler, with local
state in `data/` (`paper_state.json`, `settings.json`, `risk_state.json`, the SQLite
trade journal) and logs in `logs/`.

➡️ **Use a Compute Engine VM with a systemd service.** Cloud Run / Cloud Functions are
a poor fit here — they are request-driven, scale to zero, kill long background threads,
and use an ephemeral filesystem that would lose `data/paper_state.json` and the journal.

## Timezone (IST vs UTC)

All market-hours logic uses naive `datetime.now()`, so it depends on the process clock.
The code now **forces IST in `config/settings.py`** (`TZ` defaults to `Asia/Kolkata` and
calls `time.tzset()`), so it is correct even though GCP VMs default to UTC. You do **not**
need to change the machine clock, but setting it too is harmless and recommended:

```bash
sudo timedatectl set-timezone Asia/Kolkata
```

To override (e.g. testing), set `TZ=UTC` in the env.

---

## 1. Create the VM

Pick **asia-south1 (Mumbai)** for the lowest latency to Angel One.

```bash
gcloud compute instances create trading-bot \
  --project=YOUR_PROJECT_ID \
  --zone=asia-south1-a \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB
```

(`e2-small` = 2 vCPU / 2 GB, ~₹1k–1.5k/mo. `e2-micro` works too but is tight.)

## 2. SSH in and install dependencies

```bash
gcloud compute ssh trading-bot --zone=asia-south1-a

sudo apt update && sudo apt install -y python3-venv python3-pip git
sudo timedatectl set-timezone Asia/Kolkata
```

## 3. Get the code + virtualenv

```bash
git clone YOUR_REPO_URL ~/trading_bot      # or scp your local checkout
cd ~/trading_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
mkdir -p logs data
```

## 4. Provide secrets (NOT in git)

The bot loads `.env.${APP_ENV}` then `.env`. Create a real `.env` on the VM only:

```bash
cp .env.development .env
nano .env        # fill ANGEL_*, TELEGRAM_*, the LLM key you use
```

> **Production tip:** instead of a plaintext `.env`, store secrets in
> **GCP Secret Manager** and fetch them in a startup script. At minimum, keep `.env`
> at `chmod 600` and never commit it. (The repo's `.gitignore` ignores `.env`,
> `.env.local`, `.env.testing`, `.env.production`.)

## 5. Run as a systemd service (auto-restart, boot-start)

Create `/etc/systemd/system/trading-bot.service`:

```ini
[Unit]
Description=AI Options Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_VM_USER
WorkingDirectory=/home/YOUR_VM_USER/trading_bot
Environment=APP_ENV=production
Environment=TZ=Asia/Kolkata
ExecStart=/home/YOUR_VM_USER/trading_bot/venv/bin/python bot.py
Restart=always
RestartSec=15
StandardOutput=append:/home/YOUR_VM_USER/trading_bot/logs/service.log
StandardError=append:/home/YOUR_VM_USER/trading_bot/logs/service.err

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot
sudo systemctl status trading-bot
```

## 6. The dashboard

A web UI runs **inside the same process**, on a daemon thread beside the monitor loops.
It is not a separate service on purpose: open positions, live tick prices, the entry-gate
verdict and feed health exist only in this process's memory, so a separate reader would
show a stale, partial picture and could not stop anything.

It binds to **loopback only** by default and requires a token.

```bash
# On the VM, find the token (generated once, chmod 600):
cat ~/trading_bot/data/web_token.txt
```

### Reaching it safely

The dashboard can close positions, so **do not open port 8787 to the internet.**
Two good options:

**Tailscale (simplest).** Install it on the VM and on your phone or laptop, then set
`WEB_UI_HOST=100.x.x.x` (the VM's Tailscale address) and browse to
`http://<vm>:8787`. No public exposure at all.

**SSH tunnel (nothing to install).**

```bash
gcloud compute ssh trading-bot --zone=asia-south1-a -- -N -L 8787:localhost:8787
# then open http://localhost:8787
```

For a permanent HTTPS address, put Caddy in front and keep the app on loopback.

### Environment

| Variable | Default | What it does |
|---|---|---|
| `WEB_UI_ENABLED` | `1` | Set `0` to run the bot with no dashboard at all |
| `WEB_UI_HOST` | `127.0.0.1` | Bind address |
| `WEB_UI_PORT` | `8787` | Port |
| `WEB_UI_TOKEN` | *(generated)* | Access token; blank writes one to `data/web_token.txt` |
| `WEB_UI_AUTH` | `on` | `off` disables the login — only on a private bind |

A dashboard failure never stops the bot: startup is wrapped, and every write goes
through a command queue drained by the tick loop rather than mutating trading state
from the web thread.

## 7. Settings: env is the default, the dashboard is the override

Tunables are still read from the environment, but they are now **defaults**. Anything
changed on the Settings screen is saved to `data/settings.json` and wins, and takes
effect at the next market check with no restart. Resetting a value to its default
removes the override, so a later env change reaches it again.

Editable live: capital per trade, daily and per-trade loss limits, max open positions,
which indices to watch, the entry window, cooldown, expiry and event blackouts, the
signal threshold, the IV-rank buy and sell levels, liquidity and premium floors, and
whether Telegram alerts are sent.

## 8. Operate

```bash
# Live logs
journalctl -u trading-bot -f
tail -f ~/trading_bot/logs/bot.log

# Restart after a code update
cd ~/trading_bot && git pull
sudo systemctl restart trading-bot
```

Keep the VM **running 24/7** — the bot self-schedules its own daily jobs (08:30 login,
09:30 scan, 15:30 EOD) and sleeps between them; the daily 08:30 `init_client` re-login is
required, so don't stop the instance overnight.

## 9. Persistence & safety notes

- `data/paper_state.json`, `data/settings.json`, `data/risk_state.json` and the SQLite
  journal survive restarts and live on the boot disk — back them up (or move to a
  Persistent Disk) if you care about history.
- On restart, the bot restores any open position and force-exits it if expired.
- **A daily loss halt now survives a restart.** `data/risk_state.json` records it with
  the date, and today's realised P&L is rehydrated from the trade journal. Previously a
  restart after the daily limit was hit brought the bot back up unhalted and it kept
  trading on the same bad day. A halt stamped with an earlier date is ignored, so each
  morning still starts clean.
- Resuming from the dashboard is refused while the loss limit is still breached.
- This deploys **paper mode**. Before going live (real orders), you'll add the broker
  order layer and re-verify — do not point this at real capital yet.

## Entry logic & tuning

Entries are gated in **code** (`utils/signal_engine.py:evaluate_entry`), not by the LLM.
A trade requires: a weighted confluence score ≥ threshold, a **confirmed directional/vol
bias**, rich **IV rank** for premium selling, no event/expiry blackout, no post-exit
cooldown, and correlation limits — then the LLM is asked once as a **veto** only.

Every check is evaluated and stored in the journal's `gate_log` table, not just the
first failure, which is what lets the dashboard's **Why no trade** screen explain a
quiet day in full.

All thresholds are env-tunable (see `.env.development`) and editable live in the
dashboard: `MAX_CAPITAL_PER_TRADE`,
`MAX_CORRELATED_SHORT`, `ENTRY_COOLDOWN_MIN`, `EXPIRY_BLACKOUT_DTE`,
`EVENT_BLACKOUT_DATES`, `IV_MIN_HISTORY`, `MIN_LEG_OI`, `MIN_CREDIT_PCT`,
`STRANGLE_TARGET_DELTA`.

**Two things need real-world calibration, not guesses:**
1. **IV rank needs history.** Until `IV_MIN_HISTORY` days of IV are recorded per index
   (`data/iv_history.json`), premium-selling is correctly *blocked* (fail-safe). Let the
   bot run in paper for a few weeks to build the history before judging it.
2. **Validate the signal weights / threshold with `utils/backtester.py`** against
   historical data rather than trusting the defaults. Maintain the macro-event list in
   `EVENT_BLACKOUT_DATES` / `data/event_blackout.json` (RBI, Fed, Budget).
