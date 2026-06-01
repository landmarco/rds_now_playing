import telnetlib
import requests
import time
from unidecode import unidecode

### Parameters

update_time = 10 # sec
tn_host = "71.210.6.210"
# tn_port = 23
tn_port = 5423


### debug

# a = 'Frida Hyvönen - Jesus Was a Cross Maker'
# b = unidecode(a)
# print(a + '\n' + b)


### Get current playing from XML

link = "https://wxdu.org/plmanager/world/ajaxnowplaying.php"
f = requests.get(link)

text_xml = f.text
text_xml_l = len(text_xml)
text_rt = unidecode(text_xml[39:(text_xml_l - 20)])

print(f.text)
print(text_rt)


### Establish Telnet connection

tn = telnetlib.Telnet(tn_host, tn_port)
tn.set_debuglevel(1000)
tn.read_until(b"LOGIN:", timeout=30)
tn.write(("*******\n").encode('ascii'))
tn.read_until(b"PASSWORD:", timeout=30)
tn.write(("*******\n").encode('ascii'))


### Update RT

if text_rt == '':
    text_rt = 'A service of the Duke Union and a host of sweetie volunteers'

tn.write(("RT_TEXT=" + text_rt + "\n").encode('ascii'))
time.sleep(update_time)


### Run Update Loop

i = 0

while i == 0:

    f = requests.get(link)  # Get new artist/song info
    text_xml = f.text
    text_xml_l = len(text_xml)
    text_rt_new = unidecode(text_xml[39:(text_xml_l - 20)])

    if text_rt_new == '':
        text_rt_new = 'A service of the Duke Union and a host of sweetie volunteers'

    if text_rt_new != text_rt:  # Compare with old info, update if new
        text_rt = text_rt_new
        tn.write(("RT_TEXT=" + text_rt + "\n").encode('ascii'))
        time.sleep(30 - update_time)

    time.sleep(update_time) # Wait until next update check

