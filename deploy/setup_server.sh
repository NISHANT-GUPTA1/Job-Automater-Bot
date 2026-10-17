#!/usr/bin/env bash
# One-time (re-runnable) server setup for Job Applier on Ubuntu 22.04/24.04 (ARM64 or x86_64).
# Run from the app folder on the server:   bash deploy/setup_server.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
cd "$APP_DIR"

echo "==> 1/7 System packages"
sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip \
    xvfb x11vnc novnc websockify fluxbox curl fonts-noto-color-emoji iptables-persistent

echo "==> 2/7 Swap space (Chromium likes headroom)"
if [ ! -f /swapfile ]; then
    sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> 3/7 Python environment + Chromium"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium

echo "==> 4/7 LaTeX compiler (Tectonic) for tailored resumes"
case "$(uname -m)" in aarch64|arm64) T=aarch64-unknown-linux-musl ;; *) T=x86_64-unknown-linux-musl ;; esac
mkdir -p tools/tectonic
if [ ! -x tools/tectonic/tectonic ]; then
    curl -fsSL "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%400.15.0/tectonic-0.15.0-$T.tar.gz" \
        | tar -xz -C tools/tectonic
    chmod +x tools/tectonic/tectonic
fi

echo "==> 5/7 Settings"
sed -i 's/\r$//' .env profile.yaml        # Windows line endings -> Linux
set_env() {  # set_env KEY VALUE  (adds or replaces the line in .env)
    .venv/bin/python - "$1" "$2" <<'PY'
import sys, pathlib, re
key, val = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env"); lines = p.read_text().splitlines()
lines = [l for l in lines if not re.match(rf"\s*{re.escape(key)}\s*=", l)] + [f"{key}={val}"]
p.write_text("\n".join(lines) + "\n")
PY
}
set_env HEADLESS false
mkdir -p ~/.vnc
if [ ! -f ~/.vnc/passwd ]; then
    VNCPW="${VNC_PASS:-}"   # can be passed in: VNC_PASS=secret bash deploy/setup_server.sh
    [ -n "$VNCPW" ] || { read -rsp "Choose a password for viewing the bot's browser from your phone (6-8 characters): " VNCPW; echo; }
    x11vnc -storepasswd "$VNCPW" ~/.vnc/passwd >/dev/null
fi

echo "==> 6/7 Background services (start on boot, restart on crash)"
unit() { sudo tee "/etc/systemd/system/$1.service" >/dev/null; }
unit jobapplier-display <<EOF
[Unit]
Description=Job Applier virtual screen
[Service]
User=$USER_NAME
ExecStart=/usr/bin/Xvfb :99 -screen 0 1600x900x24 -nolisten tcp
Restart=always
[Install]
WantedBy=multi-user.target
EOF
unit jobapplier-wm <<EOF
[Unit]
Description=Job Applier window manager
After=jobapplier-display.service
Requires=jobapplier-display.service
[Service]
User=$USER_NAME
Environment=DISPLAY=:99
ExecStart=/usr/bin/fluxbox
Restart=always
[Install]
WantedBy=multi-user.target
EOF
unit jobapplier-vnc <<EOF
[Unit]
Description=Job Applier screen sharing (local only)
After=jobapplier-display.service
Requires=jobapplier-display.service
[Service]
User=$USER_NAME
ExecStart=/usr/bin/x11vnc -display :99 -forever -shared -rfbauth /home/$USER_NAME/.vnc/passwd -localhost -rfbport 5900 -quiet
Restart=always
[Install]
WantedBy=multi-user.target
EOF
unit jobapplier-novnc <<EOF
[Unit]
Description=Job Applier browser view in a web page (port 6080)
After=jobapplier-vnc.service
[Service]
User=$USER_NAME
ExecStart=/usr/bin/websockify --web /usr/share/novnc 6080 localhost:5900
Restart=always
[Install]
WantedBy=multi-user.target
EOF
unit jobapplier <<EOF
[Unit]
Description=Job Applier Telegram bot
After=network-online.target jobapplier-display.service
Wants=network-online.target
[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
Environment=DISPLAY=:99
Environment=PYTHONIOENCODING=utf-8
ExecStart=$APP_DIR/.venv/bin/python main.py
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now jobapplier-display jobapplier-wm jobapplier-vnc jobapplier-novnc

echo "==> 7/7 Tailscale: private link to the browser view (only your devices can open it)"
if ! command -v tailscale >/dev/null; then curl -fsSL https://tailscale.com/install.sh | sh; fi
sudo tailscale up            # first time: prints a login link - open it and sign in
sudo iptables -C INPUT -i tailscale0 -j ACCEPT 2>/dev/null || sudo iptables -I INPUT -i tailscale0 -j ACCEPT
sudo netfilter-persistent save >/dev/null
TS_IP="$(tailscale ip -4 | head -1)"
set_env BROWSER_VIEW_URL "http://$TS_IP:6080/vnc.html?autoconnect=1&resize=scale"

echo
.venv/bin/python main.py --check || true
sudo systemctl enable jobapplier >/dev/null
sudo systemctl restart jobapplier
sleep 5
systemctl --no-pager --lines=5 status jobapplier || true
echo
echo "Done. The bot runs 24/7 now - you can close this window and switch the laptop off."
echo "Browser view (phone/laptop with Tailscale on): http://$TS_IP:6080/vnc.html?autoconnect=1&resize=scale"
echo "Logs:    journalctl -u jobapplier -f        Restart:  sudo systemctl restart jobapplier"
