from __future__ import annotations

import os
from threading import Lock
from typing import Any, Callable
from urllib.parse import urljoin


class TunnelError(RuntimeError):
    pass


def launch_cloudflare(port: int) -> Any:
    from pycloudflared import try_cloudflare

    os.environ.setdefault("PYCLOUDFLARED_LINES_TO_CHECK", "300")
    return try_cloudflare(port=port, verbose=False)


def terminate_cloudflare(port: int) -> None:
    from pycloudflared import try_cloudflare

    try_cloudflare.terminate(port)


class TunnelManager:
    def __init__(
        self,
        port: int,
        launcher: Callable[[int], Any] | None = None,
        terminator: Callable[[int], None] | None = None,
    ) -> None:
        self.port = port
        self._launcher = launcher or launch_cloudflare
        self._terminator = terminator or terminate_cloudflare
        self._lock = Lock()
        self._public_url = ""
        self._leases = 0

    @property
    def public_url(self) -> str:
        return self._public_url

    @property
    def active(self) -> bool:
        return bool(self._public_url)

    def acquire(self) -> str:
        with self._lock:
            if not self._public_url:
                try:
                    result = self._launcher(self.port)
                    public_url = result.tunnel if hasattr(result, "tunnel") else result[0]
                except Exception as error:
                    raise TunnelError(f"Relay could not start its local call tunnel: {error}") from error
                if not str(public_url).startswith("https://"):
                    raise TunnelError("The local call tunnel did not return an HTTPS URL.")
                self._public_url = str(public_url).rstrip("/")
            self._leases += 1
            return self._public_url

    def url(self, path: str, query: str = "") -> str:
        if not self._public_url:
            raise TunnelError("The local call tunnel is not active.")
        result = urljoin(f"{self._public_url}/", path.lstrip("/"))
        return f"{result}?{query}" if query else result

    def release(self) -> None:
        with self._lock:
            if self._leases:
                self._leases -= 1
            if self._leases == 0:
                self._stop_locked()

    def stop(self) -> None:
        with self._lock:
            self._leases = 0
            self._stop_locked()

    def _stop_locked(self) -> None:
        if not self._public_url:
            return
        try:
            self._terminator(self.port)
        except (OSError, ValueError):
            pass
        finally:
            self._public_url = ""
