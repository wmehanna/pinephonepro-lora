#!/bin/bash
# Install the lora-mode switcher + both services on the PinePhone Pro.
# Run as the wmehanna user from /opt/lora-pkt-fwd on the phone.

set -e

CONF=/etc/lora-pkt-fwd/global_conf.json
NODE_CONF=/etc/lora-pkt-fwd/node_conf.json
MODE=/etc/lora-pkt-fwd/mode
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1. CLI switcher (drop in /usr/local/bin so it's on PATH)
echo "Installing lora-mode CLI..."
sudo -n -S -p "" cp "$SCRIPT_DIR/lora-mode" /usr/local/bin/lora-mode
sudo -n -S -p "" chmod +x /usr/local/bin/lora-mode

# 2. Both systemd services
echo "Installing gateway service..."
sudo -n -S -p "" cp "$SCRIPT_DIR/lora-pkt-fwd.service" /etc/systemd/system/lora-pkt-fwd.service
echo "Installing node service..."
sudo -n -S -p "" cp "$SCRIPT_DIR/lora-node.service" /etc/systemd/system/lora-node.service
sudo -n -S -p "" systemctl daemon-reload

# 3. Configs (only create if not present, never overwrite)
sudo -n -S -p "" mkdir -p /etc/lora-pkt-fwd
[ -f "$CONF" ] || sudo -n -S -p "" cp "$SCRIPT_DIR/global_conf.json.example" "$CONF"
[ -f "$NODE_CONF" ] || sudo -n -S -p "" cp "$SCRIPT_DIR/node_conf.json.example" "$NODE_CONF"

# 4. Initial mode = gateway (preserve existing setup)
if [ ! -f "$MODE" ]; then
  echo "gateway" | sudo -n -S -p "" tee "$MODE" > /dev/null
fi

# 5. Enable both services but only start the current mode
sudo -n -S -p "" systemctl enable lora-pkt-fwd.service
sudo -n -S -p "" systemctl enable lora-node.service

CURRENT=$(cat "$MODE")
if [ "$CURRENT" = "gateway" ]; then
  sudo -n -S -p "" systemctl start lora-pkt-fwd.service
elif [ "$CURRENT" = "node" ]; then
  sudo -n -S -p "" systemctl start lora-node.service
fi

echo ""
echo "Installed. Current mode: $CURRENT"
echo "Switch with: lora-mode set gateway   |   lora-mode set node"
echo "Check with:  lora-mode status"
