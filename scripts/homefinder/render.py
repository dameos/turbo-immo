"""Template filling and asset embedding.

The templates are plain HTML with `{{NAME}}` holes, so they stay editable
without touching Python. Values are HTML-escaped by default; `{{NAME|raw}}`
opts out for fragments this code built itself.
"""

from __future__ import annotations

import base64
import html
import re
from dataclasses import dataclass

from .fetch import TTL_ASSET

_HOLE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*(\|raw)?\s*\}\}")

_MAGIC = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
]


def render(template: str, ctx: dict) -> str:
    """Substitute every {{HOLE}}. Unknown holes raise rather than render blank --
    a silently empty card is worse than a loud failure."""
    missing = []

    def sub(m):
        name, raw = m.group(1), m.group(2)
        if name not in ctx:
            missing.append(name)
            return m.group(0)
        value = ctx[name]
        if value is None:
            return ""
        return str(value) if raw else html.escape(str(value))

    out = _HOLE.sub(sub, template)
    if missing:
        raise KeyError("template placeholders not provided: %s"
                       % ", ".join(sorted(set(missing))))
    return out


def sniff_mime(data: bytes) -> str:
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def data_uri(data: bytes) -> str:
    return "data:%s;base64,%s" % (sniff_mime(data),
                                  base64.b64encode(data).decode("ascii"))


@dataclass
class EmbedStats:
    embedded: int = 0
    failed: int = 0
    bytes: int = 0


class Embedder:
    """Downloads an asset once and hands back a data: URI.

    Deduplicates by URL, which matters a lot for map tiles: neighbouring
    listings in one postcode share most of their tiles, so the same PNG would
    otherwise be inlined dozens of times.
    """

    def __init__(self, fetcher, enabled: bool = True):
        self.fetcher = fetcher
        self.enabled = enabled
        self.stats = EmbedStats()
        self._cache: dict[str, str | None] = {}

    def uri(self, url: str, reject=None) -> str | None:
        if not url:
            return None
        if not self.enabled:
            return url
        if url in self._cache:
            return self._cache[url]
        try:
            data = self.fetcher.get(url, ttl=TTL_ASSET, reject=reject)
            uri = data_uri(data)
            self.stats.embedded += 1
            self.stats.bytes += len(data)
        except Exception:
            self.stats.failed += 1
            uri = None
        self._cache[url] = uri
        return uri


class TileRegistry:
    """Emits each unique map tile once, as a CSS class.

    Also the last line of defence against placeholder tiles: `reject` catches
    known provider error images, and `suspect_uniform` catches the general case
    of a provider returning one identical image for every coordinate.
    """

    def __init__(self, embedder: Embedder, reject=None):
        self.embedder = embedder
        self.reject = reject
        self.rejected = 0
        self._classes: dict[str, str | None] = {}
        self._rules: dict[str, str] = {}

    def class_for(self, url: str) -> str | None:
        if url not in self._classes:
            before = self.embedder.stats.failed
            uri = self.embedder.uri(url, reject=self.reject)
            if uri is None and self.embedder.stats.failed > before:
                self.rejected += 1
            cls = ("t%d" % len(self._rules)) if uri else None
            self._classes[url] = cls
            if cls:
                self._rules[cls] = uri
        return self._classes[url]

    def suspect_uniform(self) -> bool:
        """True when several distinct tile coordinates returned byte-identical
        images -- real map tiles at different coordinates never are."""
        distinct = {uri for uri in self._rules.values()}
        return len(self._rules) >= 3 and len(distinct) == 1

    def css(self) -> str:
        return "\n".join(".%s{background-image:url(%s)}" % (cls, uri)
                         for cls, uri in self._rules.items())
