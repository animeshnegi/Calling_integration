#!/bin/sh
set -eu

fail() { echo "[ERROR] $*" >&2; exit 1; }
require_env() { name="$1"; eval "value=\${$name:-}"; [ -n "$value" ] || fail "$name must be set"; }

require_env ASTERISK_EXTERNAL_ADDRESS
require_env ASTERISK_EXTENSIONS
require_env DEFAULT_EXTENSION
require_env IPCOMMS_SIP_SERVER
require_env IPCOMMS_SIP_PORT
require_env IPCOMMS_SIP_USERNAME
require_env IPCOMMS_SIP_PASSWORD
require_env IPCOMMS_DID
require_env IPCOMMS_ALLOWED_IPS
require_env ARI_USER
require_env ARI_PASSWORD
require_env AMI_USER
require_env AMI_PASSWORD

ASTERISK_RTP_START="${ASTERISK_RTP_START:-10000}"
ASTERISK_RTP_END="${ASTERISK_RTP_END:-10100}"
DYNAMIC_DIR=/etc/asterisk/dynamic
mkdir -p "$DYNAMIC_DIR" /etc/asterisk/keys

case "$IPCOMMS_SIP_PORT" in *[!0-9]*) fail "IPCOMMS_SIP_PORT must be numeric" ;; esac
case "$ASTERISK_RTP_START" in *[!0-9]*) fail "ASTERISK_RTP_START must be numeric" ;; esac
case "$ASTERISK_RTP_END" in *[!0-9]*) fail "ASTERISK_RTP_END must be numeric" ;; esac
[ "$ASTERISK_RTP_START" -lt "$ASTERISK_RTP_END" ] || fail "ASTERISK_RTP_START must be less than ASTERISK_RTP_END"
[ "$IPCOMMS_SIP_PORT" -ge 1 ] && [ "$IPCOMMS_SIP_PORT" -le 65535 ] || fail "IPCOMMS_SIP_PORT is invalid"
[ "$ASTERISK_RTP_START" -ge 1024 ] && [ "$ASTERISK_RTP_END" -le 65535 ] || fail "RTP range is invalid"

validate_extensions() {
  oldifs="$IFS"; IFS=','; count=0; default_found=no
  for raw_ext in $ASTERISK_EXTENSIONS; do
    ext=$(echo "$raw_ext" | tr -d '[:space:]'); [ -n "$ext" ] || continue
    case "$ext" in *[!0-9]*) fail "Invalid extension: $ext" ;; esac
    [ "$ext" -ge 100 ] && [ "$ext" -le 999 ] || fail "Extension must be between 100 and 999: $ext"
    password_var="EXTENSION_${ext}_PASSWORD"; eval "password=\${$password_var:-}"
    [ -n "$password" ] || fail "$password_var must be set"
    [ "$ext" = "$DEFAULT_EXTENSION" ] && default_found=yes
    count=$((count + 1))
  done
  IFS="$oldifs"
  [ "$count" -gt 0 ] || fail "ASTERISK_EXTENSIONS must not be empty"
  [ "$default_found" = yes ] || fail "DEFAULT_EXTENSION must appear in ASTERISK_EXTENSIONS"
}
validate_extensions

validate_ip_list() {
  oldifs="$IFS"; IFS=','; count=0
  for raw_ip in $IPCOMMS_ALLOWED_IPS; do
    ip=$(echo "$raw_ip" | tr -d '[:space:]'); [ -n "$ip" ] || continue
    case "$ip" in *[!0-9./:]*) fail "Invalid provider IP/CIDR: $ip" ;; esac
    count=$((count + 1))
  done
  IFS="$oldifs"; [ "$count" -gt 0 ] || fail "IPCOMMS_ALLOWED_IPS must not be empty"
}
validate_ip_list

