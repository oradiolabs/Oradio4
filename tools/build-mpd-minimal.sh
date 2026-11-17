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
# @Purpose:       Build a minimal MPD .deb for Oradio
#
# WHY
# Debian's mpd links 328 shared libraries: Samba, the whole ffmpeg stack,
# PulseAudio, PipeWire, JACK, tracker and game-music decoders, and more. Loading
# them takes ~4.1 s on a cold Pi 3A+ (~2.0 s CPU even when warm), before MPD
# reads its config, and during boot that competes with oradio.service for the
# SD card.
#
# Oradio plays only MP3 through MPD: files on the USB stick and HTTP(S) MP3 web
# radio streams. System sounds are WAV and go through aplay. This build keeps:
#   output    alsa
#   input     file, curl
#   decoder   mpg123 (files), mad (MP3 streams by MIME type), pcm
#   tags      id3tag
#   database  simple, gzip-compressed (zlib)
#   search    Unicode case folding (icu)
#   playlist  m3u, extm3u, pls
#   other     systemd notify and socket activation, tcp, unix socket, ipv6
# 47 libraries in total. Everything else is off.
#
# mpd.conf needs no change: MPD ignores 'sticker_file' without SQLite (Oradio
# uses no stickers) and blocks for plugins that are not built (hybrid_dsd,
# wildmidi).
#
# HOW
# Rebuilds Debian's own mpd source package with only debian/rules replaced and
# a changelog entry added. Units, mpd user, paths, conffiles and maintainer
# scripts stay Debian's. oradio_install.sh installs the result from
# install_resources/ and holds it, so pkg-helper.sh leaves it alone.
#
# VERSION
# <Debian version>+oradio.<UTC build date>, e.g. 0.24.4-1+oradio.20261117.
# Newer than Debian's build of the same release and than every earlier build,
# so devices pick up a rebuild without anything bumped by hand. A '.' before
# the date: Debian splits a version at its last '-'. No digit straight after
# "oradio": "oradio1" reads as the product Oradio 1.
# File name: the version with '+' as '-', e.g.
# mpd-0.24.4-1-oradio.20261117_arm64.deb. The part before '_arm64.deb' is the
# tag that goes into MPD_DEB_TAG in constants.env.
#
# TEST
# The installed package must play an MP3 stream with and without .mp3 in its
# URL and an MP3 file: see tools/test-mpd-minimal.sh. A build that fails it
# stops here, so there is no artifact to put in install_resources/.
#
# WHERE
# As root in a debian:trixie arm64 container, see
# .github/workflows/build-mpd-minimal.yml. Not on a Pi 3A+: hours of compiling,
# and 512 MB RAM is not enough.
#
# Usage: build-mpd-minimal.sh [--print-meson-options]
#   MPD_VERSION   Debian source version to rebuild, e.g. 0.24.4-1
#                 (default: the current one in trixie)
#   OUT_DIR       Where the .deb and build-info.txt go (default: ./dist)
#   --print-meson-options  Print the meson options and exit

set -euo pipefail

# ---------------------------------------------------------------------------
# Feature set: auto_features switches off every feature not named here
# ---------------------------------------------------------------------------
MESON_OPTIONS=(
	-Dauto_features=disabled
	-Db_ndebug=true
	-Dtest=false

	# Wanted
	-Dsystemd=enabled
	-Dalsa=enabled
	-Dcurl=enabled
	-Dmpg123=enabled
	# Web radio. MPD picks a decoder for a stream by MIME type, then by the
	# URL's suffix, then falls back to "ffmpeg" or "mad". mpg123 registers the
	# suffix .mp3 only, so a stream such as icecast.omroep.nl/radio2-bb-mp3
	# (audio/mpeg, no .mp3) got "no decoder plugin". mad registers audio/mpeg
	# and is the fallback. One small library (libmad0).
	-Dmad=enabled
	-Did3tag=enabled
	# tag_cache and install_resources/mpd.database are gzip-compressed.
	# Without zlib MPD calls them corrupted and rescans the whole stick.
	-Dzlib=enabled
	# 'search' finds "Beyoncé" for "beyonce". Costs 0.07 s cold on a Pi 3A+.
	-Dicu=enabled
	-Dipv6=enabled
	-Dtcp=true
	-Dlocal_socket=true
	-Ddatabase=true
	-Ddaemon=true

	# Needed by Debian's packaging (sphinxdoc, doc-base, man pages).
	# Nothing of it is loaded at runtime.
	-Ddocumentation=enabled

	# Booleans default to true and are not covered by auto_features
	-Dinotify=false
	-Ddsd=false
	-Dneighbor=false
	-Dcue=false
	-Dwave_encoder=false
	-Dfifo=false
	-Dhttpd=false
	-Dpipe=false
	-Drecorder=false
	-Dsnapcast=false

	# Neither are combo options
	-Dupnp=disabled
	-Dzeroconf=disabled
)

if [[ "${1:-}" == "--print-meson-options" ]]; then
	printf '%s\n' "${MESON_OPTIONS[@]}"
	exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
	echo "Run as root inside a debian:trixie container" >&2
	exit 1
fi

MPD_VERSION="${MPD_VERSION:-}"
BUILD_DATE="$(date -u +%Y%m%d)"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$(realpath -m "${OUT_DIR:-dist}")"
export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# Source and build dependencies
# ---------------------------------------------------------------------------
# The container image has no deb-src entries
if [[ -f /etc/apt/sources.list.d/debian.sources ]]; then
	sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
fi
apt-get update -q
apt-get install -y -q --no-install-recommends dpkg-dev ca-certificates binutils

