# Setting up TV Remote Kit (instructions for Claude)

The user wants this kit running on their computer. Do the steps below, in order, and finish by
giving them their links. Run commands yourself; only stop to ask when a step says so.

## 1. Check the computer

- Find the OS (macOS, Windows, or Linux).
- **Google Chrome** must be installed (macOS: `/Applications/Google Chrome.app`; Windows:
  `C:\Program Files\Google\Chrome\Application\chrome.exe` or under `%LOCALAPPDATA%`). If it isn't,
  tell the user to install it from https://www.google.com/chrome/ and wait.
- **Python 3.9+** must be installed (`python3 --version`, or `py -3 --version` on Windows). If it
  isn't, tell the user to install it from https://www.python.org/downloads/ (on Windows, ticking
  "Add python.exe to PATH") and wait.

## 2. Run the installer

From the repository's folder:

- **macOS**: `bash setup/install-mac.sh`
- **Windows** (PowerShell): `powershell -ExecutionPolicy Bypass -File setup\install-windows.ps1`
- **Linux**: `python3 -m pip install --user -r setup/requirements.txt`, then start
  `python3 app/builder-server.py --no-open` (for start-at-login, add a systemd user service or
  an autostart entry that runs that command; ask the user which they prefer).

The installer installs the Python packages, makes the helper start whenever the user logs in,
starts it, and prints the links. If it fails, read its error, fix the cause (missing Chrome or
Python, a pip problem, a port in use), and run it again.

Ports: the helper uses **8766** (builder), **8765** (TV relay) and **9333** (its own Chrome). If
one is taken by something else, create `app/settings.json` with other numbers, for example
`{"builder_port": 18766, "relay_port": 18765, "chrome_port": 19333}`, and run the installer again.

## 3. Check it works

- `python3 setup/links.py` (Windows: `py setup\links.py`) should print the links. If it says the
  helper isn't answering, start it by hand to see the error:
  `python3 app/builder-server.py` (Windows: `py app\builder-server.py`).
- Open `http://localhost:8766/` (or the builder port from settings.json): the Search Command
  Builder page should load.
- **Firewall**: the phone and TV reach this computer over Wi-Fi. On Windows, allow Python on
  *Private* networks when asked (or add an inbound rule for TCP 8765 and 8766). On macOS, click
  Allow if asked whether Python may accept incoming connections.

## 4. Site settings

The kit is generic: it doesn't know any website until it's given that site's settings (its
address and which buttons to press). Ask the user whether they have a **settings JSON** (for
example from a friend's "Copy settings").

- **If they do**: on the builder page (`http://localhost:8766/`), click **Import settings**, paste
  it and click **Save settings**. Or save it as `app/site-config.json` yourself (the format is in
  `docs/SITE_SETTINGS.md`). Either way it applies to every run, including from the phone.
- **If they don't**: point them to `docs/SITE_SETTINGS.md`, which explains how to find a site's
  buttons with Chrome's Inspect, or the builder's **Teach** option (they click the real play
  button(s) once and it's remembered).

## 5. Give the user their links

Run `python3 setup/links.py` (Windows: `py setup\links.py`) and give the user:

1. **Phone, on your Wi-Fi**: the `http://<computer's Wi-Fi address>:8766/p/<code>` link. They open
   it once on their phone (it remembers it), type a title, tap **Run command**, and pick what to
   watch.
2. **TV**: after a run starts, the phone shows the TV links. On the TV, open the link (from
   anywhere, or the Wi-Fi one at home) and press OK once so it plays with sound.
3. **Outside home**: the remote link and its passcode. Point them to `docs/OUTSIDE_HOME.md`.

Tell them to keep the phone link, remote link and passcode private (they let a device start shows
on this computer), and that the computer must stay on (the kit keeps it awake while the helper
runs; it doesn't stop the computer from shutting down).

## Troubleshooting

- **Logs**: each run's output is in the builder's **Logs** panel on the computer
  (`http://localhost:8766/`), and in `app/logs.json`. On macOS the helper's own output is in
  `app/helper.log`.
- **Nothing plays / wrong buttons**: the site's settings need adjusting (`docs/SITE_SETTINGS.md`).
  Set `"headless": false` in `app/settings.json` and restart the helper to watch the script's
  Chrome work in a normal window.
- **Restart the helper**: macOS `launchctl kickstart -k gui/$(id -u)/com.tv-remote-kit.helper`;
  Windows: end `pythonw.exe` in Task Manager and run the Startup shortcut
  ("TV Remote Kit" in `shell:startup`), or run the installer again.
- How everything works: `docs/HOW_IT_WORKS.md`.
