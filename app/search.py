"""Open a site in Chrome, click its search bar and type a name into it.

Usage:
    python3 search.py "The Bear"
    python3 search.py "The Bear" --submit                 # also press Enter
    python3 search.py "laptop stand" --url https://www.amazon.com
    python3 search.py "query" --url https://example.com --selector "#my-search"
    python3 search.py "The Bear" --url example.com --type tv     # search, then open the first TV show
    python3 search.py "The Bear" --url example.com --type tv --season 2 --episode 3   # ...pick season 2, episode 3
    python3 search.py "Up" --url example.com --type movie        # search, then open the first movie
    (with --type it then presses Play until the player's fullscreen button shows; --no-play skips that)
    python3 search.py "Up" --url example.com --type movie --teach   # you click the real Play; it remembers
    python3 search.py "Up" --url example.com --type movie --play-selector ".intro-play" --play-selector "#watch-now"   # exact buttons, in order
    python3 search.py "Up" --url example.com --type movie --fullscreen-selector ".fs-btn"   # exact fullscreen button
    python3 search.py "Up" --url example.com --type movie --after-fullscreen-selector ".big-play"   # then press this
    python3 search.py "Up" --url example.com --type movie --share-room 1234   # first share the show's tab (with sound) to the TV

Chrome runs with its own saved profile (the chrome-profile folder next to this script), so
extensions you install there, like an ad blocker, stay installed. The first run opens that
Chrome; later runs reuse the open window and add a tab.

Everything a run prints is also saved to logs.js / logs.json next to this script, so
command-builder.html can show it. The last 20 runs are kept.
"""
import argparse
import hashlib
import platform
import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
from urllib.parse import urlparse

import psutil
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

DEFAULT_URL = "https://www.example.com"  # replaced by the configuration's "site", if set

LOG_DIR = os.path.dirname(os.path.abspath(__file__))
HOME_FILE = os.path.join(LOG_DIR, ".home-id")  # this install's private namespace for room codes (made by the helper)
try:
    with open(HOME_FILE) as _f:
        HOME_ID = "".join(ch for ch in _f.read() if ch.isalnum())[:16]
except OSError:
    HOME_ID = ""
LOG_JSON = os.path.join(LOG_DIR, "logs.json")  # read back by the next run
LOG_JS = os.path.join(LOG_DIR, "logs.js")      # loaded by command-builder.html (works from file://)
KEEP_RUNS = 20

PROFILE_DIR = os.path.join(LOG_DIR, "chrome-profile")
TAUGHT_FILE = os.path.join(LOG_DIR, "play-buttons.json")  # play buttons taught with --teach, per site
PICKS_DIR = os.path.join(LOG_DIR, "picks")  # --pick: <pid>.choices.json (the results, shown by the builder
                                           # page) and <pid>.choice.json (the one picked there)
ROOMS_FILE = os.path.join(LOG_DIR, "rooms.json")  # room code -> the show and relay tabs sharing to it
NOW_DIR = os.path.join(LOG_DIR, "nowplaying")  # <room>.json: the TV show playing in that room, its season
                                              # and episode lists (for the phone's season/episode switcher)
