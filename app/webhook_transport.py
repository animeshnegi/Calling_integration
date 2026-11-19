"""Public-internet-only webhook transport, with DNS pinning and no redirects."""
import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool


def validate_url(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or len(url) > 1000
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if parsed.hostname.lower().rstrip('.') == 'localhost':
            raise ValueError()
        return parsed
    except ValueError:
        raise ValueError("Webhook URL must target a public HTTP or HTTPS destination") from None


class PinnedAdapter(HTTPAdapter):
    def __init__(self, parsed, address):
        self.parsed = parsed
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "https":
            self.pinned_pool = HTTPSConnectionPool(address, port, server_hostname=parsed.hostname,
                                                   assert_hostname=parsed.hostname, maxsize=1)
        else:
            self.pinned_pool = HTTPConnectionPool(address, port, maxsize=1)
        super().__init__(max_retries=0)

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        return self.pinned_pool

    def close(self):
        self.pinned_pool.close()
        super().close()


def post(url, **kwargs):
    parsed = validate_url(url)
    answers = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    addresses = {answer[4][0] for answer in answers}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Webhook DNS must resolve exclusively to public addresses")
    # Connect to the validated address, preserving hostname for Host, SNI and
    # certificate verification. Do not re-resolve DNS or inherit proxy settings.
    with requests.Session() as session:
        session.trust_env = False
        session.mount(parsed.scheme + "://", PinnedAdapter(parsed, sorted(addresses)[0]))
        headers = dict(kwargs.pop("headers", {}))
        headers["Host"] = parsed.netloc
        return session.post(url, headers=headers, allow_redirects=False, stream=True, **kwargs)
