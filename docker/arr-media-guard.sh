#!/bin/sh
# The image's entry point and its two commands. As root it prepares /config, then runs the command as PUID:PGID, so
# every file it writes keeps the owner of the media. docker exec runs through it too. It runs the subtitle hunter
# when it runs as arr-media-guard-subhunt, or when that name is its first argument, as in a one-off container.
set -e
cmd=arr-media-guard
if [ "$(basename "$0")" = arr-media-guard-subhunt ]; then
    cmd=arr-media-guard-subhunt
elif [ "${1:-}" = arr-media-guard-subhunt ]; then
    cmd=arr-media-guard-subhunt
    shift
fi
if [ "$(id -u)" = 0 ]; then
    # /config/state is the state store's own mount, a named volume that Docker creates owned by root. Without that
    # mount, mkdir creates it in /config. chown -R below goes into a mount too, so it gives either one to PUID:PGID.
    mkdir -p /config/state /config/logs
    # The first start writes the env file: every key of the example, then the Docker settings, which win.
    if [ ! -e /config/arr-media-guard.env ]; then
        cat /opt/arr-media-guard/examples/arr-media-guard.env /opt/arr-media-guard/docker/arr-media-guard.env > /config/arr-media-guard.env
        chmod 0640 /config/arr-media-guard.env
    fi
    [ -e /config/policy.json ] || cp /opt/arr-media-guard/examples/policy.json /config/policy.json
    chown -R "${PUID}:${PGID}" /config
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "/opt/arr-media-guard/$cmd" "$@"
fi
exec "/opt/arr-media-guard/$cmd" "$@"
