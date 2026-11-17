#!/usr/bin/env bash
#
#  ####   #####     ##    #####      #     ####
# #    #  #    #   #  #   #    #     #    #    #
# #    #  #    #  #    #  #    #     #    #    #
# #    #  #####   ######  #    #     #    #    #
# #    #  #   #   #    #  #    #     #    #    #
#  ####   #    #  #    #  #####      #     ####
#
# Created on November 17, 2026
# @author:        Henk Stevens & Olaf Mastenbroek & Onno Janssen
# @copyright:     Stichting Oradio
# @license:       GNU General Public License (GPL)
# @organization:  Stichting Oradio
# @version:       1
# @email:         info@stichtingoradio.nl
# @status:        Development
# @Purpose:       Playback test for the minimal MPD build
#
# Plays what the Oradio plays, on a private MPD instance with a null output:
#   - an MP3 stream whose URL has no .mp3, served as audio/mpeg, like
#     https://icecast.omroep.nl/radio2-bb-mp3
#   - an MP3 stream whose URL ends in .mp3, like the streamtheworld stations
#   - an MP3 file in the music directory, like the USB stick
# Each must reach "state: play" without an error. Exits 1 if one does not.
#
# Starting MPD is not enough: the first minimal build started fine and could
# not play the first kind of stream ("no decoder plugin"), because MPD picks a
# stream decoder by MIME type and mpg123 registers none.
#
# Run by tools/build-mpd-minimal.sh on the freshly installed package. Needs
# lame (to make the test MP3) and python3 (for the HTTP server). Uses its own
# ports and directories, so it does not touch a running MPD.
#
# Usage: test-mpd-minimal.sh [path to mpd]    (default /usr/bin/mpd)

set -euo pipefail

readonly MPD="${1:-/usr/bin/mpd}"
readonly MPD_PORT=16600
readonly HTTP_PORT=18765

WORK="$(mktemp -d)"
MPD_PID=""
HTTP_PID=""

cleanup() {
	[[ -n "$MPD_PID" ]] && kill "$MPD_PID" 2>/dev/null || true
	[[ -n "$HTTP_PID" ]] && kill "$HTTP_PID" 2>/dev/null || true
	wait 2>/dev/null || true
	rm -rf "$WORK"
}
trap cleanup EXIT

mkdir -p "$WORK/music" "$WORK/playlists"

# Two seconds of silence: valid MP3 frames, which is all a decoder needs
head -c 352800 /dev/zero >"$WORK/silence.raw"
lame --quiet -r -s 44.1 --bitwidth 16 -m s "$WORK/silence.raw" "$WORK/music/test.mp3" 2>/dev/null

# Serves the test MP3 for every path, as audio/mpeg, the way Icecast does
cat >"$WORK/serve.py" <<EOF
import http.server, socketserver
DATA = open("$WORK/music/test.mp3", "rb").read()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(DATA)))
        self.end_headers()
        self.wfile.write(DATA)
    def log_message(self, *args):
        pass
socketserver.TCPServer.allow_reuse_address = True
socketserver.TCPServer(("127.0.0.1", $HTTP_PORT), Handler).serve_forever()
EOF
python3 "$WORK/serve.py" &
HTTP_PID=$!

cat >"$WORK/mpd.conf" <<EOF
music_directory     "$WORK/music"
playlist_directory  "$WORK/playlists"
db_file             "$WORK/tag_cache"
log_file            "$WORK/mpd.log"
bind_to_address     "127.0.0.1"
port                "$MPD_PORT"
log_level           "verbose"
audio_output {
	type "null"
	name "test"
}
EOF
# Its own messages go to mpd.log; the one about the missing database is expected
"$MPD" --no-daemon "$WORK/mpd.conf" >/dev/null 2>&1 &
MPD_PID=$!

# One command per connection; MPD answers and closes after 'close'
mpd_cmd() {
	local fd
	exec {fd}<>"/dev/tcp/127.0.0.1/$MPD_PORT" || return 1
	printf '%s\nclose\n' "$1" >&"$fd"
	timeout 3 cat <&"$fd" || true
	exec {fd}>&-
}

# Wait for MPD to answer, at most 10 s
for _ in $(seq 50); do
	mpd_cmd "ping" 2>/dev/null | grep -q '^OK' && break
	sleep 0.2
done

# Index the music directory, for the file test
mpd_cmd "update" >/dev/null
for _ in $(seq 50); do
	mpd_cmd "status" | grep -q '^updating_db:' || break
	sleep 0.2
done

failures=0

play_test() {
	local what="$1" uri="$2" status
	mpd_cmd "clear" >/dev/null
	mpd_cmd "add \"$uri\"" >/dev/null
	mpd_cmd "play" >/dev/null
	sleep 1
	status="$(mpd_cmd "status")"
	if grep -q '^state: play' <<<"$status" && ! grep -q '^error:' <<<"$status"; then
		echo "PASS  $what"
	else
		echo "FAIL  $what ($uri)"
		grep -E '^(state|error):' <<<"$status" | sed 's/^/        /'
		failures=$((failures + 1))
	fi
	mpd_cmd "stop" >/dev/null
	mpd_cmd "clearerror" >/dev/null
}

echo "Playback test of $("$MPD" --version | head -1)"
play_test "MP3 stream, no .mp3 in the URL" "http://127.0.0.1:$HTTP_PORT/radio2-bb-mp3"
play_test "MP3 stream, .mp3 in the URL"    "http://127.0.0.1:$HTTP_PORT/station.mp3"
play_test "MP3 file in the music directory" "test.mp3"

if (( failures > 0 )); then
	echo "MPD log, errors and decoder choices:"
	grep -E 'exception|decoder_thread|decoder:' "$WORK/mpd.log" | tail -n 10 | sed 's/^/        /'
	exit 1
fi
