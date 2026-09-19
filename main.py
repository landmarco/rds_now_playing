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
# ODA (AID 4BD7) announced in group 3A.
#
# On this encoder the tags are not sent as positions. "RT Plus Auto Generation" is
# ticked on the RDS Settings / RT Plus page, and the encoder derives the tags from the
# RadioText itself — which is why build_rt() keeps the "<artist> - <song>" shape stable.
#
# Confirmed on air: the unit's own FM Tuner page, decoding the 88.7 broadcast, reports
# ODA group 11A carrying AID 4BD7, and its analyzer shows 11A at 12.6% of groups — the
# 12.5% the group sequence asks for. What that does NOT confirm is the content of the
# tags: the encoder announces the RT+ service and transmits the group, but nothing on
# the unit displays what the tags point at. Checking that needs an RT+ capable receiver.
# (Reset the analyzer before reading it — its figures are cumulative, so they carry
# whatever the group sequence used to be.)
#
# Each content type also has an editable label on that page, and sending "<label>=<value>"
# is accepted (a real label answers "+", a made-up one "!"). Those values never appeared
# in the page's value column, so they seem not to be what auto-generation uses, and their
# contribution to the working tags is unconfirmed — they are sent because they are cheap
# and may matter if auto-generation is ever turned off. Drop rtplus_fields to stop.
#
# The labels must match that page exactly: they are editable there, and renaming one
# without changing it here silently stops that field updating.
rtplus_fields = {
    "ARTISTNAME": "artist",
    "SONGTITLE": "song",
    "ALBUMNAME": "album",
}


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
    """Fetch the now-playing JSON API. Returns empty fields when nothing is on air."""
    r = session.get(link, timeout=15)
    if r.status_code == 404:      # off air — the API's documented empty response
        return {"artist": "", "song": "", "album": ""}
    r.raise_for_status()
    track = r.json()
    return {name: to_rds_text(track.get(name)) for name in ("artist", "song", "album")}


def fetch_legacy_xml(session):
    """Fetch the old XML feed, which serves artist and song as one pre-joined string."""
    f = session.get(legacy_link, timeout=15)
    f.raise_for_status()
    # Escape bare & not already part of a valid XML entity (e.g. & in artist names)
    xml = re.sub(r'&(?!(?:amp|lt|gt|apos|quot|#\d+|#x[0-9a-fA-F]+);)', '&amp;', f.text)
    text = to_rds_text(''.join(ET.fromstring(xml).itertext()))
    artist, _, song = text.partition(SEPARATOR)
    return {"artist": artist, "song": song, "album": ""}   # the old feed has no album


def get_now_playing(session):
    """Return (radiotext, fields) for whatever is playing right now.

    fields carries artist/song/album for the RT+ labels; it is empty when there is
    nothing to tag, so a stale artist can't stay tagged over the fallback text.
    """
    global _using_legacy
    try:
        track = fetch_json(session)
        if _using_legacy:
            print(f"[{ts()}] JSON API is back; using it again", flush=True)
            _using_legacy = False
    except Exception as e:
        if not _using_legacy:
            print(f"[{ts()}] JSON API unavailable ({e}); falling back to the XML feed", flush=True)
            _using_legacy = True
        track = fetch_legacy_xml(session)

    artist, song = track["artist"], track["song"]
    if not artist and not song:
        return fallback_text, {}
    if not artist or not song:
        # Only one field — nothing for the encoder to split on
        return (artist or song)[:RT_MAX], {}

    text = build_rt(artist, song)
    # Take the field values back out of the finished RadioText rather than using the
    # originals: the encoder tags by finding the value inside the RT, so a truncated
    # artist has to be sent in its truncated form or it is simply not found there.
    # (ALBUMNAME never appears in the RT, so the encoder has nothing to point a tag at —
    # it is sent to populate the field, not in the expectation of a tag.)
    track["artist"], _, track["song"] = text.partition(SEPARATOR)
    return text, track


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


def set_rt(tn, text, fields):
    """Send the RadioText, then the RT+ field values the encoder tags against.

    The RadioText goes first so the values are already findable in it. A field the feed
    left empty is cleared rather than skipped, so last song's album can't linger.
    """
    tn.write("RT_TEXT=" + text)
    for label, name in rtplus_fields.items():
        tn.write(f"{label}={fields.get(name, '')}")


### Main loop

def main():
    # Reuse the HTTP connection across polls instead of reconnecting every 10 seconds
    session = requests.Session()

    # Outer loop: reconnects everything from scratch after any fatal error (telnet failure, etc.)
    tn = None
    while True:
        try:
            ### Get initial now-playing and establish telnet connection

            text_rt, fields = get_now_playing(session)
            tn = connect_telnet()
            print(f"[{ts()}] Connected on port {tn_port}. Now playing: {text_rt}", flush=True)
            set_rt(tn, text_rt, fields)
            last_sent = time.monotonic()
            time.sleep(update_time)


            ### Update loop — runs continuously while the telnet connection is healthy

            while True:

                # Poll the now-playing endpoint; if HTTP fails, skip this cycle and retry next poll
                # without dropping the telnet connection (HTTP blips shouldn't force a reconnect)
                try:
                    text_rt_new, fields_new = get_now_playing(session)
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
                    text_rt, fields = text_rt_new, fields_new
                    # Telnet exceptions are intentionally NOT caught here — they propagate up
                    # to the outer except block, which reconnects the telnet connection
                    set_rt(tn, text_rt, fields)
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
