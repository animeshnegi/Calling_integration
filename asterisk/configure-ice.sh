#!/bin/sh
# Before Asterisk starts, publish this container's real address and recreate
# the RTP ICE include using the last validated public address from the API.
# This runs again when Docker recreates Asterisk with a different bridge IP.
set -eu
DIR="${1:?Usage: configure-ice.sh DYNAMIC_DIR}"
mkdir -p "$DIR"
# Coordinate with both API/ARI sync writers in the shared volume. The lock
# itself contains no data; directory permissions restrict who can reach it.
# Root starts this helper, while the API runs as appuser in the telephony group.
touch "$DIR/.config-sync.lock"
chmod 0666 "$DIR/.config-sync.lock"
exec 9<>"$DIR/.config-sync.lock"
flock -x 9

# Docker puts the container hostname and its address in /etc/hosts. getent is
# supplied by libc-bin in the runtime image and needs no additional package.
name="$(hostname)"
locals="$(getent ahostsv4 "$name" 2>/dev/null | awk '{print $1}' | sort -t . -k1,1n -k2,2n -k3,3n -k4,4n -u || true)"

is_ipv4() {
    printf '%s\n' "$1" | awk '
      NR > 1 { exit 1 }
      { n=split($0,a,"[.]"); if (n != 4) exit 1;
        for (i=1;i<=4;i++) if (a[i] !~ /^[0-9]+$/ || a[i] > 255 || a[i] != int(a[i])) exit 1;
        if (a[1] == 0 || a[1] == 127 || a[1] >= 224 || (a[1] == 169 && a[2] == 254)) exit 1;
      }
      END { if (NR != 1) exit 1 }'
}

# Defence in depth for a damaged/hand-edited cache: the API already rejects
# these before writing it, but an invalid address must never become an ICE
# directive at Asterisk boot. TEST-NET examples remain permitted for tests.
is_public_ipv4() {
    is_ipv4 "$1" || return 1
    printf '%s\n' "$1" | awk '
      function ipnum(s, a) { split(s,a,"[.]"); return ((a[1]*256+a[2])*256+a[3])*256+a[4] }
      function matches(ip, cidr, parts, block) {
        split(cidr,parts,"/"); block=2^(32-parts[2]);
        return int(ip/block) == int(ipnum(parts[1])/block)
      }
      BEGIN {
        # Keep in sync with TelephonyConfigSync: private, special and
        # Cloudflare edge addresses cannot be the server public RTP address.
        bad="0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 " \
            "172.16.0.0/12 192.0.0.0/24 192.88.99.0/24 192.168.0.0/16 198.18.0.0/15 " \
            "224.0.0.0/4 240.0.0.0/4 173.245.48.0/20 103.21.244.0/22 " \
            "103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 " \
            "190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 " \
            "162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22";
        count=split(bad,nets," ")
      }
      { ip=ipnum($0); for (i=1;i<=count;i++) if (matches(ip,nets[i])) exit 1 }
    '
}

local_tmp="$(mktemp "$DIR/.rtp-local.XXXXXX")"
map_tmp="$(mktemp "$DIR/.rtp-ice.XXXXXX")"
trap 'rm -f "$local_tmp" "$map_tmp"' EXIT HUP INT TERM
for ip in $locals; do
    if is_ipv4 "$ip"; then printf '%s\n' "$ip" >> "$local_tmp"; fi
done
if [ ! -s "$local_tmp" ]; then
    echo "WARNING: Could not find this Asterisk container's IPv4 address; WebRTC public ICE mapping is unavailable." >&2
fi
chmod 0644 "$local_tmp"
mv -f "$local_tmp" "$DIR/rtp.local-addresses"

# This file is written only after the API has resolved and validated the
# Service address (including private/Cloudflare rejection). Invalid or missing
# content never becomes an Asterisk configuration directive.
public=""
if [ -f "$DIR/rtp.public-address" ]; then
    public="$(cat "$DIR/rtp.public-address")"
fi
if [ -n "$public" ] && ! is_public_ipv4 "$public"; then
    echo "WARNING: Invalid cached WebRTC media address; no public ICE mapping was generated." >&2
    public=""
fi
{
    printf '; Managed for browser WebRTC; Docker publishes the same RTP ports.\n[ice_host_candidates]\n'
    if [ -n "$public" ]; then
        while IFS= read -r ip; do
            [ -z "$ip" ] || printf '%s => %s\n' "$ip" "$public"
        done < "$DIR/rtp.local-addresses"
    fi
} > "$map_tmp"
chmod 0644 "$map_tmp"
# Do not churn the mtime of an unchanged include.
if [ -f "$DIR/rtp.ice.conf" ] && cmp -s "$map_tmp" "$DIR/rtp.ice.conf"; then
    rm -f "$map_tmp"
else
    mv -f "$map_tmp" "$DIR/rtp.ice.conf"
fi
trap - EXIT HUP INT TERM
