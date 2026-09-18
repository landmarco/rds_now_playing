# rds_now_playing

Polls the WXDU now-playing endpoint every 10 seconds and pushes the current artist/song to an RDS transmitter over telnet. If the connection drops for any reason, the script waits 60 seconds and reconnects automatically. [Notes on this project are collected here.](https://docs.google.com/document/d/1epvOxYJUaPP5_70G_gQvbGMNcde6QCwgP7goqMtREHw/edit?tab=t.k7hrccdo67zm)

## Prerequisites

- macOS (iMac or Mac mini)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Network access to the RDS transmitter at a static IP

## Setup

**1. Clone or copy the project to the iMac**

```bash
git clone <repo-url> ~/codetools/rds_now_playing
cd ~/codetools/rds_now_playing
```

**2. Create a virtual environment and install dependencies**

```bash
uv venv
uv pip install -r requirements.txt
```

**3. Add your credentials**

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Then open `.env` and replace the placeholder values with the actual login and password for the RDS transmitter.

`RDS_PORT` is optional and defaults to **23**, the encoder's own telnet configuration
port. The old value of 5423 was only ever the port the router forwarded; to switch back,
set `RDS_PORT=5423` in `.env` — no code change needed.

## Running manually

```bash
uv run main.py
```

Press `Ctrl+C` to stop.

## What gets sent

The script reads `https://api.wxdu.org/api/nowplaying`, which returns `artist`, `song`,
`album` and `label` as separate JSON fields. If that API is unreachable it falls back to
the legacy XML feed (`ajaxnowplaying.php`) automatically and logs the switch once.

Text is run through three steps before it goes out:

1. **Entity decoding.** The legacy feed double-escapes — a literal `&` arrives as
   `&amp;amp;` — and also emits HTML entities XML doesn't define, like `&rsquo;`.
   Entities are decoded repeatedly until the text stops changing, so both come out as
   the character the DJ actually typed.
2. **ASCII folding** via `unidecode`, so `Björk` becomes `Bjork`.
3. **RDS character substitution.** RDS uses the G0 code table from IEC 62106, not ASCII,
   and a few punctuation marks sit at positions the standard assigns to another glyph:
   `^` renders as `―`, `` ` `` as `‖`, `~` as `¯` and `$` as `¤`. The first two are
   substituted; `$` is left for the encoder to translate, and `~` is passed through but
   no longer used by the script itself (see `TRUNCATION_MARK` in `main.py`).

RadioText is capped at 64 characters. When artist and song don't both fit, the shorter
one is kept whole and the longer one is trimmed with `TRUNCATION_MARK` (`>`) appended.
`>` is 0x3E in both ASCII and G0, so it reaches the receiver as the glyph we sent.

## RT+ (RadioText Plus)

RT+ tags a slice of the RadioText with a content type, so a car radio can show Artist and
Title as separate fields. Only two tags fit in one RT+ group, so we tag `ITEM.ARTIST` (4)
and `ITEM.TITLE` (1); album is available from the API but has no room alongside those.

The script keeps the RadioText in a stable `<artist> - <song>` shape and computes the tag
positions. Getting them on air needs the encoder configured once:

1. On the encoder's web UI, open **RDS / RT Plus** and tick **RT Plus Auto Generation**.
2. Assign RT+ a group — over telnet, `RT_PLUS=11` — and make sure that group is in the
   group sequence.
3. Confirm on an RT+ capable receiver.

With auto-generation on, the encoder derives the tags from the RadioText itself and the
script needs to send nothing extra. To send tag positions explicitly instead, the exact
command syntax has to come from the unit — the manual documents `RT_PLUS=<group>` but no
tagging command. Run `uv run probe_rtplus.py` to ask the encoder what it accepts, then set
`rtplus_template` and `rtplus_mode = "command"` in `main.py`.

`probe_rtplus.py` is read-only by default. `--write` additionally sends a test RadioText
containing the awkward characters and reads it back, which shows what the encoder keeps,
strips or substitutes before anything reaches a receiver.

## Running as a background service (launchd)

This installs the script as a **LaunchAgent** — it starts automatically when you log in and restarts itself if it ever crashes.

**1. Create the logs directory**

```bash
mkdir -p ~/bin/rds_now_playing/logs
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
| View live output | `tail -f ~/bin/rds_now_playing/logs/rds_out.log` |
| View errors | `tail -f ~/bin/rds_now_playing/logs/rds_err.log` |
| Stop | `launchctl unload ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist` |
| Start | `launchctl load ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist` |
