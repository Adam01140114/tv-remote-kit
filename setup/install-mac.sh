#!/bin/bash
# TV Remote Kit: set up on macOS.
#   1. checks Google Chrome and Python 3,
#   2. installs the Python packages (Selenium, psutil),
#   3. makes the helper start when you log in (a LaunchAgent) and starts it,
#   4. prints your links.
#
#   bash setup/install-mac.sh                 set up and start it
#   bash setup/install-mac.sh --no-autostart  only check and install packages
set -e

KIT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$KIT/app"
LABEL="com.tv-remote-kit.helper"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('builder_port', 8766))" "$APP/settings.json" 2>/dev/null || echo 8766)

echo "TV Remote Kit, from: $KIT"

if [ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ] && \
   [ ! -x "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  echo "ERROR: Google Chrome isn't installed. Install it from https://www.google.com/chrome/ and run this again."
  exit 1
fi
PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
  echo "ERROR: Python 3 isn't installed. Install it from https://www.python.org/downloads/macos/ and run this again."
  exit 1
fi
PYTHON="$("$PYTHON" -c 'import sys; print(sys.executable)')"
echo "Python: $PYTHON ($("$PYTHON" --version))"

"$PYTHON" -m pip install --quiet --upgrade -r "$KIT/setup/requirements.txt" \
  || "$PYTHON" -m pip install --quiet --user --upgrade -r "$KIT/setup/requirements.txt" \
  || "$PYTHON" -m pip install --quiet --user --break-system-packages --upgrade -r "$KIT/setup/requirements.txt"
"$PYTHON" -c "import selenium, psutil; print('Selenium', selenium.__version__, '| psutil', psutil.__version__)"
mkdir -p "$APP/nowplaying" "$APP/picks"

if [ "$1" = "--no-autostart" ]; then
  echo "OK. Start it with:  \"$PYTHON\" \"$APP/builder-server.py\""
  exit 0
fi

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$LABEL" || true
  sleep 1
fi
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$APP/builder-server.py</string>
    <string>--no-open</string>
  </array>
  <key>WorkingDirectory</key><string>$APP</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$PYTHON"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>$APP/helper.log</string>
  <key>StandardErrorPath</key><string>$APP/helper.log</string>
</dict>
</plist>
PLIST
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST"

for _ in $(seq 1 30); do
  curl -s -o /dev/null "http://localhost:$PORT/command-builder.html" && break
  sleep 1
done
if ! curl -s -o /dev/null "http://localhost:$PORT/command-builder.html"; then
  echo "ERROR: the helper didn't start. See $APP/helper.log, or run it by hand:"
  echo "  \"$PYTHON\" \"$APP/builder-server.py\""
  exit 1
fi

echo
echo "Running. It starts by itself whenever you log in."
"$PYTHON" "$KIT/setup/links.py"
