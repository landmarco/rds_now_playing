import html
import re
import requests
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from unidecode import unidecode


### Credentials — loaded from .env file, never hardcoded

_env = dict(
    line.strip().split("=", 1)
    for line in Path(".env").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)
RDS_LOGIN = _env["RDS_LOGIN"]
RDS_PASSWORD = _env["RDS_PASSWORD"]
RDS_IP = _env["RDS_IP"]

### Parameters

update_time = 10         # seconds between now-playing polls
tn_host = RDS_IP         # IP address of the RDS encoder
# Telnet port on the RDS encoder. 23 is the encoder's own "session configuration
# port" — the one that prompts LOGIN:/PASSWORD: and accepts every command. We used
# to reach it on 5423 only because the router forwarded that port; to go back, put
# RDS_PORT=5423 in .env. No code change needed either way.
tn_port = int(_env.get("RDS_PORT", "23"))

# Now-playing source. The JSON API returns artist/song/album as separate, correctly
# encoded fields — both of which we need. The legacy XML feed is kept as an automatic
# fallback so a bad deploy on the API side can't take the RDS text off the air.
link = "https://api.wxdu.org/api/nowplaying"
legacy_link = "https://wxdu.org/plmanager/world/ajaxnowplaying.php"

fallback_text = 'A service of the Duke Union and a host of sweetie volunteers'
retry_delay = 60         # seconds to wait after a fatal error before restarting
keepalive_interval = 120 # seconds between forced RT_TEXT resends even if track hasn't changed


### RDS text encoding
#
# RDS does not use ASCII. It uses the "basic code table" (G0) from IEC 62106 Table E.1,
# and a handful of ASCII punctuation marks sit at code points that table assigns to a
# different glyph, so the receiver draws something else:
#
#     0x5E  ^  ->  ―      0x60  `  ->  ‖      0x7E  ~  ->  ¯      0x24  $  ->  ¤
#
# G0 is fixed by the standard and baked into receiver font ROMs — there is no way to
# redefine a position, and the G1/G2 code tables the standard also defines contain no
# tilde either (and most receivers ignore code-table switching anyway). So the only
# lever we have is choosing characters that land on the same glyph in both tables.
#
# Deliberately left alone:
#   $  the RDS dollar sign lives at 0xAB, and translating to it is the encoder's job;
#      remapping here would translate it twice (see NRSC-G300-C §9.2).
#   ~  left as-is rather than substituted, so a tilde a DJ actually typed in a title is
#      passed through to the encoder untouched; we simply don't use it ourselves.
RDS_SUBSTITUTIONS = str.maketrans({"^": "", "`": "'"})

# Appended to artist or song when it had to be shortened to fit RadioText.
#
# ">" is 0x3E, the one code point where ASCII and G0 agree on this glyph, so it survives
# the trip intact. It replaces "~" (0x7E), which G0 maps to "¯" (macron) rather than a
# tilde — receivers drew it as a thin overline or as nothing at all, which is why the mark
# looked like it was never being sent. "*" (0x2A) and "." (0x2E) are equally safe swaps.
TRUNCATION_MARK = ">"

RT_MAX = 64              # RadioText is 64 characters, hard limit
SEPARATOR = " - "        # between artist and song; keep stable, the encoder tags on it

# RT+ content type class codes, from the RT+ specification (also listed in the encoder
# manual §4.4). Only two tags fit in one RT+ group, so artist + title is the usable pair;
# ITEM.ALBUM is defined here for reference but would have to alternate with one of them.
RTP_ITEM_TITLE = 1
RTP_ITEM_ALBUM = 2
RTP_ITEM_ARTIST = 4

### RT+ (RadioText Plus)
#
# RT+ tags a slice of the RadioText with a content type so a receiver can show "Artist"
# and "Title" as separate fields instead of one run-on string. The tags ride in an RDS
# ODA (AID 4BD7) announced in group 3A. There are two ways to get them out of this
# encoder (AUDEMAT RDS Encoder, software 1.x):
#
#   "auto"    — tick "RT Plus Auto Generation" on the RDS/RT Plus page of the encoder's
#               web UI and assign it a group with the `RT_PLUS=11` command. The encoder
#               derives the artist/title tags from the RadioText we already send, so the
#               script's only job is to keep the "<artist> - <song>" shape stable, which
#               build_rt() now guarantees. This needs no undocumented commands, so it is
#               the default.
#
#   "command" — send the tag positions ourselves. The manual documents `RT_PLUS=<group>`
#               to enable the feature but no command for the tags themselves, so the
#               syntax has to come from the unit: run probe_rtplus.py, put what it
#               accepts in rtplus_template, and switch rtplus_mode to "command".
#
# Template fields available: {t1_type} {t1_start} {t1_len} {t2_type} {t2_start} {t2_len}
rtplus_mode = "auto"
rtplus_template = None


### Helper functions

def ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


_ENTITY = re.compile(r'&(#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]{1,31});')


