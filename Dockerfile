FROM python:3.13.15-slim-bookworm@sha256:ed86c82274b3c69b52fb5820f358f0bd7df0b603332063cb5c6e32bd220c3e6e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system appuser \
    && groupadd --gid 2000 telephony \
    && useradd --system --gid appuser --groups telephony --create-home appuser \
    && install -d -o appuser -g appuser -m 0750 /app/instance \
    && install -d -o appuser -g telephony -m 0770 /app/asterisk-config \
    && install -d -o appuser -g telephony -m 0770 /app/voicemail

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser web ./web
COPY --chown=appuser:appuser wsgi.py ./wsgi.py

USER appuser

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
