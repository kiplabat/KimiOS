# KimiOS image (Phase 0). See docs/SCOPE.md.
#
# Build:  podman build -t kimios .
#         podman build --build-arg PROFILE=headless --build-arg BASE_IMAGE=quay.io/fedora/fedora-bootc -t kimios-headless .
# Use:    sudo bootc switch ghcr.io/kiplabat/kimios:latest            (on Fedora Silverblue)
#         sudo bootc switch ghcr.io/kiplabat/kimios-headless:latest   (0.15.0: no desktop, on any Fedora bootc system)

ARG FEDORA_VERSION=45
# 0.15.0: desktop (GNOME and the KimiOS session) or headless (no desktop: KimiLM's engine as a service, reached
# over SSH; for appliances, robots and boxes in a cupboard). Each profile has its own base.
ARG BASE_IMAGE=quay.io/fedora/fedora-silverblue
FROM ${BASE_IMAGE}:${FEDORA_VERSION}
ARG PROFILE=desktop

# Optional: URL of a KimiLM Linux release archive (.tar.gz). Empty means the
# image is built without KimiLM, and the KimiOS session falls back to GNOME.
ARG KIMILM_URL=""

COPY system_files/ /
COPY build_files/ /tmp/build_files/

RUN KIMILM_URL="${KIMILM_URL}" PROFILE="${PROFILE}" bash /tmp/build_files/build.sh && \
    rm -rf /tmp/build_files /var/tmp/* && \
    if command -v bootc >/dev/null 2>&1; then bootc container lint; fi

LABEL org.opencontainers.image.title="KimiOS" \
      org.opencontainers.image.description="LLM-native operating system built on Fedora bootc" \
      org.opencontainers.image.source="https://github.com/kiplabat/KimiOS" \
      org.kimios.profile="${PROFILE}" \
      containers.bootc="1"
