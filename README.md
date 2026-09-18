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
Title as separate fields instead of one run-on string. The tags ride in an RDS ODA
(AID `4BD7`) announced in group 3A. Only two tags fit in one group, so `ITEM.ARTIST` (4)
and `ITEM.TITLE` (1) are the pair worth sending; album is available from the API but has
nowhere to go alongside those.

The encoder's `HELP` output shows it has **no ASCII command for the tags themselves** —
the entire RT+ surface is two settings, and both persist on the unit:

```
RT_PLUS_AUTO=1      encoder derives the tags from the RadioText it is given
RT_PLUS=<group>     which RDS group carries them (0 removes it)
```

So the script's whole contribution is keeping the RadioText in a shape the encoder can
split — `<artist> - <song>`, with the separator marking the real boundary. `build_rt()`
guarantees that: the separator survives truncation, and a separator inside an artist name
is disguised so it can't move the split (`Emerson, Lake - Palmer` becomes
`Emerson, Lake / Palmer`). A separator inside a *song* title is harmless, since the split
takes the first one.

Enabling it — stop the service first, then:

```bash
uv run probe_rtplus.py --enable-rtplus
```

That sends `RT_PLUS_AUTO=1` and `RT_PLUS=22`, reads the state back, and checks the group
sequence, telling you the exact `RDS.GS=` line to run if something is missing. A group that
isn't in the sequence is never transmitted — the quiet way for this to fail.

Two groups have to be in `RDS.GS`: the tag group itself (11A), and **3A**, which announces
the RT+ AID so a receiver knows to look there at all. Note that `RT_PLUS` takes a group
*index* while `RDS.GS` speaks group *names* — `RT_PLUS=22` and `RDS.GS=...,11A` are the
same group.

Adding groups dilutes 0A, which carries PI/PS/AF and wants a high repetition rate, so
prefer interleaving over appending:

```
RDS.GS=0A,2A,0A,3A,0A,2A,0A,11A
```

That holds 0A at the same share it had as `0A,2A` and halves the RadioText rate, which
still leaves RT updating far faster than songs change.

`RT_PLUS` takes a group *index*, not a group name: the encoder accepts `3, 7, 9-19, 21-27`,
which map as `index = 2 × type + (0 for A, 1 for B)`. So 11A — the conventional RT+ group —
is `22`, and 12A is `24`. Pass `--enable-rtplus=24` to choose a different one.

Any other command from the `HELP` list can be sent the same way, repeatably:

```bash
uv run probe_rtplus.py --cmd="RDS.GS" --cmd="RT_PLUS"
```

Finally, confirm on an RT+ capable receiver.

Receivers find RT+ by following the AID announcement in 3A rather than by looking at a
fixed group, so the exact choice matters less than it being in the group sequence.

Sending tag positions explicitly instead would mean speaking UECP rather than this ASCII
console — a much larger change, and unnecessary while auto-generation works.

## The probe script

`probe_rtplus.py` asks the encoder what it supports and prints the answers. Read-only by
default: full `HELP` command list, current RT+ state, group sequence. With `--write` it
also sends a RadioText containing the awkward characters and reads the stored value back,
showing what the encoder keeps, strips or substitutes.

**Stop the service first** — the encoder allows a limited number of telnet sessions and
the LaunchAgent holds one permanently. A second connection is then accepted by TCP but
never answered, which looks exactly like a dead port:

```bash
launchctl unload ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist
uv run probe_rtplus.py
launchctl load ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist
```

RadioText holds on the last song while the service is stopped; it does not go silent.

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
