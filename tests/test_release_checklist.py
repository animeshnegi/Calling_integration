"""Windows image-only deployment and four live checks must stay documented."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_offline_recognizer_self_check_runs_in_source_checkout():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check-voicemail-transcription.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "transcribed"' in result.stdout
    assert '"offline": true' in result.stdout
    assert "go forward ten meters" in result.stdout


def test_release_verification_scripts_are_valid_and_allow_no_server_build():
    for name in ("scripts/verify-release.sh", "scripts/diagnose-extension-audio.sh",
                 "scripts/build-export-images.sh", "scripts/import-start-images.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / name)], capture_output=True, text=True).returncode == 0
    verifier = (ROOT / "scripts/verify-release.sh").read_text()
    assert "--network none" in verifier
    assert "check-voicemail-transcription.py" in verifier
    assert "check-softphone-websocket.py" in verifier
    assert "no-store" in verifier
    assert "docker compose build" not in verifier


def test_release_checklist_covers_images_files_keys_transcription_and_softphone():
    text = (ROOT / "deploy/RELEASE_CHECKLIST.md").read_text()
    required = (
        "docker image save --output", "scp ", "sha256sum -c", "docker image load --input",
        "tar -xzf /tmp/softphone-proxy-tools.tar.gz", "Never upload or overwrite `.env`",
        "IMAGE_TAG=\"$TAG\" docker compose up -d --no-build --force-recreate",
        "telephony-api telephony-ari", "bash scripts/verify-release.sh",
        "Save & send test email", "HTTP 401", "HTTP 403", "Recent email deliveries",
        "Include a voicemail transcript", "check-voicemail-transcription.py",
        "install-softphone-proxy.py", "check-softphone-websocket.py",
        "diagnose-extension-audio.sh", "101 ↔ 105", "outbound calling",
    )
    for item in required:
        assert item in text, f"release checklist is missing {item!r}"
    assert "api.openai.com" not in text
    assert "There is **no transcription API key**" in text


def test_tools_bundle_carries_release_and_audio_diagnostics():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kit_release", ROOT / "scripts/package-softphone-tools.py")
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    required = {
        "deploy/RELEASE_CHECKLIST.md", "deploy/THIRD_PARTY_NOTICES.md",
        "deploy/fixtures/goforward-8k.wav", "scripts/check-voicemail-transcription.py",
        "scripts/verify-release.sh", "scripts/diagnose-extension-audio.sh",
    }
    assert required.issubset(set(packager.FILES))
    for name in packager.FILES:
        assert (ROOT / name).is_file(), name
    assert not any(name.endswith((".env", "docker-compose.yml", "settings.db")) for name in packager.FILES)
    operations = (ROOT / "docs/OPERATIONS.md").read_text()
    deployment = (ROOT / "docs/DEPLOYMENT.md").read_text()
    assert "deploy/RELEASE_CHECKLIST.md" in operations and "deploy/RELEASE_CHECKLIST.md" in deployment


def test_offline_self_check_works_from_the_image_deploy_kit_layout(tmp_path):
    """No source checkout is present inside a runtime image."""
    import shutil
    (tmp_path / "app").mkdir()
    (tmp_path / "deploy-kit/scripts").mkdir(parents=True)
    (tmp_path / "deploy-kit/fixtures").mkdir(parents=True)
    shutil.copyfile(ROOT / "app/local_transcription.py", tmp_path / "app/local_transcription.py")
    shutil.copyfile(ROOT / "scripts/check-voicemail-transcription.py", tmp_path / "deploy-kit/scripts/check-voicemail-transcription.py")
    shutil.copyfile(ROOT / "deploy/fixtures/goforward-8k.wav", tmp_path / "deploy-kit/fixtures/goforward-8k.wav")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "deploy-kit/scripts/check-voicemail-transcription.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "transcribed"' in result.stdout
