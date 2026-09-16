#!/bin/sh
set -eu

fail() {
  echo "[ERROR] $*" >&2
  exit 1
}

require_env() {
  name="$1"
  eval "value=\${$name:-}"
  [ -n "$value" ] || fail "$name must be set"
}

require_env ASTERISK_EXTERNAL_ADDRESS
require_env IPCOMMS_SIP_SERVER
require_env IPCOMMS_SIP_PORT
require_env IPCOMMS_SIP_USERNAME
require_env IPCOMMS_SIP_PASSWORD
require_env IPCOMMS_DID
require_env IPCOMMS_ALLOWED_IPS
require_env ASTERISK_EXTENSIONS
require_env ARI_USER
require_env ARI_PASSWORD

ASTERISK_EXTENSIONS="$(echo "$ASTERISK_EXTENSIONS" | tr -d '[:space:]')"
WEBRTC_EXTENSIONS="$(echo "${WEBRTC_EXTENSIONS:-101}" | tr -d '[:space:]')"
WEBRTC_EXTENSION_PASSWORD="${WEBRTC_EXTENSION_PASSWORD:-}"
ASTERISK_RTP_START="${ASTERISK_RTP_START:-10000}"
ASTERISK_RTP_END="${ASTERISK_RTP_END:-10100}"

case "$IPCOMMS_SIP_PORT" in
  *[!0-9]*) fail "IPCOMMS_SIP_PORT must be numeric" ;;
esac
case "$ASTERISK_RTP_START" in
  *[!0-9]*) fail "ASTERISK_RTP_START must be numeric" ;;
esac
case "$ASTERISK_RTP_END" in
  *[!0-9]*) fail "ASTERISK_RTP_END must be numeric" ;;
esac

[ "$ASTERISK_RTP_START" -lt "$ASTERISK_RTP_END" ] || fail "ASTERISK_RTP_START must be less than ASTERISK_RTP_END"
[ "$IPCOMMS_SIP_PORT" -ge 1 ] && [ "$IPCOMMS_SIP_PORT" -le 65535 ] || fail "IPCOMMS_SIP_PORT must be between 1 and 65535"
[ "$ASTERISK_RTP_START" -ge 1024 ] && [ "$ASTERISK_RTP_END" -le 65535 ] || fail "RTP range must be between 1024 and 65535"

# Validate and load the configured local SIP extensions.
validate_extensions() {
  oldifs="$IFS"
  IFS=','
  count=0
  for raw_ext in $ASTERISK_EXTENSIONS; do
    ext=$(echo "$raw_ext" | tr -d '[:space:]')
    [ -n "$ext" ] || continue
    case "$ext" in
      *[!0-9]*) fail "Invalid extension in ASTERISK_EXTENSIONS: $ext" ;;
    esac
    [ "$ext" -ge 100 ] && [ "$ext" -le 999 ] || fail "Extension must be between 100 and 999: $ext"
    password_var="EXTENSION_${ext}_PASSWORD"
    eval "password=\${$password_var:-}"
    [ -n "$password" ] || fail "$password_var must be set for extension $ext"
    count=$((count + 1))
  done
  IFS="$oldifs"
  [ "$count" -gt 0 ] || fail "ASTERISK_EXTENSIONS must contain at least one extension"
}
validate_extensions

# WebRTC extensions are optional. Their password can be configured as
# WEBRTC_EXTENSION_<number>_PASSWORD; for 101, WEBRTC_EXTENSION_PASSWORD is
# retained as a backwards-compatible fallback.
validate_webrtc_extensions() {
  oldifs="$IFS"
  IFS=','
  for raw_ext in $WEBRTC_EXTENSIONS; do
    ext=$(echo "$raw_ext" | tr -d '[:space:]')
    [ -n "$ext" ] || continue
    case "$ext" in
      *[!0-9]*) fail "Invalid extension in WEBRTC_EXTENSIONS: $ext" ;;
    esac
    password_var="WEBRTC_EXTENSION_${ext}_PASSWORD"
    eval "password=\${$password_var:-}"
    if [ -z "$password" ] && [ "$ext" = "101" ]; then
      password="$WEBRTC_EXTENSION_PASSWORD"
    fi
    [ -n "$password" ] || fail "$password_var must be set for WebRTC extension $ext"
  done
  IFS="$oldifs"
}
validate_webrtc_extensions

