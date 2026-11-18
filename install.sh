#!/usr/bin/env bash
# Homeflix installer - sets up ~/src/homeflix and a `homeflix` command you run yourself.
# Nothing starts automatically unless you ask for it.
#
# Usage:  bash install.sh                  install; run it yourself with `homeflix`
#         bash install.sh --service        also add a systemd user service (start/stop it
#                                          with systemctl; it does NOT start at login)
#         bash install.sh --autostart      add the service AND start it at every login
#         bash install.sh --remove-service remove the service, keep everything else
#         bash install.sh --uninstall      remove the service and the `homeflix` command
set -euo pipefail

APP_DIR="$HOME/src/homeflix"
CONF_DIR="$HOME/.config/homeflix"
ENV="$CONF_DIR/env"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/homeflix"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/homeflix.service"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/homeflix.py"

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }
usage(){ sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

remove_service(){
  if [[ -f "$UNIT" ]]; then
    systemctl --user disable --now homeflix.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Service removed."
  else
    echo "No service installed."
  fi
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
    echo "Removed $LAUNCHER. App files left in $APP_DIR, settings in $CONF_DIR,"
    echo "watch history in ~/.local/share/homeflix, thumbnails in ~/.cache/homeflix."
    exit 0 ;;
  -h|--help)        usage; exit 0 ;;
  *)                echo "Unknown option: $1"; echo; usage; exit 1 ;;
esac

[[ -f "$SRC" ]] || { echo "homeflix.py must sit next to install.sh"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3,8))' || { echo "Python 3.8+ required"; exit 1; }

bold "== Homeflix installer =="

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "ffmpeg is not installed. It's needed for thumbnails, durations and playing MKV/AVI/HEVC files."
  read -rp "Install it now with apt (needs sudo)? [Y/n] " a
  [[ "${a,,}" != n* ]] && sudo apt-get update && sudo apt-get install -y ffmpeg
fi

mkdir -p "$APP_DIR" "$CONF_DIR" "$BIN_DIR"
install -m 755 "$SRC" "$APP_DIR/homeflix.py"
echo "Installed to $APP_DIR/homeflix.py"

if [[ ! -f "$ENV" ]]; then
  read -rp "Port [8484]: " PORT; PORT="${PORT:-8484}"
  read -rp "Movies folder [~/Downloads/movies]: " VID; VID="${VID:-$HOME/Downloads/movies}"
  read -rp "Music folder [~/Music]: " MUS; MUS="${MUS:-$HOME/Music}"
  VID="${VID/#\~/$HOME}"; MUS="${MUS/#\~/$HOME}"
  echo "Set a password so only people you give it to can watch (Enter = no password)."
  read -rsp "Password: " PW; echo
  esc(){ printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  umask 077
  {
    echo "HOMEFLIX_PORT=$PORT"
    echo "HOMEFLIX_VIDEOS=\"$(esc "$VID")\""
    echo "HOMEFLIX_MUSIC=\"$(esc "$MUS")\""
    [[ -n "$PW" ]] && echo "HOMEFLIX_PASSWORD=\"$(esc "$PW")\""
  } > "$ENV"
  umask 022
  echo "Settings saved to $ENV (edit it any time)"
else
  echo "Keeping existing settings in $ENV"
fi
PORT="$(grep -oP '^HOMEFLIX_PORT=\K\d+' "$ENV" || echo 8484)"

# Launcher: loads the settings file (without letting the shell expand anything in it,
# so passwords with $ or ` are safe), then runs the app. Command-line flags win.
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env python3
"""Run Homeflix with the settings in $ENV. Any flags are passed through."""
import os, sys
env = os.path.expanduser("~/.config/homeflix/env")
if os.path.exists(env):
    for line in open(env, encoding="utf-8"):
        k, sep, v = line.strip().partition("=")
        if not sep or not k.startswith("HOMEFLIX_"):
            continue
        if len(v) >= 2 and v[0] == v[-1] == '"':
            out, i, v = [], 0, v[1:-1]
            while i < len(v):
                if v[i] == "\\\\" and i + 1 < len(v):
                    i += 1
                out.append(v[i]); i += 1
            v = "".join(out)
        os.environ.setdefault(k, v)
app = os.path.expanduser("~/src/homeflix/homeflix.py")
os.execv(sys.executable, [sys.executable, app] + sys.argv[1:])
EOF
chmod 755 "$LAUNCHER"
echo "Installed the 'homeflix' command to $LAUNCHER"

# Firewall: only open the port to the local subnet
if command -v ufw >/dev/null; then
  if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    DEV="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'dev \K\S+')"
    CIDR="$(ip -o -f inet addr show dev "$DEV" | awk '{print $4}' | head -1)"
    NET="$(python3 -c "import ipaddress,sys;print(ipaddress.ip_interface(sys.argv[1]).network)" "$CIDR")"
    read -rp "UFW is active. Allow port $PORT from your LAN ($NET) only? [Y/n] " a
    [[ "${a,,}" != n* ]] && sudo ufw allow from "$NET" to any port "$PORT" proto tcp comment homeflix
  fi
fi

IP="$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || hostname -I | awk '{print $1}')"
echo

if [[ "$MODE" == manual ]]; then
  if [[ -f "$UNIT" ]]; then
    echo "Note: a Homeflix service from an earlier install is still set up."
    read -rp "Remove it so Homeflix only runs when you start it? [Y/n] " a
    [[ "${a,,}" != n* ]] && remove_service
    echo
  fi
  bold "Homeflix is installed. Start it whenever you want with:"
  echo "  homeflix                 (Ctrl+C to stop)"
  [[ ":$PATH:" != *":$BIN_DIR:"* ]] && echo "  ($BIN_DIR isn't on your PATH yet; open a new terminal or run $LAUNCHER)"
  echo
  echo "  Then open on this PC:      http://localhost:$PORT"
  echo "  Others on your LAN:        http://$IP:$PORT"
  echo
  echo "Want it as a background service instead? Re-run with --service or --autostart."
  exit 0
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT" <<EOF
[Unit]
Description=Homeflix media server
After=network-online.target

[Service]
EnvironmentFile=$ENV
ExecStart=/usr/bin/env python3 $APP_DIR/homeflix.py --quiet
Restart=on-failure
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

if [[ "$MODE" == autostart ]]; then
  systemctl --user enable homeflix.service
  read -rp "Keep Homeflix running even when you're logged out (enable linger)? [y/N] " a
  [[ "${a,,}" == y* ]] && sudo loginctl enable-linger "$USER"
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
