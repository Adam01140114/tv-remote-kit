"""The helper: serves the builder page and starts search.py when you tap Run.

Usage:
    python3 builder-server.py              # then it opens http://localhost:8766/
    python3 builder-server.py --no-open    # without opening the browser

This computer can always use it. Your phone can too, on the same Wi-Fi, through the phone link
(the builder's "Open on my phone" button shows it as a QR code), which carries a code the phone
then remembers; anything without it is turned away. From anywhere, the remote page (on the
public relay) reaches this computer through the remote agent, with the remote link's key and
the passcode. It also serves Screen Relay (the relay folder next to this one) so a TV on the
same Wi-Fi can load it, and keeps the computer awake while it runs.

Ports and the public relay's address can be changed in settings.json next to this file.
"""
import hashlib
import psutil
import hmac
import secrets
import threading
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
def load_settings():
    """settings.json next to this file (optional): ports, the public relay's address."""
    try:
        with open(os.path.join(HERE, "settings.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


SETTINGS = load_settings()
PORT = int(SETTINGS.get("builder_port", 8766))
RELAY_PORT = int(SETTINGS.get("relay_port", 8765))
# The TV page and the remote page, served from this project's own GitHub Pages site (works from
# any network). A self-hosted copy can be set with "public_relay" in settings.json.
PUBLIC_RELAY = SETTINGS.get("public_relay") or "https://adam01140114.github.io/tv-remote-kit/relay/"
if not PUBLIC_RELAY.endswith("/"):
    PUBLIC_RELAY += "/"
RELAY_DIR = next((d for d in (os.path.join(os.path.dirname(HERE), "relay"),
                              os.path.join(os.path.dirname(HERE), "screenshare")) if os.path.isdir(d)),
                 os.path.join(os.path.dirname(HERE), "relay"))  # the Screen Relay folder next to this one
SERVED = {"/command-builder.html", "/logs.js", "/logs.json", "/agent.html"}  # nothing else here is shared (e.g. chrome-profile)
NOW_DIR = os.path.join(HERE, "nowplaying")  # search.py: <room>.json, the TV show playing in that room
PICKS_DIR = os.path.join(HERE, "picks")  # search.py --pick: <pid>.choices.json waiting, <pid>.choice.json picked
REMOTE_KEY_FILE = os.path.join(HERE, ".remote-key")    # the long random key in the remote link
REMOTE_PASS_FILE = os.path.join(HERE, ".remote-pass")  # the passcode the remote page asks for
def find_chrome():
    """Google Chrome's program, on macOS, Windows or Linux."""
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), r"Google\Chrome\Application\chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ]
    return next((c for c in candidates if c and os.path.exists(c)), candidates[0])


CHROME = find_chrome()
DETACHED = ({"creationflags": 0x00000008 | 0x00000200} if os.name == "nt"  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            else {"start_new_session": True})
HOME_FILE = os.path.join(HERE, ".home-id")  # this install's private namespace for room codes
SITE_CONFIG_FILE = os.path.join(HERE, "site-config.json")  # the pasted-in site settings (see /config)
AGENT_PROFILE = os.path.join(HERE, "agent-profile")  # the hidden agent Chrome's own profile
KEY_FILE = os.path.join(HERE, ".builder-key")  # the phone link's secret key, made once
COOKIE = "builder_key"

runs = {}  # room code (None when not sharing) -> the search.py process started for it


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def lan_ip():
    """This computer's address on the Wi-Fi, which the TV uses to reach it (no traffic is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return None


def ensure_relay():
    """Start Screen Relay on every network interface if nothing is on its port yet."""
    if port_open(RELAY_PORT) or not os.path.isdir(RELAY_DIR):
        return port_open(RELAY_PORT)
    subprocess.Popen([sys.executable, "-m", "http.server", str(RELAY_PORT), "--directory", RELAY_DIR],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **DETACHED)
    for _ in range(20):
        if port_open(RELAY_PORT):
            return True
        time.sleep(0.1)
    return False


KEY_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1: easy to read and type
KEY_LEN = 6
MAX_MISSES = 10  # wrong codes from one device before it's locked out
LOCKOUT = 15 * 60
misses = {}  # device address -> (wrong tries, time of the last one)


def load_key():
    """The phone link's code: made once and kept, so the link never changes.

    To pick your own, put it in .builder-key (4+ letters or digits) and restart.
    """
    try:
        with open(KEY_FILE) as f:
            key = f.read().strip()
        if 4 <= len(key) <= 32 and key.isalnum():  # or one you chose, e.g. 1234
            return key.upper()
    except OSError:
        pass
    key = "".join(secrets.choice(KEY_CHARS) for _ in range(KEY_LEN))
    with open(os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(key)
    return key


KEY = load_key()


def read_or_make(path, make):
    try:
        with open(path) as f:
            value = f.read().strip()
        if value:
            return value
    except OSError:
        pass
    value = make()
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write(value)
    return value


HOME_ID = read_or_make(HOME_FILE, lambda: "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8)))
REMOTE_KEY = read_or_make(REMOTE_KEY_FILE, lambda: "".join(secrets.choice(KEY_CHARS) for _ in range(24)))
REMOTE_PASS = read_or_make(REMOTE_PASS_FILE, lambda: "".join(secrets.choice("0123456789") for _ in range(6)))


def agent_peer_id():
    """Where the agent waits: made from the remote key and passcode (remote.html does the same)."""
    return "screenrelay-remote-" + hashlib.sha256(f"{REMOTE_KEY}:{REMOTE_PASS}".encode()).hexdigest()[:32]


def remote_link():
    return f"{PUBLIC_RELAY}remote.html#k={REMOTE_KEY}"


agent = None  # the hidden Chrome running agent.html


def keep_agent_running():
    """Start the hidden agent Chrome, and start it again whenever it stops (checked every 30s)."""
    global agent
    while True:
        if (agent is None or agent.poll() is not None) and os.path.exists(CHROME):
            agent = subprocess.Popen([CHROME, "--headless=new", f"--user-data-dir={AGENT_PROFILE}", "--no-first-run",
                                      "--no-default-browser-check", "--disable-background-timer-throttling",
                                      f"http://localhost:{PORT}/agent.html"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **DETACHED)
        time.sleep(30)


def keep_awake():
    """Keep the computer from sleeping while this runs (shows, the TV and the phone links keep
    working). The screen can still turn off. Undone automatically when this process ends."""
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            import ctypes  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, held for this process's lifetime
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        else:
            subprocess.Popen(["systemd-inhibit", "--what=sleep", "--why=TV remote", "sleep", "infinity"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("Keeping this computer awake while the helper runs.")
    except (OSError, AttributeError):
        pass


def phone_link():
    ip = lan_ip()
    return f"http://{ip}:{PORT}/p/{KEY}" if ip else None


def start_run(args):
    """Start search.py with these arguments. A new run for a room replaces the one still going
    for that room (they'd fight over its tabs); runs for other rooms keep going."""
    room = args[args.index("--share-room") + 1].upper() if "--share-room" in args[:-1] else None
    old = runs.pop(room, None)
    if old and old.poll() is None:
        old.terminate()
        try:
            old.wait(5)
        except subprocess.TimeoutExpired:
            old.kill()
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "search.py"), *args], cwd=HERE,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **DETACHED)  # keeps running if this server stops
    runs[room] = proc
    return proc


