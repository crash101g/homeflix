#!/usr/bin/env bash
# Homeflix installer - sets up ~/src/homeflix and a systemd *user* service.
# Usage:  bash install.sh            (interactive)
#         bash install.sh --uninstall
set -euo pipefail

APP_DIR="$HOME/src/homeflix"
CONF_DIR="$HOME/.config/homeflix"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/homeflix.service"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/homeflix.py"

bold(){ printf '\033[1m%s\033[0m\n' "$*"; }

if [[ "${1:-}" == "--uninstall" ]]; then
  systemctl --user disable --now homeflix.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl --user daemon-reload
  echo "Service removed. App files left in $APP_DIR, settings in $CONF_DIR,"
  echo "watch history in ~/.local/share/homeflix, thumbnails in ~/.cache/homeflix."
  exit 0
fi

[[ -f "$SRC" ]] || { echo "homeflix.py must sit next to install.sh"; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3,8))' || { echo "Python 3.8+ required"; exit 1; }

bold "== Homeflix installer =="

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "ffmpeg is not installed. It's needed for thumbnails, durations and playing MKV/AVI/HEVC files."
  read -rp "Install it now with apt (needs sudo)? [Y/n] " a
  [[ "${a,,}" != n* ]] && sudo apt-get update && sudo apt-get install -y ffmpeg
fi

mkdir -p "$APP_DIR" "$CONF_DIR" "$UNIT_DIR"
install -m 755 "$SRC" "$APP_DIR/homeflix.py"
echo "Installed to $APP_DIR/homeflix.py"

ENV="$CONF_DIR/env"
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
  echo "Settings saved to $ENV (edit it any time, then: systemctl --user restart homeflix)"
else
  echo "Keeping existing settings in $ENV"
fi
PORT="$(grep -oP '^HOMEFLIX_PORT=\K\d+' "$ENV" || echo 8484)"

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
systemctl --user enable --now homeflix.service
sleep 2
systemctl --user --no-pager --lines=0 status homeflix.service | head -3 || true

read -rp "Keep Homeflix running even when you're logged out (enable linger)? [y/N] " a
[[ "${a,,}" == y* ]] && sudo loginctl enable-linger "$USER"

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
bold "Homeflix is running."
echo "  On this PC:          http://localhost:$PORT"
echo "  Others on your LAN:  http://$IP:$PORT"
echo "  Logs:                journalctl --user -u homeflix -f"
