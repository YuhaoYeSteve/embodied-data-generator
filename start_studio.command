#!/bin/bash
set -e
cd "$(dirname "$0")"
exec python3 -B studio_server.py --open