def now_playing(room):
    """The TV show search.py recorded as playing in this room (NOW_DIR/<room>.json), or None."""
    if not room.isalnum() or not 4 <= len(room) <= 6:
        return None
    try:
        with open(os.path.join(NOW_DIR, f"{room.upper()}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def room_servers(room):
    """The site's video servers for the show in this room, and which is selected, or None."""
    if not room.isalnum() or not 4 <= len(room) <= 6:
        return None
    try:
        with open(os.path.join(NOW_DIR, f"{room.upper()}.servers.json")) as f:
            data = json.load(f)
        return {"servers": [str(x) for x in data["servers"]][:20], "server": data.get("server")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def busy_rooms():
    """Room codes a search.py run is going for right now (from the running processes)."""
    rooms = set()
    for proc in psutil.process_iter(["cmdline"]):
        try:
            parts = proc.info["cmdline"] or []
        except (psutil.Error, KeyError):
            continue
        if any(p.endswith("search.py") for p in parts) and "--share-room" in parts[:-1]:
            rooms.add(parts[parts.index("--share-room") + 1].upper())
    return rooms


def next_free_room(room, busy):
    """The next room code after this one that nothing is playing in: 1234 -> 1235 (or later)."""
    room = room.upper()
    if room.isdigit():
        for n in range(int(room) + 1, int(room) + 200):
            code = str(n).zfill(len(room))
            if len(code) <= 6 and code not in busy:
                return code
    for suffix in "23456789":  # letters, or digits at their limit: add a digit
        code = (room + suffix)[-6:] if len(room) >= 6 else room + suffix
        if code not in busy:
            return code
    return None


def room_status(room):
    """Whether a run is going in this room, what it's playing, and the next free room."""
    busy = busy_rooms()
    room = room.upper()
    if room not in busy:
        return {"busy": False}
    status = {"busy": True, "next": next_free_room(room, busy)}
    try:
        with open(os.path.join(NOW_DIR, f"{room}.info.json")) as f:
            info = json.load(f)
        status.update({"title": info.get("title"), "kind": info.get("kind")})
    except (OSError, ValueError):
        pass
    playing = now_playing(room)
    if playing and status.get("kind", "tv") == "tv":
        status.update({"title": status.get("title") or playing.get("title"), "season": playing.get("season"),
                       "episode": playing.get("episode")})
    return status


def room_playback(room):
    """Where the room's video is: {time, duration, paused, at}, or None. Only if fresh (the
    run reports it every second while it's watching)."""
    if not room.isalnum() or not 4 <= len(room) <= 6:
        return None
    try:
        with open(os.path.join(NOW_DIR, f"{room.upper()}.playback.json")) as f:
            data = json.load(f)
        if time.time() - float(data["at"]) > 20:
            return None
        return {k: data[k] for k in ("time", "duration", "paused", "at")}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pending_choices(pid):
    """The results the run with this process id (search.py --pick) is waiting on, or None."""
    try:
        with open(os.path.join(PICKS_DIR, f"{int(pid)}.choices.json")) as f:
            data = json.load(f)
        if not psutil.pid_exists(int(data["pid"])):  # that run has ended
            return None
        return {"id": data["id"], "kind": data.get("kind", "result"), "prompt": data.get("prompt", "Which one?"),
                "items": data["items"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def info():
    ip = lan_ip()
    return {"relayRunning": ensure_relay(), "lanRelay": f"http://{ip}:{RELAY_PORT}/" if ip else None,
            "publicRelay": PUBLIC_RELAY, "home": HOME_ID, "running": sum(p.poll() is None for p in runs.values())}


CONFIG_KEYS = {"site": str, "room": str, "play_selectors": list, "fullscreen_selector": str,
               "after_fullscreen_selectors": list, "keep_playing_selector": str,
               "server_button_selector": str, "page_fix_query": str}


def clean_config(data):
    """Keep only the known settings, with the right types (text, or lists of text)."""
    if not isinstance(data, dict):
        raise ValueError("The settings must be a JSON object, like {\"site\": \"www.example.com\"}.")
    out = {}
    for key, kind in CONFIG_KEYS.items():
        if key not in data or data[key] in (None, ""):
            continue
        value = data[key]
        if kind is list:
            if isinstance(value, str):
                value = [v for v in value.splitlines()]
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"\"{key}\" should be a list of text.")
            value = [v.strip() for v in value if v.strip()][:20]
        else:
            if not isinstance(value, str):
                raise ValueError(f"\"{key}\" should be text.")
            value = value.strip()[:500]
        out[key] = value
    if "room" in out and not (out["room"].isalnum() and 4 <= len(out["room"]) <= 6):
        raise ValueError("\"room\" should be 4 to 6 letters or digits.")
    return out


def read_config():
    try:
        with open(SITE_CONFIG_FILE) as f:
            return clean_config(json.load(f))
    except (OSError, ValueError):
        return {}


# Shown to a device that hasn't been let in yet: type the code (e.g. 1234) to get in.
CODE_PAGE = b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Search Command Builder</title>
<style>
:root { color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --text: #1b1f24; --muted: #5f6b7a;
        --border: #d7dce2; --accent: #2f6fed; }
@media (prefers-color-scheme: dark) { :root { --bg: #14171b; --card: #1d2126; --text: #e8ebef; --muted: #9aa5b1;
        --border: #333a42; --accent: #5b8ef5; } }
body { margin: 0; padding: 40px 16px; background: var(--bg); color: var(--text);
       font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
form { max-width: 360px; margin: 0 auto; background: var(--card); border: 1px solid var(--border);
       border-radius: 12px; padding: 24px; display: grid; gap: 12px; }
h1 { font-size: 20px; margin: 0; } p { margin: 0; color: var(--muted); }
input { font: 600 28px ui-monospace, Menlo, monospace; letter-spacing: .3em; text-align: center; padding: 10px;
        border: 1px solid var(--border); border-radius: 8px; background: var(--bg); color: var(--text); text-transform: uppercase; }
button { font: 600 16px inherit; padding: 12px; border: 0; border-radius: 8px; background: var(--accent); color: #fff; }
</style></head><body>
<form onsubmit="event.preventDefault(); const c = this.code.value.trim(); if (c) location.href = '/p/' + encodeURIComponent(c);">
  <h1>Search Command Builder</h1>
  <p>Enter the phone code (the end of the phone link) to use the builder on this device.</p>
  <input name="code" autocomplete="off" autocapitalize="characters" inputmode="text" placeholder="1234" autofocus>
  <button type="submit">Open</button>
</form></body></html>"""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        # Only other devices (your phone) are shown, which helps when a link doesn't open there.
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            msg = (fmt % args).replace(KEY, "****")
            if ("/logs.js" in msg or "/choices" in msg or "/now-playing" in msg) and '" 200 ' in msg:
                return  # the builder checks for new log output and results every second
            if "Bad request version" in msg or "Bad HTTP/0.9" in msg:
                msg += "  <- this was an https:// request; the link must start with http://"
            print(f"{time.strftime('%H:%M:%S')} {self.client_address[0]} {msg}", flush=True)

    def send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def host_ok(self):
        """The address typed in the browser must be this computer (stops DNS-rebinding tricks)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1", lan_ip())

    def has_key(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        return COOKIE in cookie and hmac.compare_digest(cookie[COOKIE].value, KEY)

    def allowed(self):
        """This computer, or a device that opened the phone link. Otherwise answers 403 and returns False."""
        if not self.host_ok():
            self.send_error(403)
            return False
        if self.is_local() or self.has_key():
            return True
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(CODE_PAGE)
        return False

    def locked_out(self):
        tries, last = misses.get(self.client_address[0], (0, 0))
        if tries >= MAX_MISSES and time.time() - last < LOCKOUT:
            return True
        if tries >= MAX_MISSES:
            misses.pop(self.client_address[0], None)  # lockout over
        return False

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        key = parse_qs(url.query).get("key", [""])[0]
        if path.startswith("/p/"):
            key = path[3:].strip("/").upper()
        if key and self.locked_out():
            return self.send_error(429, "Too many wrong codes. Try again later.")
        if key and not hmac.compare_digest(key.upper(), KEY):
            tries, _ = misses.get(self.client_address[0], (0, 0))
            misses[self.client_address[0]] = (tries + 1, time.time())
        if key and self.host_ok() and hmac.compare_digest(key.upper(), KEY):
            # The phone link: remember the key on this device, then drop it from the address bar.
            self.send_response(302)
            self.send_header("Set-Cookie", f"{COOKIE}={KEY}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
            self.send_header("Location", "/command-builder.html")
            self.end_headers()
            return
        if not self.allowed():
            return
        if path in ("/phone-link", "/agent-config", "/agent.html") and not self.is_local():
            return self.send_error(403)  # only for this computer
        if path == "/phone-link":
            self.send_json(200, {"url": phone_link(), "remote": remote_link()})
        elif path == "/config":
            self.send_json(200, read_config())
        elif path == "/agent-config":
            self.send_json(200, {"peerId": agent_peer_id()})
        elif path == "/":
            self.send_response(302)
            self.send_header("Location", "/command-builder.html")
            self.end_headers()
        elif path == "/now-playing":
            room = parse_qs(url.query).get("room", [""])[0].upper()
            playing = now_playing(room)
            body = {k: playing[k] for k in ("room", "title", "season", "episode", "seasons", "episodes")} if playing else {}
            body.update(room_servers(room) or {})  # {servers: [...], server: n}, for movies too
            playback = room_playback(room)
            if playback:
                body["playback"] = playback  # {time, duration, paused, at}
            self.send_json(200, body)
        elif path == "/room-status":
            room = parse_qs(url.query).get("room", [""])[0]
            if not room.isalnum() or not 4 <= len(room) <= 6:
                return self.send_json(400, {"error": "Bad room code."})
            self.send_json(200, room_status(room))
        elif path == "/choices":
            pid = parse_qs(url.query).get("pid", [""])[0]
            waiting = pid.isdigit() and pending_choices(pid)
            alive = False
            if pid.isdigit():
                alive = psutil.pid_exists(int(pid))
            self.send_json(200, {**(waiting or {}), "alive": alive})  # alive: the run is still going
        elif path == "/info":
            self.send_json(200, info())
        elif path in SERVED:
            super().do_GET()
        else:
            self.send_error(404)

    def end_headers(self):
        if self.path.split("?")[0] in ("/logs.js", "/logs.json"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self):
        if self.path not in ("/run", "/choose", "/switch", "/server", "/control", "/config"):
            return self.send_error(404)
        if not self.allowed():
            return
        # A JSON body from our own page only: another site can't send this without the
        # browser asking us first (and we never say yes).
        if (self.headers.get("Origin") != f"http://{self.headers.get('Host')}"
                or not (self.headers.get("Content-Type") or "").startswith("application/json")):
            return self.send_json(403, {"error": "Run only works from the builder page itself."})
        if self.path == "/config":
            # Pasted-in site settings (the builder's "Import settings"), saved for every run.
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                config = clean_config(body)
            except ValueError as e:
                return self.send_json(400, {"error": str(e) if str(e) else "That isn't valid JSON."})
            with open(SITE_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
            return self.send_json(200, config)
        if self.path == "/control":
            # Pause/Play or a jump for the room's video: left for that room's run to carry out.
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                room, action = str(body["room"]).upper(), str(body["action"])
                at = float(body["time"]) if action == "seek" else None
            except (ValueError, KeyError, TypeError):
                return self.send_json(400, {"error": "Bad request."})
            if action not in ("play", "pause", "toggle", "seek") or (at is not None and not 0 <= at < 86400):
                return self.send_json(400, {"error": "Bad request."})
            if not room_playback(room):
                return self.send_json(409, {"error": "Nothing is playing in that room right now."})
            with open(os.path.join(NOW_DIR, f"{room}.control.json"), "w") as f:
                json.dump({"action": action, "time": at}, f)
            return self.send_json(200, {"ok": True})
        if self.path == "/server":
            # Another video server for the show in a room: left for that room's run to pick up.
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                room, number = str(body["room"]).upper(), int(body["server"])
            except (ValueError, KeyError, TypeError):
                return self.send_json(400, {"error": "Bad request."})
            servers = room_servers(room)
            if not servers or not 1 <= number <= len(servers["servers"]):
                return self.send_json(409, {"error": "That server isn't available in this room."})
            with open(os.path.join(NOW_DIR, f"{room}.server-request.json"), "w") as f:
                json.dump({"server": number}, f)
            return self.send_json(200, {"ok": True})
        if self.path == "/switch":
            # Another season or episode of what's playing in a room: a new run straight to the
            # show's page (no search), built from what was recorded, not from the request.
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                room = str(body["room"]).upper()
                season = int(body["season"])
                episode = int(body["episode"]) if body.get("episode") not in (None, "") else None
            except (ValueError, KeyError, TypeError):
                return self.send_json(400, {"error": "Bad request."})
            playing = now_playing(room)
            if not playing or not playing.get("show_url"):
                return self.send_json(409, {"error": "Nothing switchable is playing in that room."})
            args = [playing.get("title") or "show", "--url", playing.get("site") or playing["show_url"],
                    "--show-url", playing["show_url"], "--pick", "--share-room", room, "--season", str(season)]
            if episode:
                args += ["--episode", str(episode)]
            return self.send_json(200, {"pid": start_run(args).pid, **info()})
        if self.path == "/choose":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                pid, pick_id, index = int(body["pid"]), str(body["id"]), int(body["index"])
            except (ValueError, KeyError, TypeError):
                return self.send_json(400, {"error": "Bad request."})
            waiting = pending_choices(pid)
            if not waiting or waiting["id"] != pick_id or not 0 <= index < len(waiting["items"]):
                return self.send_json(409, {"error": "That search isn't waiting for a pick anymore."})
            with open(os.path.join(PICKS_DIR, f"{pid}.choice.json"), "w") as f:
                json.dump({"id": pick_id, "index": index}, f)
            return self.send_json(200, {"ok": True, "item": waiting["items"][index]})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            args = body["args"]
            assert isinstance(args, list) and args and all(isinstance(a, str) for a in args)
        except (ValueError, KeyError, AssertionError):
            return self.send_json(400, {"error": "Bad request."})
        if any(a == "--profile" or a.startswith("--profile=") for a in args):
            return self.send_json(400, {"error": "--profile can't be set from the builder."})
        self.send_json(200, {"pid": start_run(args).pid, **info()})


def main():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)  # the phone link needs the Wi-Fi too
    except OSError:
        print(f"Something is already using port {PORT}; if it's this server, just open http://localhost:{PORT}/")
        webbrowser.open(f"http://localhost:{PORT}/")
        return
    ensure_relay()
    url = f"http://localhost:{PORT}/"
    print(f"Search Command Builder: {url}  (Ctrl+C to stop)")
    if phone_link():
        print(f"On your phone (same Wi-Fi): {phone_link()}")
    print(f"From anywhere: {remote_link()}  (passcode in {REMOTE_PASS_FILE})")
    threading.Thread(target=keep_agent_running, daemon=True).start()
    keep_awake()
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        if agent and agent.poll() is None:
            agent.terminate()


if __name__ == "__main__":
    main()
