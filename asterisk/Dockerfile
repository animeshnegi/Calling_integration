# syntax=docker/dockerfile:1
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

ARG ASTERISK_VERSION=22.11.0
# Local builds run on the developer workstation before the image is imported to
# the VPS. Four workers significantly reduce compile time on typical PCs while
# remaining reasonable on smaller builders; override with --build-arg when needed.
ARG ASTERISK_MAKE_JOBS=4
ENV DEBIAN_FRONTEND=noninteractive

# Upstream BUILD_NATIVE defaults to yes (-march=native). Exported images must
# also run on a different VM CPU: disable it before compiling Asterisk, codec
# modules and bundled pjproject. Keep frame pointers for usable native traces.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/tmp/asterisk-download,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl openssl libedit-dev libjansson-dev \
       libncurses5-dev libnewt-dev libogg-dev libopus-dev libsamplerate0-dev \
       libsndfile1-dev libsrtp2-dev libspeex-dev libspeexdsp-dev libsqlite3-dev libssl-dev \
       libtool libvorbis-dev libxml2 libxml2-dev pkg-config procps uuid-dev xmlstarlet \
    && cd /tmp/asterisk-download \
    && if [ ! -s "asterisk-${ASTERISK_VERSION}.tar.gz" ]; then \
         curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o "asterisk-${ASTERISK_VERSION}.tar.gz"; \
       fi \
    && if [ ! -s "asterisk-${ASTERISK_VERSION}.sha256" ]; then \
         curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.sha256" -o "asterisk-${ASTERISK_VERSION}.sha256"; \
       fi \
    && sha256sum -c "asterisk-${ASTERISK_VERSION}.sha256" \
    && rm -rf "/tmp/asterisk-${ASTERISK_VERSION}" \
    && tar -xzf "asterisk-${ASTERISK_VERSION}.tar.gz" -C /tmp \
    && cd "/tmp/asterisk-${ASTERISK_VERSION}" \
    && ./configure --with-jansson-bundled --with-pjproject-bundled \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE --enable KEEP_FRAME_POINTERS menuselect.makeopts \
    && if grep -Eq '(^|[[:space:]])BUILD_NATIVE([[:space:]]|$)' menuselect.makeopts; then \
         echo "ERROR: BUILD_NATIVE is still enabled; refusing a CPU-specific export image." >&2; \
         exit 1; \
       fi \
    && make -j"${ASTERISK_MAKE_JOBS}" \
    && make install && ldconfig \
    && mkdir -p /usr/share/asterisk \
    && printf 'ASTERISK_VERSION=%s\nBUILD_NATIVE=disabled\nKEEP_FRAME_POINTERS=enabled\n' "$ASTERISK_VERSION" \
         > /usr/share/asterisk/build-portability.txt \
    && if [ ! -f /usr/lib/asterisk/modules/res_srtp.so ]; then \
         echo "ERROR: res_srtp.so was not built. WebRTC endpoints negotiate DTLS-SRTP" >&2; \
         echo "       and every browser call would fail at media setup. Was libsrtp2-dev" >&2; \
         echo "       installed before ./configure? Refusing to ship this image." >&2; \
         exit 1; \
       fi \
    && for module in res_http_websocket res_pjsip_transport_websocket; do \
         if [ ! -f "/usr/lib/asterisk/modules/${module}.so" ]; then \
           echo "ERROR: ${module}.so was not built; /ws cannot serve SIP WebSockets. Refusing this image." >&2; \
           exit 1; \
         fi; \
       done \
    && cd / \
    && rm -rf "/tmp/asterisk-${ASTERISK_VERSION}" \
    && apt-get purge -y --auto-remove build-essential curl libedit-dev libjansson-dev libncurses5-dev libnewt-dev libogg-dev libopus-dev libsamplerate0-dev libsndfile1-dev libsrtp2-dev libspeex-dev libspeexdsp-dev libsqlite3-dev libssl-dev libtool libvorbis-dev libxml2-dev pkg-config uuid-dev xmlstarlet \
    && apt-get install -y --no-install-recommends libxml2 libsqlite3-0 libedit2 libcurl4 libsrtp2-1 libspeex1 libspeexdsp1 libogg0 libvorbis0a libvorbisenc2 libvorbisfile3 util-linux diffutils hostname mawk \
    && command -v flock && command -v getent && command -v hostname && command -v cmp && command -v awk

COPY config/ /etc/asterisk/
# Recordings the operator supplies (see sounds/custom/README.md). The directory
# may hold nothing but that note, in which case the stock prompts are used.
COPY sounds/ /var/lib/asterisk/sounds/
COPY entrypoint.sh configure-ice.sh /
# Windows Git checkouts can convert shell scripts to CRLF. Normalize them in the image.
RUN sed -i 's/\r$//' /entrypoint.sh /configure-ice.sh \
    && chmod 750 /entrypoint.sh /configure-ice.sh \
    && groupadd --system asterisk \
    && groupadd --gid 2000 telephony \
    && useradd --system --gid asterisk --groups telephony --home-dir /var/lib/asterisk --no-create-home asterisk \
    && mkdir -p /var/lib/asterisk /var/lib/asterisk/keys/keys /var/spool/asterisk /var/log/asterisk /var/run/asterisk /etc/asterisk/keys /etc/asterisk/dynamic \
    && chown -R asterisk:asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk /etc/asterisk/keys \
    && chown asterisk:telephony /etc/asterisk/dynamic \
    && chmod 0770 /etc/asterisk/dynamic

EXPOSE 5060/udp 5060/tcp 8089/tcp 10000-10100/udp
VOLUME ["/var/lib/asterisk", "/var/spool/asterisk", "/etc/asterisk/keys"]

ENTRYPOINT ["/entrypoint.sh"]
