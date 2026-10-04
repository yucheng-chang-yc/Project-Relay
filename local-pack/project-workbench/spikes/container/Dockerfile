# Pass an independently verified Debian image digest as BASE_IMAGE. No implicit mutable tag.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends r-base-core python3 zip coreutils \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir /workspace && chmod 1777 /workspace
WORKDIR /workspace
USER 65532:65532
