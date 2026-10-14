# TV Remote Kit

Type a title on your phone, pick it from a list, and it plays fullscreen, with sound, on your TV,
from your computer. Your phone becomes the remote: season and episode menus, Pause, a progress
bar, volume, and the next episode starts by itself.

Works on **macOS** and **Windows** (Linux by hand). It's generic: you give it the settings for
the website you use (**Import settings**, see [docs/SITE_SETTINGS.md](docs/SITE_SETTINGS.md)).

## Set it up

**With Claude** (easiest): open this folder in Claude Code and say *"set this up for me"*. It
follows [CLAUDE.md](CLAUDE.md): installs what's needed, starts it, and gives you your links.

**By hand**: install [Google Chrome](https://www.google.com/chrome/) and
[Python 3](https://www.python.org/downloads/), then from this folder:

- macOS: `bash setup/install-mac.sh`
- Windows (PowerShell): `powershell -ExecutionPolicy Bypass -File setup\install-windows.ps1`

It installs two Python packages (Selenium, psutil), makes the helper start whenever you log in,
starts it, and prints your links.

## Use it

1. **Settings**: on the computer, open `http://localhost:8766/`, click **Import settings** and
   paste the settings JSON for your site (or fill in the fields; see
   [docs/SITE_SETTINGS.md](docs/SITE_SETTINGS.md)).
2. **Phone**: open your phone link (`http://<computer's address>:8766/p/<code>`) on your Wi-Fi.
   Type a title, tap **Run command**, pick what to watch.
3. **TV**: open the TV link the phone shows, and press OK once so it has sound.
4. **Away from home**: use your remote link and passcode, see
   [docs/OUTSIDE_HOME.md](docs/OUTSIDE_HOME.md).

`python3 setup/links.py` (Windows: `py setup\links.py`) prints all your links again.

## What's in here

| | |
|---|---|
| `app/search.py` | Drives its own (hidden) Chrome: searches, picks, plays fullscreen, shares the tab to the TV, plays the next episode. |
| `app/builder-server.py` | The helper: the phone/computer page, runs, picks, the TV relay, the remote agent. |
| `app/command-builder.html` | The page you use on the computer and the phone. |
| `app/agent.html` | The remote agent, for the remote link. |
| `relay/` | The TV page and the remote page (also published at the project's GitHub Pages site). |
| `setup/` | Installers, requirements, and `links.py`. |
| `docs/` | Site settings, using it outside home, how it works. |

Everything it creates on your computer (Chrome profiles, keys, logs, settings) stays in `app/`
and is ignored by git.

## Good to know

- Each install has its own private **home code**, so your room 1234 never meets anyone else's.
- The computer must be on; the helper keeps it awake while it runs.
- Keep your phone link, remote link and passcode private.
- Only use it with sites and content you're allowed to watch.

How it all works: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).
