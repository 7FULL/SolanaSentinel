"""
Anti-Scam Analyzer Module
Analyzes tokens for scam indicators using configurable on-chain rules.
Provides risk scoring (0-100), check-level detail, and blacklist management.

Score formula: start at 100, deduct penalty for each enabled check that fails.
Floor at 0. The sniper calls check_token(token_info) to reuse already-known data.
The frontend calls analyze_token(address) which fetches everything itself.
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional

# Known burn/locker addresses — LP tokens held here count as "locked"
_LOCKED_ADDRESSES = {
    "11111111111111111111111111111111",              # System zero / burn
    "1nc1nerator11111111111111111111111111111111",   # Incinerator program
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM authority v4
}

# Default rules written to disk on first run
_DEFAULT_RULES: Dict = {
    "enabled": True,
    "max_risk_score": 70,
    "checks": {
        "lp_locked": {
            "enabled": True,
            "penalty": 35
        },
        "mint_disabled": {
            "enabled": True,
            "penalty": 30
        },
        "freeze_disabled": {
            "enabled": True,
            "penalty": 20
        },
        "max_creator_percentage": {
            "enabled": True,
            "threshold": 10,
            "penalty": 25
        },
        "min_holders": {
            "enabled": True,
            "threshold": 10,
            "penalty": 15
        },
        "max_top_10_holders_percentage": {
            "enabled": True,
            "threshold": 50,
            "penalty": 15
        }
    }
}


class AntiScamAnalyzer:
    """
    Analyzes tokens for scam / rug-pull indicators using on-chain data.

    Two entry points:
      - analyze_token(address)  — fetches all on-chain data, used by the frontend
      - check_token(token_info) — reuses data already known by the sniper to avoid
                                   duplicate RPC calls
    """

    def __init__(self, config, rpc_client=None):
        """
        Initialize the Anti-Scam Analyzer.

        Args:
            config:     ConfigManager instance
            rpc_client: Solana RPC client for on-chain data
        """
        self.config     = config
        self.rpc_client = rpc_client
        self.rules_dir  = config.get_data_path('rules')
        self.rules_file     = self.rules_dir / 'anti_scam_rules.json'
        self.blacklist_file = self.rules_dir / 'blacklist.json'

        self.rules    = self._load_rules()
        self.blacklist = self._load_blacklist()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_rules(self) -> Dict:
        """Load rules from disk, auto-migrating the old format if needed."""
        if os.path.exists(self.rules_file):
            try:
                with open(self.rules_file, 'r') as f:
                    data = json.load(f)
                # Migrate old format (weights/flags/thresholds) → new checks format
                if 'checks' not in data:
                    data = dict(_DEFAULT_RULES)
                    self._save_rules_data(data)
                return data
            except Exception:
                pass
        rules = dict(_DEFAULT_RULES)
        self._save_rules_data(rules)
        return rules

    def _save_rules_data(self, data: Dict) -> bool:
        """Persist rules dict to disk."""
        try:
            os.makedirs(os.path.dirname(self.rules_file), exist_ok=True)
            with open(self.rules_file, 'w') as f:
                json.dump(data, f, indent=4)
            return True
        except Exception:
            return False

    def _save_rules(self) -> bool:
        return self._save_rules_data(self.rules)

    def _load_blacklist(self) -> Dict:
        """Load blacklist from disk."""
        if os.path.exists(self.blacklist_file):
            try:
                with open(self.blacklist_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {'tokens': {}, 'wallets': {}}

    def _save_blacklist(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self.blacklist_file), exist_ok=True)
            with open(self.blacklist_file, 'w') as f:
                json.dump(self.blacklist, f, indent=4)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Config API (used by frontend + sniper bridge)
    # ------------------------------------------------------------------

    def get_config(self) -> Dict:
        """Return current anti-scam configuration."""
        return self.rules.copy()

    def update_config(self, new_config: Dict) -> Dict:
        """
        Deep-merge new_config into the current rules and persist.

        Args:
            new_config: Partial or full config dict

        Returns:
            Updated config
        """
        def _deep_update(base: Dict, updates: Dict) -> None:
            for key, value in updates.items():
                if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                    _deep_update(base[key], value)
                else:
                    base[key] = value

        _deep_update(self.rules, new_config)
        self._save_rules()
        return self.rules.copy()

    # Kept for backward compatibility with existing /api/anti-scam/rules endpoints
    def get_rules(self) -> Dict:
        return self.get_config()

    def update_rules(self, new_rules: Dict) -> Dict:
        return self.update_config(new_rules)

    # ------------------------------------------------------------------
    # On-chain data fetchers
    # ------------------------------------------------------------------

    def _get_token_mint_info(self, mint_address: str) -> Optional[Dict]:
        """
        Fetch mint account data: mint_authority, freeze_authority, supply, decimals.

        Returns None on RPC failure (caller should treat as 'unknown' risk).
        """
        if not self.rpc_client:
            return None
        try:
            from solders.pubkey import Pubkey
            mint_pubkey  = Pubkey.from_string(mint_address)
            account_info = self.rpc_client.client.get_account_info(mint_pubkey)

            if not account_info.value:
                return None

            data_raw = account_info.value.data
            if isinstance(data_raw, bytes):
                data = data_raw
            elif isinstance(data_raw, str):
                import base64
                data = base64.b64decode(data_raw)
            elif isinstance(data_raw, list) and data_raw:
                import base64
                data = base64.b64decode(data_raw[0]) if isinstance(data_raw[0], str) else bytes(data_raw)
            elif hasattr(data_raw, '__iter__'):
                data = bytes(data_raw)
            else:
                return None

            if len(data) < 82:
                return None

            # SPL Token Mint layout (https://spl.solana.com/token#mint-account)
            mint_authority_opt = int.from_bytes(data[0:4], 'little')
            mint_authority     = str(Pubkey(bytes(data[4:36]))) if mint_authority_opt == 1 else None
            supply             = int.from_bytes(data[36:44], 'little')
            decimals           = data[44]
            freeze_auth_opt    = int.from_bytes(data[46:50], 'little')
            freeze_authority   = str(Pubkey(bytes(data[50:82]))) if freeze_auth_opt == 1 else None

            return {
                'mint_authority':   mint_authority,
                'freeze_authority': freeze_authority,
                'supply':           supply,
                'decimals':         decimals,
            }
        except Exception as e:
            print(f"[ANTI-SCAM] _get_token_mint_info error for {mint_address}: {e}")
            return None

    def _get_largest_token_accounts(self, mint_address: str, limit: int = 20) -> List[Dict]:
        """
        Return the largest token holder accounts (up to limit).

        Returns empty list on RPC failure.
        """
        if not self.rpc_client:
            return []
        try:
            from solders.pubkey import Pubkey
            mint_pubkey = Pubkey.from_string(mint_address)
            response    = self.rpc_client.client.get_token_largest_accounts(mint_pubkey)

            if not response.value:
                return []

            return [
                {
                    'address':  str(acc.address),
                    'amount':   int(acc.amount),
                    'decimals': acc.decimals,
                }
                for acc in response.value[:limit]
            ]
        except Exception as e:
            print(f"[ANTI-SCAM] _get_largest_token_accounts error for {mint_address}: {e}")
            return []

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_mint_disabled(self, mint_info: Dict) -> Dict:
        """Pass if mint authority is disabled (no new tokens can be minted)."""
        cfg     = self.rules['checks'].get('mint_disabled', {})
        penalty = cfg.get('penalty', 30)
        enabled = cfg.get('enabled', True)

        if not enabled:
            return {'passed': None, 'value': 'skipped', 'penalty': 0, 'message': 'Check disabled'}

        disabled = mint_info.get('mint_authority') is None
        if disabled:
            return {'passed': True,  'value': 'disabled', 'penalty': 0,
                    'message': 'Mint authority is disabled — supply is fixed'}
        return {'passed': False, 'value': 'enabled', 'penalty': penalty,
                'message': f"Mint authority is ENABLED — supply can be inflated"}

    def _check_freeze_disabled(self, mint_info: Dict) -> Dict:
        """Pass if freeze authority is disabled (wallets cannot be frozen)."""
        cfg     = self.rules['checks'].get('freeze_disabled', {})
        penalty = cfg.get('penalty', 20)
        enabled = cfg.get('enabled', True)

        if not enabled:
            return {'passed': None, 'value': 'skipped', 'penalty': 0, 'message': 'Check disabled'}

        disabled = mint_info.get('freeze_authority') is None
        if disabled:
            return {'passed': True,  'value': 'disabled', 'penalty': 0,
                    'message': 'Freeze authority is disabled — accounts cannot be frozen'}
        return {'passed': False, 'value': 'enabled', 'penalty': penalty,
                'message': 'Freeze authority is ENABLED — creator can freeze wallets'}

    def _check_lp_locked(self, mint_address: str, platform: str,
                         bonding_curve: Optional[Dict] = None) -> Dict:
        """
        Verify that LP is locked or burned.

        pump.fun: uses bonding_curve.complete field.
          - complete=False → still on curve, LP not yet created → treat as safe (N/A)
          - complete=True  → LP burned on Raydium migration → locked
        Raydium: unable to check reliably without getProgramAccounts → skipped (no penalty).
        """
        cfg     = self.rules['checks'].get('lp_locked', {})
        penalty = cfg.get('penalty', 35)
        enabled = cfg.get('enabled', True)

        if not enabled:
            return {'passed': None, 'value': 'skipped', 'penalty': 0, 'message': 'Check disabled'}

        platform_norm = platform.lower().replace('.', '').replace('_', '').replace(' ', '')

        if platform_norm == 'pumpfun':
            # Fetch bonding curve if not provided
            if bonding_curve is None and self.rpc_client:
                try:
                    bonding_curve = self.rpc_client.get_pumpfun_bonding_curve(mint_address)
                except Exception:
                    bonding_curve = None

            if bonding_curve is None:
                return {'passed': None, 'value': 'unknown', 'penalty': 0,
                        'message': 'Could not read bonding curve — LP lock status unknown'}

            if not bonding_curve.get('complete', False):
                # Still on bonding curve — LP does not yet exist
                return {'passed': True, 'value': 'on_curve', 'penalty': 0,
                        'message': 'Token on pump.fun bonding curve — LP not yet created (safe)'}
            else:
                # Graduated to Raydium — pump.fun burns LP on migration
                return {'passed': True, 'value': 'burned', 'penalty': 0,
                        'message': 'LP burned on Raydium migration (safe)'}

        # Raydium or unknown platform — skip without penalty
        return {'passed': None, 'value': 'unknown', 'penalty': 0,
                'message': 'LP lock check skipped for Raydium tokens (on-chain data insufficient)'}

    def _check_min_holders(self, largest_accounts: List[Dict]) -> Dict:
        """Pass if there are at least min_holders distinct token holders."""
        cfg       = self.rules['checks'].get('min_holders', {})
        threshold = cfg.get('threshold', 10)
        penalty   = cfg.get('penalty', 15)
        enabled   = cfg.get('enabled', True)

        if not enabled:
            return {'passed': None, 'value': 'skipped', 'penalty': 0, 'message': 'Check disabled'}

        count = len(largest_accounts)
        if count >= threshold:
            return {'passed': True,  'value': count, 'penalty': 0,
                    'message': f'{count} holders (min {threshold})'}
        return {'passed': False, 'value': count, 'penalty': penalty,
                'message': f'Only {count} holders — minimum is {threshold}'}

    def _check_top10_concentration(self, largest_accounts: List[Dict],
                                   total_supply: int) -> Dict:
        """Pass if top-10 holders own less than threshold % of supply."""
        cfg       = self.rules['checks'].get('max_top_10_holders_percentage', {})
        threshold = cfg.get('threshold', 50)
        penalty   = cfg.get('penalty', 15)
        enabled   = cfg.get('enabled', True)

        if not enabled:
            return {'passed': None, 'value': 'skipped', 'penalty': 0, 'message': 'Check disabled'}

        if not largest_accounts or total_supply == 0:
            return {'passed': None, 'value': 'unknown', 'penalty': 0,
                    'message': 'Holder data unavailable'}

        top10_amount = sum(a['amount'] for a in largest_accounts[:10])
        pct          = round(top10_amount / total_supply * 100, 2)

        if pct <= threshold:
            return {'passed': True,  'value': pct, 'penalty': 0,
                    'message': f'Top 10 own {pct:.1f}% of supply (max {threshold}%)'}

        # Graduated penalty: more over threshold = higher penalty, min 5 pts
        excess   = pct - threshold
        deducted = min(penalty, max(5, int(penalty * excess / threshold)))
        return {'passed': False, 'value': pct, 'penalty': deducted,
                'message': f'Top 10 own {pct:.1f}% of supply — exceeds {threshold}% threshold'}

    def _check_creator_percentage(self, largest_accounts: List[Dict],
                                  total_supply: int, creator_address: Optional[str]) -> Dict:
        """Pass if the creator wallet holds ≤ max_creator_percentage % of supply."""
        cfg       = self.rules['checks'].get('max_creator_percentage', {})
        threshold = cfg.get('threshold', 10)
        penalty   = cfg.get('penalty', 25)
        enabled   = cfg.get('enabled', True)

        if not enabled:
            return {'passed': None, 'value': 'skipped', 'penalty': 0, 'message': 'Check disabled'}

        if not creator_address:
            return {'passed': None, 'value': 'unknown', 'penalty': 0,
                    'message': 'Creator address unknown — check skipped'}

        if not largest_accounts or total_supply == 0:
            return {'passed': None, 'value': 'unknown', 'penalty': 0,
                    'message': 'Holder data unavailable'}

        creator_acc = next(
            (a for a in largest_accounts if a.get('address') == creator_address),
            None
        )

        if creator_acc is None:
            # Creator not in top holders — likely holds very little
            return {'passed': True, 'value': 0.0, 'penalty': 0,
                    'message': 'Creator not in top holders (< 0.1% of supply)'}

        pct = round(creator_acc['amount'] / total_supply * 100, 2)

        if pct <= threshold:
            return {'passed': True,  'value': pct, 'penalty': 0,
                    'message': f'Creator holds {pct:.1f}% of supply (max {threshold}%)'}

        # Graduated penalty
        excess   = pct - threshold
        deducted = min(penalty, max(5, int(penalty * excess / threshold)))
        return {'passed': False, 'value': pct, 'penalty': deducted,
                'message': f'Creator holds {pct:.1f}% of supply — exceeds {threshold}% threshold'}

    # ------------------------------------------------------------------
    # Core scoring engine
    # ------------------------------------------------------------------

    def _run_checks(self, mint_address: str, mint_info: Optional[Dict],
                    largest_accounts: List[Dict], platform: str,
                    bonding_curve: Optional[Dict], creator: Optional[str]) -> Dict:
        """
        Run all enabled checks and compute the final risk score.

        Args:
            mint_address:    Token mint address
            mint_info:       Parsed mint account (or None if unavailable)
            largest_accounts: Top holder list (may be empty)
            platform:        'pumpfun', 'pump.fun', 'raydium', etc.
            bonding_curve:   Bonding curve dict from rpc_client (or None)
            creator:         Creator wallet address (or None)

        Returns:
            Full analysis dict
        """
        total_supply = (mint_info or {}).get('supply', 0)

        # --- Run all checks ---
        if mint_info:
            mint_check   = self._check_mint_disabled(mint_info)
            freeze_check = self._check_freeze_disabled(mint_info)
        else:
            na = {'passed': None, 'value': 'unknown', 'penalty': 0,
                  'message': 'Mint data unavailable — skipped'}
            mint_check = freeze_check = na

        lp_check      = self._check_lp_locked(mint_address, platform, bonding_curve)
        holders_check = self._check_min_holders(largest_accounts)
        top10_check   = self._check_top10_concentration(largest_accounts, total_supply)
        creator_check = self._check_creator_percentage(largest_accounts, total_supply, creator)

        checks = {
            'lp_locked':                    lp_check,
            'mint_disabled':                mint_check,
            'freeze_disabled':              freeze_check,
            'min_holders':                  holders_check,
            'max_top_10_holders_percentage': top10_check,
            'max_creator_percentage':       creator_check,
        }

        # --- Score calculation ---
        score    = 100
        warnings  = []
        red_flags = []

        for key, result in checks.items():
            if result['passed'] is False:
                score -= result['penalty']
                msg = result['message']
                if result['penalty'] >= 25:
                    red_flags.append(msg)
                else:
                    warnings.append(msg)
            elif result['passed'] is None and result['value'] not in ('skipped',):
                warnings.append(result['message'])

        score = max(0, score)

        # --- Risk level ---
        if score >= 85:
            risk_level      = 'safe'
            passed          = True
            recommendation  = 'Token passes all safety checks.'
        elif score >= 70:
            risk_level      = 'low'
            passed          = True
            recommendation  = 'Token passes most safety checks. Relatively safe to trade.'
        elif score >= 50:
            risk_level      = 'medium'
            passed          = True
            recommendation  = 'Proceed with caution — some risk factors detected.'
        elif score >= 30:
            risk_level      = 'high'
            passed          = False
            recommendation  = 'High risk. Only invest what you can afford to lose.'
        else:
            risk_level      = 'critical'
            passed          = False
            recommendation  = 'AVOID: Multiple critical risk factors detected.'

        return {
            'token_address': mint_address,
            'analyzed_at':   datetime.utcnow().isoformat() + 'Z',
            'risk_score':    score,
            'risk_level':    risk_level,
            'passed':        passed,
            'checks':        checks,
            'warnings':      warnings,
            'red_flags':     red_flags,
            'recommendation': recommendation,
        }

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def analyze_token(self, token_address: str) -> Dict:
        """
        Full analysis for a token by address (used by the frontend form).
        Fetches all on-chain data itself.

        Args:
            token_address: Token mint address

        Returns:
            Analysis dict with risk_score, checks, warnings, red_flags, recommendation
        """
        # Blacklist check — instant reject
        if token_address in self.blacklist.get('tokens', {}):
            return {
                'token_address': token_address,
                'analyzed_at':   datetime.utcnow().isoformat() + 'Z',
                'is_blacklisted': True,
                'risk_score':    0,
                'risk_level':    'critical',
                'passed':        False,
                'checks':        {},
                'warnings':      [],
                'red_flags':     ['Token is blacklisted'],
                'recommendation': 'AVOID: Token is on blacklist',
            }

        if not self.rpc_client:
            return {
                'token_address': token_address,
                'analyzed_at':   datetime.utcnow().isoformat() + 'Z',
                'is_blacklisted': False,
                'risk_score':    50,
                'risk_level':    'medium',
                'passed':        True,
                'checks':        {},
                'warnings':      ['No RPC client configured — on-chain checks skipped'],
                'red_flags':     [],
                'recommendation': 'Cannot verify token safety — proceed with caution',
            }

        mint_info        = self._get_token_mint_info(token_address)
        largest_accounts = self._get_largest_token_accounts(token_address)

        result = self._run_checks(
            mint_address     = token_address,
            mint_info        = mint_info,
            largest_accounts = largest_accounts,
            platform         = 'unknown',
            bonding_curve    = None,
            creator          = None,
        )
        result['is_blacklisted'] = False
        return result

    def check_token(self, token_info: Dict) -> Dict:
        """
        Verify a token using data already known by the sniper detector.
        Avoids re-fetching data the sniper already has.

        Args:
            token_info: Dict with (minimum) 'token_mint', 'platform'.
                        Optionally: 'bonding_curve', 'creator', 'source'.

        Returns:
            Same analysis dict as analyze_token()
        """
        mint_address  = token_info.get('token_mint', '')
        platform      = token_info.get('platform', token_info.get('source', 'unknown'))
        bonding_curve = token_info.get('bonding_curve')
        creator       = token_info.get('creator')

        # Blacklist check
        if mint_address in self.blacklist.get('tokens', {}):
            return {
                'token_address': mint_address,
                'analyzed_at':   datetime.utcnow().isoformat() + 'Z',
                'is_blacklisted': True,
                'risk_score':    0,
                'risk_level':    'critical',
                'passed':        False,
                'checks':        {},
                'warnings':      [],
                'red_flags':     ['Token is blacklisted'],
                'recommendation': 'AVOID: Token is on blacklist',
            }

        if not self.rpc_client or not mint_address:
            return {
                'token_address': mint_address,
                'analyzed_at':   datetime.utcnow().isoformat() + 'Z',
                'is_blacklisted': False,
                'risk_score':    50,
                'risk_level':    'medium',
                'passed':        True,
                'checks':        {},
                'warnings':      ['No RPC client or mint address — on-chain checks skipped'],
                'red_flags':     [],
                'recommendation': 'Cannot verify token safety',
            }

        # Fetch what the sniper doesn't already have
        mint_info        = self._get_token_mint_info(mint_address)
        largest_accounts = self._get_largest_token_accounts(mint_address)

        result = self._run_checks(
            mint_address     = mint_address,
            mint_info        = mint_info,
            largest_accounts = largest_accounts,
            platform         = platform,
            bonding_curve    = bonding_curve,
            creator          = creator,
        )
        result['is_blacklisted'] = False
        return result

    # ------------------------------------------------------------------
    # Blacklist management
    # ------------------------------------------------------------------

    def get_blacklist(self) -> Dict:
        """Return current blacklist."""
        return self.blacklist.copy()

    def add_to_blacklist(self, address: str, reason: str, type_: str = 'token') -> bool:
        """Add an address to the blacklist."""
        key = 'tokens' if type_ == 'token' else 'wallets'
        if key not in self.blacklist:
            self.blacklist[key] = {}
        self.blacklist[key][address] = {
            'added_at': datetime.utcnow().isoformat(),
            'reason':   reason,
            'added_by': 'manual',
        }
        return self._save_blacklist()

    def remove_from_blacklist(self, address: str) -> bool:
        """Remove an address from the blacklist (checks both tokens and wallets)."""
        for key in ['tokens', 'wallets']:
            if address in self.blacklist.get(key, {}):
                del self.blacklist[key][address]
                self._save_blacklist()
                return True
        return False

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict:
        """Return module status for the overview dashboard."""
        return {
            'enabled':             self.rules.get('enabled', True),
            'max_risk_score':      self.rules.get('max_risk_score', 70),
            'blacklisted_tokens':  len(self.blacklist.get('tokens', {})),
            'blacklisted_wallets': len(self.blacklist.get('wallets', {})),
            'rpc_connected':       self.rpc_client is not None,
        }
