#!/usr/bin/env sh
# Run Switchboard at login as a systemd *user* service (no root needed).
# Usage: ./deploy/linux/install-service.sh        Remove: ./deploy/linux/install-service.sh --remove
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT="$HOME/.config/systemd/user/switchboard.service"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now switchboard 2>/dev/null || true
  rm -f "$UNIT" && systemctl --user daemon-reload
  echo "removed"; exit 0
fi

[ -x "$REPO/.venv/bin/python" ] || { echo "create the venv first (see README)"; exit 1; }
[ -f "$REPO/config.yaml" ] || { echo "create config.yaml first (see README)"; exit 1; }

mkdir -p "$(dirname "$UNIT")"
cat > "$UNIT" <<UNIT
[Unit]
Description=Switchboard LLM router (localhost only)
After=network-online.target

[Service]
WorkingDirectory=$REPO
ExecStart=$REPO/.venv/bin/python -m switchboard --config config.yaml serve --host 127.0.0.1 --port 8787
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
LockPersonality=true
RestrictRealtime=true
SystemCallArchitectures=native
MemoryMax=512M

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now switchboard
echo "running. logs: journalctl --user -u switchboard -f"