def decode_entities(text):
    """Decode HTML/XML character entities, including double-escaped ones.

    The legacy XML feed double-escapes: a literal "&" arrives as "&amp;amp;", which
    ElementTree decodes exactly once, leaving "&amp;" to go out over the air. It also
    emits HTML entities that XML does not define ("&rsquo;", "&hellip;"), which the
    escaping in fetch_legacy_xml() turns into "&amp;rsquo;" and which then survive the
    parse the same way. Decoding repeatedly until the text stops changing handles both,
    at any depth. Only well-formed "&name;" / "&#nn;" forms are touched, so a bare "&"
    in "Simon & Garfunkel", or the "&" in "R&B", is left exactly as it is.
    """
    for _ in range(4):
        decoded = _ENTITY.sub(lambda m: html.unescape(m.group(0)), text)
        if decoded == text:
            break
        text = decoded
    return text


def to_rds_text(text):
    """Decode entities, fold to ASCII, then drop the ASCII characters RDS renders wrong."""
    return unidecode(decode_entities(text or "")).translate(RDS_SUBSTITUTIONS).strip()


def _shorten(value, limit):
    """Trim value to limit characters, spending the last of them on the truncation mark."""
    if len(value) <= limit:
        return value
    # rstrip so the mark doesn't end up floating after a space when the cut lands
    # between two words ("That Goes >" reads worse than "That Goes>")
    return value[:limit - len(TRUNCATION_MARK)].rstrip() + TRUNCATION_MARK


def build_rt(artist, song):
    """Compose the RadioText line and its RT+ tags from a separate artist and song.

    Returns (text, tags), where tags maps an RT+ content type to (start, length) with
    start a 0-based character offset into text — the form RT+ start/length markers take.
    """
    budget = RT_MAX - len(SEPARATOR)   # characters left for artist and song together
    half = budget // 2

    if len(artist) + len(song) > budget:
        # Give the short field what it needs and the long field the rest, so a long
        # artist with a short song (or the reverse) is shortened as little as possible.
        if len(song) <= half:
            artist_limit, song_limit = budget - len(song), len(song)
        elif len(artist) <= half:
            artist_limit, song_limit = len(artist), budget - len(artist)
        else:
            artist_limit, song_limit = half, budget - half
        artist = _shorten(artist, artist_limit)
        song = _shorten(song, song_limit)

    text = artist + SEPARATOR + song
    tags = {
        RTP_ITEM_ARTIST: (0, len(artist)),
        RTP_ITEM_TITLE: (len(artist) + len(SEPARATOR), len(song)),
    }
    return text, tags


def rtplus_command_lines(tags):
    """Render the encoder command(s) carrying the RT+ tags, for rtplus_mode == 'command'.

    The length marker is the length *in addition to* the first character, so it is
    len - 1. Tag 1 has 6 bits for it and tag 2 only 5, so the longer field goes in
    tag 1 and tag 2 is clamped to the 32 characters it can actually describe.
    """
    if rtplus_mode != "command" or not rtplus_template or not tags:
        return []

    first, second = sorted(tags.items(), key=lambda item: item[1][1], reverse=True)
    (t1_type, (t1_start, t1_length)), (t2_type, (t2_start, t2_length)) = first, second
    return [rtplus_template.format(
        t1_type=t1_type, t1_start=t1_start, t1_len=min(t1_length, 64) - 1,
        t2_type=t2_type, t2_start=t2_start, t2_len=min(t2_length, 32) - 1,
    )]


_using_legacy = False


def fetch_json(session):
    """Fetch the now-playing JSON API. Returns ('', '') when nothing is on air."""
    r = session.get(link, timeout=15)
    if r.status_code == 404:      # off air — the API's documented empty response
        return "", ""
    r.raise_for_status()
    track = r.json()
    return to_rds_text(track.get("artist")), to_rds_text(track.get("song"))


def fetch_legacy_xml(session):
    """Fetch the old XML feed, which serves artist and song as one pre-joined string."""
    f = session.get(legacy_link, timeout=15)
    f.raise_for_status()
    # Escape bare & not already part of a valid XML entity (e.g. & in artist names)
    xml = re.sub(r'&(?!(?:amp|lt|gt|apos|quot|#\d+|#x[0-9a-fA-F]+);)', '&amp;', f.text)
    text = to_rds_text(''.join(ET.fromstring(xml).itertext()))
    artist, _, song = text.partition(SEPARATOR)
    return artist, song


def get_now_playing(session):
    """Return (radiotext, rt+ tags) for whatever is playing right now."""
    global _using_legacy
    try:
        artist, song = fetch_json(session)
        if _using_legacy:
            print(f"[{ts()}] JSON API is back; using it again", flush=True)
            _using_legacy = False
    except Exception as e:
        if not _using_legacy:
            print(f"[{ts()}] JSON API unavailable ({e}); falling back to the XML feed", flush=True)
            _using_legacy = True
        artist, song = fetch_legacy_xml(session)

    if not artist and not song:
        return fallback_text, {}
    if not artist or not song:
        # Only one field — nothing to tag, and nothing to split on
        return (artist or song)[:RT_MAX], {}
    return build_rt(artist, song)


