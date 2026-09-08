# PinePhone Pro LoRaWAN Gateway

Single-channel LoRaWAN gateway running on a PinePhone Pro with the PineDio SX1262 back cover, forwarding packets to a ChirpStack server over UDP/1700.

## What this does

The PinePhone Pro has a PineDio back cover with a Semtech SX1262 LoRa radio plus an ATtiny84 microcontroller that bridges the PinePhone's I2C bus to the SX1262's SPI bus. This repo contains:

- `lora/bridge.py` - ATtiny84 I2C↔SPI bridge protocol driver (smbus2 over `/dev/i2c-5:0x28`)
- `lora/sx1262.py` - Semtech SX1262 register-level driver (init, RX, TX, IRQ)
- `lora/lora-mode` - CLI switcher between gateway and node modes
- `lora/lora-pkt-fwd.py` - Semtech UDP packet forwarder (gateway mode)
- `lora/lora-node.py` - LoRaWAN 1.0.x node MAC (node mode, OTAA uplink + RX windows)
- `device-tree/` - Patched DTB enabling the previously-disabled I2C controller
- `lora/global_conf.json.example` - Forwarder config template
- `lora/node_conf.json.example` - Node OTAA config template
- `lora/lora-pkt-fwd.service` - Gateway systemd unit
- `lora/lora-node.service` - Node systemd unit
- `lora/install.sh` - One-shot installer

## Hardware fix that's the real breakthrough

The PinePhone Pro kernel (postmarketOS v26.06 megi) ships with the I2C controller at `i2c@ff150000` (`/dev/i2c-5`) DISABLED. The PineDio back cover's ATtiny84 bridge lives on this bus. Without enabling the bus, every scan returns silent - no SX1262 reachable.

Fix: decompile the stock DTB (`rk3399-pinephone-pro.dtb`), change `status = "disabled"` to `status = "okay"` on `i2c@ff150000`, add a child `lora_back@28` node, recompile with `dtc`, point GRUB at the patched DTB. After reboot, `i2cdetect -y 5` shows `28` (the ATtiny84 bridge).

The patched DTB is `device-tree/dtb.pinedio` (md5 `e7424de6cfd5e1b442879c049e352ea9`). The original is `device-tree/dtb.original` for reference.

## Layout

```
pinephonepro/
├── README.md
├── .gitignore
├── lora/
│   ├── bridge.py                  ATtiny84 I2C↔SPI bridge protocol
│   ├── sx1262.py                  Semtech SX1262 register-level driver
│   ├── lora-mode                  CLI: lora-mode status / set gateway / set node
│   ├── lora-pkt-fwd.py            Semtech UDP packet forwarder (gateway mode)
│   ├── lora-node.py               LoRaWAN 1.0.x node MAC (node mode)
│   ├── global_conf.json.example   Forwarder config template
│   ├── node_conf.json.example     Node OTAA config template
│   ├── lora-pkt-fwd.service       Gateway systemd unit
│   ├── lora-node.service          Node systemd unit
│   ├── install.sh                 One-shot installer
│   ├── pyvenv.cfg                 Python venv marker
│   └── requirements.txt           smbus2==0.6.1
├── device-tree/
│   ├── dtb.original               Untouched PinePhone Pro DTB
│   ├── dtb.pinedio                Patched DTB (enables i2c@ff150000)
│   ├── pinedio-sx126x.dtbo        Vestigial overlay (unused; SPI path not used)
│   ├── grub.cfg                   Working GRUB config pointing at dtb.pinedio
│   └── rk3399-pinephone-pro.dts   Decompiled DTS source (for reference)
└── docs/
    └── HARDWARE-FIX.md           Detailed writeup of the i2c@ff150000 discovery
```

## Deployment (already done on the PinePhone Pro)

```bash
# Copy code
scp -r lora/ wmehanna@192.168.1.249:/opt/lora-pkt-fwd/

# Install venv + deps
sudo apk add python3 py3-virtualenv libgpiod
python3 -m venv /opt/lora-venv
sudo /opt/lora-venv/bin/pip install smbus2

# Patch DTB
sudo dtc -@ -I dts -O dtb -o /boot/rk3399-pinephone-pro-pinedio.dtb \
  device-tree/rk3399-pinephone-pro.dts

# Point GRUB at patched DTB
sudo sed -i 's|devicetree /rk3399-pinephone-pro.dtb|devicetree /rk3399-pinephone-pro-pinedio.dtb|' \
  /boot/grub/grub.cfg

# Install service
sudo cp lora/lora-pkt-fwd.service /etc/systemd/system/
sudo cp lora/global_conf.json.example /etc/lora-pkt-fwd/global_conf.json
sudo systemctl daemon-reload
sudo rc-update add lora-pkt-fwd default
sudo systemctl start lora-pkt-fwd
```

## Switching between gateway and node

The PinePhone can run as either a LoRaWAN **gateway** (forwards packets from nodes to ChirpStack) or a **node** (OTAA device that sends uplinks to a gateway). The `lora-mode` CLI swaps between them.

```bash
lora-mode status                       # current mode, service state, last log line
lora-mode set gateway                  # stop node, start forwarder
lora-mode set node                     # stop forwarder, start node MAC
```

Single source of truth: `/etc/lora-pkt-fwd/mode`. Both systemd units are always enabled; only one is active at a time.

`install.sh` does the one-shot setup of both services + mode file.

## Runtime state

- Phone: `pine64-pinephonepro` at `192.168.1.249` (split-horizon DNS wmsolinc)
- ChirpStack server: `192.168.1.94:1700` (Dragino HP0C, US915)
- Gateway EUI: `FFFED62E55624467` (derived from PinePhone wlan0 MAC)
- Frequency: `903.0 MHz` (US915 ch 0, standard OTAA join frequency)
- Modulation: LoRa SF7 BW125 CR4/5 (standard join settings)
- Service log: `/var/log/lora-pkt-fwd.log` (PULL_ACK every 10s)

## Constraints

- SX1262 is single-channel - only one frequency. Real LoRaWAN gateways use 8 channels. The PinePhone's role here is a secondary/backup gateway plus a tinkering platform.
- postmarketOS kernel has `CONFIG_LORA` and `CONFIG_LORA_SX126X` DISABLED, so the entire LoRa stack is in userspace.
- ChirpStack gateway-bridge 4.x rejects PULL_DATA frames with a JSON payload ("12 bytes expected"); use header-only PULL_DATA (12 bytes).
- `wmehanna` user must use `sudo -S -p "" -k` wrappers for privileged ops; the `wmehanna` user is not in the `i2c` group.

## See also

- `docs/HARDWARE-FIX.md` - the i2c@ff150000 discovery writeup
- `puurpl/pinephonepro-lora` - upstream ATtiny84 bridge protocol reference
- `jpmeijers/single_chan_pkt_fwd` - reference for Semtech UDP forwarder
- `lupyuen/lora-sx1262` - SX1262 register-level C reference
