#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Installs mkvtoolnix at one exact version from MKVToolNix's own Debian repository, as root, after apt-get update.
# docker/Dockerfile and the test workflow both run it, so the tests use the version the image ships. Debian 13 itself
# ships mkvtoolnix 92. The install fails when the repository's key file holds any other primary key than KEY_FPR.
# README.md "Install on a host" installs the same VERSION with the same key, and a test holds the two equal.
set -eu
VERSION=102.0-0~debian13bunkus01
KEY_FPR=D9199745B0545F2E8197062B0F92290A445B9007   # Moritz Bunkus, who signs the repository
KEYRING=/usr/share/keyrings/mkvtoolnix.gpg
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends ca-certificates curl gpg
curl -fsSL -o "$KEYRING" https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg
home=$(mktemp -d)
got=$(GNUPGHOME=$home gpg --show-keys --with-colons "$KEYRING" | awk -F: '$1 == "pub" {p = 1; next} $1 == "fpr" && p {print $10; p = 0}')
rm -rf "$home"
[ "$got" = "$KEY_FPR" ] || { echo "the mkvtoolnix key file holds $got, not $KEY_FPR" >&2; exit 1; }
echo "deb [signed-by=$KEYRING] https://mkvtoolnix.download/debian/ trixie main" > /etc/apt/sources.list.d/mkvtoolnix.list
apt-get update
apt-get install -y --no-install-recommends "mkvtoolnix=$VERSION"
apt-get purge -y --auto-remove curl gpg
