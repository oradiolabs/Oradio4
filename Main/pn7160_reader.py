"""Standalone PN7160 reader based on the Oradio v0.4 hardware reference."""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

CMD_RESET = bytes.fromhex("20 00 01 00")
CMD_INIT = bytes.fromhex("20 01 02 00 00")
CMD_PROP_ACT = bytes.fromhex("2F 02 00")
CMD_GET_PMU_CFG = bytes.fromhex("20 03 03 01 A0 0E")
PMU_CFG_RESPONSE_PREFIX = bytes.fromhex("40 03 10 00 01 A0 0E 0B")
ELECHOUSE_5V_PMU_CFG = bytes.fromhex("11 01 C2 B2 00 B2 1E 1F 00 D0 0C")
CMD_DISCOVER_A = bytes.fromhex("21 03 03 01 00 01")
CMD_DEACT_IDLE = bytes.fromhex("21 06 01 00")


class NFCError(Exception):
    """Recoverable PN7160, NCI or tag-read problem."""


class TagReadTimeout(NFCError):
    """No response to the tag READ within its response deadline."""


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
    return TagInfo(uid=uid_bytes.hex(":").upper(), credits=credits,
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


class _NCITransport:
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
            self.chip, consumer="oradio-pn7160",
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
        raise TagReadTimeout("Tag READ timeout")


@dataclass(frozen=True)
class Observation:
    """State is present, absent (quiet discovery), or uncertain."""

    state: str
    uid: str | None = None


class ReaderOffline(RuntimeError):
    """Recovery budget exhausted. The process supervisor may restart the reader."""


class PN7160Reader:
    """Single-owner reader. Call poll and read_ndef from the same thread."""

    def __init__(self, bus=8, address=0x28, chip='/dev/gpiochip0', ven=17, irq=27,
                 trace=None, report=None):
        self._transport = _NCITransport(bus, address, chip, ven, irq, trace or (lambda *_: None))
        self._report = report or (lambda _: None)
        self._uid = None
        self._recovery = None
        self._recovery_failures = 0
        self._a1_start = None
        self._a1_count = 0
        self._a1_bursts = 0

    def __enter__(self):
        transport = self._transport
        transport.__enter__()
        try:
            transport.reset_init()
            self._check_pmu_config()
            transport.discover()
        except BaseException:
            transport.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc):
        return self._transport.__exit__(*exc)

    def _check_pmu_config(self):
        transport = self._transport
        try:
            transport.write(CMD_GET_PMU_CFG)
            response = transport.wait_for(b"\x40\x03")
            prefix_length = len(PMU_CFG_RESPONSE_PREFIX)
            if (not response.startswith(PMU_CFG_RESPONSE_PREFIX)
                    or len(response) != prefix_length + len(ELECHOUSE_5V_PMU_CFG)):
                raise NFCError(f"Unexpected PMU_CFG response: {hexstr(response)}")
        except (NFCError, OSError) as exc:
            self._report(f"WARNING PN7160 PMU_CFG read failed: {exc}")
            return

        config = response[prefix_length:]
        if config != ELECHOUSE_5V_PMU_CFG:
            self._report(
                f"WARNING PN7160 PMU_CFG differs from Elechouse 5 V reference: "
                f"read {hexstr(config)}, expected {hexstr(ELECHOUSE_5V_PMU_CFG)}"
            )

    def _schedule(self, reason, delay=0.75, reset=False):
        self._uid = None
        self._recovery = (time.monotonic() + delay, reset)
        self._report(reason)

    def _recover(self):
        due, reset = self._recovery
        if time.monotonic() < due:
            return
        transport = self._transport
        try:
            if not reset:
                try:
                    transport.deact_to_idle()
                    transport.discover()
                    self._report('RF_DISCOVERY')
                except (OSError, NFCError):
                    reset = True
            if reset:
                transport.reset_init()
                transport.discover()
                self._report('VEN_RESET')
        except (OSError, NFCError) as exc:
            self._recovery_failures += 1
            if self._recovery_failures >= 3:
                raise ReaderOffline('PN7160 recovery failed three consecutive times') from exc
            delay = min(8.0, 0.75 * 2 ** self._recovery_failures)
            self._schedule(str(exc), delay, reset=True)
        else:
            self._recovery = None
            self._recovery_failures = 0

    def _activation_failure(self):
        now = time.monotonic()
        if self._a1_start is None or now - self._a1_start > 3.0:
            self._a1_start = now
            self._a1_count = 0
        self._a1_count += 1
        if self._a1_count >= 8:
            self._a1_start = None
            self._a1_bursts += 1
            delay = min(8.0, 2.0 * 2 ** min(self._a1_bursts - 1, 5))
            self._schedule('Repeated A1 activation failures', delay, reset=True)

    def poll(self):
        """Read one discovery notification or perform one presence READ."""
        if self._recovery is not None:
            self._recover()
            return Observation('uncertain')
        transport = self._transport
        try:
            if transport.state == 'ACTIVE':
                transport.read_four_pages(4, timeout=0.25)
                return Observation('present', self._uid)
            if transport.state != 'DISCOVERY':
                raise NFCError(f'Unexpected PN7160 state: {transport.state}')
            packet = transport.read_packet(0.075)
            if packet is None:
                return Observation('absent')
            if packet[:2] == b'\x61\x05':
                tag = parse_activation(packet)
                transport.credits = tag.credits
                transport.state = 'ACTIVE'
                self._uid = tag.uid
                self._a1_start = None
                self._a1_count = self._a1_bursts = 0
                return Observation('present', self._uid)
            if packet == b'\x60\x07\x01\xa1':
                self._activation_failure()
            else:
                kind = transport.handle_aux(packet)
                if kind == 'rf_deactivated' and transport.state == 'DISCOVERY':
                    self._uid = None
                elif kind != 'credits':
                    raise NFCError(f'Unexpected discovery notification: {hexstr(packet)}')
        except TagReadTimeout as exc:
            self._schedule(str(exc), delay=0.20)
        except (OSError, NFCError) as exc:
            self._schedule(str(exc))
        return Observation('uncertain')

    def read_ndef(self, expected_uid):
        """Return one raw Type 2 NDEF message from the current activation."""
        if expected_uid != self._uid or self._transport.state != 'ACTIVE':
            raise NFCLinkLost('NDEF requested for an inactive UID')
        try:
            cc = self._transport.read_four_pages(3)[:4]
            if cc[0] != 0xe1 or cc[1] >> 4 != 1 or not cc[2]:
                raise ValueError(f'Unsupported Type 2 capability container: {hexstr(cc)}')
            limit = min(cc[2] * 8, 1024)
            memory = bytearray()
            for page in range(4, 4 + ((limit + 15) // 16) * 4, 4):
                memory.extend(self._transport.read_four_pages(page))
                ndef, reason = decode_type2_tlvs(memory[:limit])
                if ndef is not None:
                    return ndef
                if reason == 'No NDEF TLV before terminator':
                    break
            raise ValueError('No complete NDEF message in Type 2 data area')
        except (OSError, NFCError) as exc:
            self._schedule(str(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description='Standalone read-only PN7160 smoke test')
    parser.add_argument('--duration', type=float, default=60)
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error('--duration must be positive')
    deadline = time.monotonic() + args.duration
    previous = Observation('uncertain')
    try:
        with PN7160Reader(report=lambda message: print(f'READER {message}', flush=True)) as reader:
            print('READY', flush=True)
            while time.monotonic() < deadline:
                observed = reader.poll()
                if observed != previous:
                    print(observed.state.upper(), observed.uid or '', flush=True)
                    if observed.uid is not None:
                        try:
                            print('NDEF', hexstr(reader.read_ndef(observed.uid)), flush=True)
                        except (ValueError, OSError, NFCError) as exc:
                            print('NDEF_ERROR', exc, flush=True)
                    previous = observed
                time.sleep(0.20 if observed.state == 'present' else 0.025)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
