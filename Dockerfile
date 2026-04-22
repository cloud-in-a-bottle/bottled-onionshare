FROM debian:bookworm-slim

# Install runtime dependencies: tor (the daemon onionshare invokes),
# python + pip, and the onionshare-cli Python package (brings its own
# Flask/Tornado web stack used to serve each share).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tor \
        obfs4proxy \
        python3 \
        python3-pip \
        python3-venv \
        procps \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install onionshare-cli. It ships a bundled resources/ directory and
# its own Flask server; it will spawn a Tor process per invocation.
RUN pip3 install --no-cache-dir --break-system-packages onionshare-cli==2.6.3

# onionshare invokes /usr/bin/tor directly; make sure it's on PATH and
# the Debian tor package installs to /usr/bin/tor.
RUN test -x /usr/bin/tor

WORKDIR /app
COPY server.py shares.py entrypoint.sh ./
COPY templates/ templates/
COPY static/ static/

RUN chmod +x /app/entrypoint.sh

# Admin UI only. All public-facing share traffic goes out over Tor's
# anonymous network inside the container; no extra ports are bound on
# the host.
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
