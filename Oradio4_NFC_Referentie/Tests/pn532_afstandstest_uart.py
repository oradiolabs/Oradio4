#!/usr/bin/env python3
"""Live UID-monitor voor de Elechouse PN532 op de Oradio."""
import ctypes as C
import ctypes.util
import os
import re
import sys
import time

INTERVAL = 0.2
NP_INFINITE_SELECT = 7


class Modulation(C.Structure):
    _fields_ = [("type", C.c_int), ("baud", C.c_int)]


ISO14443A_106 = Modulation(1, 1)


def load_libnfc():
    lib = C.CDLL(ctypes.util.find_library("nfc") or "libnfc.so.6")
    ptr = C.c_void_p
    signatures = {
        "nfc_init": (None, [C.POINTER(ptr)]),
        "nfc_exit": (None, [ptr]),
        "nfc_open": (ptr, [ptr, C.c_char_p]),
        "nfc_close": (None, [ptr]),
        "nfc_initiator_init": (C.c_int, [ptr]),
        "nfc_device_set_property_bool": (C.c_int, [ptr, C.c_int, C.c_bool]),
        "nfc_initiator_select_passive_target":
            (C.c_int, [ptr, Modulation, ptr, C.c_size_t, ptr]),
        "str_nfc_target": (C.c_int, [C.POINTER(ptr), ptr, C.c_bool]),
        "nfc_free": (None, [ptr]),
        "nfc_strerror": (C.c_char_p, [ptr]),
    }
    for name, (result, args) in signatures.items():
        func = getattr(lib, name)
        func.restype = result
        func.argtypes = args
    return lib


def read_uid(lib, device, target):
    count = lib.nfc_initiator_select_passive_target(
        device, ISO14443A_106, None, 0, target)
    if count < 0:
        raise RuntimeError(lib.nfc_strerror(device).decode())
    if count == 0:
        return "Geen kaart"
    description = C.c_void_p()
    try:
        lib.str_nfc_target(C.byref(description), target, False)
        info = C.string_at(description) if description.value else b""
        uid = re.search(rb"UID \(NFCID1\):[ \t]*([0-9a-fA-F ]+)", info)
        if not uid:
            raise RuntimeError("Kaart gedetecteerd, UID onleesbaar")
        return bytes.fromhex(uid[1].decode()).hex(":").upper()
    finally:
        lib.nfc_free(description)


def main():
    lib = load_libnfc()
    context = C.c_void_p()
    lib.nfc_init(C.byref(context))
    if not context.value:
        raise RuntimeError("Initialisatie van libnfc mislukt")
    device = None
    try:
        bus = os.getenv("LIBNFC_DEVICE", "pn532_uart:/dev/serial0")
        device = lib.nfc_open(context, bus.encode())
        if not device:
            raise RuntimeError(f"Lezer openen mislukt ({bus})")
        if (lib.nfc_initiator_init(device) < 0 or
                lib.nfc_device_set_property_bool(device, NP_INFINITE_SELECT, False) < 0):
            raise RuntimeError(lib.nfc_strerror(device).decode())
        # Opaque storage for one libnfc 1.8 nfc_target, formatted by libnfc.
        target = (C.c_uint64 * 128)()
        print("Live NFC-test | Ctrl+C = stop", flush=True)
        while True:
            start = time.monotonic()
            status = read_uid(lib, device, target)
            if sys.stdout.isatty():
                print(f"\r{status:<32}", end="", flush=True)
            else:
                print(status, flush=True)
            time.sleep(max(0, INTERVAL - (time.monotonic() - start)))
    finally:
        if device:
            lib.nfc_close(device)
        lib.nfc_exit(context)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
    except (OSError, RuntimeError) as error:
        sys.exit(f"\nLezerfout: {error}")
