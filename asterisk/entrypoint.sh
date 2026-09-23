#!/bin/sh
set -eu

# Asterisk container entrypoint.
# Static configuration is shipped in the image. Runtime credentials and
# database-managed objects are generated into the mounted dynamic directory.

: "${ASTERISK_EXTERNAL_ADDRESS:?ASTERISK_EXTERNAL_ADDRESS must be set}"
: "${ARI_USER:?ARI_USER must be set}"
: "${ARI_PASSWORD:?ARI_PASSWORD must be set}"
: "${AMI_USER:?AMI_USER must be set}"
: "${AMI_PASSWORD:?AMI_PASSWORD must be set}"

ASTERISK_CONFIG_DIR=/etc/asterisk
DYNAMIC_DIR=/etc/asterisk/dynamic

mkdir -p "$DYNAMIC_DIR" \
    /var/run/asterisk \
    /var/log/asterisk \
    /var/spool/asterisk \
    /var/lib/asterisk \
    /var/lib/asterisk/keys/keys

# Database-managed includes may start empty. The ARI worker renders them after
# Asterisk is healthy; bootstrap SIP objects would duplicate the rendered objects.
touch "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf" "$DYNAMIC_DIR/voicemail.dynamic.conf"
chown asterisk:telephony "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf" "$DYNAMIC_DIR/voicemail.dynamic.conf"
chmod 0660 "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf" "$DYNAMIC_DIR/voicemail.dynamic.conf"
mkdir -p /var/spool/asterisk/voicemail
chown -R asterisk:telephony /var/spool/asterisk/voicemail
chmod 2770 /var/spool/asterisk/voicemail

cat > "$ASTERISK_CONFIG_DIR/ari.conf" <<EOF
[general]
enabled = yes
pretty = no
; ARI is private to the telephony-internal Docker network.
allowed_origins = http://asterisk:8088,https://asterisk:8089

[$ARI_USER]
type = user
read_only = no
password = $ARI_PASSWORD
EOF

cat > "$ASTERISK_CONFIG_DIR/manager.conf" <<EOF
[general]
enabled = yes
webenabled = no
port = 5038
; AMI is reachable only through the private telephony-internal Docker network.
; The host port is not published by docker-compose.
bindaddr = 0.0.0.0

[$AMI_USER]
secret = $AMI_PASSWORD
read = system,call,log,verbose,command,agent,user,config,dtmf,reporting,originate
write = system,call,log,verbose,command,agent,user,config,dtmf,reporting,originate
EOF

# Transports are static; all endpoints, authentication objects, registrations,
# and provider identify rules are rendered from the administration database.
cat > "$ASTERISK_CONFIG_DIR/pjsip.bootstrap.conf" <<EOF
[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_media_address=$ASTERISK_EXTERNAL_ADDRESS
external_signaling_address=$ASTERISK_EXTERNAL_ADDRESS

[transport-tcp]
type=transport
protocol=tcp
bind=0.0.0.0:5060
external_media_address=$ASTERISK_EXTERNAL_ADDRESS
external_signaling_address=$ASTERISK_EXTERNAL_ADDRESS
EOF


cat > "$ASTERISK_CONFIG_DIR/http.conf" <<EOF
[general]
enabled = yes
bindaddr = 0.0.0.0
bindport = 8088
enable_static = no

tlsenable = yes
tlsbindaddr = 0.0.0.0:8089
tlsprivatekey = /etc/asterisk/keys/asterisk.key
tlscertfile = /etc/asterisk/keys/asterisk.crt
EOF

# Generate a self-signed certificate only when one is not already persisted.
# Browser WSS remains intentionally internal until a trusted certificate path
# is deployed.
KEY_DIR="$ASTERISK_CONFIG_DIR/keys"
mkdir -p "$KEY_DIR"
if [ ! -s "$KEY_DIR/asterisk.key" ] || [ ! -s "$KEY_DIR/asterisk.crt" ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
        -keyout "$KEY_DIR/asterisk.key" \
        -out "$KEY_DIR/asterisk.crt" \
        -subj "/CN=$ASTERISK_EXTERNAL_ADDRESS" \
        >/dev/null 2>&1
    chown asterisk:asterisk "$KEY_DIR/asterisk.key" "$KEY_DIR/asterisk.crt"
    chmod 0600 "$KEY_DIR/asterisk.key"
    chmod 0644 "$KEY_DIR/asterisk.crt"
fi

exec asterisk -f -T -C "$ASTERISK_CONFIG_DIR/asterisk.conf"
