#!/usr/bin/env python3
"""Oradio4 – PN7160 RF/NDEF/CPU reliability test v0.4 (read-only).

Target: Raspberry Pi 3A+, Debian 13, Python 3.13, libgpiod 2.x, smbus2.
Wiring: PN7160 via software I2C bus 8 (SDA GPIO12 / SCL GPIO13),
        VEN GPIO17, IRQ GPIO27, I2C address 0x28.

This is a diagnostic reader for NFC Forum Type 2 / NFC-A NTAG tags, not a
complete NCI stack. It READs four pages starting at page 4 repeatedly.
A failed READ is recorded immediately; a 'not detected' verdict after the
configurable grace period is *RF absence*, not proof of physical removal.

No changes to Oradio services, system configuration, NFC tags or firmware.
"""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import select
import sys
import time
from typing import Callable, Optional

CMD_RESET = bytes.fromhex("20 00 01 00")
CMD_INIT = bytes.fromhex("20 01 02 00 00")
CMD_PROP_ACT = bytes.fromhex("2F 02 00")
CMD_DISCOVER_A = bytes.fromhex("21 03 03 01 00 01")
CMD_DEACT_IDLE = bytes.fromhex("21 06 01 00")


class NFCError(Exception):
    """Recoverable PN7160, NCI or tag-read problem."""


class NFCLinkLost(NFCError):
    """NCI reports a tag/interface deactivation while reading."""


@dataclass(frozen=True)
class TagInfo:
    uid: str
    credits: int
    protocol: int
    interface: int


def hexstr(data: bytes) -> str:
    return data.hex(" ").upper()


def parse_activation(packet: bytes) -> TagInfo:
    """NCI RF_INTF_ACTIVATED_NTF, NFC-A passive / Type 2 / Frame RF interface.

    Uses the format observed in Oradio4 testing:
      61 05 18 01 01 02 00 FF 01 0D 44 00 07 <7-byte UID> ...
    """
    if len(packet) < 13 or packet[:2] != b"\x61\x05":
        raise NFCError(f"Not an activation notification: {hexstr(packet)}")
    if len(packet) != 3 + packet[2]:
        raise NFCError("RF_INTF_ACTIVATED_NTF length mismatch")
    body = packet[3:]
    if len(body) < 10:
        raise NFCError("Activation body too short")
    interface, protocol, technology = body[1], body[2], body[3]
    if (interface, protocol, technology) != (1, 2, 0):
        raise NFCError(
            f"Unsupported card: interface={interface}, protocol={protocol}, "
            f"technology={technology}; only NFC-A Type 2 Frame supported"
        )
    credits = body[5]
    tech_len = body[6]
    tech_params = body[7:7 + tech_len]
    if len(tech_params) != tech_len or len(tech_params) < 4:
        raise NFCError("Incomplete NFC-A activation parameters")
    uid_len = tech_params[2]
    uid_bytes = tech_params[3:3 + uid_len]
    if uid_len not in (4, 7, 10) or len(uid_bytes) != uid_len:
        raise NFCError(f"Invalid NFC-A UID length {uid_len}")
    return TagInfo(uid=uid_bytes.hex(" ").upper(), credits=credits,
                   protocol=protocol, interface=interface)


def parse_ntag_read(packet: bytes) -> bytes:
    """Return 16 NTAG memory bytes, checking NCI RF status byte."""
    if len(packet) < 4 or packet[:2] != b"\x00\x00" or len(packet) != packet[2] + 3:
        raise NFCError(f"Not a valid NCI data frame: {hexstr(packet)}")
    payload = packet[3:]
    if len(payload) != 17:
        raise NFCError(f"NTAG READ returned {len(payload)} bytes; expected 17")
    status = payload[-1]
    if status != 0:
        labels = {0xB0: "RF_TRANSMISSION_ERROR", 0xB1: "RF_PROTOCOL_ERROR",
                  0xB2: "RF_TIMEOUT_ERROR"}
        raise NFCError(f"Tag READ status 0x{status:02X} "
                       f"({labels.get(status, 'UNKNOWN')})")
    return payload[:-1]


@dataclass(frozen=True)
class CardDetails:
    uid: str
    cc_hex: str
    cc_capacity_bytes: Optional[int]
    read_only: Optional[bool]
    ndef_length: Optional[int]
    ndef_records: tuple[str, ...]
    ndef_raw: str
    note: str = ""


