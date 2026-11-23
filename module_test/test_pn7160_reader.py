"""Hardware-free tests for the standalone reader."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Main'))
import pn7160_reader as nfc

UID = '04:59:78:D2:BC:21:90'
ACTIVATED = bytes.fromhex(
    '61 05 18 01 01 02 00 FF 01 0D 44 00 07 04 59 78 D2 BC 21 90 '
    '01 00 00 00 00 00 00'
)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader = nfc.PN7160Reader()
        self.transport = Mock(spec=nfc._NCITransport)
        self.transport.state = 'DISCOVERY'
        self.reader._transport = self.transport

    def activate(self):
        self.transport.read_packet.return_value = ACTIVATED
        self.assertEqual(self.reader.poll(), nfc.Observation('present', UID))

    def test_captured_read(self):
        packet = bytes.fromhex('00 00 11 01 03 A0 0C 34 03 2A D1 01 26 54 02 65 6E 6F 72 00')
        self.assertEqual(nfc.parse_ntag_read(packet), packet[3:-1])
        with self.assertRaises(nfc.NFCError):
            nfc.parse_ntag_read(packet[:-1] + b'\xb2')

    def test_empty_then_present(self):
        self.transport.read_packet.return_value = None
        self.assertEqual(self.reader.poll(), nfc.Observation('absent'))
        self.activate()
        self.assertEqual(self.reader.poll(), nfc.Observation('present', UID))
        self.transport.read_four_pages.assert_called_once_with(4, timeout=0.25)

    def test_ndef_spans_pages(self):
        self.activate()
        payload = b'\x02enoradio:v1:card:theme:jaren_zeventig'
        ndef = bytes((0xd1, 1, len(payload), ord('T'))) + payload
        data = (bytes((3, len(ndef))) + ndef + b'\xfe').ljust(48, b'\x00')
        self.transport.read_four_pages.side_effect = [
            bytes.fromhex('E1 10 12 00') + bytes(12),
            data[:16], data[16:32], data[32:],
        ]
        with self.assertRaises(nfc.NFCLinkLost):
            self.reader.read_ndef('04:00:00:00:00:00:00')
        self.transport.read_four_pages.assert_not_called()
        self.assertEqual(self.reader.read_ndef(UID), ndef)

    def test_read_timeout_is_uncertain_until_discovery(self):
        self.activate()
        self.transport.read_four_pages.side_effect = nfc.TagReadTimeout('Tag READ timeout')
        self.transport.discover.side_effect = lambda: setattr(self.transport, 'state', 'DISCOVERY')
        self.transport.read_packet.return_value = None
        self.reader._transport = self.transport
        with patch.object(nfc.time, 'monotonic', side_effect=[10.0, 10.1, 10.25]):
            self.assertEqual(self.reader.poll(), nfc.Observation('uncertain'))
            self.assertEqual(self.reader.poll(), nfc.Observation('uncertain'))
            self.transport.deact_to_idle.assert_not_called()
            self.assertEqual(self.reader.poll(), nfc.Observation('uncertain'))
        self.transport.discover.assert_called_once()
        self.transport.reset_init.assert_not_called()
        self.assertEqual(self.reader.poll(), nfc.Observation('absent'))
        self.activate()

    def test_eight_a1_notifications_schedule_reset(self):
        self.transport.read_packet.return_value = bytes.fromhex('60 07 01 A1')
        with patch.object(nfc.time, 'monotonic', return_value=10):
            for _ in range(7):
                self.assertEqual(self.reader.poll(), nfc.Observation('uncertain'))
            self.assertIsNone(self.reader._recovery)
            self.assertEqual(self.reader.poll(), nfc.Observation('uncertain'))
        self.transport.reset_init.assert_not_called()
        with patch.object(nfc.time, 'monotonic', return_value=12):
            self.assertEqual(self.reader.poll(), nfc.Observation('uncertain'))
        self.transport.reset_init.assert_called_once()

    def test_failed_recovery_is_bounded(self):
        self.activate()
        self.transport.read_four_pages.side_effect = nfc.NFCError('timeout')
        self.transport.deact_to_idle.side_effect = nfc.NFCError('RF recovery failed')
        self.transport.reset_init.side_effect = OSError('I2C unavailable')
        clock = Mock()
        clock.side_effect = [0, 1, 1, 3, 3, 7]
        with patch.object(nfc.time, 'monotonic', clock):
            for _ in range(3):
                self.assertEqual(self.reader.poll(), nfc.Observation('uncertain'))
            with self.assertRaises(nfc.ReaderOffline):
                self.reader.poll()
        self.assertEqual(self.transport.reset_init.call_count, 3)

    def test_initialization_failure_releases_transport(self):
        transport = nfc._NCITransport(8, 0x28, '/dev/gpiochip0', 17, 27, Mock())
        self.reader._transport = transport
        with patch.object(nfc._NCITransport, '__enter__'), \
                patch.object(nfc._NCITransport, '__exit__') as release, \
                patch.object(transport, 'reset_init', side_effect=nfc.NFCError('bad response')):
            with self.assertRaises(nfc.NFCError), self.reader:
                self.fail('Initialization failure must propagate')
        release.assert_called_once()


if __name__ == '__main__':
    unittest.main()
