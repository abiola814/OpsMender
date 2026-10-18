"""Generate the one-time setup script a user runs on each server to enable metrics."""

from __future__ import annotations

from importlib import resources

PROBE_PATH = "/usr/local/lib/opsmender/probe"


def probe_source() -> str:
    return resources.files("opsmender.metrics").joinpath("probe.sh").read_text()


def install_script(public_key: str) -> str:
    key = public_key.strip()
    if "\n" in key or "'" in key or not key.startswith("ssh-"):
        raise ValueError("unexpected public key format")
    options = f'command="{PROBE_PATH}",restrict,no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding'
    return f"""#!/bin/sh
# OpsMender metrics setup. Review it, then run it once as root on the server:
#   sudo sh opsmender-setup.sh
#
# What it does:
#   1. creates a locked-down system user "opsmender" (no password, no interactive shell use)
#   2. installs a read-only probe script at {PROBE_PATH}
#   3. lets that user run only the probe as root (needed to read container names via docker)
#   4. authorizes OpsMender's SSH key for that user, restricted to running the probe
#
# To undo:  userdel -r opsmender; rm -rf /usr/local/lib/opsmender /etc/sudoers.d/opsmender
set -eu

if [ "$(id -u)" != "0" ]; then echo "Run this as root (sudo sh $0)." >&2; exit 1; fi

if ! id opsmender >/dev/null 2>&1; then
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/sh opsmender
  else
    adduser -S -D -h /home/opsmender -s /bin/sh opsmender
  fi
fi
# "*" = no password login possible, but the account is not "locked", so key auth works.
usermod -p '*' opsmender 2>/dev/null || passwd -u opsmender >/dev/null 2>&1 || true

install -d -m 0755 -o root -g root /usr/local/lib/opsmender
cat > {PROBE_PATH} <<'OPSMENDER_PROBE_EOF'
{probe_source().rstrip()}
OPSMENDER_PROBE_EOF
chown root:root {PROBE_PATH}
chmod 0755 {PROBE_PATH}

if command -v sudo >/dev/null 2>&1; then
  echo 'opsmender ALL=(root) NOPASSWD: {PROBE_PATH}' > /etc/sudoers.d/opsmender
  chmod 0440 /etc/sudoers.d/opsmender
  if command -v visudo >/dev/null 2>&1; then visudo -cf /etc/sudoers.d/opsmender >/dev/null; fi
else
  echo "warning: sudo is not installed; host metrics will work but container metrics will not." >&2
fi

home=$(getent passwd opsmender 2>/dev/null | cut -d: -f6)
[ -n "$home" ] || home=/home/opsmender
install -d -m 0700 -o opsmender "$home/.ssh"
echo '{options} {key}' > "$home/.ssh/authorized_keys"
chown opsmender "$home/.ssh/authorized_keys"
chmod 0600 "$home/.ssh/authorized_keys"

echo "OpsMender probe installed. Go back to OpsMender and click 'Test connection'."
"""
