#!/usr/bin/env sh
set -eu

USER_NAME="$(id -un)"
TARGET_USER="${SUDO_USER:-$USER_NAME}"
TEE_PATH="$(command -v tee)"
RULE_FILE="/etc/sudoers.d/lumen-gauge-drop-caches"
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./install_memory_cleanup_sudoers.sh" >&2
  exit 1
fi

if [ -z "$TEE_PATH" ]; then
  echo "tee not found" >&2
  exit 1
fi

printf '%s ALL=(root) NOPASSWD: %s /proc/sys/vm/drop_caches\n' "$TARGET_USER" "$TEE_PATH" > "$TMP_FILE"
chmod 0440 "$TMP_FILE"

if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$TMP_FILE" >/dev/null
fi

mv "$TMP_FILE" "$RULE_FILE"
echo "Installed $RULE_FILE"