def decode_ndef_records(ndef: bytes) -> tuple[str, ...]:
    """Decode well-known NDEF Text/URI records; show unknown records as hex.

    Avoid assuming every card stores an Oradio Text record. Only decode
    records completely contained in the NDEF message.
    """
    uri_prefixes = (
        "", "http://www.", "https://www.", "http://", "https://",
        "tel:", "mailto:", "ftp://anonymous:anonymous@", "ftp://ftp.",
        "ftps://", "sftp://", "smb://", "nfs://", "ftp://", "dav://",
        "news:", "telnet://", "imap:", "rtsp://", "urn:", "pop:",
        "sip:", "sips:", "tftp:", "btspp://", "btl2cap://", "btgoep://",
        "tcpobex://", "irdaobex://", "file://", "urn:epc:id:",
        "urn:epc:tag:", "urn:epc:pat:", "urn:epc:raw:", "urn:epc:",
        "urn:nfc:",
    )
    out = []
    pos = 0
    while pos < len(ndef):
        start = pos
        flags = ndef[pos]
        pos += 1
        if pos >= len(ndef):
            raise NFCError("Truncated NDEF record")
        typelen = ndef[pos]
        pos += 1
        short = bool(flags & 0x10)
        if short:
            if pos >= len(ndef):
                raise NFCError("Truncated NDEF short payload length")
            payloadlen = ndef[pos]
            pos += 1
        else:
            if pos + 4 > len(ndef):
                raise NFCError("Truncated NDEF payload length")
            payloadlen = int.from_bytes(ndef[pos:pos+4], "big")
            pos += 4
        idlen = 0
        if flags & 0x08:
            if pos >= len(ndef):
                raise NFCError("Truncated NDEF ID length")
            idlen = ndef[pos]
            pos += 1
        end = pos + typelen + idlen + payloadlen
        if end > len(ndef):
            raise NFCError("NDEF record extends beyond message")
        typ = ndef[pos:pos+typelen]
        pos += typelen + idlen
        payload = ndef[pos:pos+payloadlen]
        pos = end
        tnf = flags & 0x07
        if tnf == 1 and typ == b"T" and payload:
            status = payload[0]
            langlen = status & 0x3F
            if 1 + langlen > len(payload):
                raise NFCError("Invalid NDEF text-language length")
            lang = payload[1:1+langlen].decode("ascii", errors="replace")
            encoding = "utf-16" if status & 0x80 else "utf-8"
            textval = payload[1+langlen:].decode(encoding, errors="replace")
            out.append(f"TEXT [{lang}]: {textval}")
        elif tnf == 1 and typ == b"U" and payload:
            prefix = uri_prefixes[payload[0]] if payload[0] < len(uri_prefixes) else ""
            uri = prefix + payload[1:].decode("utf-8", errors="replace")
            out.append(f"URI: {uri}")
        else:
            out.append(f"RECORD TNF={tnf} TYPE={hexstr(typ)} RAW={hexstr(ndef[start:end])}")
        if flags & 0x40:  # ME: message end
            if pos != len(ndef):
                raise NFCError("Trailing bytes after final NDEF record")
            break
        if len(out) > 32:
            raise NFCError("Too many NDEF records")
    return tuple(out)


def decode_type2_tlvs(memory: bytes) -> tuple[Optional[bytes], str]:
    """Parse TLVs in the Type 2 user area, skipping Lock/Memory Control TLVs."""
    pos = 0
    while pos < len(memory):
        tag = memory[pos]
        pos += 1
        if tag == 0x00:  # NULL
            continue
        if tag == 0xFE:  # Terminator
            return None, "No NDEF TLV before terminator"
        if pos >= len(memory):
            return None, "TLV length not yet read"
        size = memory[pos]
        pos += 1
        if size == 0xFF:
            if pos + 2 > len(memory):
                return None, "Extended TLV length not yet read"
            size = int.from_bytes(memory[pos:pos+2], "big")
            pos += 2
        if pos + size > len(memory):
            return None, f"TLV 0x{tag:02X} incomplete, needs {pos+size} bytes"
        if tag == 0x03:
            return memory[pos:pos+size], ""
        pos += size
    return None, "NDEF TLV not found in read data"


class PresenceTracker:
    """User-facing state and counters; independent of physical transport."""

    def __init__(self, grace_seconds: float):
        self.grace_seconds = grace_seconds
        self.state = "GEEN_KAART"
        self.uid = ""
        self.previous_uid = ""
        self.suspect_since: Optional[float] = None
        self.activations = 0
        self.read_ok = 0
        self.read_errors = 0
        self.current_failure_streak = 0
        self.rf_absences = 0
        self.ndef_ok = 0
        self.ndef_errors = 0
        self.ndef_mismatches = 0
        self.ndef_last_ms = None
        self.short_dropouts = 0
        self.recoveries = 0
        self.restarts = 0
        self.activation_failures = 0
        self.recovery_suppressed = 0
        self.unique_uids: set[str] = set()
        self.last_read_ms: Optional[float] = None
        self.last_good_at: Optional[float] = None

    def activation(self, uid: str, now: float) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        self.activations += 1
        self.unique_uids.add(uid)
        if self.state == "TWIJFEL" and uid == self.uid:
            self.short_dropouts += 1
            elapsed_ms = (now - (self.suspect_since or now)) * 1000
            events.append(("RF_HERSTELD", f"zelfde UID; onderbreking {elapsed_ms:.0f} ms"))
        elif self.uid and uid != self.uid:
            events.append(("KAART_GEWISSELD", f"{self.uid} -> {uid}"))
        elif self.state == "GEEN_KAART" and self.previous_uid == uid:
            events.append(("KAART_TERUG", uid))
        elif self.state == "GEEN_KAART":
            events.append(("KAART_GEVONDEN", uid))
        else:
            events.append(("KAART_HERKEND", uid))
        self.uid = uid
        self.state = "AANWEZIG"
        self.suspect_since = None
        self.current_failure_streak = 0
        return events

    def success(self, now: float, read_ms: float) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        if self.state == "TWIJFEL":
            self.short_dropouts += 1
            events.append(("RF_HERSTELD", "READ weer succesvol"))
        self.state = "AANWEZIG"
        self.suspect_since = None
        self.current_failure_streak = 0
        self.read_ok += 1
        self.last_good_at = now
        self.last_read_ms = read_ms
        return events

    def failure(self, now: float, message: str) -> list[tuple[str, str]]:
        self.read_errors += 1
        self.current_failure_streak += 1
        if self.state == "AANWEZIG":
            self.state = "TWIJFEL"
            self.suspect_since = now
        return [("LEESONDERBREKING", message)]

    def tick(self, now: float) -> list[tuple[str, str]]:
        if (self.state == "TWIJFEL" and self.suspect_since is not None
                and now - self.suspect_since >= self.grace_seconds):
            self.state = "GEEN_KAART"
            self.rf_absences += 1
            self.previous_uid = self.uid
            self.uid = ""
            self.suspect_since = None
            self.current_failure_streak = 0
            return [("RF_NIET_MEER_GEVONDEN", "kaart verwijderd óf RF-blind spot")]
        return []


