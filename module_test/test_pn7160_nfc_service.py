"""Exercise the music-card event policy with simulated reader observations."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Main'))
from nfc_service import NFCService
from pn7160_reader import NFCError, Observation, ReaderOffline

UID = '04:7A:85:22:F0:1C:91'
CODE = 'oradio:v1:card:theme:jaren_zestig'
PRESENT = Observation('present', UID)
ABSENT = Observation('absent')
UNCERTAIN = Observation('uncertain')


def text_record(text):
    payload = b'\x02en' + text.encode()
    return bytes((0xd1, 1, len(payload), ord('T'))) + payload


class ServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cards = Path(directory.name) / 'music_cards.json'
        cards.write_text(json.dumps({'schema_version': 1, 'cards': {CODE: {'playlist': 'test'}}}))
        self.events = []
        self.service = NFCService(cards, self.events.append)
        self.reader = Mock()
        self.reader.read_ndef.return_value = text_record(CODE)

    def poll(self, sequence):
        self.reader.poll.side_effect = sequence + [ReaderOffline('test finished')]
        clock = iter(range(1000))
        with patch('nfc_service.time.monotonic', side_effect=lambda: next(clock) / 10), \
                patch.object(self.service.stop_event, 'wait'):
            with self.assertRaisesRegex(ReaderOffline, 'test finished'):
                self.service._poll(self.reader)
        return [e for e in self.events if e['event'] in ('CARD', 'REMOVED', 'INVALID')]

    def test_held_card_and_short_rf_loss_only_trigger_once(self):
        events = self.poll([PRESENT] * 5 + [ABSENT] * 2 + [UNCERTAIN] * 10 + [ABSENT] * 2 + [PRESENT])
        self.assertEqual([e['event'] for e in events], ['CARD'])
        self.reader.read_ndef.assert_called_once_with(UID)

    def test_continuous_absence_rearms_and_uses_cache(self):
        events = self.poll([PRESENT, UNCERTAIN] + [ABSENT] * 4 + [PRESENT])
        self.assertEqual([e['event'] for e in events], ['CARD', 'REMOVED', 'CARD'])
        self.assertGreaterEqual(events[1]['absent_seconds'], 0.6)
        self.assertEqual([events[0]['source'], events[2]['source']], ['tag', 'cache'])
        self.reader.read_ndef.assert_called_once_with(UID)
        self.assertEqual(json.loads(self.service.cache.path.read_text())['uids'], {UID: CODE})

    def test_initial_empty_period_releases_bridge_after_restart(self):
        events = self.poll([ABSENT] * 12 + [PRESENT])
        self.assertEqual([e['event'] for e in events], ['REMOVED', 'CARD'])

    def test_ordinary_text_and_multiple_records_do_not_trigger(self):
        self.reader.read_ndef.return_value = bytes.fromhex(
            '91 01 0D 54 02 65 6E 54 65 73 74 20 6E 66 63 20 31 '
            '51 01 0D 54 02 65 6E 53 70 65 65 6C 20 41 42 42 41'
        )
        self.assertEqual([e['event'] for e in self.poll([PRESENT] * 6)], ['INVALID'])
        self.assertFalse(self.service.cache.path.exists())

    def test_failed_read_retries_without_learning_partial_data(self):
        self.reader.read_ndef.side_effect = [NFCError('timeout'), text_record(CODE)]
        events = self.poll([PRESENT, UNCERTAIN, PRESENT, PRESENT])
        self.assertEqual([e['event'] for e in events], ['CARD'])
        self.assertEqual(events[0]['attempt'], 2)

    def test_different_uid_is_new_presentation(self):
        events = self.poll([PRESENT, Observation('present', '04:59:78:D2:BC:21:90')])
        self.assertEqual([e['event'] for e in events], ['CARD', 'CARD'])
        self.assertNotEqual(events[0]['uid'], events[1]['uid'])


if __name__ == '__main__':
    unittest.main()
