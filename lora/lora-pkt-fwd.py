#!/usr/bin/env python3
"""LoRaWAN single-channel packet forwarder for the PinePhone Pro / PineDio backplate.

Translates raw LoRa RF packets between the on-board SX1262 (via ATtiny84 I2C
bridge) and a LoRaWAN network server (ChirpStack) using the Semtech UDP
protocol on port 1700.

For a single-channel gateway the "MAC layer" is trivial: the radio receives
raw LoRa frames on the one configured frequency, and we forward them as
Semtech rxpk JSON to the server. The server sends back txpk JSON (downlinks),
which we transmit at the same frequency. No LoRaWAN node-side MAC needed here.

References:
- Semtech packet_forwarder PROTOCOL.TXT (PUSH_DATA / PULL_DATA / PULL_RESP).
- puurpl/pinephonepro-lora (I2CBridgeHal.h) for bridge timing.
"""

import argparse
import base64
import json
import os
import random
import signal
import socket
import struct
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import I2CBridge, BridgeError  # noqa: E402
from sx1262 import SX1262, IRQ_TX_DONE, IRQ_TIMEOUT  # noqa: E402

# Semtech UDP protocol constants
PROTOCOL_VERSION = 2
PUSH_DATA = 0x00
PUSH_ACK = 0x01
PULL_DATA = 0x02
PULL_RESP = 0x03
PULL_ACK = 0x04
TX_ACK = 0x05

KEEPALIVE_S = 10          # PULL_DATA interval
STAT_INTERVAL_S = 30      # PUSH_DATA stats interval (per protocol, PUSH_DATA itself is on-demand)
TXPK_RX_TIMEOUT_S = 0xFFFFFF  # SetRx with infinite timeout (continuous)


def mac_to_gateway_id(mac: str) -> bytes:
    """Convert 'aa:bb:cc:dd:ee:ff' to 8-byte gateway EUI."""
    parts = mac.lower().split(":")
    if len(parts) != 6:
        raise ValueError(f"bad MAC format: {mac}")
    mac_bytes = bytes(int(p, 16) for p in parts)
    # Semtech convention: prepend 0xFFFE
    return bytes([0xFF, 0xFE]) + mac_bytes


