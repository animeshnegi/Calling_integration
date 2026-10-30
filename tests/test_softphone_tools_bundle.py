"""Image-only deployments must carry host tools, not assume a Git checkout."""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kit_package", ROOT / "scripts/package-softphone-tools.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def kit_sources(tmp_path):
    root = tmp_path / "source"
    for name in packager.FILES:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    (root / ".env").write_text("SECRET_KEY=never-export-this\n")
    (root / "settings.db").write_bytes(b"do not export databases")
    (root / "docker-compose.yml").write_text("do not overwrite deployed compose")
    return root


def test_tools_archive_has_only_allowlisted_files_and_valid_checksum(tmp_path):
    root = kit_sources(tmp_path)
    archive, checksum = packager.package(tmp_path / "softphone-proxy-tools.tar.gz", root)
    with tarfile.open(archive, "r:gz") as kit:
        assert set(kit.getnames()) == set(packager.FILES)
        for member in kit.getmembers():
            assert member.isfile() and member.name[0] != "/" and ".." not in Path(member.name).parts
            assert member.mode == 0o644
            assert kit.extractfile(member).read() == (root / member.name).read_bytes()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksum.read_bytes() == f"{digest}  {archive.name}\n".encode("ascii")


def test_extracted_kit_runs_without_source_repo_or_image_build(tmp_path):
    archive, _ = packager.package(tmp_path / "tools.tar.gz")
    target = tmp_path / "empty-vm-checkout"
    with tarfile.open(archive) as kit:
        for name in packager.FILES:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(kit.extractfile(name).read())
    result = subprocess.run([sys.executable, str(target / "scripts/install-softphone-proxy.py"), "--help"], capture_output=True, text=True, cwd=tmp_path, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "--connect-network" in result.stdout
    assert not (target / "app").exists() and not (target / ".env").exists()


def test_kit_packager_rejects_symlinks_without_destroying_existing_archive(tmp_path):
    root = kit_sources(tmp_path)
    output = tmp_path / "tools.tar.gz"
    output.write_bytes(b"existing archive")
    name = root / packager.FILES[0]
    name.unlink()
    name.symlink_to(root / ".env")
    with pytest.raises(ValueError, match="symlinked"):
        packager.package(output, root)
    assert output.read_bytes() == b"existing archive"


def test_kit_packager_cannot_overwrite_its_source(tmp_path):
    root = kit_sources(tmp_path)
    with pytest.raises(ValueError, match="overwrite a source"):
        packager.package(root / packager.FILES[0], root)


def test_dockerfile_embeds_all_required_host_tools_without_changing_runtime_command():
    dockerfile = (ROOT / "Dockerfile").read_text()
    copies = [line for line in dockerfile.splitlines() if line.startswith("COPY ") and "./deploy-kit/" in line]
    for name in packager.FILES:
        line = next(line for line in copies if name in line.split())
        destination = line.split()[-1].removeprefix("./deploy-kit/")
        assert str(Path(destination) / Path(name).name) == name
    assert '"wsgi:app"' in dockerfile
    assert "docker.sock" not in dockerfile
    assert not any(line.startswith("RUN ") and "install-softphone-proxy" in line for line in dockerfile.splitlines())


def fake_docker(tmp_path, sources):
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "docker-calls.jsonl"
    script = binary / "docker"
    script.write_text('''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
args=sys.argv[1:]
with open(os.environ['DOCKER_CALLS'],'a') as f:f.write(json.dumps(args)+'\\n')
if args[:2]==['compose','build']:print('built images')
elif args[:2]==['image','save']:sys.stdout.buffer.write(b'saved Docker image stream')
elif args[0]=='create':print('1234567890abcdef')
elif args[0]=='cp':
    if os.environ.get('FAIL_KIT_COPY')=='1':sys.exit(5)
    assert args[1]=='1234567890abcdef:/app/deploy-kit/.'
    shutil.copytree(os.environ['KIT_SOURCE'],args[2],dirs_exist_ok=True)
elif args[0]=='rm':pass
elif args[:2]==['image','load']:
    assert args[2]=='--input'
    assert pathlib.Path(args[3]).is_file()
elif args[:2]==['network','inspect']:pass
elif args[:2]==['compose','up']:
    assert '--no-build' in args and '--build' not in args
elif args[:2]==['compose','ps']:pass
else:sys.exit(90)
''')
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "DOCKER_CALLS": str(calls), "KIT_SOURCE": str(sources)}, calls


