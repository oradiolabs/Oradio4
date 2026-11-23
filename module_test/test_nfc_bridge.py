"""Process-boundary tests, no NFC hardware or Oradio services."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Main'))
from nfc_bridge import NFCBridge

class BridgeTests(unittest.TestCase):
    def make_bridge(self, source, **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        events=[]
        kwargs.setdefault("restart_delays", ())
        bridge=NFCBridge(events.append, Mock(), command=[sys.executable,'-u','-c',source],
                         log_path=Path(directory.name)/'events.log', **kwargs)
        self.addCleanup(bridge.stop)
        return bridge,events

    def test_only_card_forwarded_and_exit_is_contained(self):
        lines=[{'event':'READY'},{'event':'CARD','code':'test'}, {'event':'REMOVED'}, {'event':'SUMMARY'}]
        source='import sys\nprint("noise")\n'+''.join('print('+repr(json.dumps(e))+')\n' for e in lines)+'sys.exit(1)'
        bridge,events=self.make_bridge(source)
        bridge.start(); bridge.thread.join(5)
        self.assertFalse(bridge.thread.is_alive())
        self.assertEqual(events,[lines[1]])
        self.assertIn('exited: 1',bridge.log_path.read_text())

    def test_stop_before_ready_does_not_spawn(self):
        bridge,events=self.make_bridge('raise Exception("must not run")',ready=lambda:False)
        bridge.start(); bridge.stop()
        self.assertIsNone(bridge._process)
        self.assertEqual(events,[])

    def test_stop_terminates_child(self):
        bridge,events=self.make_bridge('import time\nprint(\'{"event":"CARD"}\')\ntime.sleep(30)')
        received=threading.Event()
        bridge.publish=lambda event:received.set()
        bridge.start()
        self.assertTrue(received.wait(5))
        bridge.stop()
        self.assertIsNotNone(bridge._process.poll())
        self.assertFalse(bridge.thread.is_alive())

    def test_publish_failure_stops_child(self):
        bridge,_=self.make_bridge('import time\nprint(\'{"event":"CARD"}\')\ntime.sleep(30)')
        bridge.publish=Mock(side_effect=RuntimeError('publisher failed'))
        bridge.start(); bridge.thread.join(5)
        self.assertFalse(bridge.thread.is_alive())
        self.assertIsNotNone(bridge._process.poll())

    def test_failed_reader_restarts_with_bounded_budget(self):
        bridge,events=self.make_bridge('print("STOP: NFC_EIO (-1)"); raise SystemExit(1)',
                                      restart_delays=(0,0,0))
        bridge.start(); bridge.thread.join(5)
        self.assertFalse(bridge.thread.is_alive())
        log=bridge.log_path.read_text()
        self.assertEqual(log.count('--- NFC reader started ---'),4)
        self.assertIn('recovery exhausted',log)
        bridge.log.error.assert_called_once()
        self.assertTrue(any('NFC_EIO' in str(call) for call in bridge.log.warning.call_args_list))

    def test_recovery_does_not_restart_music_for_held_card(self):
        # Each process reports the same held card. Only the first is forwarded.
        source='print(\'{"event":"READY"}\'); print(\'{"event":"CARD","uid":"A"}\'); raise SystemExit(1)'
        bridge,events=self.make_bridge(source,restart_delays=(0,))
        bridge.start(); bridge.thread.join(5)
        self.assertEqual(len(events),1)
        self.assertTrue(any('recovery confirmed' in str(c) for c in bridge.log.info.call_args_list))
        self.assertTrue(any('READY received' in str(c) for c in bridge.log.warning.call_args_list))

    def test_removal_allows_same_card_after_recovery(self):
        source='print(\'{"event":"CARD","uid":"A"}\'); print(\'{"event":"REMOVED"}\'); raise SystemExit(1)'
        bridge,events=self.make_bridge(source,restart_delays=(0,))
        bridge.start(); bridge.thread.join(5)
        self.assertEqual(len(events),2)

    def test_stop_during_backoff_prevents_restart(self):
        bridge,events=self.make_bridge('raise SystemExit(1)',restart_delays=(30,))
        waiting=threading.Event()
        def warning(message,*args):
            if 'automatic recovery %s/%s' in message:
                waiting.set()
        bridge.log.warning.side_effect=warning
        bridge.start()
        self.assertTrue(waiting.wait(5))
        bridge.stop()
        self.assertFalse(bridge.thread.is_alive())
        self.assertEqual(bridge.log_path.read_text().count('--- NFC reader started ---'),1)

if __name__=='__main__':
    unittest.main()
