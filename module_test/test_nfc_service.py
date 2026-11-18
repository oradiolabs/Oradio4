"""Hardware-free regression tests: python3 -m unittest discover -s module_test -p test_nfc_service.py"""
import ctypes as C
import sys
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Main'))
import nfc_service as nfc

TAG_UID = '04:7A:85:22:F0:1C:91'
CODE = 'oradio:v1:card:theme:jaren_zestig'

def memory():
    payload = b'\x02en' + CODE.encode()
    record = bytes([0xd1, 1, len(payload), ord('T')]) + payload
    return (bytes([3, len(record)]) + record + b'\xfe').ljust(144, b'\0')

class Stop:
    def __init__(self):
        self.stopped = False
    def is_set(self):
        return self.stopped
    def wait(self, seconds):
        return self.stopped

class ReaderTests(unittest.TestCase):
    def service(self):
        service = object.__new__(nfc.NFCService)
        service.stop_event = Stop()
        service.cards = {CODE: {'playlist': 'Pop60s'}}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service.cache = nfc.UIDCache(Path(directory.name) / 'cache.json')
        service.events = []
        service.emit = service.events.append
        return service

    def test_read_command_pages_and_payload(self):
        service = self.service()
        pages = []
        def checked(lib, name, device, tx, txsize, rx, rxsize, timeout):
            self.assertEqual((name, tx[0], txsize, rxsize),
                             ('nfc_initiator_transceive_bytes', 0x30, 2, 16))
            pages.append(tx[1])
            offset = (tx[1] - 4) * 4
            rx[:] = memory()[offset:offset + 16]
            return 16
        with patch.object(nfc.reader, 'checked', checked):
            self.assertEqual(service._read_code(None, None), (CODE, 48))
        self.assertEqual(pages, [4, 8, 12])

    def run_sequence(self, selections, reads):
        service = self.service()
        selections, reads = iter(selections), iter(reads)
        def select():
            try:
                item = next(selections)
            except StopIteration:
                service.stop_event.stopped = True
                return None
            if isinstance(item, Exception):
                raise item
            return item
        def read():
            item = next(reads)
            if isinstance(item, Exception):
                raise item
            return item
        clock = [0.0]
        def wait(seconds):
            clock[0] += 0.65
        service.stop_event.wait = wait
        with patch.object(nfc.time, 'monotonic', side_effect=lambda: clock[0]):
            service._poll(select, read)
        self.last_service = service
        return [e for e in service.events if e['event'] != 'CACHE_SAVED']

    def test_retry_unlimited_once_per_presentation(self):
        events = self.run_sequence([TAG_UID] * 8 + [None] * 3 + [TAG_UID],
                                  [nfc.reader.NFCError('read', -20)] * 5 + [(CODE,48)] * 2)
        cards = [e for e in events if e['event'] == 'CARD']
        self.assertEqual([e['attempt'] for e in cards], [6, 1])
        self.assertEqual(sum(e['event'] == 'REMOVED' for e in events), 1)

    def test_select_error_is_not_removal(self):
        events = self.run_sequence([TAG_UID, None, None, nfc.reader.NFCError('select', -20), None, TAG_UID], [(CODE,48)])
        self.assertEqual([e['event'] for e in events], ['CARD', 'SELECT_RETRY'])

    def test_transport_errors_stop(self):
        for code in (-1,):
            with self.subTest(code=code), self.assertRaises(nfc.reader.NFCError):
                self.run_sequence([TAG_UID], [nfc.reader.NFCError('read',code)])
            with self.subTest(code=code), self.assertRaises(nfc.reader.NFCError):
                self.run_sequence([nfc.reader.NFCError('select',code)], [])

    def test_partial_read_discarded_after_error(self):
        service = self.service()
        pages = []
        def checked(lib, name, device, tx, txsize, rx, rxsize, timeout):
            pages.append(tx[1])
            if len(pages) == 2:
                raise nfc.reader.NFCError('read', -20)
            offset = (tx[1]-4)*4
            rx[:] = memory()[offset:offset+16]
            return 16
        with patch.object(nfc.reader, 'checked', checked):
            with self.assertRaises(nfc.reader.NFCError):
                service._read_code(None,None)
            self.assertEqual(service._read_code(None,None), (CODE,48))
        self.assertEqual(pages,[4,8,4,8,12])

    def test_short_response_retried(self):
        service = self.service()
        with patch.object(nfc.reader,'checked',return_value=1):
            with self.assertRaises(nfc.IncompleteRead):
                service._read_code(None,None)
        events=self.run_sequence([TAG_UID,TAG_UID],[nfc.IncompleteRead('short',4),(CODE,48)])
        self.assertEqual([e['event'] for e in events],['READ_RETRY','CARD'])

    def test_invalid_has_separate_counter(self):
        events = self.run_sequence([TAG_UID] * 6,
            [nfc.reader.NFCError('read', -20)] * 2 + [ValueError('bad')] * 3)
        self.assertEqual([e['event'] for e in events], ['READ_RETRY'] * 4 + ['INVALID'])
        self.assertEqual(events[-1]['attempt'], 5)

    def test_read_timeout_and_chip_errors_are_bounded(self):
        for code in (-6, -90):
            with self.subTest(code=code), self.assertRaises(nfc.reader.NFCError):
                self.run_sequence([TAG_UID] * 4, [nfc.reader.NFCError('read',code)] * 4)

    def test_transient_timeout_can_recover(self):
        events = self.run_sequence([TAG_UID] * 3,
            [nfc.reader.NFCError('read', -6), (CODE,48)])
        self.assertEqual([e['event'] for e in events], ['READ_RETRY','CARD'])

    def test_unknown_only_once(self):
        events = self.run_sequence([TAG_UID] * 3, [('oradio:v1:card:theme:onbekend',48)])
        self.assertEqual([e['event'] for e in events], ['UNKNOWN'])

    def test_cache_survives_new_instance(self):
        service = self.service()
        service.cache.learn(TAG_UID, CODE)
        loaded = nfc.UIDCache(service.cache.path)
        loaded.load()
        self.assertEqual(loaded.entries, {TAG_UID: CODE})
        self.assertTrue(loaded.forget(TAG_UID))
        loaded = nfc.UIDCache(service.cache.path)
        loaded.load()
        self.assertEqual(loaded.entries, {})

    def test_cache_hit_does_not_read_memory(self):
        self.run_sequence([TAG_UID] + [None] * 3 + [TAG_UID], [(CODE, 48)])
        cards = [e for e in self.last_service.events if e['event'] == 'CARD']
        self.assertEqual([e['source'] for e in cards], ['tag', 'cache'])
        self.assertEqual(cards[-1]['bytes_read'], 0)
        self.assertEqual(self.last_service.stats['cache_hits'], 1)
        self.assertEqual(self.last_service.stats['learned'], 1)

    def test_brief_absence_does_not_repeat_card(self):
        events = self.run_sequence([TAG_UID, None, None, TAG_UID], [(CODE,48)])
        self.assertEqual([e['event'] for e in events], ['CARD'])

    def test_corrupt_cache_preserved(self):
        cache = self.service().cache
        cache.path.write_text('broken')
        with self.assertRaises(ValueError):
            cache.load()
        self.assertEqual(cache.path.read_text(), 'broken')

    def test_failed_atomic_replace_preserves_disk(self):
        cache = self.service().cache
        cache.learn(TAG_UID, CODE)
        before = cache.path.read_bytes()
        with patch.object(nfc.os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                cache.learn('04:00:00:00:00:00:01', CODE)
        self.assertEqual(cache.path.read_bytes(), before)
        self.assertEqual(list(cache.path.parent.glob('.nfc-cache-*')), [])

    def test_cache_write_failure_still_delivers_card(self):
        with patch.object(nfc.UIDCache, 'save', side_effect=OSError('disk error')):
            events = self.run_sequence([TAG_UID] + [None]*3 + [TAG_UID], [(CODE,48)])
        self.assertEqual([e['event'] for e in events],
                         ['CACHE_WRITE_ERROR','CARD','REMOVED','CARD'])
        self.assertEqual(self.last_service.stats['cache_write_failures'], 1)

    def test_restart_uses_cache_and_current_playlist(self):
        service = self.service()
        service.cache.learn(TAG_UID, CODE)
        service.cache = nfc.UIDCache(service.cache.path)
        service.cache.load()
        service.cards[CODE] = {'playlist': 'NieuwePlaylist'}
        selections = iter([TAG_UID, TAG_UID])
        def select():
            try:
                return next(selections)
            except StopIteration:
                service.stop_event.stopped = True
                return None
        with patch.object(nfc.time, 'monotonic', return_value=0):
            service._poll(select, lambda: self.fail('Cache hit read tag memory'))
        card = next(e for e in service.events if e['event'] == 'CARD')
        self.assertEqual(card['card']['playlist'], 'NieuwePlaylist')
        self.assertEqual(card['source'], 'cache')

    def test_removed_mapping_does_not_use_stale_cache(self):
        service = self.service()
        service.cache.learn(TAG_UID, CODE)
        service.cards = {}
        def read():
            service.stop_event.stopped = True
            return CODE, 48
        service._poll(lambda: TAG_UID, read)
        self.assertEqual(service.events[-1]['event'], 'UNKNOWN')
        self.assertEqual(service.events[-1]['source'], 'tag')

    def test_cache_default_follows_cards_file(self):
        self.assertEqual(nfc.cache_location('/media/oradio/Systeem/music_cards.json'),
                         Path('/media/oradio/Systeem/nfc_uid_cache.json'))
        self.assertEqual(nfc.cache_location('/tmp/demo/music_cards.json'),
                         Path('/tmp/demo/nfc_uid_cache.json'))
        self.assertEqual(nfc.cache_location('/tmp/demo/music_cards.json', '/tmp/explicit.json'),
                         Path('/tmp/explicit.json'))

    def test_service_uses_adjacent_cache(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        cards = Path(directory.name) / 'music_cards.json'
        cards.write_text(json.dumps({'schema_version': 1, 'cards': {CODE: {'playlist': 'Pop60s'}}}))
        service = nfc.NFCService(cards, lambda event: None)
        self.assertEqual(service.cache.path, cards.with_name('nfc_uid_cache.json'))

    def test_missing_usb_rejected_even_when_directory_exists(self):
        cache = nfc.UIDCache('/media/oradio/Systeem/nfc_uid_cache.json')
        with patch.object(Path, 'is_mount', return_value=False):
            with self.assertRaisesRegex(OSError, 'USB niet aangekoppeld'):
                cache.load()
            with self.assertRaisesRegex(OSError, 'USB niet aangekoppeld'):
                cache.save()

    def test_missing_parent_never_created(self):
        cache = self.service().cache
        cache.path = cache.path.parent / 'missing' / 'cache.json'
        with self.assertRaises(OSError):
            cache.learn(TAG_UID, CODE)
        self.assertFalse(cache.path.parent.exists())

    def test_storage_change_does_not_overwrite_other_cache(self):
        cache = self.service().cache
        cache.learn(TAG_UID, CODE)
        previous = cache.path.read_bytes()
        cache._storage_identity = (-1, -1)
        with self.assertRaisesRegex(OSError, 'gewisseld'):
            cache.save()
        self.assertEqual(cache.path.read_bytes(), previous)

    def test_ndef_boundaries(self):
        self.assertFalse(nfc.ndef_complete(memory()[:16]))
        self.assertEqual(nfc.decode_text(memory()),CODE)
        for malformed in (b'\x03\xfe', b'\x03\xff', b'\xfe'):
            with self.assertRaises(ValueError):
                nfc.ndef_complete(malformed)

if __name__ == '__main__':
    unittest.main()
