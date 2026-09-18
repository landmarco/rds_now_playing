"""Ask the RDS encoder what it actually supports, so we can stop guessing.

The AUDEMAT manual documents `RT_PLUS=<group>` to switch RT+ on, but no command for
sending the tags themselves — and it notes that the "session command port" (2000)
carries "only RT+ and dynamic PS commands", which implies such a command exists. This
script asks the unit directly and prints exactly what it says back.

It also settles the truncation-mark question: it sends a RadioText containing "~" and
reads the stored value back, which shows whether the encoder keeps the character,
strips it, or substitutes something else before it ever reaches a receiver.

    uv run probe_rtplus.py              # read-only: queries and HELP, changes nothing
    uv run probe_rtplus.py --write      # also sends test text, then restores what was there
    uv run probe_rtplus.py --port=5423  # try the old router mapping instead of 23

STOP THE SERVICE FIRST. The encoder allows a limited number of telnet sessions, and
the LaunchAgent holds one permanently — a second connection is then accepted by TCP
but never answered, which looks like a dead port:

    launchctl unload ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist
    ... run the probe ...
    launchctl load ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist

RadioText holds on the last song while the service is stopped; it does not go silent.

--write briefly changes the on-air RadioText (a few seconds), so run it at a quiet
moment. The original RadioText is read first and restored at the end.
"""

import socket
import sys
import time
from pathlib import Path

_env = dict(
    line.strip().split("=", 1)
    for line in Path(".env").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)
HOST = _env["RDS_IP"]
LOGIN = _env["RDS_LOGIN"]
PASSWORD = _env["RDS_PASSWORD"]
CONFIG_PORT = int(_env.get("RDS_PORT", "23"))   # accepts every command, prompts for login
for _arg in sys.argv[1:]:                       # --port 5423 to try the old mapping
    if _arg.startswith("--port="):
        CONFIG_PORT = int(_arg.split("=", 1)[1])
COMMAND_PORT = 2000                             # per the manual: RT+ and dynamic PS only

# Read-only queries. An unsupported name comes back as "UNKNOWN COMMAND", which is itself
# the answer we want — it tells us which spellings this firmware knows.
QUERIES = [
    "RDS.PI", "RDS.RT", "RT_PLUS", "RT", "RT_TEXT",
    "RDS.RADIOTEXT.TEXT", "RDS.RADIOTEXT.TOGGLE",
    "HELP", "?", "HELP RT", "HELP RT_PLUS", "VERSION", "IDENT",
]

# Candidate spellings for the tag command, in the two shapes Audemat gear has used:
# explicit markers (content type, start, length) and named fields the encoder tags itself.
# Values describe "Test Artist - Test Song": artist at 0 len 11, title at 14 len 9.
TAG_CANDIDATES = [
    "RT_PLUS_TAG=4,0,10,1,14,8",
    "RTPLUS=4,0,10,1,14,8",
    "RT_PLUS.TAG=4,0,10,1,14,8",
    "RDS.RTPLUS.TAG=4,0,10,1,14,8",
    "RDS.RT_PLUS.TAG=4,0,10,1,14,8",
    "RTP=4,0,10,1,14,8",
    "RT_PLUS_ITEM=4,0,10,1,14,8",
    "ARTISTNAME=Test Artist",
    "SONGTITLE=Test Song",
    "ALBUMNAME=Test Album",
]

PROBE_TEXT = "WXDU probe ~ > * . $ ^ ` end"


def read_for(sock, seconds, until=None):
    """Read for up to `seconds`, stopping early on `until`. Returns (text, closed)."""
    deadline = time.monotonic() + seconds
    chunks = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return b"".join(chunks).decode("utf-8", "replace"), False
        sock.settimeout(remaining)
        try:
            data = sock.recv(4096)
        except socket.timeout:
            return b"".join(chunks).decode("utf-8", "replace"), False
        except OSError:
            # Reset by the peer, or by something in between — same as a close here
            return b"".join(chunks).decode("utf-8", "replace"), True
        if not data:
            return b"".join(chunks).decode("utf-8", "replace"), True
        chunks.append(data)
        text = b"".join(chunks).decode("utf-8", "replace")
        if until and until in text.upper():
            return text, False


def send(sock, line, wait=1.0):
    sock.sendall((line + "\r\n").encode("utf-8"))
    return read_for(sock, wait)[0]


def show(label, response):
    body = response.strip() or "(no response)"
    print(f"  {label:<34} -> {body!r}")


