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
      downlink = node.listen_window(seconds=3)
"""

import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge import I2CBridge
from sx1262 import SX1262, IRQ_RX_DONE

from pylorawan.message import MHDR, MType, JoinRequest, MACPayloadUplink, PHYPayload
from pylorawan.common import (
    generate_mic_join_request,
    generate_mic_mac_payload,
    decrypt_frm_payload,
    encrypt_frm_payload,
)
from pylorawan.encryption import aes128_encrypt

log = logging.getLogger("lora_mac")

# US915 downlink (RX1) channel plan is a FIXED set of eight 500 kHz channels,
# completely separate from the 125 kHz uplink channels. RX1 downlink channel
# = uplink_channel % 8. Our uplink is always on sub-band-2 channel 8
# (903.9 MHz), so 8 % 8 = 0 -> downlink channel 0 = 923.3 MHz. Confirmed
# directly from the Dragino gateway's own txpk logs: every join-accept it
# actually transmits shows freq=923.3, datr=SF7BW500.
RX1_FREQ_HZ = 923_300_000
RX_BW_IDX = 0x06  # 500 kHz (SX1262 datasheet Table 13-47; 0x04 is 125 kHz)


def _derive_session_keys(app_key: bytes, dev_nonce: bytes, join_nonce: bytes, dev_addr: bytes) -> tuple:
    """Derive NwkSKey and AppSKey per LoRaWAN 1.0.x spec."""
    base_nwk = bytes([0x01]) + dev_nonce + join_nonce
    nwk_skey = aes128_encrypt(app_key, base_nwk)
    base_app = bytes([0x02]) + dev_nonce + join_nonce
    app_skey = aes128_encrypt(app_key, base_app)
    return nwk_skey, app_skey


def _dev_addr_to_bytes(dev_addr: int) -> bytes:
    return dev_addr.to_bytes(4, "little")


def _dev_nonce_to_bytes(dev_nonce: int) -> bytes:
    return dev_nonce.to_bytes(2, "little")


def _join_nonce_to_bytes(join_nonce: int) -> bytes:
    return join_nonce.to_bytes(3, "little")


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
        # colliding with a nonce ChirpStack already accepted on a prior run
        # (process restarts reset in-memory state, but the server remembers).
        self.dev_nonce = int(time.time()) & 0xFFFF
        self.joined = False
        self.dev_addr = None
        self.nwk_skey = None
        self.app_skey = None
        self.join_nonce = None
        self.rx1_delay_s = 1.0
        self.rx2_delay_s = 2.0
        self.frame_counter = 0

        log.info("init SX1262 via ATtiny84 bridge (US915 ch 0, 903 MHz, SF7 BW125)")
        self.bridge = I2CBridge()
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

    def join(self, timeout_s: float = 8.0) -> bool:
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
        self.radio.transmit(frame)
        self._retune_rx1()
        # RX1 window opens 1s after TX end
        # transmit() blocks with an internal ~1.8s tail sleep after the
        # actual radiate (this back cover cannot confirm TX_DONE any other
        # way), so by the time we get here RX1 (opens 1.0s post-TX) has
        # likely already closed. Listen for a long combined window so we
        # still have real margin to catch RX2 (opens 2.0s post-TX) even
        # accounting for I2C bridge round-trip latency on SetRx.
        # The ambient RF environment here has other LoRaWAN devices
        # transmitting on these same channels roughly every 8-10s (dense
        # local Helium network). A naive "return on first RX_DONE" misses
        # the real join-accept whenever a neighbour's uplink lands in the
        # window first. Keep listening and skip anything that doesn't
        # parse as a JoinAccept until the whole window is used up.
        deadline = time.monotonic() + timeout_s
        phy = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            pkt = self._wait_for_packet(timeout_s=remaining)
            if pkt is None:
                break
            try:
                candidate = PHYPayload.parse(pkt)
            except Exception as e:
                log.info("join: skipping non-parseable packet in RX window: %s", e)
                continue
            if candidate.mhdr.mtype != MType.JoinAccept:
                log.info("join: skipping mtype=%s (not JoinAccept), still listening", candidate.mhdr.mtype)
                continue
            phy = candidate
            break
        if phy is None:
            log.warning("join: no JoinAccept received in %.1fs window", timeout_s)
            return False
        join_accept = phy.payload()
        # Decrypt join-accept payload
        encrypted = join_accept.generate()
        decrypted = aes128_encrypt(self.app_key, encrypted)  # ECB single-block decrypt
        # Parse decrypted payload
        join_nonce = int.from_bytes(decrypted[0:3], "big")
        dev_addr = int.from_bytes(decrypted[3:7], "little")
        # DLSettings + RXDelay follow but we ignore for now
        self.join_nonce = join_nonce
        self.dev_addr = dev_addr
        self.nwk_skey, self.app_skey = _derive_session_keys(
            self.app_key,
            _dev_nonce_to_bytes(self.dev_nonce),
            _join_nonce_to_bytes(join_nonce),
            _dev_addr_to_bytes(dev_addr),
        )
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
        fhdr = mhdr.generate() + _dev_addr_to_bytes(self.dev_addr)[:4] + self.frame_counter.to_bytes(2, "little") + bytes([0x00])
        encrypted_payload = encrypt_frm_payload(
            payload, self.app_skey, self.dev_addr, self.frame_counter, direction=0  # uplink
        )
        mac_payload = fhdr + bytes([fport]) + encrypted_payload
        mic = generate_mic_mac_payload(mhdr, MACPayloadUplink.parse_raw(mac_payload[1:], self.dev_addr, self.frame_counter, 0), self.nwk_skey)
        frame = mhdr.generate() + mac_payload + mic
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
        """Wait up to timeout_s for any received packet."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            irq = self.radio.get_irq()
            if irq & IRQ_RX_DONE:
                pkt = self.radio.receive(timeout_s=0.1)
                if pkt:
                    self.radio.clear_irq(IRQ_RX_DONE)
                    return pkt
            time.sleep(0.05)
        return None
