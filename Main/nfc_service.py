#!/usr/bin/env python3
"""Standalone NTAG213 music-card reader using the proven libnfc UID reader.

Run from the repository: python3 -u Main/nfc_service.py
Transport errors stop this process. The backend watchdog can terminate it.
Run standalone until lifecycle integration with oradio_control is completed.

Version 10: UID cache beside music_cards.json (USB by default), removal debounce and session
statistics. Standalone process lifetime only.
"""
import argparse
import ctypes as C
import json
import os
import tempfile
from pathlib import Path
import re
import signal
import threading
import time
import fcntl

import pn532_uid_monitor as reader

CODE = re.compile(r'oradio:v1:card:(moment|theme|mood|artist|function):[a-z0-9]+(?:_[a-z0-9]+)*\Z')

# Recovery policy: RF/release retries; timeout/chip retries are bounded.
# These error codes alone do not establish an RF or I2C root cause.
RETRYABLE = (-6, -10, -20, -90)   # timeout, target released, RF-fout, chipfout
MAX_SELECT_ERRORS = 15            # count, not a wall-clock deadline
MAX_READ_ERRORS = 3               # consecutive timeout/chip errors during READ
INVALID_AFTER = 3                 # pogingen voor een ongeldige inhoud definitief INVALID is
POLL_INTERVAL = 0.2               # seconden tussen polls


def decode_text(memory):
    """Read the NDEF TLV and one unchunked short UTF-8 Text record."""
    offset = 0
    while offset < len(memory):
        kind = memory[offset]
        offset += 1
        if kind == 0:
            continue
        if kind == 0xfe:
            break
        if offset >= len(memory):
            raise ValueError('Afgebroken TLV')
        length = memory[offset]
        offset += 1
        if length == 255:
            raise ValueError('Uitgebreide TLV-lengte niet ondersteund voor deze demo')
        end = offset + length
        if end > len(memory):
            raise ValueError('TLV buiten taggeheugen')
        value = memory[offset:end]
        offset = end
        if kind != 3:
            continue
        if len(value) < 5 or value[0] != 0xd1 or value[1] != 1 or value[3] != ord('T'):
            raise ValueError('Verwacht één kort NDEF-tekstrecord')
        if len(value) != 4 + value[2]:
            raise ValueError('Ongeldige NDEF-lengte')
        status = value[4]
        if status & 0xc0:
            raise ValueError('Verwacht UTF-8 tekst')
        start = 5 + (status & 0x3f)
        if start > len(value):
            raise ValueError('Ongeldige taalcode')
        text = value[start:].decode('utf-8')
        if not CODE.fullmatch(text):
            raise ValueError('Onbekende kaartcodegrammatica')
        return text
    raise ValueError('Geen NDEF-tekst gevonden')


def ndef_complete(memory):
    """Whether a complete NDEF TLV has arrived in the blocks read so far."""
    offset = 0
    while offset < len(memory):
        kind = memory[offset]
        offset += 1
        if kind == 0:
            continue
        if kind == 0xfe:
            raise ValueError('Geen NDEF-tekst gevonden')
        if offset >= len(memory):
            return False
        length = memory[offset]
        offset += 1
        if length == 255:
            raise ValueError('Uitgebreide TLV-lengte niet ondersteund')
        end = offset + length
        if end > 144:
            raise ValueError('TLV buiten NTAG213-geheugen')
        if end > len(memory):
            return False
        if kind == 3:
            return True
        offset = end
    return False


DEFAULT_CARDS = Path('/media/oradio/Systeem/music_cards.json')
USB_MOUNT = Path('/media/oradio')


def cache_location(cards_path, cache_path=None):
    return (Path(cache_path).expanduser() if cache_path is not None else
            Path(cards_path).expanduser().with_name('nfc_uid_cache.json'))
REMOVAL_SECONDS = 1.2
UID = re.compile(r'(?:[0-9A-F]{2}:){6}[0-9A-F]{2}\Z')


