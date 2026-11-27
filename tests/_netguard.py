"""Network denial for tests: while installed, any attempt to open a socket or resolve a name is recorded and fails.

The guard patches socket creation (`socket.socket`, `socket.socketpair`, `socket.fromfd`,
`socket.create_connection`, `socket.create_server`) and every name-resolution call, in both the
`socket` module and the `_socket` extension. `NetworkAttempt` derives from BaseException so that a
broad `except Exception` or `except OSError` in the code under test cannot swallow it, and every
attempt is also appended to `attempts` so a test can assert the list is empty.
"""

import _socket
import socket
from unittest import mock


class NetworkAttempt(BaseException):
    """Raised when code under test tries to use the network."""


class NetGuard:
    def __init__(self):
        self.attempts = []
        self._patches = []

    def _deny(self, name):
        def denied(*args, **kwargs):
            self.attempts.append(name)
            raise NetworkAttempt(name)

        return denied

    def install(self):
        targets = [
            (socket.socket, "__init__"),
            (socket, "socketpair"),
            (socket, "fromfd"),
            (socket, "create_connection"),
            (socket, "create_server"),
            (socket, "getaddrinfo"),
            (socket, "gethostbyname"),
            (socket, "gethostbyname_ex"),
            (socket, "gethostbyaddr"),
            (socket, "getnameinfo"),
            (_socket, "getaddrinfo"),
            (_socket, "gethostbyname"),
            (_socket, "gethostbyname_ex"),
            (_socket, "gethostbyaddr"),
            (_socket, "getnameinfo"),
            (_socket, "socketpair"),
        ]
        for obj, name in targets:
            patcher = mock.patch.object(obj, name, self._deny(f"{getattr(obj, '__name__', 'socket')}.{name}"))
            patcher.start()
            self._patches.append(patcher)
        return self

    def uninstall(self):
        while self._patches:
            self._patches.pop().stop()
