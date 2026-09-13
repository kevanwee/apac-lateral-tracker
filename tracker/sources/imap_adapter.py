"""IMAP adapter — newsletters you subscribed to, read from your own mailbox.

This is the lawful-access route to outlets that block automated clients. ALB
returns 403 to our crawler on every path including robots.txt, so we do not
crawl it. But if you subscribe to their newsletter, they send the content to
you, deliberately. Reading your own inbox is not circumvention; it is reading
mail that was addressed to you.

## Scope, deliberately narrow

This adapter reads a **named folder**, filtered to a **named sender allowlist**,
**read-only** (`readonly=True`, so nothing is marked read, moved or deleted).
It cannot see the rest of your mailbox, and it has no code path that writes.

Set it up by filtering the newsletter into its own folder at your mail
provider, then pointing `options.folder` at it. If the folder is wrong the
adapter yields nothing; it never falls back to INBOX.

## What is stored

The same as any other source: URL, headline, date, outlet. Newsletter bodies
are parsed in memory for the links and headlines they contain, then discarded.
No message body, no sender address, no recipient, no message-id beyond a
content hash. Phase 0 section 2 applies here exactly as it does to a feed.

## Credentials

`IMAP_PASSWORD` must be an app-specific password, not your account password,
and the account should ideally be a dedicated address that only receives these
newsletters. See .env.example.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from tracker.sources.base import RawItem, SourceConfig

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# Links in a newsletter body, with the anchor text that labels them.
_ANCHOR_RE = re.compile(
    r'<a\b[^>]*href=["\'](?P<href>https?://[^"\']+)["\'][^>]*>(?P<text>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# Tracking and housekeeping links that are never articles.
_SKIP_URL = re.compile(
    r"(unsubscribe|preferences|privacy|mailto:|/subscribe|twitter\.com|linkedin\.com"
    r"|facebook\.com|\.(png|jpg|gif|css|js)(\?|$))",
    re.IGNORECASE,
)
_MIN_ANCHOR_WORDS = 4


class ImapConfigError(RuntimeError):
    pass


def clean(text: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def strip_tracking(url: str) -> str:
    """Drop campaign parameters so the same article dedupes on its URL."""
    base, _, query = url.partition("?")
    if not query:
        return base
    keep = [
        p for p in query.split("&")
        if p and not re.match(r"(utm_|mc_|_hs|ck_|elq|mkt_tok|trk)", p, re.IGNORECASE)
    ]
    return f"{base}?{'&'.join(keep)}" if keep else base


@dataclass
class ImapAdapter:
    """Reads article links out of newsletters in one mailbox folder.

    options:
      host            IMAP host, e.g. imap.gmail.com
      folder          the folder the newsletter is filtered into (required)
      from_allowlist  list of sender substrings; anything else is ignored
      url_pattern     only links matching this regex are treated as articles
      max_messages    ceiling for one run
    """

    config: SourceConfig
    username: str
    password: str

    def fetch(self, *, since: datetime | None = None) -> Iterable[RawItem]:
        opts = self.config.options
        host = opts.get("host")
        folder = opts.get("folder")
        if not host or not folder:
            raise ImapConfigError(
                f"{self.config.slug}: imap adapter needs options.host and options.folder"
            )

        allow = [a.lower() for a in opts.get("from_allowlist", [])]
        if not allow:
            raise ImapConfigError(
                f"{self.config.slug}: imap adapter needs options.from_allowlist; "
                f"an unfiltered mailbox read is not in scope"
            )
        url_pattern = re.compile(opts["url_pattern"]) if opts.get("url_pattern") else None
        max_messages = int(opts.get("max_messages", 500))

        items: dict[str, RawItem] = {}
        with self._connect(host) as imap:
            # readonly: this adapter never marks, moves or deletes anything.
            status, _ = imap.select(folder, readonly=True)
            if status != "OK":
                raise ImapConfigError(
                    f"{self.config.slug}: cannot open folder {folder!r} "
                    f"(it is not created automatically)"
                )

            for num in self._search(imap, since)[:max_messages]:
                status, data = imap.fetch(num, "(RFC822)")
                if status != "OK" or not data or not isinstance(data[0], tuple):
                    continue
                message = email.message_from_bytes(data[0][1])
                if not self._sender_allowed(message, allow):
                    continue
                for item in self._items_from(message, url_pattern, since):
                    items.setdefault(item.url, item)

        return list(items.values())

    # -- internals ---------------------------------------------------------

    def _connect(self, host: str) -> imaplib.IMAP4_SSL:
        imap = imaplib.IMAP4_SSL(host)
        try:
            imap.login(self.username, self.password)
        except imaplib.IMAP4.error as exc:
            raise ImapConfigError(
                f"{self.config.slug}: IMAP login failed. Use an app-specific "
                f"password, not the account password. ({exc})"
            ) from exc
        return imap

    @staticmethod
    def _search(imap: imaplib.IMAP4_SSL, since: datetime | None) -> list[bytes]:
        criteria = ["ALL"]
        if since is not None:
            criteria = ["SINCE", since.strftime("%d-%b-%Y")]
        status, data = imap.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    @staticmethod
    def _sender_allowed(message: Message, allow: list[str]) -> bool:
        sender = (message.get("From") or "").lower()
        return any(a in sender for a in allow)

    def _items_from(
        self, message: Message, url_pattern: re.Pattern[str] | None, since: datetime | None
    ) -> Iterable[RawItem]:
        sent = self._sent_at(message)
        if since is not None and sent < since:
            return

        for part in message.walk():
            if part.get_content_type() != "text/html":
                continue
            try:
                body = part.get_payload(decode=True).decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            except (AttributeError, LookupError):
                continue

            for match in _ANCHOR_RE.finditer(body):
                url = strip_tracking(match.group("href").strip())
                if _SKIP_URL.search(url):
                    continue
                if url_pattern and not url_pattern.search(url):
                    continue
                headline = clean(match.group("text"))
                # Anchors like "Read more" or a bare image are not headlines.
                if len(headline.split()) < _MIN_ANCHOR_WORDS:
                    continue
                yield RawItem(
                    source_slug=self.config.slug,
                    url=url,
                    headline=headline,
                    # The send date, not the publication date. Close enough to
                    # bucket by, and flagged so nothing treats it as exact.
                    published_at=sent,
                    published_at_is_estimated=True,
                    access_level=self.config.default_access_level,
                    body_text=None,
                )

    @staticmethod
    def _sent_at(message: Message) -> datetime:
        raw = message.get("Date")
        if raw:
            try:
                parsed = parsedate_to_datetime(raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed
            except (TypeError, ValueError):
                pass
        return datetime.now(UTC)

    @staticmethod
    def _subject(message: Message) -> str:
        raw = message.get("Subject")
        if not raw:
            return ""
        try:
            return str(make_header(decode_header(raw)))
        except (UnicodeDecodeError, LookupError):
            return raw
