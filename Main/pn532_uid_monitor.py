#!/usr/bin/env python3
"""Standalone ISO14443-A UID monitor, libnfc 1.8.0 / PN532 I2C.
One printed result per poll; desired start interval 0.200 s.
No emulation, writes to tag memory, nfc_idle, nfc_close or nfc_exit.
Normal stop: RF off, then process exit. Fault: no further hardware commands.
"""
import argparse
import ctypes as C
import ctypes.util
from datetime import datetime
import fcntl
import os
from pathlib import Path
import re
import signal
import threading
import time

INTERVAL = 0.200
ERRORS = {-1: 'NFC_EIO', -2: 'NFC_EINVARG', -3: 'NFC_EDEVNOTSUPP',
          -4: 'NFC_ENOTSUCHDEV', -5: 'NFC_EOVFLOW', -6: 'NFC_ETIMEOUT',
          -7: 'NFC_EOPABORTED', -8: 'NFC_ENOTIMPL', -10: 'NFC_ETGRELEASED',
          -20: 'NFC_ERFTRANS', -80: 'NFC_ESOFT', -90: 'NFC_ECHIP'}


class Modulation(C.Structure):
    _pack_ = 1
    _fields_ = [('type', C.c_int), ('baud', C.c_int)]


def emergency(message, code=1):
    os.write(2, (f'STOP: {message}\nGeen nieuwe NFC-commando’s; chiptoestand onbekend.\n').encode())
    os._exit(code)


def guarded(function, *args, seconds=5):
    """Timer only terminates the process: never calls libnfc concurrently."""
    watchdog = threading.Timer(seconds, emergency,
                               (f'watchdog bij {function.__name__}', 124))
    watchdog.daemon = True
    watchdog.start()
    try:
        return function(*args)
    finally:
        watchdog.cancel()


def load_libnfc():
    lib = C.CDLL(ctypes.util.find_library('nfc') or 'libnfc.so.6')
    ptr = C.c_void_p
    signatures = {
        'nfc_version': (C.c_char_p, []),
        'nfc_init': (None, [C.POINTER(ptr)]),
        'nfc_open': (ptr, [ptr, C.c_char_p]),
        'nfc_initiator_init': (C.c_int, [ptr]),
        'nfc_device_set_property_bool': (C.c_int, [ptr, C.c_int, C.c_bool]),
        'nfc_device_set_property_int': (C.c_int, [ptr, C.c_int, C.c_int]),
        'nfc_initiator_select_passive_target':
            (C.c_int, [ptr, Modulation, ptr, C.c_size_t, ptr]),
        'str_nfc_target': (C.c_int, [C.POINTER(ptr), ptr, C.c_bool]),
        'nfc_free': (None, [ptr]),
    }
    for name, (result, args) in signatures.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = result, args
    return lib


class NFCError(RuntimeError):
    """Preserve the libnfc return code for operation-specific recovery."""
    def __init__(self, operation, code):
        self.code = code
        super().__init__(f'{operation}: {ERRORS.get(code, "onbekende fout")} ({code})')


def checked(lib, name, *args):
    result = guarded(getattr(lib, name), *args)
    if result < 0:
        # Decode the actual return code, not a potentially stale last_error.
        raise NFCError(name, result)
    return result


def read_uid(lib, device, target):
    count = checked(lib, 'nfc_initiator_select_passive_target',
                    device, Modulation(1, 1), None, 0, target)
    if count == 0:
        return None
    if count != 1:
        raise RuntimeError(f'Onverwacht aantal targets: {count}')
    description = C.c_void_p()
    try:
        length = lib.str_nfc_target(C.byref(description), target, False)
        if length < 0 or not description.value:
            raise RuntimeError('Kan targetbeschrijving niet uitlezen')
        info = C.string_at(description)
        match = re.search(rb'UID \(NFCID1\):[ \t]*([0-9a-fA-F ]+)', info)
        if not match:
            raise RuntimeError('Tag gevonden, maar UID ontbreekt')
        uid = bytes.fromhex(match[1].decode())
        if not 1 <= len(uid) <= 10:
            raise RuntimeError('Ongeldige UID-lengte')
        return uid.hex(':').upper()
    finally:
        if description.value:
            lib.nfc_free(description)


