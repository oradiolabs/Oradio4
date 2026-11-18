"""Own the standalone NFC reader process; forward only CARD events to Oradio.

Bounded reader-process restart after a fault; no bus reset, Pi reboot or
power cycling. Oradio keeps running. Standalone nfc_service.py is unchanged.
"""
import json
from datetime import datetime
import subprocess
import sys
import threading
from pathlib import Path


class NFCBridge:
    def __init__(self, publish, log, ready=lambda: True, command=None, log_path=None, restart_delays=(2, 5, 10)):
        self.publish = publish
        self.log = log
        self.ready = ready
        self.command = command or [sys.executable, '-u',
                                  str(Path(__file__).with_name('nfc_service.py'))]
        self.log_path = Path(log_path) if log_path else Path.home() / '.local/state/oradio/nfc_bridge.log'
        # Lifetime budget: READY alone never resets it, preventing an endless
        # crash/reopen cycle. A deliberate Oradio restart starts a new budget.
        self.restart_delays = tuple(restart_delays)
        self._last_uid = None
        self.stop_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process = None
        self.thread = threading.Thread(target=self._run, daemon=True, name='nfc-bridge')

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self._process_lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
        if process is not None:
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self.thread.is_alive() and threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)

    @staticmethod
    def _write(output, text):
        stamp = datetime.now().astimezone().isoformat(timespec='milliseconds')
        output.write(f'{stamp} {text.rstrip()}\n')

    def _read_process(self, output, recovering):
        with self._process_lock:
            if self.stop_event.is_set():
                return None, None
            process = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True,
                                       encoding='utf-8', errors='replace', bufsize=1)
            self._process = process
        self._write(output, '--- NFC reader started ---')
        self.log.info('NFC reader started; events: %s', self.log_path)
        last_error = None
        awaiting_card = recovering
        try:
            for line in process.stdout:
                self._write(output, line)
                if self.stop_event.is_set():
                    continue
                # Keep the actual STOP cause for oradio.log, not just exit 1.
                if line.startswith('STOP:'):
                    last_error = line.strip()
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get('event')
                if kind == 'READY' and recovering:
                    self.log.warning('NFC reader reopened after failure; READY received; awaiting card detection')
                elif kind == 'REMOVED':
                    self._last_uid = None
                elif kind == 'CARD':
                    if awaiting_card:
                        self.log.info('NFC recovery confirmed by card detection: %s', event.get('uid'))
                        awaiting_card = False
                    uid = event.get('uid')
                    # Reopening resets the reader's presentation state. Do not
                    # restart music merely because the same card stayed put.
                    if uid is not None and uid == self._last_uid:
                        continue
                    self.publish(event)
                    self._last_uid = uid
            code = process.wait()
            self._write(output, f'--- NFC reader exited: {code} ---')
            return code, last_error
        finally:
            if process.stdout:
                process.stdout.close()

    def _run(self):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open('a', encoding='utf-8', buffering=1) as output:
                restarts = 0
                while not self.stop_event.is_set():
                    while not self.stop_event.is_set() and not self.ready():
                        self.stop_event.wait(0.5)
                    if self.stop_event.is_set():
                        return
                    code, cause = self._read_process(output, recovering=restarts > 0)
                    if self.stop_event.is_set():
                        return
                    self.log.warning('NFC reader stopped unexpectedly (exit %s): %s; Oradio continues',
                                     code, cause or 'no STOP detail; see NFC log')
                    if restarts >= len(self.restart_delays):
                        message = ('NFC automatic recovery exhausted; reader remains offline. '
                                   'Check NFC log and PN532 power/reset; Oradio continues')
                        self.log.error(message)
                        self._write(output, message)
                        return
                    delay = self.restart_delays[restarts]
                    restarts += 1
                    self.log.warning('NFC automatic recovery %s/%s: restarting reader in %s seconds',
                                     restarts, len(self.restart_delays), delay)
                    self._write(output, f'--- NFC recovery {restarts}/{len(self.restart_delays)} in {delay}s ---')
                    if self.stop_event.wait(delay):
                        return
        except Exception:
            self.log.exception('NFC bridge failed; Oradio continues')
            # A forwarding/configuration failure is not repaired by repeatedly
            # opening the chip. Reap any child and leave the bridge stopped.
            self.stop()
