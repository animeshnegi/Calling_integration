#!/bin/sh
set -eu

# Asterisk container entrypoint.
# Configuration is generated from runtime environment variables and the
# database-managed dynamic include mounted at /etc/asterisk/dynamic.

: "${ASTERISK_EXTERNAL_ADDRESS:?ASTERISK_EXTERNAL_ADDRESS must be set}"
: "${ASTERISK_EXTENSIONS:?ASTERISK_EXTENSIONS must be set}"
: "${DEFAULT_EXTENSION:?DEFAULT_EXTENSION must be set}"
: "${ARI_USER:?ARI_USER must be set}"
: "${ARI_PASSWORD:?ARI_PASSWORD must be set}"
: "${AMI_USER:?AMI_USER must be set}"
: "${AMI_PASSWORD:?AMI_PASSWORD must be set}"

ASTERISK_CONFIG_DIR=/etc/asterisk
DYNAMIC_DIR=/etc/asterisk/dynamic

mkdir -p "$DYNAMIC_DIR" /var/run/asterisk /var/log/asterisk /var/spool/asterisk /var/lib/asterisk

cat > "$ASTERISK_CONFIG_DIR/ari.conf" <<EOF
[general]
enabled = yes
pretty = no
allowed_origins =

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
bindaddr = 127.0.0.1

[$AMI_USER]
secret = $AMI_PASSWORD
read = system,call,log,verbose,command,agent,user,config,dtmf,reporting,originate
write = system,call,log,verbose,command,agent,user,config,dtmf,reporting,originate
EOF

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

[asterisk-bootstrap]
type=endpoint
context=from-internal
disallow=all
allow=ulaw,alaw
aors=asterisk-bootstrap-aor

[asterisk-bootstrap-aor]
type=aor
max_contacts=1

[ipcomms-bootstrap]
type=endpoint
context=from-provider
disallow=all
allow=ulaw,alaw
outbound_auth=ipcomms-bootstrap-auth
aors=ipcomms-bootstrap-aor
from_domain=ipcomms.com

[ipcomms-bootstrap-auth]
type=auth
auth_type=userpass
username=$IPCOMMS_USERNAME
password=$IPCOMMS_PASSWORD

[ipcomms-bootstrap-aor]
type=aor
contact=sip:$IPCOMMS_DID@ipcomms.com:5060
qualify_frequency=30

[ipcomms-bootstrap-identify]
type=identify
endpoint=ipcomms-bootstrap
match=$IPCOMMS_ALLOWED_IPS
EOF

cat > "$ASTERISK_CONFIG_DIR/extensions.bootstrap.conf" <<EOF
[from-internal]
exten => _1XX,1,Dial(PJSIP/\${EXTEN},30)
 same => n,Hangup()

[from-provider]
exten => s,1,Dial(PJSIP/$DEFAULT_EXTENSION,30)
 same => n,Hangup()
EOF

# Keep a deterministic WSS listener configuration available for future
# trusted reverse-proxy deployment. It is not published by Compose.
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

# Validate the rendered configuration before starting the foreground daemon.
asterisk -T -C "$ASTERISK_CONFIG_DIR/asterisk.conf" -rx 'core show version' >/dev/null

exec asterisk -f -T -C "$ASTERISK_CONFIG_DIR/asterisk.conf"
