"""SX1262 register-level driver over the ATtiny84 I2C bridge.

Provides:
- Init: SetSleep(0), SetStandby(STDBY_RC), SetPacketType(LORA),
  SetRfFrequency, SetPaConfig, SetTxParams, SetModulationParams,
  SetPacketParams, SetDioIrqParams, ClearIrqStatus.
- RX: SetRx(timeout), poll IRQ status, ReadBuffer on RX_DONE.
- TX: SetBufferBaseAddress, WriteBuffer, SetTx.

References:
- Semtech SX1262 datasheet rev 2.1.
- lupyuen/lora-sx1262 (C reference for command sequences).
"""

import time
from typing import Optional

from bridge import I2CBridge

# SX1262 SPI opcodes
OP_SET_SLEEP = 0x84
OP_SET_STANDBY = 0x80
OP_SET_PACKET_TYPE = 0x8A
OP_SET_RF_FREQUENCY = 0x86
OP_SET_PA_CONFIG = 0x95
OP_SET_TX_PARAMS = 0x8E
OP_SET_MODULATION_PARAMS = 0x8B
OP_SET_PACKET_PARAMS = 0x8C
OP_SET_BUFFER_BASE = 0x8F
OP_WRITE_BUFFER = 0x0E
OP_READ_BUFFER = 0x1E
OP_SET_TX = 0x83
OP_SET_RX = 0x82
OP_GET_STATUS = 0xC0
OP_GET_IRQ_STATUS = 0x12
OP_CLEAR_IRQ_STATUS = 0x02
OP_GET_RX_BUFFER_STATUS = 0x13
OP_GET_PACKET_STATUS = 0x14
OP_SET_DIO_IRQ_PARAMS = 0x08

# Standby modes
STDBY_RC = 0x00
STDBY_XOSC = 0x01

# Packet type
PACKET_TYPE_LORA = 0x01

# IRQ flags
IRQ_TX_DONE = 0x0001
IRQ_RX_DONE = 0x0002
IRQ_PREAMBLE_DETECTED = 0x0004
IRQ_SYNC_WORD_VALID = 0x0008
IRQ_HEADER_VALID = 0x0010
IRQ_HEADER_ERROR = 0x0020
IRQ_CRC_ERROR = 0x0040
IRQ_CAD_DONE = 0x0080
IRQ_CAD_DETECTED = 0x0100
IRQ_TIMEOUT = 0x0200

# Power amplifier selection (SX1262 only, not SX1261)
PA_SX1262 = 0x00
PA_SX1261 = 0x01

# Ramp time (10..3440 ms in steps of 10; in LoRa use values in register table)
RAMP_200U = 0x04  # 200us, fastest non-extreme ramp

# Band EU868 default; frequency set by frequency_hz
RF_FREQ_XTAL_HZ = 32_000_000

# Default LoRa modulation (SF7 BW125 CR4/5 explicit header)
DEFAULT_SF = 7
DEFAULT_BW_IDX = 7   # 125 kHz
DEFAULT_CR_IDX = 1   # 4/5


class SX1262Error(RuntimeError):
    pass


