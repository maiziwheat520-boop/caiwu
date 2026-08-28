#!/bin/sh
set -eu

socket_path=/run/ledgerbridge-internal/core.sock
if [ "$socket_path" != /run/ledgerbridge-internal/core.sock ]; then
    echo "internal reader socket path is invalid" >&2
    exit 1
fi

rm -f -- "$socket_path"
uvicorn ledgerbridge.main:app --uds "$socket_path" &
child=$!
trap 'kill "$child" 2>/dev/null || true' INT TERM EXIT

attempt=0
while [ ! -S "$socket_path" ]; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 100 ]; then
        echo "internal reader socket was not created" >&2
        exit 1
    fi
    if ! kill -0 "$child" 2>/dev/null; then
        wait "$child"
    fi
    sleep 0.05
done
chmod 0660 "$socket_path"
wait "$child"
