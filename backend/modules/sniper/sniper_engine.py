"""
Sniper Bot Engine Module
Detects and automatically purchases new tokens from Pump.fun and Raydium.
Supports configurable filters, a monitoring window, persistent storage, and
multiple execution modes (notification, simulation, auto_buy).
"""

import json
import uuid
import threading
import time
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple


class SniperEngine:
    """
    Automated token detection and sniping system.

    Flow per detected token:
      1. Normalize fields and add to detected_tokens list.
      2. Immediately check filters.
         - Passes  → execute action (notification / simulation / buy).
         - Fails   → enter monitoring window: re-check every MONITOR_INTERVAL
                     seconds until the token passes or max_time_since_creation
                     expires.
      3. On expiry without passing: status → "expired".
    """

    # Execution modes
    MODE_NOTIFICATION = 'notification'
    MODE_SIMULATION   = 'simulation'
    MODE_AUTO_BUY     = 'auto_buy'

    # How often the background thread re-checks pending tokens (seconds)
    MONITOR_INTERVAL = 15

    # How often open simulation positions are checked for TP/SL (seconds)
    SIM_CHECK_INTERVAL = 3

    # Maximum price snapshots stored per token
    MAX_PRICE_SNAPSHOTS = 120   # 120 × 15 s = 30 minutes of history

    def __init__(self, config, token_detector=None, notification_manager=None,
                 ai_analyzer=None, logging_engine=None, anti_scam_analyzer=None,
                 db=None):
        """
        Initialize the Sniper Engine.

        Args:
            config:               ConfigManager instance
            token_detector:       Optional TokenDetector for real-time monitoring
            notification_manager: Optional NotificationManager for alerts
            ai_analyzer:          Optional AIAnalyzer for risk scoring
            logging_engine:       Optional LoggingEngine for structured logs
            anti_scam_analyzer:   Optional AntiScamAnalyzer for on-chain risk checks
            db:                   Optional DatabaseManager for persistent storage
        """
        self.config      = config
        self.rules_dir   = config.get_data_path('rules')
        self.config_file = self.rules_dir / 'sniper_config.json'

        self.token_detector       = token_detector
        self.notification_manager = notification_manager
        self.ai_analyzer          = ai_analyzer
        self.logging_engine       = logging_engine
        self.anti_scam_analyzer   = anti_scam_analyzer
        self.db                   = db

        # Thread safety: all writes to detected_tokens go through this lock
        self._lock = threading.Lock()

        # Load persisted state
        self.sniper_config  = self._load_config()
        self.history        = self._load_history()
        self.detected_tokens: List[Dict] = self._load_detected_tokens()
        self.sim_positions:   List[Dict] = self._load_sim_positions()

        self.is_running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._sim_thread: Optional[threading.Thread] = None

        # Register token detection callback
        if self.token_detector:
            self.token_detector.on_token_detected(self._handle_token_detected)

    # ------------------------------------------------------------------
    # Internal logging helper
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str, data: Optional[Dict] = None) -> None:
        """
        Write a structured log entry via LoggingEngine (if wired) AND print to console.

        Args:
            level:   'INFO', 'WARNING', 'ERROR', 'DEBUG', or 'CRITICAL'
            message: Human-readable log message
            data:    Optional dict of extra context fields
        """
        print(f"[SNIPER] {message}")
        if self.logging_engine:
            self.logging_engine.log(level=level, message=message,
                                    module='sniper', data=data or {})

    # ------------------------------------------------------------------
    # Config / history / detected-token persistence
    # ------------------------------------------------------------------

    def _load_config(self) -> Dict:
        """Load sniper configuration from JSON file."""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass

        return {
            'enabled': False,
            'mode': self.MODE_NOTIFICATION,
            'filters': {
                'min_liquidity': 1000,
                'max_liquidity': None,
                'min_market_cap': 0,
                'max_market_cap': 500000,
                'min_initial_volume': 0,
                'max_time_since_creation_minutes': 5,
                'platforms': ['pump.fun', 'raydium']
            },
            'execution': {
                'auto_buy_amount': 0.1,
                'max_slippage': 10.0,
                'priority_fee': 0.001,
                'use_jito': False
            },
            'simulation': {
                'tp_percent':        50.0,
                'sl_percent':        30.0,
                'fee_entry_percent': 1.25,  # Pump.fun 1% + ~0.25% slippage
                'fee_exit_percent':  1.25,  # same on the way out
            },
            'anti_scam': {
                'enabled': True,
                'max_risk_score': 80,
            },
            'ml': {
                'enabled': False,        # when True, tokens below min_pump_score are rejected
                'min_pump_score': 0,     # 0-100; only used when enabled=True
            },
            'limits': {
                'max_buys_per_hour': 10,
                'max_total_investment': 5.0
            }
        }

    def _save_config(self) -> bool:
        """Save sniper configuration to JSON file."""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w') as f:
                json.dump(self.sniper_config, f, indent=4)
            return True
        except Exception as e:
            print(f"Error saving sniper config: {e}")
            return False

    def _load_history(self) -> List[Dict]:
        """Load sniper action history from DB."""
        if self.db:
            try:
                return self.db.get_sniper_history(limit=2000)
            except Exception:
                pass
        return []

    def _save_history(self) -> bool:
        """History is persisted per-entry via DB in _add_history()."""
        return True

    def _load_detected_tokens(self) -> List[Dict]:
        """Load previously detected tokens from DB (last 24 h)."""
        if self.db:
            try:
                return self.db.get_detected_tokens(limit=500)
            except Exception:
                pass
        return []

    def _save_detected_tokens(self):
        """Tokens are persisted individually via DB in _emit_token_detected()."""
        pass

    def _load_sim_positions(self) -> List[Dict]:
        """Load simulation positions from DB."""
        if self.db:
            try:
                return self.db.get_sniper_positions(limit=2000)
            except Exception:
                pass
        return []

    def _save_sim_positions(self):
        """Positions are persisted individually via DB on open/close."""
        pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_config(self) -> Dict:
        return self.sniper_config.copy()

    def update_config(self, new_config: Dict) -> Dict:
        """Deep-merge new_config into the current config and persist."""
        def deep_update(base, updates):
            for key, value in updates.items():
                if isinstance(value, dict) and key in base:
                    deep_update(base[key], value)
                else:
                    base[key] = value
        deep_update(self.sniper_config, new_config)
        self._save_config()
        return self.sniper_config

    def start(self) -> bool:
        """Start the sniper bot and its background monitoring thread."""
        self.is_running = True

        if self.token_detector:
            try:
                platforms = self.sniper_config.get('filters', {}).get(
                    'platforms', ['pump.fun', 'raydium']
                )
                self.token_detector.start(platforms=platforms)
            except Exception as e:
                print(f"Error starting token detector: {e}")
                return False

        # Thread 1: re-evaluate watching tokens (every MONITOR_INTERVAL seconds)
        self._monitor_thread = threading.Thread(
            target=self._monitoring_loop,
            name="SniperMonitor",
            daemon=True,
        )
        self._monitor_thread.start()

        # Thread 2: check open sim positions for TP/SL (every SIM_CHECK_INTERVAL seconds)
        self._sim_thread = threading.Thread(
            target=self._sim_loop,
            name="SniperSimMonitor",
            daemon=True,
        )
        self._sim_thread.start()

        print("[SNIPER] Monitoring threads started "
              f"(tokens: {self.MONITOR_INTERVAL}s, sim: {self.SIM_CHECK_INTERVAL}s)")
        return True

    def stop(self) -> bool:
        """Stop the sniper bot."""
        self.is_running = False

        if self.token_detector:
            try:
                self.token_detector.stop()
            except Exception as e:
                print(f"Error stopping token detector: {e}")

        # Threads exit on their own when is_running=False
        self._monitor_thread = None
        self._sim_thread = None
        return True

    def get_detected_tokens(self, limit: int = 50,
                            status_filter: Optional[str] = None) -> List[Dict]:
        """
        Return detected tokens, optionally filtered by status.

        Args:
            limit:         Maximum number of tokens to return
            status_filter: If given, only return tokens with this status
                           ('detected', 'watching', 'passed_filters',
                            'expired', 'rejected_by_ai')
        """
        with self._lock:
            tokens = list(self.detected_tokens)

        if status_filter:
            tokens = [t for t in tokens if t.get('status') == status_filter]

        return tokens[:limit]

    def get_token_detail(self, mint: str) -> Optional[Dict]:
        """Return the full stored record for a single token including price_snapshots."""
        with self._lock:
            for t in self.detected_tokens:
                if t.get('token_mint') == mint:
                    return dict(t)
        return None

    def get_history(self, limit: int = 50) -> List[Dict]:
        return self.history[:limit] if self.history else []

    # ------------------------------------------------------------------
    # Simulation public API
    # ------------------------------------------------------------------

    def get_sim_positions(self, status_filter: Optional[str] = None,
                          limit: int = 200) -> List[Dict]:
        """Return simulation positions, optionally filtered by status."""
        with self._lock:
            positions = list(self.sim_positions)
        if status_filter:
            positions = [p for p in positions if p.get('status') == status_filter]
        return positions[:limit]

    def get_sim_stats(self) -> Dict:
        """Compute aggregate simulation statistics."""
        with self._lock:
            positions = list(self.sim_positions)

        open_pos   = [p for p in positions if p['status'] == 'open']
        closed_pos = [p for p in positions if p['status'] in ('closed_tp', 'closed_sl', 'closed_manual')]
        tp_hits    = [p for p in positions if p['status'] == 'closed_tp']
        sl_hits    = [p for p in positions if p['status'] == 'closed_sl']

        # Realized P&L: only closed trades
        realized_sol = sum(p.get('pnl_sol', 0.0) for p in closed_pos)
        # Unrealized P&L: open positions at current price
        unrealized_sol = sum(p.get('pnl_sol', 0.0) for p in open_pos)
        total_invested = sum(p.get('simulated_sol', 0.0) for p in positions)

        all_pnl = [p.get('pnl_percent', 0.0) for p in positions
                   if p.get('pnl_percent') is not None]

        return {
            'total_trades':         len(positions),
            'open_count':           len(open_pos),
            'closed_count':         len(closed_pos),
            'tp_hits':              len(tp_hits),
            'sl_hits':              len(sl_hits),
            'win_rate':             round(len(tp_hits) / len(closed_pos) * 100, 1) if closed_pos else 0.0,
            'realized_pnl_sol':     round(realized_sol, 4),
            'unrealized_pnl_sol':   round(unrealized_sol, 4),
            'total_pnl_sol':        round(realized_sol + unrealized_sol, 4),
            'total_invested_sol':   round(total_invested, 4),
            'roi_percent':          round((realized_sol + unrealized_sol) / total_invested * 100, 2)
                                    if total_invested > 0 else 0.0,
            'best_trade_pct':       round(max(all_pnl), 2) if all_pnl else 0.0,
            'worst_trade_pct':      round(min(all_pnl), 2) if all_pnl else 0.0,
            'avg_trade_pct':        round(sum(all_pnl) / len(all_pnl), 2) if all_pnl else 0.0,
        }

    def get_status(self) -> Dict:
        today = datetime.utcnow().date()
        with self._lock:
            detected_today = sum(
                1 for t in self.detected_tokens
                if 'detected_at' in t and
                datetime.fromisoformat(t['detected_at'].replace('Z', '')).date() == today
            )
            watching = sum(
                1 for t in self.detected_tokens if t.get('status') == 'watching'
            )

        bought_today = sum(
            1 for op in self.history
            if 'timestamp' in op and
            datetime.fromisoformat(op['timestamp'].replace('Z', '')).date() == today and
            op.get('mode') == 'auto_buy' and op.get('status') == 'success'
        )

        return {
            'running':               self.is_running,
            'mode':                  self.sniper_config.get('mode', 'notification'),
            'tokens_detected_today': detected_today,
            'tokens_bought_today':   bought_today,
            'tokens_watching':       watching,
        }

    def get_metrics(self) -> Dict:
        """Return metrics for the overview dashboard endpoint."""
        sim_stats = self.get_sim_stats()
        with self._lock:
            total_detected = len(self.detected_tokens)
            open_sim       = sum(1 for p in self.sim_positions if p.get('status') == 'open')
        return {
            'total_detected':    total_detected,
            'open_simulations':  open_sim,
            'sim_win_rate':      sim_stats['win_rate'],
            'sim_total_pnl_sol': sim_stats['total_pnl_sol'],
            'mode':              self.sniper_config.get('mode', 'notification'),
            'running':           self.is_running,
        }

    def refresh_sim_prices(self) -> None:
        """
        Force an immediate price refresh for all open simulation positions.
        Equivalent to one monitoring loop tick — useful when the frontend
        Refresh button is clicked and the user wants up-to-date P&L without
        waiting for the next 15-second monitoring interval.
        """
        self._check_sim_positions()

    def reset_sim_positions(self) -> bool:
        """
        Clear all simulation positions from memory and the DB.
        Used when the user wants to start a fresh simulation run.

        Returns:
            True on success
        """
        with self._lock:
            self.sim_positions = []
            if self.db:
                try:
                    self.db.clear_sniper_positions()
                except Exception as e:
                    self._log('ERROR', f"Failed to clear sim positions from DB: {e}",
                              {'error': str(e)})
        return True

    # ------------------------------------------------------------------
    # Token detection handler
    # ------------------------------------------------------------------

    def _handle_token_detected(self, token_info: Dict):
        """
        Entry point for every new token emitted by the TokenDetector.
        Normalizes fields, persists, then immediately evaluates filters.
        Tokens that fail filters enter the monitoring window.
        """
        if not self.is_running:
            return

        symbol = token_info.get('token_symbol', token_info.get('symbol', 'UNKNOWN'))
        mint   = token_info.get('token_mint')

        # Avoid duplicates (same mint already in list)
        with self._lock:
            if any(t.get('token_mint') == mint for t in self.detected_tokens if mint):
                return

        self._log('INFO', f"New token detected: {symbol} ({mint[:12] if mint else '?'}…)",
                  {'symbol': symbol, 'mint': mint,
                   'platform': token_info.get('source', token_info.get('platform', 'unknown')),
                   'liquidity': token_info.get('initial_liquidity', token_info.get('liquidity', 0)),
                   'market_cap': token_info.get('market_cap', 0)})

        # Normalize to a stable set of keys
        token_info.setdefault('detected_at',    datetime.utcnow().isoformat() + 'Z')
        token_info.setdefault('symbol',         symbol)
        token_info.setdefault('name',           token_info.get('token_name', symbol))
        token_info.setdefault('platform',       token_info.get('source', 'unknown'))
        token_info.setdefault('liquidity',      token_info.get('initial_liquidity', 0.0))
        token_info.setdefault('market_cap',     0.0)
        token_info.setdefault('price_usd',      0.0)
        token_info.setdefault('risk_score',     100)
        token_info.setdefault('status',         'detected')
        token_info.setdefault('action_taken',   'pending')
        token_info.setdefault('filter_fail_reason', '')
        token_info.setdefault('price_snapshots', [])

        # Record the entry price snapshot
        self._record_price_snapshot(token_info)
        token_info['entry_price_usd'] = token_info['price_usd']

        with self._lock:
            self.detected_tokens.insert(0, token_info)
            if len(self.detected_tokens) > 500:
                self.detected_tokens = self.detected_tokens[:500]
            self._save_detected_tokens()

        # Immediately try to pass filters
        reason = self._filter_fail_reason(token_info)
        if not reason:
            self._run_ai_and_execute(token_info)
        else:
            token_info['status']            = 'watching'
            token_info['filter_fail_reason'] = reason
            self._log('INFO', f"{symbol} watching — {reason}",
                      {'symbol': symbol, 'mint': mint, 'filter_fail_reason': reason})
            with self._lock:
                self._save_detected_tokens()

    # ------------------------------------------------------------------
    # Background monitoring loop
    # ------------------------------------------------------------------

    def _monitoring_loop(self):
        """
        Background thread: re-evaluates 'watching' tokens every MONITOR_INTERVAL
        seconds.  Heavier — involves RPC calls to refresh market data.
        """
        print("[SNIPER] Token monitoring loop running")
        while self.is_running:
            try:
                self._check_pending_tokens()
            except Exception as e:
                print(f"[SNIPER] Token monitoring error: {e}")
            time.sleep(self.MONITOR_INTERVAL)
        print("[SNIPER] Token monitoring loop stopped")

    def _sim_loop(self):
        """
        Background thread: checks open simulation positions for TP/SL every
        SIM_CHECK_INTERVAL seconds.  Lighter than the token monitor — only
        fetches prices for open positions.
        """
        print("[SNIPER] Sim monitoring loop running")
        while self.is_running:
            try:
                self._check_sim_positions()
            except Exception as e:
                print(f"[SNIPER] Sim monitoring error: {e}")
            time.sleep(self.SIM_CHECK_INTERVAL)
        print("[SNIPER] Sim monitoring loop stopped")

    def _check_pending_tokens(self):
        """Re-evaluate all 'watching' tokens."""
        max_minutes = self.sniper_config.get('filters', {}).get(
            'max_time_since_creation_minutes', 5
        )
        now = datetime.utcnow()
        changed = False

        with self._lock:
            watching = [t for t in self.detected_tokens if t.get('status') == 'watching']

        for token in watching:
            mint = token.get('token_mint')

            # Check age — expire if past the window
            try:
                detected_at = datetime.fromisoformat(
                    token['detected_at'].replace('Z', '')
                )
                age_minutes = (now - detected_at).total_seconds() / 60
            except Exception:
                age_minutes = 0

            if age_minutes > max_minutes:
                token['status'] = 'expired'
                sym = token.get('symbol', '?')
                self._log('WARNING', f"{sym} expired after {age_minutes:.1f} min (max {max_minutes} min)",
                          {'symbol': sym, 'mint': mint,
                           'age_minutes': round(age_minutes, 1),
                           'filter_fail_reason': token.get('filter_fail_reason', '')})
                changed = True
                continue

            if not mint:
                token['status'] = 'expired'
                changed = True
                continue

            # Refresh market data from on-chain
            updated = self._refresh_token_market_data(token)
            if updated:
                self._record_price_snapshot(token)
                changed = True

            # Re-evaluate filters
            reason = self._filter_fail_reason(token)
            if not reason:
                sym = token.get('symbol', '?')
                self._log('INFO', f"{sym} now passes filters after {age_minutes:.1f} min — executing action",
                          {'symbol': sym, 'mint': mint, 'age_minutes': round(age_minutes, 1)})
                self._run_ai_and_execute(token)
                changed = True
            else:
                token['filter_fail_reason'] = reason

        if changed:
            with self._lock:
                self._save_detected_tokens()

    def _check_sim_positions(self):
        """
        Check all open simulation positions against TP/SL thresholds.
        Updates pnl_percent/pnl_sol for each open position in place.
        Closes positions that hit TP or SL.
        """
        sim_cfg = self.sniper_config.get('simulation', {})
        tp_pct  = float(sim_cfg.get('tp_percent', 50.0))
        sl_pct  = float(sim_cfg.get('sl_percent', 30.0))

        with self._lock:
            open_positions = [p for p in self.sim_positions if p.get('status') == 'open']

        if not open_positions:
            return

        changed = False
        for pos in open_positions:
            mint        = pos.get('token_mint')
            entry_price = pos.get('entry_price', 0.0)
            if not mint or entry_price == 0:
                continue

            current_price = self._get_current_price(mint)
            if current_price is None or current_price == 0:
                continue

            # Raw price change (no fees)
            change_pct    = (current_price - entry_price) / entry_price * 100
            sim_sol_net   = pos.get('simulated_sol_net', pos.get('simulated_sol', 0.1))
            fee_exit_pct  = pos.get('fee_exit_pct', 1.25)

            # Gross return on the net position
            gross_return_sol = sim_sol_net * change_pct / 100
            # Exit fee applied to the gross proceeds (net + return)
            proceeds_sol     = sim_sol_net + gross_return_sol
            fee_exit_sol     = round(proceeds_sol * fee_exit_pct / 100, 6)
            # Total fees = entry fee (already paid) + exit fee
            total_fees_sol   = round(pos.get('fee_entry_sol', 0.0) + fee_exit_sol, 6)
            # Net P&L after both fees
            net_pnl_sol      = round(gross_return_sol - fee_exit_sol, 6)
            # Effective P&L % relative to original gross investment
            sim_sol_gross    = pos.get('simulated_sol', 0.1)
            net_pnl_pct      = round(net_pnl_sol / sim_sol_gross * 100, 2)

            pos['current_price']  = current_price
            pos['pnl_percent']    = net_pnl_pct
            pos['pnl_sol']        = net_pnl_sol
            pos['fees_sol']       = total_fees_sol
            pos['last_updated']   = datetime.utcnow().isoformat() + 'Z'
            changed = True

            symbol = pos.get('symbol', '?')
            closing = None
            if change_pct >= tp_pct:
                closing = 'closed_tp'
                self._log('INFO',
                          f"SIM TP ✅ {symbol}: price +{change_pct:.1f}% → net {net_pnl_pct:+.1f}% ({net_pnl_sol:+.4f} SOL after fees)",
                          {'symbol': symbol, 'mint': pos.get('token_mint'),
                           'change_pct': round(change_pct, 2), 'net_pnl_pct': net_pnl_pct,
                           'net_pnl_sol': net_pnl_sol, 'result': 'tp'})
            elif change_pct <= -sl_pct:
                closing = 'closed_sl'
                self._log('WARNING',
                          f"SIM SL ❌ {symbol}: price {change_pct:.1f}% → net {net_pnl_pct:+.1f}% ({net_pnl_sol:+.4f} SOL after fees)",
                          {'symbol': symbol, 'mint': pos.get('token_mint'),
                           'change_pct': round(change_pct, 2), 'net_pnl_pct': net_pnl_pct,
                           'net_pnl_sol': net_pnl_sol, 'result': 'sl'})

            if closing:
                pos['status']       = closing
                pos['exit_price']   = current_price
                pos['exit_time']    = datetime.utcnow().isoformat() + 'Z'
                pos['fee_exit_sol'] = fee_exit_sol
                if self.db:
                    try:
                        self.db.save_sniper_position(pos)
                    except Exception as e:
                        print(f"[SNIPER] DB sim position close error: {e}")

        if changed:
            with self._lock:
                self._save_sim_positions()

    def _get_current_price(self, mint: str) -> Optional[float]:
        """
        Fetch the latest price for a token.
        Tries DexScreener first, falls back to on-chain bonding curve.
        """
        if not self.token_detector:
            return None
        dex = self.token_detector.dexscreener
        rpc = self.token_detector.rpc

        data = dex.get_token_data(mint, retries=1, retry_delay=0)
        if data and data.get('price_usd'):
            return data['price_usd']

        curve = rpc.get_pumpfun_bonding_curve(mint)
        if curve:
            sol_price = dex.get_sol_price_usd()
            return curve['price_sol'] * sol_price

        return None

    def _open_sim_position(self, token_info: Dict):
        """
        Create a new simulation position entry when a token passes filters
        in simulation mode.

        Entry fee is applied immediately: the virtual SOL used to buy tokens
        is reduced by fee_entry_percent, simulating the Pump.fun fee + slippage
        paid on the way in.
        """
        sim_cfg      = self.sniper_config.get('simulation', {})
        fee_entry    = float(sim_cfg.get('fee_entry_percent', 1.25))
        sim_sol_gross = self.sniper_config.get('execution', {}).get('auto_buy_amount', 0.1)
        # Net SOL actually used to buy tokens (after entry fee)
        fee_entry_sol = round(sim_sol_gross * fee_entry / 100, 6)
        sim_sol_net   = round(sim_sol_gross - fee_entry_sol, 6)

        entry_price = token_info.get('price_usd', 0.0)

        position = {
            'id':              str(uuid.uuid4()),
            'token_mint':      token_info.get('token_mint', ''),
            'token_symbol':    token_info.get('token_symbol', token_info.get('symbol', '?')),
            'symbol':          token_info.get('token_symbol', token_info.get('symbol', '?')),
            'name':            token_info.get('name',   token_info.get('token_name', '')),
            'platform':        token_info.get('platform', 'unknown'),
            'entry_price':     entry_price,
            'entry_time':      datetime.utcnow().isoformat() + 'Z',
            'entry_mc':        token_info.get('market_cap', 0.0),
            'entry_liquidity': token_info.get('liquidity', 0.0),
            # Gross = what we'd send; net = what buys tokens after entry fee
            'simulated_sol':   sim_sol_gross,
            'simulated_sol_net': sim_sol_net,
            'fee_entry_sol':   fee_entry_sol,
            'fee_entry_pct':   fee_entry,
            'fee_exit_pct':    float(sim_cfg.get('fee_exit_percent', 1.25)),
            'current_price':   entry_price,
            'pnl_percent':     0.0,
            'pnl_sol':         0.0,
            'fees_sol':        fee_entry_sol,   # running total fees paid
            'status':          'open',
            'exit_price':      None,
            'exit_time':       None,
            'solscan_url':     token_info.get('solscan_url', ''),
            'dexscreener_url': token_info.get('dexscreener_url', ''),
            'last_updated':    datetime.utcnow().isoformat() + 'Z',
            # ML pump prediction at entry time
            'ml_score':        token_info.get('ml_score'),
            'pump_probability': token_info.get('pump_probability'),
            'pump_signal':     token_info.get('pump_signal'),
        }

        with self._lock:
            self.sim_positions.insert(0, position)
            if self.db:
                try:
                    self.db.save_sniper_position(position)
                except Exception as e:
                    print(f"[SNIPER] DB sim position save error: {e}")
            self._save_sim_positions()

        tp = sim_cfg.get('tp_percent', 50)
        sl = sim_cfg.get('sl_percent', 30)
        self._log('INFO',
                  f"SIM opened: {position['symbol']} @ ${entry_price:.8f} "
                  f"({sim_sol_gross} SOL gross / {sim_sol_net:.4f} SOL net after {fee_entry}% fee) "
                  f"— TP +{tp}% / SL -{sl}%",
                  {'symbol': position['symbol'], 'mint': position.get('token_mint'),
                   'entry_price': entry_price, 'sim_sol_gross': sim_sol_gross,
                   'sim_sol_net': sim_sol_net, 'fee_entry_pct': fee_entry,
                   'tp_percent': tp, 'sl_percent': sl})

    def _refresh_token_market_data(self, token: Dict) -> bool:
        """
        Fetch latest bonding curve / DexScreener data and update token in place.

        Returns True if any value changed.
        """
        if not self.token_detector:
            return False

        mint = token.get('token_mint')
        if not mint:
            return False

        rpc  = self.token_detector.rpc
        dex  = self.token_detector.dexscreener
        prev = (token.get('price_usd', 0), token.get('market_cap', 0))

        # Try DexScreener first (only works after migration to a DEX pair)
        dex_data = dex.get_token_data(mint, retries=1, retry_delay=0)
        if dex_data:
            token['price_usd']   = dex_data['price_usd']
            token['market_cap']  = dex_data['market_cap']
            token['liquidity']   = dex_data['liquidity_usd']
            token['volume_1h']   = dex_data['volume_1h']
        else:
            # Fall back to on-chain bonding curve
            curve = rpc.get_pumpfun_bonding_curve(mint)
            if curve:
                sol_price = dex.get_sol_price_usd()
                token['price_usd']  = curve['price_sol']      * sol_price
                token['market_cap'] = curve['market_cap_sol'] * sol_price
                token['liquidity']  = curve['real_sol']        * sol_price * 2

        return (token.get('price_usd', 0), token.get('market_cap', 0)) != prev

    # ------------------------------------------------------------------
    # AI gate + action execution
    # ------------------------------------------------------------------

    def _run_ai_and_execute(self, token_info: Dict):
        """
        Run optional anti-scam and AI analysis, then execute the configured action.
        Anti-scam check runs first (on-chain rules, no LLM cost).
        AI analysis runs second (optional, heavier).
        Sets token status/action_taken in place.
        """
        token_mint  = token_info.get('token_mint')
        symbol      = token_info.get('symbol', '?')
        anti_scam   = self.sniper_config.get('anti_scam', {})

        # --- Anti-scam gate (on-chain rule checks) ---
        if self.anti_scam_analyzer and anti_scam.get('enabled', True) and token_mint:
            try:
                result = self.anti_scam_analyzer.check_token(token_info)
                token_info['risk_score'] = result.get('risk_score', 100)
                token_info['risk_level'] = result.get('risk_level', 'unknown')
                token_info['ai_flags']   = result.get('red_flags', [])

                self._log('INFO',
                          f"Anti-scam: {symbol} scored {token_info['risk_score']}/100 "
                          f"({token_info['risk_level']})",
                          {'symbol': symbol, 'mint': token_mint,
                           'risk_score': token_info['risk_score'],
                           'risk_level': token_info['risk_level'],
                           'checks': result.get('checks', {})})

                # Score is a SAFETY score: 100 = fully safe, 0 = critical risk.
                # Reject when the safety score falls BELOW the configured minimum.
                min_safe = anti_scam.get('max_risk_score', 80)
                if token_info['risk_score'] < min_safe:
                    token_info['status']       = 'rejected_by_ai'
                    token_info['action_taken'] = 'rejected'
                    self._log('WARNING',
                              f"{symbol} rejected by anti-scam (score {token_info['risk_score']} < min {min_safe})",
                              {'symbol': symbol, 'mint': token_mint,
                               'risk_score': token_info['risk_score'],
                               'risk_level': token_info['risk_level']})
                    if self.db:
                        try:
                            self.db.update_token_sniper_decision(
                                mint=token_mint, status='rejected_by_anti_scam',
                                action='rejected',
                                reject_reason=f"risk_score {token_info['risk_score']} < {min_safe}",
                                risk_score=token_info['risk_score'],
                                risk_level=token_info.get('risk_level'),
                                risk_checks=token_info.get('risk_checks'),
                            )
                        except Exception:
                            pass
                    with self._lock:
                        self._save_detected_tokens()
                    return
            except Exception as e:
                self._log('ERROR', f"Anti-scam check failed for {symbol}: {e}",
                          {'error': str(e), 'symbol': symbol, 'mint': token_mint})

        # --- ML pump-probability gate ---
        if self.ai_analyzer and hasattr(self.ai_analyzer, 'predict_pump_probability'):
            try:
                ml_result = self.ai_analyzer.predict_pump_probability(token_info)
                if not ml_result.get('skipped'):
                    token_info['pump_probability'] = ml_result['pump_probability']
                    token_info['pump_signal']      = ml_result['pump_signal']
                    token_info['ml_score']         = ml_result['ml_score']

                    ml_cfg      = self.sniper_config.get('ml', {})
                    ml_enabled  = ml_cfg.get('enabled', False)
                    min_ml      = ml_cfg.get('min_pump_score', 0)  # 0-100

                    self._log('INFO',
                              f"ML pump score {symbol}: {ml_result['ml_score']}/100 "
                              f"(signal={'YES' if ml_result['pump_signal'] else 'NO'}, "
                              f"prob={ml_result['pump_probability']:.3f})",
                              {'symbol': symbol, 'mint': token_mint,
                               'ml_score': ml_result['ml_score'],
                               'pump_signal': ml_result['pump_signal']})

                    if ml_enabled and ml_result['ml_score'] < min_ml:
                        token_info['status']       = 'rejected_by_ml'
                        token_info['action_taken'] = 'rejected'
                        self._log('WARNING',
                                  f"{symbol} rejected by ML (score {ml_result['ml_score']} < min {min_ml})",
                                  {'symbol': symbol, 'mint': token_mint,
                                   'ml_score': ml_result['ml_score']})
                        if self.db:
                            try:
                                self.db.update_token_sniper_decision(
                                    mint=token_mint, status='rejected_by_ml',
                                    action='rejected',
                                    reject_reason=f"ml_score {ml_result['ml_score']} < {min_ml}",
                                    ml_score=ml_result['ml_score'],
                                    pump_probability=ml_result.get('pump_probability'),
                                )
                            except Exception:
                                pass
                        return
            except Exception as ml_err:
                self._log('ERROR', f"ML prediction failed: {ml_err}",
                          {'error': str(ml_err)})

        # --- AI gate (heuristic on-chain risk scoring) ---
        if self.ai_analyzer and token_mint:
            try:
                ai_result = self.ai_analyzer.analyze_token(token_mint)
                token_info['risk_score']          = ai_result.get('risk_score', 100)
                token_info['risk_level']           = ai_result.get('risk_level', 'unknown')
                token_info['ai_flags']             = ai_result.get('red_flags', [])
                token_info['rugpull_probability']  = ai_result.get('rugpull_probability', 0.0)
                print(
                    f"[SNIPER] AI score {token_mint[:12]}: "
                    f"{token_info['risk_score']}/100 ({token_info['risk_level']})"
                )
                # Re-check risk threshold with the score from the AI analyzer
                min_safe = anti_scam.get('max_risk_score', 80)
                if anti_scam.get('enabled', True) and token_info['risk_score'] < min_safe:
                    token_info['status']       = 'rejected_by_ai'
                    token_info['action_taken'] = 'rejected'
                    sym = token_info.get('symbol', '?')
                    self._log('WARNING',
                              f"{sym} rejected by AI (score {token_info['risk_score']} < min {min_safe})",
                              {'symbol': sym, 'mint': token_info.get('token_mint'),
                               'risk_score': token_info.get('risk_score', 100)})
                    return
            except Exception as ai_err:
                self._log('ERROR', f"AI analysis failed: {ai_err}",
                          {'error': str(ai_err)})

        self._execute_action(token_info)

    def _execute_action(self, token_info: Dict):
        """
        Perform the configured action (notification / simulation / auto_buy)
        and record to history.
        """
        mode   = self.sniper_config.get('mode', self.MODE_NOTIFICATION)
        symbol = token_info.get('symbol', 'UNKNOWN')

        token_info['status']       = 'passed_filters'
        token_info['action_taken'] = mode

        self._log('INFO', f"Action '{mode}' for {symbol} — passed all filters",
                  {'symbol': symbol, 'mint': token_info.get('token_mint'),
                   'mode': mode, 'platform': token_info.get('platform', 'unknown'),
                   'liquidity': token_info.get('liquidity', 0),
                   'market_cap': token_info.get('market_cap', 0),
                   'risk_score': token_info.get('risk_score', 0)})

        if self.notification_manager:
            try:
                self.notification_manager.notify_sniper_detection({
                    'symbol':       symbol,
                    'name':         token_info.get('name', symbol),
                    'platform':     token_info.get('platform', 'unknown'),
                    'liquidity':    token_info.get('liquidity', 0),
                    'market_cap':   token_info.get('market_cap', 0),
                    'risk_score':   token_info.get('risk_score', 0),
                    'action_taken': mode,
                    'address':      token_info.get('token_mint', ''),
                })
            except Exception as e:
                print(f"[SNIPER] Notification failed: {e}")

        # Open a simulation position to track TP/SL
        if mode == self.MODE_SIMULATION:
            self._open_sim_position(token_info)

        # Record to history
        entry = {
            'id':            str(uuid.uuid4()),
            'timestamp':     datetime.utcnow().isoformat() + 'Z',
            'mode':          mode,
            'token_symbol':  symbol,
            'token_name':    token_info.get('name', symbol),
            'token_mint':    token_info.get('token_mint', ''),
            'platform':      token_info.get('platform', 'unknown'),
            'liquidity':     token_info.get('liquidity', 0),
            'market_cap':    token_info.get('market_cap', 0),
            'price_usd':     token_info.get('price_usd', 0),
            'entry_price_usd': token_info.get('entry_price_usd', token_info.get('price_usd', 0)),
            'risk_score':    token_info.get('risk_score', 0),
            'action_taken':  mode,
            'status':        'success',
            'solscan_url':   token_info.get('solscan_url', ''),
            'dexscreener_url': token_info.get('dexscreener_url', ''),
        }
        self.history.insert(0, entry)
        if len(self.history) > 1000:
            self.history = self.history[:1000]
        self._save_history()

        # Record sniper decision in DB for AI training data
        if self.db:
            try:
                self.db.update_token_sniper_decision(
                    mint=token_info.get('token_mint', ''),
                    status='passed_filters',
                    action=mode,
                    risk_score=token_info.get('risk_score'),
                    risk_level=token_info.get('risk_level'),
                    risk_checks=token_info.get('risk_checks'),
                    ml_score=token_info.get('ml_score'),
                    pump_probability=token_info.get('pump_probability'),
                )
            except Exception as e:
                print(f"[SNIPER] DB decision update error: {e}")

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------

    def _filter_fail_reason(self, token_info: Dict) -> str:
        """
        Return a human-readable string explaining why the token fails market/timing
        filters, or an empty string if it passes all of them.

        NOTE: risk_score (anti-scam) is intentionally NOT checked here because
        the score is only valid after AntiScamAnalyzer.check_token() has run.
        Checking it here against the default value of 100 would reject every token
        before analysis even starts.  The risk gate lives in _run_ai_and_execute().
        """
        filters    = self.sniper_config.get('filters', {})
        liquidity  = token_info.get('liquidity', token_info.get('initial_liquidity', 0))
        market_cap = token_info.get('market_cap', 0)
        volume     = token_info.get('volume_1h', token_info.get('initial_volume', 0))
        platform   = token_info.get('platform', '')

        min_liq = filters.get('min_liquidity', 0)
        if liquidity < min_liq:
            return f"liq ${liquidity:.0f} < min ${min_liq:.0f}"

        max_liq = filters.get('max_liquidity')
        if max_liq and liquidity > max_liq:
            return f"liq ${liquidity:.0f} > max ${max_liq:.0f}"

        min_mc = filters.get('min_market_cap', 0)
        if market_cap < min_mc:
            return f"MC ${market_cap:.0f} < min ${min_mc:.0f}"

        max_mc = filters.get('max_market_cap')
        if max_mc and market_cap > max_mc:
            return f"MC ${market_cap:.0f} > max ${max_mc:.0f}"

        min_vol = filters.get('min_initial_volume', 0)
        if volume < min_vol:
            return f"vol ${volume:.0f} < min ${min_vol:.0f}"

        def _norm(s: str) -> str:
            return s.lower().replace('.', '').replace(' ', '').replace('_', '')

        allowed = filters.get('platforms', [])
        if allowed and _norm(platform) not in [_norm(p) for p in allowed]:
            return f"platform '{platform}' not allowed"

        return ''

    def _passes_filters(self, token_info: Dict) -> bool:
        """Convenience wrapper — returns True if _filter_fail_reason is empty."""
        return self._filter_fail_reason(token_info) == ''

    # ------------------------------------------------------------------
    # Price snapshot helper
    # ------------------------------------------------------------------

    def _record_price_snapshot(self, token: Dict):
        """Append a price/MC snapshot to token['price_snapshots']."""
        snapshots = token.setdefault('price_snapshots', [])
        snapshots.append({
            'ts':         datetime.utcnow().isoformat() + 'Z',
            'price_usd':  token.get('price_usd', 0),
            'market_cap': token.get('market_cap', 0),
            'liquidity':  token.get('liquidity', 0),
        })
        # Keep only the last N snapshots
        if len(snapshots) > self.MAX_PRICE_SNAPSHOTS:
            token['price_snapshots'] = snapshots[-self.MAX_PRICE_SNAPSHOTS:]