class CSVEventLog:
    FIELDS = (
        "wall_time", "elapsed_s", "event", "state", "uid", "read_ms", "ok_count",
        "error_count", "consecutive_errors", "activations", "rf_absences",
        "short_dropouts", "recoveries", "restarts", "activation_failures", "recovery_suppressed", "detail",
        "mode", "ndef_ok", "ndef_errors", "ndef_mismatches",
        "cpu_process_one_core_pct", "cpu_system_all_cores_pct", "cpu_cores",
    )

    def __init__(self, filename: Path, tracker: PresenceTracker, start: float, verbose: bool, mode: str):
        filename.parent.mkdir(parents=True, exist_ok=True)
        self.file = filename.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, fieldnames=self.FIELDS)
        self.writer.writeheader()
        self.file.flush()
        self.path = filename
        self.tracker = tracker
        self.start = start
        self.verbose = verbose
        self.mode = mode

    def emit(self, event: str, detail: str = "", read_ms: Optional[float] = None,
             *, show: bool = True, cpu_process: Optional[float] = None,
             cpu_system: Optional[float] = None) -> None:
        t = self.tracker
        elapsed = time.monotonic() - self.start
        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        row = {
            "wall_time": stamp, "elapsed_s": f"{elapsed:.3f}", "event": event,
            "state": t.state, "uid": t.uid,
            "read_ms": "" if read_ms is None else f"{read_ms:.2f}",
            "ok_count": t.read_ok, "error_count": t.read_errors,
            "consecutive_errors": t.current_failure_streak,
            "activations": t.activations, "rf_absences": t.rf_absences,
            "short_dropouts": t.short_dropouts, "recoveries": t.recoveries,
            "restarts": t.restarts, "activation_failures": t.activation_failures,
            "recovery_suppressed": t.recovery_suppressed, "detail": detail,
            "mode": self.mode, "ndef_ok": t.ndef_ok,
            "ndef_errors": t.ndef_errors, "ndef_mismatches": t.ndef_mismatches,
            "cpu_process_one_core_pct": "", "cpu_system_all_cores_pct": "",
            "cpu_cores": os.cpu_count() or 1,
        }
        if cpu_process is not None:
            row["cpu_process_one_core_pct"] = f"{cpu_process:.2f}"
        if cpu_system is not None:
            row["cpu_system_all_cores_pct"] = f"{cpu_system:.2f}"
        self.writer.writerow(row)
        self.file.flush()
        if show:
            print(f"{stamp[11:23]}  {event:<23} {detail}", flush=True)

    def events(self, events: list[tuple[str, str]]) -> None:
        for kind, detail in events:
            self.emit(kind, detail)

    def close(self) -> None:
        self.file.close()


class CPUSampler:
    """Approximate CPU; process includes Python but not all kernel I2C costs.

    system % includes kernel/I2C and other Oradio tasks; compare with idle
    baseline on the same machine. CPU figures are not isolated driver usage.
    """

    def __init__(self):
        self.core_count = os.cpu_count() or 1
        self.last_time = time.monotonic()
        self.last_proc = time.process_time()
        self.last_total, self.last_idle = self._system()

    @staticmethod
    def _system():
        with open("/proc/stat", encoding="ascii") as f:
            values = [int(x) for x in f.readline().split()[1:]]
        idle = values[3] + values[4]
        total = sum(values)
        return total, idle

    def sample(self):
        now = time.monotonic()
        proc = time.process_time()
        total, idle = self._system()
        wall = max(now - self.last_time, 1e-9)
        proc_pct = 100 * max(0, proc - self.last_proc) / wall
        dt = total - self.last_total
        system_pct = 100 * (1 - (idle - self.last_idle) / dt) if dt > 0 else 0.0
        self.last_time, self.last_proc = now, proc
        self.last_total, self.last_idle = total, idle
        return proc_pct, max(0.0, min(100.0, system_pct))


