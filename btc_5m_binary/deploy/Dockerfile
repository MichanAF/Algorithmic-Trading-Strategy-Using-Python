# The quote watcher, containerised.  It reads two public APIs and writes one
# CSV; it holds no key, signs nothing and has no order path, so there is nothing
# in this image worth stealing.
#
# Build from the package directory, not from the repository root:
#
#     docker build -f deploy/Dockerfile -t btc5m-watch .
#     docker run -d --name btc5m-watch --restart unless-stopped \
#         -v /var/lib/btc5m:/data btc5m-watch
#
# arm64 is fine: numpy publishes aarch64 wheels, so a Graviton instance needs
# no build tooling.

FROM python:3.12-slim

# numpy is the only dependency, and it ships wheels, so no compiler is needed.
RUN pip install --no-cache-dir "numpy>=1.24"

RUN useradd --system --create-home --shell /usr/sbin/nologin btc5m \
 && mkdir -p /data && chown btc5m:btc5m /data

WORKDIR /app
COPY btc5m/ ./btc5m/
COPY configs/ ./configs/

USER btc5m
VOLUME /data

# The CSV is the deliverable, so it lives on the volume rather than in the
# container.  --windows 0 runs until the container is stopped.
ENTRYPOINT ["python", "-m", "btc5m"]
CMD ["watch", "--config", "configs/fade-flow-pooled-5m.json", \
     "--venue", "polymarket-btc-5m", "--windows", "0", \
     "--offsets", "5,15,30", "--out", "/data/quotes.csv"]