def test_image_export_also_exports_tools_and_never_starts_a_container(tmp_path):
    # Even an unexpectedly overfull image kit must not leak secrets/Compose.
    root = kit_sources(tmp_path)
    env, calls_path = fake_docker(tmp_path, root)
    result = subprocess.run(["bash", str(ROOT / "scripts/build-export-images.sh"), "kit-test"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert gzip.decompress((tmp_path / "engineerip-telephony-kit-test.tar.gz").read_bytes()) == b"saved Docker image stream"
    with tarfile.open(tmp_path / "engineerip-softphone-tools-kit-test.tar.gz") as kit:
        assert set(kit.getnames()) == set(packager.FILES)
    for name in ("engineerip-telephony-kit-test.tar.gz", "engineerip-softphone-tools-kit-test.tar.gz"):
        assert (tmp_path / f"{name}.sha256").read_text().startswith(hashlib.sha256((tmp_path / name).read_bytes()).hexdigest())
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert ["rm", "1234567890abcdef"] in calls
    assert not any(args[0] in ("start", "run") for args in calls)
    assert "COPY THIS TOO" in result.stdout


def test_export_fails_and_cleans_up_if_image_has_no_kit(tmp_path):
    root = kit_sources(tmp_path)
    env, calls_path = fake_docker(tmp_path, root)
    env["FAIL_KIT_COPY"] = "1"
    result = subprocess.run(["bash", str(ROOT / "scripts/build-export-images.sh"), "old-image"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert ["rm", "1234567890abcdef"] in [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert not (tmp_path / "engineerip-softphone-tools-old-image.tar.gz").exists()


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("absolute_path", [False, True])
def test_import_accepts_windows_tar_or_gzip_without_building_or_overwriting_env(tmp_path, compressed, absolute_path):
    root = kit_sources(tmp_path)
    env, calls_path = fake_docker(tmp_path, root)
    secret = tmp_path / ".env"
    secret.write_text("SECRET_KEY=keep-existing-server-secrets\n")
    archive_dir = tmp_path / "transfer" if absolute_path else tmp_path
    archive_dir.mkdir(exist_ok=True)
    archive = archive_dir / ("windows-images.tar.gz" if compressed else "windows-images.tar")
    archive_arg = str(archive) if absolute_path else archive.name
    archive.write_bytes(gzip.compress(b"images") if compressed else b"images")
    archive.with_name(archive.name + ".sha256").write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + f"  {archive.name}\n")
    result = subprocess.run(["bash", str(ROOT / "scripts/import-start-images.sh"), archive_arg, "local-build"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert ["image", "load", "--input", archive_arg] in calls
    assert ["compose", "up", "-d", "--no-build"] in calls
    assert not any("build" in args for args in calls)
    assert secret.read_text() == "SECRET_KEY=keep-existing-server-secrets\n"
    assert "does not install host scripts" in result.stdout


def test_import_bad_checksum_stops_before_docker_load(tmp_path):
    root = kit_sources(tmp_path)
    env, calls_path = fake_docker(tmp_path, root)
    (tmp_path / ".env").write_text("existing secret")
    archive = tmp_path / "bad.tar"
    archive.write_bytes(b"changed")
    (tmp_path / "bad.tar.sha256").write_text("0" * 64 + "  bad.tar\n")
    result = subprocess.run(["bash", str(ROOT / "scripts/import-start-images.sh"), "bad.tar", "local-build"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert not calls_path.exists()
