"""HTTP access to public registries: retries, a per-host concurrency limit and a disk cache.

A cached response is the registry's own answer to the same request, saved with its fetch
time. It is not a term mapping: which candidate is used is decided in each run. Every
response says whether it was "live" or "cache".
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 4
MAX_RETRY_AFTER_SECONDS = 30.0
PER_HOST_CONCURRENCY = 4
USER_AGENT = "pmatch-ctml/0.1 (clinical trial protocol to CTML)"


class RegistryError(RuntimeError):
    """A registry request failed after retries."""


def urllib_get(url: str, params: dict | None, timeout: float) -> tuple[int, bytes]:
    """(status, body) with the standard library client. Some registry firewalls refuse the
    httpx client with 403 but accept this one (ClinicalTrials.gov has done so); proxies set in
    HTTPS_PROXY are used."""
    full = f"{url}?{urlencode(params)}" if params else url
    request = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b""


class RegistryHttp:
    def __init__(
        self,
        timeout_seconds: float = 30.0,
        cache_dir: Path | None = None,
        cache_days: float = 7.0,
        transport: httpx.AsyncBaseTransport | None = None,
        fallback: Callable[[str, dict | None, float], tuple[int, bytes]] | str | None = "default",
    ) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        self._cache_dir = cache_dir
        self._cache_seconds = cache_days * 86400
        self._timeout = timeout_seconds
        # The standard library fallback reaches the network: never with a test transport unless
        # a test passes its own fallback.
        if fallback == "default":
            fallback = urllib_get if transport is None else None
        self._fallback = fallback
        self._limits: dict[str, asyncio.Semaphore] = {}
        self.live_calls = 0
        self.cache_hits = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    def _cache_path(self, url: str, params: dict | None) -> Path | None:
        if self._cache_dir is None:
            return None
        key = json.dumps({"url": url, "params": params or {}}, sort_keys=True)
        return self._cache_dir / "registry" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _read_cache(self, path: Path | None) -> Any | None:
        if path is None or not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - float(record.get("fetched_at", 0)) > self._cache_seconds:
            return None
        return record.get("data")

    def _write_cache(self, path: Path | None, url: str, params: dict | None, data: Any) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"url": url, "params": params or {}, "fetched_at": time.time(), "data": data}
        path.write_text(json.dumps(record), encoding="utf-8")

    async def get_json(
        self,
        url: str,
        params: dict | None = None,
        use_cache: bool = True,
        not_found_ok: bool = False,
    ) -> tuple[Any, str]:
        """(data, "live" or "cache"). A 404 returns (None, "live") when not_found_ok."""
        path = self._cache_path(url, params) if use_cache else None
        cached = self._read_cache(path)
        if cached is not None:
            self.cache_hits += 1
            return cached, "cache"
        host = urlsplit(url).netloc
        limit = self._limits.setdefault(host, asyncio.Semaphore(PER_HOST_CONCURRENCY))
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            async with limit:
                try:
                    response = await self._client.get(url, params=params)
                except httpx.TransportError as error:
                    last_error = f"{type(error).__name__}: {error}"
                    response = None
            if response is not None:
                if response.status_code == 404 and not_found_ok:
                    self.live_calls += 1
                    return None, "live"
                if response.status_code < 400:
                    self.live_calls += 1
                    try:
                        data = response.json()
                    except ValueError as error:
                        raise RegistryError(f"{host}: the response is not JSON ({error})") from error
                    self._write_cache(path, url, params, data)
                    return data, "live"
                last_error = f"HTTP {response.status_code}"
                if response.status_code == 403 and self._fallback is not None:
                    data = await self._fallback_json(url, params, host)
                    if data is not None:
                        self._write_cache(path, url, params, data)
                        return data, "live"
                    last_error = "HTTP 403 (also refused to the standard library client)"
                if response.status_code not in RETRY_STATUS:
                    break
            if attempt < MAX_ATTEMPTS:
                delay = 2.0 ** (attempt - 1)
                if response is not None and response.headers.get("Retry-After", "").isdigit():
                    delay = min(float(response.headers["Retry-After"]), MAX_RETRY_AFTER_SECONDS)
                logger.debug("Retrying %s after %s (%.0fs)", host, last_error, delay)
                await asyncio.sleep(delay)
        raise RegistryError(f"{host}: {last_error}")

    async def _fallback_json(self, url: str, params: dict | None, host: str) -> Any | None:
        try:
            status, body = await asyncio.to_thread(self._fallback, url, params, self._timeout)
        except (OSError, http.client.HTTPException) as error:
            logger.debug("Fallback request to %s failed: %s", host, error)
            return None
        if status >= 400:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            return None
        self.live_calls += 1
        logger.info("%s refused httpx (403); the standard library client succeeded", host)
        return data
