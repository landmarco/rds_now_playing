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
# Measured on the unit, by sending "WXDU probe ~ > * . $ ^ ` end" and reading RDS.RT
# back (probe_rtplus.py --write): it answered "WXDU probe   > * . $     end". So this
# encoder silently DROPS ~ ^ and ` — they never reach the air at all — while > * . and
# $ pass through. That is one step earlier than a receiver drawing the wrong glyph.
#
# Deliberately left alone:
#   $  survives the encoder, which translates it to 0xAB (where RDS keeps the dollar
#      sign) itself; remapping here would translate it twice — see NRSC-G300-C §9.2.
#   ~  dropped by the encoder rather than mangled, so a tilde a DJ typed in a title
#      just vanishes. Harmless, and not worth guessing a replacement for.
RDS_SUBSTITUTIONS = str.maketrans({"^": "", "`": "'"})

# Appended to artist or song when it had to be shortened to fit RadioText.
#
# ">" is 0x3E in both ASCII and G0, and the round-trip above confirms the encoder keeps
# it. It replaces "~", which the encoder drops outright — that is why the mark never
# appeared on air, however long the artist or title was. "*" and "." also survived the
# round-trip and are equally safe swaps.
TRUNCATION_MARK = ">"

RT_MAX = 64              # RadioText is 64 characters, hard limit
SEPARATOR = " - "        # between artist and song; keep stable, the encoder tags on it

### RT+ (RadioText Plus)
#
# RT+ tags a slice of the RadioText with a content type, so a receiver can show "Artist"
# and "Title" as separate fields instead of one run-on string. The tags ride in an RDS
# ODA (AID 4BD7) announced in group 3A; the content type codes are ITEM.TITLE=1,
# ITEM.ALBUM=2, ITEM.ARTIST=4. Only two tags fit in one group, which is why artist and
# title are the pair worth sending and album has nowhere to go.
#
# The encoder's own HELP output settles how to drive it: there is no ASCII command for
# the tags themselves. The complete RT+ surface is two commands —
#
#     RT_PLUS_AUTO=1      have the encoder derive the tags from the RadioText
#     RT_PLUS=<group>     which RDS group carries them (0 removes it)
#
# — both set once on the unit, where they persist. So the script's entire contribution
# to RT+ is keeping the RadioText in a shape the encoder can split: "<artist> - <song>",
# with SEPARATOR appearing exactly once, which build_rt() guarantees. Sending the tag
# positions ourselves would mean speaking UECP instead of this ASCII console.


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
    """Compose the RadioText line from a separate artist and song.

    Both fields are kept whole where they fit, because the encoder's RT+ auto-generation
    splits this string on SEPARATOR to work out where the artist ends and the title
    begins — so the separator has to survive truncation and mark the real boundary.
    """
    # A song may contain " - " harmlessly, since the split takes the first one; an artist
    # containing it would move the boundary, so that copy is disguised ("Emerson, Lake
    # - Palmer" would otherwise be tagged as the artist "Emerson, Lake").
    artist = artist.replace(SEPARATOR, " / ")

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

    return artist + SEPARATOR + song


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
    """Return the RadioText line for whatever is playing right now."""
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
        return fallback_text
    if not artist or not song:
        # Only one field — nothing for the encoder to split on
        return (artist or song)[:RT_MAX]
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


def set_rt(tn, text):
    """Send an RT_TEXT update command to the RDS encoder over the telnet connection."""
    tn.write("RT_TEXT=" + text)


### Main loop

def main():
    # Reuse the HTTP connection across polls instead of reconnecting every 10 seconds
    session = requests.Session()

    # Outer loop: reconnects everything from scratch after any fatal error (telnet failure, etc.)
    tn = None
    while True:
        try:
            ### Get initial now-playing and establish telnet connection

            text_rt = get_now_playing(session)
            tn = connect_telnet()
            print(f"[{ts()}] Connected on port {tn_port}. Now playing: {text_rt}", flush=True)
            set_rt(tn, text_rt)
            last_sent = time.monotonic()
            time.sleep(update_time)


            ### Update loop — runs continuously while the telnet connection is healthy

            while True:

                # Poll the now-playing endpoint; if HTTP fails, skip this cycle and retry next poll
                # without dropping the telnet connection (HTTP blips shouldn't force a reconnect)
                try:
                    text_rt_new = get_now_playing(session)
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
                    text_rt = text_rt_new
                    # Telnet exceptions are intentionally NOT caught here — they propagate up
                    # to the outer except block, which reconnects the telnet connection
                    set_rt(tn, text_rt)
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
