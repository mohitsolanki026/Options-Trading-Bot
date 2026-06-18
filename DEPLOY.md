# Deploying the Trading Bot to GCP

This bot is a **long-running, stateful process**: background threads (5-min monitor,
1-sec tick monitor, WebSocket feed) plus a daily scheduler, with local state in
`data/` (`paper_state.json`, the SQLite trade journal) and logs in `logs/`.

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

## 6. Operate

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

## 7. Persistence & safety notes

- `data/paper_state.json` and the SQLite journal survive restarts and live on the boot
  disk — back them up (or move to a Persistent Disk) if you care about history.
- On restart, the bot restores any open position and force-exits it if expired.
- This deploys **paper mode**. Before going live (real orders), you'll add the broker
  order layer and re-verify — do not point this at real capital yet.
