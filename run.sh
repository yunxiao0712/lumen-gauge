#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
export GDK_BACKEND=x11,wayland
exec python3 hardware_widget.py
