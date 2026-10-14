# Site settings

TV Remote Kit doesn't come set up for any website. You give it a site's settings: its address
and which buttons to press to start a video. Paste them on the builder page with **Import
settings**, or save them as `app/site-config.json`. **Copy settings** gives you the current ones
as JSON, to keep or to share with someone using the same site.

## The format

```json
{
  "site": "www.example.com",
  "room": "1234",
  "play_selectors": [".intro-play", ".play-now"],
  "fullscreen_selector": ".player .fullscreen",
  "after_fullscreen_selectors": [".big-play", ".unmute"],
  "keep_playing_selector": ".big-play",
  "server_button_selector": ".server-list button",
  "page_fix_query": ""
}
```

Every key is optional.

| Key | What it is |
|---|---|
| `site` | The site's address. Searches start there. |
| `room` | The default room code (4 to 6 letters or digits). |
| `play_selectors` | Buttons pressed in order to start playing, as CSS selectors. For example an intro Play that reveals the season and episode lists, then the real Play. With two or more, the season and episode questions happen right after the first one; with one, before it. |
| `fullscreen_selector` | The player's fullscreen button. Without it, the kit looks for one itself, and if needed makes the player fullscreen directly. |
| `after_fullscreen_selectors` | Buttons pressed once each, in order, after it goes fullscreen (a big Play, Unmute…). |
| `keep_playing_selector` | A play button pressed only while the video is paused, until it's really playing. |
| `server_button_selector` | The site's video-server buttons, if it has several servers. Without it, buttons labelled "Server 1", "Server 2"… are found by their text. A server that shows no video within a few seconds is skipped, and the phone can pick one. |
| `page_fix_query` | Something added to the site's address when it's opened, for sites that change their pages with a setting in the address (for example `layout=new`). |

## Finding a button's selector

1. Open the site in Chrome, go to a show's page.
2. Right-click the button > **Inspect**. The Elements panel highlights its code.
3. Right-click the highlighted line > **Copy** > **Copy selector**, and paste it into the setting.
   A shorter selector that still picks only that button (a class name like `.play-button`) is
   more robust than a long copied one.

Or use **Teach** on the builder page: tick it, run once, and click the real play button(s)
yourself in the Chrome window that opens. They're remembered for that site.

## What the kit finds by itself

The search box, the search results (title, poster, TV show or movie), season lists and episode
buttons (from labels like "Season 3" and "Episode 4: …"), common video players' fullscreen
buttons and time readouts, dead video servers, and the end of an episode (to play the next one).
Settings are only needed for what a particular site does differently.
