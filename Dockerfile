FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/opt/hf DATA_DIR=/tmp/clipforge
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Bake the speech model into the image so the first export is not slow (falls back to download on first use)
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')" \
    || echo "Whisper model will download on first use"
COPY app app
COPY static static
RUN useradd -m clip && mkdir -p /tmp/clipforge /opt/hf && chown -R clip /app /opt/hf /tmp/clipforge
USER clip
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
