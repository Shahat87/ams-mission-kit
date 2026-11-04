# Egress allowlist proxy: squid on the same pinned python base the plan already trusts (no new third-party digest).
# Config is NOT baked in: the host renders squid.conf.tmpl and passes it base64 in env MCL_SQUID_CONF_B64; the default
# CMD decodes it to /tmp/squid.conf and execs squid (all pid/cache/log/core paths are under /tmp or stdio).
# Run: docker run --user 13:13 --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp
#        -e MCL_SQUID_CONF_B64=... <image>   (or override CMD with your own sh -c)
# UNVERIFIED until Docker exists (smoke S18): that squid starts as uid 13 under --read-only/--cap-drop ALL and that
# uid 13 can open /dev/stdout and /dev/stderr for the logs.
FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83
RUN apt-get update \
 && apt-get install -y --no-install-recommends squid \
 && rm -rf /var/lib/apt/lists/* \
 && id proxy | grep -q 'uid=13('
COPY squid.conf.tmpl /opt/proxy/squid.conf.tmpl
USER 13:13
EXPOSE 3128
CMD ["sh", "-c", "printf %s \"$MCL_SQUID_CONF_B64\" | base64 -d > /tmp/squid.conf && exec squid -N -f /tmp/squid.conf"]