WORK="$(mktemp -d)"
cd "$WORK"
apt-get source -q "mpd${MPD_VERSION:+=$MPD_VERSION}"
cd mpd-*/

# All of Debian's build dependencies: more than needed, but no second list to
# maintain, and it only costs CI time.
apt-get build-dep -y -q ./

DEBIAN_VERSION="$(dpkg-parsechangelog -S Version)"
NEW_VERSION="${DEBIAN_VERSION}+oradio.${BUILD_DATE}"
echo "Rebuilding mpd ${DEBIAN_VERSION} as ${NEW_VERSION}"

# ---------------------------------------------------------------------------
# debian/rules: Debian's overrides, with our feature set for configure
# ---------------------------------------------------------------------------
cp debian/rules debian/rules.debian
{
	echo '#!/usr/bin/make -f'
	echo '# GENERATED by tools/build-mpd-minimal.sh (Oradio). Original: rules.debian'
	echo 'export DEB_BUILD_MAINT_OPTIONS = hardening=+all'
	echo
	echo '%:'
	printf '\tdh $@ --with sphinxdoc\n'
	echo
	echo 'override_dh_auto_configure:'
	printf '\tdh_auto_configure -- %s\n' "${MESON_OPTIONS[*]}"
	echo
	echo 'override_dh_auto_test:'
	echo
	echo 'override_dh_installchangelogs:'
	printf '\tdh_installchangelogs NEWS\n'
	printf '\trm -f debian/mpd/usr/share/doc/mpd/COPYING debian/mpd/usr/share/doc/mpd/NEWS debian/mpd/usr/share/doc/mpd/README.md\n'
	echo
	echo 'override_dh_installsystemd:'
	printf '\tdh_installsystemd --no-enable --no-start\n'
	echo
	echo 'override_dh_installsystemduser:'
	printf '\tdh_installsystemduser --no-enable\n'
} >debian/rules
chmod 0755 debian/rules

# Stop if a newer Debian revision has overrides this script does not reproduce
known='override_dh_auto_configure|override_dh_installchangelogs|override_dh_installsystemd|override_dh_installsystemduser'
unknown="$(grep -oE '^override_[a-z_]+' debian/rules.debian | grep -vxE "$known" || true)"
if [[ -n "$unknown" ]]; then
	echo "Debian's debian/rules has overrides this script does not reproduce:" >&2
	echo "$unknown" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# Changelog entry for the local version
# ---------------------------------------------------------------------------
{
	printf 'mpd (%s) trixie; urgency=medium\n\n' "$NEW_VERSION"
	printf '  * Minimal build for Oradio: alsa, curl, mpg123, mad, id3tag, zlib, icu,\n'
	printf '    systemd.\n'
	printf '    Built by tools/build-mpd-minimal.sh in oradiolabs/Oradio4.\n\n'
	printf ' -- Stichting Oradio <info@stichtingoradio.nl>  %s\n\n' "$(date -R)"
	cat debian/changelog
} >debian/changelog.new
mv debian/changelog.new debian/changelog

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
DEB_BUILD_OPTIONS="nocheck parallel=$(nproc)" dpkg-buildpackage -b -uc -us

# ---------------------------------------------------------------------------
# Collect and check
# ---------------------------------------------------------------------------
mkdir -p "$OUT_DIR"
DEB_ARCH="$(dpkg-architecture -qDEB_HOST_ARCH)"
DEB_BUILT="../mpd_${NEW_VERSION#*:}_${DEB_ARCH}.deb"

# See VERSION above. constants.env values may not contain '+'.
TAG="mpd-${NEW_VERSION//[+:~]/-}"
DEB_NAME="${TAG}_${DEB_ARCH}.deb"
cp "$DEB_BUILT" "$OUT_DIR/${DEB_NAME}"

# Installing it here also checks that its dependencies resolve on trixie
apt-get install -y -q "$OUT_DIR/${DEB_NAME}"

# Play a web radio stream, an MP3 URL and a file with it, and stop the build if
# one fails. tools/test-mpd-minimal.sh is next to this script.
apt-get install -y -q --no-install-recommends lame python3
PLAYBACK_RESULT="$(bash "$SCRIPT_DIR/test-mpd-minimal.sh" /usr/bin/mpd)" || {
	echo "$PLAYBACK_RESULT"
	echo "Playback test failed: not keeping this build" >&2
	exit 1
}
echo "$PLAYBACK_RESULT"

needed="$(readelf -d /usr/bin/mpd | grep -c NEEDED)"
total="$(ldd /usr/bin/mpd | grep -c '=>')"
{
	echo "Package:        mpd ${NEW_VERSION} (${DEB_ARCH})"
	echo "Rebuilt from:   mpd ${DEBIAN_VERSION} (Debian trixie source)"
	echo "Tag:            ${TAG}"
	echo "Direct libs:    ${needed}"
	echo "Total libs:     ${total} (Debian's build: 328)"
	echo
	echo "To use: copy ${DEB_NAME} to install_resources/ and set in constants.env:"
	echo "  MPD_DEB_TAG=${TAG}"
	echo
	echo "$PLAYBACK_RESULT"
	echo
	echo "Meson options:"
	printf '  %s\n' "${MESON_OPTIONS[@]}"
	echo
	echo "Depends:"
	dpkg-deb -f "$OUT_DIR/${DEB_NAME}" Depends | tr ',' '\n' | sed 's/^ */  /'
	echo
	mpd --version
} >"$OUT_DIR/build-info.txt"

cat "$OUT_DIR/build-info.txt"

# The workflow names the artifact after the tag
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
	echo "tag=${TAG}" >>"$GITHUB_OUTPUT"
fi