class PacketForwarder:
    def __init__(self, conf: dict):
        self.conf = conf
        self.server = conf["server_address"]
        self.port_up = conf["serv_port_up"]
        self.port_down = conf["serv_port_down"]
        self.gateway_id = bytes.fromhex(conf["gateway_ID"])
        assert len(self.gateway_id) == 8, "gateway_ID must be 8 bytes"
        self.freq_hz = float(conf["frequency_hz"])
        self.modulation = conf.get("modulation", "LORA")
        self.datarate = conf.get("datarate", "SF7BW125")
        self.codr = conf.get("coding_rate", "4/5")
        self.sf, self.bw_khz = self._parse_datarate(self.datarate)
        self.sync_word = int(conf.get("sync_word", "0x3444"), 16)
        self.preamble_len = int(conf.get("preamble_len", 8))
        self.tx_power = int(conf.get("tx_power_dbm", 14))

        # Net state
        self._stat = {
            "time": "",
            "rxnb": 0, "rxok": 0, "rxfw": 0, "ackr": 0.0,
            "dwnb": 0, "txnb": 0,
        }
        self._stop = threading.Event()
        self._tx_done = threading.Event()  # signals TX finished so RX thread can resume
        self._sock_lock = threading.Lock()
        self._txpk_queue: list[dict] = []
        self._last_payload: bytes | None = None  # dedup identical packets
        self._last_push_ts = 0.0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port_up))

    @staticmethod
    def _parse_datarate(d: str) -> tuple[int, int]:
        # Expect "SF7BW125" -> (7, 125)
        if not d.startswith("SF"):
            raise ValueError(f"bad datarate: {d}")
        rest = d[2:]
        bw_end = rest.find("BW")
        sf = int(rest[:bw_end])
        bw = int(rest[bw_end + 2:])
        return sf, bw

    @staticmethod
    def _bw_to_idx(bw_khz: int) -> int:
        # SX1262 LoRa BW index: 5=31.25, 6=41.7, 7=62.5, 8=125, 9=250, 10=500
        return {31.25: 5, 41.7: 6, 62.5: 7, 125: 8, 250: 9, 500: 10}.get(bw_khz, 8)

    def _log(self, msg: str) -> None:
        sys.stdout.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stdout.flush()

    # ---------- UDP I/O ----------

    def _send(self, kind: int, payload: bytes) -> int:
        """Build a Semtech UDP frame and send it. Returns the token used."""
        token = random.randint(0, 0xFFFF)
        frame = bytes([PROTOCOL_VERSION]) + struct.pack(">H", token) + bytes([kind])
        if kind in (PUSH_DATA, PULL_DATA, TX_ACK):
            frame += self.gateway_id
        # PUSH_DATA + TX_ACK carry a JSON payload. PULL_DATA is sent with
        # NO payload - some gateway-bridge versions (notably
        # chirpstack-gateway-bridge <= 3.x) reject PULL_DATA frames that
        # include extra bytes beyond the 12-byte header.
        if kind in (PUSH_DATA, TX_ACK):
            frame += payload
        with self._sock_lock:
            self._sock.sendto(frame, (self.server, self.port_up))
        return token

    def _send_push(self, rxpk: list[dict]) -> int:
        body = {"stat": self._make_stat()}
        if rxpk:
            body["rxpk"] = rxpk
        payload = json.dumps(body).encode()
        token = self._send(PUSH_DATA, payload)
        self._stat["rxnb"] += len(rxpk)
        self._stat["rxok"] += len(rxpk)
        self._stat["rxfw"] += len(rxpk)
        return token

    def _send_pull(self) -> int:
        body = {"stat": self._make_stat()}
        payload = json.dumps(body).encode()
        return self._send(PULL_DATA, payload)

    def _send_txack(self, token: int, error: str = "NONE") -> None:
        body = {"txpk_ack": {"error": error}}
        payload = json.dumps(body).encode()
        self._send(TX_ACK, payload)

    def _make_stat(self) -> dict:
        s = dict(self._stat)
        s["time"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        return s

    def _rx_thread(self) -> None:
        """Listen for PULL_ACK / PULL_RESP / PUSH_ACK from the server."""
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(4096)
            except OSError:
                return
            if len(data) < 4:
                continue
            ver, token, kind = data[0], struct.unpack(">H", data[1:3])[0], data[3]
            if ver != PROTOCOL_VERSION:
                continue
            if kind == PULL_ACK:
                self._log("PULL_ACK")
            elif kind == PUSH_ACK:
                pass  # track in stat.ackr
            elif kind == PULL_RESP:
                try:
                    msg = json.loads(data[4:].decode())
                except Exception as e:
                    self._log(f"PULL_RESP parse error: {e}")
                    continue
                txpk = msg.get("txpk")
                if not txpk:
                    continue
                self._log(f"PULL_RESP txpk freq={txpk.get('freq')} size={txpk.get('size')}")
                self._txpk_queue.append(txpk)

    # ---------- main loops ----------

    def run(self) -> None:
        bridge = I2CBridge()
        try:
            if not bridge.sync_buffer(verbose=False):
                raise RuntimeError("bridge sync failed")

            radio = SX1262(bridge)
            bw_idx = self._bw_to_idx(self.bw_khz)
            radio.init(
                freq_hz=int(self.freq_hz),
                tx_power_dbm=self.tx_power,
                sf=self.sf,
                bw_idx=bw_idx,
                cr_idx=1,  # 4/5
                preamble_len=self.preamble_len,
                sync_word=self.sync_word,
            )
            self._log(f"radio init OK: freq={self.freq_hz}Hz SF{self.sf} BW{self.bw_khz}kHz")
        except Exception:
            traceback.print_exc()
            bridge.close()
            raise

        threads = [
            threading.Thread(target=self._rx_thread, daemon=True, name="udp-rx"),
            threading.Thread(target=self._pull_loop, daemon=True, name="pull"),
            threading.Thread(target=self._push_loop, args=(radio,), daemon=True, name="rx-loop"),
            threading.Thread(target=self._tx_drain_loop, args=(radio,), daemon=True, name="tx-drain"),
        ]
        for t in threads:
            t.start()

        # wait for stop
        try:
            while not self._stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
            bridge.shutdown()
            bridge.close()
            self._sock.close()

    def _pull_loop(self) -> None:
        """Periodic PULL_DATA every KEEPALIVE_S."""
        while not self._stop.is_set():
            try:
                self._send_pull()
            except Exception as e:
                self._log(f"PULL send error: {e}")
            self._stop.wait(KEEPALIVE_S)

    def _push_loop(self, radio: SX1262) -> None:
        """RX loop: put radio in continuous RX, on RX_DONE push the rxpk.

        Stops RX while a TX is being performed (SX1262 is half-duplex).
        """
        try:
            radio._set_command(0x82, bytes([0xFF, 0xFF, 0xFF]))  # SetRx continuous
        except Exception as e:
            self._log(f"SetRx failed: {e}")
            return

        deadline = time.monotonic() + 0.5
        while not self._stop.is_set():
            if self._tx_done.is_set():
                # A TX just finished; resume RX
                self._tx_done.clear()
                try:
                    radio._set_command(0x82, bytes([0xFF, 0xFF, 0xFF]))
                except Exception as e:
                    self._log(f"SetRx resume failed: {e}")
                time.sleep(0.05)

            try:
                irq = radio.get_irq()
            except Exception as e:
                self._log(f"IRQ read error: {e}")
                time.sleep(0.05)
                continue

            if irq & 0x0002:  # RX_DONE
                try:
                    radio.clear_irq()
                    r = radio._cmd(0x13, bytes([0x00] * 7))  # 8-byte GetRxBufferStatus
                    plen = r[5]
                    start = r[6]
                    if plen == 0 or plen > 255:
                        # Bad payload length; re-arm RX and skip
                        try:
                            radio._set_command(0x82, bytes([0xFF, 0xFF, 0xFF]))
                        except Exception:
                            pass
                        continue
                    read_len = plen + 8  # pad to clear preamble
                    buf = radio._cmd(0x1E, bytes([start] + [0x00] * (read_len - 1)))
                    payload = bytes(buf[4 : 4 + plen])
                except Exception as e:
                    self._log(f"RX read error: {e}")
                    continue

                rxpk = [{
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "tmst": int(time.time() * 1_000_000) & 0xFFFFFFFF,
                    "freq": self.freq_hz / 1_000_000.0,
                    "chan": 0,
                    "rfch": 0,
                    "stat": 1,
                    "modu": "LORA",
                    "datr": self.datarate,
                    "codr": self.codr,
                    "rssi": -50,
                    "lsnr": 7.5,
                    "size": len(payload),
                    "data": base64.b64encode(payload).decode(),
                }]
                try:
                    # Dedup: skip identical payloads in quick succession, and
                    # rate-limit to <= 1 PUSH_DATA per second (Semtech spec).
                    now = time.monotonic()
                    same_payload = (payload == self._last_payload)
                    rate_ok = (now - self._last_push_ts) >= 1.0
                    if same_payload or not rate_ok:
                        # still re-arm RX below; don't flood ChirpStack
                        pass
                    else:
                        self._send_push(rxpk)
                        self._last_payload = payload
                        self._last_push_ts = now
                        self._log(f"RX {len(payload)}B -> pushed")
                except Exception as e:
                    self._log(f"PUSH error: {e}")

                # Re-enter RX
                try:
                    radio._set_command(0x82, bytes([0xFF, 0xFF, 0xFF]))
                except Exception:
                    pass

            elif irq & 0x0200:  # TIMEOUT
                radio.clear_irq(0x0200)
            elif irq & 0x0040:  # CRC_ERROR
                self._log("CRC_ERROR")
                radio.clear_irq()
                try:
                    radio._set_command(0x82, bytes([0xFF, 0xFF, 0xFF]))
                except Exception:
                    pass
            elif irq & 0x0020:  # HEADER_ERROR
                self._log("HEADER_ERROR")
                radio.clear_irq()
                try:
                    radio._set_command(0x82, bytes([0xFF, 0xFF, 0xFF]))
                except Exception:
                    pass
            else:
                time.sleep(0.005)


    def _tx_drain_loop(self, radio: SX1262) -> None:
        """Consume PULL_RESP txpks queued by _rx_thread and transmit them."""
        while not self._stop.is_set():
            if not self._txpk_queue:
                time.sleep(0.1)
                continue
            txpk = self._txpk_queue.pop(0)
            try:
                payload = base64.b64decode(txpk["data"])
            except Exception as e:
                self._log(f"TX data decode error: {e}")
                continue
            try:
                # SX1262 is half-duplex: take it out of continuous RX by going to standby.
                radio._set_command(0x80, bytes([0x00]))  # SetStandby(STDBY_RC)
                time.sleep(0.01)
                # Set TX buffer base = 0
                radio._set_command(0x8F, bytes([0x00, 0x00]))
                radio._set_command(0x0E, bytes([0x00]) + payload)
                # SetTx single-shot
                radio._set_command(0x83, bytes([0x00, 0x00, 0x00]))
                # Wait for TX_DONE
                end = time.monotonic() + 5.0
                while time.monotonic() < end:
                    irq = radio.get_irq()
                    if irq & IRQ_TX_DONE:
                        radio.clear_irq()
                        self._stat["dwnb"] += 1
                        self._stat["txnb"] += 1
                        self._log(f"TX {len(payload)}B done")
                        break
                    if irq & IRQ_TIMEOUT:
                        radio.clear_irq()
                        self._log("TX timeout")
                        break
                    time.sleep(0.01)
            except Exception as e:
                self._log(f"TX error: {e}")
                traceback.print_exc()
            finally:
                # Tell RX thread to resume
                self._tx_done.set()


def load_conf(path: str) -> dict:
    with open(path) as f:
        c = json.load(f)
    # Single-channel convenience: pick the first channel
    if "frequency_hz" not in c and "radio_0" in c:
        chans = c["radio_0"]["channels"]
        c["frequency_hz"] = chans[0]["freq_hz"]
        c["datarate"] = c["radio_0"].get("modulation", "LORA") and (
            f"SF{chans[0].get('sf', 7)}BW{int(chans[0].get('bw_khz', 125))}"
        )
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="/etc/lora-pkt-fwd/global_conf.json")
    args = ap.parse_args()
    conf = load_conf(args.config)
    fwd = PacketForwarder(conf)
    fwd.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())