PICK_WAIT = 600  # seconds to wait for a pick
def load_settings():
    """settings.json next to this file (optional): ports, hidden/visible Chrome, logs."""
    try:
        with open(os.path.join(LOG_DIR, "settings.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


SETTINGS = load_settings()
DEBUG_PORT = int(SETTINGS.get("chrome_port", 9333))  # the script's Chrome listens here so later runs reuse it
HEADLESS = bool(SETTINGS.get("headless", True))  # True: Chrome runs hidden. False: a normal window (troubleshooting).
SAVE_LOGS = bool(SETTINGS.get("save_logs", True))  # keep the last runs' output (the builder's Logs panel)


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


CHROME_BIN = find_chrome()
DETACHED = ({"creationflags": 0x00000008 | 0x00000200} if os.name == "nt"  # DETACHED_PROCESS | NEW_PROCESS_GROUP
            else {"start_new_session": True})  # keeps running after this script ends
SHARE_URL = f"http://localhost:{int(SETTINGS.get('relay_port', 8765))}/"  # Screen Relay, which puts the show on the TV
SHARE_TAB_TITLE = "Site Search Show"  # the show's tab gets this title so Chrome shares it (with its sound) without asking


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def chrome_process(port=DEBUG_PORT):
    """The script's own Chrome (its main process, started with this debugging port), or None."""
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = proc.info["cmdline"] or []
        except (psutil.Error, KeyError):
            continue
        if any(a == f"--remote-debugging-port={port}" for a in cmd) and not any(a.startswith("--type=") for a in cmd):
            return proc
    return None


def chrome_command_line(port=DEBUG_PORT):
    """The open script Chrome's command line, or "" if it isn't running."""
    proc = chrome_process(port)
    try:
        return " ".join(proc.cmdline()) if proc else ""
    except psutil.Error:
        return ""


def chrome_major_version():
    """Chrome's major version number, as text (e.g. "155")."""
    if os.name == "nt":  # chrome.exe --version prints nothing on Windows; the registry has it
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon") as key:
                return str(winreg.QueryValueEx(key, "version")[0]).split(".")[0]
        except OSError:
            return "155"
    try:
        out = subprocess.run([CHROME_BIN, "--version"], capture_output=True, text=True, timeout=10).stdout
        return out.split()[-1].split(".")[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return "155"


def normal_user_agent():
    """What a regular (not headless) Chrome of this version calls itself, so sites treat it as one."""
    system = platform.system()
    os_part = ("Windows NT 10.0; Win64; x64" if system == "Windows" else
               "X11; Linux x86_64" if system == "Linux" else "Macintosh; Intel Mac OS X 10_15_7")
    return (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_major_version()}.0.0.0 Safari/537.36")


def open_browser(profile, port=DEBUG_PORT, timeout=20, auto_share=False, visible=False):
    """Start Chrome with the saved profile (or reuse the one already open) and control it.

    Chrome is started as a normal app rather than by Selenium, so it has no automation
    restrictions and the Chrome Web Store works for installing extensions into the profile.
    Unless visible, it runs headless: nothing opens, goes fullscreen or flashes on this computer's
    screen, so someone using the computer isn't interrupted (the TV and phones still see the show).
    An open Chrome that doesn't match (visible vs hidden, or without the auto-share setting,
    which it only reads at startup) is restarted, which ends the shows playing in it.
    """
    running = chrome_command_line(port)
    if running and port_open(port):
        hidden = "--headless" in running
        if hidden == visible or (auto_share and not chrome_auto_shares(port)):
            print("Switching the script's Chrome to a visible window…" if visible and hidden else
                  "Switching the script's Chrome to run hidden, so the computer's screen isn't taken over…" if not visible
                  and not hidden else "Restarting the script's Chrome so it shares the show's tab without asking…")
            options = webdriver.ChromeOptions()
            options.debugger_address = f"127.0.0.1:{port}"
            try:
                webdriver.Chrome(options=options).execute_cdp_cmd("Browser.close", {})
            except WebDriverException:
                pass  # it closes as the command returns
            deadline = time.monotonic() + timeout
            while port_open(port) and time.monotonic() < deadline:
                time.sleep(0.3)
            time.sleep(1)  # let it finish quitting before it's opened again
    launched = not port_open(port)
    if launched:
        os.makedirs(profile, exist_ok=True)
        flags = [f"--user-data-dir={profile}", f"--remote-debugging-port={port}", "--no-first-run",
                 "--no-default-browser-check",
                 # When a page asks to share the screen, share the show's tab (picked by its
                 # title) instead of asking. Unlike the entire screen, a tab's sound comes too.
                 f"--auto-select-tab-capture-source-by-title={SHARE_TAB_TITLE}",
                 # The relay tab isn't the one in front; without these Chrome slows a background
                 # tab's timers after a few minutes, and its connection drops.
                 "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                 "--disable-backgrounding-occluded-windows"]
        if not os.path.exists(CHROME_BIN):
            print("Google Chrome isn't installed (looked in the usual places). Install it from "
                  "https://www.google.com/chrome/ and try again.", file=sys.stderr)
            sys.exit(1)
        mode = (["--start-maximized"] if visible else
                ["--headless=new", "--window-size=1920,1080", f"--user-agent={normal_user_agent()}"])
        subprocess.Popen([CHROME_BIN, *mode, *flags], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **DETACHED)
        deadline = time.monotonic() + timeout
        while not port_open(port):
            if time.monotonic() > deadline:
                print("Chrome didn't start.", file=sys.stderr)
                sys.exit(1)
            time.sleep(0.3)
    # The driver can only attach to a Chrome with at least one page open, and a hidden Chrome
    # doesn't open a new one when its last window closes; so make sure there's one.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
            pages = [t for t in json.load(r) if t.get("type") == "page"]
        if not pages:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/json/new?about:blank",
                                                          method="PUT"), timeout=5).read()
    except (OSError, ValueError):
        pass
    options = webdriver.ChromeOptions()
    options.debugger_address = f"127.0.0.1:{port}"
    # Don't let the driver wait for pages and frames to finish loading: a video server that's
    # down leaves its player frame loading forever, and every step into that frame would hang
    # (then the run crashed after 2 minutes). Pages are waited for with load() instead.
    options.page_load_strategy = "none"
    try:
        driver = webdriver.Chrome(options=options)
    except WebDriverException as e:
        # Chrome updated itself while this one kept running: the driver now matches the new
        # version and won't talk to the old one. Restart it (on the new version) and try again.
        if "only supports Chrome version" not in str(e) or getattr(open_browser, "_restarted", False):
            raise
        print("Chrome updated itself since it was started; restarting it on the new version…")
        open_browser._restarted = True
        proc = chrome_process(port)
        if proc:
            try:
                proc.terminate()
            except psutil.Error:
                pass
        deadline = time.monotonic() + timeout
        while port_open(port) and time.monotonic() < deadline:
            time.sleep(0.3)
        time.sleep(1)
        return open_browser(profile, port, timeout, auto_share, visible)
    if not launched:
        # A window of its own: leaves what's already open alone, and a show in another room
        # (another window) stays fullscreen, since only one tab per window can be.
        driver.switch_to.new_window("window")
    return driver


class RunLog:
    """Saves this run's output, newest run first, rewriting the files as lines come in."""

    def __init__(self, argv):
        try:
            with open(LOG_JSON) as f:
                runs = json.load(f) if SAVE_LOGS else []
        except (OSError, ValueError):
            runs = []
        self.run = {"id": f"{time.time():.3f}", "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "command": shlex.join(["python3"] + argv), "status": "running", "text": ""}
        self.runs = [self.run] + runs[:KEEP_RUNS - 1]
        self.save()

    def write(self, text):
        self.run["text"] += text
        self.save()

    def finish(self, status):
        self.run["status"] = status
        self.save()

    def save(self):
        if not SAVE_LOGS:
            return
        data = json.dumps(self.runs)
        for path, body in ((LOG_JSON, data), (LOG_JS, f"window.SEARCH_LOGS = {data};\n")):
            tmp = path + ".tmp"
            try:
                with open(tmp, "w") as f:
                    f.write(body)
                os.replace(tmp, path)  # the page never sees a half-written file
            except OSError:
                pass  # logging must never break the search itself


class Tee:
    """Sends output to the terminal as usual and to the run log."""

    def __init__(self, stream, log):
        self.stream, self.log = stream, log

    def write(self, text):
        self.log.write(text)
        n = self.stream.write(text)
        if "\n" in text:
            self.stream.flush()  # show each line as it happens, even when the output goes to a pipe
        return n

    def flush(self):
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)

# Tried in order until one matches a visible, usable box.
SEARCH_SELECTORS = [
    "input[type='search']",
    "input[name='q']",
    "textarea[name='q']",
    "input[name*='search' i]",
    "input[placeholder*='search' i]",
    "input[aria-label*='search' i]",
    "input[id*='search' i]",
    "input[class*='search' i]",
]


def find_search_box(driver, selectors, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for sel in selectors:
            for el in driver.find_elements(By.CSS_SELECTOR, sel):
                try:
                    if el.is_displayed() and el.is_enabled():
                        return el, sel
                except WebDriverException:
                    continue  # element went stale while the page was still loading
        time.sleep(0.5)
    return None, None


def type_into_search(driver, selectors, text, timeout, attempts=4, settle=2.0):
    """Type the text, then make sure it is still there once the page settles.

    Many sites (JustWatch included) re-render the search bar shortly after loading,
    which wipes anything typed too early. If that happens, find the new box and retype.
    """
    for _ in range(attempts):
        box, used = find_search_box(driver, selectors, timeout)
        if box is None:
            return None, None
        try:
            try:
                box.click()
            except WebDriverException:
                driver.execute_script("arguments[0].focus();", box)  # something is covering it
            box.clear()
            box.send_keys(text)
            time.sleep(settle)
            if box.get_attribute("value") == text:
                return box, used
        except WebDriverException:
            pass  # the box was replaced mid-typing; look for the new one
    return None, None


# Section headings on the results page, per --type. Matched case-insensitively against
# an element's whole text, so "MOVIES" and "Movies" both work.
SECTION_LABELS = {
    "movie": ["movies", "movie", "films", "film"],
    "tv": ["tv shows", "tv show", "tv series", "shows", "series", "tv"],
}

# Walks the page in order, tracking which section (MOVIES / TV SHOWS) we're in, and
# collects clickable things inside the wanted section. A section label can appear more
# than once (site menu, filter tabs, the heading itself), so every occurrence switches the
# current section and none of them is ever a candidate. Section chrome such as
# "1 results" or "See all" is skipped. A candidate whose text, image alt or label contains
# the search title wins; otherwise the first one is used.
FIND_RESULTS_JS = """
const wanted = arguments[0], others = arguments[1], query = (arguments[2] || '');
const norm = t => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
const labels = [...wanted, ...others];
// "TV Shows (1)", "TV SHOWS 1 results", "2 Movies" all count as the label.
const stripCount = t => t.replace(/[\\s(\\[·-]*\\d+\\s*(results?)?[\\s)\\]]*$/, '').replace(/^\\d+\\s*/, '').trim();
const inMenu = el => !!el.closest('nav, header, [role=navigation], [role=menu], [role=menubar]');
const sectionOf = el => {
    if (el.children.length > 3 || inMenu(el) || !visible(el)) return null;
    const t = stripCount(norm(el.innerText));
    return wanted.includes(t) ? 'wanted' : others.includes(t) ? 'other' : null;
};
const skip = /favorite|favourite|notify|watchlist|share|trailer/i;
const chrome = /^(\\d+\\s+results?|see all|view all|show all|more|show more|load more)$/i;
const pointer = el => el && el.nodeType === 1 && getComputedStyle(el).cursor === 'pointer';
const clickable = el => el.matches('a[href], [role=link], [onclick]')
    || (pointer(el) && !pointer(el.parentElement));          // outermost cursor:pointer element
// Text of the element minus its buttons, so a card holding "Add to Favorites" still counts.
const ownText = el => {
    const c = el.cloneNode(true);
    c.querySelectorAll('button, [role=button]').forEach(b => b.remove());
    return norm(c.textContent);
};
const label = el => norm([el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'),
    ...Array.from(el.querySelectorAll('img[alt]'), i => i.alt)].join(' '));

const all = Array.from(document.body.querySelectorAll('*'));
const sections = new Map(all.map(el => [el, sectionOf(el)]).filter(([, s]) => s));
const headings = [...sections.keys()];
let section = null;
const candidates = [];
for (const el of all) {
    if (sections.has(el)) { section = sections.get(el); continue; }
    if (section !== 'wanted' || inMenu(el) || !clickable(el) || !visible(el)) continue;
    if (el.matches('button, [role=button]')) continue;
    if (headings.some(h => h.contains(el) || el.contains(h))) continue;   // part of a section label
    if (candidates.some(c => c.contains(el))) continue;                  // inside a card we already have
    const href = el.getAttribute('href') || '';
    if (href.startsWith('#') || href.startsWith('javascript:')) continue;
    const text = ownText(el);
    if (!text && !el.querySelector('img')) continue;
    if (skip.test(text) || skip.test(el.getAttribute('aria-label') || '')) continue;
    if (chrome.test(text) || labels.includes(stripCount(text))) continue;
    candidates.push(el);
}
const q = norm(query);
const best = (q && candidates.find(el => label(el).includes(q))) || candidates[0] || null;
return [best, candidates.map(el => [el.tagName.toLowerCase(), label(el).slice(0, 80),
                                    el.getAttribute('href') || '', el.outerHTML.slice(0, 200)])];
"""


# --pick: every result on the results page, as {title, href, poster, kind}. A result is a
# link holding a poster image; its kind comes from its address (/series/... is a TV show,
# /movie/... a movie) or else from the MOVIES / TV SHOWS heading it's under.
LIST_RESULTS_JS = """
const movieLabels = arguments[0], tvLabels = arguments[1];
const norm = t => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
const stripCount = t => t.replace(/[\\s(\\[·-]*\\d+\\s*(results?)?[\\s)\\]]*$/, '').replace(/^\\d+\\s*/, '').trim();
const inMenu = el => !!el.closest('nav, header, [role=navigation], [role=menu], [role=menubar]');
const sectionOf = el => {
    if (el.children.length > 3 || inMenu(el) || !visible(el)) return null;
    const t = stripCount(norm(el.innerText));
    return movieLabels.includes(t) ? 'movie' : tvLabels.includes(t) ? 'tv' : null;
};
let section = null;
const out = [], seen = new Set();
for (const el of document.body.querySelectorAll('*')) {
    const s = sectionOf(el);
    if (s) { section = s; continue; }
    if (!el.matches('a[href]') || inMenu(el) || !visible(el)) continue;
    const img = el.querySelector('img');
    if (!img) continue;
    let url;
    try { url = new URL(el.href); } catch (e) { continue; }
    if (url.host !== location.host || seen.has(url.href)) continue;
    const kind = /\\/(series|tv|tv-shows?|shows?)\\//i.test(url.pathname) ? 'tv'
               : /\\/(movies?|films?)\\//i.test(url.pathname) ? 'movie' : section;
    if (!kind) continue;
    const title = (el.getAttribute('oldtitle') || el.getAttribute('title') || img.alt || el.innerText || '')
        .trim().split('\\n')[0].trim();
    if (!title) continue;
    const lazy = img.getAttribute('data-original') || img.getAttribute('data-src') || img.getAttribute('data-lazy-src');
    let poster = img.currentSrc || img.src || '';
    if ((!poster || /no_poster|placeholder|^data:/i.test(poster)) && lazy) poster = new URL(lazy, location.href).href;
    seen.add(url.href);
    out.push({ title, href: url.href, poster, kind });
    if (out.length >= 40) break;
}
return out;
"""


def ask_phone(kind, prompt, items, wait=PICK_WAIT):
    """Ask the builder page (the phone) to pick one of `items`, and wait. Returns its index, or None.

    kind is "result" (items have title, poster, kind), "season" or "episode" (items have
    title and maybe subtitle); prompt is the pop-up's heading. The question goes to
    PICKS_DIR/<pid>.choices.json, which builder-server.py shows on the phone; the answer
    comes back in <pid>.choice.json with the same id.
    """
    pick_id = secrets.token_hex(8)
    os.makedirs(PICKS_DIR, exist_ok=True)
    choices_file = os.path.join(PICKS_DIR, f"{os.getpid()}.choices.json")
    choice_file = os.path.join(PICKS_DIR, f"{os.getpid()}.choice.json")
    for path in (choice_file, choices_file):
        try:
            os.remove(path)
        except OSError:
            pass
    with open(choices_file, "w") as f:
        json.dump({"id": pick_id, "pid": os.getpid(), "kind": kind, "prompt": prompt, "items": items}, f)
    index = None
    try:
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                with open(choice_file) as f:
                    choice = json.load(f)
            except (OSError, ValueError):
                choice = None
            if choice and choice.get("id") == pick_id:
                i = choice.get("index")
                if isinstance(i, int) and 0 <= i < len(items):
                    index = i
                    break
            time.sleep(0.5)
    finally:
        for path in (choice_file, choices_file):
            try:
                os.remove(path)
            except OSError:
                pass
    if index is None:
        print(f"Nothing was picked within {wait // 60} minutes.", file=sys.stderr)
    return index


def pick_result(driver, timeout, wait=PICK_WAIT):
    """--pick: list the results for the builder page, wait for one to be picked, and open it.

    The results are shown on the phone through ask_phone. Returns the picked {title, href, poster, kind}, or None.
    """
    items, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            items = driver.execute_script(LIST_RESULTS_JS, SECTION_LABELS["movie"], SECTION_LABELS["tv"]) or []
        except WebDriverException:
            items = []
        if items:
            break
        time.sleep(0.5)
    if not items:
        return None
    time.sleep(1)  # posters load lazily; give them a moment, then read them again
    try:
        items = driver.execute_script(LIST_RESULTS_JS, SECTION_LABELS["movie"], SECTION_LABELS["tv"]) or items
    except WebDriverException:
        pass

    shows = sum(i["kind"] == "tv" for i in items)
    print(f"Found {len(items)} result(s) ({shows} TV, {len(items) - shows} movies). Pick one on the builder page…")
    index = ask_phone("result", "Which one?", items, wait)
    if index is None:
        return None
    item = items[index]
    open_show(driver, item["href"])
    return item


def apply_page_fix(driver, url, timeout):
    """Open the site with the configuration's page_fix_query added to its address, if there is one.

    Some sites change their pages with a setting in the address (a page design or player
    choice, for example), which they then remember; page_fix_query is that setting.
    """
    if not PAGE_FIX_QUERY:
        return
    try:
        if PAGE_FIX_QUERY in driver.current_url:
            return
    except WebDriverException:
        return
    target = url.split("#")[0]
    load(driver, target + ("&" if "?" in target else "?") + PAGE_FIX_QUERY, timeout)
    print(f"Opened the site with its page setting ({PAGE_FIX_QUERY}).")


def click_first_result(driver, kind, query, timeout, debug=False):
    """Wait for the results, then click the first result under the MOVIES or TV SHOWS heading."""
    wanted = SECTION_LABELS[kind]
    others = [label for k, labels in SECTION_LABELS.items() if k != kind for label in labels]
    deadline = time.monotonic() + timeout
    result = None
    candidates = []
    while time.monotonic() < deadline and result is None:
        try:
            result, candidates = driver.execute_script(FIND_RESULTS_JS, wanted, others, query)
        except WebDriverException:
            pass  # page still re-rendering
        if result is None:
            time.sleep(0.5)
    if debug:
        print(f"[debug] {len(candidates)} clickable item(s) found in that section:")
        for i, (tag, text, href, html) in enumerate(candidates, 1):
            print(f"[debug] {i}. <{tag}> text={text!r} href={href!r}\n[debug]    {html}")
    if result is None:
        return None

    imgs = result.find_elements(By.CSS_SELECTOR, "img[alt]")
    title = ((imgs[0].get_attribute("alt") if imgs else "") or result.get_attribute("aria-label") or result.text
             or result.get_attribute("href") or "").splitlines()
    title = title[0] if title else "(untitled)"
    windows = set(driver.window_handles)
    # On a whole card, click the poster: the card's center may be its "Add to Favorites" button.
    target = next((img for img in imgs if img.is_displayed()), result)
    if debug:
        print(f"[debug] clicking: {target.get_attribute('outerHTML')[:200]}")
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target)
    try:
        target.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", target)  # something is covering it

    time.sleep(1)
    new_windows = set(driver.window_handles) - windows
    if new_windows:  # the link opened a new tab
        driver.switch_to.window(new_windows.pop())
    return title


# Finds the title page's play button. Anything clickable is scored: its text, aria-label
# or title saying "Play" / "Watch now" / "Resume" counts most, a class or id like
# "play-button" counts less, and among equals the biggest one wins (the main play button
# is usually the large one). Trailers, playlists and watchlist buttons are ignored.
# arguments[0]: skip buttons this script already pressed (marked data-site-search-pressed).
# Otherwise they still count, but below a new one. A paused <video> counts too, since
# clicking a player usually starts it: a big one (a player) beats pressing the same button
# again, a small one is a last resort.
FIND_PLAY_JS = """
const skipPressed = arguments[0];
const norm = t => (t || '').replace(/\\s+/g, ' ').trim();
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0
    && getComputedStyle(el).visibility !== 'hidden'; };
const pointer = el => el && el.nodeType === 1 && getComputedStyle(el).cursor === 'pointer';
const clickable = el => el.matches('button, a[href], [role=button], [onclick], input[type=button], input[type=submit]')
    || (pointer(el) && !pointer(el.parentElement));
const said = /^(play|play now|watch|watch now|start watching|resume|resume watching|continue watching)\\b|\\bplay (episode|movie|s\\d|season)|^(play|resume|watch)\\s+s?\\d/i;
const named = /(^|[-_\\s])(play|btn-?play|play-?(btn|button|icon))([-_\\s]|$)|playbutton|playbtn|btnplay/i;
const not = /trailer|teaser|preview|clip|playlist|watchlist|watch later|favou?rite|notify|share|add to|settings|autoplay|display|pause|full ?screen|server|mirror|\\bsource|quality|subtitle|caption|audio|language/i;

const found = [];
for (const el of document.body.querySelectorAll('*')) {
    if (!clickable(el) || !visible(el) || el.closest('nav')) continue;
    const marked = !!el.closest('[data-site-search-pressed]');
    if (skipPressed && marked) continue;
    if (found.some(f => f.el.contains(el))) continue;     // already have its clickable parent
    const words = norm([el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'),
                        el.value].filter(Boolean).join(' '));
    const ids = [el.getAttribute('class'),
                 el.id, el.getAttribute('data-testid'), el.getAttribute('data-test'), el.getAttribute('data-qa'),
                 ...Array.from(el.querySelectorAll('svg, i, use, img'), c =>
                    [c.getAttribute('class'), c.getAttribute('aria-label'), c.getAttribute('alt'),
                     c.getAttribute('href'), c.getAttribute('xlink:href'), c.getAttribute('data-icon')].join(' '))
                ].join(' ');
    if (not.test(words) || (!words && not.test(ids))) continue;
    let score = said.test(words) ? 3 : named.test(ids) ? 1 : 0;
    if (!score) continue;
    if (marked) score = Math.max(score - 2, 0.25);
    const r = el.getBoundingClientRect();
    found.push({ el, score, area: r.width * r.height, words, ids });
}
for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect(), marked = !!v.closest('[data-site-search-pressed]');
    if (!v.paused || !visible(v) || marked || found.some(f => f.el.contains(v))) continue;
    const big = r.width * r.height >= 0.2 * innerWidth * innerHeight;
    if (skipPressed && !big) continue;   // a small one only after re-pressing is allowed too
    found.push({ el: v, score: big ? 2 : 0.2, area: r.width * r.height, words: 'video player', ids: '' });
}
found.sort((a, b) => b.score - a.score || b.area - a.area);
return [found.length ? found[0].el : null,
        found.map(f => [f.score, Math.round(f.area), f.words.slice(0, 60), f.el.outerHTML.slice(0, 200)])];
"""

PLAY_LOOP_SECONDS = 120  # keep pressing Play this long, waiting for the fullscreen button

# A visible fullscreen control (or the page already being fullscreen), as a short label.
# Players often draw their controls only once the video has really started, so this is
# the sign that pressing Play worked.
# arguments[0] = true marks the ones already there (e.g. on a trailer) so they're ignored later.
# arguments[1] = true returns the button itself (its clickable parent if it's an icon) to click.
FIND_FULLSCREEN_JS = """
const mark = arguments[0], wantElement = arguments[1];
if (!mark && (document.fullscreenElement || document.webkitFullscreenElement)) return 'already fullscreen';
const re = /full[ -]?screen/i;
const attrs = ['aria-label', 'title', 'class', 'id', 'data-testid', 'data-test', 'data-qa', 'data-tooltip',
               'data-title', 'data-tip', 'data-icon', 'alt'];
for (const el of document.querySelectorAll('*')) {
    const text = el.children.length ? '' : (el.textContent || '');
    const words = attrs.map(a => el.getAttribute(a) || '').join(' ') + ' ' + text;
    if (!re.test(words)) continue;
    if (el.matches('html, body, video') || el.querySelector('video')) continue;   // the player box itself
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || r.width * r.height > 40000) continue;           // hidden, or not a button
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') continue;
    if (mark) { el.setAttribute('data-site-search-old-fs', '1'); continue; }
    if (el.closest('[data-site-search-old-fs]')) continue;
    if (wantElement) return el.closest('button, [role=button], a[href]') || el;
    return (el.getAttribute('aria-label') || el.getAttribute('title') || 'fullscreen button').trim();
}
return null;
"""

PLAYING_VIDEO_JS = ("return Array.from(document.querySelectorAll('video'))"
                    ".find(v => !v.paused && !v.ended && !v.hasAttribute('data-site-search-was-playing')) || null;")
# Videos already playing before the first press (e.g. a muted background trailer) are
# marked and don't count as "playing".
VIDEO_PLAYING_JS = ("return Array.from(document.querySelectorAll('video'))"
                    ".some(v => !v.paused && !v.ended && !v.hasAttribute('data-site-search-was-playing'));")
MARK_PLAYING_JS = ("document.querySelectorAll('video').forEach(v => { if (!v.paused && !v.ended) "
                   "v.setAttribute('data-site-search-was-playing', '1'); });")


# Put on the show's tab before the site loads (Chrome runs it at the start of every page).
# The site's video players can "frame-bust": once loaded, a player sends the whole tab away
# (through its own address, to the site's home page), which loses the show. So every player
# frame gets a sandbox that lets it do everything a player needs (scripts, its own storage,
# fullscreen, pop-ups) except navigate the page around it. A frame that's already loading
# when it's found is reloaded with the sandbox on.
PLAYER_GUARD_JS = r"""
(() => {
  // Player frames may not navigate the page around them (frame-busting); everything else allowed.
  const SANDBOX = "allow-scripts allow-same-origin allow-forms allow-presentation allow-popups allow-popups-to-escape-sandbox allow-pointer-lock allow-downloads";
  const guard = f => { if (f && f.tagName === "IFRAME" && !f.hasAttribute("sandbox")) f.setAttribute("sandbox", SANDBOX); };
  const desc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, "src");
  Object.defineProperty(HTMLIFrameElement.prototype, "src", { configurable: true, enumerable: true,
    get() { return desc.get.call(this); }, set(v) { guard(this); desc.set.call(this, v); } });
  const setAttr = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (name, value) { if (this.tagName === "IFRAME" && String(name).toLowerCase() === "src") guard(this); return setAttr.call(this, name, value); };
  new MutationObserver(list => { for (const r of list) for (const n of r.addedNodes) {
      const frames = n.tagName === "IFRAME" ? [n] : (n.querySelectorAll ? [...n.querySelectorAll("iframe")] : []);
      for (const f of frames) if (!f.hasAttribute("sandbox")) { guard(f); if (f.src) f.src = f.src; } } })
    .observe(document.documentElement, { childList: true, subtree: true });
})();
"""


def guard_players(driver):
    """Sandbox the site's player frames in the current tab (see PLAYER_GUARD_JS)."""
    try:
        driver.execute_cdp_cmd("Page.enable", {})
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": PLAYER_GUARD_JS})
    except WebDriverException:
        pass


def open_show(driver, url, timeout=20, tries=3):
    """Open a show's (or movie's) page and make sure it's really there.

    The site sometimes sends a first visit on a detour (through another of its addresses and
    back to its home page) while it checks the browser. Then the page is opened again, up to
    `tries` times. Returns True once the address matches.
    """
    want = urlparse(url).path.rstrip("/")
    for attempt in range(tries):
        load(driver, url, timeout)
        time.sleep(1.5)  # a detour happens just after the page starts loading
        try:
            here = driver.current_url
        except WebDriverException:
            continue
        if urlparse(here).path.rstrip("/") == want:
            return True
        print(f"The site sent it to {urlparse(here).netloc}{urlparse(here).path} instead of the show; "
              f"opening the show again ({attempt + 2}/{tries})…" if attempt + 1 < tries else
              f"The site keeps sending it to {urlparse(here).netloc}{urlparse(here).path} instead of the show.")
    return False


def load(driver, url, timeout=20):
    """Open url and wait for its page itself to be ready (not for every frame in it)."""
    driver.get(url)
    deadline = time.monotonic() + timeout
    time.sleep(0.3)  # let the navigation start, so the old page's readyState isn't read
    while time.monotonic() < deadline:
        try:
            if driver.execute_script("return document.readyState") in ("interactive", "complete"):
                break
        except WebDriverException:
            pass  # between pages
        time.sleep(0.25)
    time.sleep(0.5)  # scripts that run as the page finishes


def each_frame(driver, depth=3, path=()):
    """Switch into the page, then each iframe in it (players often live in one, sometimes three
    frames deep), yielding the path."""
    driver.switch_to.default_content()
    for i in path:
        driver.switch_to.frame(driver.find_elements(By.TAG_NAME, "iframe")[i])
    if path and driver.execute_script("return window.top === window;"):
        # The switch didn't take (the frame is reloading, e.g. right after a server switch), so
        # this is still the site's own page: don't treat it as the frame, or its buttons (like
        # "Play Season 2 · Episode 1") would be pressed by mistake.
        driver.switch_to.default_content()
        raise WebDriverException("not inside the frame")
    yield path
    if depth:
        for i in range(len(driver.find_elements(By.TAG_NAME, "iframe"))):
            try:
                yield from each_frame(driver, depth - 1, path + (i,))
            except (WebDriverException, IndexError):
                pass  # frame went away while we were looking
    if not path:
        driver.switch_to.default_content()


def video_playing(driver):
    try:
        for _ in each_frame(driver):
            if driver.execute_script(VIDEO_PLAYING_JS):
                return True
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return False


def find_play(driver, skip_pressed, rules=None):
    """Best play button across the page and its iframes, as (frame path, element, candidates).

    With rules (taught with --teach), only buttons matching one of them count.
    """
    best, everything = None, []
    try:
        for path in each_frame(driver):
            try:
                if rules:
                    el, found = driver.execute_script(MATCH_TAUGHT_JS, rules, skip_pressed)
                else:
                    el, found = driver.execute_script(FIND_PLAY_JS, skip_pressed)
            except WebDriverException:
                continue
            where = "page" if not path else "frame " + ".".join(map(str, path))
            everything += [[where] + f for f in found]
            if el is not None and (best is None or found[0][:2] > best[2][0][:2]):
                best = (path, el, found)
    except WebDriverException:
        pass
    driver.switch_to.default_content()
    if best is None:
        return None, everything
    everything.sort(key=lambda f: (f[1], f[2]), reverse=True)
    return best[:2], everything


# Invisible ad links laid over a page (a link to another website, with nothing in it, covering
# part of the page) catch clicks meant for the buttons underneath. They're removed before a click.
REMOVE_AD_OVERLAYS_JS = """
let removed = 0;
for (const a of document.querySelectorAll('a[href]')) {
    let host;
    try { host = new URL(a.href, location.href).host; } catch (e) { continue; }
    if (!host || host === location.host) continue;
    if (a.querySelector('img, video, svg, picture') || (a.textContent || '').trim()) continue;
    const s = getComputedStyle(a), r = a.getBoundingClientRect();
    if (r.width * r.height < 2500 && s.position !== 'fixed' && s.position !== 'absolute') continue;
    a.remove();
    removed++;
}
return removed;
"""


def remove_ad_overlays(driver):
    """Remove invisible ad links from the page and its iframes (see REMOVE_AD_OVERLAYS_JS)."""
    try:
        for _ in each_frame(driver):
            try:
                driver.execute_script(REMOVE_AD_OVERLAYS_JS)
            except WebDriverException:
                pass
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()


def close_ad_tabs(driver, before, home):
    """Close tabs opened since `before` that aren't on the same site; return a same-site one, if any."""
    keep = None
    host = urlparse(driver.current_url).hostname if driver.current_window_handle == home else None
    for handle in set(driver.window_handles) - before:
        try:
            driver.switch_to.window(handle)
            new_host = urlparse(driver.current_url).hostname
            if new_host and host and (new_host == host or new_host.endswith("." + host) or host.endswith("." + new_host)):
                keep = handle  # the player opened in a new tab on the same site
            else:
                driver.close()  # an ad (another website, or a blank pop-up)
        except WebDriverException:
            pass
    driver.switch_to.window(keep or home)
    return keep


def click_play(driver, path, button, debug=False):
    """Click a button found by find_play (inside its iframe) and return a short label for it."""
    home = driver.current_window_handle
    remove_ad_overlays(driver)
    for _ in each_frame(driver, depth=0, path=path):
        break  # just switches into the button's frame
    label = (button.get_attribute("aria-label") or button.text or button.get_attribute("title") or "play button")
    label = label.splitlines()[0].strip() or "play button"
    windows = set(driver.window_handles)
    driver.execute_script("arguments[0].setAttribute('data-site-search-pressed', '1');"
                          "arguments[0].scrollIntoView({block: 'center'});", button)
    if debug:  # show which button it picked before pressing it
        driver.execute_script("arguments[0].style.outline = '4px solid red';"
                              "arguments[0].style.outlineOffset = '2px';", button)
        print(f"[debug] about to press \"{label}\": it's outlined in red in Chrome")
        time.sleep(2)
    try:
        button.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", button)  # something is covering it
    driver.switch_to.default_content()
    time.sleep(1)
    if set(driver.window_handles) - windows:  # a new tab: the player (same site), or an ad (closed)
        close_ad_tabs(driver, windows, home)
    return label


def fullscreen_button(driver, mark=False):
    """Label of a new visible fullscreen button on the page or in its iframes, or None.

    With mark=True, records the ones already showing so they don't count later.
    """
    try:
        for _ in each_frame(driver):
            label = driver.execute_script(FIND_FULLSCREEN_JS, mark)
            if label:
                return label
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return None


# Everything worth clicking to go fullscreen, best first. A label match is often an icon or
# a tooltip inside (or next to) the real control, so each one is swapped for the nearest
# clickable thing around it. arguments[0] is a CSS selector (from --fullscreen-selector) to use
# instead of guessing. Ones already clicked twice without effect are skipped.
FULLSCREEN_TARGETS_JS = """
const selector = arguments[0];
const re = /full[ -]?screen|expand|maximi[sz]e/i;
const attrs = ['aria-label', 'title', 'class', 'id', 'data-testid', 'data-test', 'data-qa', 'data-tooltip',
               'data-title', 'data-tip', 'data-icon', 'data-plyr', 'alt'];
const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
const pointer = el => el && el.nodeType === 1 && getComputedStyle(el).cursor === 'pointer';
const target = el => {
    const c = el.closest('button, [role=button], a[href], [onclick], input[type=button]');
    if (c) return c;
    for (let p = el, i = 0; p && p !== document.body && i < 5; p = p.parentElement, i++)
        if (pointer(p) && !pointer(p.parentElement)) return p;
    return el;
};
let found = [];
if (selector) {
    try { found = Array.from(document.querySelectorAll(selector)); } catch (e) { return []; }
    found = found.filter(visible).map(target);
} else {
    for (const el of document.querySelectorAll('*')) {
        const text = el.children.length ? '' : (el.textContent || '');
        if (text.length > 40) continue;
        const words = attrs.map(a => (el.getAttribute(a) || '')).join(' ') + ' ' + text;
        if (!/full[ -]?screen/i.test(words) && !(re.test(words) && el.closest('[class*=player i], [id*=player i], [class*=control i]'))) continue;
        if (el.matches('html, body, video') || el.querySelector('video')) continue;
        if (!visible(el) || el.closest('[data-site-search-old-fs]')) continue;
        if (/theme/i.test(words)) continue;  // the site's theme switcher is not a fullscreen button
        // In the site's own page (not a player's frame), only buttons that belong to a player count.
        if (window.top === window && !el.closest('[class*=player i], [id*=player i], [class*=art-] , video')) continue;
        const t = target(el);
        const r = t.getBoundingClientRect();
        if (r.width * r.height > 40000 || t.querySelector('video')) continue;
        found.push(t);
    }
}
return found.filter((el, i) => found.indexOf(el) === i)
            .filter(el => +(el.getAttribute('data-site-search-fs-tries') || 0) < 2);
"""

IS_FULLSCREEN_JS = "return !!(document.fullscreenElement || document.webkitFullscreenElement);"

_last_fallback = [0.0]


def is_fullscreen(driver):
    driver.switch_to.default_content()  # an iframe going fullscreen shows up in the top page too
    try:
        return bool(driver.execute_script(IS_FULLSCREEN_JS))
    except WebDriverException:
        return False


def wait_fullscreen(driver, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if is_fullscreen(driver):
            return True
        time.sleep(0.25)
    return False


def real_press(driver, el):
    """A trusted click (browsers only allow fullscreen from one), never a JS .click()."""
    try:
        driver.execute_script(REMOVE_AD_OVERLAYS_JS)  # in this element's frame
    except WebDriverException:
        pass
    try:
        el.click()
        return True
    except WebDriverException:
        pass
    try:
        ActionChains(driver).move_to_element(el).click().perform()
        return True
    except WebDriverException:
        return False


# Makes the biggest visible player frame (or, without one, the biggest video) fullscreen.
# Run through Chrome's DevTools with userGesture, so it counts as if it came from a click.
FULLSCREEN_PLAYER_JS = """(() => {
  const size = e => { const r = e.getBoundingClientRect(); return r.width * r.height; };
  const big = [...document.querySelectorAll('iframe, video')].filter(e => {
      const r = e.getBoundingClientRect(); return r.width > 300 && r.height > 150; })
    .sort((a, b) => (b.tagName === 'IFRAME') - (a.tagName === 'IFRAME') || size(b) - size(a))[0];
  if (!big) return Promise.resolve('no player');
  return big.requestFullscreen().then(() => 'ok', e => 'refused: ' + e.message);
})()"""


def fullscreen_player_frame(driver):
    """Make the player fullscreen without finding its button (works for any server's player)."""
    try:
        res = driver.execute_cdp_cmd("Runtime.evaluate", {"expression": FULLSCREEN_PLAYER_JS, "userGesture": True,
                                                          "awaitPromise": True, "returnByValue": True})
        return (res.get("result") or {}).get("value") == "ok"
    except WebDriverException:
        return False


def click_fullscreen(driver, selector=None):
    """Click the player's fullscreen button and check the page really went fullscreen.

    Tries each likely button (in the page and its iframes) with a real click, then, as a last
    resort, double-clicks the playing video (most players go fullscreen on that). Returns a
    label once the page is fullscreen, or None.
    """
    if is_fullscreen(driver):
        return "already fullscreen"
    try:
        for path in each_frame(driver):
            try:
                targets = driver.execute_script(FULLSCREEN_TARGETS_JS, selector)
            except WebDriverException:
                continue
            for el in targets or []:
                try:
                    label = (el.get_attribute("aria-label") or el.get_attribute("title")
                             or el.get_attribute("data-tooltip") or "fullscreen button").strip()
                    driver.execute_script("arguments[0].setAttribute('data-site-search-fs-tries',"
                                          " +(arguments[0].getAttribute('data-site-search-fs-tries') || 0) + 1);"
                                          "arguments[0].dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));",
                                          el)
                except WebDriverException:
                    continue
                if not real_press(driver, el):
                    continue
                if wait_fullscreen(driver):
                    return label
                print(f"Clicked \"{label}\" but the page didn't go fullscreen; trying something else.")
                for _ in each_frame(driver, depth=0, path=path):
                    break  # back into this button's frame for the next one

        if time.monotonic() - _last_fallback[0] > 5:  # at most every 5 seconds
            _last_fallback[0] = time.monotonic()
            driver.switch_to.default_content()
            # Only once a video has loaded: a dead server's empty player shouldn't count as done.
            if list_on_page(driver, VIDEO_PRESENT_JS) and fullscreen_player_frame(driver) and wait_fullscreen(driver):
                return "the player, made fullscreen directly"
            for _ in each_frame(driver):
                video = driver.execute_script(PLAYING_VIDEO_JS)
                if video is None:
                    continue
                _last_fallback[0] = time.monotonic()
                try:
                    ActionChains(driver).move_to_element(video).double_click().perform()
                except WebDriverException:
                    break
                if wait_fullscreen(driver):
                    return "double-clicked the video"
                break
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return None


# First visible element a selector matches, in page order (so the lowest-numbered of a row
# of buttons, e.g. "Server 1", when they're listed in order).
FIRST_MATCH_JS = """
let els = [];
try { els = Array.from(document.querySelectorAll(arguments[0])); } catch (e) { return null; }
return els.find(el => { const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; }) || null;
"""


# The main video's state: the biggest visible one (not a small preview or ad). Returns
# [area, "playing" | "paused"], or null when there's no video in this frame.
MAIN_VIDEO_JS = """
let best = null;
for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect(), area = r.width * r.height;
    if (area && (!best || area > best[0])) best = [area, v.paused || v.ended ? 'paused' : 'playing'];
}
return best;
"""


def main_video_state(driver):
    """"playing", "paused", or None (no video), for the biggest video on the page or in its iframes."""
    best = None
    try:
        for _ in each_frame(driver):
            found = driver.execute_script(MAIN_VIDEO_JS)
            if found and (best is None or found[0] > best[0]):
                best = found
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return best[1] if best else None


def keep_playing(driver, selector, presses=4, settle=3):
    """Press `selector` only while the video is paused, until it has kept playing for `settle` seconds.

    Play buttons usually also pause, so pressing a fixed number of times can end on pause;
    this looks before every press instead. Returns True if the video ends up playing.
    """
    pressed, steady = 0, 0
    deadline = time.monotonic() + presses * (settle + 2) + 5
    while time.monotonic() < deadline:
        state = main_video_state(driver)
        if state == "playing":
            steady += 1
            if steady >= settle:
                print("Video is playing." + (f" (pressed play {pressed} time(s) to get there)" if pressed else ""))
                return True
        elif state == "paused" and pressed < presses:
            steady = 0
            hit = None
            try:
                for _ in each_frame(driver):
                    hit = driver.execute_script(FIRST_MATCH_JS, selector)
                    if hit is not None:
                        break
                if hit is not None and real_press(driver, hit):
                    pressed += 1
                    print(f"Video was paused, so pressed play ({selector}).")
                elif hit is None and press_player_play(driver):  # another server's player
                    pressed += 1
                    print("Video was paused, so pressed the player's Play.")
                elif hit is None:  # no Play button found: start its video directly
                    for _ in each_frame(driver):
                        video = driver.execute_script(BIGGEST_VIDEO_JS)
                        if video is not None:
                            driver.execute_script("arguments[0].play().catch(() => {});", video)
                            pressed += 1
                            print("Video was paused, so started it.")
                            break
            except WebDriverException:
                pass
            finally:
                driver.switch_to.default_content()
        time.sleep(1)
    print(f"The video still isn't playing after {pressed} press(es) of {selector}.", file=sys.stderr)
    return False


# The biggest visible video in this frame (the main player), or null.
BIGGEST_VIDEO_JS = """
let best = null, bestArea = 0;
for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect(), area = r.width * r.height;
    if (area > bestArea) { best = v; bestArea = area; }
}
return best;
"""

# Whether the player's control bar is showing (ArtPlayer's .art-bottom, or a generic one).
CONTROLS_SHOWING_JS = """
const bar = document.querySelector('.art-bottom, .art-controls, .vjs-control-bar, .plyr__controls, .jw-controlbar');
if (!bar) return false;
const s = getComputedStyle(bar), r = bar.getBoundingClientRect();
return r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity) > 0.1;
"""


def hide_controls(driver, wait=5):
    """Get the player's controls off the screen once the video plays.

    Clicking Unmute leaves the mouse resting on the control bar, which keeps the controls
    showing. So the mouse is moved up onto the picture and left still, which lets the player
    hide them. If they're still showing, the video is paused and played again from code (no
    clicks near the controls), which makes most players hide them.
    """
    try:
        for path in each_frame(driver):
            video = driver.execute_script(BIGGEST_VIDEO_JS)
            if video is None:
                continue
            height = video.size.get("height") or 0
            ActionChains(driver).move_to_element_with_offset(video, 0, -int(height / 4)).perform()
            end = time.monotonic() + wait
            while time.monotonic() < end:
                time.sleep(0.5)
                if not driver.execute_script(CONTROLS_SHOWING_JS):
                    print("Player controls are hidden.")
                    return True
            driver.execute_script("const v = arguments[0]; v.pause(); setTimeout(() => v.play(), 400);", video)
            time.sleep(wait)
            if not driver.execute_script(CONTROLS_SHOWING_JS):
                print("Player controls are hidden (after a quick pause and play).")
                return True
            if driver.execute_script("return arguments[0].paused;", video):
                driver.execute_script("arguments[0].play();", video)
            print("The player's controls are still showing.", file=sys.stderr)
            return False
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return False


def press_after_fullscreen(driver, selector, timeout, tries=1, pause=1.0):
    """Press a button the player shows once it's fullscreen, like Play or Unmute (in the page or an iframe).

    Waits up to `timeout` seconds for it, presses it, then waits `pause` seconds. With tries > 1,
    it's pressed again while still showing (up to `tries` times). Returns how many presses, or 0
    if it never showed.
    """
    presses = 0
    deadline = time.monotonic() + timeout
    try:
        while presses < tries and time.monotonic() < deadline:
            hit = None
            for path in each_frame(driver):
                el = driver.execute_script(FIRST_MATCH_JS, selector)
                if el is not None:
                    hit = el
                    break
            if hit is None:
                if presses:
                    return presses  # it went away: the press worked
                time.sleep(0.5)
                continue
            if real_press(driver, hit):
                presses += 1
                print(f"After fullscreen: pressed {selector}" + (f" (press {presses})." if presses > 1 else "."))
            driver.switch_to.default_content()
            time.sleep(pause)
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return presses


def chrome_auto_shares(port=DEBUG_PORT):
    """Whether the open Chrome was started with the setting that shares the show's tab without asking."""
    line = chrome_command_line(port)
    return "--auto-select-tab-capture-source-by-title" in line and "--disable-background-timer-throttling" in line


def load_rooms():
    try:
        with open(ROOMS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def share_screen(driver, room, share_url, wait=120):
    """Put the show's tab, with its sound, on the TV through Screen Relay, before the show is found.

    An earlier run's show and relay tabs for the same room are closed (other rooms keep going).
    The show's tab is
    given the title SHARE_TAB_TITLE so Chrome picks it by itself (see open_browser); then
    Screen Relay opens in a new tab with the room code, Share screen is clicked, and it comes
    back to the show's tab. Sharing a tab keeps going while that tab moves on to the show. It doesn't wait for the TV: the relay page
    keeps trying to reach it on its own, so the TV can open the room any time. Returns True
    once the screen is shared.
    """
    show_tab = driver.current_window_handle
    relay_host = urlparse(share_url).netloc
    rooms = load_rooms()
    old = set((rooms.get(room) or {}).values())
    for handle in driver.window_handles:  # this room's earlier show and relay tabs (only this room's)
        if handle == show_tab:
            continue
        try:
            if handle not in old:
                driver.switch_to.window(handle)
                url = urlparse(driver.current_url)
                if url.netloc != relay_host or url.fragment.upper() != room:
                    continue
            driver.switch_to.window(handle)
            driver.close()
        except WebDriverException:
            pass
    driver.switch_to.window(show_tab)
    load(driver, f"data:text/html,<title>{SHARE_TAB_TITLE}</title>", 5)
    auto = chrome_auto_shares()
    if not auto:
        print(f"Chrome didn't get the auto-share setting, so it will ask what to share: pick the Chrome tab "
              f"\"{SHARE_TAB_TITLE}\" and keep \"Also share tab audio\" on.")

    # Its own window, so the show's tab stays the one in front of its window: a tab behind
    # another isn't drawn in headless Chrome, and its shared picture would stop.
    driver.switch_to.new_window("window")
    url = share_url.split("#")[0] + "#" + room + (f"&h={HOME_ID}" if HOME_ID else "")
    try:
        load(driver, url)
    except WebDriverException:
        print(f"Couldn't open {share_url}. Is Screen Relay running? (~/Desktop/screenshare/serve.sh)",
              file=sys.stderr)
        driver.close()
        driver.switch_to.window(show_tab)
        return False
    ok = False
    try:
        box = WebDriverWait(driver, 10).until(lambda d: d.find_element(By.ID, "codeInput"))
        box.clear()
        box.send_keys(room)
        button = WebDriverWait(driver, 10, poll_frequency=0.3).until(
            lambda d: d.execute_script("const b = document.getElementById('shareBtn');"
                                       "return b && b.offsetParent ? b : null;"))
        if not real_press(driver, button):  # a real click, since browsers only share the screen after one
            raise WebDriverException("couldn't click Share screen")
        print(f"Sharing to room {room}…")
        # Once the screen is captured the page switches to its sharing view, and from then on it
        # connects to the TV by itself (it keeps trying until a TV opens that room), so there's
        # no need to wait for the TV here. A short wait shows whether it went live straight away.
        state = WebDriverWait(driver, 30 if not auto else 20, poll_frequency=0.5).until(lambda d: d.execute_script(
            "const err = document.getElementById('joinError').textContent;"
            "if (!document.getElementById('home').hidden) return err ? 'error: ' + err : null;"
            "return document.getElementById('sharePill').dataset.state === 'live' ? 'live' : 'sharing';"))
        if state == "sharing":
            end = time.monotonic() + min(wait, 5)
            while state != "live" and time.monotonic() < end:
                time.sleep(0.5)
                state = driver.execute_script(
                    "return document.getElementById('sharePill').dataset.state === 'live' ? 'live' : 'sharing';")
        if state == "live":
            print(f"Screen is live on the TV (room {room}).")
            ok = True
        elif state == "sharing":
            print(f"Screen is shared. No TV on room {room} yet: it goes on the TV as soon as one opens the "
                  f"Screen Relay link ending in #tv={room}.")
            ok = True
        else:
            print(f"Screen sharing didn't start ({state[7:]}). Carrying on with the show.", file=sys.stderr)
    except TimeoutException:
        print("Screen sharing didn't start in time (was the share picker left open?). Carrying on with the show.",
              file=sys.stderr)
    except WebDriverException as e:
        print(f"Screen sharing hit a problem ({str(e.msg or e).splitlines()[0]}). Carrying on with the show.",
              file=sys.stderr)
    relay_tab = driver.current_window_handle
    driver.switch_to.window(show_tab)  # the share tab stays open in the background
    rooms = load_rooms()
    rooms[room] = {"show": show_tab, "relay": relay_tab}
    try:
        with open(ROOMS_FILE, "w") as f:
            json.dump(rooms, f, indent=2)
    except OSError:
        pass
    return ok


def episode_selector(n, season=None):
    """Buttons labelled like "Play Season 24 · Episode 13: Friend's Best Man" (or ending in "Episode 13").

    The ":" / end of label keeps episode 1 from matching episode 11. With a season, the label
    must also say "Season N " (the space keeps season 2 from matching season 24), so a button
    left over from the previous season while the list reloads isn't clicked.
    """
    s = f'[aria-label*="Season {season} " i]' if season else ""
    return f'{s}[aria-label*="Episode {n}:" i], {s}[aria-label$="Episode {n}" i]'


# Finds the season dropdown (a role=listbox that mentions "season": its attributes or its
# options' text) and the option for season arguments[0] (data-value N, or text "Season N").
# Returns [option, already selected?, visible?, the element that opens the dropdown] or null.
FIND_SEASON_JS = """
const n = String(arguments[0]);
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0
    && getComputedStyle(el).visibility !== 'hidden'; };
const mentionsSeason = box => Array.from(box.attributes).some(a => /season/i.test(a.name + ' ' + a.value))
    || /^\\s*season\\b/i.test((box.querySelector('[role=option]') || {}).textContent || '');
for (const box of document.querySelectorAll('[role=listbox]')) {
    if (!mentionsSeason(box)) continue;
    const opt = Array.from(box.querySelectorAll('[role=option]')).find(o =>
        o.getAttribute('data-value') === n
        || o.textContent.replace(/\\s+/g, ' ').trim().toLowerCase() === 'season ' + n);
    if (!opt) continue;
    const selected = opt.getAttribute('aria-selected') === 'true';
    const parent = box.parentElement;
    let trigger = parent && Array.from(parent.querySelectorAll(
        '[aria-haspopup], [aria-expanded], [class*="select_selected"], [class*="selected"], [class*="trigger"], '
        + '[class*="toggle"], button')).find(el => !box.contains(el) && visible(el));
    if (!trigger && box.previousElementSibling && visible(box.previousElementSibling)) trigger = box.previousElementSibling;
    if (!trigger && parent && visible(parent)) trigger = parent;
    return [opt, selected, visible(opt), trigger || null];
}
return null;
"""


def find_season(driver, n):
    """(frame path, [option, selected, visible, trigger]) for season n, or None."""
    try:
        for path in each_frame(driver):
            res = driver.execute_script(FIND_SEASON_JS, n)
            if res:
                return path, res
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return None


def real_click(driver, el):
    try:
        el.click()
    except WebDriverException:
        driver.execute_script("arguments[0].click();", el)  # covered, or hidden in a closed dropdown


def season_step(n, timeout):
    """A step for play_in_steps: pick season n in the site's season dropdown."""
    def step(driver):
        deadline = time.monotonic() + timeout
        found = find_season(driver, n)
        while found is None and time.monotonic() < deadline:
            time.sleep(0.5)
            found = find_season(driver, n)
        if found is None:
            return None
        path, (option, selected, shown, trigger) = found
        if selected:
            return f"Season {n} was already selected"
        # The site redraws the season list as it opens, so the option found a moment ago can be
        # gone by the time it's clicked ("stale"). Find it again and retry, then check it took.
        for attempt in range(4):
            try:
                found = find_season(driver, n) or found
                path, (option, selected, shown, trigger) = found
                if selected:
                    break
                for _ in each_frame(driver, depth=0, path=path):
                    break  # switch into the dropdown's frame
                if not shown and trigger is not None:
                    real_click(driver, trigger)  # open the dropdown
                    time.sleep(0.5)
                    driver.switch_to.default_content()
                    found = find_season(driver, n) or found
                    path, (option, selected, shown, trigger) = found
                    for _ in each_frame(driver, depth=0, path=path):
                        break
                real_click(driver, option)
            except WebDriverException:
                pass  # stale or covered: look it up again
            finally:
                driver.switch_to.default_content()
            time.sleep(1)  # let the episode list reload
            again = find_season(driver, n)
            if again and again[1][1]:  # selected now
                break
        else:
            return None
        return f"picked Season {n}"
    step.description = f"the Season {n} option"
    return step


# Every season in the site's season dropdown, as [number, label] in the order listed.
LIST_SEASONS_JS = """
const mentionsSeason = box => Array.from(box.attributes).some(a => /season/i.test(a.name + ' ' + a.value))
    || /^\\s*season\\b/i.test((box.querySelector('[role=option]') || {}).textContent || '');
for (const box of document.querySelectorAll('[role=listbox]')) {
    if (!mentionsSeason(box)) continue;
    const out = [];
    for (const o of box.querySelectorAll('[role=option]')) {
        const label = o.textContent.replace(/\\s+/g, ' ').trim();
        const m = (o.getAttribute('data-value') || '').match(/^\\d+$/) || label.match(/season\\s*(\\d+)/i);
        if (m) out.push([parseInt(m[1] || m[0], 10), label]);
    }
    if (out.length) return out;
}
return null;
"""

# Every episode button showing (labelled like "Play Season 3 · Episode 4: One If by Clam"), as
# [number, label] in order. arguments[0]: only that season's, when given.
LIST_EPISODES_JS = """
const season = arguments[0];
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
const out = [], seen = new Set();
for (const el of document.querySelectorAll('[aria-label*="Episode" i]')) {
    const label = (el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    const m = label.match(/episode\\s*(\\d+)/i);
    if (!m || !visible(el)) continue;
    const sm = label.match(/season\\s*(\\d+)/i);
    if (season != null && (!sm || parseInt(sm[1], 10) !== season)) continue;
    const n = parseInt(m[1], 10);
    if (seen.has(n)) continue;
    seen.add(n);
    out.push([n, label.replace(/^play\\s+/i, '')]);
}
return out;
"""


def list_on_page(driver, script, *args):
    """What `script` returns in the page or the first of its iframes that has an answer."""
    try:
        for _ in each_frame(driver):
            found = driver.execute_script(script, *args)
            if found:
                return found
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return None


def save_now_playing(playing, season, episode, seasons, episodes):
    """Record the TV show playing in a room, for the phone's season/episode switcher."""
    try:
        os.makedirs(NOW_DIR, exist_ok=True)
        with open(os.path.join(NOW_DIR, f"{playing['room']}.json"), "w") as f:
            json.dump({"room": playing["room"], "title": playing.get("title", ""), "site": playing.get("site", ""),
                       "show_url": playing.get("show_url", ""), "season": season, "episode": episode,
                       "seasons": [[n, label] for n, label in seasons],
                       "episodes": [[n, label.split(":", 1)[1].strip() if ":" in label else ""] for n, label in episodes]},
                      f)
    except OSError:
        pass


# The player's time readout ("21:38 / 21:38": played / total) as [played, total] seconds,
# and whether its video says it has ended. ArtPlayer's .art-control-time first, otherwise any
# short "m:ss / m:ss" text. null when this frame has no player.
PLAYER_TIME_JS = """
const secs = t => t.split(':').reduce((a, b) => a * 60 + (parseInt(b, 10) || 0), 0);
const re = /^\\s*(\\d{1,2}(?::\\d{2}){1,2})\\s*\\/\\s*(\\d{1,2}(?::\\d{2}){1,2})\\s*$/;
let m = null;
const el = document.querySelector('.art-control-time');
if (el) m = (el.textContent || '').match(re);
if (!m) for (const e of document.querySelectorAll('[class*=time i], [class*=duration i]')) {
    if (e.children.length) continue;
    m = (e.textContent || '').match(re);
    if (m) break;
}
let ended = false, best = 0;
for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect(), area = r.width * r.height;
    if (area > best) { best = area; ended = v.ended && v.duration > 60; }
}
if (!m && !best) return null;
if (!m) {  // no time readout (another server's player): use the video's own position and length
    const v = [...document.querySelectorAll('video')].sort((a, b) => {
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        return rb.width * rb.height - ra.width * ra.height; })[0];
    if (v && v.duration > 0 && isFinite(v.duration)) return [Math.floor(v.currentTime), Math.floor(v.duration), ended];
}
return [m ? secs(m[1]) : -1, m ? secs(m[2]) : -1, ended];
"""


def next_episode(now):
    """(season, episode) after what's recorded as playing, or None after the last one."""
    numbers = [n for n, _ in now.get("episodes") or []]
    season, episode = now.get("season"), now.get("episode")
    later = [n for n in numbers if n > (episode or 0)]
    if later:
        return season, min(later)
    seasons = [n for n, _ in now.get("seasons") or []]
    next_seasons = [n for n in seasons if season is not None and n > season]
    return (min(next_seasons), 1) if next_seasons else None


def restart_playback(driver, wait=25):
    """After switching servers mid-show: get the new server's video playing and fullscreen."""
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if list_on_page(driver, VIDEO_PRESENT_JS) and video_playing(driver):
            break
        press_player_play(driver)
        time.sleep(2)
    for _ in range(10):
        if click_fullscreen(driver):
            break
        time.sleep(1)
    for _ in each_frame(driver):  # still paused: start it directly
        video = driver.execute_script(BIGGEST_VIDEO_JS)
        if video is not None:
            driver.execute_script("if (arguments[0].paused) arguments[0].play().catch(() => {});", video)
            break
    driver.switch_to.default_content()
    hide_controls(driver)


# The biggest video in this frame (the show) as [position, length, paused], or null.
PLAYBACK_JS = """
let best = null, area = 0;
for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect(), a = r.width * r.height;
    if (a > area && (v.readyState > 0 || v.currentSrc)) { best = v; area = a; }
}
if (!best) return null;
return [best.currentTime || 0, isFinite(best.duration) ? best.duration : 0, best.paused];
"""

# Plays, pauses or jumps the biggest video in this frame. arguments[0]: "play", "pause",
# "toggle" or "seek"; arguments[1]: the position (seconds) for "seek". Returns its new
# [position, length, paused], or null when this frame has no video.
CONTROL_JS = """
const action = arguments[0], to = arguments[1];
let v = null, area = 0;
for (const x of document.querySelectorAll('video')) {
    const r = x.getBoundingClientRect(), a = r.width * r.height;
    if (a > area && (x.readyState > 0 || x.currentSrc)) { v = x; area = a; }
}
if (!v) return null;
if (action === 'seek' && isFinite(to)) v.currentTime = Math.max(0, Math.min(to, (v.duration || to) - 1));
if (action === 'play' || (action === 'toggle' && v.paused)) v.play().catch(() => {});
else if (action === 'pause' || action === 'toggle') v.pause();
return [v.currentTime || 0, isFinite(v.duration) ? v.duration : 0, v.paused];
"""


def save_room_info(room, title, kind):
    """Record what a room is playing (title, TV or movie), so Run on that room can say so."""
    if not room:
        return
    try:
        os.makedirs(NOW_DIR, exist_ok=True)
        with open(os.path.join(NOW_DIR, f"{room.upper()}.info.json"), "w") as f:
            json.dump({"title": title, "kind": kind}, f)
    except OSError:
        pass


def save_playback(room, state):
    """Record where the room's video is, for the phone's progress bar and Pause/Play button."""
    if not room:
        return
    try:
        position, length, paused = state
        with open(os.path.join(NOW_DIR, f"{room}.playback.json"), "w") as f:
            json.dump({"time": round(position, 1), "duration": round(length, 1), "paused": bool(paused),
                       "at": time.time()}, f)
    except (OSError, TypeError, ValueError):
        pass


def take_control_request(room):
    """A Pause/Play or jump sent from the phone for this room (and clear it), or None."""
    path = os.path.join(NOW_DIR, f"{room}.control.json")
    try:
        with open(path) as f:
            request = json.load(f)
        os.remove(path)
        if request.get("action") in ("play", "pause", "toggle", "seek"):
            return request
    except (OSError, ValueError, AttributeError):
        pass
    return None


def apply_control(driver, request):
    """Carry out a phone's Pause/Play or jump on the show's video (in whichever frame it is)."""
    action, to = request["action"], request.get("time")
    try:
        to = float(to) if to is not None else None
    except (TypeError, ValueError):
        to = None
    try:
        for _ in each_frame(driver):
            state = driver.execute_script(CONTROL_JS, action, to)
            if state:
                return state
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    if action in ("play", "toggle"):
        click_player_middle(driver)  # no video to talk to directly: press the player instead
    return None


def play_next_when_done(driver, room, check_every=1, next_episodes=True):
    """Keep watching the episode playing in this room; when it ends, switch the room to the next one.

    The end is when the player's readout shows the same time twice ("21:38 / 21:38"), or its
    video says it ended, but only after it was seen playing an episode over a minute long (a
    player still loading can show "00:00 / 00:00"). The switch goes through the builder's
    helper, like the phone's season/episode dropdowns, so the TV and phones stay on the room.
    Stops if the show's tab closes or the room switches to something else.
    """
    now_file = os.path.join(NOW_DIR, f"{room}.json")
    now = None
    if next_episodes:  # a TV show: what's playing, for the next episode
        try:
            with open(now_file) as f:
                now = json.load(f)
        except (OSError, ValueError):
            now = None
    show_tab = driver.current_window_handle
    if now:
        print(f"Watching Season {now.get('season')} · Episode {now.get('episode')}; the next one starts when it ends.")
    else:
        print("Watching; a server picked on the phone switches to it.")
    seen_playing = False
    errors = 0
    while True:
        time.sleep(check_every)
        wanted = take_server_request(room)
        if wanted:
            try:
                driver.switch_to.window(show_tab)
                switched = select_server(driver, wanted)
            except WebDriverException:
                switched = None
            if switched:
                print(f"Switched to {switched}, as picked on the phone.")
                save_servers(driver, room)
                try:
                    restart_playback(driver)
                except WebDriverException:
                    driver.switch_to.default_content()  # keep watching; the phone can pick again
                seen_playing = False
            continue
        if now:
            try:
                with open(now_file) as f:
                    current = json.load(f)
                if (current.get("season"), current.get("episode"), current.get("show_url")) != \
                        (now.get("season"), now.get("episode"), now.get("show_url")):
                    print("Stopped watching: the room switched to something else.")
                    return
            except (OSError, ValueError):
                print("Stopped watching: the room's now-playing record is gone.")
                return
        try:
            if show_tab not in driver.window_handles:
                print("Stopped watching: the show's window closed.")
                return
            driver.switch_to.window(show_tab)
            control = take_control_request(room)
            if control:
                state = apply_control(driver, control)
                what = {"play": "Played", "pause": "Paused", "toggle": "Paused/played",
                        "seek": "Jumped"}[control["action"]]
                print(f"{what} from the phone" + (f" (to {int(control.get('time') or 0) // 60}:"
                                                  f"{int(control.get('time') or 0) % 60:02d})" if control["action"] == "seek" else "") + ".")
                if state:
                    save_playback(room, state)
                if control["action"] == "seek":
                    seen_playing = False  # a jump near the end shouldn't count as the episode ending
            playback = list_on_page(driver, PLAYBACK_JS)
            if playback:
                save_playback(room, playback)
            if not now:
                continue  # a movie: nothing plays next
            readout = list_on_page(driver, PLAYER_TIME_JS)
        except WebDriverException as e:
            errors += 1
            if errors >= 15:  # about 30 seconds of nothing but errors: Chrome is gone
                print(f"Stopped watching: Chrome keeps failing ({str(e.msg or e).splitlines()[0][:80]}).")
                return
            continue
        errors = 0
        if not readout:
            continue
        played, total, ended = readout
        if total >= 60 and 0 <= played < total:
            seen_playing = True
        if seen_playing and ((total >= 60 and played >= total) or ended):
            break
    nxt = next_episode(now)
    if nxt is None:
        print("That was the last episode, so nothing plays next.")
        return
    season, episode = nxt
    print(f"Episode over. Playing Season {season} · Episode {episode} next…")
    request = urllib.request.Request(
        "http://localhost:8766/switch", data=json.dumps({"room": room, "season": season, "episode": episode}).encode(),
        headers={"Content-Type": "application/json", "Origin": "http://localhost:8766"}, method="POST")
    try:
        urllib.request.urlopen(request, timeout=10).read()  # the new run replaces this one
    except OSError as e:
        print(f"Couldn't start the next episode: the builder's helper didn't answer ({e}).", file=sys.stderr)


def ask_episode_steps(timeout, season=None, episode=None, playing=None):
    """Steps for play_in_steps that pick the season, then the episode, asking the phone if needed.

    The season dropdown and episode buttons show up after the intro Play, so these go right
    after it. A show without a season dropdown skips straight to its episodes. A season or
    episode given (switching from the phone's dropdowns) is picked without asking.
    playing ({room, title, site, show_url}): once the episode is picked, what's playing and
    the season and episode lists are saved to NOW_DIR/<room>.json for the phone's switcher.
    """
    chosen = {"seasons": []}
    want_season, want_episode = season, episode

    def season(driver):
        deadline = time.monotonic() + timeout
        seasons = list_on_page(driver, LIST_SEASONS_JS)
        while not seasons and time.monotonic() < deadline:
            time.sleep(0.5)
            seasons = list_on_page(driver, LIST_SEASONS_JS)
        if not seasons:
            return "no season list, so going by episodes"
        chosen["seasons"] = seasons
        if want_season:
            n = want_season
        elif len(seasons) == 1:
            n = seasons[0][0]
        else:
            print(f"Found {len(seasons)} seasons. Pick one on the builder page…")
            i = ask_phone("season", "Which season?", [{"title": label} for _, label in seasons])
            if i is None:
                return None
            n = seasons[i][0]
        chosen["season"] = n
        return season_step(n, timeout)(driver)
    season.description = "the season list"

    def episode(driver):
        n_season = chosen.get("season")
        deadline = time.monotonic() + timeout
        episodes = list_on_page(driver, LIST_EPISODES_JS, n_season)
        while not episodes and time.monotonic() < deadline:
            time.sleep(0.5)
            episodes = list_on_page(driver, LIST_EPISODES_JS, n_season)
        if not episodes:
            return None
        numbers = [n for n, _ in episodes]
        if want_episode in numbers:
            i = numbers.index(want_episode)
        elif want_episode is not None and want_episode <= min(numbers):
            # "Episode 1" of a season that numbers them differently (e.g. from 0, or listing only
            # later ones): its first episode, so playing on into the next season never stops to ask.
            i = numbers.index(min(numbers))
        else:
            print(f"Found {len(episodes)} episodes. Pick one on the builder page…")
            items = []
            for n, label in episodes:
                title = label.split(":", 1)[1].strip() if ":" in label else ""
                items.append({"title": f"Episode {n}", "subtitle": title})
            i = ask_phone("episode", f"Which episode?" + (f" (Season {n_season})" if n_season else ""), items)
            if i is None:
                return None
        hit = find_step(driver, episode_selector(episodes[i][0], n_season))
        if hit is None:
            return None
        if playing and playing.get("room"):
            save_now_playing(playing, n_season, episodes[i][0], chosen["seasons"], episodes)
        return f"pressed \"{click_play(driver, *hit)}\""
    episode.description = "the episode list"
    return [season, episode]


def find_step(driver, selector):
    """(frame path, element) of the first visible match for selector, in the page or its iframes."""
    try:
        for path in each_frame(driver):
            el = driver.execute_script(FIRST_MATCH_JS, selector)
            if el is not None:
                return path, el
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()
    return None


SERVER_WAIT = 7  # seconds a video server gets to show a video before the next one is tried

# Whether this frame has a video with something to play. A dead server's player has none, and
# another server's player has an empty one until its own Play button is pressed.
# A video that has something to play (a source, or data loaded). An empty player has none.
VIDEO_PRESENT_JS = "return [...document.querySelectorAll('video')].some(v => v.readyState > 0 || !!v.currentSrc);"

# The site's video-server buttons, in order, as [element, selected?]: the configuration's
# server_button_selector if set, otherwise buttons or tabs labelled "Server 1", "Server 2", ...
SERVER_BUTTONS_JS = """
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
const selector = arguments[0];
let buttons = [];
if (selector) { try { buttons = [...document.querySelectorAll(selector)]; } catch (e) {} }
if (!buttons.length) buttons = [...document.querySelectorAll('button, [role=tab], a')]
    .filter(b => /^\\s*server\\s*\\d+\\s*$/i.test(b.textContent || ''));
return buttons.filter(visible).map(b => [b, b.classList.contains('is-active') || b.getAttribute('aria-selected') === 'true']);
"""


def press_player_play(driver):
    """Press the play button inside the video player's frame (not the site's own page, whose
    episode buttons also say "Play"), using the general play-button finder. Returns its label."""
    best = None
    try:
        for path in each_frame(driver):
            if not path:
                continue  # the site's page itself
            try:
                el, found = driver.execute_script(FIND_PLAY_JS, False)
            except WebDriverException:
                continue
            if el is not None and (best is None or found[0][:2] > best[2][0][:2]):
                best = (path, el, found)
    except WebDriverException:
        pass
    driver.switch_to.default_content()
    if best is None:
        return click_player_middle(driver)
    try:
        return click_play(driver, best[0], best[1])
    except WebDriverException:  # the player reloaded under it (e.g. right after a server switch)
        driver.switch_to.default_content()
        return None


def click_player_middle(driver):
    """Click the middle of the player, where most players put their start button (often just a
    ▶ icon with no label to find). Only while nothing is playing, so it can't pause a video."""
    if video_playing(driver):
        return None
    driver.switch_to.default_content()
    try:
        frame = driver.execute_script("""return [...document.querySelectorAll('iframe')].filter(f => {
            const r = f.getBoundingClientRect(); return r.width > 300 && r.height > 150; })
          .sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height
                        - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0] || null;""")
        if frame is None:
            return None
        driver.execute_script(REMOVE_AD_OVERLAYS_JS)
        ActionChains(driver).move_to_element(frame).click().perform()
        time.sleep(1)
        return "the middle of the player"
    except WebDriverException:
        return None


def player_on_screen(driver):
    """Whether the site's video player (a visible iframe of a decent size) is showing."""
    driver.switch_to.default_content()
    try:
        return bool(driver.execute_script("return [...document.querySelectorAll('iframe')].some(f => {"
                                          " const r = f.getBoundingClientRect(); return r.width > 300 && r.height > 150; });"))
    except WebDriverException:
        return False


def save_servers(driver, room):
    """Record the site's servers and which is selected, for the phone's server dropdown."""
    if not room:
        return
    driver.switch_to.default_content()
    try:
        buttons = driver.execute_script(SERVER_BUTTONS_JS, SERVER_BUTTON_SELECTOR) or []
        labels = [(b.text or f"Server {i}").strip().title() for i, (b, _) in enumerate(buttons, 1)]
        active = next((i for i, (_, selected) in enumerate(buttons, 1) if selected), None)
    except WebDriverException:
        return
    if not labels:
        return
    try:
        os.makedirs(NOW_DIR, exist_ok=True)
        with open(os.path.join(NOW_DIR, f"{room}.servers.json"), "w") as f:
            json.dump({"servers": labels, "server": active}, f)
    except OSError:
        pass


def take_server_request(room):
    """A server number picked on the phone for this room (and clear the request), or None."""
    if not room:
        return None
    path = os.path.join(NOW_DIR, f"{room}.server-request.json")
    try:
        with open(path) as f:
            wanted = int(json.load(f)["server"])
        os.remove(path)
        return wanted
    except (OSError, ValueError, KeyError, TypeError):
        return None


def select_server(driver, number):
    """Switch to server `number` (1-based): scrolled into view, clicked for real, and pressed
    directly if the site doesn't select it. Returns its label if it ended up selected."""
    driver.switch_to.default_content()
    try:
        buttons = driver.execute_script(SERVER_BUTTONS_JS, SERVER_BUTTON_SELECTOR) or []
    except WebDriverException:
        return None
    if not 1 <= number <= len(buttons):
        return None
    button = buttons[number - 1][0]
    is_selected = ("return arguments[0].classList.contains('is-active')"
                   " || arguments[0].getAttribute('aria-selected') === 'true';")
    try:
        label = (button.text or f"Server {number}").strip()
        if driver.execute_script(is_selected, button):
            return label
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
        real_press(driver, button)
        time.sleep(1)
        if not driver.execute_script(is_selected, button):
            driver.execute_script("arguments[0].click();", button)
            time.sleep(1)
        return label if driver.execute_script(is_selected, button) else None
    except WebDriverException:
        return None


def next_server(driver, tried):
    """Switch to the next video server not tried yet (in the top page). Returns its label, or None.

    tried is the set of server numbers already tried (1-based); the selected one is added to it.
    The button is scrolled into view and clicked for real; if the site doesn't select it (the
    click hit something on top of it), it's pressed directly instead. Each server is tried once.
    """
    driver.switch_to.default_content()
    try:
        buttons = driver.execute_script(SERVER_BUTTONS_JS, SERVER_BUTTON_SELECTOR) or []
    except WebDriverException:
        return None
    for i, (_, selected) in enumerate(buttons, 1):
        if selected:
            tried.add(i)
    for i, (button, _) in enumerate(buttons, 1):
        if i in tried:
            continue
        tried.add(i)
        label = (button.text or f"Server {i}").strip()
        is_selected = ("return arguments[0].classList.contains('is-active')"
                       " || arguments[0].getAttribute('aria-selected') === 'true';")
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", button)
            real_press(driver, button)
            time.sleep(1)
            if not driver.execute_script(is_selected, button):
                driver.execute_script("arguments[0].click();", button)
                time.sleep(1)
            if driver.execute_script(is_selected, button):
                return label
        except WebDriverException:
            pass
    return None


def play_in_steps(driver, selectors, timeout, debug=False, limit=PLAY_LOOP_SECONDS, retry_every=5,
                  fs_selector=None, room=None):
    """Press each selector's button once, in order, then get to fullscreen.

    E.g. intro Play, then a server, then the real Play. A step that doesn't show up within
    `timeout` seconds is skipped. Then: wait up to `retry_every` seconds for a fullscreen
    button and click it; if none shows, press the last step's button again, and repeat
    (for up to `limit` seconds). Once a video is playing, the last button isn't pressed again
    (it's usually also Pause); the mouse is moved over the player instead to show its controls.
    Returns (labels pressed, fullscreen label or None).
    """
    pressed = []
    try:
        for _ in each_frame(driver):
            driver.execute_script(MARK_PLAYING_JS)
    except WebDriverException:
        pass
    driver.switch_to.default_content()
    fullscreen_button(driver, mark=True)

    for n, sel in enumerate(selectors, 1):
        if callable(sel):  # a step that does its own work, like picking a season
            try:
                label = sel(driver)
            except WebDriverException as e:  # one step's trouble shouldn't end the whole run
                driver.switch_to.default_content()
                print(f"Step {n}: Chrome error ({str(e.msg or e).splitlines()[0][:80]}).")
                label = None
            if label is None:
                print(f"Step {n}: couldn't find {sel.description} within {timeout:g}s, so it was skipped.")
            else:
                pressed.append(label)
                print(f"Step {n}: {label}.")
            continue
        started = time.monotonic()
        deadline = started + timeout
        hit = find_step(driver, sel)
        while hit is None and time.monotonic() < deadline:
            time.sleep(0.5)
            if n == len(selectors) and time.monotonic() - started > 3 and player_on_screen(driver):
                break  # the last button isn't needed: the player is already showing
            hit = find_step(driver, sel)
        if hit is None:
            print(f"Step {n}: nothing matched {sel}, so it was skipped.")
            continue
        label = click_play(driver, *hit, debug=debug)
        pressed.append(label)
        print(f"Step {n}: pressed \"{label}\".")
    if not pressed:
        return pressed, None

    last = next((sel for sel in reversed(selectors) if not callable(sel)), None)  # pressed again on retries
    give_up = time.monotonic() + limit
    server_since = time.monotonic()  # when the current video server was picked
    servers_tried = set()
    save_servers(driver, room)
    while time.monotonic() < give_up:
        wait_until = min(time.monotonic() + retry_every, give_up)
        switched = None
        while time.monotonic() < wait_until:
            fs = click_fullscreen(driver, fs_selector)
            if fs:
                return pressed, fs
            # A server picked on the phone, or a dead server (the player stays empty: no video
            # ever shows up), means another server: the site's "Server 2", "Server 3", ... buttons.
            wanted = take_server_request(room)
            if wanted:
                switched = select_server(driver, wanted)
                if switched:
                    print(f"Switched to {switched}, as picked on the phone.")
                    servers_tried.add(wanted)
            elif time.monotonic() - server_since > SERVER_WAIT and not list_on_page(driver, VIDEO_PRESENT_JS):
                switched = next_server(driver, servers_tried)
                if switched:
                    print(f"No video from this server after {SERVER_WAIT}s, so switched to {switched}.")
            if switched:
                save_servers(driver, room)
                server_since = time.monotonic()
                give_up = max(give_up, time.monotonic() + 30)  # give the new server time
                break
            time.sleep(0.5)
        if switched:
            press_player_play(driver)  # many servers' players need their own Play pressed
            continue
        if video_playing(driver):
            hover_player(driver)
            continue
        hit = find_step(driver, last) if last else None
        if hit is None:
            label = press_player_play(driver)  # another server's player has its own Play button
            if label:
                print(f"No fullscreen button yet; pressed the player's \"{label}\".")
            else:
                print(f"No fullscreen button after {retry_every:g}s, and {last} isn't showing to press again.")
            continue
        label = click_play(driver, *hit, debug=debug)
        pressed.append(label)
        print(f"No fullscreen button after {retry_every:g}s. Pressed \"{label}\" again.")
    return pressed, None


def hover_player(driver):
    """Move the mouse over a playing video so the player shows its controls."""
    try:
        for _ in each_frame(driver):
            video = driver.execute_script(PLAYING_VIDEO_JS)
            if video is not None:
                ActionChains(driver).move_to_element(video).move_by_offset(10, 10).move_by_offset(-10, -10).perform()
                return
    except WebDriverException:
        pass
    finally:
        driver.switch_to.default_content()


# --teach: records what the user clicks. Each click's button is described by its tag, id,
# test id, label, text, classes, position in the page (CSS path) and the frame's host, and
# kept in sessionStorage so it survives the page navigating away on the click.
TEACH_LISTEN_JS = """
if (window.__ssTeachOn) return;
window.__ssTeachOn = true;
const norm = t => (t || '').replace(/\\s+/g, ' ').trim();
const pointer = el => el && el.nodeType === 1 && getComputedStyle(el).cursor === 'pointer';
const clickable = el => el.matches('button, a[href], [role=button], [onclick], input[type=button], input[type=submit]')
    || (pointer(el) && !pointer(el.parentElement));
const cssPath = el => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body && parts.length < 8) {
        if (el.id && !/\\d{3,}/.test(el.id)) { parts.unshift('#' + CSS.escape(el.id)); break; }
        let i = 1, sib = el;
        while ((sib = sib.previousElementSibling)) if (sib.tagName === el.tagName) i++;
        parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
        el = el.parentElement;
    }
    return parts.join(' > ');
};
const state = /^(is-|has-)?(active|hover|hovered|focus|focused|visible|hidden|show|shown|open|playing|paused)$/i;
document.addEventListener('pointerdown', e => {
    let t = e.target;
    while (t && t !== document.body && !clickable(t)) t = t.parentElement;
    if (!t || t === document.body) t = e.target;
    const fp = { tag: t.tagName.toLowerCase(), id: t.id || '',
        testid: t.getAttribute('data-testid') || t.getAttribute('data-test') || t.getAttribute('data-qa') || '',
        aria: norm(t.getAttribute('aria-label')), title: norm(t.getAttribute('title')),
        text: norm(t.innerText).slice(0, 60), classes: Array.from(t.classList).filter(c => !state.test(c)),
        path: cssPath(t), host: location.host };
    try {
        const saved = JSON.parse(sessionStorage.getItem('__ssTeach') || '[]');
        saved.push(fp);
        sessionStorage.setItem('__ssTeach', JSON.stringify(saved));
    } catch (err) { (window.__ssTeach = window.__ssTeach || []).push(fp); }
}, true);
"""

TEACH_READ_JS = """
let out = window.__ssTeach || [];
window.__ssTeach = [];
try { out = out.concat(JSON.parse(sessionStorage.getItem('__ssTeach') || '[]'));
      sessionStorage.removeItem('__ssTeach'); } catch (err) {}
return out;
"""

BANNER_JS = """
let b = document.getElementById('__ss_banner');
if (arguments[0] === null) { if (b) b.remove(); return; }
if (!b) {
    b = document.createElement('div');
    b.id = '__ss_banner';
    Object.assign(b.style, { position: 'fixed', top: '0', left: '0', right: '0', zIndex: '2147483647',
        background: '#2f6fed', color: '#fff', font: '600 15px -apple-system, sans-serif',
        padding: '10px 16px', textAlign: 'center', pointerEvents: 'none' });
    document.documentElement.appendChild(b);
}
b.textContent = arguments[0];
"""

# Rules with a "selector" (from --play-selector, copied out of Chrome's Inspect panel) match
# exactly the elements that selector finds. They're steps in order (e.g. an intro Play, then
# the real one): a step not pressed yet goes first, earliest first; when pressing again,
# the latest step goes first. Other rules (from --teach) are scored:
# Scores every visible element against the taught buttons; 6+ counts as a match. A fake
# button that merely says "Play" scores low: it differs in classes, position and usually
# lives in another frame (an ad), which costs points.
MATCH_TAUGHT_JS = """
const rules = arguments[0], skipPressed = arguments[1];
const norm = t => (t || '').replace(/\\s+/g, ' ').trim();
const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0
    && getComputedStyle(el).visibility !== 'hidden'; };
const pointer = el => el && el.nodeType === 1 && getComputedStyle(el).cursor === 'pointer';
const clickable = el => el.matches('button, a[href], [role=button], [onclick], input[type=button], input[type=submit]')
    || (pointer(el) && !pointer(el.parentElement));
const scored = [];
rules.filter(r => r.selector).forEach((r, step) => {
    let els = [];
    try { els = Array.from(document.querySelectorAll(r.selector)); } catch (e) {}
    for (const el of els) {
        const marked = !!el.closest('[data-site-search-pressed]');
        if ((skipPressed && marked) || !visible(el) || scored.some(f => f.el === el)) continue;
        scored.push({ el, best: marked ? 100 + step : 200 - step });
    }
});
const fingerprints = rules.filter(r => !r.selector);
const byPath = fingerprints.map(r => { try { return document.querySelector(r.path); } catch (e) { return null; } });
for (const el of document.body.querySelectorAll('*')) {
    const marked = !!el.closest('[data-site-search-pressed]');
    if (skipPressed && marked) continue;
    const onPath = byPath.includes(el);
    if (!onPath && !clickable(el)) continue;
    if (!visible(el)) continue;
    const tid = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-qa') || '';
    const cls = new Set(el.classList);
    let best = -99;
    if (!fingerprints.length) break;
    fingerprints.forEach((r, i) => {
        let s = byPath[i] === el ? 3 : 0;
        if (r.id && el.id === r.id) s += 4;
        if (r.testid && tid === r.testid) s += 4;
        if (r.aria && norm(el.getAttribute('aria-label')) === r.aria) s += 2;
        if (r.title && norm(el.getAttribute('title')) === r.title) s += 1;
        if (r.text && norm(el.innerText).slice(0, 60) === r.text) s += 2;
        const union = new Set([...r.classes, ...cls]).size;
        if (union) s += 3 * r.classes.filter(c => cls.has(c)).length / union;
        s += el.tagName.toLowerCase() === r.tag ? 1 : -2;
        s += location.host === r.host ? 1 : -3;
        if (marked) s -= 1;
        best = Math.max(best, s);
    });
    if (best >= 3) scored.push({ el, best });
}
scored.sort((a, b) => b.best - a.best);
const label = el => norm(el.getAttribute('aria-label') || el.innerText || el.getAttribute('title')).slice(0, 60);
return [scored.length && scored[0].best >= 6 ? scored[0].el : null,
        scored.slice(0, 8).map(f => [Math.round(f.best * 10) / 10, 0, label(f.el), f.el.outerHTML.slice(0, 200)])];
"""


def site_key(url):
    """What saved settings are filed under for a site: a short code made from its address
    (not the address itself, so the saved settings file doesn't name the site)."""
    host = (urlparse(url).hostname or url).lower()
    host = host[4:] if host.startswith("www.") else host
    return "site-" + hashlib.sha256(host.encode()).hexdigest()[:12]


# The site configuration (site-config.json, pasted in with the builder's "Import settings"):
# used when nothing is saved for a site and none are given on the command line. Its keys:
#   site, play_selectors, fullscreen_selector, after_fullscreen_selectors,
#   keep_playing_selector, server_button_selector, page_fix_query, room
SITE_CONFIG_FILE = os.path.join(LOG_DIR, "site-config.json")


def site_config():
    try:
        with open(SITE_CONFIG_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


SITE_CONFIG = site_config()
if SITE_CONFIG.get("site"):
    DEFAULT_URL = SITE_CONFIG["site"] if "://" in SITE_CONFIG["site"] else "https://" + SITE_CONFIG["site"]
DEFAULT_PLAY_SELECTORS = [x for x in SITE_CONFIG.get("play_selectors") or [] if isinstance(x, str) and x.strip()]
DEFAULT_FULLSCREEN_SELECTOR = SITE_CONFIG.get("fullscreen_selector") or None
DEFAULT_AFTER_SELECTORS = [x for x in SITE_CONFIG.get("after_fullscreen_selectors") or [] if isinstance(x, str) and x.strip()]
DEFAULT_KEEP_SELECTOR = SITE_CONFIG.get("keep_playing_selector") or None
SERVER_BUTTON_SELECTOR = SITE_CONFIG.get("server_button_selector") or ""
PAGE_FIX_QUERY = (SITE_CONFIG.get("page_fix_query") or "").lstrip("?&")


def load_taught():
    try:
        with open(TAUGHT_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_taught(site, buttons):
    data = load_taught()
    data[site] = buttons
    with open(TAUGHT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def teach_play(driver, limit):
    """Record the buttons the user clicks until the fullscreen button shows (or `limit` seconds)."""
    print("Teach mode: in Chrome, click the REAL play button yourself, as many times as it takes. "
          f"It stops once the player's fullscreen button shows (or after {limit:g}s).")
    fullscreen_button(driver, mark=True)
    taught, windows = [], set(driver.window_handles)
    give_up = time.monotonic() + limit
    while time.monotonic() < give_up:
        new_windows = set(driver.window_handles) - windows
        if new_windows:  # the click opened the player in a new tab
            windows |= new_windows
            driver.switch_to.window(new_windows.pop())
        try:
            for path in each_frame(driver):
                driver.execute_script(TEACH_LISTEN_JS)
                if not path:
                    driver.execute_script(BANNER_JS, "site-search is learning: click the real Play button")
                for fp in driver.execute_script(TEACH_READ_JS) or []:
                    taught.append(fp)
                    name = fp["aria"] or fp["text"] or fp["title"] or "no label"
                    print(f"Recorded click {len(taught)}: <{fp['tag']}> \"{name}\"")
        except WebDriverException:
            pass  # page changing under us; try again
        driver.switch_to.default_content()
        if taught and fullscreen_button(driver):
            print("Fullscreen button is showing. Done learning.")
            break
        time.sleep(0.4)
    try:
        driver.execute_script(BANNER_JS, None)
    except WebDriverException:
        pass
    unique = []
    for fp in taught:  # the same button clicked twice only needs saving once
        if not any(fp["path"] == u["path"] and fp["host"] == u["host"] and fp["text"] == u["text"] for u in unique):
            unique.append(fp)
    return unique


def start_playback(driver, timeout, debug=False, limit=PLAY_LOOP_SECONDS, rules=None, fs_selector=None):
    """Press Play until the player's fullscreen button shows up, for up to `limit` seconds.

    Each round: press the best play button (one not pressed yet is preferred; after a few
    seconds the same one may be pressed again), then wait a few seconds for a fullscreen
    button. Once a video is playing, Play isn't pressed again (it's usually also the Pause
    button); the mouse is moved over the player instead, which is what reveals its controls.
    Then clicks it. Returns (labels of what was pressed, label of the fullscreen button or None).
    """
    pressed = []
    try:
        for _ in each_frame(driver):
            driver.execute_script(MARK_PLAYING_JS)
    except WebDriverException:
        pass
    driver.switch_to.default_content()
    fullscreen_button(driver, mark=True)

    give_up = time.monotonic() + limit
    shown_debug = 0
    while time.monotonic() < give_up:
        if pressed:
            fs = click_fullscreen(driver, fs_selector)
            if fs:
                return pressed, fs

        if pressed and video_playing(driver):
            hover_player(driver)
        else:
            hit, found = None, []
            deadline = min(time.monotonic() + timeout, give_up)
            allow_same = time.monotonic() + min(4, timeout / 2)  # leave time to press the same one again
            while time.monotonic() < deadline:
                hit, found = find_play(driver, skip_pressed=time.monotonic() < allow_same, rules=rules)
                if hit or (pressed and (fullscreen_button(driver) or video_playing(driver))):
                    break
                time.sleep(0.5)
            if debug and shown_debug < 3:  # the list rarely changes after a few rounds
                shown_debug += 1
                print(f"[debug] press {len(pressed) + 1}: {len(found)} possible play button(s), best first:")
                for i, (where, score, area, words, html) in enumerate(found, 1):
                    print(f"[debug] {i}. {where} score={score} size={area} text={words!r}\n[debug]    {html}")
            if hit:
                label = click_play(driver, *hit, debug=debug)
                pressed.append(label)
                print(f"Pressed \"{label}\" (press {len(pressed)}). Looking for the fullscreen button…")
            elif not pressed:
                return pressed, None  # nothing to press at all

        for _ in range(6):  # give the player a moment to show its controls
            fs = click_fullscreen(driver, fs_selector)
            if fs:
                return pressed, fs
            time.sleep(0.5)
    return pressed, None


def main():
    parser = argparse.ArgumentParser(description="Open a site, click its search bar and type a name.")
    parser.add_argument("name", help="Text to type into the search bar")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Site to open (default: {DEFAULT_URL})")
    parser.add_argument("--selector", help="CSS selector for the search box, if it isn't found automatically")
    parser.add_argument("--submit", action="store_true", help="Press Enter after typing")
    parser.add_argument("--type", choices=["movie", "tv"],
                        help="After searching, open the first result in the MOVIES or TV SHOWS section (implies --submit)")
    parser.add_argument("--pick", action="store_true",
                        help="After searching, show the results (TV shows and movies) on the builder page and open "
                             "the one picked there; use instead of --type")
    parser.add_argument("--show-url", metavar="URL",
                        help="Open this TV show's page directly instead of searching (used by the phone's "
                             "season/episode switcher); with --season/--episode, or the phone is asked")
    parser.add_argument("--no-play", action="store_true", help="With --type, open the title but don't press Play")
    parser.add_argument("--play-for", type=float, default=PLAY_LOOP_SECONDS,
                        help=f"Seconds to keep pressing Play until the fullscreen button shows (default: {PLAY_LOOP_SECONDS})")
    parser.add_argument("--play-selector", metavar="CSS", action="append",
                        help="With --type, the exact play button to press: a selector copied from Chrome's Inspect "
                             "panel (right-click the element > Copy > Copy selector). Repeat it for buttons pressed "
                             "in order (intro Play, a server, then the real Play); the last one is pressed again every "
                             "--retry-every seconds until the fullscreen button shows, which is then clicked. "
                             "Remembered for this site.")
    parser.add_argument("--fullscreen-selector", metavar="CSS",
                        help="With --type, the player's exact fullscreen button: a selector copied from Chrome's "
                             "Inspect panel, for when it isn't found by itself. Remembered for this site.")
    parser.add_argument("--after-fullscreen-selector", metavar="CSS", action="append",
                        help="With --type, a button to press once the player is fullscreen (e.g. a big Play, then "
                             "Unmute): a selector copied from Chrome's Inspect panel. Repeat it for buttons pressed "
                             "in order, about a second apart; each one is pressed once, so list a button twice "
                             "to press it twice. Remembered for this site.")
    parser.add_argument("--keep-playing", metavar="CSS",
                        help="With --type, after the buttons above: press this play button only while the video "
                             "is paused, until it's really playing (it never presses it while playing). "
                             "Remembered for this site.")
    parser.add_argument("--season", type=int, metavar="N",
                        help="With --type tv: pick season N in the site's season dropdown before the episode")
    parser.add_argument("--episode", type=int, metavar="N",
                        help="With --type tv: click the button for episode N (labelled like \"Episode 13: …\") "
                             "after the intro Play, before the real Play")
    parser.add_argument("--retry-every", type=float, default=5,
                        help="With --play-selector: seconds to wait for the fullscreen button before pressing the "
                             "last button again (default: 5)")
    parser.add_argument("--teach", action="store_true",
                        help="With --type, let you click the real play button(s) yourself and remember them for this site")
    parser.add_argument("--debug", action="store_true", help="With --type, list everything it could click")
    parser.add_argument("--share-room", metavar="CODE",
                        help="First, share the show's tab (with its sound) to the TV through Screen Relay with this room code "
                             "(the TV opens the relay page with #tv=CODE)")
    parser.add_argument("--share-wait", type=float, default=5,
                        help="With --share-room: seconds to see whether the TV picks it up straight away before "
                             "finding the show (default: 5; the TV can still join later)")
    parser.add_argument("--share-url", default=SHARE_URL,
                        help=f"Screen Relay page to share through (default: {SHARE_URL})")
    parser.add_argument("--visible", action="store_true",
                        help="Show the script's Chrome on this computer's screen (by default it runs hidden, so whoever "
                             "is using the computer isn't interrupted). --teach always shows it.")
    parser.add_argument("--profile", default=PROFILE_DIR,
                        help="Chrome profile folder to use, so extensions stay installed (default: chrome-profile here)")
    parser.add_argument("--timeout", type=float, default=15, help="Seconds to wait for the search box (default: 15)")
    args = parser.parse_args()
    if "://" not in args.url:
        args.url = "https://" + args.url  # Chrome needs a scheme; allow plain "example.com"
    if args.type or args.pick:
        args.submit = True
    if args.show_url:
        args.submit = False  # no search: straight to the show's page

    print(f"Opening {args.url} …")
    driver = open_browser(args.profile, auto_share=bool(args.share_room),
                          visible=args.visible or args.teach or not HEADLESS)
    if args.share_room:
        for leftover in ("json", "servers.json", "server-request.json", "playback.json", "control.json",
                         "info.json"):
            try:
                os.remove(os.path.join(NOW_DIR, f"{args.share_room.upper()}.{leftover}"))
            except OSError:
                pass
        share_screen(driver, args.share_room.upper(), args.share_url, args.share_wait)
    guard_players(driver)  # the show's tab: players may not send it away
    try:
        if args.show_url:
            open_show(driver, args.show_url, args.timeout)
        else:
            load(driver, args.url, args.timeout)
    except WebDriverException as e:
        reason = str(e.msg or e).splitlines()[0]
        print(f"Couldn't open {args.url} ({reason}). Check the site address: it should look like "
              "www.example.com, with no spaces.", file=sys.stderr)
        sys.exit(1)

    WebDriverWait(driver, args.timeout).until(lambda d: d.execute_script("return document.readyState") == "complete")
    apply_page_fix(driver, args.show_url or args.url, args.timeout)

    selectors = [args.selector] if args.selector else SEARCH_SELECTORS
    box, used = (None, None) if args.show_url else type_into_search(driver, selectors, args.name, args.timeout)
    if box is None and not args.show_url:
        print("Couldn't type into a search box on that page. Find it with right-click > Inspect, then pass --selector.",
              file=sys.stderr)
        sys.exit(1)
    if not args.show_url:
        print(f"Typed \"{args.name}\" into the search box ({used}) and checked it stuck.")

    if args.submit:
        start_url = driver.current_url
        box.send_keys(Keys.ENTER)
        try:
            WebDriverWait(driver, args.timeout).until(lambda d: d.current_url != start_url)
            print(f"Pressed Enter. Results: {driver.current_url}")
        except TimeoutException:
            print("Pressed Enter, but the page didn't change. This site may need a click on its search button.",
                  file=sys.stderr)

    show_url = args.show_url
    kind = None
    if args.type or args.pick or args.show_url:
        if args.show_url:
            kind = "tv"
            print(f"Opened {args.name} directly.")
            save_room_info(args.share_room, args.name, "tv")
        elif args.pick:
            item = pick_result(driver, args.timeout)
            if item is None:
                print("Couldn't find any TV show or movie results on that page.", file=sys.stderr)
                sys.exit(1)
            kind = item["kind"]
            show_url = item["href"]
            args.name = item["title"]
            print(f"Opened the {'TV show' if kind == 'tv' else 'movie'} you picked: {item['title']}")
            save_room_info(args.share_room, item["title"], kind)
            if kind == "movie":
                args.season = args.episode = None  # seasons and episodes are for TV shows only
        else:
            kind = args.type
            section = "MOVIES" if kind == "movie" else "TV SHOWS"
            title = click_first_result(driver, kind, args.name, args.timeout, args.debug)
            if title is None:
                print(f"Couldn't find a result under {section} on that page.", file=sys.stderr)
                sys.exit(1)
            print(f"Opened the first {section} result: {title}")

        if not args.no_play:
            site = site_key(args.url)
            if args.teach:
                taught = teach_play(driver, args.play_for)
                if not taught:
                    print("No clicks were recorded, so nothing was saved.", file=sys.stderr)
                    sys.exit(1)
                save_taught(site, taught)
                print(f"Saved {len(taught)} play button(s) for {site}. Next runs press only these.")
                print("Chrome stays open. Close it when you're done.")
                return
            if args.play_selector:
                save_taught(site, [{"selector": sel} for sel in args.play_selector])
                print(f"Saved your play-button selector(s) for {site}. Next runs use them even without --play-selector.")
            fs_key = site + " fullscreen"
            if args.fullscreen_selector:
                data = load_taught()
                data[fs_key] = args.fullscreen_selector
                with open(TAUGHT_FILE, "w") as f:
                    json.dump(data, f, indent=2)
                print(f"Saved your fullscreen selector for {site}.")
            fs_selector = load_taught().get(fs_key) or DEFAULT_FULLSCREEN_SELECTOR
            after_key = site + " after fullscreen"
            if args.after_fullscreen_selector:
                data = load_taught()
                data[after_key] = args.after_fullscreen_selector
                with open(TAUGHT_FILE, "w") as f:
                    json.dump(data, f, indent=2)
                print(f"Saved your after-fullscreen button(s) for {site}.")
            after_selectors = load_taught().get(after_key) or DEFAULT_AFTER_SELECTORS
            if isinstance(after_selectors, str):  # saved by an older version as a single selector
                after_selectors = [after_selectors]
            keep_key = site + " keep playing"
            if args.keep_playing:
                data = load_taught()
                data[keep_key] = args.keep_playing
                with open(TAUGHT_FILE, "w") as f:
                    json.dump(data, f, indent=2)
            keep_selector = load_taught().get(keep_key) or DEFAULT_KEEP_SELECTOR
            if fs_selector:
                print(f"Fullscreen button: {fs_selector}")
            rules = load_taught().get(site) or ([{"selector": sel} for sel in DEFAULT_PLAY_SELECTORS] or None)
            if rules and rules[0].get("selector"):
                steps = " then ".join(r["selector"] for r in rules)
                print(f"Pressing only what your play-button selector(s) match, in order: {steps}")
            elif rules:
                print(f"Using the {len(rules)} play button(s) you taught for {site}.")
            episode = episode_selector(args.episode, args.season) if args.episode else None
            picks = ([season_step(args.season, args.timeout)] if args.season else []) + ([episode] if episode else [])
            asking = kind == "tv" and (args.show_url or (args.pick and not args.season and not args.episode))
            if asking:  # which season and episode: picked as given, or asked on the phone
                playing = {"room": (args.share_room or "").upper(), "title": args.name, "site": args.url,
                           "show_url": show_url}
                picks = ask_episode_steps(args.timeout, args.season, args.episode, playing)
            if rules and rules[0].get("selector"):
                steps = [r["selector"] for r in rules]
                # season and episode go after the intro Play, before the rest
                steps = steps[:1] + picks + steps[1:] if len(steps) > 1 else picks + steps
                pressed, fullscreen = play_in_steps(driver, steps, args.timeout, args.debug, args.play_for,
                                                    args.retry_every, fs_selector, (args.share_room or "").upper())
            else:
                if asking:
                    for ask in picks:  # no play selectors here, so ask right away
                        print(f"{ask(driver) or 'Skipped ' + ask.description}.")
                if args.season and not asking:
                    picked = season_step(args.season, args.timeout)(driver)
                    print(f"{picked}." if picked else f"Couldn't find the Season {args.season} option.")
                if episode and not asking:
                    hit = find_step(driver, episode)
                    deadline = time.monotonic() + args.timeout
                    while hit is None and time.monotonic() < deadline:
                        time.sleep(0.5)
                        hit = find_step(driver, episode)
                    if hit is None:
                        print(f"Couldn't find a button for episode {args.episode}.", file=sys.stderr)
                    else:
                        print(f"Pressed \"{click_play(driver, *hit, debug=args.debug)}\".")
                pressed, fullscreen = start_playback(driver, args.timeout, args.debug, args.play_for, rules,
                                                     fs_selector)
            if not pressed:
                if rules and rules[0].get("selector"):
                    print("Nothing on the page matched your play-button selector(s). Copy them again from Inspect "
                          "(the site may have changed) and pass it with --play-selector.", file=sys.stderr)
                elif rules:
                    print("None of the play buttons you taught showed up. If the site changed, run again with --teach.",
                          file=sys.stderr)
                else:
                    print("Couldn't find a play button on the title page. Run again with --debug to see what's there.",
                          file=sys.stderr)
                sys.exit(1)
            for sel in after_selectors:
                if not press_after_fullscreen(driver, sel, min(args.timeout, 6)):
                    print(f"The button after fullscreen ({sel}) didn't show within {args.timeout:g}s, so it "
                          "was skipped.", file=sys.stderr)
            if keep_selector:
                keep_playing(driver, keep_selector)
            if after_selectors or keep_selector:
                hide_controls(driver)
            playing = "Video is playing. " if video_playing(driver) else ""
            if fullscreen:
                print(f"{playing}Now fullscreen ({fullscreen}).")
            else:
                print(f"{playing}Pressed play {len(pressed)} time(s) over {args.play_for:g}s but couldn't get to "
                      "fullscreen. Right-click the player's fullscreen button > Inspect, copy the selector of its "
                      "button (or the element just around the icon) and pass it with --fullscreen-selector.",
                      file=sys.stderr)
    print("Chrome stays open. Close it when you're done.")
    if args.share_room and kind and not args.no_play:
        play_next_when_done(driver, args.share_room.upper(), next_episodes=kind == "tv")


def _stopped(signum, frame):
    raise KeyboardInterrupt  # a new run from the builder stops this one: log it as stopped


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _stopped)
    log = RunLog(sys.argv)
    sys.stdout, sys.stderr = Tee(sys.stdout, log), Tee(sys.stderr, log)
    try:
        main()
    except SystemExit as e:
        log.finish("done" if e.code in (None, 0) else "failed")
        raise
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        log.finish("stopped")
        sys.exit(130)
    except BaseException:
        traceback.print_exc()
        log.finish("failed")
        sys.exit(1)
    else:
        log.finish("done")