class PN7160:
    """Small NCI 2.0 transport for observed PN7160 / NTAG behavior."""

    def __init__(self, bus_id: int, address: int, chip: str, ven: int, irq: int,
                 trace: Callable[[str, bytes], None]):
        self.bus_id, self.address = bus_id, address
        self.chip, self.ven, self.irq = chip, ven, irq
        self.trace = trace
        self.gpio = None
        self.bus = None
        self.credits = 0
        self.state = "OFF"

    def __enter__(self):
        # Delayed imports permit --self-test on any computer without Pi packages.
        import gpiod
        from gpiod.line import Direction, Value
        from smbus2 import SMBus
        self._active = Value.ACTIVE
        self._inactive = Value.INACTIVE
        self.gpio = gpiod.request_lines(
            self.chip, consumer="pn7160-rf-test",
            config={
                self.ven: gpiod.LineSettings(direction=Direction.OUTPUT,
                                             output_value=Value.INACTIVE),
                self.irq: gpiod.LineSettings(direction=Direction.INPUT),
            },
        )
        try:
            self.bus = SMBus(self.bus_id)
        except Exception:
            self.gpio.release()
            self.gpio = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.gpio is not None:
                self.gpio.set_value(self.ven, self._inactive)
        finally:
            try:
                if self.bus is not None:
                    self.bus.close()
            finally:
                if self.gpio is not None:
                    self.gpio.release()
        self.state = "OFF"

    def irq_high(self) -> bool:
        return self.gpio.get_value(self.irq) == self._active

    def write(self, data: bytes) -> None:
        from smbus2 import i2c_msg
        self.trace("TX", data)
        self.bus.i2c_rdwr(i2c_msg.write(self.address, data))

    def read_packet(self, timeout: float) -> Optional[bytes]:
        from smbus2 import i2c_msg
        deadline = time.monotonic() + max(0, timeout)
        while not self.irq_high():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.003)

        def read_n(n: int) -> bytes:
            msg = i2c_msg.read(self.address, n)
            self.bus.i2c_rdwr(msg)
            return bytes(msg)

        header = read_n(3)
        length = header[2]
        if length > 254:
            raise NFCError(f"Invalid NCI length {length}")
        payload = read_n(length) if length else b""
        frame = header + payload
        self.trace("RX", frame)
        return frame

    def handle_aux(self, pkt: bytes) -> Optional[str]:
        if pkt[:2] == b"\x60\x06":  # CORE_CONN_CREDITS_NTF
            b = pkt[3:]
            if len(b) >= 3 and b[0] >= 1:
                pos = 1
                for _ in range(b[0]):
                    if pos + 2 > len(b):
                        break
                    conn_id, delta = b[pos], b[pos + 1]
                    if conn_id == 0:
                        self.credits += delta
                    pos += 2
            return "credits"
        if pkt[:2] == b"\x61\x06":  # RF_DEACTIVATE_NTF
            self.state = "DISCOVERY" if len(pkt) >= 4 and pkt[3] == 3 else "IDLE"
            return "rf_deactivated"
        if pkt[:2] in (b"\x60\x07", b"\x60\x08"):
            return "nci_error"
        return None

    def wait_for(self, header: bytes, timeout: float = 1.5) -> bytes:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pkt = self.read_packet(deadline - time.monotonic())
            if pkt is None:
                break
            if pkt[:len(header)] == header:
                if len(pkt) < 4 or pkt[3] != 0:
                    raise NFCError(f"NCI response indicates error: {hexstr(pkt)}")
                return pkt
            self.handle_aux(pkt)
        raise NFCError(f"Timeout waiting for {hexstr(header)}")

    def reset_init(self) -> None:
        self.gpio.set_value(self.ven, self._inactive)
        time.sleep(0.15)
        self.gpio.set_value(self.ven, self._active)
        time.sleep(0.15)
        self.state = "IDLE"
        self.credits = 0
        self.write(CMD_RESET)
        self.wait_for(b"\x40\x00")
        ntf = self.read_packet(1.5)
        if ntf is None or ntf[:2] != b"\x60\x00":
            raise NFCError(f"Missing CORE_RESET_NTF: {hexstr(ntf or b'')}")
        self.write(CMD_INIT)
        self.wait_for(b"\x40\x01")
        self.write(CMD_PROP_ACT)
        self.wait_for(b"\x4F\x02")
        self.state = "IDLE"

    def discover(self) -> None:
        if self.state != "IDLE":
            raise NFCError(f"RF_DISCOVER requested from {self.state} (expected IDLE)")
        self.write(CMD_DISCOVER_A)
        self.wait_for(b"\x41\x03")
        self.state = "DISCOVERY"

    def deact_to_idle(self) -> None:
        if self.state == "IDLE":
            return
        self.write(CMD_DEACT_IDLE)
        self.wait_for(b"\x41\x06")
        # In POLL_ACTIVE the controller may send a separate RF_DEACTIVATE_NTF.
        # Consume it before starting discovery; in DISCOVERY it may be omitted.
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            pkt = self.read_packet(max(0, deadline - time.monotonic()))
            if pkt is None:
                break
            self.handle_aux(pkt)
            if pkt[:2] == b"\x61\x06":
                break
        self.state = "IDLE"
        self.credits = 0

    def read_card_details(self, uid: str, max_bytes: int = 1024) -> CardDetails:
        """Read NFC Forum Type 2 CC and NDEF; never write to the tag.

        This is invoked once per newly observed UID by default. Its I/O is
        recorded separately from the continuous presence-check reads.
        """
        cc_block = self.read_four_pages(3)  # pages 3..6
        cc = cc_block[:4]
        cap = None
        readonly = None
        note = ""
        if cc[0] == 0xE1 and (cc[1] >> 4) == 1:
            cap = cc[2] * 8  # NFC Forum Type 2 CC data area size
            readonly = (cc[3] & 0x0F) != 0
        else:
            note = f"Unexpected Type 2 Capability Container: {hexstr(cc)}"
        # CC byte 2 describes the size of the NFC Forum user data area, not
        # necessarily the full EEPROM capacity or the exact NTAG model.
        limit = min(cap if cap is not None else 64, max_bytes)
        memory = bytearray()
        ndef = None
        reason = ""
        # READ returns four pages (16 bytes) per call. Only read within
        # the declared CC data area, except harmless READ overshoot at end.
        for page in range(4, 4 + ((limit + 15) // 16) * 4, 4):
            memory.extend(self.read_four_pages(page))
            ndef, reason = decode_type2_tlvs(memory)
            if ndef is not None:
                break
            if reason == "No NDEF TLV before terminator":
                break
        records = ()
        raw = ""
        if ndef is not None:
            raw = hexstr(ndef)
            try:
                records = decode_ndef_records(ndef)
            except (NFCError, UnicodeError) as exc:
                note = (note + "; " if note else "") + f"NDEF decode error: {exc}"
        else:
            note = (note + "; " if note else "") + reason
        return CardDetails(uid=uid, cc_hex=hexstr(cc),
                           cc_capacity_bytes=cap, read_only=readonly,
                           ndef_length=len(ndef) if ndef is not None else None,
                           ndef_records=records, ndef_raw=raw, note=note)

    def read_four_pages(self, page: int, timeout: float = 0.85) -> bytes:
        if self.state != "ACTIVE":
            raise NFCError("READ while not ACTIVE")
        # NFC data connections are credit-based. If a credit arrives late,
        # consume it before transmitting another data packet.
        credit_deadline = time.monotonic() + timeout
        while self.credits <= 0 and time.monotonic() < credit_deadline:
            pkt = self.read_packet(credit_deadline - time.monotonic())
            if pkt is None:
                break
            kind = self.handle_aux(pkt)
            if kind == "rf_deactivated":
                raise NFCLinkLost("RF_DEACTIVATE_NTF before READ")
            if kind == "nci_error":
                raise NFCError(f"NCI interface error: {hexstr(pkt)}")
        if self.credits <= 0:
            raise NFCError("No NCI connection credit")

        self.write(bytes((0x00, 0x00, 0x02, 0x30, page)))
        self.credits -= 1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pkt = self.read_packet(deadline - time.monotonic())
            if pkt is None:
                break
            if pkt[:2] == b"\x00\x00":
                return parse_ntag_read(pkt)
            kind = self.handle_aux(pkt)
            if kind == "rf_deactivated":
                raise NFCLinkLost(f"RF_DEACTIVATE_NTF while reading: {hexstr(pkt)}")
            if kind == "nci_error":
                raise NFCError(f"NCI interface error: {hexstr(pkt)}")
        raise NFCError("Tag READ timeout")


def print_summary(t: PresenceTracker, elapsed: float) -> None:
    latency = "—" if t.last_read_ms is None else f"{t.last_read_ms:.1f} ms"
    print(
        f"STATUS={t.state:<11} UID={t.uid or '—':<21} "
        f"OK={t.read_ok:<6} NIET_GELEZEN={t.read_errors:<5} "
        f"RF_WEG={t.rf_absences:<4} KORT={t.short_dropouts:<4} "
        f"HERSTEL={t.recoveries:<4} NDEF={t.ndef_ok}/{t.ndef_errors}/{t.ndef_mismatches} "
        f"Laatste={latency:<9} t={elapsed:.1f}s",
        flush=True,
    )


def try_stdin(log: CSVEventLog) -> bool:
    """Non-blocking marking: Enter, or `m positie`, or `q` + Enter."""
    if not sys.stdin.isatty():
        return False
    try:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
    except (ValueError, OSError):
        return False
    if not ready:
        return False
    line = sys.stdin.readline()
    if not line:
        return False
    line = line.strip()
    if line.lower() in ("q", "quit"):
        return True
    if line.lower().startswith("m "):
        line = line[2:].strip()
    log.emit("MARKERING", line or "meetmoment (Enter)")
    return False


def self_test() -> None:
    # Real packets recorded from the successful Oradio4 PN7160 tests.
    activated = bytes.fromhex(
        "61 05 18 01 01 02 00 FF 01 0D 44 00 07 04 59 78 D2 BC 21 90 "
        "01 00 00 00 00 00 00")
    tag = parse_activation(activated)
    assert tag.uid == "04 59 78 D2 BC 21 90" and tag.credits == 1
    pkt = bytes.fromhex("00 00 11 01 03 A0 0C 34 03 2A D1 01 26 54 02 65 6E 6F 72 00")
    assert parse_ntag_read(pkt) == bytes.fromhex("01 03 A0 0C 34 03 2A D1 01 26 54 02 65 6E 6F 72")
    p = PresenceTracker(grace_seconds=0.5)
    assert p.activation(tag.uid, 1.0)[0][0] == "KAART_GEVONDEN"
    assert not p.success(1.1, 9.5)
    assert p.failure(1.2, "RF timeout")[0][0] == "LEESONDERBREKING"
    assert p.state == "TWIJFEL"
    assert not p.tick(1.5)
    assert p.activation(tag.uid, 1.55)[0][0] == "RF_HERSTELD"
    assert p.short_dropouts == 1 and p.state == "AANWEZIG"
    p.failure(2.0, "RF timeout")
    assert not p.tick(2.3)
    assert p.tick(2.51)[0][0] == "RF_NIET_MEER_GEVONDEN"
    assert p.activation(tag.uid, 2.9)[0][0] == "KAART_TERUG"
    assert p.read_errors == 2 and p.rf_absences == 1
    memory = bytes.fromhex(
        "01 03 A0 0C 34 03 2A D1 01 26 54 02 65 6E 6F 72 "
        "61 64 69 6F 3A 76 31 3A 63 61 72 64 3A 74 68 65 "
        "6D 65 3A 6A 61 72 65 6E 5F 7A 65 76 65 6E 74 69 "
        "67 FE 00 00 00 00 00 00 00 00 00 00 00 00 00 00")
    ndef, note = decode_type2_tlvs(memory)
    assert note == "" and len(ndef) == 42
    assert decode_ndef_records(ndef) == (
        "TEXT [en]: oradio:v1:card:theme:jaren_zeventig",)
    assert decode_type2_tlvs(bytes.fromhex("03 05 D1"))[0] is None
    assert CPUSampler().sample()[0] >= 0
    print("SELF-TEST OK: captured NCI frames, UID, hysteresis, NDEF and CPU metrics")


def run(args: argparse.Namespace) -> None:
    tracker = PresenceTracker(args.loss_grace)
    started = time.monotonic()
    if args.log:
        csv_file = Path(args.log).expanduser()
    else:
        date_code = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_file = Path.home() / "pn7160_logs" / f"pn7160_rf_{date_code}.csv"
    log = CSVEventLog(csv_file, tracker, started, verbose=args.verbose, mode=args.mode)
    cpu_sampler = CPUSampler()
    next_cpu = time.monotonic() + args.cpu_interval
    last_summary = 0.0
    next_read = 0.0
    last_uid = ""
    card_cache: dict[str, CardDetails] = {}
    baseline_by_uid: dict[str, tuple[str, ...]] = {}
    # Recovery scheduler: never issue deactivation/discovery for every notification.
    pending_recovery: Optional[str] = None
    next_recovery_at = 0.0
    recovery_fail_streak = 0
    activation_error_window_start = 0.0
    activation_error_window_count = 0
    activation_error_last_log = 0.0
    a1_burst_streak = 0

    def trace(direction: str, pkt: bytes) -> None:
        if args.trace_nci:
            log.emit("NCI_" + direction, hexstr(pkt), show=args.verbose)

    def schedule_recovery(device: PN7160, reason: str, *, reset: bool = False,
                          delay: Optional[float] = None) -> None:
        """Schedule one recovery, coalescing bursts rather than restarting immediately."""
        nonlocal pending_recovery, next_recovery_at
        if pending_recovery is not None:
            tracker.recovery_suppressed += 1
            return
        pending_recovery = ("RESET:" if reset else "RF:") + reason
        wait = args.recovery_cooldown if delay is None else delay
        next_recovery_at = time.monotonic() + wait
        device.state = "RECOVERY_WAIT"
        log.emit("HERSTEL_GEPLAND", f"{reason}; wachttijd {wait:.2f}s")

    def execute_recovery(device: PN7160) -> None:
        """Bounded RF retry, fallback reset, then exponential cooldown if unsuccessful."""
        nonlocal pending_recovery, next_recovery_at, recovery_fail_streak
        if pending_recovery is None or time.monotonic() < next_recovery_at:
            return
        reason = pending_recovery
        pending_recovery = None
        t0 = time.monotonic()
        tracker.recoveries += 1
        log.emit("HERSTEL_START", reason)
        try:
            if reason.startswith("RESET:"):
                tracker.restarts += 1
                device.reset_init()
                device.discover()
                action = "VEN_RESET"
            else:
                # The state was deliberately set to RECOVERY_WAIT by the scheduler;
                # force RF deactivation to IDLE before issuing RF_DISCOVER again.
                device.state = "ACTIVE"
                try:
                    device.deact_to_idle()
                    device.discover()
                    action = "RF_DISCOVERY"
                except (OSError, NFCError) as exc:
                    log.emit("RF_HERSTEL_MISLUKT", str(exc))
                    tracker.restarts += 1
                    device.reset_init()
                    device.discover()
                    action = "VEN_RESET_FALLBACK"
            recovery_fail_streak = 0
            log.emit("HERSTEL_OK", action,
                     read_ms=(time.monotonic()-t0)*1000)
        except (OSError, NFCError) as exc:
            recovery_fail_streak += 1
            pause = min(args.recovery_max_backoff,
                        args.recovery_cooldown * (2 ** min(recovery_fail_streak, 8)))
            pending_recovery = "RESET:previous recovery failed"
            next_recovery_at = time.monotonic() + pause
            device.state = "RECOVERY_WAIT"
            log.emit("HERSTEL_MISLUKT", f"{exc}; volgende poging na {pause:.1f}s")

    print("=== ORADIO4 PN7160 RF / NDEF / CPU v0.4 ===")
    print("Mode:", args.mode)
    print("Software-I2C:", args.bus, "  SDA=GPIO12 SCL=GPIO13 VEN=GPIO17 IRQ=GPIO27")
    print(f"Leesinterval={args.interval:.2f}s  RF-grens={args.loss_grace:.2f}s")
    print("Geen kaart schrijven; test leest NTAG pagina's 4–7")
    print(f"Herstel: cooldown={args.recovery_cooldown}s; A1-drempel={args.activation_burst_threshold} in {args.activation_burst_window}s")
    print("Tijdens SSH: Enter = markeren, 'm midden links' = positie, 'q' = stoppen")
    print("CSV-log:", csv_file, flush=True)

    def sample_cpu(now: float) -> None:
        nonlocal next_cpu
        if now >= next_cpu:
            proc, system = cpu_sampler.sample()
            log.emit("CPU_SAMPLE", f"Python={proc:.1f}% van 1 kern, systeem={system:.1f}% totaal",
                     cpu_process=proc, cpu_system=system, show=True)
            next_cpu = now + args.cpu_interval

    try:
        if args.mode == "cpu-idle":
            log.emit("START", "idle reference: no I2C access, no GPIO claim")
            while args.duration <= 0 or time.monotonic() - started < args.duration:
                now = time.monotonic()
                sample_cpu(now)
                if try_stdin(log):
                    break
                time.sleep(0.05)
            return

        with PN7160(args.bus, args.address, args.chip, args.ven, args.irq, trace) as device:
            log.emit("START", f"bus={args.bus}, addr=0x{args.address:02X}")
            device.reset_init()
            device.discover()
            log.emit("DISCOVERY_START", "NFC-A Type 2 (NTAG213/215/216)")

            while True:
                now = time.monotonic()
                elapsed = now - started
                if args.duration > 0 and elapsed >= args.duration:
                    break
                sample_cpu(now)
                if try_stdin(log):
                    log.emit("STOP_GEBRUIKER", "q + Enter")
                    break
                log.events(tracker.tick(now))
                if now - last_summary >= args.summary_interval:
                    print_summary(tracker, elapsed)
                    last_summary = now

                if device.state == "RECOVERY_WAIT":
                    execute_recovery(device)
                    time.sleep(0.02)
                    continue

                if device.state == "DISCOVERY":
                    try:
                        packet = device.read_packet(0.075)
                        if packet is None:
                            continue  # Normal: no tag in range, not a read error.
                        if packet[:2] == b"\x61\x05":
                            tag = parse_activation(packet)
                            a1_burst_streak = 0
                            activation_error_window_count = 0
                            device.state = "ACTIVE"
                            device.credits = tag.credits
                            last_uid = tag.uid
                            log.events(tracker.activation(tag.uid, time.monotonic()))
                            next_read = time.monotonic()
                            if (args.tag_info or args.mode == "ndef") and (args.tag_info_each_activation or tag.uid not in card_cache):
                                info_start = time.monotonic()
                                try:
                                    details = device.read_card_details(tag.uid, args.max_ndef_bytes)
                                    card_cache[tag.uid] = details
                                    if details.ndef_records and tag.uid not in baseline_by_uid:
                                        baseline_by_uid[tag.uid] = details.ndef_records
                                    summary = (f"UID={tag.uid}; NFC Forum Type 2; "
                                               f"CC={details.cc_hex}; "
                                               f"NDEF={details.ndef_length} bytes; "
                                               f"capacity={details.cc_capacity_bytes} bytes; "
                                               f"read_only={details.read_only}")
                                    log.emit("TAG_INFO", summary)
                                    for record in details.ndef_records:
                                        log.emit("TAG_NDEF", record)
                                    if details.note:
                                        log.emit("TAG_INFO_OPMERKING", details.note)
                                    log.emit("TAG_INFO_TIJD", "apart van RF-metingen",
                                             read_ms=(time.monotonic()-info_start)*1000,
                                             show=args.verbose)
                                except (OSError, NFCError) as exc:
                                    # A failed metadata READ must not be silently counted as
                                    # successful. Report it separately and recover discovery.
                                    log.emit("TAG_INFO_NIET_GELEZEN", str(exc))
                                    log.events(tracker.failure(time.monotonic(),
                                                               "tag info: " + str(exc)))
                                    schedule_recovery(device, "tag info read interrupted")
                        elif packet[:2] == b"\x61\x03":
                            log.emit("MEERDERE_KAARTEN", "RF_DISCOVER_NTF; handmatige selectie niet ondersteund")
                            schedule_recovery(device, "RF_DISCOVER_NTF requires selection")
                        else:
                            kind = device.handle_aux(packet)
                            if kind == "rf_deactivated":
                                # Device may have switched to DISCOVERY or IDLE itself.
                                log.emit("RF_DEACTIVATIE", hexstr(packet))
                                if device.state != "DISCOVERY":
                                    schedule_recovery(device, "onverwachte RF-deactivatie")
                            elif kind == "nci_error":
                                if packet == bytes.fromhex("60 07 01 A1"):
                                    # NXP DISCOVERY_TARGET_ACTIVATION_FAILED. Log all in CSV,
                                    # but do NOT react to each notification with RF_DEACTIVATE.
                                    tracker.activation_failures += 1
                                    t = time.monotonic()
                                    if t - activation_error_window_start > args.activation_burst_window:
                                        activation_error_window_start = t
                                        activation_error_window_count = 0
                                    activation_error_window_count += 1
                                    log.emit("KAARTACTIVATIE_NIET_GELUKT", hexstr(packet),
                                             show=(activation_error_window_count == 1 or
                                                   t - activation_error_last_log >= 2.0))
                                    if t - activation_error_last_log >= 2.0:
                                        activation_error_last_log = t
                                    if activation_error_window_count >= args.activation_burst_threshold:
                                        log.emit("A1_REEKS", f"{activation_error_window_count} meldingen in "
                                                 f"{args.activation_burst_window:.1f}s; eerst rustperiode")
                                        activation_error_window_count = 0
                                        activation_error_window_start = t
                                        # Deliberately avoid RF restart for isolated A1. A
                                        # persistent stream triggers a bounded reset later.
                                        a1_burst_streak += 1
                                        delay = min(args.recovery_max_backoff,
                                                    2.0 * (2 ** min(a1_burst_streak - 1, 5)))
                                        schedule_recovery(device, "repeated A1 activation failures",
                                                          reset=True, delay=delay)
                                else:
                                    log.emit("NCI_PROTOCOLMELDING", hexstr(packet))
                                    schedule_recovery(device, "other NCI error")
                            elif kind != "credits":
                                log.emit("ONVERWACHT_BERICHT", hexstr(packet))
                    except (OSError, NFCError) as exc:
                        log.emit("DISCOVERY_FOUT", str(exc))
                        schedule_recovery(device, "discovery exception")
                    continue

                if device.state == "ACTIVE":
                    if now < next_read:
                        time.sleep(min(0.02, next_read - now))
                        continue
                    t0 = time.monotonic()
                    try:
                        if args.mode == "ndef":
                            details = device.read_card_details(last_uid, args.max_ndef_bytes)
                            latency = (time.monotonic() - t0) * 1000
                            if not details.ndef_records or details.note:
                                raise NFCError(f"NDEF missing/invalid: {details.note}")
                            baseline = baseline_by_uid.setdefault(last_uid, details.ndef_records)
                            if details.ndef_records != baseline:
                                tracker.ndef_mismatches += 1
                                log.emit("NDEF_MISMATCH", f"expected={baseline!r} received={details.ndef_records!r}",
                                         read_ms=latency)
                            else:
                                tracker.ndef_ok += 1
                                log.emit("NDEF_OK", "; ".join(details.ndef_records),
                                         read_ms=latency, show=args.verbose)
                            tracker.ndef_last_ms = latency
                        else:
                            result = device.read_four_pages(args.page, args.read_timeout)
                            latency = (time.monotonic() - t0) * 1000
                            log.emit("READ_OK", f"PAGE {args.page}: {hexstr(result)}",
                                     read_ms=latency, show=args.verbose)
                        log.events(tracker.success(time.monotonic(), latency))
                        next_read = time.monotonic() + args.interval
                    except (OSError, NFCError) as exc:
                        if args.mode == "ndef":
                            tracker.ndef_errors += 1
                            log.emit("NDEF_NIET_GELEZEN", str(exc))
                        log.events(tracker.failure(time.monotonic(), str(exc)))
                        schedule_recovery(device, f"READ interrupted, UID={last_uid}")
                        next_read = time.monotonic() + args.interval
                    continue

                # RF controller unexpected state -> try to recover; no tight loop.
                time.sleep(0.05)
                schedule_recovery(device, f"unexpected RF state={device.state}")

    except KeyboardInterrupt:
        print("\nCtrl+C: test gestopt")
        log.emit("STOP_GEBRUIKER", "Ctrl+C")
    except Exception as exc:
        log.emit("FATALE_FOUT", f"{type(exc).__name__}: {exc}")
        print(f"FATALE FOUT: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
    finally:
        elapsed = time.monotonic() - started
        print("\n=== EINDRESULTAAT ===")
        print_summary(tracker, elapsed)
        print(f"Unieke UID's: {len(tracker.unique_uids)}")
        print(f"Kaartactivaties niet gelukt (A1): {tracker.activation_failures}")
        print(f"Samengevoegde herstelverzoeken: {tracker.recovery_suppressed}")
        print(f"CSV-bestand: {csv_file}")
        log.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="PN7160 RF, NDEF and CPU tests with CSV log")
    parser.add_argument("--mode", choices=("rf", "ndef", "cpu-idle"), default="rf",
                        help="rf: one 16-byte read; ndef: full NDEF every cycle; cpu-idle: CPU baseline without NFC")
    parser.add_argument("--cpu-interval", type=float, default=5.0,
                        help="CPU sample interval in seconds (default 5)")
    parser.add_argument("--bus", type=int, default=8, help="I2C bus; default 8 (i2c-gpio)")
    parser.add_argument("--address", type=lambda x: int(x, 0), default=0x28, help="I2C address (0x28)")
    parser.add_argument("--chip", default="/dev/gpiochip0")
    parser.add_argument("--ven", type=int, default=17)
    parser.add_argument("--irq", type=int, default=27)
    parser.add_argument("--page", type=int, default=4, choices=range(4, 36), metavar="4..35",
                        help="Start page of four-page READ (default 4)")
    parser.add_argument("--interval", type=float, default=0.20,
                        help="Pause after each successful READ (default 0.2 s)")
    parser.add_argument("--recovery-cooldown", type=float, default=0.75,
                        help="Wait before recovery after link loss (default 0.75 s)")
    parser.add_argument("--recovery-max-backoff", type=float, default=8.0,
                        help="Cap retry backoff after recovery failure (default 8 s)")
    parser.add_argument("--activation-burst-threshold", type=int, default=8,
                        help="A1 notifications within window before one controlled reset (default 8)")
    parser.add_argument("--activation-burst-window", type=float, default=3.0,
                        help="Time window for A1 notification burst in seconds (default 3)")
    parser.add_argument("--read-timeout", type=float, default=0.85,
                        help="Max time waiting for READ data/credit, default 0.85 s")
    parser.add_argument("--loss-grace", type=float, default=0.70,
                        help="RF non-detection debounce, default 0.7 s")
    parser.add_argument("--summary-interval", type=float, default=2.0,
                        help="Print summary every N seconds")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="Auto-stop after N seconds, 0 = run until Ctrl+C")
    parser.add_argument("--log", help="CSV output path; default ~/pn7160_logs/timestamp.csv")
    parser.add_argument("--verbose", action="store_true", help="Print every READ_OK and NCI trace")
    parser.add_argument("--trace-nci", action="store_true", help="Log raw NCI TX/RX to CSV")
    parser.add_argument("--tag-info", action=argparse.BooleanOptionalAction, default=True,
                        help="Read CC and NDEF once per new UID (default enabled)")
    parser.add_argument("--tag-info-each-activation", action="store_true",
                        help="Repeat CC/NDEF on each reactivation, increases RF traffic")
    parser.add_argument("--max-ndef-bytes", type=int, default=1024,
                        help="Maximum Type 2 user bytes to scan for NDEF (default 1024)")
    parser.add_argument("--self-test", action="store_true", help="No hardware: validate captured NCI frames")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    for name in ("interval", "read_timeout", "loss_grace", "summary_interval", "cpu_interval"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_','-')} must be > 0")
    if args.max_ndef_bytes < 16 or args.max_ndef_bytes > 4096:
        parser.error("--max-ndef-bytes must be between 16 and 4096")
    run(args)


if __name__ == "__main__":
    main()
