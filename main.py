import telnetlib
import requests
import socket
import time
import xml.etree.ElementTree as ET
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


### Parameters

update_time = 10         # seconds between now-playing polls
tn_host = "71.210.6.210" # IP address of the RDS encoder
tn_port = 5423           # telnet port on the RDS encoder
link = "https://wxdu.org/plmanager/world/ajaxnowplaying.php"  # now-playing XML endpoint
fallback_text = 'A service of the Duke Union and a host of sweetie volunteers'
retry_delay = 60         # seconds to wait after a fatal error before restarting
keepalive_interval = 120 # seconds between forced RT_TEXT resends even if track hasn't changed


### Helper functions

def get_now_playing(session):
    """Fetch and parse the now-playing XML endpoint, return ASCII-safe artist/track string."""
    f = session.get(link, timeout=15)
    root = ET.fromstring(f.text)
    text = ''.join(root.itertext()).strip()  # itertext() handles any XML structure
    return unidecode(text) or fallback_text  # unidecode converts accented chars to ASCII


def connect_telnet():
    """Open a telnet connection to the RDS encoder, log in, and return the connection."""
    tn = telnetlib.Telnet(tn_host, tn_port, timeout=30)
    tn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)  # detect dead connections at OS level
    tn.read_until(b"LOGIN:", timeout=30)
    tn.write((RDS_LOGIN + "\n").encode('ascii'))
    tn.read_until(b"PASSWORD:", timeout=30)
    tn.write((RDS_PASSWORD + "\n").encode('ascii'))
    return tn


def set_rt(tn, text):
    """Send an RT_TEXT update command to the RDS encoder over the telnet connection."""
    tn.write(("RT_TEXT=" + text + "\n").encode('ascii'))


### Main loop

def main():
    # Reuse the HTTP connection across polls instead of reconnecting every 10 seconds
    session = requests.Session()

    # Outer loop: reconnects everything from scratch after any fatal error (telnet failure, etc.)
    while True:
        try:
            ### Get initial now-playing and establish telnet connection

            text_rt = get_now_playing(session)
            print(text_rt)

            tn = connect_telnet()
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
                    print(f"HTTP error: {e}. Retrying next poll...")
                    time.sleep(update_time)
                    continue

                now = time.monotonic()
                track_changed = text_rt_new != text_rt
                # Periodically resend RT_TEXT even if the track hasn't changed — this acts as a
                # heartbeat to keep the telnet connection alive on the network
                keepalive_due = (now - last_sent) >= keepalive_interval

                if track_changed or keepalive_due:
                    text_rt = text_rt_new
                    set_rt(tn, text_rt)
                    last_sent = now
                    if track_changed:
                        # After a track change, wait longer before the next poll — no point
                        # checking again immediately when a new song just started
                        time.sleep(30 - update_time)

                time.sleep(update_time)  # Wait before next poll

        except Exception as e:
            print(f"Error: {e}. Restarting in {retry_delay}s...")
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
