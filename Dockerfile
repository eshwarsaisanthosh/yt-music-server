FROM python:3.11-slim

# ffmpeg/ffprobe for probing, transcoding, and chapter splitting.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

ENV YTM_DATA_ROOT=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8000

# The worker service overrides this command with: python -m app.worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
