# Cross toolchain for the SIM7600 module (MDM9607, ARMv7, Linux 3.18): static binaries, no runtime deps.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc-arm-linux-gnueabihf libc6-dev-armhf-cross make \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
