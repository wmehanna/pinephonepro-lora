"""LoRaWAN MAC glue layer for PinePhone Pro + PineDio SX1262.

Combines:
- bridge.py + sx1262.py for the radio (I2CBridge + register-level driver)
- pylorawan/ for the LoRaWAN 1.0.x MAC (MIC, encryption, JoinAccept parsing, key derivation)

Single import: from lora_mac import LoRaWANNode

Usage:
  from lora_mac import LoRaWANNode
  node = LoRaWANNode(dev_eui, app_eui, app_key)
  if node.join():
      node.send_uplink(b"hello")
      downlink = node.listen_window(timeout_s=3)
"""

import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge import I2CBridge
from sx1262 import SX1262

from pylorawan.message import MHDR, MType, JoinRequest, JoinAccept, MACPayloadUplink
from pylorawan.common import (
    generate_mic_join_request,
    generate_nwk_s_key,
    generate_app_s_key,
    generate_mic_mac_payload,
    encrypt_frm_payload,
)

log = logging.getLogger("lora_mac")

# US915 downlink (RX1) channel plan is a FIXED set of eight 500 kHz channels,
# completely separate from the 125 kHz uplink channels. RX1 downlink channel
# = uplink_channel % 8. Our uplink is always on sub-band-2 channel 8
# (903.9 MHz), so 8 % 8 = 0 -> downlink channel 0 = 923.3 MHz. Confirmed
# directly from the Dragino gateway's own txpk logs: every join-accept it
# actually transmits shows freq=923.3, datr=SF7BW500.
RX1_FREQ_HZ = 923_300_000
RX_BW_IDX = 0x06  # 500 kHz (SX1262 datasheet Table 13-47; 0x04 is 125 kHz)


def _dev_addr_to_bytes(dev_addr: int) -> bytes:
    return dev_addr.to_bytes(4, "little")


