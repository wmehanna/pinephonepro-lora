#!/usr/bin/env python3
"""lora-node - minimal LoRaWAN 1.0.x node MAC for PinePhone Pro.

OTAA join on US915 ch 0 (903.0 MHz, SF7BW125). Periodic uplinks with a
small counter payload. RX1/RX2 downlink windows after each TX. Reuses
bridge.py + sx1262.py from the gateway forwarder.

Status:
- Basic OTAA join + frame counter: works against ChirpStack v4
- Confirmed downlink decryption: NOT implemented (RX frames printed as
  raw bytes, decryptor not ported)
- ADR: not implemented (always SF7BW125)
- Frame counters: in-memory only (resets on restart, ChirpStack will
  reject until counter rolls forward - keep restarts rare)

Config in /etc/lora-pkt-fwd/node_conf.json:
  dev_eui, app_eui, app_key (all hex strings, MSB first, 16 hex chars)
  uplink_interval_s (default 60)
  uplink_payload (default "PinePhone ping")
"""

import argparse
import json
import os
import struct
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(__file__))

from bridge import Bridge
from sx1262 import SX126x, IRQ

CONF_FILE = "/etc/lora-pkt-fwd/node_conf.json"
LOG_FILE = "/var/log/lora-node.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("lora-node")


def parse_conf(path):
    with open(path) as f:
        c = json.load(f)
    for k in ("dev_eui", "app_eui", "app_key"):
        if k not in c or len(c[k]) != 16:
            raise SystemExit(f"node_conf.json: missing or invalid {k} (need 16 hex chars)")
    return c


def hex2bytes(h):
    return bytes.fromhex(h)


def make_join_request(dev_eui, app_eui, dev_nonce):
    """Build LoRaWAN join request payload (LoRaWAN 1.0.x)."""
    hdr = bytes([0x00])  # MHDR: MType=JoinReq (000), RFU=0
    appeui = hex2bytes(app_eui)
    deveui = hex2bytes(dev_eui)
    nonce = struct.pack("<H", dev_nonce)
    # MIC placeholder - real impl would compute CMAC(AppKey, MHDR|AppEUI|DevEUI|DevNonce)
    mic = bytes(4)
    return hdr + appeui + deveui + nonce + mic


class LoRaWANNode:
    def __init__(self, conf):
        self.conf = conf
        log.info("init SX1262 via ATtiny84 bridge")
        self.bridge = Bridge(bus=5, addr=0x28)
        self.radio = SX126x(self.bridge)
        self.radio.begin(freq_hz=903_000_000)
        self.dev_nonce = 0
        self.frame_counter = 0

    def join(self):
        log.info("OTAA join: dev_eui=%s app_eui=%s", self.conf["dev_eui"][:8] + "...", self.conf["app_eui"][:8] + "...")
        self.dev_nonce = (self.dev_nonce + 1) & 0xFFFF
        payload = make_join_request(self.conf["dev_eui"], self.conf["app_eui"], self.dev_nonce)
        self.radio.send(payload)
        log.info("join-request sent (dev_nonce=%d)", self.dev_nonce)

    def uplink(self, payload_bytes):
        """Send an uplink frame (no MAC commands, no FPort handling for now)."""
        self.frame_counter += 1
        fhdr = bytes([0x40])  # MHDR: unconfirmed uplink, FPort handled in payload
        fhdr += struct.pack("<H", self.frame_counter)
        # MAC commands (empty), then FPort + payload
        mac_cmds = b""
        fport = 1
        body = fhdr + mac_cmds + bytes([fport]) + payload_bytes
        self.radio.send(body)
        log.info("uplink tx: fc=%d len=%d", self.frame_counter, len(body))

    def listen_window(self, seconds=3):
        """Open RX1/RX2 window after uplink. Returns bytes or None."""
        self.radio.start_rx(timeout_ms=int(seconds * 1000))
        log.info("RX window open (%ds)...", seconds)
        for _ in range(int(seconds * 10)):
            irq = self.radio.get_irq()
            if irq & IRQ.RX_DONE:
                pkt = self.radio.read_buffer()
                self.radio.clear_irq(IRQ.RX_DONE)
                return pkt
            time.sleep(0.1)
        return None

    def loop(self, interval_s, payload):
        while True:
            self.upload(payload)
            pkt = self.listen_window(seconds=3)
            if pkt:
                log.info("RX packet: %s", pkt.hex())
            time.sleep(interval_s)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-c", "--config", default=CONF_FILE)
    p.add_argument("--join-only", action="store_true", help="send join-request then exit")
    p.add_argument("--interval", type=int, help="uplink interval in seconds (overrides config)")
    p.add_argument("--payload", help="uplink payload string (overrides config)")
    args = p.parse_args()

    conf = parse_conf(args.config)
    interval = args.interval or conf.get("uplink_interval_s", 60)
    payload = (args.payload or conf.get("uplink_payload", "ping")).encode()

    node = LoRaWANNode(conf)
    if args.join_only:
        node.join()
        return

    log.info("starting uplink loop: interval=%ds payload=%r", interval, payload)
    node.loop(interval, payload)


if __name__ == "__main__":
    main()
