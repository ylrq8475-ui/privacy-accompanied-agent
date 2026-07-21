FROM nvcr.io/nvidia/vllm:26.06-py3 AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app
COPY requirements.txt ./
# DGX already caches the NVIDIA ARM64 runtime with Pydantic, Uvicorn, OpenCV,
# and HTTPX. Only the fixed audio decoder is absent; installing this small
# package avoids pulling unrelated base images or any additional model weights.
RUN python -m pip install --no-cache-dir miniaudio==1.71 \
    && python -c "import cv2, miniaudio, pydantic, uvicorn; assert pydantic.VERSION.startswith('2.')"

COPY backend ./backend
COPY external_connector ./external_connector
COPY track_catalog ./track_catalog
COPY data/music ./data/music
COPY data/audius_playlists.example.json ./data/audius_playlists.example.json
COPY console/dist ./console/dist

RUN groupadd --system spark-demo \
    && useradd --system --gid spark-demo --home-dir /nonexistent spark-demo \
    && mkdir -p /var/lib/spark-demo \
    && chown spark-demo:spark-demo /var/lib/spark-demo

USER spark-demo

FROM python-base AS test
USER root
COPY tests ./tests
COPY scripts ./scripts
COPY Dockerfile docker-compose.dgx.yml ./
USER spark-demo
CMD ["python", "-m", "unittest", "discover", "-s", "tests"]

FROM python-base AS runtime
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "backend.app.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
