from pathlib import Path


def test_compose_bounds_every_container_log():
    compose = Path("docker-compose.yml").read_text()
    assert compose.count("logging: *bounded-logging") == 3
    assert 'max-size: "10m"' in compose
    assert 'max-file: "3"' in compose
    assert "/var/log/asterisk:rw,noexec,nosuid,size=32m" in compose


def test_asterisk_does_not_write_unbounded_internal_log_files():
    logger = Path("asterisk/config/logger.conf").read_text()
    assert "console => warning,error,security" in logger
    assert "messages =>" not in logger
    assert "security =>" not in logger
