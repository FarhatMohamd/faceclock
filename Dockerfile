# ---- FaceClock container image ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FACECLOCK_DATABASE=/data/faceclock.db FACECLOCK_MODELS_DIR=/app/models

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Download the face detection / recognition models at build time
RUN python scripts/download_models.py

RUN useradd --create-home faceclock && mkdir -p /data && chown -R faceclock /data /app
USER faceclock
VOLUME ["/data"]
EXPOSE 8000

CMD ["gunicorn", "-w", "3", "-b", "0.0.0.0:8000", "--timeout", "60", "wsgi:app"]
