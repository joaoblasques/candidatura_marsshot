"""Endpoint-hardening primitives for the public inbound webhook (U2).

Replay protection and rate limiting are kept here behind small interfaces. In
v1.0 they are in-memory; once the shared revise-state store lands (U7/U8) the
replay guard can be backed by it so protection holds across replicas.
"""

from __future__ import annotations

import time
from collections import deque


def resolve_client_ip(cf_connecting_ip: str | None, peer_host: str | None) -> str:
    """The rate-limit source IP: trust Cloudflare's ``CF-Connecting-IP`` header.

    Prod serves the webhook over a cloudflared tunnel to a loopback-bound app, so
    the socket peer is always ``127.0.0.1`` and would collapse every sender into
    one rate-limit bucket. ``CF-Connecting-IP`` carries the real client IP and is
    trustworthy here precisely because nothing but the tunnel can reach the app
    (it can't be spoofed by an outside caller). Falls back to the socket peer for
    dev/direct serving, then ``"unknown"`` so the key is never empty.
    """
    if cf_connecting_ip and cf_connecting_ip.strip():
        return cf_connecting_ip.strip()
    if peer_host:
        return peer_host
    return "unknown"


class ReplayGuard:
    """Rejects a provider event id seen within the recent window (replay defense).

    The window should cover the action/revise TTL (``state_ttl_seconds``): a body
    replayed while its original state is still actionable would otherwise re-run
    the pipeline. Memory is bounded by the count of distinct events within the
    window (per-call eviction drops anything older).
    """

    def __init__(self, window_seconds: int = 600) -> None:
        self._window = window_seconds
        self._seen: dict[str, float] = {}

    def is_replay(self, event_id: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        self._evict(now)
        if event_id in self._seen:
            return True
        self._seen[event_id] = now
        return False

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        stale = [k for k, t in self._seen.items() if t < cutoff]
        for k in stale:
            del self._seen[k]


class RateLimiter:
    """Fixed-window per-key request limiter (e.g. keyed by source/sender)."""

    def __init__(
        self, max_requests: int = 60, window_seconds: int = 60, max_keys: int = 10_000
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        # Per-source keying (real client IP) means idle senders leave empty
        # buckets behind; sweep them out once the table grows past the bound so
        # memory stays bounded by the count of *active* sources, not all-time.
        if len(self._hits) > self._max_keys:
            self._sweep(now)
        bucket = self._hits.setdefault(key, deque())
        cutoff = now - self._window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True

    def _sweep(self, now: float) -> None:
        """Drop buckets with no hits left in the current window (idle sources)."""
        cutoff = now - self._window
        stale = []
        for key, bucket in self._hits.items():
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if not bucket:
                stale.append(key)
        for key in stale:
            del self._hits[key]
