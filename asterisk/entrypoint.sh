#!/bin/sh
set -eu

# Do not overwrite mounted/configured credentials. Generate a local ARI config
# only when placeholders are supplied through environment variables.
if [ -n "${ARI_USER:-}" ] && [ -n "${ARI_PASSWORD:-}" ]; then
  cat > /etc/asterisk/ari.conf <<EOF
[general]
enabled = yes
pretty = yes
allowed_origins = *

[${ARI_USER}]
type = user
read_only = no
password = ${ARI_PASSWORD}
EOF
fi

# Asterisk must own its runtime directories.
chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk 2>/dev/null || true

exec asterisk -f -U asterisk -G asterisk -vvv