if [ ! -s /etc/asterisk/keys/asterisk.pem ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
    -keyout /etc/asterisk/keys/asterisk.pem -out /etc/asterisk/keys/asterisk.pem \
    -subj "/CN=${ASTERISK_EXTERNAL_ADDRESS}" >/dev/null 2>&1 || fail "Could not generate TLS certificate"
  chmod 600 /etc/asterisk/keys/asterisk.pem
fi

DID_USER=$(echo "$IPCOMMS_DID" | tr -d '+ -()')
PROVIDER_ID=$(printf '%s' 'IPComms' | sha256sum | cut -c1-12)

cat > /etc/asterisk/pjsip.conf <<EOF
[global]
user_agent=EngineerIP-Telephony
unidentified_request_count=3
unidentified_request_period=5
unidentified_request_prune_interval=30
default_auth_algorithms_uas=SHA-256,MD5

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_signaling_address=${ASTERISK_EXTERNAL_ADDRESS}
external_signaling_port=5060
external_media_address=${ASTERISK_EXTERNAL_ADDRESS}
local_net=172.16.0.0/12

[transport-tcp]
type=transport
protocol=tcp
bind=0.0.0.0:5060
external_signaling_address=${ASTERISK_EXTERNAL_ADDRESS}
external_signaling_port=5060
external_media_address=${ASTERISK_EXTERNAL_ADDRESS}
local_net=172.16.0.0/12
allow_reload=yes

[transport-wss]
type=transport
protocol=wss
bind=0.0.0.0

#include /etc/asterisk/dynamic/pjsip.dynamic.conf
EOF

: > "$DYNAMIC_DIR/pjsip.dynamic.conf"
oldifs="$IFS"; IFS=','
for raw_ext in $ASTERISK_EXTENSIONS; do
  ext=$(echo "$raw_ext" | tr -d '[:space:]'); [ -n "$ext" ] || continue
  password_var="EXTENSION_${ext}_PASSWORD"; eval "password=\${$password_var:-}"
  cat >> "$DYNAMIC_DIR/pjsip.dynamic.conf" <<EOF
[${ext}]
type=aor
max_contacts=5
remove_existing=yes

[auth-${ext}]
type=auth
auth_type=userpass
username=${ext}
password=${password}
supported_algorithms_uas=SHA-256,MD5

[${ext}]
type=endpoint
aors=${ext}
auth=auth-${ext}
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
IFS="$oldifs"

cat >> "$DYNAMIC_DIR/pjsip.dynamic.conf" <<EOF
[provider-${PROVIDER_ID}]
type=endpoint
transport=transport-udp
context=from-provider
disallow=all
allow=ulaw,alaw
outbound_auth=provider-${PROVIDER_ID}-auth
aors=provider-${PROVIDER_ID}
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
from_user=${IPCOMMS_SIP_USERNAME}
from_domain=${IPCOMMS_SIP_SERVER}

[provider-${PROVIDER_ID}-auth]
type=auth
auth_type=userpass
username=${IPCOMMS_SIP_USERNAME}
password=${IPCOMMS_SIP_PASSWORD}

[provider-${PROVIDER_ID}]
type=aor
contact=sip:${IPCOMMS_SIP_SERVER}:${IPCOMMS_SIP_PORT}
qualify_frequency=60

[provider-${PROVIDER_ID}-reg]
type=registration
transport=transport-udp
outbound_auth=provider-${PROVIDER_ID}-auth
server_uri=sip:${IPCOMMS_SIP_SERVER}:${IPCOMMS_SIP_PORT}
client_uri=sip:${IPCOMMS_SIP_USERNAME}@${IPCOMMS_SIP_SERVER}
contact_user=${DID_USER}
retry_interval=30
forbidden_retry_interval=300
expiration=300

EOF

oldifs="$IFS"; IFS=','; i=1
for raw_ip in $IPCOMMS_ALLOWED_IPS; do
  ip=$(echo "$raw_ip" | tr -d '[:space:]'); [ -n "$ip" ] || continue
  cat >> "$DYNAMIC_DIR/pjsip.dynamic.conf" <<EOF
[ipcomms-identify-$i]
type=identify
endpoint=provider-${PROVIDER_ID}
match=$ip
EOF
  i=$((i + 1))
done
IFS="$oldifs"

cat > /etc/asterisk/extensions.conf <<EOF
[general]
static=yes
autofallthrough=yes

#include /etc/asterisk/dynamic/extensions.dynamic.conf
EOF
cat > "$DYNAMIC_DIR/extensions.dynamic.conf" <<EOF
[from-internal]
exten => _1XX,1,NoOp(EngineerIP extension \${EXTEN})
 same => n,Dial(PJSIP/\${EXTEN},30)
 same => n,Hangup()

[from-provider]
exten => ${DID_USER},1,NoOp(Inbound IPComms DID ${DID_USER})
 same => n,Dial(PJSIP/${DEFAULT_EXTENSION},30)
 same => n,Hangup()

exten => s,1,NoOp(Inbound provider call \${CALLERID(all)})
 same => n,Dial(PJSIP/${DEFAULT_EXTENSION},30)
 same => n,Hangup()
EOF

chmod 600 "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf"
chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk 2>/dev/null || true
# The dynamic configuration volume is shared with the unprivileged API via GID 2000.
chown asterisk:telephony "$DYNAMIC_DIR" 2>/dev/null || true
chmod 0770 "$DYNAMIC_DIR"
chmod 0660 "$DYNAMIC_DIR/pjsip.dynamic.conf" "$DYNAMIC_DIR/extensions.dynamic.conf"

VALIDATION_LOG=/tmp/asterisk-config-validation.log
rm -f "$VALIDATION_LOG"
set +e
(timeout 8s asterisk -f -U asterisk -G asterisk -vvv >"$VALIDATION_LOG" 2>&1)
validation_status=$?
set -e
if [ "$validation_status" -ne 124 ] && [ "$validation_status" -ne 0 ]; then
  cat "$VALIDATION_LOG" >&2
  exit "$validation_status"
fi
if grep -Eiq '(ERROR|Unable to load|failed to load|Parsing.*failed|config.*error)' "$VALIDATION_LOG"; then
  cat "$VALIDATION_LOG" >&2
  exit 1
fi
exec asterisk -f -U asterisk -G asterisk -vvv
