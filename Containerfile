# KimiOS image (Phase 0). See docs/SCOPE.md.
#
# Build:  podman build -t kimios .
# Use:    sudo bootc switch ghcr.io/kiplabat/kimios:latest   (on Fedora Silverblue)

ARG FEDORA_VERSION=45
FROM quay.io/fedora/fedora-silverblue:${FEDORA_VERSION}

# Optional: URL of a KimiLM Linux release archive (.tar.gz). Empty means the
# image is built without KimiLM, and the KimiOS session falls back to GNOME.
ARG KIMILM_URL=""

COPY system_files/ /
COPY build_files/ /tmp/build_files/

RUN KIMILM_URL="${KIMILM_URL}" bash /tmp/build_files/build.sh && \
    rm -rf /tmp/build_files /var/tmp/* && \
    if command -v bootc >/dev/null 2>&1; then bootc container lint; fi

LABEL org.opencontainers.image.title="KimiOS" \
      org.opencontainers.image.description="LLM-native operating system built on Fedora bootc" \
      org.opencontainers.image.source="https://github.com/kiplabat/KimiOS" \
      containers.bootc="1"
