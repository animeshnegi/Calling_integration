# syntax=docker/dockerfile:1
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

ARG ASTERISK_VERSION=22.11.0
# Local builds run on the developer workstation before the image is imported to
# the VPS. Four workers significantly reduce compile time on typical PCs while
# remaining reasonable on smaller builders; override with --build-arg when needed.
ARG ASTERISK_MAKE_JOBS=4
ENV DEBIAN_FRONTEND=noninteractive

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    --mount=type=cache,target=/tmp/asterisk-download,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl openssl libedit-dev libjansson-dev \
       libncurses5-dev libnewt-dev libogg-dev libopus-dev libsamplerate0-dev \
       libsndfile1-dev libspeex-dev libspeexdsp-dev libsqlite3-dev libssl-dev \
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
    && make -j"${ASTERISK_MAKE_JOBS}" \
    && make install && ldconfig \
    && cd / \
    && rm -rf "/tmp/asterisk-${ASTERISK_VERSION}" \
    && apt-get purge -y --auto-remove build-essential curl libedit-dev libjansson-dev libncurses5-dev libnewt-dev libogg-dev libopus-dev libsamplerate0-dev libsndfile1-dev libspeex-dev libspeexdsp-dev libsqlite3-dev libssl-dev libtool libvorbis-dev libxml2-dev pkg-config uuid-dev xmlstarlet \
    && apt-get install -y --no-install-recommends libxml2 libsqlite3-0 libedit2 libcurl4 libspeex1 libspeexdsp1 libogg0 libvorbis0a libvorbisenc2 libvorbisfile3

COPY config/ /etc/asterisk/
COPY entrypoint.sh /entrypoint.sh
# Windows Git checkouts can convert shell scripts to CRLF. Normalize it in the image.
RUN sed -i 's/\r$//' /entrypoint.sh \
    && chmod 750 /entrypoint.sh \
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
