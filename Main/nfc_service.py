#!/usr/bin/env python3
"""Standalone PN7160 music-card service with JSON events and a persistent UID cache."""
import argparse
import json
import os
import tempfile
from pathlib import Path
import re
import signal
import threading
import time
import fcntl

from pn7160_reader import NFCError, PN7160Reader

CODE = re.compile(r'oradio:v1:card:(moment|theme|mood|artist|function):[a-z0-9]+(?:_[a-z0-9]+)*\Z')

INVALID_AFTER = 3
POLL_INTERVAL = 0.2


def decode_text(value):
    """Decode one short UTF-8 NDEF Text record containing a card code."""
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


DEFAULT_CARDS = Path('/media/oradio/Systeem/music_cards.json')
USB_MOUNT = Path('/media/oradio')
BUNDLED_CARDS = Path(__file__).resolve().parents[1] / 'install_resources' / 'music_cards.json'


def seed_default_cards():
    # Create the mapping only on a mounted stick.
    if DEFAULT_CARDS.exists():
        return
    contents = BUNDLED_CARDS.read_bytes()
    if not USB_MOUNT.is_mount():
        raise OSError(f'USB niet aangekoppeld: {USB_MOUNT}; kaartconfiguratie niet geschreven')
    DEFAULT_CARDS.parent.mkdir(parents=True, exist_ok=True)
    try:
        with DEFAULT_CARDS.open('xb') as destination:
            destination.write(contents)
    except FileExistsError:
        # A mapping was provided after the existence check.
        return


def cache_location(cards_path, cache_path=None):
    return (Path(cache_path).expanduser() if cache_path is not None else
            Path(cards_path).expanduser().with_name('nfc_uid_cache.json'))
REMOVAL_SECONDS = 0.6
LOCK_NAME = ".oradio_nfc_service.lock"
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
        with open(Path.home() / LOCK_NAME, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cache.load()
            with PN7160Reader(report=lambda message: self.emit(
                {'event': 'READER', 'message': message}
            )) as reader:
                self.emit({'event': 'READY', 'cards': len(self.cards),
                           'reader': 'PN7160', 'cache_file': str(self.cache.path),
                           'cached_uids': len(self.cache.entries),
                           'removal_seconds': REMOVAL_SECONDS})
                try:
                    self._poll(reader)
                finally:
                    self.emit({'event': 'SUMMARY', **self.stats})
            self.emit({'event': 'STOPPED'})

    def _emit_retry(self, uid, attempts, error):
        # Dempen: de eerste 5 pogingen, daarna elke 10e.
        if attempts <= 5 or attempts % 10 == 0:
            self.emit({'event': 'READ_RETRY', 'uid': uid, 'attempt': attempts,
                       'error': str(error)})

    def _poll(self, reader):
        previous = absent_since = None
        attempts = invalid_count = 0
        handled = False
        self.stats = dict(presentations=0, cards=0, first_try=0, recovered=0,
                          read_failures=0, uncertain_polls=0, invalid=0, unknown=0,
                          ended_without_card=0, pending_without_card=0, cache_hits=0,
                          learned=0, cache_write_failures=0)
        # Presentation counts are reader observations, not physical tap counts.
        card_delivered = False
        empty_reported = False
        while not self.stop_event.is_set():
            start = time.monotonic()
            observation = reader.poll()
            if observation.state == 'uncertain':
                absent_since = None
                self.stats['uncertain_polls'] += 1
            elif observation.state == 'absent':
                now = time.monotonic()
                if absent_since is None:
                    absent_since = now
                if not empty_reported and now - absent_since >= REMOVAL_SECONDS:
                    if previous is not None:
                        self.stats['ended_without_card'] += int(not card_delivered)
                    self.stats['pending_without_card'] = 0
                    previous = None
                    empty_reported = True
                    self.emit({'event': 'REMOVED', 'absent_seconds': round(now - absent_since, 2)})
            elif observation.state == 'present' and observation.uid is not None:
                uid = observation.uid
                empty_reported = False
                absent_since = None
                if uid != previous:
                    if previous is not None:
                        self.stats['ended_without_card'] += int(not card_delivered)
                    previous, attempts, handled = uid, 0, False
                    invalid_count = 0
                    card_delivered = False
                    self.stats['presentations'] += 1
                    self.stats['pending_without_card'] = 1
                if not handled:
                    attempts += 1
                    try:
                        cached_code = self.cache.entries.get(uid)
                        # A removed mapping must never trigger an old playlist.
                        from_cache = cached_code in self.cards
                        if not UID.fullmatch(uid):
                            raise ValueError('Verwacht UID met zeven bytes')
                        if from_cache:
                            code, size = cached_code, 0
                        else:
                            message = reader.read_ndef(uid)
                            code, size = decode_text(message), len(message)
                        handled = True
                        card = self.cards.get(code)
                        invalid_count = 0
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
                    except (NFCError, OSError) as error:
                        self.stats['read_failures'] += 1
                        invalid_count = 0
                        self._emit_retry(uid, attempts, error)
                    except ValueError as error:
                        invalid_count += 1
                        if invalid_count >= INVALID_AFTER:
                            self.stats['invalid'] += 1
                            handled = True
                            self.emit({'event': 'INVALID', 'uid': uid, 'attempt': attempts,
                                       'error': str(error)})
                        else:
                            self._emit_retry(uid, attempts, error)
            else:
                raise RuntimeError(f'Ongeldige readerwaarneming: {observation}')
            self.stop_event.wait(max(0, start + POLL_INTERVAL - time.monotonic()))
        self.stats['pending_without_card'] = int(previous is not None and not card_delivered)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cards', default=str(DEFAULT_CARDS))
    parser.add_argument('--cache', default=None, help='Cachebestand; standaard naast --cards op USB')
    parser.add_argument('--forget-uid', help='Verwijder één UID uit de cache en stop; geen NFC-verkeer')
    args = parser.parse_args()
    if args.forget_uid:
        with open(Path.home() / LOCK_NAME, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cache = UIDCache(cache_location(args.cards, args.cache))
            cache.load()
            removed = cache.forget(args.forget_uid)
            print(json.dumps({'event': 'CACHE_FORGOTTEN', 'uid': args.forget_uid.upper(),
                              'removed': removed}), flush=True)
        return
    if Path(args.cards).expanduser() == DEFAULT_CARDS:
        seed_default_cards()
    service = NFCService(args.cards, lambda event: print(json.dumps(event, ensure_ascii=False), flush=True), cache_path=args.cache)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: service.stop())
    service.run()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'STOP: {error}', flush=True)
        raise SystemExit(1)