# IPComms provider IPs are deliberately required so inbound SIP is matched by
# source address instead of trusting arbitrary SIP usernames.
validate_ip_list() {
  oldifs="$IFS"
  IFS=','
  count=0
  for raw_ip in $IPCOMMS_ALLOWED_IPS; do
    ip=$(echo "$raw_ip" | tr -d '[:space:]')
    [ -n "$ip" ] || continue
    case "$ip" in
      *[!0-9./:]*) fail "Invalid IP/CIDR in IPCOMMS_ALLOWED_IPS: $ip" ;;
    esac
    count=$((count + 1))
  done
  IFS="$oldifs"
  [ "$count" -gt 0 ] || fail "IPCOMMS_ALLOWED_IPS must contain at least one provider IP/CIDR"
}
validate_ip_list

mkdir -p /etc/asterisk/keys
if [ ! -s /etc/asterisk/keys/asterisk.pem ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
    -keyout /etc/asterisk/keys/asterisk.pem \
    -out /etc/asterisk/keys/asterisk.pem \
    -subj "/CN=${ASTERISK_EXTERNAL_ADDRESS}" >/dev/null 2>&1 || fail "Could not generate temporary TLS certificate"
  chmod 600 /etc/asterisk/keys/asterisk.pem
fi

DID_USER=$(echo "$IPCOMMS_DID" | tr -d '+ -()')
[ -n "$DID_USER" ] || fail "IPCOMMS_DID must contain a valid telephone number"

cat > /etc/asterisk/pjsip.conf <<EOF
[global]
user_agent=EngineerIP-Telephony

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_signaling_address=${ASTERISK_EXTERNAL_ADDRESS}
external_signaling_port=5060
external_media_address=${ASTERISK_EXTERNAL_ADDRESS}
local_net=172.16.0.0/12

[transport-wss]
type=transport
protocol=wss
bind=0.0.0.0

EOF

# Generate every configured local SIP endpoint.
i=1
oldifs="$IFS"
IFS=','
for raw_ext in $ASTERISK_EXTENSIONS; do
  ext=$(echo "$raw_ext" | tr -d '[:space:]')
  [ -n "$ext" ] || continue
  password_var="EXTENSION_${ext}_PASSWORD"
  eval "password=\${$password_var:-}"
  cat >> /etc/asterisk/pjsip.conf <<EOF
; Local SIP extension ${ext} for Zoiper / IP phones.
[${ext}]
type=aor
max_contacts=5
remove_existing=yes

[${ext}]
type=auth
auth_type=userpass
username=${ext}
password=${password}

[${ext}]
type=endpoint
aors=${ext}
auth=${ext}
context=from-internal
disallow=all
allow=ulaw,alaw
transport=transport-udp
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes

EOF
  i=$((i + 1))
done
IFS="$oldifs"

cat >> /etc/asterisk/pjsip.conf <<EOF
; IPComms registered SIP trunk.
[ipcomms]
type=endpoint
transport=transport-udp
context=from-provider
disallow=all
allow=ulaw,alaw
outbound_auth=ipcomms-auth
aors=ipcomms
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
from_user=${IPCOMMS_SIP_USERNAME}
from_domain=${IPCOMMS_SIP_SERVER}

[ipcomms-auth]
type=auth
auth_type=userpass
username=${IPCOMMS_SIP_USERNAME}
password=${IPCOMMS_SIP_PASSWORD}

[ipcomms]
type=aor
contact=sip:${IPCOMMS_SIP_SERVER}:${IPCOMMS_SIP_PORT}
qualify_frequency=60

[ipcomms-reg]
type=registration
transport=transport-udp
outbound_auth=ipcomms-auth
server_uri=sip:${IPCOMMS_SIP_SERVER}:${IPCOMMS_SIP_PORT}
client_uri=sip:${IPCOMMS_SIP_USERNAME}@${IPCOMMS_SIP_SERVER}
contact_user=${DID_USER}
retry_interval=30
forbidden_retry_interval=300
expiration=300

EOF

# Generate optional browser/WebRTC endpoints.
oldifs="$IFS"
IFS=','
for raw_ext in $WEBRTC_EXTENSIONS; do
  ext=$(echo "$raw_ext" | tr -d '[:space:]')
  [ -n "$ext" ] || continue
  password_var="WEBRTC_EXTENSION_${ext}_PASSWORD"
  eval "password=\${$password_var:-}"
  if [ -z "$password" ] && [ "$ext" = "101" ]; then
    password="$WEBRTC_EXTENSION_PASSWORD"
  fi
  cat >> /etc/asterisk/pjsip.conf <<EOF
; Browser WebRTC extension ${ext}-web. Replace the temporary certificate with a trusted
; certificate before production browser use.
[${ext}-web]
type=aor
max_contacts=2
remove_existing=yes

[${ext}-web]
type=auth
auth_type=userpass
username=${ext}-web
password=${password}

[${ext}-web]
type=endpoint
aors=${ext}-web
auth=${ext}-web
context=from-internal
disallow=all
allow=opus,ulaw,alaw
transport=transport-wss
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
webrtc=yes

EOF
done
IFS="$oldifs"

# Provider identify rules.
i=1
oldifs="$IFS"
IFS=','
for raw_ip in $IPCOMMS_ALLOWED_IPS; do
  ip=$(echo "$raw_ip" | tr -d '[:space:]')
  [ -n "$ip" ] || continue
  cat >> /etc/asterisk/pjsip.conf <<EOF
[ipcomms-identify-$i]
type=identify
endpoint=ipcomms
match=$ip
EOF
  i=$((i + 1))
done
IFS="$oldifs"

# Generate the dialplan at runtime so the configured IPComms DID route is
# always present, even when /etc/asterisk is backed by a persistent volume.
cat > /etc/asterisk/extensions.conf <<EOF
[general]
static=yes
autofallthrough=yes

[globals]

[from-internal]
; Dial another local SIP extension.
exten => _1XX,1,NoOp(EngineerIP extension \${EXTEN})
 same => n,Dial(PJSIP/\${EXTEN},30)
 same => n,Hangup()

; Outbound E.164 calls through the IPComms trunk.
exten => _+X.,1,NoOp(Outbound E.164 \${EXTEN})
 same => n,Dial(PJSIP/\${EXTEN}@ipcomms,60)
 same => n,Hangup()

[web-outbound]
exten => _+X.,1,NoOp(WebRTC outbound \${EXTEN})
 same => n,Dial(PJSIP/\${EXTEN}@ipcomms,60)
 same => n,Hangup()

[from-provider]
; IPComms may deliver the DID as the called extension instead of 's'.
exten => ${DID_USER},1,NoOp(Inbound IPComms DID ${DID_USER} \${CALLERID(all)})
 same => n,Dial(PJSIP/${DEFAULT_EXTENSION:-101},30)
 same => n,Hangup()

; Fallback for providers that deliver the called number as 's'.
exten => s,1,NoOp(Inbound IPComms call \${CALLERID(all)})
 same => n,Dial(PJSIP/${DEFAULT_EXTENSION:-101},30)
 same => n,Hangup()
EOF

cat > /etc/asterisk/rtp.conf <<EOF
[general]
rtpstart=${ASTERISK_RTP_START}
rtpend=${ASTERISK_RTP_END}
icesupport=yes
EOF

cat > /etc/asterisk/ari.conf <<EOF
[general]
enabled = yes
pretty = yes
; ARI is only reachable on the private Docker network; 8088 is not published.
allowed_origins = https://127.0.0.1

[${ARI_USER}]
type = user
read_only = no
password = ${ARI_PASSWORD}
EOF

chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk 2>/dev/null || true

VALIDATION_LOG=/tmp/asterisk-config-validation.log
rm -f "$VALIDATION_LOG"
set +e
(timeout 8s asterisk -f -U asterisk -G asterisk -vvv >"$VALIDATION_LOG" 2>&1)
validation_status=$?
set -e

if [ "$validation_status" -ne 124 ] && [ "$validation_status" -ne 0 ]; then
  echo "[ERROR] Asterisk configuration validation failed:" >&2
  cat "$VALIDATION_LOG" >&2
  exit "$validation_status"
fi

if grep -Eiq '(ERROR|Unable to load|failed to load|Parsing.*failed|config.*error)' "$VALIDATION_LOG"; then
  echo "[ERROR] Asterisk reported configuration errors during validation:" >&2
  cat "$VALIDATION_LOG" >&2
  exit 1
fi

exec asterisk -f -U asterisk -G asterisk -vvv