### Telnet connection
#
# Python dropped telnetlib in 3.13, so this is the small part of it we actually used:
# read until a prompt, write a line, and turn down any option negotiation the encoder
# opens with. The encoder's dialog is plain line-based text, so nothing more is needed.

IAC = 255                            # "interpret as command" — introduces a telnet option
DONT, DO, WONT, WILL = 254, 253, 252, 251


class EncoderConnection:
    """A line-oriented telnet client for the RDS encoder."""

    def __init__(self, host, port, timeout=30):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)  # detect dead connections at OS level
        self.buffer = b""

    def _refuse_options(self, data):
        """Answer every telnet option with a refusal and drop it from the text stream.

        This is what telnetlib did when given no option callback. The encoder most
        likely never negotiates at all, but if it does, the option bytes must not be
        left in the buffer where they'd break the search for the login prompt.
        """
        text = bytearray()
        i = 0
        while i < len(data):
            if data[i] != IAC:
                text.append(data[i])
                i += 1
            elif i + 2 < len(data) and data[i + 1] in (DO, DONT, WILL, WONT):
                verb, option = data[i + 1], data[i + 2]
                self.sock.sendall(bytes((IAC, WONT if verb in (DO, DONT) else DONT, option)))
                i += 3
            else:
                i += 2   # an escaped 0xFF, or a two-byte command we have no use for
        return bytes(text)

    def read_until(self, marker, timeout=30):
        """Read until marker turns up, or the timeout runs out. Returns what was read."""
        deadline = time.monotonic() + timeout
        while marker not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.sock.settimeout(remaining)
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                break
            if not data:
                break                # encoder closed the connection
            self.buffer += self._refuse_options(data)

        found = self.buffer.find(marker)
        if found == -1:
            read, self.buffer = self.buffer, b""
        else:
            end = found + len(marker)
            read, self.buffer = self.buffer[:end], self.buffer[end:]
        return read

    def write(self, line):
        """Send one command line. Raises on a dead connection, which triggers a reconnect."""
        self.sock.sendall(line.encode('ascii', 'replace') + b"\n")

    def close(self):
        self.sock.close()


def connect_telnet():
    """Open a connection to the RDS encoder, log in, and return the connection."""
    tn = EncoderConnection(tn_host, tn_port, timeout=30)
    tn.read_until(b"LOGIN:", timeout=30)
    tn.write(RDS_LOGIN)
    tn.read_until(b"PASSWORD:", timeout=30)
    tn.write(RDS_PASSWORD)
    return tn


def set_rt(tn, text, tags):
    """Send an RT_TEXT update command to the RDS encoder over the telnet connection."""
    tn.write("RT_TEXT=" + text)
    for line in rtplus_command_lines(tags):
        tn.write(line)


### Main loop

def main():
    # Reuse the HTTP connection across polls instead of reconnecting every 10 seconds
    session = requests.Session()

    # Outer loop: reconnects everything from scratch after any fatal error (telnet failure, etc.)
    tn = None
    while True:
        try:
            ### Get initial now-playing and establish telnet connection

            text_rt, tags = get_now_playing(session)
            tn = connect_telnet()
            print(f"[{ts()}] Connected on port {tn_port}. Now playing: {text_rt}", flush=True)
            set_rt(tn, text_rt, tags)
            last_sent = time.monotonic()
            time.sleep(update_time)


            ### Update loop — runs continuously while the telnet connection is healthy

            while True:

                # Poll the now-playing endpoint; if HTTP fails, skip this cycle and retry next poll
                # without dropping the telnet connection (HTTP blips shouldn't force a reconnect)
                try:
                    text_rt_new, tags_new = get_now_playing(session)
                except Exception as e:
                    print(f"[{ts()}] HTTP error: {e}. Retrying next poll...", flush=True)
                    time.sleep(update_time)
                    continue

                now = time.monotonic()
                track_changed = text_rt_new != text_rt
                # Periodically resend RT_TEXT even if the track hasn't changed — this acts as a
                # heartbeat to keep the telnet connection alive on the network
                keepalive_due = (now - last_sent) >= keepalive_interval

                if track_changed or keepalive_due:
                    text_rt, tags = text_rt_new, tags_new
                    # Telnet exceptions are intentionally NOT caught here — they propagate up
                    # to the outer except block, which reconnects the telnet connection
                    set_rt(tn, text_rt, tags)
                    last_sent = now
                    if track_changed:
                        print(f"[{ts()}] Now playing: {text_rt}", flush=True)
                        # After a track change, wait longer before the next poll — no point
                        # checking again immediately when a new song just started
                        time.sleep(30 - update_time)

                time.sleep(update_time)  # Wait before next poll

        except Exception as e:
            print(f"[{ts()}] Error: {e}. Restarting in {retry_delay}s...", flush=True)
            if tn is not None:
                # Let go of the old socket before connect_telnet() opens a new one,
                # so months of reconnects don't leak a file descriptor each time
                try:
                    tn.close()
                except OSError:
                    pass
                tn = None
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
