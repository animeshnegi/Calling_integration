FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       asterisk \
       asterisk-core-sounds-en-ulaw \
       ca-certificates \
       openssl \
       procps \
    && rm -rf /var/lib/apt/lists/*

COPY config/ /etc/asterisk/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5060/udp 5060/tcp 8088/tcp 8089/tcp 10000-10100/udp
VOLUME ["/var/lib/asterisk", "/var/spool/asterisk", "/etc/asterisk"]

ENTRYPOINT ["/entrypoint.sh"]
