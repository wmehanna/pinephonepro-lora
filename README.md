# PinePhone Pro LoRaWAN on postmarketOS

Userspace LoRaWAN stack for the PinePhone Pro with the PineDio SX1262 back cover. Runs on postmarketOS (or any Linux distro) without kernel patches, custom modules, or rebundled images.

Includes a CLI switcher so the same phone can act as either a LoRaWAN **gateway** (forwards packets to ChirpStack) or a **node** (sends OTAA uplinks).

## Why this exists

postmarketOS v26.06 (megi kernel) ships the PinePhone Pro with the I2C controller at `i2c@ff150000` (`/dev/i2c-5`) **disabled** in the device tree. The PineDio back cover's ATtiny84 I2C-to-SPI bridge lives on this exact bus at address `0x28`. With the controller disabled, every `i2cdetect` returns silent `--` and no community LoRa stack on the PinePhone Pro can reach the radio.

This repo ships the fix (one-line DTB edit) plus a complete userspace LoRaWAN stack on top.

## Who this is for

- PinePhone Pro owners with a PineDio SX1262 back cover who want LoRaWAN without reflashing
- Anyone running postmarketOS on PinePhone Pro and wondering why their LoRa back cover is dead
- Tinkerers who want a single-channel LoRaWAN gateway or a Linux-attached LoRaWAN node

## What's in the box

- `device-tree/` - patched DTB enabling the disabled I2C controller (the breakthrough)
- `lora/bridge.py` - ATtiny84 I2C↔SPI bridge protocol driver (smbus2 over `/dev/i2c-5:0x28`)
- `lora/sx1262.py` - Semtech SX1262 register-level driver (init, RX, TX, IRQ)
- `lora/lora-pkt-fwd.py` - Semtech UDP packet forwarder for ChirpStack (PUSH_DATA/PULL_DATA)
- `lora/lora-node.py` - LoRaWAN 1.0.x node MAC (OTAA join, periodic uplink, RX1/RX2 windows)
- `lora/lora-mode` - CLI to swap between gateway and node modes
- `lora/install.sh` - one-shot installer
- `docs/HARDWARE-FIX.md` - the i2c@ff150000 discovery writeup

## Quick start

### Prerequisites

- PinePhone Pro with PineDio SX1262 back cover physically attached
- postmarketOS v26.06 (or any distro with the PinePhone Pro megi kernel)
- SSH or console access to the phone as a user with `sudo`
- A ChirpStack v4 server on the LAN (for gateway mode), or any LoRaWAN gateway (for node mode)

### 1. Apply the device tree fix (the breakthrough)

```bash
sudo apk add dtc
sudo dtc -@ -I dtb -O dts -o /tmp/rk3399-pinephone-pro.dts /boot/rk3399-pinephone-pro.dtb
sudo sed -i 's|i2c@ff150000 {|&\n\t\tstatus = "okay";|; s|status = "disabled";|status = "okay";|' /tmp/rk3399-pinephone-pro.dts
sudo dtc -@ -I dts -O dtb -o /boot/rk3399-pinephone-pro-pinedio.dtb /tmp/rk3399-pinephone-pro.dts
sudo sed -i 's|devicetree /rk3399-pinephone-pro.dtb|devicetree /rk3399-pinephone-pro-pinedio.dtb|' /boot/grub/grub.cfg
sudo reboot
```

After reboot, verify the bridge is alive:

```bash
sudo apk add i2c-tools
sudo i2cdetect -y 5
# expect a row like: 20: -- -- -- -- -- -- -- -- 28 -- -- -- -- -- -- --
#                              ATtiny84 at 0x28
```

### 2. Install the stack

```bash
sudo apk add python3 py3-virtualenv libgpiod
python3 -m venv /opt/lora-venv
sudo /opt/lora-venv/bin/pip install smbus2

# Clone this repo
git clone https://github.com/your-user/pinephonepro-lora.git /tmp/pinephonepro-lora
sudo cp -r /tmp/pinephonepro-lora/lora /opt/lora-pkt-fwd

# Install services + CLI
sudo /opt/lora-pkt-fwd/install.sh
```

