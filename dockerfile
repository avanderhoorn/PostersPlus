# ── Builder stage ─────────────────────────────────────────────────────────────
# Compiles pycairo (and any future C-extension wheels) against the cairo dev
# headers, then we copy only the resulting wheels into the runtime image so the
# ~200MB of build toolchain doesn't ship to users.
FROM python:3.11-slim AS builder
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libcairo2-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels --no-cache-dir -r requirements.txt
RUN find /wheels -type f -name 'opencv_python-*.whl' -delete

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim
WORKDIR /app
ARG RENDERER_REVISION=unknown
ENV POSTERSPLUS_RENDERER_REVISION=${RENDERER_REVISION}

# libcairo2 (runtime only — no -dev headers needed) for pycairo;
# gosu for privilege drop in entrypoint.sh.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
# RapidOCR's GUI OpenCV dependency is API-compatible with the headless wheel we
# intentionally install. Normalize its installed metadata so `pip check` and
# dependency scanners do not report the deliberate substitution as broken.
RUN pip install --no-cache-dir --no-deps /wheels/*.whl \
    && sed -i 's/^Requires-Dist: opencv_python/Requires-Dist: opencv-python-headless/' \
       /usr/local/lib/python3.11/site-packages/rapidocr-*.dist-info/METADATA \
    && pip check \
    && rm -rf /wheels

# Bake the PP-OCRv5 Mobile detector into the image.  TEXTLESS_TEXT_DETECTION is
# on by default, so baking avoids the one-time ~4.6MB runtime download that would
# otherwise stall the first low-vote textless request, and it survives cache-
# volume wipes / works on air-gapped hosts.  Adds ~4.6MB to the image, and makes
# the build depend on PPOCR_MODEL_URL being reachable.  Opt out for a lean image
# (e.g. if you disable detection) — the model then downloads once at runtime:
#   docker build --build-arg BAKE_PPOCR_MODEL=false ...
#   (or set BAKE_PPOCR_MODEL=false in .env when building via compose)
ARG BAKE_PPOCR_MODEL=true
ARG PPOCR_MODEL_URL=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx
ARG PPOCR_MODEL_SHA256=4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae
RUN if [ "$BAKE_PPOCR_MODEL" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends curl && \
      mkdir -p /app/models && \
      curl -fsSL "$PPOCR_MODEL_URL" -o /app/models/ch_PP-OCRv5_det_mobile.onnx && \
      echo "$PPOCR_MODEL_SHA256  /app/models/ch_PP-OCRv5_det_mobile.onnx" | sha256sum -c - && \
      apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* ; \
    fi

RUN adduser --disabled-password --gecos '' appuser

# Copy app files and set ownership on everything except the cache dir,
# which is a runtime volume mount — permissions are fixed by entrypoint.sh.
COPY . .
RUN chown -R appuser:appuser /app

# Run as root so entrypoint.sh can fix cache volume permissions at startup,
# then it drops to appuser via gosu before exec-ing uvicorn.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)" || exit 1
CMD ["/bin/sh", "entrypoint.sh"]
