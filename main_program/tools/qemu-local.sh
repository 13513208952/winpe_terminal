#!/bin/sh
# Place this launcher in <asset-root>/tools/qemu-system-x86_64.
# Only selects the private tool prefix; it does not bypass the CLI's root gate.
set -eu
prefix=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/ubuntu
export LD_LIBRARY_PATH="$prefix/usr/lib/x86_64-linux-gnu"
export QEMU_MODULE_DIR="$prefix/usr/lib/x86_64-linux-gnu/qemu"
exec "$prefix/usr/bin/qemu-system-x86_64" -L "$prefix/usr/share/qemu" -L "$prefix/usr/share/seabios" -bios "$prefix/usr/share/seabios/bios-256k.bin" "$@"
