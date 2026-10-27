"""Windows image-only deployment and four live checks must stay documented."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_script(name, module_name):
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "scripts" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    finish = (ROOT / "scripts/finish-deployment.py").read_text()
    assert "--force-recreate" in finish and "--no-build" in finish
    assert "--network none" in finish and "check-voicemail-transcription.py" in finish
    assert "--apply" in finish and "--connect-network" in finish and "no-store" in finish
    assert "docker compose build" not in finish
    wrapper = (ROOT / "scripts/verify-release.sh").read_text()
    assert "--verify-only" in wrapper and "set -o pipefail" not in wrapper
    # Every executable line ends in a comment so a CRLF copy stays parseable.
    assert all(line.rstrip().endswith("#") for line in wrapper.splitlines()[1:] if line.strip())


def test_release_checklist_covers_images_files_keys_transcription_and_softphone():
    text = (ROOT / "deploy/RELEASE_CHECKLIST.md").read_text()
    required = (
        "docker image save --output", "scp ", "sha256sum -c", "docker image load --input",
        "tar -xzf /tmp/softphone-proxy-tools.tar.gz", "Never upload or overwrite `.env`",
        "python3 scripts/finish-deployment.py", "--image-tar",
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
        "deploy/fixtures/goforward-8k.wav", "scripts/finish-deployment.py",
        "scripts/check-voicemail-transcription.py",
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
    (tmp_path / "deploy-kit/deploy/fixtures").mkdir(parents=True)
    shutil.copyfile(ROOT / "app/local_transcription.py", tmp_path / "app/local_transcription.py")
    shutil.copyfile(ROOT / "scripts/check-voicemail-transcription.py", tmp_path / "deploy-kit/scripts/check-voicemail-transcription.py")
    shutil.copyfile(ROOT / "deploy/fixtures/goforward-8k.wav", tmp_path / "deploy-kit/deploy/fixtures/goforward-8k.wav")
    result = subprocess.run(
        [sys.executable, str(tmp_path / "deploy-kit/scripts/check-voicemail-transcription.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"status": "transcribed"' in result.stdout


def test_legacy_verify_wrapper_runs_even_with_windows_crlf(tmp_path):
    import shutil
    shutil.copyfile(ROOT / "scripts/finish-deployment.py", tmp_path / "finish-deployment.py")
    wrapper = (ROOT / "scripts/verify-release.sh").read_bytes().replace(b"\n", b"\r\n")
    target = tmp_path / "verify-release.sh"
    target.write_bytes(wrapper)
    assert subprocess.run(["bash", "-n", str(target)], capture_output=True, text=True).returncode == 0
    result = subprocess.run(["bash", str(target), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert "finish-deployment.py" in result.stdout


def test_finish_deployment_normalizes_crlf_but_not_binary_fixtures(tmp_path, monkeypatch):
    module = load_script("finish-deployment.py", "finish_deployment_normalise_test")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "deploy").mkdir()
    (tmp_path / "scripts/tool.sh").write_bytes(b"echo ok\r\n")
    (tmp_path / "deploy/site.conf").write_bytes(b"one\r\ntwo\r\n")
    (tmp_path / "deploy/audio.wav").write_bytes(b"RIFF\r\nDATA\r\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    module.normalise_text_files()
    assert (tmp_path / "scripts/tool.sh").read_bytes() == b"echo ok\n"
    assert (tmp_path / "deploy/site.conf").read_bytes() == b"one\ntwo\n"
    assert (tmp_path / "deploy/audio.wav").read_bytes() == b"RIFF\r\nDATA\r\n"


def test_tools_archive_normalizes_windows_line_endings(tmp_path):
    import shutil
    import tarfile
    packager = load_script("package-softphone-tools.py", "kit_crlf_test")
    source = tmp_path / "source"
    for name in packager.FILES:
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    for name in ("scripts/finish-deployment.py", "scripts/verify-release.sh"):
        (source / name).write_bytes((ROOT / name).read_bytes().replace(b"\n", b"\r\n"))
    archive, _ = packager.package(tmp_path / "tools.tar.gz", source)
    with tarfile.open(archive) as kit:
        for name in ("scripts/finish-deployment.py", "scripts/verify-release.sh"):
            assert b"\r\n" not in kit.extractfile(name).read()


def test_finish_deployment_removes_helper_container_after_failed_copy(monkeypatch):
    module = load_script("finish-deployment.py", "finish_deployment_cleanup_test")
    calls = []

    def fake_run(argv, **kwargs):
        argv = [str(part) for part in argv]
        calls.append(argv)
        if argv[:2] == ["docker", "create"]:
            return SimpleNamespace(returncode=0, stdout="helper-container-id\n", stderr="")
        if argv[:2] == ["docker", "cp"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no deploy-kit")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module, "run", fake_run)
    with pytest.raises(RuntimeError, match="/app/deploy-kit"):
        module.extract_deploy_kit("engineerip/telephony-api:latest")
    assert ["docker", "rm", "helper-container-id"] in calls
