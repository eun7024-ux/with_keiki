#!/bin/sh
cd "$(dirname "$0")"
(sleep 1; open http://127.0.0.1:8700 2>/dev/null || xdg-open http://127.0.0.1:8700 2>/dev/null) &
exec python3 server.py "$@"
