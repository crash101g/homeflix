#!/usr/bin/env bash
# Homeflix installer for Linux and macOS - installs the app and a `homeflix` command
# you run yourself. Nothing starts automatically unless you ask for it.
# (Windows: use install.ps1 instead.)
#
# Usage:  bash install.sh                  install; run it yourself with `homeflix`
#         bash install.sh --service        also add a background service (systemd on Linux,
#                                          launchd on macOS); it does NOT start at login
#         bash install.sh --autostart      add the service AND start it at every login
#         bash install.sh --remove-service remove the service, keep everything else
#         bash install.sh --uninstall      remove the service and the `homeflix` command
#
# Works with the bash 3.2 that ships with macOS.
set -euo pipefail

OS="$(uname -s)"
APP_DIR="$HOME/src/homeflix"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/homeflix"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/homeflix.py"

if [[ "$OS" == Darwin ]]; then
  CONF_DIR="$HOME/Library/Application Support/Homeflix"
  LABEL="com.homeflix"
  AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"   # loaded at login (autostart)
  MANUAL_PLIST="$CONF_DIR/$LABEL.plist"             # only loaded when you start it
  LOG="$HOME/Library/Logs/Homeflix.log"
  DOMAIN="gui/$(id -u)"
else
  CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/homeflix"
  UNIT_DIR="$HOME/.config/systemd/user"
  UNIT="$UNIT_DIR/homeflix.service"
fi
ENV="$CONF_DIR/env"

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
usage(){ sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
# ask "Question" Y|N  -> returns 0 for yes. Default answer when you just press Enter.
ask(){
  local a; read -rp "$1 [$( [[ $2 == Y ]] && echo Y/n || echo y/N )] " a || a=""
  a="$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "$a" ]]; then [[ $2 == Y ]]; else [[ "$a" == y* ]]; fi
}

service_installed(){
  if [[ "$OS" == Darwin ]]; then [[ -f "$AGENT" || -f "$MANUAL_PLIST" ]]; else [[ -f "$UNIT" ]]; fi
}

remove_service(){
  if ! service_installed; then echo "No service installed."; return 0; fi
  if [[ "$OS" == Darwin ]]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$AGENT" "$MANUAL_PLIST"
  else
    systemctl --user disable --now homeflix.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
  fi
  echo "Service removed."
}

MODE=manual
case "${1:-}" in
  "")               ;;
  --service)        MODE=service ;;
  --autostart)      MODE=autostart ;;
  --remove-service) remove_service; exit 0 ;;
  --uninstall)
    remove_service
    rm -f "$LAUNCHER"
    echo "Removed $LAUNCHER. App files left in $APP_DIR and settings in $CONF_DIR."
    exit 0 ;;
  -h|--help)        usage; exit 0 ;;
  *)                echo "Unknown option: $1"; echo; usage; exit 1 ;;
esac

[[ -f "$SRC" ]] || { echo "homeflix.py must sit next to install.sh"; exit 1; }
command -v python3 >/dev/null || { echo "Python 3.8+ is required (python3 not found)."; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3,8))' || { echo "Python 3.8+ required"; exit 1; }
PY="$(python3 -c 'import sys; print(sys.executable)')"

bold "== Homeflix installer =="

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "ffmpeg is not installed. It's needed for thumbnails, durations and playing MKV/AVI/HEVC files."
  if [[ "$OS" == Darwin ]]; then
    if command -v brew >/dev/null; then
      ask "Install it now with Homebrew?" Y && brew install ffmpeg
    else
      echo "Install Homebrew (https://brew.sh), then run: brew install ffmpeg"
    fi
  elif command -v apt-get >/dev/null; then
    ask "Install it now with apt (needs sudo)?" Y && sudo apt-get update && sudo apt-get install -y ffmpeg
  elif command -v dnf >/dev/null; then
    ask "Install it now with dnf (needs sudo)?" Y && sudo dnf install -y ffmpeg
  elif command -v pacman >/dev/null; then
    ask "Install it now with pacman (needs sudo)?" Y && sudo pacman -S --needed ffmpeg
  else
    echo "Install ffmpeg with your package manager, then re-run this installer."
  fi
fi

mkdir -p "$APP_DIR" "$CONF_DIR" "$BIN_DIR"
install -m 755 "$SRC" "$APP_DIR/homeflix.py"
echo "Installed to $APP_DIR/homeflix.py"

