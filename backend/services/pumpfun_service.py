"""
Pump.fun Service
Fetches token metadata from the Pump.fun bonding curve API.
Used for newly launched tokens that are not yet indexed on DexScreener.
"""

import logging
import time
from typing import Optional, Dict
import requests

_PUMP_API = "https://frontend-api.pump.fun"
_TIMEOUT = 8  # seconds


class PumpFunService:
    """
    Fetches token data directly from the Pump.fun frontend API.

    This is the correct source for tokens still on the bonding curve
    (before they migrate to Raydium at ~$69k market cap).
    DexScreener does NOT index bonding curve tokens, so we fall back here.
    """

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; SolanaSentinel/1.0)",
            "Accept": "application/json",
        })

    def get_token_data(
        self,
        mint_address: str,
        retries: int = 3,
        retry_delay: float = 3.0,
    ) -> Optional[Dict]:
        """
        Fetch metadata for a Pump.fun bonding curve token.

        Args:
            mint_address: SPL token mint address (base58)
            retries: Number of attempts (new tokens may take a few seconds to appear)
            retry_delay: Seconds between retries

        Returns:
            Dict with name, symbol, market_cap, liquidity, etc.
            or None if not found / API error.
        """
        url = f"{_PUMP_API}/coins/{mint_address}"

        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, timeout=_TIMEOUT)

                if resp.status_code == 404:
                    if attempt < retries:
                        self.logger.debug(
                            f"PumpFun: token {mint_address[:12]}… not found yet "
                            f"(attempt {attempt}/{retries}), retrying in {retry_delay}s"
                        )
                        time.sleep(retry_delay)
                        continue
                    return None

                resp.raise_for_status()
                data = resp.json()

                if not data or not isinstance(data, dict):
                    return None

                name        = data.get("name") or "Unknown"
                symbol      = data.get("symbol") or "???"
                # Pump.fun reports market_cap in USD already
                market_cap  = float(data.get("usd_market_cap") or data.get("market_cap") or 0)
                # Virtual SOL reserves approximate the "liquidity" (bonding curve depth)
                virtual_sol = float(data.get("virtual_sol_reserves") or 0)
                # 1 virtual SOL reserve unit = 1e9 lamports; approximate USD at ~$150/SOL
                # This is a rough estimate; real price requires the sol_price feed
                # DexScreener is better for liquidity once the token is indexed.
                sol_price   = 150.0  # fallback; imprecise but better than 0
                liquidity_approx = (virtual_sol / 1e9) * sol_price
                price_usd   = float(data.get("price") or 0)
                description = data.get("description") or ""
                image_uri   = data.get("image_uri") or ""
                creator     = data.get("creator") or ""
                created_ts  = data.get("created_timestamp") or None

                self.logger.info(
                    f"PumpFun data: {symbol} ({name}) "
                    f"MC=${market_cap:,.0f} liq~${liquidity_approx:,.0f}"
                )

                return {
                    "name":          name,
                    "symbol":        symbol,
                    "mint":          mint_address,
                    "price_usd":     price_usd,
                    "market_cap":    market_cap,
                    "liquidity_usd": liquidity_approx,
                    "volume_1h":     0.0,   # not provided by bonding curve API
                    "volume_24h":    float(data.get("volume_24h") or 0),
                    "description":   description,
                    "image_uri":     image_uri,
                    "creator":       creator,
                    "created_at":    created_ts,
                    "bonding_curve": data.get("bonding_curve"),
                    "complete":      data.get("complete", False),  # True = migrated to Raydium
                    "dexscreener_url": f"https://dexscreener.com/solana/{mint_address}",
                    "pumpfun_url":   f"https://pump.fun/{mint_address}",
                }

            except requests.RequestException as e:
                self.logger.warning(
                    f"PumpFun API request failed (attempt {attempt}/{retries}): {e}"
                )
                if attempt < retries:
                    time.sleep(retry_delay)

        return None
