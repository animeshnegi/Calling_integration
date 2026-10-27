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
# PocketSphinx includes its offline English model in the wheel. Validate it
# while building on the workstation, not by downloading models on the VM.
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "from pocketsphinx import Decoder; Decoder(loglevel='FATAL')"

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser web ./web
COPY --chown=appuser:appuser wsgi.py ./wsgi.py
COPY --chown=appuser:appuser deploy/THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md

# Image-only/Windows deployments do not copy repository scripts to the VM.
# Carry the host-side proxy/repair and offline verification kit in the API
# image so docker cp can extract it after docker load. It is NOT run inside the
# API container and the container receives no Docker socket or host-proxy
# privileges. The offline speech sample/model are baked in; nothing downloads.
COPY --chown=appuser:appuser scripts/finish-deployment.py scripts/install-softphone-proxy.py scripts/check-softphone-websocket.py scripts/check-voicemail-transcription.py scripts/verify-release.sh scripts/diagnose-extension-audio.sh ./deploy-kit/scripts/
COPY --chown=appuser:appuser deploy/nginx/softphone-websocket.conf ./deploy-kit/deploy/nginx/
COPY --chown=appuser:appuser deploy/SOFTPHONE_TOOLS.md deploy/RELEASE_CHECKLIST.md deploy/THIRD_PARTY_NOTICES.md ./deploy-kit/deploy/
COPY --chown=appuser:appuser deploy/fixtures/README.md deploy/fixtures/goforward-8k.wav ./deploy-kit/deploy/fixtures/

# A Windows builder may have CRLF working-tree files. Normalize only text
# host-tools; the WAV sample and all other bytes remain untouched.
RUN python -c "from pathlib import Path; ext={'.sh','.py','.conf','.md'}; [(p.write_bytes(p.read_bytes().replace(b'\r\n',b'\n'))) for p in Path('/app/deploy-kit').rglob('*') if p.is_file() and p.suffix in ext]"

USER appuser

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
