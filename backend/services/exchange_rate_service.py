"""Exchange rate service – auto-updates KRW/USD rate from public APIs.

Fetches the KRW/USD exchange rate periodically and caches it.
Falls back to environment variable KRW_USD_RATE if API calls fail.

Uses free public APIs (no API key needed):
  1. exchangerate-api.com (primary)
  2. open.er-api.com (fallback)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Default fallback rate if all API calls fail and no cached rate exists
_DEFAULT_RATE = 1350.0

# Update interval: every 6 hours (rates don't change that fast)
_UPDATE_INTERVAL_SECONDS = 6 * 60 * 60

# Cache file for persistence across restarts
_CACHE_FILENAME = "exchange_rate_cache.json"


class ExchangeRateService:
    """Fetches and caches KRW/USD exchange rate."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._rate: float = _DEFAULT_RATE
        self._last_updated: float = 0.0
        self._source: str = "default"
        self._timer: threading.Timer | None = None
        self._running = False

        # Load cached rate from disk
        self._load_cache()

        # Override with env var if set and no fresh cache exists
        env_rate = os.environ.get("KRW_USD_RATE", "")
        if env_rate and self._source == "default":
            try:
                self._rate = float(env_rate)
                self._source = "env"
            except ValueError:
                pass

    @property
    def rate(self) -> float:
        """Current KRW per 1 USD."""
        with self._lock:
            return self._rate

    @property
    def info(self) -> dict:
        """Current rate with metadata."""
        with self._lock:
            return {
                "rate": self._rate,
                "last_updated": self._last_updated,
                "source": self._source,
                "age_seconds": int(time.time() - self._last_updated) if self._last_updated else None,
            }

    def start(self) -> None:
        """Start the background update loop."""
        if self._running:
            return
        self._running = True
        logger.info("Exchange rate service starting (current rate: %.2f from %s)", self._rate, self._source)
        # Do initial fetch in background thread
        t = threading.Thread(target=self._fetch_and_schedule, daemon=True)
        t.start()

    def stop(self) -> None:
        """Stop the background update loop."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def force_update(self) -> dict:
        """Force an immediate rate update. Returns the new rate info."""
        self._do_fetch()
        return self.info

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    def _fetch_and_schedule(self) -> None:
        """Fetch rate and schedule next update."""
        self._do_fetch()
        if self._running:
            self._timer = threading.Timer(_UPDATE_INTERVAL_SECONDS, self._fetch_and_schedule)
            self._timer.daemon = True
            self._timer.start()

    def _do_fetch(self) -> None:
        """Try multiple APIs to get the current KRW/USD rate."""
        fetchers = [
            self._fetch_open_er_api,
            self._fetch_exchangerate_api,
        ]
        for fetcher in fetchers:
            try:
                rate, source = fetcher()
                if rate and rate > 0:
                    with self._lock:
                        self._rate = round(rate, 2)
                        self._last_updated = time.time()
                        self._source = source
                    self._save_cache()
                    # Also update the env var so payment_service picks it up
                    os.environ["KRW_USD_RATE"] = str(self._rate)
                    logger.info("Exchange rate updated: 1 USD = %.2f KRW (source: %s)", self._rate, source)
                    return
            except Exception as exc:
                logger.warning("Exchange rate fetch failed (%s): %s", fetcher.__name__, exc)

        logger.warning("All exchange rate APIs failed, keeping current rate: %.2f", self._rate)

    def _fetch_open_er_api(self) -> tuple[float, str]:
        """Fetch from open.er-api.com (primary, free, no key)."""
        resp = httpx.get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") == "success":
            krw = data.get("rates", {}).get("KRW")
            if krw:
                return float(krw), "open.er-api.com"
        raise ValueError("Unexpected response format")

    def _fetch_exchangerate_api(self) -> tuple[float, str]:
        """Fetch from exchangerate-api.com (fallback, free, no key)."""
        resp = httpx.get(
            "https://api.exchangerate-api.com/v4/latest/USD",
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        krw = data.get("rates", {}).get("KRW")
        if krw:
            return float(krw), "exchangerate-api.com"
        raise ValueError("KRW rate not found in response")

    # ------------------------------------------------------------------
    # Cache persistence
    # ------------------------------------------------------------------

    def _save_cache(self) -> None:
        try:
            path = self.data_dir / _CACHE_FILENAME
            with self._lock:
                data = {
                    "rate": self._rate,
                    "last_updated": self._last_updated,
                    "source": self._source,
                }
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
        except Exception as exc:
            logger.warning("Failed to save exchange rate cache: %s", exc)

    def _load_cache(self) -> None:
        try:
            path = self.data_dir / _CACHE_FILENAME
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rate = data.get("rate", 0)
            updated = data.get("last_updated", 0)
            source = data.get("source", "cache")
            if rate > 0:
                # Accept cache if less than 24 hours old
                age = time.time() - updated
                if age < 24 * 60 * 60:
                    self._rate = rate
                    self._last_updated = updated
                    self._source = f"cache ({source})"
                    logger.info("Loaded cached exchange rate: %.2f (age: %.0f min)", rate, age / 60)
        except Exception as exc:
            logger.warning("Failed to load exchange rate cache: %s", exc)
