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

# app/ now contains templates/ and static/ (css, js, images).
COPY --chown=appuser:appuser app ./app
# The browser and Asterisk use the same original instrumental hold loop.
COPY --chown=appuser:appuser asterisk/moh ./asterisk/moh
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

# Fail the workstation build, rather than the server deployment, if a refactor
# omitted a Python package or the precompiled offline stylesheet/page assets.
# Imports do not create an app, connect to MySQL, or start the ARI event worker.
RUN python -c "from pathlib import Path; from app.models.settings import SettingsStore; from app.routes.admin import register_admin; from app.asterisk.client import AsteriskClient; from app.asterisk.worker import main; root=Path('/app/app'); pages=list((root/'templates').glob('*.html')); assert len(pages) >= 6, 'Missing HTML templates'; assets=['css/tailwind.css', 'css/phone.css', 'js/admin.js', 'js/phone.js', 'js/jssip-fallback.js', 'js/console-theme.js', 'js/login.js', 'js/signup.js', 'js/console-check.js', 'images/favicon.svg', 'images/icons.svg']; assert all((root/'static'/p).is_file() and (root/'static'/p).stat().st_size for p in assets), 'Missing bundled UI asset'"

EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
