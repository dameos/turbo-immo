"""Polite, cached HTTP. Stdlib only.

Everything that touches the network goes through `Fetcher` so that throttling,
retries and caching are enforced in one place rather than per adapter.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Assets are content-addressed by URL and never change; search pages go stale.
TTL_SEARCH = 6 * 3600
TTL_ASSET = 365 * 24 * 3600


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "find-homes" / "cache"


class FetchError(Exception):
    pass


class Fetcher:
    # OpenStreetMap's tile server is a donated community resource with an
    # explicit "no bulk downloading" policy. Cached tiles mean a repeat report
    # costs nothing; the first one stays deliberately slow.
    HOST_DELAY = {"tile.openstreetmap.org": 1.0}

    # OSM's tile usage policy REQUIRES a User-Agent identifying the application
    # and forbids impersonating a browser. Sending the browser UA above gets
    # every tile replaced by a "418 Access blocked" image -- served with HTTP
    # 200, so it looks like success unless you check the bytes.
    HOST_UA = {
        "tile.openstreetmap.org":
            "find-homes/1.0 (personal property report generator; stdlib urllib)",
    }

    def __init__(self, cache_dir: Path | None = None, delay: float = 1.0,
                 timeout: int = 30, retries: int = 3, offline: bool = False,
                 verbose: bool = True):
        self.cache_dir = Path(cache_dir or default_cache_dir())
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.offline = offline
        self.verbose = verbose
        self._last_hit: dict[str, float] = {}
        self.stats = {"hits": 0, "misses": 0, "errors": 0}

    # -- cache ------------------------------------------------------------

    def _path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        return self.cache_dir / h[:2] / (h + ".bin")

    def _read_cache(self, url: str, ttl: int) -> bytes | None:
        p = self._path(url)
        if not p.exists():
            return None
        if ttl >= 0 and (time.time() - p.stat().st_mtime) > ttl:
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    def _write_cache(self, url: str, body: bytes) -> None:
        p = self._path(url)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_bytes(body)
            os.replace(tmp, p)
        except OSError:
            pass  # a broken cache must never break a search

    # -- network ----------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = max(self.delay, self.HOST_DELAY.get(host, 0.0))
        last = self._last_hit.get(host)
        if last is not None:
            wait = delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.time()

    def get(self, url: str, headers: dict | None = None,
            ttl: int = TTL_SEARCH, reject=None) -> bytes:
        """Fetch `url`, using the disk cache when fresh.

        `reject(body) -> bool` marks a response as unusable even though the
        server called it a success. A rejected *cached* body is re-fetched once
        rather than trusted, so a cache poisoned by an earlier bad run heals
        itself instead of persisting for the asset TTL of a year.
        """
        cached = self._read_cache(url, ttl)
        if cached is not None and not (reject and reject(cached)):
            self.stats["hits"] += 1
            return cached
        if self.offline:
            raise FetchError("offline and not cached: %s" % url)

        host = urlsplit(url).netloc
        hdrs = {"User-Agent": self.HOST_UA.get(host, UA),
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8"}
        hdrs.update(headers or {})

        last_err = None
        for attempt in range(self.retries):
            self._throttle(url)
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                if reject and reject(body):
                    # Deliberately not cached, and not retried: a policy block
                    # is permanent until the request itself changes, and caching
                    # it is how the blocked-tile bug survived a re-run.
                    self.stats["errors"] += 1
                    raise FetchError("%s -> server returned unusable content "
                                     "(policy block or placeholder)" % url)
                self.stats["misses"] += 1
                self._write_cache(url, body)
                return body
            except FetchError:
                raise            # rejected content: permanent, don't retry
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (404, 410):
                    break        # not transient, don't burn retries
            except Exception as e:  # timeouts, DNS, connection resets
                last_err = e
            if attempt < self.retries - 1:
                time.sleep(1.5 * (2 ** attempt))

        self.stats["errors"] += 1
        raise FetchError("%s -> %s" % (url, last_err))

    def get_text(self, url: str, **kw) -> str:
        return self.get(url, **kw).decode("utf-8", "replace")

    def get_json(self, url: str, **kw):
        kw.setdefault("headers", {})["Accept"] = "application/json"
        return json.loads(self.get(url, **kw).decode("utf-8", "replace"))

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)
