"""Network policy interface. Closed by default in M2; no tool opens it yet."""

from __future__ import annotations

from urllib.parse import urlsplit

from atlas_harness.kernel.clock import Clock, SystemClock
from atlas_harness.kernel.errors import PolicyDeniedError

ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_METHODS: tuple[str, ...] = ("GET", "HEAD")


class NetworkPolicy:
    """Decide whether an outbound request may be made.

    The interface exists so later milestones cannot bolt network access on
    without passing a policy; with ``enabled=False`` every call is refused.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        allowed_hosts: tuple[str, ...] = (),
        allowed_methods: tuple[str, ...] = DEFAULT_METHODS,
        max_requests_per_minute: int = 60,
        clock: Clock | None = None,
    ) -> None:
        self.enabled = enabled
        self.allowed_hosts = tuple(host.lower().lstrip(".") for host in allowed_hosts)
        self.allowed_methods = frozenset(method.upper() for method in allowed_methods)
        self.max_requests_per_minute = max_requests_per_minute
        self.clock: Clock = clock or SystemClock()
        self._recent: list[int] = []

    def host_allowed(self, host: str) -> bool:
        candidate = host.lower()
        return any(
            candidate == allowed or candidate.endswith(f".{allowed}")
            for allowed in self.allowed_hosts
        )

    def check(self, url: str, *, method: str = "GET") -> None:
        if not self.enabled:
            raise PolicyDeniedError(
                "network access is disabled",
                details={"rule": "network_disabled", "url": url},
            )
        parts = urlsplit(url)
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            raise PolicyDeniedError(
                "unsupported url scheme",
                details={"rule": "network_scheme", "scheme": parts.scheme},
            )
        host = parts.hostname or ""
        if not host:
            raise PolicyDeniedError(
                "url has no host",
                details={"rule": "network_host", "url": url},
            )
        if method.upper() not in self.allowed_methods:
            raise PolicyDeniedError(
                "http method is not allowed",
                details={"rule": "network_method", "method": method.upper()},
            )
        if not self.host_allowed(host):
            raise PolicyDeniedError(
                "host is not on the network allowlist",
                details={
                    "rule": "network_host_not_allowlisted",
                    "host": host,
                    "allowed": list(self.allowed_hosts),
                },
            )
        self._consume_rate(url)

    def _consume_rate(self, url: str) -> None:
        now = self.clock.now_ms()
        cutoff = now - 60_000
        self._recent = [stamp for stamp in self._recent if stamp > cutoff]
        if len(self._recent) >= self.max_requests_per_minute:
            raise PolicyDeniedError(
                "network rate limit exceeded",
                details={
                    "rule": "network_rate_limit",
                    "limit": self.max_requests_per_minute,
                    "url": url,
                },
            )
        self._recent.append(now)