def connect(port, login=True, patience=15.0):
    """Connect and log in, reporting exactly what the encoder does and doesn't say."""
    started = time.monotonic()
    sock = socket.create_connection((HOST, port), timeout=15)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    print(f"  TCP connect to {HOST}:{port} succeeded in {time.monotonic() - started:.1f}s")

    banner, closed = read_for(sock, patience, until="LOGIN")
    print(f"  banner after {time.monotonic() - started:.1f}s -> {banner.strip()!r}")
    if closed:
        print("  !! the encoder closed the connection immediately.")
        return sock, banner

    if not banner.strip():
        # Some units stay silent until they see a newline. Nudge it before giving up.
        print("  nothing yet — sending a bare newline to prompt it...")
        try:
            sock.sendall(b"\r\n")
            more, closed = read_for(sock, patience, until="LOGIN")
        except OSError as e:
            print(f"  connection dropped when nudged: {e}")
            more = ""
        print(f"  after nudge -> {more.strip()!r}")
        banner += more

    if not banner.strip():
        print()
        print("  !! Connected, but the encoder never sent anything. The usual cause is")
        print("     that it allows only ONE telnet session and the LaunchAgent already")
        print("     has it. Stop the service and run this again:")
        print("       launchctl unload ~/Library/LaunchAgents/org.wxdu.rds-now-playing.plist")
        print("     (RadioText freezes on the last song until you load it back; it does")
        print("      not go silent.) If it is still quiet with the service stopped, then")
        print("     something between here and the encoder is accepting the connection")
        print("     without passing it through — check the router's port-23 forward.")
        return sock, banner

    if login:
        # Send credentials whenever a prompt showed up at all, rather than insisting
        # on seeing it inside one short window.
        send(sock, LOGIN, wait=3.0)
        response = send(sock, PASSWORD, wait=3.0)
        print(f"  after login -> {response.strip()!r}")
        if "LOGGED" not in response.upper():
            print("  !! no LOGGED confirmation — check RDS_LOGIN / RDS_PASSWORD in .env")
        banner += response

    return sock, banner


def main():
    write_mode = "--write" in sys.argv
    print(f"Connecting to {HOST}:{CONFIG_PORT} (configuration port)\n")

    sock, banner = connect(CONFIG_PORT)
    if not banner.strip():
        print("\nStopping here — no point sending commands into silence.")
        sock.close()
        return

    print("\nRead-only queries:")
    for query in QUERIES:
        show(query, send(sock, query))

    print(f"\nCommand port {COMMAND_PORT} (manual says RT+ / dynamic PS only):")
    try:
        cmd_sock, _ = connect(COMMAND_PORT, login=False, patience=5.0)
        show("RT_PLUS", send(cmd_sock, "RT_PLUS"))
    except OSError as e:
        cmd_sock = None
        print(f"  not reachable: {e}")

    if not write_mode:
        print("\nRead-only pass done. Re-run with --write to test the tag commands")
        print("and the '~' round-trip (briefly changes the on-air RadioText).")
        sock.close()
        return

    original = send(sock, "RDS.RT").strip()
    print(f"\nCurrent RadioText, will be restored: {original!r}")

    print(f"\nCharacter round-trip — sending {PROBE_TEXT!r}:")
    send(sock, "RT_TEXT=" + PROBE_TEXT)
    time.sleep(1.0)
    readback = send(sock, "RDS.RT")
    show("RDS.RT readback", readback)
    print("  ^ compare against what was sent: characters the encoder refuses to")
    print("    carry are dropped or substituted here, before any receiver sees them.")

    print("\nTag command candidates (looking for anything that is not UNKNOWN COMMAND):")
    for candidate in TAG_CANDIDATES:
        show(candidate, send(sock, candidate))
        if cmd_sock:
            show(f"  [port {COMMAND_PORT}] {candidate.split('=')[0]}", send(cmd_sock, candidate))

    if original.startswith("RDS.RT="):
        send(sock, original.replace("RDS.RT=", "RT_TEXT=", 1))
        print(f"\nRestored: {original!r}")
    else:
        print("\nCouldn't parse the original RadioText to restore it — the running")
        print("service will overwrite it on its next poll anyway.")

    sock.close()
    if cmd_sock:
        cmd_sock.close()


if __name__ == "__main__":
    try:
        main()
    except OSError as e:
        print(f"\nCould not talk to the encoder at {HOST}:{CONFIG_PORT} — {e}")
        print("If this is a timeout, the port is filtered rather than closed; check the")
        print("router's forward. Try --port=5423 to see whether the old mapping answers.")
