# TODO: pin by digest (debian:bookworm-slim@sha256:...) once Docker is available to resolve it.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends squid \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /var/log/squid /run/squid \
 && chown -R proxy:proxy /var/log/squid /run/squid

COPY squid.conf /etc/squid/squid.conf

EXPOSE 3128
# -N: stay in the foreground; squid drops to the proxy user after binding.
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
