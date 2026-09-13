"""The only way this codebase makes an outbound request.

Every Phase 0 politeness rule is enforced here rather than in each adapter, so
a new adapter cannot forget one:

  * descriptive User-Agent carrying a contact address (checked at config load)
  * robots.txt consulted per origin, cached, re-checked after 24h
  * at least 10s between requests to the same origin, or the host's longer
    Crawl-delay
  * no UA rotation, no retry-with-different-headers, no rendering

There is no `verify=False`, no cookie jar and no credential parameter. Fetching
something behind a paywall is not a capability this client has.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from tracker.config import Config
from tracker.net.robots import RobotsPolicy, origin_of, robots_url_for

log = logging.getLogger(__name__)

ROBOTS_TTL = timedelta(hours=24)


class RobotsDisallowed(Exception):
    """Raised when robots.txt does not permit a fetch. Not an error condition."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    text: str
    fetched_at: datetime

    @property
    def content_hash(self) -> bytes:
        return hashlib.sha256(self.text.encode("utf-8")).digest()


@dataclass
class _OriginState:
    policy: RobotsPolicy | None = None
    policy_checked_at: datetime | None = None
    last_request_at: float | None = None


@dataclass
class PoliteClient:
    """Rate-limited, robots-respecting HTTP client.

    Rate limiting is per origin rather than per source row, because two sources
    on the same host are still one host being polled.
    """

    config: Config = field(default_factory=Config.load)
    _client: httpx.Client | None = field(default=None, repr=False)
    _origins: dict[str, _OriginState] = field(default_factory=dict, repr=False)
    # Injectable so tests do not sleep.
    _sleep: object = field(default=time.sleep, repr=False)
    _now: object = field(default=time.monotonic, repr=False)

    def __post_init__(self) -> None:
        if self._client is None:
            self._client = httpx.Client(
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "application/rss+xml, application/atom+xml, "
                    "application/xml;q=0.9, text/html;q=0.8",
                },
                timeout=httpx.Timeout(20.0),
                follow_redirects=True,
                max_redirects=5,
            )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- politeness --------------------------------------------------------

    def _state(self, origin: str) -> _OriginState:
        return self._origins.setdefault(origin, _OriginState())

    def robots_policy(self, url: str) -> RobotsPolicy:
        """Fetch and cache robots.txt for this origin."""
        origin = origin_of(url)
        state = self._state(origin)
        now = datetime.now(UTC)

        fresh = (
            state.policy is not None
            and state.policy_checked_at is not None
            and now - state.policy_checked_at < ROBOTS_TTL
        )
        if fresh:
            return state.policy  # type: ignore[return-value]

        # The robots.txt fetch is itself rate limited: it is a request to the
        # same host as everything else.
        self._wait_for_slot(origin, crawl_delay=None)
        try:
            response = self._client.get(robots_url_for(url))  # type: ignore[union-attr]
            state.last_request_at = self._now()  # type: ignore[operator]
            if response.status_code == 404:
                # No robots.txt is a genuine absence of restrictions, unlike a
                # failure to retrieve one.
                policy = RobotsPolicy.parse(origin, "", self.config.user_agent)
            elif response.status_code >= 400:
                policy = RobotsPolicy.unavailable(origin)
            else:
                policy = RobotsPolicy.parse(origin, response.text, self.config.user_agent)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable for %s (%s); treating as disallow", origin, exc)
            policy = RobotsPolicy.unavailable(origin)

        state.policy = policy
        state.policy_checked_at = now
        return policy

    def _wait_for_slot(self, origin: str, *, crawl_delay: int | None) -> None:
        state = self._state(origin)
        if state.last_request_at is None:
            return
        interval = max(self.config.min_request_interval_seconds, crawl_delay or 0)
        elapsed = self._now() - state.last_request_at  # type: ignore[operator]
        if elapsed < interval:
            self._sleep(interval - elapsed)  # type: ignore[operator]

    # -- fetching ----------------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """Fetch a URL, or raise RobotsDisallowed if we may not."""
        origin = origin_of(url)
        policy = self.robots_policy(url)

        if not policy.allows(url, self.config.user_agent):
            reason = "robots.txt unreachable" if not policy.fetched else "disallowed by robots.txt"
            raise RobotsDisallowed(f"{url}: {reason}")

        self._wait_for_slot(origin, crawl_delay=policy.crawl_delay_seconds)
        response = self._client.get(url)  # type: ignore[union-attr]
        self._state(origin).last_request_at = self._now()  # type: ignore[operator]
        response.raise_for_status()

        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            text=response.text,
            fetched_at=datetime.now(UTC),
        )
