# How it works

```
 Phone                         Computer                                     TV
 ─────                         ────────                                     ──
 Builder page ──Run──▶  builder-server.py (helper, :8766)
 (Wi-Fi link, or               │ starts
  remote.html ──▶ agent)        ▼
                          search.py ──drives──▶ its own Chrome (hidden)
  "Which one?" ◀── asks ──┤                     ├─ show tab: search, pick, play fullscreen
  season / episode        │                     └─ relay tab: shares the show tab ──────▶ TV page
  live view, controls ◀────────────────────── same room (direct connection) ───────────▶ (#tv=room&h=home)
```

## A run

1. **Room check.** If the room is already showing something, the phone asks whether to use the
   next free room.
2. **Chrome.** The helper starts `search.py`, which opens (or reuses) its own Chrome: hidden by
   default (`"headless": false` in `app/settings.json` shows it), with its own profile.
3. **Sharing first.** The run's tab is named "Site Search Show". Chrome is started with a setting
   that shares that tab automatically, so the relay page shares it, sound included, to the room
   before the site is even opened. A tab keeps sharing while it moves between pages.
4. **Player guard.** Before the site loads, every video-player frame it creates gets a sandbox:
   the player can do everything it needs, but it can't send the whole tab elsewhere.
5. **Search and pick.** It types the title into the site's search box and presses Enter. Then it
   reads the results: links with a poster image. Their titles come from the link or the poster's
   alt text, and TV show or movie comes from the address (`/series/`, `/movie/`…) or the
   heading they're under. The results go to the phone as posters.
6. **Season and episode.** On a TV show's page (after the first play button, if the site needs it)
   it reads the season list and the episode buttons (labels like "Season 3", "Episode 4: …"), and
   asks the phone.
7. **Play.** It presses the configured play buttons. If a video server shows no video within a few
   seconds, it presses the next server button. It goes fullscreen (the player's button, or the
   player made fullscreen directly), presses any after-fullscreen buttons, makes sure the video is
   really playing, and moves the mouse off the controls so they hide.
8. **Watching.** It reports the video's position to the phone every second (progress bar, Pause,
   Play, jumping), takes server picks from the phone, and when an episode ends (the player's time
   shows the same number twice, or the video says it ended) it starts the next one, and after a
   season's last episode the next season's first.

## The pieces

- **Helper** (`app/builder-server.py`): serves the builder page and starts runs; one run per room,
  and a new run for a room replaces the old one. It also passes questions and answers between the
  phone and the run, and serves the TV relay on the Wi-Fi (`:8765`). It keeps the remote agent
  running and keeps the computer awake. Devices other than the computer need the phone code
  (10 wrong tries locks a device out for 15 minutes), and changes are only accepted from the
  builder page itself.
- **Screen Relay** (`relay/index.html`): WebRTC through PeerJS. A room has up to 4 screens. The
  computer calls all of them and keeps retrying until one opens the room. Video goes up to
  8 Mbps at full resolution, in stereo, and the screen keeps playing through dropped connections.
  The TV page asks for one press of OK so it may play sound.
- **Home code** (`app/.home-id`): goes in front of every room code (`&h=` in links), so two
  installs using room 1234 on the same public relay never meet.
- **Remote** (`relay/remote.html` + `app/agent.html`): see [OUTSIDE_HOME.md](OUTSIDE_HOME.md).

## Files it creates (in `app/`, ignored by git)

| File | What |
|---|---|
| `settings.json` | Optional: `builder_port`, `relay_port`, `chrome_port`, `headless`, `save_logs`, `public_relay`. |
| `site-config.json` | The site settings ([SITE_SETTINGS.md](SITE_SETTINGS.md)). |
| `play-buttons.json` | Buttons saved per site (from the builder fields or Teach). |
| `.builder-key`, `.remote-key`, `.remote-pass`, `.home-id` | Phone code, remote key, passcode, home code. |
| `chrome-profile/`, `agent-profile/` | The two Chrome profiles. |
| `nowplaying/`, `picks/`, `rooms.json`, `logs.json` | What's playing, open questions, rooms, run logs. |
