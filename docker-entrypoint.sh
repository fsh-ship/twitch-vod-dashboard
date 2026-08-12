#!/bin/sh
set -eu

umask 077

puid="${PUID:-1000}"
pgid="${PGID:-1000}"

case "$puid" in
    ''|*[!0-9]*)
        echo "PUID and PGID must be positive numeric IDs." >&2
        exit 1
        ;;
esac

case "$pgid" in
    ''|*[!0-9]*)
        echo "PUID and PGID must be positive numeric IDs." >&2
        exit 1
        ;;
esac

if [ "$puid" -eq 0 ] || [ "$pgid" -eq 0 ]; then
    echo "PUID and PGID must not be 0; the application does not run as root." >&2
    exit 1
fi

groupmod --non-unique --gid "$pgid" app
usermod --non-unique --uid "$puid" --gid "$pgid" app

mkdir -p /data /downloads
chown "$puid:$pgid" /data /downloads
chmod 0700 /data

for private_file in \
    /data/dashboard-settings.json \
    /data/streamer.txt \
    /data/archive.txt \
    /data/twitch-cookies.txt \
    /data/client_secret.json \
    /data/youtube-token.json \
    /data/dashboard.log \
    /data/dashboard.log.1
do
    if [ -f "$private_file" ]; then
        chown "$puid:$pgid" "$private_file"
        chmod 0600 "$private_file"
    fi
done

exec gosu "$puid:$pgid" "$@"
