FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

ARG ASTERISK_VERSION=22.11.0
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       ca-certificates \
       curl \
       openssl \
       libedit-dev \
       libjansson-dev \
       libncurses5-dev \
       libnewt-dev \
       libogg-dev \
       libopus-dev \
       libsamplerate0-dev \
       libsndfile1-dev \
       libspeex-dev \
       libspeexdsp-dev \
       libsqlite3-dev \
       libssl-dev \
       libtool \
       libvorbis-dev \
       libxml2-dev \
       pkg-config \
       procps \
       uuid-dev \
       xmlstarlet \
    && curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o "/tmp/asterisk-${ASTERISK_VERSION}.tar.gz" \
    && curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.sha256" -o /tmp/asterisk.sha256 \
    && cd /tmp \
    && sha256sum -c asterisk.sha256 \
    && tar -xzf "/tmp/asterisk-${ASTERISK_VERSION}.tar.gz" -C /tmp \
    && cd "/tmp/asterisk-${ASTERISK_VERSION}" \
    && ./configure --with-jansson-bundled --with-pjproject-bundled \
    && make -j2 \
    && make install \
    && make samples \
    && ldconfig \
    && cd / \
    && rm -rf "/tmp/asterisk-${ASTERISK_VERSION}" "/tmp/asterisk-${ASTERISK_VERSION}.tar.gz" /tmp/asterisk.sha256 \
    && apt-get purge -y --auto-remove \
       build-essential curl \
       libedit-dev libjansson-dev libncurses5-dev libnewt-dev \
       libogg-dev libopus-dev libsamplerate0-dev libsndfile1-dev \
       libspeex-dev libspeexdsp-dev libsqlite3-dev libssl-dev \
       libtool libvorbis-dev libxml2-dev pkg-config uuid-dev xmlstarlet \
    && rm -rf /var/lib/apt/lists/*

COPY config/ /etc/asterisk/
COPY entrypoint.sh /entrypoint.sh
RUN chmod 750 /entrypoint.sh \
    && groupadd --system asterisk \
    && groupadd --gid 2000 telephony \
    && useradd --system --gid asterisk --groups telephony --home-dir /var/lib/asterisk --no-create-home asterisk \
    && mkdir -p /var/lib/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk /etc/asterisk/keys /etc/asterisk/dynamic \
    && chown -R asterisk:asterisk /var/lib/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk /etc/asterisk/keys \
    && chown asterisk:telephony /etc/asterisk/dynamic \
    && chmod 0770 /etc/asterisk/dynamic

# ARI 8088 is intentionally not exposed/published. WebRTC WSS is kept internal
# until a trusted certificate/reverse-proxy path is explicitly deployed.
EXPOSE 5060/udp 5060/tcp 8089/tcp 10000-10100/udp
VOLUME ["/var/lib/asterisk", "/var/spool/asterisk", "/etc/asterisk", "/etc/asterisk/keys"]

ENTRYPOINT ["/entrypoint.sh"]
