# mc-lane: sandbox image for external coding CLIs (spec sections 4, 8, 14).
#
# Base choice: NODE image is the final stage, python 3.14 is copied INTO it from the pinned python image.
# Why: node 22.23.3 must be exactly the pinned one (NODE_USE_ENV_PROXY, pi needs >=22.19) and npm installs
# run here; python is only needed by entry.py and the verify commands, and its runtime shared libs are installed
# with apt in the same Debian bookworm base. The build fails early (import check) if a lib is missing.
# UNVERIFIED until Docker exists: that check, the npm installs, and `codex/grok/pi --help` in the image.
FROM python:3.14-slim-bookworm@sha256:48b13b003dda20b16f9442b8475aa05fe21bf6579a8c881db92ffb4d8fd20f83 AS py

FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392

COPY --from=py /usr/local/bin/python3.14 /usr/local/bin/python3.14
COPY --from=py /usr/local/lib/python3.14 /usr/local/lib/python3.14
COPY --from=py /usr/local/lib/libpython3.14.so.1.0 /usr/local/lib/libpython3.14.so.1.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates libssl3 libffi8 libsqlite3-0 zlib1g libbz2-1.0 \
        liblzma5 libncursesw6 libreadline8 libexpat1 libuuid1 libgdbm6 libgdbm-compat4 \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s python3.14 /usr/local/bin/python3 \
 && ln -s python3.14 /usr/local/bin/python \
 && ldconfig \
 && python3 -c "import ssl, sqlite3, ctypes, zlib, bz2, lzma, readline, json, hashlib, tarfile; print('python ok')"

# the node image ships a `node` user with uid/gid 1000; replace it with `agent`
RUN (userdel -r node 2>/dev/null || true) \
 && (groupdel node 2>/dev/null || true) \
 && groupadd -g 1000 agent \
 && useradd -u 1000 -g 1000 -m -d /home/agent -s /bin/bash agent

# exact pins from (design spec kept in the maintainers' docs, not shipped) (all at least 14 days old on 2026-10-21)
RUN npm install -g \
        @openai/codex@0.160.1 \
        @xai-official/grok@1.0.50 \
        @earendil-works/pi-coding-agent@1.0.4 \
 && npm cache clean --force

COPY --chown=0:0 sandbox/entry.py sandbox/tools.json /opt/mc-lane/
COPY --chown=0:0 sandbox/profiles/ /opt/mc-lane/profiles/
RUN chmod -R a-w,a+rX /opt/mc-lane && chmod 0555 /opt/mc-lane/entry.py

# named volumes copy ownership from these mount points; without it the run container cannot write
RUN mkdir -p /work /out /pristine /repo /x /in /dst /tool /outsrc /seed && chown 1000:1000 /work /out /pristine /repo /x /in /dst /tool /outsrc /seed

ENV HOME=/home/agent LANG=C.UTF-8 PATH=/usr/local/bin:/usr/bin:/bin
USER 1000:1000
WORKDIR /work
ENTRYPOINT ["python3", "/opt/mc-lane/entry.py"]
CMD ["--self-test"]
