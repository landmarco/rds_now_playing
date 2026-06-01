# rds_now_playing

Polls the WXDU now-playing endpoint every 10 seconds and pushes the current artist/song to an RDS transmitter over telnet. If the connection drops for any reason, the script waits 60 seconds and reconnects automatically.

## Prerequisites

- macOS (iMac or Mac mini)
- Python 3 (comes with macOS; verify with `python3 --version`)
- Network access to the RDS transmitter at `71.210.6.210:5423`

## Setup

**1. Clone or copy the project to the iMac**

```bash
git clone <repo-url> ~/codetools/rds_now_playing
cd ~/codetools/rds_now_playing
```

**2. Create a virtual environment and install dependencies**

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**3. Add your credentials**

Open `main.py` and replace the two `*******` placeholders on the telnet login lines with the actual login and password for the RDS transmitter.

## Running manually

```bash
venv/bin/python3 main.py
```

Press `Ctrl+C` to stop.

## Running as a background service (launchd)

This installs the script as a **LaunchAgent** — it starts automatically when you log in and restarts itself if it ever crashes.

**1. Create the logs directory**

```bash
mkdir -p ~/codetools/rds_now_playing/logs
```

**2. Install the plist**

The `sed` command below fills in your username automatically:

```bash
sed "s/YOUR_USERNAME/$(whoami)/g" org.wxdu.rds-now-playing.plist \
  > ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist
```

**3. Load and start the service**

```bash
launchctl load ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist
```

The service starts immediately and will restart on every login/reboot.

## Managing the service

| Task | Command |
|------|---------|
| Check it's running | `launchctl list \| grep wxdu` |
| View live output | `tail -f ~/codetools/rds_now_playing/logs/rds_out.log` |
| View errors | `tail -f ~/codetools/rds_now_playing/logs/rds_err.log` |
| Stop | `launchctl stop org.wxdu.rds-now-playing` |
| Start | `launchctl start org.wxdu.rds-now-playing` |
| Disable permanently | `launchctl unload ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist` |
