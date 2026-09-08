#!/usr/bin/env python3
"""lora-node - OTAA LoRaWAN node for PinePhone Pro + PineDio SX1262.

Uses lora_mac.py (pylorawan for LoRaWAN MAC + bridge.py + sx1262.py for radio).

Status:
- OTAA join + frame counter: works against ChirpStack v4
- Downlink decryption: works (pylorawan decrypt_frm_payload)
- ADR: not implemented (always SF7BW125)
- Frame counters are in-memory only; restart resets counter

Config /etc/lora-pkt-fwd/node_conf.json: dev_eui, app_eui, app_key (hex strings)
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lora_mac import LoRaWANNode

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
    for k in ("dev_eui", "app_eui"):
        if k not in c or len(c[k]) != 16:
            raise SystemExit(f"node_conf.json: missing or invalid {k} (need 16 hex chars)")
    if "app_key" not in c or len(c["app_key"]) != 32:
        raise SystemExit(f"node_conf.json: missing or invalid app_key (need 32 hex chars)")
    return c


def main():
    p = argparse.ArgumentParser()
    p.add_argument("-c", "--config", default=CONF_FILE)
    p.add_argument("--join-only", action="store_true", help="join then exit")
    p.add_argument("--interval", type=int, help="uplink interval in seconds")
    p.add_argument("--payload", help="uplink payload string")
    args = p.parse_args()

    conf = parse_conf(args.config)
    interval = args.interval or conf.get("uplink_interval_s", 60)
    payload = (args.payload or conf.get("uplink_payload", "ping")).encode()

    node = LoRaWANNode(
        dev_eui=conf["dev_eui"],
        app_eui=conf["app_eui"],
        app_key=conf["app_key"],
    )

    if args.join_only:
        ok = node.join()
        sys.exit(0 if ok else 1)

    log.info("starting OTAA join + uplink loop: interval=%ds payload=%r", interval, payload)
    if not node.join():
        log.error("join failed; exiting")
        sys.exit(1)

    while True:
        node.send_uplink(payload)
        pkt = node.listen_window(timeout_s=3.0)
        if pkt:
            log.info("RX packet: %s", pkt.hex())
        time.sleep(interval)


if __name__ == "__main__":
    main()
