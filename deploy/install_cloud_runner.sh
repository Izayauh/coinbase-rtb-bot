#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:-$HOME/crypto_trading}"
INSTALL_DIR="/opt/crypto-research"
SERVICE_FILE="crypto-research-shard.service"

sudo apt-get update
sudo apt-get install -y python3-venv

if ! id crypto-research >/dev/null 2>&1; then
  sudo useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin crypto-research
fi

sudo mkdir -p "$INSTALL_DIR"
sudo cp -R "$SOURCE_DIR/research_pipeline" "$INSTALL_DIR/"
sudo cp "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/"
sudo cp "$SOURCE_DIR/pyproject.toml" "$INSTALL_DIR/"
sudo python3 -m venv "$INSTALL_DIR/venv"
sudo "$INSTALL_DIR/venv/bin/python" -m pip install --upgrade pip
sudo "$INSTALL_DIR/venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
sudo chown -R crypto-research:crypto-research "$INSTALL_DIR"

sudo install -m 0644 \
  "$SOURCE_DIR/deploy/systemd/$SERVICE_FILE" \
  "/etc/systemd/system/$SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_FILE"
sudo systemctl --no-pager --full status "$SERVICE_FILE"
