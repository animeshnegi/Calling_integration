#!/bin/sh
set -eu

: "${ASTERISK_EXTERNAL_ADDRESS:?ASTERISK_EXTERNAL_ADDRESS must be set to the VPS public IP or telephony hostname}"
: "${IPCOMMS_SIP_SERVER:?IPCOMMS_SIP_SERVER must be set}"
: "${IPCOMMS_SIP_USERNAME:?IPCOMMS_SIP_USERNAME must be set}"
: "${IPCOMMS_SIP_PASSWORD:?IPCOMMS_SIP_PASSWORD must be set}"
: "${IPCOMMS_DID:?IPCOMMS_DID must be set}"
: "${EXTENSION_101_PASSWORD:?EXTENSION_101_PASSWORD must be set}"

WEBRTC_EXTENSION_PASSWORD="${WEBRTC_EXTENSION_PASSWORD:-${EXTENSION_101_PASSWORD}}"
ASTERISK_RTP_START="${ASTERISK_RTP_START:-10000}"
ASTERISK_RTP_END="${ASTERISK_RTP_END:-10100}"

mkdir -p /etc/asterisk/keys
if [ ! -s /etc/asterisk/keys/asterisk.pem ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
    -keyout /etc/asterisk/keys/asterisk.pem \
    -out /etc/asterisk/keys/asterisk.pem \
    -subj "/CN=${ASTERISK_EXTERNAL_ADDRESS}" >/dev/null 2>&1
  chmod 600 /etc/asterisk/keys/asterisk.pem
fi

cat > /etc/asterisk/pjsip.conf <<EOF
[global]
user_agent=EngineerIP-Telephony

[transport-udp]
type=transport
protocol=udp
bind=0.0.0.0:5060
external_signaling_address=${ASTERISK_EXTERNAL_ADDRESS}
external_signaling_port=5060
local_net=172.16.0.0/12

[transport-wss]
type=transport
protocol=wss
bind=0.0.0.0

; Extension 101 for Zoiper and SIP phones.
[101]
type=aor
max_contacts=5
remove_existing=yes

[101]
type=auth
auth_type=userpass
username=101
password=${EXTENSION_101_PASSWORD}

[101]
type=endpoint
aors=101
auth=101
context=from-internal
disallow=all
allow=opus,ulaw,alaw
transport=transport-udp
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes

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
contact=sip:${IPCOMMS_SIP_SERVER}:5060
qualify_frequency=60

[ipcomms-reg]
type=registration
transport=transport-udp
outbound_auth=ipcomms-auth
server_uri=sip:${IPCOMMS_SIP_SERVER}:5060
client_uri=sip:${IPCOMMS_SIP_USERNAME}@${IPCOMMS_SIP_SERVER}
contact_user=${IPCOMMS_DID}
retry_interval=30
forbidden_retry_interval=300
expiration=300

; Browser WebRTC extension. A trusted certificate should replace the temporary
; startup certificate before production browser use.
[101-web]
type=aor
max_contacts=2
remove_existing=yes

[101-web]
type=auth
auth_type=userpass
username=101-web
password=${WEBRTC_EXTENSION_PASSWORD}

[101-web]
type=endpoint
aors=101-web
auth=101-web
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

if [ -n "${IPCOMMS_ALLOWED_IPS:-}" ]; then
  i=1
  oldifs="$IFS"
  IFS=','
  for ip in $IPCOMMS_ALLOWED_IPS; do
    ip=$(echo "$ip" | tr -d '[:space:]')
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
fi

cat > /etc/asterisk/rtp.conf <<EOF
[general]
rtpstart=${ASTERISK_RTP_START}
rtpend=${ASTERISK_RTP_END}
icesupport=yes
EOF

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

chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk 2>/dev/null || true

exec asterisk -f -U asterisk -G asterisk -vvv
