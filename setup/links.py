"""Print this computer's TV Remote Kit links (the helper must be running).

    python3 setup/links.py        (macOS / Linux)
    py setup\\links.py             (Windows)
"""
import json
import os
import sys
import urllib.request

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def read(name):
    try:
        with open(os.path.join(APP, name)) as f:
            return f.read().strip()
    except OSError:
        return None


try:
    with open(os.path.join(APP, "settings.json")) as f:
        port = int(json.load(f).get("builder_port", 8766))
except (OSError, ValueError):
    port = 8766

try:
    links = json.load(urllib.request.urlopen(f"http://localhost:{port}/phone-link", timeout=5))
    info = json.load(urllib.request.urlopen(f"http://localhost:{port}/info", timeout=10))
except OSError as e:
    print(f"The helper isn't answering on port {port} ({e}). Is it running?")
    sys.exit(1)

home = info.get("home") or ""
room = "1234"
ns = f"&h={home}" if home else ""
print()
print("=== TV Remote Kit: your links ===")
print(f"On this computer:            http://localhost:{port}/")
print(f"Phone, on your Wi-Fi:        {links.get('url')}")
print(f"Phone, from anywhere:        {links.get('remote')}")
print(f"  passcode for that link:    {read('.remote-pass')}")
print(f"TV, from anywhere (room {room}): {info.get('publicRelay')}#tv={room}{ns}")
if info.get("lanRelay"):
    print(f"TV, on your Wi-Fi (room {room}): {info.get('lanRelay')}#tv={room}{ns}")
print("Keep the links private: they let a device start shows on this computer.")