class UIDCache:
    """Only UID -> validated code; playlist always comes from music_cards.json.

    Caller holds the process lock during load/change/save. A malformed file is
    never silently overwritten. Atomic replacement preserves the previous file
    when writing fails before replacement.
    """
    def __init__(self, path):
        self.path = Path(path).expanduser().absolute()
        self.entries = {}
        self._storage_identity = None

    def _check_storage(self):
        # Never create a replacement directory on the SD card when the USB
        # mount has disappeared. Explicit local --cache paths still work.
        if self.path.is_relative_to(USB_MOUNT) and not USB_MOUNT.is_mount():
            raise OSError(f'USB niet aangekoppeld: {USB_MOUNT}; cache niet geschreven')
        if not self.path.parent.is_dir():
            raise OSError(f'Cachemap ontbreekt: {self.path.parent}')
        stat = self.path.parent.stat()
        identity = (stat.st_dev, stat.st_ino)
        if self._storage_identity is not None and identity != self._storage_identity:
            raise OSError('Cacheopslag gewisseld; herstart de NFC-lezer voor deze USB-stick')
        self._storage_identity = identity

    def load(self):
        self._check_storage()
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        if (not isinstance(data, dict) or data.get('schema_version') != 1
                or not isinstance(data.get('uids'), dict)):
            raise ValueError(f'Ongeldige UID-cache: {self.path}; bestand behouden')
        for uid, code in data['uids'].items():
            if not UID.fullmatch(uid) or not isinstance(code, str) or not CODE.fullmatch(code):
                raise ValueError(f'Ongeldige UID-cache: {self.path}; bestand behouden')
        self.entries = data['uids']

    def save(self):
        self._check_storage()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    dir=self.path.parent, prefix='.nfc-cache-', delete=False) as out:
                temporary = out.name
                json.dump({'schema_version': 1, 'uids': self.entries}, out,
                          ensure_ascii=False, indent=2, sort_keys=True)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
            self._check_storage()
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def learn(self, uid, code):
        if not UID.fullmatch(uid) or not CODE.fullmatch(code):
            raise ValueError('Ongeldige UID/code voor cache')
        self.entries[uid] = code
        self.save()

    def forget(self, uid):
        uid = uid.upper()
        if not UID.fullmatch(uid):
            raise ValueError('Gebruik een UID met zeven bytes, bijvoorbeeld 04:7A:85:22:F0:1C:91')
        if uid in self.entries:
            del self.entries[uid]
            self.save()
            return True
        return False


class IncompleteRead(RuntimeError):
    def __init__(self, message, page):
        self.page = page
        super().__init__(message)