class LoRaWANNode:
    """High-level OTAA LoRaWAN node for PinePhone Pro + PineDio SX1262.

    After successful join(), the node holds DevAddr, NwkSKey, AppSKey, frame_counter.
    Downlinks arrive via listen_window().
    """

    def __init__(self, dev_eui: str, app_eui: str, app_key: str, freq_hz: int = 903_900_000):
        self.dev_eui = bytes.fromhex(dev_eui)
        self.app_eui = bytes.fromhex(app_eui)
        self.app_key = bytes.fromhex(app_key)
        self.freq_hz = freq_hz
        # DevNonce must never repeat for this DevEUI+JoinEUI pair per
        # LoRaWAN spec. Seeding from wall-clock time instead of 0 avoids
        # immediate restart collisions. This is not persistent nonce storage:
        # clock rollback or a 16-bit wrap can still reuse a nonce.
        self.dev_nonce = int(time.time()) & 0xFFFF
        self.joined = False
        self.dev_addr = None
        self.nwk_skey = None
        self.app_skey = None
        self.join_nonce = None
        self.rx1_delay_s = 1.0
        self.rx2_delay_s = 2.0
        self.frame_counter = 0

        log.info("init SX1262 via ATtiny84 bridge (US915 ch 8, 903.9 MHz, SF7 BW125)")
        self.bridge = I2CBridge()
        if not self.bridge.sync_buffer(verbose=False):
            raise RuntimeError("ATtiny84 bridge synchronization failed")
        self.radio = SX1262(self.bridge)
        # One full init() at construction sets everything that never changes
        # again (packet params, sync word, IRQ params, buffer base) and
        # caches the values transmit() needs on the SX1262 object. Every
        # later channel switch uses the fast radio.retune() path instead.
        self.radio.init(
            freq_hz=self.freq_hz,
            sf=7,
            bw_idx=0x04,     # 125 kHz (SX1262 BW index, datasheet Table 13-47)
            cr_idx=1,        # 4/5
            preamble_len=8,
            sync_word=0x3444,
            tx_power_dbm=14,
        )

    def _retune_uplink(self) -> None:
        """(Re)tune the radio to the uplink channel: 903.9 MHz / 125 kHz.

        Must be called before every transmit(). The radio is shared between
        TX and RX and gets retuned to a completely different frequency and
        bandwidth for RX1 (see _retune_rx1), so without this every transmit
        after the first RX1 listen would go out on the wrong channel too.
        Uses the fast path (radio.retune), not the full init() sequence,
        RX1/RX2 are narrow timed windows and the extra ~7 round-trips the
        full init() does (PA config, TX params, sync word, IRQ params,
        buffer base, none of which actually change here) were eating
        enough wall-clock time to plausibly miss them.
        """
        self.radio.retune(freq_hz=self.freq_hz, sf=7, bw_idx=0x04, cr_idx=1)

    def _retune_rx1(self) -> None:
        """Retune to the US915 RX1 downlink channel: 923.3 MHz / 500 kHz.

        US915 RX1 uses a FIXED set of eight 500 kHz downlink channels,
        completely separate from the 125 kHz uplink channels (RX1 channel
        = uplink_channel % 8). Our uplink is always sub-band-2 channel 8
        (903.9 MHz), so 8 % 8 = 0 -> downlink channel 0 = 923.3 MHz.
        Confirmed directly from the Dragino gateway's own txpk logs: every
        join-accept and downlink it actually transmits shows freq=923.3,
        datr=SF7BW500.
        """
        self.radio.retune(freq_hz=RX1_FREQ_HZ, sf=7, bw_idx=RX_BW_IDX, cr_idx=1)

    def join(self, timeout_s: float = 15.0) -> bool:
        """Send OTAA join-request and listen for join-accept.

        Returns True if join succeeded, False if timeout/no accept.
        """
        self.dev_nonce = (self.dev_nonce + 1) & 0xFFFF
        mhdr = MHDR(mtype=MType.JoinRequest, major=0)
        join_req = JoinRequest(
            app_eui=int.from_bytes(self.app_eui, "big"),
            dev_eui=int.from_bytes(self.dev_eui, "big"),
            dev_nonce=self.dev_nonce,
        )
        mic = generate_mic_join_request(mhdr, join_req, self.app_key)
        frame = mhdr.generate() + join_req.generate() + mic
        log.info("OTAA join: dev_eui=%s dev_nonce=%d", self.dev_eui.hex(), self.dev_nonce)
        self._retune_uplink()
        t_tx_start = time.monotonic()
        self.radio.transmit(frame)
        t_tx_done = time.monotonic()
        self._retune_rx1()
        t_listen_start = time.monotonic()
        log.info(
            "join timing: tx_call=%.3fs retune_rx1=%.3fs (tx_start->listen_start=%.3fs)",
            t_tx_done - t_tx_start, t_listen_start - t_tx_done, t_listen_start - t_tx_start,
        )
        # JoinAccept uses JOIN_ACCEPT_DELAY1=5s (not the data RXDelay).
        # Arm early and keep RX continuous through that scheduled downlink.
        deadline = time.monotonic() + timeout_s
        join_accept = None
        while time.monotonic() < deadline:
            pkt = self._wait_for_packet(timeout_s=deadline - time.monotonic())
            if pkt is None:
                break
            if len(pkt) not in (17, 33) or pkt[0] != 0x20:
                log.info("join: skipping non-JoinAccept packet")
                continue
            try:
                # This parser decrypts the complete ciphertext (including MIC)
                # and verifies the MIC before returning any session fields.
                join_accept = JoinAccept.parse(pkt, self.app_key)
            except Exception as e:
                log.warning("join: invalid JoinAccept: %s", e)
                continue
            break
        if join_accept is None:
            log.warning("join: no JoinAccept received in %.1fs window", timeout_s)
            return False
        join_nonce = join_accept.app_nonce
        dev_addr = join_accept.dev_addr
        self.join_nonce = join_nonce
        self.dev_addr = dev_addr
        self.nwk_skey = generate_nwk_s_key(self.app_key, join_nonce, join_accept.net_id, self.dev_nonce)
        self.app_skey = generate_app_s_key(self.app_key, join_nonce, join_accept.net_id, self.dev_nonce)
        self.rx1_delay_s = float(join_accept.rx_delay or 1)
        self.rx2_delay_s = self.rx1_delay_s + 1
        self.joined = True
        log.info("joined: dev_addr=%08x join_nonce=%06x", dev_addr, join_nonce)
        return True

    def send_uplink(self, payload: bytes, fport: int = 1) -> bool:
        """Send an unconfirmed data uplink. Returns True if TX done."""
        if not self.joined:
            log.warning("send_uplink: not joined")
            return False
        self.frame_counter += 1
        mhdr = MHDR(mtype=MType.UnconfirmedDataUp, major=0)
        fhdr = (_dev_addr_to_bytes(self.dev_addr) + bytes([0x00])
                + (self.frame_counter & 0xFFFF).to_bytes(2, "little"))
        encrypted_payload = encrypt_frm_payload(
            payload, self.app_skey, self.dev_addr, self.frame_counter, direction=0
        )
        mac_payload = MACPayloadUplink.parse(fhdr + bytes([fport]) + encrypted_payload)
        mac_payload.fhdr.f_cnt = self.frame_counter
        mic = generate_mic_mac_payload(mhdr, mac_payload, self.nwk_skey)
        frame = mhdr.generate() + mac_payload.generate() + mic
        log.info("uplink tx: fc=%d len=%d", self.frame_counter, len(frame))
        self._retune_uplink()
        self.radio.transmit(frame)
        return True

    def listen_window(self, timeout_s: float = 3.0):
        """Open RX window, wait for packet. Returns raw bytes or None."""
        self._retune_rx1()
        log.info("RX window open (%ds)...", timeout_s)
        return self._wait_for_packet(timeout_s=timeout_s)

    def _wait_for_packet(self, timeout_s: float):
        """Wait up to timeout_s for any received packet.

        radio.receive() is what actually issues SetRx and arms the chip to
        listen; polling get_irq() alone (the old approach here) never arms
        RX at all; after a transmit() the chip sits in standby forever and
        RX_DONE can never legitimately set. Keep receive() armed until the
        deadline, re-entering only after a packet or error.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            # Keep RX armed across the whole remaining join window.
            try:
                pkt = self.radio.receive(timeout_s=remaining, downlink=True)
            except Exception as e:
                log.info("_wait_for_packet: receive() raised %s, resyncing bridge", e)
                try:
                    self.bridge.sync_buffer(verbose=False)
                except Exception:
                    pass
                time.sleep(0.05)
                continue
            if pkt:
                return pkt
