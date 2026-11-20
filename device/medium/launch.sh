#!/bin/sh
# MAS portable device: the one command the host's owner runs (D-021, D-034). EMULATED in v0.
#   sh /media/mas/launch.sh --root <host folder> --state <device log folder> --target <this host's name>
# Nothing is installed and nothing is written to this medium: the medium's own Python runs isolated (-I) and writes no bytecode (-B).
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$HERE/runtime/python/bin/python3.13" -I -B "$HERE/runtime/launch.py" "$@"
