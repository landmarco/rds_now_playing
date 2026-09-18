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


def drain(sock, wait=0.6):
    """Collect whatever the encoder has to say, until it goes quiet."""
    sock.settimeout(wait)
    chunks = []
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
    except socket.timeout:
        pass
    return b"".join(chunks).decode("utf-8", "replace")


def send(sock, line, wait=0.6):
    sock.sendall((line + "\r\n").encode("utf-8"))
    return drain(sock, wait)


def show(label, response):
    body = response.strip() or "(no response)"
    print(f"  {label:<34} -> {body!r}")


def connect(port, login=True):
    sock = socket.create_connection((HOST, port), timeout=15)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    banner = drain(sock, 2.0)
    if login and "LOGIN" in banner.upper():
        send(sock, LOGIN)
        send(sock, PASSWORD)
        banner += drain(sock, 1.0)
    return sock, banner


def main():
    write_mode = "--write" in sys.argv
    print(f"Connecting to {HOST}:{CONFIG_PORT} (configuration port)\n")

    sock, banner = connect(CONFIG_PORT)
    print(f"  banner -> {banner.strip()!r}\n")

    print("Read-only queries:")
    for query in QUERIES:
        show(query, send(sock, query))

    print(f"\nCommand port {COMMAND_PORT} (manual says RT+ / dynamic PS only):")
    try:
        cmd_sock, cmd_banner = connect(COMMAND_PORT)
        print(f"  banner -> {cmd_banner.strip()!r}")
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
    main()
