"""
Price Service
Fetches real-time token prices from CoinGecko's public API (no key required).
Results are cached per-token to avoid hitting rate limits (default TTL: 60s).

SOL/USD is always pre-cached since it is the most frequently needed price.
"""

import logging
import time
from typing import Dict, Optional
import requests

# CoinGecko public API — no key required for basic queries
_COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
_COINGECKO_TOKEN_URL = "https://api.coingecko.com/api/v3/simple/token_price/solana"

# Known CoinGecko IDs for common coins
_KNOWN_IDS: Dict[str, str] = {
    "solana": "solana",
    "sol":    "solana",
    "usdc":   "usd-coin",
    "usdt":   "tether",
    "btc":    "bitcoin",
    "eth":    "ethereum",
    "bonk":   "bonk",
    "wen":    "wen-4",
    "jto":    "jito-governance-token",
    "ray":    "raydium",
    "jup":    "jupiter-exchange-solana",
}

_DEFAULT_TTL = 60  # seconds


class PriceService:
    """
    Thread-safe in-memory price cache backed by CoinGecko.

    Usage:
        ps = PriceService()
        sol_usd = ps.get_sol_price()          # float
        price   = ps.get_token_price_usd(mint_address)  # float | None
    """

    def __init__(self, cache_ttl: int = _DEFAULT_TTL):
        self.cache_ttl = cache_ttl
        self.logger = logging.getLogger(__name__)
        # {cache_key: (price_usd, fetched_at)}
        self._cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_sol_price(self) -> float:
        """
        Return the current SOL/USD price.

        Returns:
            SOL price in USD, or 0.0 on failure.
        """
        cached = self._get_cached("sol")
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                _COINGECKO_PRICE_URL,
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=8,
            )
            if resp.status_code == 200:
                price = float(resp.json().get("solana", {}).get("usd", 0.0))
                self._set_cache("sol", price)
                return price
        except Exception as e:
            self.logger.warning(f"Failed to fetch SOL price: {e}")

        return 0.0

    def get_coin_price(self, symbol_or_id: str) -> float:
        """
        Fetch a known coin price by symbol or CoinGecko ID.

        Args:
            symbol_or_id: e.g. 'sol', 'usdc', 'bonk', or a CoinGecko id

        Returns:
            Price in USD or 0.0 on failure.
        """
        key = symbol_or_id.lower()
        cached = self._get_cached(key)
        if cached is not None:
            return cached

        cg_id = _KNOWN_IDS.get(key, key)
        try:
            resp = requests.get(
                _COINGECKO_PRICE_URL,
                params={"ids": cg_id, "vs_currencies": "usd"},
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                if cg_id in data:
                    price = float(data[cg_id].get("usd", 0.0))
                    self._set_cache(key, price)
                    return price
        except Exception as e:
            self.logger.warning(f"Failed to fetch price for {symbol_or_id}: {e}")

        return 0.0

    def get_token_price_usd(self, mint_address: str) -> Optional[float]:
        """
        Fetch the USD price of a Solana SPL token by its mint address.

        Uses CoinGecko's token_price endpoint for the Solana platform.

        Args:
            mint_address: SPL token mint address

        Returns:
            Price in USD or None if not found / error.
        """
        cached = self._get_cached(mint_address)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                _COINGECKO_TOKEN_URL,
                params={
                    "contract_addresses": mint_address,
                    "vs_currencies": "usd",
                },
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                # CoinGecko returns lowercase addresses as keys
                key_lower = mint_address.lower()
                for k, v in data.items():
                    if k.lower() == key_lower:
                        price = float(v.get("usd", 0.0))
                        self._set_cache(mint_address, price)
                        return price
        except Exception as e:
            self.logger.warning(f"Failed to fetch token price for {mint_address}: {e}")

        return None

    def convert_sol_to_usd(self, sol_amount: float) -> float:
        """
        Convert a SOL amount to USD using the live SOL price.

        Args:
            sol_amount: Amount in SOL

        Returns:
            Equivalent amount in USD.
        """
        return sol_amount * self.get_sol_price()

    def get_status(self) -> Dict:
        """Return service status and cache statistics."""
        now = time.time()
        valid_entries = sum(
            1 for _, (_, fetched_at) in self._cache.items()
            if now - fetched_at < self.cache_ttl
        )
        return {
            "cache_entries": len(self._cache),
            "valid_entries": valid_entries,
            "cache_ttl_seconds": self.cache_ttl,
        }

    # ------------------------------------------------------------------
    # Cache internals
    # ------------------------------------------------------------------

    def _get_cached(self, key: str) -> Optional[float]:
        """Return cached price if still fresh, else None."""
        entry = self._cache.get(key)
        if entry is not None:
            price, fetched_at = entry
            if time.time() - fetched_at < self.cache_ttl:
                return price
        return None

    def _set_cache(self, key: str, price: float) -> None:
        self._cache[key] = (price, time.time())
