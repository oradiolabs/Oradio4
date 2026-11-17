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
# Debian's mpd links 328 shared libraries: Samba (77), the whole ffmpeg stack,
# ICU, PulseAudio, PipeWire, JACK, tracker and game-music decoders, and more.
# Loading them costs ~4.1 s on a cold Pi 3A+ and ~2.0 s of CPU even when warm,
# before MPD reads its first line of config. During boot that load competes
# with oradio.service for the SD card, and MPD was ready only at ~16 s.
#
# Oradio plays MP3 only: files from the USB stick and plain HTTP(S) MP3 web
# radio streams. System sounds are WAV, played by aplay, not by MPD. So this
# build keeps exactly:
#   output   alsa
#   input    file (built in), curl
#   decoder  mpg123 (and pcm, built in)
#   tags     id3tag
#   database simple, gzip-compressed (zlib)
#   search   Unicode case folding (icu)
#   playlist m3u, extm3u, pls (built in, no dependencies)
#   other    systemd notify/socket activation, tcp, unix socket, ipv6
# Everything else is switched off with -Dauto_features=disabled plus explicit
# 'false' for the boolean options.
#
# Known differences from Debian's build that matter to Oradio:
#  - No SQLite: 'sticker_file' in mpd.conf is ignored. Oradio uses no stickers.
#  - Decoder blocks in mpd.conf for plugins that are not built (hybrid_dsd,
#    wildmidi) are ignored silently by MPD; mpd.conf needs no change.
#
# HOW
# Rebuilds Debian's own source package, so the result keeps Debian's systemd
# units, mpd user, paths, conffiles and maintainer scripts. Only debian/rules
# is replaced (the feature list) and a local changelog entry is added, giving a
# version such as 0.24.4-1+oradio1: newer than Debian's same upstream release.
# oradio_install.sh installs it and puts mpd on hold, so pkg-helper.sh leaves it
# alone.
#
# WHERE
# Runs as root in a debian:trixie arm64 container: see
# .github/workflows/build-mpd-minimal.yml. Building on a Pi 3A+ itself is not
# practical (hours, and 512 MB RAM is not enough for the C++ compile).
#
# Usage: build-mpd-minimal.sh
#   MPD_VERSION   Debian source version to rebuild, e.g. 0.24.4-1 (default:
#                 the current one in trixie)
#   LOCAL_SUFFIX  Appended to the Debian version (default: +oradio1)
#   OUT_DIR       Where the .deb (named <tag>_arm64.deb), SHA256SUMS and
#                 build-info.txt go
#                 (default: ./dist)
#   --print-meson-options  Print the meson options and exit (for testing)

set -euo pipefail

# ---------------------------------------------------------------------------
# The feature set. Everything not named here is disabled by auto_features.
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
	-Did3tag=enabled
	# The simple database (/var/lib/mpd/tag_cache, and install_resources/
	# mpd.database) is gzip-compressed. Without zlib MPD reports "Database
	# corrupted", discards it and rescans the whole USB stick.
	-Dzlib=enabled
	# Unicode case folding for 'search': "beyonce" finds "Beyoncé", as with
	# Debian's build. Measured on a Pi 3A+: 0.07 s cold, 0.01 s warm.
	-Dicu=enabled
	-Dipv6=enabled
	-Dtcp=true
	-Dlocal_socket=true
	-Ddatabase=true
	-Ddaemon=true

	# Kept so Debian's packaging (dh --with sphinxdoc, doc-base, manpages)
	# works unchanged. Build-time only: adds nothing MPD loads at runtime.
	-Ddocumentation=enabled

	# Boolean options default to true; auto_features does not touch them
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

	# Combo options: auto_features does not touch these either
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
LOCAL_SUFFIX="${LOCAL_SUFFIX:-+oradio1}"
OUT_DIR="$(realpath -m "${OUT_DIR:-dist}")"
export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# Source and build dependencies
# ---------------------------------------------------------------------------
# The container ships binary sources only
if [[ -f /etc/apt/sources.list.d/debian.sources ]]; then
	sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources
fi
apt-get update -q
apt-get install -y -q --no-install-recommends dpkg-dev ca-certificates binutils

WORK="$(mktemp -d)"
cd "$WORK"
apt-get source -q "mpd${MPD_VERSION:+=$MPD_VERSION}"
cd mpd-*/

# Debian's full build-dependency list: more than this build needs, but it keeps
# the script free of a second list to maintain, and it only costs CI time.
apt-get build-dep -y -q ./

DEBIAN_VERSION="$(dpkg-parsechangelog -S Version)"
NEW_VERSION="${DEBIAN_VERSION}${LOCAL_SUFFIX}"
echo "Rebuilding mpd ${DEBIAN_VERSION} as ${NEW_VERSION}"

# ---------------------------------------------------------------------------
# debian/rules: Debian's, with the configure step replaced
# ---------------------------------------------------------------------------
# Debian's other overrides are kept as they are. If a future Debian revision
# adds overrides this script does not know about, the build will still
# succeed but without them: check 'diff' in build-info.txt when bumping.
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

# Fail loudly if Debian's rules grew overrides this script would silently drop
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
	printf '  * Minimal build for Oradio: alsa, curl, mpg123, id3tag, systemd.\n'
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

# Release tag and asset name without '+', ':' or '~': those are awkward in tags,
# URLs and constants.env (whose values may not contain '+'). dpkg reads the
# version from inside the package, not from the file name.
TAG="mpd-${NEW_VERSION//[+:~]/-}"
DEB_NAME="${TAG}_${DEB_ARCH}.deb"
cp "$DEB_BUILT" "$OUT_DIR/${DEB_NAME}"

# Install it here, which also checks its dependencies resolve on trixie
apt-get install -y -q "$OUT_DIR/${DEB_NAME}"

needed="$(readelf -d /usr/bin/mpd | grep -c NEEDED)"
total="$(ldd /usr/bin/mpd | grep -c '=>')"
(cd "$OUT_DIR" && sha256sum "${DEB_NAME}" >SHA256SUMS)
{
	echo "Package:        mpd ${NEW_VERSION} (${DEB_ARCH})"
	echo "Rebuilt from:   mpd ${DEBIAN_VERSION} (Debian trixie source)"
	echo "Release tag:    ${TAG}"
	echo "Direct libs:    ${needed}"
	echo "Total libs:     ${total} (Debian's build: 328)"
	echo
	echo "For constants.env:"
	echo "  MPD_DEB_TAG=${TAG}"
	echo "  MPD_DEB_SHA256=$(cut -d' ' -f1 "$OUT_DIR/SHA256SUMS")"
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

# For the workflow: the version and file name of what was built
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
	{
		echo "version=${NEW_VERSION}"
		echo "deb=${DEB_NAME}"
		echo "tag=${TAG}"
	} >>"$GITHUB_OUTPUT"
fi
