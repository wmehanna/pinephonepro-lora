# OTAA verification, 2026-09-10

PinePhone Pro / PineDio SX1262 joined the existing ChirpStack v4 server through
Dragino HP0C without gateway or server configuration changes.

First successful session:

- DevEUI: `d62e556244670000`; DevAddr: `012a6446`.
- JoinRequest DevNonce: 13788.
- Phone received `2047bcde4470bd428178b43a559dbd25ef` at
  04:45:21 UTC, IRQ `0x0002`, RSSI -61 dBm, SNR 11 dB.
- Dragino's downlink `data=IEe83kRwvUKBeLQ6VZ29Je8=` is the identical frame,
  at 923.3 MHz, SF7BW500, inverted IQ. The phone verified its JoinAccept MIC.
- ChirpStack accepted the first data uplink and published `event/up`.
  A read-only PostgreSQL query of the device record returned
  `last_seen_at=2026-09-10 04:45:21.983217+00`.
- The second periodic uplink advanced that timestamp to
  `2026-09-10 04:46:27.480534+00`.

The final code also joined successfully after a service restart at 04:47:41 UTC,
using DevAddr `005595df`, RSSI -50 dBm and SNR 12 dB.

## Fixed blockers

1. RX retained normal TX IQ; LoRaWAN downlinks require inverted IQ. Set packet
   parameters for each direction, including the SX126x IQ-polarity erratum.
2. LoRa SetModulationParams lacked the fourth parameter (LDRO).
3. ReadBuffer omitted the dummy byte and included status as payload.
4. The bridge read cursor was not synchronized. Its firmware cycles through a
   128-byte response buffer even when no fresh data exists. Blindly draining
   256 bytes cannot repair a cursor offset. Write/read a known marker and stop
   exactly at its end before initializing the radio. Repeated `0xa2a2` / `0xa2d2`
   values disappeared after synchronization; reboot alone did not fix them.
5. Generic PHYPayload.parse does not support JoinAccept. Use JoinAccept.parse,
   which decrypts the entire encrypted block and verifies its MIC. Derive keys
   using AppNonce, NetID and DevNonce in the correct order and byte order.
6. Uplink serialization duplicated MHDR, misplaced FCtrl, and called a nonexistent
   parser. Build one MAC payload and compute the MIC over the proper frame.

The first join after marker synchronization succeeded. This demonstrates actual
reception at the phone; collision is not needed to explain the prior failures.
Earlier varied mtypes without MIC validation were not proof of ambient traffic.

JoinAccept RX1 is scheduled five seconds after the JoinRequest, independently of
`rx1_delay=1` for data frames. Continuous RX avoids periodic re-arm gaps.

## Validation

Run with dependencies from `lora/requirements.txt` installed:

```sh
python3 -m unittest discover -s lora/tests
```

The tests cover JoinAccept authentication, session-key inputs, uplink frame layout
and MIC, RX dummy-byte removal, rejection of CRC-error packets, and marker-based bridge synchronization.

## Remaining scope

This is still a minimal fixed-channel node, not a complete LoRaWAN implementation.
It listens for joins on RX1 only (923.3 MHz / SF7BW500), does not implement RX2
fallback or ADR/MAC-command responses, and returns raw downlink bytes. The existing
1.8-second TX tail can miss normal one-second data receive windows. DevNonce is
clock-seeded rather than durably allocated. Session state and frame counters are
in memory; this verification does not establish long-term counter-wrap handling.

References: [Semtech SX126x driver](https://github.com/Lora-net/sx126x_driver/blob/master/src/sx126x.c),
[ATtiny bridge firmware](https://github.com/zschroeder6212/tiny-i2c-spi/blob/master/src/main.c),
[PineDio reference HAL](https://github.com/puurpl/pinephonepro-lora/blob/master/helium-lorawan/I2CBridgeHal.h),
[LoRaWAN regional parameters](https://lora-alliance.org/wp-content/uploads/2021/05/RP002-1.0.3-FINAL-1.pdf).