class NFCService:
    """Blocking reader loop. Callback receives dictionaries with event details."""
    def __init__(self, cards_path, emit, cache_path=None):
        with open(cards_path, encoding='utf-8') as source:
            config = json.load(source)
        if config.get('schema_version') != 1 or not isinstance(config.get('cards'), dict):
            raise ValueError('Ongeldige music_cards.json')
        self.cards = config['cards']
        for code, card in self.cards.items():
            if not CODE.fullmatch(code) or not isinstance(card, dict):
                raise ValueError('Ongeldige kaart in configuratie')
            if not isinstance(card.get('playlist'), str) or not card['playlist'].strip():
                raise ValueError(f'Playlist ontbreekt voor {code}')
        self.cache = UIDCache(cache_location(cards_path, cache_path))
        self.emit = emit
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        with open(Path.home() / '.pn532_dual_test.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cache.load()
            self._read_loop()

    def _read_loop(self):
        lib = reader.load_libnfc()
        if lib.nfc_version() != b'1.8.0':
            raise RuntimeError('Deze reader vereist libnfc 1.8.0')
        lib.nfc_initiator_transceive_bytes.restype = C.c_int
        lib.nfc_initiator_transceive_bytes.argtypes = [C.c_void_p, C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t, C.c_int]
        context = C.c_void_p()
        reader.guarded(lib.nfc_init, C.byref(context))
        if not context.value:
            raise RuntimeError('nfc_init mislukt')
        device = reader.guarded(lib.nfc_open, context, b'pn532_i2c:/dev/i2c-1', seconds=12)
        if not device:
            raise RuntimeError('PN532 openen mislukt')
        reader.checked(lib, 'nfc_device_set_property_int', device, 0, 1000)
        reader.checked(lib, 'nfc_initiator_init', device)
        reader.checked(lib, 'nfc_device_set_property_bool', device, 7, False)
        target = (C.c_uint64 * 128)()
        self.emit({'event': 'READY', 'cards': len(self.cards), 'reader_version': 10, 'read_route': 'InDataExchange',
                   'retry_until_read': True, 'cache_file': str(self.cache.path),
                   'cached_uids': len(self.cache.entries), 'removal_seconds': REMOVAL_SECONDS})
        try:
            self._poll(lambda: reader.read_uid(lib, device, target),
                       lambda: self._read_code(lib, device))
        finally:
            if hasattr(self, 'stats'):
                self.emit({'event': 'SUMMARY', **self.stats})
        reader.checked(lib, 'nfc_device_set_property_bool', device, 5, False)
        self.emit({'event': 'STOPPED', 'note': 'RF uit; procesexit ruimt resources op'})

    def _transceive(self, lib, device, command, expected, page):
        """One raw tag command; the response must be exactly `expected` bytes."""
        tx = (C.c_ubyte * len(command))(*command)
        rx = (C.c_ubyte * expected)()
        try:
            size = reader.checked(lib, 'nfc_initiator_transceive_bytes',
                                  device, tx, len(command), rx, expected, 1000)
        except reader.NFCError as error:
            error.page = page
            raise
        if size != expected:
            # A NAK/short response is not usable tag memory.
            raise IncompleteRead(f'READ pagina {page}: {size} bytes, verwacht {expected}', page)
        return bytes(rx)

    def _read_code(self, lib, device):
        # Each attempt owns a fresh buffer. Never combine partial reads across
        # selection attempts (another card may have entered the field).
        # READ (0x30): 4 pagina's (16 bytes) per transactie.
        steps = [((0x30, page), 16, page) for page in range(4, 40, 4)]
        memory = bytearray()
        for command, expected, page in steps:
            if self.stop_event.is_set():
                return None
            memory.extend(self._transceive(lib, device, command, expected, page))
            if ndef_complete(memory):
                return decode_text(memory), len(memory)
        raise ValueError('Geen volledig NDEF-record binnen NTAG213-geheugen')

    def _emit_retry(self, uid, attempts, page, error):
        # Dempen: de eerste 5 pogingen, daarna elke 10e.
        if attempts <= 5 or attempts % 10 == 0:
            self.emit({'event': 'READ_RETRY', 'uid': uid, 'attempt': attempts,
                       'page': page, 'error': str(error)})

    def _poll(self, select, read_code):
        previous, absent_since, attempts, handled, errors = None, None, 0, False, 0
        invalid_count = read_errors = 0
        self.stats = dict(presentations=0, cards=0, first_try=0, recovered=0,
                          read_failures=0, select_failures=0, invalid=0, unknown=0,
                          ended_without_card=0, pending_without_card=0, cache_hits=0,
                          learned=0, cache_write_failures=0)
        # Presentation counts are reader observations, not physical tap counts.
        card_delivered = False
        while not self.stop_event.is_set():
            start = time.monotonic()
            try:
                uid = select()
            except reader.NFCError as error:
                errors += 1
                self.stats['select_failures'] += 1
                # Een RF-fout is geen bewijs van verwijdering. Alleen bekende
                # tijdelijke fouten worden opnieuw geprobeerd, en niet eindeloos.
                if error.code not in RETRYABLE or errors > MAX_SELECT_ERRORS:
                    raise
                absent_since = None
                self.emit({'event': 'SELECT_RETRY', 'error': str(error), 'consecutive': errors})
            else:
                errors = 0
                if uid is None:
                    # Only uninterrupted successful no-target polls count as
                    # absence; an RF error is not proof of physical removal.
                    now = time.monotonic()
                    if absent_since is None:
                        absent_since = now
                    if now - absent_since >= REMOVAL_SECONDS and previous is not None:
                        self.stats['ended_without_card'] += int(not card_delivered)
                        self.stats['pending_without_card'] = 0
                        previous = None
                        self.emit({'event': 'REMOVED', 'absent_seconds': round(now - absent_since, 2)})
                else:
                    absent_since = None
                    if uid != previous:
                        if previous is not None:
                            self.stats['ended_without_card'] += int(not card_delivered)
                        previous, attempts, handled = uid, 0, False
                        invalid_count = read_errors = 0
                        card_delivered = False
                        self.stats['presentations'] += 1
                        self.stats['pending_without_card'] = 1
                    if not handled:
                        attempts += 1
                        try:
                            cached_code = self.cache.entries.get(uid)
                            # A removed mapping must never trigger an old playlist.
                            from_cache = cached_code in self.cards
                            result = (cached_code, 0) if from_cache else read_code()
                            if result is None:  # orderly stop between blocks
                                break
                            code, size = result
                            handled = True
                            card = self.cards.get(code)
                            invalid_count = read_errors = 0
                            card_delivered = bool(card)
                            self.stats['pending_without_card'] = int(not card_delivered)
                            if card:
                                if from_cache:
                                    self.stats['cache_hits'] += 1
                                else:
                                    try:
                                        self.cache.learn(uid, code)
                                        self.stats['learned'] += 1
                                        self.emit({'event': 'CACHE_SAVED', 'uid': uid, 'code': code})
                                    except OSError as error:
                                        # This session can still use the validated
                                        # code, but do not claim it survived reboot.
                                        self.stats['cache_write_failures'] += 1
                                        self.emit({'event': 'CACHE_WRITE_ERROR', 'uid': uid,
                                                   'error': str(error), 'note': 'Alleen in geheugen bewaard'})
                                self.stats['cards'] += 1
                                self.stats['first_try' if attempts == 1 else 'recovered'] += 1
                            else:
                                self.stats['unknown'] += 1
                            self.emit({'event': 'CARD' if card else 'UNKNOWN',
                                       'uid': uid, 'code': code, 'card': card,
                                       'attempt': attempts, 'bytes_read': size,
                                       'source': 'cache' if from_cache else 'tag'})
                        except (reader.NFCError, IncompleteRead) as error:
                            if isinstance(error, reader.NFCError) and error.code not in RETRYABLE:
                                raise
                            self.stats['read_failures'] += 1
                            invalid_count = 0
                            if isinstance(error, reader.NFCError) and error.code in (-6, -90):
                                read_errors += 1
                                if read_errors >= MAX_READ_ERRORS:
                                    raise
                            else:
                                read_errors = 0
                            self._emit_retry(uid, attempts, getattr(error, 'page', None), error)
                        except (ValueError, UnicodeError) as error:
                            # Pas definitief ongeldig na meerdere pogingen: een enkele
                            # verstoorde read mag een geldige kaart niet blokkeren.
                            read_errors = 0
                            invalid_count += 1
                            if invalid_count >= INVALID_AFTER:
                                self.stats['invalid'] += 1
                                handled = True
                                self.emit({'event': 'INVALID', 'uid': uid, 'attempt': attempts,
                                           'error': str(error)})
                            else:
                                self._emit_retry(uid, attempts, None, error)
            # Re-select before every retry; no raw READ in an uncertain tag
            # state, no bus reset and no RF-field toggling on read failure.
            self.stop_event.wait(max(0, start + POLL_INTERVAL - time.monotonic()))
        self.stats['pending_without_card'] = int(previous is not None and not card_delivered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cards', default=str(DEFAULT_CARDS))
    parser.add_argument('--cache', default=None, help='Cachebestand; standaard naast --cards op USB')
    parser.add_argument('--forget-uid', help='Verwijder één UID uit de cache en stop; geen NFC-verkeer')
    args = parser.parse_args()
    if args.forget_uid:
        with open(Path.home() / '.pn532_dual_test.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cache = UIDCache(cache_location(args.cards, args.cache))
            cache.load()
            removed = cache.forget(args.forget_uid)
            print(json.dumps({'event': 'CACHE_FORGOTTEN', 'uid': args.forget_uid.upper(),
                              'removed': removed}), flush=True)
        return
    service = NFCService(args.cards, lambda event: print(json.dumps(event, ensure_ascii=False), flush=True), cache_path=args.cache)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: service.stop())
    service.run()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        reader.emergency(str(error))