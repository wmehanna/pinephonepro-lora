"""Protocol regressions; run with python -m unittest discover -s lora/tests."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lora_mac import LoRaWANNode
from pylorawan.encryption import aes128_decrypt, aes128_encrypt, generate_mic
from sx1262 import SX1262
from bridge import I2CBridge, SYNC_SEQUENCE


class OtaaTest(unittest.TestCase):
    def node(self):
        node = LoRaWANNode.__new__(LoRaWANNode)
        node.app_key = bytes(range(16))
        node.dev_eui = bytes.fromhex('d62e556244670000')
        node.app_eui = bytes(8)
        node.dev_nonce = 0x1233
        node.joined = False
        node.frame_counter = 0
        node.radio = Mock()
        node._retune_uplink = Mock()
        node._retune_rx1 = Mock()
        return node

    def test_join_and_uplink_wire_format(self):
        node = self.node()
        # Explicit little-endian AppNonce, NetID, DevAddr, DLSettings, RXDelay.
        body = bytes.fromhex('030201060504443322110801')
        mic = generate_mic(node.app_key, b'\x20' + body)
        packet = b'\x20' + aes128_decrypt(node.app_key, body + mic)
        node._wait_for_packet = Mock(return_value=packet)
        self.assertTrue(node.join())
        self.assertEqual(node.dev_addr, 0x11223344)
        expected_nwk = aes128_encrypt(node.app_key, bytes.fromhex('010302010605043412') + bytes(7))
        expected_app = aes128_encrypt(node.app_key, bytes.fromhex('020302010605043412') + bytes(7))
        self.assertEqual(node.nwk_skey, expected_nwk)
        self.assertEqual(node.app_skey, expected_app)
        node.send_uplink(b'ping')
        frame = node.radio.transmit.call_args.args[0]
        self.assertEqual(frame[:9], bytes.fromhex('404433221100010001'))
        block = bytes.fromhex('490000000000443322110100000000') + bytes([len(frame)-4])
        self.assertEqual(frame[-4:], generate_mic(expected_nwk, block + frame[:-4]))
        stream = aes128_encrypt(expected_app, bytes.fromhex('01000000000044332211010000000001'))
        self.assertEqual(bytes(a ^ b for a, b in zip(frame[9:-4], stream)), b'ping')

    def test_bad_join_mic_does_not_create_session(self):
        node = self.node()
        node._wait_for_packet = Mock(side_effect=[b'\x20' + bytes(16), None])
        self.assertFalse(node.join())
        self.assertFalse(node.joined)

    def test_bridge_sync_stops_at_marker_not_fixed_drain_count(self):
        bridge = I2CBridge.__new__(I2CBridge)
        bridge.fd = 42
        stale = bytes([0xa2]) * 37
        stream = iter(stale + SYNC_SEQUENCE + bytes([0x99]))
        bridge.read_one = Mock(side_effect=lambda: next(stream))
        with patch('bridge.os.write', side_effect=lambda fd, data: len(data)), patch('bridge.time.sleep'):
            self.assertTrue(bridge.sync_buffer(verbose=False))
        self.assertEqual(bridge.read_one.call_count, len(stale) + len(SYNC_SEQUENCE))
        self.assertEqual(next(stream), 0x99)
        bridge.read_one = Mock(return_value=0xa2)
        with patch('bridge.os.write', side_effect=lambda fd, data: len(data)), patch('bridge.time.sleep'):
            self.assertFalse(bridge.sync_buffer(verbose=False))

    def test_receive_discards_dummy_and_rejects_crc_error(self):
        radio = SX1262(Mock())
        radio._preamble_len, radio._header_type, radio._crc_on = 8, 0, 1
        radio.configure_packet = Mock()
        radio.get_irq = Mock(return_value=2)
        packet = b'\x20' + bytes(range(16))
        def command(opcode, payload=b''):
            if opcode == 0x14:
                return bytes([0, 0, 122, 44, 122])
            if opcode == 0x13:
                return bytes([0, 0, len(packet), 128])
            if opcode == 0x1e:
                self.assertEqual(payload, bytes([128, 0]) + bytes(len(packet)))
                return bytes([0xa2, 0xa2, 0xa2]) + packet
            return bytes(len(payload) + 1)
        radio._cmd = command
        self.assertEqual(radio.receive(1, downlink=True), packet)
        radio.configure_packet.assert_called_with(255, downlink=True)
        radio.get_irq.return_value = 0x42
        self.assertIsNone(radio.receive(1, downlink=True))


if __name__ == '__main__':
    unittest.main()
