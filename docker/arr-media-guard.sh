#!/bin/sh
# The image's entry point and its arr-media-guard command. As root it prepares /config, then runs the script as
# PUID:PGID, so every file it writes keeps the owner of the media. docker exec runs through it too.
set -e
if [ "$(id -u)" = 0 ]; then
    mkdir -p /config/state/queue /config/state/claimed /config/state/alerts /config/logs
    # The first start writes the env file: every key of the example, then the Docker settings, which win.
    if [ ! -e /config/arr-media-guard.env ]; then
        cat /opt/arr-media-guard/examples/arr-media-guard.env /opt/arr-media-guard/docker/arr-media-guard.env > /config/arr-media-guard.env
        chmod 0640 /config/arr-media-guard.env
    fi
    [ -e /config/policy.json ] || cp /opt/arr-media-guard/examples/policy.json /config/policy.json
    chown -R "${PUID}:${PGID}" /config
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups /opt/arr-media-guard/arr-media-guard "$@"
fi
exec /opt/arr-media-guard/arr-media-guard "$@"
