#!/bin/sh
set -eu

# Asterisk container entrypoint.
# Static configuration is shipped in the image. Runtime credentials and
# database-managed objects are generated into the mounted dynamic directory.

: "${ASTERISK_EXTERNAL_ADDRESS:?ASTERISK_EXTERNAL_ADDRESS must be set}"
: "${ASTERISK_EXTENSIONS:?ASTERISK_EXTENSIONS must be set}"
: "${DEFAULT_EXTENSION:?DEFAULT_EXTENSION must be set}"
: "${ARI_USER:?ARI_USER must be set}"
: "${ARI_PASSWORD:?ARI_PASSWORD must be set}"
: "${AMI_USER:?AMI_USER must be set}"
: "${AMI_PASSWORD:?AMI_PASSWORD must be set}"
: "${IPCOMMS_SIP_USERNAME:?IPCOMMS_SIP_USERNAME must be set}"
: "${IPCOMMS_SIP_PASSWORD:?IPCOMMS_SIP_PASSWORD must be set}"
: "${IPCOMMS_SIP_SERVER:?IPCOMMS_SIP_SERVER must be set}"
: "${IPCOMMS_SIP_PORT:?IPCOMMS_SIP_PORT must be set}"
: "${IPCOMMS_DID:?IPCOMMS_DID must be set}"
: "${IPCOMMS_ALLOWED_IPS:?IPCOMMS_ALLOWED_IPS must be set}"

ASTERISK_CONFIG_DIR=/etc/asterisk
DYNAMIC_DIR=/etc/asterisk/dynamic

mkdir -p "$DYNAMIC_DIR" \
    /var/run/asterisk \
    /var/log/asterisk \
    /var/spool/asterisk \
    /var/lib/asterisk \
    /var/lib/asterisk/keys/keys

# Only create bootstrap PJSIP objects when the database-managed include is
# empty. Once the API has rendered real objects, those objects must not be
# duplicated by the bootstrap configuration.
BOOTSTRAP_PJSIP=0
if [ ! -s "$DYNAMIC_DIR/pjsip.dynamic.conf" ]; then
    BOOTSTRAP_PJSIP=1
fi

touch "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf"
chown asterisk:telephony "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf"
chmod 0660 "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf"

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

if [ "$BOOTSTRAP_PJSIP" -eq 1 ]; then
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

    # Bootstrap local SIP extensions so the healthcheck and first registration
    # work before the admin/database sync has rendered dynamic configuration.
    for EXTENSION in $(printf '%s' "$ASTERISK_EXTENSIONS" | tr ',' ' '); do
        case "$EXTENSION" in
            ''|*[!0-9]*)
                echo "Invalid extension in ASTERISK_EXTENSIONS: $EXTENSION" >&2
                exit 1
                ;;
        esac

        PASSWORD_VAR="EXTENSION_${EXTENSION}_PASSWORD"
        EXTENSION_PASSWORD="$(printenv "$PASSWORD_VAR" 2>/dev/null || true)"
        if [ -z "$EXTENSION_PASSWORD" ]; then
            echo "$PASSWORD_VAR must be set for bootstrap extension $EXTENSION" >&2
            exit 1
        fi

        cat >> "$ASTERISK_CONFIG_DIR/pjsip.bootstrap.conf" <<EOF

[$EXTENSION]
type=aor
max_contacts=5
remove_existing=yes

[auth-$EXTENSION]
type=auth
auth_type=userpass
username=$EXTENSION
password=$EXTENSION_PASSWORD
supported_algorithms_uas=SHA-256,MD5

[$EXTENSION]
type=endpoint
aors=$EXTENSION
auth=auth-$EXTENSION
context=from-internal
disallow=all
allow=ulaw,alaw
transport=transport-udp
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
allow_subscribe=no
EOF
    done

    # Bootstrap IPComms trunk. Database-managed provider configuration replaces
    # this object after the admin settings are synchronized.
    cat >> "$ASTERISK_CONFIG_DIR/pjsip.bootstrap.conf" <<EOF

[ipcomms]
type=endpoint
context=from-provider
disallow=all
allow=ulaw,alaw
outbound_auth=ipcomms-auth
aors=ipcomms-aor
from_domain=$IPCOMMS_SIP_SERVER

[ipcomms-auth]
type=auth
auth_type=userpass
username=$IPCOMMS_SIP_USERNAME
password=$IPCOMMS_SIP_PASSWORD

[ipcomms-aor]
type=aor
contact=sip:$IPCOMMS_SIP_SERVER:$IPCOMMS_SIP_PORT
qualify_frequency=30

[ipcomms-identify]
type=identify
endpoint=ipcomms
match=$IPCOMMS_ALLOWED_IPS
EOF
else
    # Keep the file valid but empty when the database-managed config is active.
    : > "$ASTERISK_CONFIG_DIR/pjsip.bootstrap.conf"
fi

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