class SX1262:
    def __init__(self, bridge: I2CBridge):
        self.b = bridge

    # ---------- raw helpers ----------

    def _cmd(self, opcode: int, payload: bytes = b"") -> bytes:
        """Send a SPI command with payload, return MISO bytes of equal length."""
        out = bytes([opcode]) + payload
        return self.b.spi_transfer(list(out))

    def _set_command(self, opcode: int, payload: bytes = b"") -> None:
        """Fire-and-forget command (ignores MISO)."""
        self._cmd(opcode, payload)

    # ---------- init ----------

    def init(
        self,
        freq_hz: int,
        tx_power_dbm: int = 14,
        sf: int = DEFAULT_SF,
        bw_idx: int = DEFAULT_BW_IDX,
        cr_idx: int = DEFAULT_CR_IDX,
        preamble_len: int = 8,
        sync_word: int = 0x3444,
        payload_len: int = 0,   # 0 = explicit header
    ) -> None:
        """Bring the SX1262 into a clean LORA RX-ready state at the given freq.

        Matches the standard SX1262 LoRa init sequence from the datasheet.
        """
        # 1. Sleep with warm-start retention (set bit 0 of param = 0)
        self._set_command(OP_SET_SLEEP, bytes([0x00]))
        time.sleep(0.005)

        # 2. Standby using RC oscillator
        self._set_command(OP_SET_STANDBY, bytes([STDBY_RC]))
        time.sleep(0.001)

        # 3. Packet type LORA
        self._set_command(OP_SET_PACKET_TYPE, bytes([PACKET_TYPE_LORA]))

        # 4. Set RF frequency. SX1262 expects (freq_hz * 2^25) / 32e6 as 4 bytes BE.
        rf_reg = int((freq_hz << 25) / RF_FREQ_XTAL_HZ) & 0xFFFFFFFF
        self._set_command(OP_SET_RF_FREQUENCY, rf_reg.to_bytes(4, "big"))

        # 5. PA config: SX1262, +14 dBm, 4 bits duty, 3 bits hp max
        # paDutyCycle = 0x04, hpMax = 0x07, deviceSel = PA_SX1262
        self._set_command(OP_SET_PA_CONFIG, bytes([0x04, 0x07, PA_SX1262, 0x00]))

        # 6. TX params: power in dBm (signed), ramp time
        self._set_command(OP_SET_TX_PARAMS, bytes([tx_power_dbm & 0xFF, RAMP_200U]))

        # 7. Modulation params: SF, BW, CR
        # SF6 is encoded as 0 in LoRa but 6 in spread - we keep SF >= 7 here
        sf_reg = sf & 0xFF
        self._set_command(OP_SET_MODULATION_PARAMS, bytes([sf_reg, bw_idx, cr_idx]))

        # 8. Packet params: 16-bit preamble length, header type (0=variable/explicit),
        # payload length (0 = explicit header mode), CRC type (0=off, 1=on),
        # IQ inversion (0=false)
        preamble = preamble_len & 0xFFFF
        header_type = 0x00 if payload_len == 0 else 0x01
        crc_on = 0x01
        self._set_command(OP_SET_PACKET_PARAMS, bytes([
            preamble.to_bytes(2, "big")[0],
            preamble.to_bytes(2, "big")[1],
            header_type,
            payload_len & 0xFF,
            crc_on,
            0x00,
        ]))

        # 9. Sync word (LoRa). Set as 2 bytes, MSB first (LoRa: 0x74 for private)
        # For private LoRaWAN we use 0x12
        sw = sync_word & 0xFFFF
        # WriteBuffer op with offset 0x07 stores the LoRa sync word in the data buffer
        # Actually SX1262 stores the LoRa sync word via reg addr 0x0740-0x0741,
        # but the data-sheet procedure uses WriteBuffer with offset=0x07.
        self._set_command(OP_WRITE_BUFFER, bytes([0x07, (sw >> 8) & 0xFF, sw & 0xFF]))

        # 10. IRQ params: route all useful IRQs to DIO1 (we poll, but configure anyway).
        # 4 masks of 2 bytes each = 8 bytes total
        irq_mask = IRQ_TX_DONE | IRQ_RX_DONE | IRQ_TIMEOUT | IRQ_CRC_ERROR | IRQ_HEADER_ERROR
        mask_b = irq_mask.to_bytes(2, "big")
        self._set_command(OP_SET_DIO_IRQ_PARAMS, mask_b * 4)

        # 11. Set buffer base address: tx=0, rx=128 (separate halves)
        self._set_command(OP_SET_BUFFER_BASE, bytes([0x00, 0x80]))

        # 12. Clear any pending IRQs
        self._set_command(OP_CLEAR_IRQ_STATUS, bytes([0xFF, 0xFF]))

    # ---------- IRQ ----------

    def get_irq(self) -> int:
        """Return the 16-bit IRQ status word.

        SX1262 GetIrqStatus via ATtiny84 bridge: send [0x12, 0x00, 0x00, 0x00]
        (opcode + 3 NOPs = 4 bytes total), get back [status, NOP, IRQ_H, IRQ_L].
        IRQ word is at response positions [2..3].
        """
        r = self._cmd(OP_GET_IRQ_STATUS, bytes([0x00, 0x00, 0x00]))  # 4-byte transfer
        return (r[2] << 8) | r[3]

    def clear_irq(self, mask: int = 0xFFFF) -> None:
        self._set_command(OP_CLEAR_IRQ_STATUS, bytes([(mask >> 8) & 0xFF, mask & 0xFF]))

    # ---------- TX ----------

    def transmit(self, payload: bytes) -> None:
        """Send a single LoRa packet at the configured frequency.

        On the PineDio back cover, the SX1262 IRQ register readback returns
        static garbage (0xa6a6) so IRQ polling for TX_DONE is unreliable.
        Instead: kick SetTx, sleep a conservative TX duration (SF7/BW125
        packet of N bytes takes roughly 70ms; 1.8s is a safe margin that
        also covers the ~1.4s TCXO warm-up delay seen on this chip).
        """
        if len(payload) > 255:
            raise SX1262Error(f"payload too long: {len(payload)}")
        # Standby first - SX1262 must be in standby before SetTx
        self._set_command(OP_SET_STANDBY, bytes([0x01]))  # STDBY_XOSC, keep TCXO warm
        time.sleep(0.01)
        # Point TX at base 0, write the payload, kick TX with finite timeout
        self._set_command(OP_SET_BUFFER_BASE, bytes([0x00, 0x80]))
        self._set_command(OP_WRITE_BUFFER, bytes([0x00]) + payload)
        # SetTx with ~500ms on-chip timeout (0x7D00 = 32000 ticks * 15.625us = 500ms)
        self._set_command(OP_SET_TX, bytes([0x00, 0x7D, 0x00]))
        # PineDio quirk: SetTx takes ~1.4s to actually transition chip to TX.
        # Sleep 1.8s (covers TCXO + TX duration at SF7 BW125).
        time.sleep(1.8)
        # After TX, return to standby so next SetTx works
        self._set_command(OP_SET_STANDBY, bytes([0x01]))
        # Best-effort IRQ clear
        try:
            self.clear_irq()
        except Exception:
            pass

    # ---------- RX ----------

    def receive(self, timeout_s: float) -> Optional[bytes]:
        """Put the radio in RX for up to timeout_s. Return payload bytes
        on RX_DONE, or None on TIMEOUT / CRC error."""
        # Convert seconds to LoRa time steps: 15.625us per tick
        # 0xFFFFFF = ~15.7s, 0 = single shot, ~anything = timeout
        ticks = int(timeout_s * 64_000)  # 1 tick = 15.625us = 1/64000 s
        ticks = min(ticks, 0xFFFFFF)
        if timeout_s <= 0:
            param = 0xFFFFFF
        else:
            param = ticks & 0xFFFFFF
        self._set_command(OP_SET_RX, bytes([
            (param >> 16) & 0xFF,
            (param >> 8) & 0xFF,
            param & 0xFF,
        ]))
        deadline = time.monotonic() + timeout_s + 0.1
        while time.monotonic() < deadline:
            irq = self.get_irq()
            if irq & (IRQ_RX_DONE | IRQ_TIMEOUT | IRQ_HEADER_ERROR | IRQ_CRC_ERROR):
                self.clear_irq()
                if irq & IRQ_RX_DONE:
                    # GetRxBufferStatus: MOSI [0x13, NOP, NOP, NOP]
                    # MISO: [chip_status, payload_length, rx_start_offset, junk]
                    r = self._cmd(OP_GET_RX_BUFFER_STATUS, bytes([0x00, 0x00, 0x00]))
                    plen = r[1]
                    start = r[2]
                    if plen == 0 or plen > 255:
                        return None
                    # ReadBuffer: MOSI [0x1E, offset, NOP*N]
                    # MISO: [chip_status, NOP, data0, data1, ...]
                    read_len = plen + 2
                    buf = self._cmd(OP_READ_BUFFER, bytes([start] + [0x00] * (read_len - 1)))
                    return bytes(buf[2 : 2 + plen])
                return None
            time.sleep(0.01)
        return None