def poll_loop(read, emit, stop, seconds=0, clock=time.monotonic):
    """No extra 200-ms sleep after a slow read; no catch-up bursts."""
    origin = clock()
    previous = None
    stats = dict(polls=0, tags=0, empty=0, slow=0, max_read_ms=0.0)
    while not stop.is_set():
        start = clock()
        if seconds and start-origin >= seconds:
            break
        uid = read()  # errors propagate; never print a read error as 'Geen tag'
        duration = clock()-start
        gap = None if previous is None else start-previous
        previous = start
        stats['polls'] += 1
        stats['tags' if uid else 'empty'] += 1
        stats['slow'] += int(duration > INTERVAL)
        stats['max_read_ms'] = max(stats['max_read_ms'], duration*1000)
        emit(stats['polls'], uid, duration, gap)
        delay = max(0, start+INTERVAL-clock())
        if seconds:
            delay = min(delay, max(0, origin+seconds-clock()))
        stop.wait(delay)
    stats['elapsed_s'] = clock()-origin
    return stats


def display(number, uid, duration, gap):
    stamp = datetime.now().astimezone().isoformat(timespec='milliseconds')
    gap_text = '---' if gap is None else f'{gap*1000:.0f}'
    note = ' | METING LANGER DAN 200 ms' if duration > INTERVAL else ''
    print(f'{stamp} #{number:06d} | {uid or "Geen tag":<29} | '
          f'meting={duration*1000:.0f} ms | startafstand={gap_text} ms{note}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=0,
                        help='Stop na dit aantal seconden; 0 = tot Ctrl+C (standaard)')
    args = parser.parse_args()
    if args.seconds < 0:
        parser.error('--seconds moet 0 of positief zijn')
    # Same advisory lock as the earlier dual test. Does not lock out Oradio.
    lock = open(Path.home()/'.pn532_dual_test.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = threading.Event()

    def on_signal(signum, frame):
        if stop.is_set():
            emergency('tweede stopverzoek', 128+signum)
        stop.set()  # no bus operations in a signal handler

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    lib = load_libnfc()
    version = lib.nfc_version().decode()
    if version != '1.8.0':
        raise RuntimeError(f'Verwacht libnfc 1.8.0, geladen: {version}')
    print(f'START | libnfc {version} | alleen UID | intervaldoel 200 ms', flush=True)
    context = C.c_void_p()
    guarded(lib.nfc_init, C.byref(context))
    if not context.value:
        raise RuntimeError('nfc_init mislukt')
    device = guarded(lib.nfc_open, context, b'pn532_i2c:/dev/i2c-1', seconds=12)
    if not device:
        raise RuntimeError('nfc_open mislukt; driver kan intern cleanup hebben uitgevoerd')
    checked(lib, 'nfc_device_set_property_int', device, 0, 1000)
    if not stop.is_set():
        checked(lib, 'nfc_initiator_init', device)
        checked(lib, 'nfc_device_set_property_bool', device, 7, False)
    # Same aligned opaque storage as the original afstandstest. 1024 bytes
    # exceed nfc_target in libnfc 1.8.0 on both supported 32/64-bit Pi ABIs.
    # libnfc formats the result: no guessed field/union offsets in Python.
    target = (C.c_uint64 * 128)()
    print('Iedere poll wordt getoond. Ctrl+C = stoppen en RF uitzetten.', flush=True)
    stats = poll_loop(lambda: read_uid(lib, device, target), display, stop, args.seconds)
    # Only reached after successful calls / orderly stop. Never on an I/O error.
    checked(lib, 'nfc_device_set_property_bool', device, 5, False)
    print(f'KLAAR | polls={stats["polls"]} | met_tag={stats["tags"]} | '
          f'zonder_tag={stats["empty"]} | meting_boven_200ms={stats["slow"]} | '
          f'max_meting={stats["max_read_ms"]:.0f} ms | duur={stats["elapsed_s"]:.2f} s | RF uit', flush=True)
    # Deliberate standalone-process policy: avoid known nfc_close PowerDown path.
    # OS reclaims descriptors/memory. Do not import main into a long-running service.
    os._exit(0)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emergency(str(exc))
