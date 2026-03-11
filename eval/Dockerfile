# Reviewer-friendly image for typefuzz.
#
# Layer ordering is optimized for Docker layer caching:
#   1-4  deps + V8 baseline checkout/sync/build at a FIXED commit
#   5    upstream fuzzilli clone + our patch + swift build
#   6    COPY our small source files (cov.ours.{h,cc}, instrument_v8.py)
#   7    optional V8 commit switch via ARG V8_COMMIT
#   8    run instrument_v8.py on the (possibly switched) V8 tree
#   9    incremental ninja rebuild
#
# Editing cov.ours.* / instrument_v8.py only invalidates from layer 6. The
# expensive V8 clone and baseline build remain cached.
#
# Build context must be the project root (eval/'s parent). Run `bash
# eval/run.sh` or use the manual form:
#   docker build -f eval/Dockerfile -t typefuzz-eval .
# The top-level .dockerignore excludes heavy directories so the context
# stays around ~100 KB.

FROM swift:6.1.2

# ----- System dependencies for building V8 and fuzzilli -----
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip git curl jq xz-utils file \
        build-essential pkg-config lsb-release sudo ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ----- depot_tools: single latest clone used by all V8 operations -----
ENV DEPOT_TOOLS=/opt/depot_tools
RUN git clone --depth 1 \
        https://chromium.googlesource.com/chromium/tools/depot_tools.git \
        $DEPOT_TOOLS
ENV PATH=$DEPOT_TOOLS:$PATH
ENV GCLIENT_SUPPRESS_GIT_VERSION_WARNING=1

# ----- V8 baseline checkout (FIXED, cached) -----
ARG V8_BASELINE=acaa58ed12a8645ab7a23dedd07d32f8208e9ea4
ENV V8_BASELINE=$V8_BASELINE
WORKDIR /work
RUN fetch v8
WORKDIR /work/v8
RUN git checkout --force $V8_BASELINE \
    && gclient sync -D --force --reset --no-history

# ----- V8 baseline build (uninstrumented, primes out/fuzzbuild/*.o) -----
RUN gn gen out/fuzzbuild --args='is_debug=false dcheck_always_on=true v8_static_library=true v8_enable_verify_heap=true v8_enable_partition_alloc=false v8_fuzzilli=true sanitizer_coverage_flags="trace-pc-guard" target_cpu="x64"' \
    && ninja -C out/fuzzbuild d8

# ----- Upstream fuzzilli (pinned) + our patch + swift build -----
ARG FUZZILLI_UPSTREAM=https://github.com/googleprojectzero/fuzzilli.git
ARG FUZZILLI_BASE=cbade79edaf1dae0c233890b7957f560008d2c63
WORKDIR /work
RUN git clone $FUZZILLI_UPSTREAM fuzzilli \
    && cd fuzzilli \
    && git checkout $FUZZILLI_BASE
COPY eval/patches/typefuzz-fuzzilli.patch /tmp/typefuzz-fuzzilli.patch
WORKDIR /work/fuzzilli
RUN git apply /tmp/typefuzz-fuzzilli.patch \
    && swift build -c release
ENV PATH=/work/fuzzilli/.build/release:$PATH
# Fuzzilli's binary expects Swift runtime libs at /usr/local/lib/swift;
# the base image puts them at /usr/lib/swift.
RUN ln -sf /usr/lib/swift /usr/local/lib/swift

# ----- Our small source files (this is the cache-invalidation boundary) -----
WORKDIR /work
COPY cov.ours.h cov.ours.cc ./
COPY instrument_v8.py ./

# ----- Optional V8 commit switch -----
# Default matches V8_BASELINE so this layer is effectively a no-op. Pass a
# different V8_COMMIT at build time to rebuild d8 at a specific commit; nearby
# commits are much faster to rebuild than far ones (ninja reuses .o files
# primed by the baseline build above).
ARG V8_COMMIT=acaa58ed12a8645ab7a23dedd07d32f8208e9ea4
WORKDIR /work/v8
RUN if [ "$V8_COMMIT" != "$V8_BASELINE" ]; then \
        git checkout --force $V8_COMMIT && \
        gclient sync -D --force --reset --no-history; \
    fi

# ----- Instrument V8 + incremental rebuild -----
WORKDIR /work
RUN python3 instrument_v8.py --instrument --no-build /work/v8
WORKDIR /work/v8
RUN ninja -C out/fuzzbuild d8

# ----- Runtime -----
WORKDIR /work
CMD ["/bin/bash"]
