# Valhalla 3.6.3 + the two Europe Lite patches, built from source.
#
# This is the CANONICAL recipe: a clean clone at a pinned commit, the two patches, a full Release
# build. It is FUNCTIONALLY EQUIVALENT to `wedrive-valhalla:lite-prod2`, the image the 2026-09-24
# `final2` graph was built with, but it is NOT the recipe that image came from. lite-prod2 was made
# by an experimental workflow: Valhalla's own Dockerfile over a COPY of a checkout (not a clone),
# ccache as the compiler launcher, then the patches applied with `patch -p1` in a `docker commit`
# layer and only `valhalla_build_tiles` rebuilt incrementally and installed. No Dockerfile can
# rebuild that image, and none should.
#
# Measured 2026-09-24 against lite-prod2: same base image layers, same apt package versions, same
# gcc, byte-identical `valhalla_service`; `valhalla_build_tiles` differs in md5. Docker image byte
# identity is NOT a release criterion — see README, "What reproducible means here".
#
# The base image is pinned by digest. apt packages are NOT pinned: `install-linux-deps.sh` installs
# whatever the Ubuntu archive serves on the day of the build (see README).
#
#   docker build -t wedrive-valhalla:lite tools/europe-lite/valhalla
FROM ubuntu:24.04@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3
ARG VALHALLA_TAG=3.6.3
ARG VALHALLA_COMMIT=e2f017b16080f49203de245a211b09efab09cf72
ENV DEBIAN_FRONTEND=noninteractive
ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV LD_LIBRARY_PATH=/usr/local/lib:/lib/x86_64-linux-gnu:/usr/lib/x86_64-linux-gnu

RUN apt-get update && apt-get install -y --no-install-recommends sudo git ca-certificates

RUN git clone --branch "${VALHALLA_TAG}" --depth 1 --recurse-submodules --shallow-submodules \
        https://github.com/valhalla/valhalla.git /src/valhalla \
 && test "$(git -C /src/valhalla rev-parse HEAD)" = "${VALHALLA_COMMIT}"

WORKDIR /src/valhalla
RUN bash ./scripts/install-linux-deps.sh

# Exactly the two patches, in this order; the build fails if either does not apply cleanly.
COPY valhalla-3.6.3-ferry-first-edge.patch valhalla-3.6.3-border-control-contract.patch /patches/
RUN git apply --check /patches/valhalla-3.6.3-ferry-first-edge.patch \
 && git apply /patches/valhalla-3.6.3-ferry-first-edge.patch \
 && git apply --check /patches/valhalla-3.6.3-border-control-contract.patch \
 && git apply /patches/valhalla-3.6.3-border-control-contract.patch \
 && git status --short

RUN cmake -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=gcc \
        -DENABLE_TESTS=Off -DENABLE_SINGLE_FILES_WERROR=Off \
 && make -C build all -j"$(nproc)" \
 && make -C build install
