# Run-check toolchain image (design doc §11 J10): Python 3.12 + pytest, Node 22 + npm,
# g++, make, cmake, git. Based on the c-bundle functional-run-sandbox Dockerfile.
#
#   Laptop: docker build -t resume-verifier-runcheck -f docker/runcheck.Dockerfile .
#   GB10:   docker build --platform linux/arm64 -t resume-verifier-runcheck -f docker/runcheck.Dockerfile .
#   Demo repos' extra Python deps: --build-arg EXTRA_PIP="numpy"
#
# The sandbox runs this image with --network none, as uid 1000, with only /work and /tmp writable.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ARG EXTRA_PIP=""

RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ make cmake git ca-certificates \
      libxcb1 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN pip install --no-cache-dir pytest ${EXTRA_PIP}

RUN useradd --create-home --uid 1000 runner \
 && mkdir -p /work && chown 1000:1000 /work
USER 1000:1000
WORKDIR /work
