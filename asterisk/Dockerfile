FROM debian:bookworm-slim

ARG ASTERISK_VERSION=22.11.0
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       ca-certificates \
       curl \
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
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_VERSION}.tar.gz" -o /tmp/asterisk.tar.gz \
    && tar -xzf /tmp/asterisk.tar.gz -C /tmp \
    && cd "/tmp/asterisk-${ASTERISK_VERSION}" \
    && ./configure --with-jansson-bundled --with-pjproject-bundled \
    && make -j2 \
    && make install \
    && make samples \
    && ldconfig \
    && cd / \
    && rm -rf "/tmp/asterisk-${ASTERISK_VERSION}" /tmp/asterisk.tar.gz

COPY config/ /etc/asterisk/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5060/udp 5060/tcp 8088/tcp 8089/tcp 10000-10100/udp
VOLUME ["/var/lib/asterisk", "/var/spool/asterisk", "/etc/asterisk"]

ENTRYPOINT ["/entrypoint.sh"]
