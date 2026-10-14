# Using it outside home

At home, your phone talks to your computer over your Wi-Fi. Away from home (or on mobile data),
use the **remote link** instead. Your computer at home still does the work, so it has to be on.

## What you need

- Your **remote link**: `…/relay/remote.html#k=<your key>` (printed by the installer and by
  `setup/links.py`, and shown on the computer's builder page under **Open on my phone**).
- Your **passcode**: in `app/.remote-pass` on the computer (also printed by `setup/links.py`).
- Your computer at home switched on, with the helper running. It starts by itself at login, and
  keeps the computer awake while it runs.

## Using it

1. Open the remote link on your phone, anywhere. Bookmark it.
2. Enter the passcode once (the phone remembers it). The top of the page says **Connected to your
   computer** when it's through. On slow networks it can take up to a minute, and it retries by
   itself.
3. Use it like at home: title, **Run command**, pick, season and episode. The live screen, Pause,
   the progress bar, the volume slider and the server picker all work.
4. For a TV elsewhere (a hotel, a friend's place), open the **TV link from anywhere** that the phone
   shows (`…/relay/#tv=<room>&h=<home code>`) and press OK once for sound.

## How it gets through

The remote page is a public web page. It contacts your computer through a free connection service
(PeerJS) that introduces the two devices; after that they talk directly, or through a relay
server when a direct path is blocked. Your computer keeps a hidden Chrome (the "agent") connected
for this.

The agent's address is made from the key in your link and your passcode, so someone needs
**both** to reach it. The part of the link after `#` is never sent to the web server. Even with
both, the remote page can only start a search (title, site, room) and control playback; it can't
run anything else on your computer.

## Keeping it private

- Don't share the remote link or the passcode with people you don't want starting shows on your
  computer.
- To change the passcode: edit `app/.remote-pass` and restart the helper.
- If the link gets out: delete `app/.remote-key` and restart the helper. That makes a new link
  and switches the old one off.

## If it can't connect

- **"Can't reach your computer. Check the passcode…"**: the passcode is wrong, or the computer or
  its helper is off.
- **"Can't reach your computer right now"**: it found your computer but couldn't open a
  connection. Some networks (strict workplace or public Wi-Fi, some mobile networks) block these;
  try again, or switch between Wi-Fi and mobile data.
- At home, the Wi-Fi link (`http://<computer's address>:8766/`) always works without any of this.
