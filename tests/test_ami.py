"""The AMI restart contract: what fails loudly, what is just the restart.

`restart()` exists because transport changes (and removed endpoint sections)
only take effect at startup. Swallowing every failure there leaves the running
Asterisk on the old configuration while the files on disk hold the new one -
the split this must prevent. So: failing to reach or authenticate with AMI
raises (the restart never happened), a connection drop after the command
landed is the restart itself, and a restart that never comes back raises too.
"""
import socket

import pytest

from app.ami import AMIError, AsteriskAMI

SUCCESS_LOGIN = b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
SUCCESS_COMMAND = b"Response: Success\r\nMessage: Command executed\r\n\r\n"
ERROR_LOGIN = b"Response: Error\r\nMessage: Bad username or password\r\n\r\n"
ERROR_COMMAND = b"Response: Error\r\nMessage: Command 'core restart now' is not permitted\r\n\r\n"


class FakeSocket:
    """One AMI connection: recv hands out the scripted replies, then EOF."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = b""
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        return self.replies.pop(0) if self.replies else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def scripted(monkeypatch, connections, failures=0):
    """Route `create_connection` through `connections`; refuse the first N."""
    opened = []
    pending = list(connections)
    refused = [failures]

    def create_connection(address, timeout=None):
        opened.append(address)
        if refused[0] > 0:
            refused[0] -= 1
            raise OSError("connection refused")
        sock = FakeSocket(pending.pop(0) if pending else [])
        return sock

    monkeypatch.setattr(socket, "create_connection", create_connection)
    return opened


def ami():
    return AsteriskAMI("127.0.0.1", 5038, "admin", "secret", timeout=0.5)


def test_a_failure_before_the_restart_command_raises(monkeypatch):
    """Not reaching AMI, or not authenticating, means NO restart happened."""
    monkeypatch.setattr("time.sleep", lambda _s: None)

    scripted(monkeypatch, [], failures=1)
    with pytest.raises(OSError):
        ami().restart(verify_timeout=0.2)

    scripted(monkeypatch, [[ERROR_LOGIN]])
    with pytest.raises(AMIError) as refused:
        ami().restart(verify_timeout=0.2)
    assert "not restarted" in str(refused.value)

    # A refused command is a real failure too - the reply landed, unlike a
    # restart's dropped connection, so it must not be waved away.
    scripted(monkeypatch, [[SUCCESS_LOGIN, ERROR_COMMAND]])
    with pytest.raises(AMIError) as refused:
        ami().restart(verify_timeout=0.2)
    assert "refused the restart" in str(refused.value)


def test_a_dropped_connection_after_the_command_is_the_restart_itself(monkeypatch):
    """`core restart now` can take the connection down before the reply lands.

    That drop is the restart under way - and then Asterisk must come back:
    AMI is polled on a fresh connection until it answers again.
    """
    monkeypatch.setattr("time.sleep", lambda _s: None)
    opened = scripted(monkeypatch, [
        [SUCCESS_LOGIN, b""],          # login ok; the reply never lands
        [SUCCESS_LOGIN, SUCCESS_COMMAND],  # the verify poll: back up
    ])
    ami().restart(verify_timeout=5)
    assert len(opened) == 2

    # The same is true when the Success reply does land before the drop.
    opened = scripted(monkeypatch, [
        [SUCCESS_LOGIN, SUCCESS_COMMAND],
        [SUCCESS_LOGIN, SUCCESS_COMMAND],
    ])
    ami().restart(verify_timeout=5)
    assert len(opened) == 2


def test_a_restart_that_never_comes_back_raises(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    scripted(monkeypatch, [
        [SUCCESS_LOGIN, SUCCESS_COMMAND],  # the restart went out
        [b""],                             # every verify poll finds it down
    ])
    with pytest.raises(AMIError) as refused:
        ami().restart(verify_timeout=0.1)
    assert "did not come back" in str(refused.value)
