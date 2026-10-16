"""The AMI restart contract: what fails loudly, what is just the restart.

`restart()` exists because transport changes (and removed endpoint sections)
only take effect at startup. Swallowing every failure there leaves the running
Asterisk on the old configuration while the files on disk hold the new one -
the split this must prevent. So: failing to reach or authenticate with AMI
raises (the restart never happened), a connection drop after the command
landed is the restart itself, and a restart that never comes back raises too.

The replies are scripted as Asterisk's AMI actually speaks them: `Action:
Command` is not an ordinary action and answers `Response: Follows` with the
CLI output ended by `--END COMMAND--`, not `Response: Success`. The `Success`
shape is accepted too (some versions answer `Success` with `Output:` lines),
but `Follows` is the common one - and an earlier version of these tests that
scripted `Success` let a protocol mismatch through that would have called
every healthy restart a failure.
"""
import socket

import pytest

from app.ami import AMIError, AsteriskAMI

SUCCESS_LOGIN = b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n"
ERROR_LOGIN = b"Response: Error\r\nMessage: Bad username or password\r\n\r\n"
ERROR_COMMAND = b"Response: Error\r\nMessage: Command 'core restart now' is not permitted\r\n\r\n"

# The normal CLI-command reply: Response: Follows, the output, end marker.
FOLLOWS_RESTART = (
    b"Response: Follows\r\n"
    b"Privilege: Command\r\n"
    b"Command: core restart now\r\n"
    b"\r\n"
    b"--END COMMAND--\r\n"
)
FOLLOWS_VERSION = (
    b"Response: Follows\r\n"
    b"Privilege: Command\r\n"
    b"Command: core show version\r\n"
    b"\r\n"
    b"Asterisk 18.20.0 built by mockbuild on x86_64\r\n"
    b"--END COMMAND--\r\n"
)
# Some versions emit no blank line between the header block and the output.
FOLLOWS_NO_GAP = (
    b"Response: Follows\r\n"
    b"Privilege: Command\r\n"
    b"Command: core show version\r\n"
    b"Asterisk 18.20.0 built by mockbuild on x86_64\r\n"
    b"--END COMMAND--\r\n"
)
# Others answer Success with Output: lines instead.
SUCCESS_OUTPUT = (
    b"Response: Success\r\n"
    b"Output: Asterisk 18.20.0 built by mockbuild on x86_64\r\n"
    b"\r\n"
)


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


def test_a_cli_command_is_read_as_follows_not_as_a_refusal(monkeypatch):
    """The protocol contract, pinned to the real reply shapes.

    `Action: Command` answers `Response: Follows` + `--END COMMAND--` when the
    command ran. Judged by the ordinary `Response: Success` contract, every
    healthy answer looks like a failure - so the reader must drain the body
    through the end marker and accept Follows, the no-blank-line layout, and
    the Success/Output variant alike.
    """
    for reply in (FOLLOWS_VERSION, FOLLOWS_NO_GAP, SUCCESS_OUTPUT):
        scripted(monkeypatch, [[SUCCESS_LOGIN, reply]])
        ami().command("core show version")

    # Ordinary actions keep their ordinary contract: `Response: Success`
    # only - the Follows acceptance is specific to CLI commands.
    scripted(monkeypatch, [[SUCCESS_LOGIN, SUCCESS_OUTPUT]])
    ami().action("Reload", Module="res_pjsip.so")
    scripted(monkeypatch, [[SUCCESS_LOGIN, FOLLOWS_VERSION]])
    with pytest.raises(AMIError):
        ami().action("Reload", Module="res_pjsip.so")

    # A refusal is still a refusal, whatever shape it arrives in.
    scripted(monkeypatch, [[SUCCESS_LOGIN, ERROR_COMMAND]])
    with pytest.raises(AMIError) as refused:
        ami().command("core restart now")
    assert "not permitted" in str(refused.value)


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
    AMI is polled on a fresh connection until it answers again. Both reply
    shapes count: the Follows block that landed before the drop, and the
    empty read that never landed at all.
    """
    monkeypatch.setattr("time.sleep", lambda _s: None)

    # The Follows reply for the restart lands, then the process goes down.
    opened = scripted(monkeypatch, [
        [SUCCESS_LOGIN, FOLLOWS_RESTART],  # the command ran; process restarting
        [SUCCESS_LOGIN, FOLLOWS_VERSION],  # the verify poll: back up
    ])
    ami().restart(verify_timeout=5)
    assert len(opened) == 2

    # Same again when the connection dies before any reply reaches us.
    opened = scripted(monkeypatch, [
        [SUCCESS_LOGIN, b""],
        [SUCCESS_LOGIN, FOLLOWS_VERSION],
    ])
    ami().restart(verify_timeout=5)
    assert len(opened) == 2


def test_a_restart_that_never_comes_back_raises(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    scripted(monkeypatch, [
        [SUCCESS_LOGIN, FOLLOWS_RESTART],  # the restart went out
        [b""],                             # every verify poll finds it down
    ])
    with pytest.raises(AMIError) as refused:
        ami().restart(verify_timeout=0.1)
    assert "did not come back" in str(refused.value)
