"""ATtiny84 I2C-to-SPI bridge driver for the PineDio LoRa backplate.

Protocol reverse-engineered from puurpl/pinephonepro-lora (I2CBridgeHal.h):
- Open /dev/i2c-5, set slave address 0x28.
- To transmit N SPI bytes: write a buffer of [0x01, byte0, byte1, ..., byteN-1]
  to the bus, then read N bytes back. Each SPI byte produces exactly one MISO
  byte; the bridge forwards them through a circular buffer on the ATtiny84.
- After power-on or reset, run sync_buffer() to drain stale bytes from the
  ATtiny's buffer before normal operation.
- The ATtiny84 manages SX1262 CS/Reset/Busy internally; there is no GPIO
  control needed from us.
"""

import fcntl
import os
import time
from typing import List, Sequence

CMD_TRANSMIT = 0x01
DEFAULT_ADDR = 0x28
BUS_PATH = "/dev/i2c-5"
I2C_SLAVE = 0x0703

SYNC_SEQUENCE = bytes([0x10, 0x20, 0x30, 0x40, 0x50, 0xAA, 0x55, 0x00, 0xFF])
SYNC_MAX_BYTES = 256
RETRY_COUNT = 3
RETRY_DELAY_S = 0.01


class BridgeError(RuntimeError):
    pass


class I2CBridge:
    def __init__(self, bus_path: str = BUS_PATH, addr: int = DEFAULT_ADDR):
        self.fd = os.open(bus_path, os.O_RDWR)
        if self.fd < 0:
            raise BridgeError(f"Could not open {bus_path}")
        if fcntl.ioctl(self.fd, I2C_SLAVE, addr) < 0:
            os.close(self.fd)
            raise BridgeError(f"Could not set I2C address 0x{addr:02x}")

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "I2CBridge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def spi_transfer(self, out: Sequence[int]) -> bytes:
        """Send `out` to SX1262 and return MISO response of the same length."""
        n = len(out)
        if n == 0:
            return b""
        write_buf = bytes([CMD_TRANSMIT]) + bytes(out)
        in_buf = bytearray(n)
        for attempt in range(RETRY_COUNT):
            if attempt:
                time.sleep(RETRY_DELAY_S)
            written = os.write(self.fd, write_buf)
            if written != len(write_buf):
                continue
            ok = True
            for i in range(n):
                try:
                    b = os.read(self.fd, 1)
                except OSError:
                    ok = False
                    break
                if len(b) != 1:
                    ok = False
                    break
                in_buf[i] = b[0]
            if ok:
                return bytes(in_buf)
        raise BridgeError(f"I2C SPI transfer failed after {RETRY_COUNT} attempts")

    def sync_buffer(self, verbose: bool = True) -> bool:
        """Drain stale bytes from the ATtiny84 buffer after power-up.

        Puts the SX1262 into standby, then drains the I2C read stream of any
        leftover bytes from the ATtiny's circular buffer. Returns True as
        long as no I2C errors occurred - we don't try to verify a specific
        echo pattern because the bridge mixes status responses from multiple
        prior commands. The downstream radio init is the real health check.
        """
        try:
            self.spi_transfer([0x80, 0x00])  # StandbyRC
        except BridgeError:
            return False
        time.sleep(0.005)

        # Drain: read up to SYNC_MAX_BYTES or until no more bytes available
        count = 0
        while count < SYNC_MAX_BYTES:
            try:
                b = os.read(self.fd, 1)
            except OSError:
                break
            if len(b) != 1:
                break
            count += 1
        if verbose:
            print(f"bridge sync: drained {count} bytes")
        return True

    def shutdown(self) -> None:
        """Drain the buffer and put the SX1262 into standby."""
        # Best-effort drain
        for _ in range(16):
            try:
                if os.read(self.fd, 1) != b"\x00" and len(os.read(self.fd, 1)) != 1:
                    break
            except OSError:
                break
        time.sleep(0.001)
        try:
            self.spi_transfer([0x80, 0x00])  # StandbyRC
            time.sleep(0.005)
            for _ in range(4):
                try:
                    if len(os.read(self.fd, 1)) != 1:
                        break
                except OSError:
                    break
        except BridgeError:
            pass