### 3. Configure for your setup

- **Gateway mode**: edit `/etc/lora-pkt-fwd/global_conf.json` with your ChirpStack IP, gateway EUI, and frequency. Register the gateway EUI in ChirpStack under your tenant.
- **Node mode**: edit `/etc/lora-pkt-fwd/node_conf.json` with your DevEUI, AppEUI, AppKey. Register the device in ChirpStack under the same tenant.

### 4. Run

```bash
lora-mode status    # show current mode + service state
lora-mode set gateway
lora-mode set node
```

## How it works

```
PinePhone Pro motherboard
    └─ i2c@ff150000 (/dev/i2c-5) ── pogo pins ── PineDio back cover
                                                  └─ ATtiny84 (bridge)
                                                       └─ SPI ── Semtech SX1262 LoRa radio
```

Without the DTB fix, `/dev/i2c-5` is silent (controller disabled in kernel device tree). With the fix, the ATtiny84 bridge responds on address `0x28`. From userspace, the entire LoRa stack talks to `/dev/i2c-5:0x28` via smbus2, which translates to SPI commands for the SX1262.

No kernel module compilation, no kernel rebuild, no SD card swap. Just a DTB edit and a Python stack.

## Why single-channel?

The SX1262 is a single-channel radio (one frequency at a time). Real LoRaWAN gateways use 8-channel concentrators like the SX1301/SX1302/SX1303. The PinePhone Pro + PineDio is therefore a tinkering platform or a single-channel backup gateway, not a production 8-channel gateway. The Dragino HP0C or similar SX130x-based hardware is what you want for full coverage.

This stack is tuned to US915 channel 0 (903.0 MHz, SF7, BW125) which is the standard OTAA join frequency. Edit the frequency in `global_conf.json` or `node_conf.json` for other regions.

## Known limitations

- SX1262 = single channel. Pick your frequency carefully.
- postmarketOS kernel has `CONFIG_LORA` and `CONFIG_LORA_SX126X` DISABLED, so the entire LoRa stack is userspace. Performance is fine for ~1 uplink/sec but won't scale to thousands.
- No AES decryption of LoRaWAN downlinks in `lora-node.py` (the OTAA join accepts the frame and increments the frame counter, but application payloads are printed as raw hex). For full MAC compliance, port the LMIC library or use `pylora-modem`.
- Frame counters are in-memory only. Restarting `lora-node.py` resets the counter; ChirpStack will reject until the counter rolls forward. Keep restarts rare.
- No ADR (adaptive data rate). Hard-coded to SF7/BW125.

## Contributing

Patches welcome. If you have:
- A fix for the in-memory frame counter (persist to disk)
- A port of LMIC for proper downlink decryption
- Support for more frequency plans (EU868, AS923, etc.)
- A real device tree patch for upstream pmaports submission

...open a PR. The hardware fix in particular should be filed upstream at https://gitlab.postmarketos.org/postmarketOS/pmaports/ - it's a one-line change.

## References

- PineDio back cover hardware: https://wiki.pine64.org/wiki/Pinedio
- ATtiny84 bridge firmware (megous): https://github.com/megous/pine64-lora
- Bridge protocol Python reference: https://github.com/puurpl/pinephonepro-lora
- Semtech SX1262 datasheet (DS_SX1261-2_V2.1)
- Semtech UDP packet forwarder protocol spec
- Single-channel LoRaWAN reference: https://github.com/jpmeijers/single_chan_pkt_fwd
- SX1262 C reference: https://github.com/lupyuen/lora-sx1262

## License

MIT.

## Credits

Built while debugging a 16-hour PinePhone Pro LoRa bring-up session. The hardware fix (i2c@ff150000 enable) is the actual contribution; the rest is glue.
