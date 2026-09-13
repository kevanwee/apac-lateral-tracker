"""robots.txt parsing and caching.

Phase 0, section 1. Two rules that differ from the stdlib default and matter:

  * A robots.txt we could not retrieve is treated as disallow, not as
    permission. `urllib.robotparser` does the opposite.
  * A Crawl-delay longer than our own floor wins. The floor is a floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser


@dataclass(frozen=True)
class RobotsPolicy:
    """What robots.txt permits for one origin."""

    origin: str
    fetched: bool
    crawl_delay_seconds: int | None
    _parser: RobotFileParser | None

    def allows(self, url: str, user_agent: str) -> bool:
        # Could not read robots.txt: assume we are not welcome.
        if not self.fetched or self._parser is None:
            return False
        return self._parser.can_fetch(user_agent, url)

    @classmethod
    def unavailable(cls, origin: str) -> RobotsPolicy:
        return cls(origin=origin, fetched=False, crawl_delay_seconds=None, _parser=None)

    @classmethod
    def parse(cls, origin: str, body: str, user_agent: str) -> RobotsPolicy:
        parser = RobotFileParser()
        parser.parse(body.splitlines())

        delay = parser.crawl_delay(user_agent)
        # Some hosts only declare a delay for the wildcard agent.
        if delay is None:
            delay = parser.crawl_delay("*")

        return cls(
            origin=origin,
            fetched=True,
            crawl_delay_seconds=int(delay) if delay is not None else None,
            _parser=parser,
        )


def origin_of(url: str) -> str:
    parts = urlparse(url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"not an absolute URL: {url!r}")
    return f"{parts.scheme}://{parts.netloc}"


def robots_url_for(url: str) -> str:
    return f"{origin_of(url)}/robots.txt"
