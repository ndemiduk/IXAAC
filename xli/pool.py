"""Round-robin pool of API clients for swarm dispatch.

Primary client is always pool[0] — used for sync + main agent.
Workers call `acquire()` to get the next client; if more workers are running
than there are keys, keys are reused (gRPC + httpx clients are thread-safe).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from xli.client import Clients, MissingCredentials
from xli.config import GlobalConfig


@dataclass
class ClientPool:
    clients: list[Clients]
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next: int = 0

    @classmethod
    def from_config(cls, cfg: GlobalConfig, *, require_management: bool = True) -> "ClientPool":
        pairs = cfg.key_pairs()
        if not pairs:
            raise MissingCredentials("no API keys configured")
        clients = [
            Clients.from_keypair(p, require_management=require_management) for p in pairs
        ]
        return cls(clients=clients)

    def primary(self) -> Clients:
        return self.clients[0]

    def acquire(self) -> Clients:
        with self._lock:
            c = self.clients[self._next % len(self.clients)]
            self._next += 1
            return c

    def __len__(self) -> int:
        return len(self.clients)
