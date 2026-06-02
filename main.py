import telnetlib
import requests
import time
import os
from pathlib import Path
from unidecode import unidecode

### Credentials

_env = dict(
    line.strip().split("=", 1)
    for line in Path(".env").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)
RDS_LOGIN = _env["RDS_LOGIN"]
RDS_PASSWORD = _env["RDS_PASSWORD"]


### Parameters

update_time = 10  # sec
tn_host = "71.210.6.210"
tn_port = 5423
link = "https://wxdu.org/plmanager/world/ajaxnowplaying.php"
fallback_text = 'A service of the Duke Union and a host of sweetie volunteers'
retry_delay = 60  # seconds to wait after any failure before restarting


def main():
    while True:
        try:
            ### Get current playing from XML

            f = requests.get(link, timeout=15)
            text_xml = f.text
            text_rt = unidecode(text_xml[39:(len(text_xml) - 20)])

            print(f.text)
            print(text_rt)


            ### Establish Telnet connection

            tn = telnetlib.Telnet(tn_host, tn_port)
            tn.set_debuglevel(1000)
            tn.read_until(b"LOGIN:", timeout=30)
            tn.write((RDS_LOGIN + "\n").encode('ascii'))
            tn.read_until(b"PASSWORD:", timeout=30)
            tn.write((RDS_PASSWORD + "\n").encode('ascii'))


            ### Update RT

            if text_rt == '':
                text_rt = fallback_text

            tn.write(("RT_TEXT=" + text_rt + "\n").encode('ascii'))
            time.sleep(update_time)


            ### Run Update Loop

            while True:

                f = requests.get(link, timeout=15)  # Get new artist/song info
                text_xml = f.text
                text_rt_new = unidecode(text_xml[39:(len(text_xml) - 20)])

                if text_rt_new == '':
                    text_rt_new = fallback_text

                if text_rt_new != text_rt:  # Compare with old info, update if new
                    text_rt = text_rt_new
                    tn.write(("RT_TEXT=" + text_rt + "\n").encode('ascii'))
                    time.sleep(30 - update_time)

                time.sleep(update_time)  # Wait until next update check

        except Exception as e:
            print(f"Error: {e}. Restarting in {retry_delay}s...")
            time.sleep(retry_delay)


if __name__ == "__main__":
    main()