if [[ ! -f "$ENV" ]]; then
  while :; do
    read -rp "Port [8484]: " PORT || PORT=""; PORT="${PORT:-8484}"
    [[ "$PORT" =~ ^[0-9]{1,5}$ ]] && (( PORT >= 1 && PORT <= 65535 )) && break
    echo "Please enter a number from 1 to 65535."
  done
  read -rp "Movies folder [~/Downloads/movies]: " VID || true; VID="${VID:-$HOME/Downloads/movies}"
  read -rp "Music folder [~/Music]: " MUS || true; MUS="${MUS:-$HOME/Music}"
  VID="${VID/#\~/$HOME}"; MUS="${MUS/#\~/$HOME}"
  echo "Set a password so only people you give it to can watch (Enter = no password)."
  read -rsp "Password: " PW || true; echo
  esc(){ printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  umask 077
  {
    echo "HOMEFLIX_PORT=$PORT"
    echo "HOMEFLIX_VIDEOS=\"$(esc "$VID")\""
    echo "HOMEFLIX_MUSIC=\"$(esc "$MUS")\""
    if [[ -n "$PW" ]]; then echo "HOMEFLIX_PASSWORD=\"$(esc "$PW")\""; fi
  } > "$ENV"
  umask 022
  echo "Settings saved to $ENV (edit it any time)"
else
  echo "Keeping existing settings in $ENV"
fi
PORT="$(sed -n 's/^HOMEFLIX_PORT=\([0-9][0-9]*\).*/\1/p' "$ENV" | head -1)"; PORT="${PORT:-8484}"

# The app reads its settings file itself, so the launcher only has to run it.
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# Runs Homeflix with the settings in $ENV. Flags are passed through.
exec "$PY" "$APP_DIR/homeflix.py" "\$@"
EOF
chmod 755 "$LAUNCHER"
echo "Installed the 'homeflix' command to $LAUNCHER"

# Firewall: only open the port to the local subnet (Linux + UFW)
if [[ "$OS" != Darwin ]] && command -v ufw >/dev/null && command -v ip >/dev/null; then
  if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    DEV="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
    CIDR="$(ip -o -f inet addr show dev "$DEV" | awk '{print $4}' | head -1)"
    NET="$(python3 -c "import ipaddress,sys;print(ipaddress.ip_interface(sys.argv[1]).network)" "$CIDR")"
    if ask "UFW is active. Allow port $PORT from your LAN ($NET) only?" Y; then
      sudo ufw allow from "$NET" to any port "$PORT" proto tcp comment homeflix
    fi
  fi
fi

IP="$(python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("10.255.255.255", 1)); print(s.getsockname()[0])
except OSError:
    print("<this-computer-ip>")
' 2>/dev/null)"
echo

if [[ "$MODE" == manual ]]; then
  if service_installed; then
    echo "Note: a Homeflix service from an earlier install is still set up."
    if ask "Remove it so Homeflix only runs when you start it?" Y; then remove_service; fi
    echo
  fi
  bold "Homeflix is installed. Start it whenever you want with:"
  echo "  homeflix                 (Ctrl+C to stop)"
  if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "  ($BIN_DIR isn't on your PATH. Either run $LAUNCHER, or add this line"
    echo "   to your ~/.zshrc or ~/.bashrc:  export PATH=\"\$HOME/.local/bin:\$PATH\")"
  fi
  echo
  echo "  Then open on this computer: http://localhost:$PORT"
  echo "  Others on your network:     http://$IP:$PORT"
  if [[ "$OS" == Darwin ]]; then
    echo
    echo "macOS will ask to allow access to folders like Downloads or Music, and to accept"
    echo "incoming network connections. Click Allow both times."
  fi
  echo
  echo "Want it as a background service instead? Re-run with --service or --autostart."
  exit 0
fi

# ---------------------------------------------------------------------------- service
if [[ "$OS" == Darwin ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$AGENT" "$MANUAL_PLIST"
  if [[ "$MODE" == autostart ]]; then PLIST="$AGENT"; else PLIST="$MANUAL_PLIST"; fi
  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
  xml(){ printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
  # launchd starts with a bare PATH; add Homebrew's folders so ffmpeg is found.
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$PY")</string>
    <string>$(xml "$APP_DIR/homeflix.py")</string>
    <string>--quiet</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF
  # bootout above finishes asynchronously, so retry briefly if the label is still unloading
  for i in 1 2 3 4 5; do
    launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && break
    [[ $i == 5 ]] && { echo "launchctl could not load $PLIST"; exit 1; }
    sleep 1
  done
  sleep 2
  if launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -q "state = running"; then
    STATUS="running"
  else
    STATUS="NOT running yet - check $LOG"
  fi
  echo
  bold "Homeflix service is $STATUS$([[ "$MODE" == autostart ]] && echo " (starts at login)")."
  echo "  On this Mac:         http://localhost:$PORT"
  echo "  Others on your LAN:  http://$IP:$PORT"
  echo "  Stop:                launchctl bootout $DOMAIN/$LABEL"
  echo "  Start:               launchctl bootstrap $DOMAIN \"$PLIST\""
  echo "  Logs:                tail -f ~/Library/Logs/Homeflix.log"
  echo
  echo "If your media is in Downloads, Documents or Desktop, macOS blocks background apps"
  echo "from reading it. Fix: System Settings > Privacy & Security > Full Disk Access, click +,"
  echo "press Cmd+Shift+G and add $PY"
  echo "Or keep your media in another folder, such as ~/Movies."
  exit 0
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Homeflix media server
After=network-online.target

[Service]
EnvironmentFile=$ENV
ExecStart=$PY $APP_DIR/homeflix.py --quiet
Restart=on-failure
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

if [[ "$MODE" == autostart ]]; then
  systemctl --user enable homeflix.service
  if ask "Keep Homeflix running even when you're logged out (enable linger)?" N; then
    sudo loginctl enable-linger "$USER"
  fi
else
  systemctl --user disable homeflix.service 2>/dev/null || true
fi

# restart (not just start) so re-running the installer picks up a new homeflix.py
systemctl --user restart homeflix.service
sleep 2
systemctl --user --no-pager --lines=0 status homeflix.service | head -3 || true

echo
bold "Homeflix is running as a service$([[ "$MODE" == autostart ]] && echo " (starts at login)")."
echo "  On this PC:          http://localhost:$PORT"
echo "  Others on your LAN:  http://$IP:$PORT"
echo "  Stop / start:        systemctl --user stop homeflix  /  systemctl --user start homeflix"
echo "  Logs:                journalctl --user -u homeflix -f"
