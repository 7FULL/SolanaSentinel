"""
DexScreener Service
Fetches real-time market data (price, liquidity, volume) for Solana tokens
from the DexScreener public API. No API key required.
"""

import logging
import time
from typing import Optional, Dict
import requests

# DexScreener public API — no authentication needed
_BASE_URL = "https://api.dexscreener.com/latest/dex"
_TIMEOUT = 8  # seconds per HTTP request


class DexScreenerService:
    """
    Fetches market data for Solana tokens from DexScreener.

    DexScreener indexes new pools within ~30 seconds of creation, so this
    service retries a configurable number of times before giving up on a
    brand-new token.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "SolanaSentinel/1.0"})

    def get_token_data(
        self,
        mint_address: str,
        retries: int = 3,
        retry_delay: float = 5.0,
    ) -> Optional[Dict]:
        """
        Fetch market data for a token by mint address.

        Retries because brand-new tokens may not yet be indexed.

        Args:
            mint_address: SPL token mint address (base58)
            retries: Number of attempts before giving up
            retry_delay: Seconds to wait between retries

        Returns:
            Dict with liquidity, price, volume, name, symbol, dex_id, pair_address;
            or None if not found / API error.
        """
        url = f"{_BASE_URL}/tokens/{mint_address}"

        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, timeout=_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()

                pairs = data.get("pairs") or []
                if not pairs:
                    if attempt < retries:
                        self.logger.debug(
                            f"DexScreener: no pairs for {mint_address[:12]}… "
                            f"(attempt {attempt}/{retries}), retrying in {retry_delay}s"
                        )
                        time.sleep(retry_delay)
                        continue
                    return None

                # Pick the pair with the highest liquidity (most representative)
                best = max(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)

                # Determine which side of the pair is our token
                base_addr = (best.get("baseToken") or {}).get("address", "")
                if base_addr.lower() == mint_address.lower():
                    token_meta = best.get("baseToken", {})
                else:
                    token_meta = best.get("quoteToken", {})

                liquidity_usd = (best.get("liquidity") or {}).get("usd", 0) or 0
                volume_h1     = (best.get("volume") or {}).get("h1", 0) or 0
                volume_h24    = (best.get("volume") or {}).get("h24", 0) or 0
                price_usd     = float(best.get("priceUsd") or 0)
                fdv           = best.get("fdv") or 0
                market_cap    = best.get("marketCap") or fdv or 0

                return {
                    "name":          token_meta.get("name", "Unknown"),
                    "symbol":        token_meta.get("symbol", "???"),
                    "mint":          mint_address,
                    "dex_id":        best.get("dexId", "unknown"),
                    "pair_address":  best.get("pairAddress", ""),
                    "price_usd":     price_usd,
                    "liquidity_usd": liquidity_usd,
                    "market_cap":    market_cap,
                    "volume_1h":     volume_h1,
                    "volume_24h":    volume_h24,
                    "price_change_1h":  (best.get("priceChange") or {}).get("h1", 0) or 0,
                    "price_change_24h": (best.get("priceChange") or {}).get("h24", 0) or 0,
                    "created_at":    best.get("pairCreatedAt"),
                    "dexscreener_url": f"https://dexscreener.com/solana/{best.get('pairAddress', '')}",
                }

            except requests.RequestException as e:
                self.logger.warning(f"DexScreener request failed (attempt {attempt}): {e}")
                if attempt < retries:
                    time.sleep(retry_delay)

        return None

    def get_sol_price_usd(self) -> float:
        """
        Fetch the current SOL/USD price from DexScreener.

        Uses the most liquid SOL/USDC pair on Raydium as the price source.
        Falls back to a conservative estimate if the request fails.

        Returns:
            SOL price in USD, or 150.0 as a safe fallback.
        """
        _SOL_MINT = "So11111111111111111111111111111111111111112"
        _FALLBACK  = 150.0

        try:
            url = f"{_BASE_URL}/tokens/{_SOL_MINT}"
            resp = self.session.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            pairs = resp.json().get("pairs") or []
            if not pairs:
                return _FALLBACK

            # Pick the most liquid pair where SOL is the base token
            sol_pairs = [
                p for p in pairs
                if (p.get("baseToken") or {}).get("address", "") == _SOL_MINT
            ]
            if not sol_pairs:
                sol_pairs = pairs

            best = max(sol_pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0)
            price = float(best.get("priceUsd") or 0)
            return price if price > 0 else _FALLBACK

        except Exception as e:
            self.logger.warning(f"Could not fetch SOL price: {e}")
            return _FALLBACK
