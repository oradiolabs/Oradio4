"""Simulated CARD -> real state-machine/MPD method, fake hardware only.
Run: python3 -m unittest discover -s module_test -p test_music_cards.py -v
"""
import ast
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Main'))
from music_card_control import handle_card_event
from state_machine_race_test import _load, _Recorder

EVENT = {'event': 'CARD', 'code': 'oradio:v1:card:theme:jaren_zestig',
         'card': {'playlist': 'Pop60s', 'name_nl': 'Jaren 60'}}

class CardTests(unittest.TestCase):
    def setUp(self):
        self.recorder = _Recorder()
        machine, self.ns = _load(self.recorder)
        self.machine = machine()
        self.play = Mock(return_value=True)
        self.ns['mpd_control'].play_playlist = self.play

    def test_card_from_preset_stop_and_play(self):
        for state in ('StatePreset1', 'StatePreset2', 'StatePreset3',
                      'StateStop', 'StatePlay', 'StateIdle'):
            with self.subTest(state=state):
                self.machine.state = state
                self.recorder.clear()
                self.assertTrue(handle_card_event(EVENT, self.machine.play_music_card))
                self.assertEqual(self.machine.state, 'StatePlay')
                self.play.assert_called_with('Pop60s')
                self.assertEqual(self.machine.prev_state, state)
                sounds = [c for c in self.recorder.calls if c[0] == 'sound']
                expected = [('sound', self.ns['SOUND_PLAY'])]
                self.assertEqual(sounds, expected)
                self.assertIn(('led', 'turn_off_all_leds'), self.recorder.calls)
                self.assertIn(('led', 'turn_on_led', self.ns['LED_PLAY']), self.recorder.calls)
                self.assertFalse(any(c[0] == 'play' for c in self.recorder.calls))

    def test_two_cards_each_confirm_new_start_in_play(self):
        self.machine.state = 'StatePlay'
        second = {'event': 'CARD', 'code': 'oradio:v1:card:theme:jaren_zeventig',
                  'card': {'playlist': 'Pop70s'}}
        for event in (EVENT, second):
            self.assertTrue(handle_card_event(event, self.machine.play_music_card))
        self.assertEqual([call.args for call in self.play.call_args_list],
                         [('Pop60s',), ('Pop70s',)])
        sounds = [c for c in self.recorder.calls if c[0] == 'sound']
        self.assertEqual(sounds, [('sound', self.ns['SOUND_PLAY'])] * 2)
        self.assertEqual(self.machine.state, 'StatePlay')
        handle_card_event({'event': 'REMOVED'}, self.machine.play_music_card)
        self.assertEqual([c for c in self.recorder.calls if c[0] == 'sound'], sounds)

    def test_missing_playlist_preserves_state_and_leds(self):
        self.machine.state = 'StatePreset2'
        self.play.return_value = False
        self.assertFalse(handle_card_event(EVENT, self.machine.play_music_card))
        self.assertEqual(self.machine.state, 'StatePreset2')
        self.assertEqual(self.recorder.calls, [])

    def test_error_and_missing_usb_block_play(self):
        self.machine.state = 'StateError'
        self.assertFalse(handle_card_event(EVENT, self.machine.play_music_card))
        self.machine.state = 'StateIdle'
        self.ns['usb_present'].clear()
        self.assertFalse(handle_card_event(EVENT, self.machine.play_music_card))
        self.play.assert_not_called()

    def test_removal_unknown_invalid_do_nothing(self):
        for event in ({'event':'REMOVED'}, {'event':'UNKNOWN'}, {}, None,
                      {**EVENT, 'card': {'playlist': ''}}):
            self.assertFalse(handle_card_event(event, self.play))
        self.play.assert_not_called()

    def test_old_worker_cannot_override_card(self):
        old_generation = self.machine._generation
        self.machine.state = 'StatePreset1'
        self.assertTrue(handle_card_event(EVENT, self.machine.play_music_card))
        self.machine.run_state_method('StatePreset1', old_generation)
        self.assertEqual(self.machine.state, 'StatePlay')
        self.assertFalse(any(c[0] == 'play' for c in self.recorder.calls))

class PlaylistTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((ROOT/'Main/mpd_control.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=='MPDControl')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name=='play_playlist')
        method.decorator_list=[]
        namespace={}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'mpd_control.py', 'exec'), namespace)
        self.method=namespace['play_playlist']
        self.target=Mock()
        self.target._library_ready=threading.Event()
        self.target._library_ready.set()
        self.target._playback_intent=0
        self.target._disk_directories.return_value=[]
        self.responses={'listplaylists':[{'playlist':'Pop60s'}],
                        'listplaylistinfo':[{'file':'Music/a.mp3'},{'file':'Music/b.mp3'}],
                        'status':{'state':'play'}}
        self.target._execute.side_effect=lambda command,*args:self.responses.get(command)

    def test_load_order_repeat_and_first_track(self):
        self.assertTrue(self.method(self.target,'Pop60s'))
        self.assertEqual([c.args for c in self.target._execute.call_args_list], [
            ('listplaylists',), ('listplaylistinfo','Pop60s'), ('clear',),
            ('add','Music/a.mp3'), ('add','Music/b.mp3'), ('random',0),
            ('repeat',1), ('play',0), ('status',)])

    def test_directory_is_added_recursively_and_shuffled(self):
        self.responses['listplaylists'] = []
        self.target._disk_directories.return_value = ['Pop60s', 'Pop70s']
        self.responses['count'] = {'songs': '142'}
        for directory in ('Pop60s', 'Pop70s'):
            with self.subTest(directory=directory):
                self.target._execute.reset_mock()
                self.assertTrue(self.method(self.target, directory))
                self.assertEqual([c.args for c in self.target._execute.call_args_list], [
                    ('listplaylists',), ('count', 'base', directory),
                    ('clear',), ('add', directory), ('shuffle',),
                    ('random', 0), ('repeat', 1), ('play', 0), ('status',)])

    def test_empty_unscanned_or_bad_directory_count_preserves_queue(self):
        self.responses['listplaylists'] = []
        self.target._disk_directories.return_value = ['Pop60s']
        for count in ({'songs': '0'}, {}, None, {'songs': 'invalid'}):
            with self.subTest(count=count):
                self.responses['count'] = count
                self.target._execute.reset_mock()
                self.assertFalse(self.method(self.target, 'Pop60s'))
                self.assertNotIn('clear', [c.args[0] for c in self.target._execute.call_args_list])

    def test_playlist_precedes_same_named_directory(self):
        self.target._disk_directories.return_value = ['Pop60s']
        self.assertTrue(self.method(self.target, 'Pop60s'))
        self.target._disk_directories.assert_not_called()
        self.assertNotIn('shuffle', [c.args[0] for c in self.target._execute.call_args_list])

    def test_missing_empty_unready_preserve_queue(self):
        for case in ('missing','empty','unready'):
            with self.subTest(case=case):
                self.setUp()
                if case=='missing': self.responses['listplaylists']=[]
                if case=='empty': self.responses['listplaylistinfo']=[]
                if case=='unready': self.target._library_ready.clear()
                self.assertFalse(self.method(self.target,'Pop60s'))
                self.assertNotIn('clear',[c.args[0] for c in self.target._execute.call_args_list])

if __name__=='__main__':
    unittest.main()
