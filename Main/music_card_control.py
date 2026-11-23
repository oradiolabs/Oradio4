"""Adapter between validated NFC events and Oradio's playlist action.

No hardware imports or background threads. The future NFC process bridge can
publish CommandMessage(source="NFC", message="CARD", data=event).
"""
import re

_CARD_CODE = re.compile(r'oradio:v1:card:(moment|theme|mood|artist|function):[a-z0-9]+(?:_[a-z0-9]+)*\Z')


def handle_card_event(event, play_playlist):
    """Ignore removal/unknown/malformed events; dispatch a resolved CARD once.

    Accept only trusted events resolved against music_cards.json by NFCService.
    No deduplication here: NFCService decides when a new presentation starts.
    """
    if not isinstance(event, dict) or event.get('event') != 'CARD':
        return False
    code, card = event.get('code'), event.get('card')
    if not isinstance(code, str) or not _CARD_CODE.fullmatch(code) or not isinstance(card, dict):
        return False
    playlist = card.get('playlist')
    if not isinstance(playlist, str) or not playlist.strip():
        return False
    return play_playlist(playlist)
