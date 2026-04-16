"""
Copy Trading Engine
Monitors target wallets and copies/simulates their trades with configurable
follow modes and execution modes.

Follow modes:
    simple  — monitors only the wallet address you explicitly add.
    deep    — also auto-monitors sub-wallets funded by the target wallet,
              capturing trades made via delegate/proxy wallets (one level only,
              no recursion).

Execution modes:
    notify    — alert only, no virtual or real execution.
    simulate  — open/close virtual positions to track what the P&L would be.
    auto      — simulate + fixed SOL amount per copy (real execution stub).
    precise   — simulate + scale the copy amount proportionally to match the
                whale's portfolio percentage, applied to the assigned wallet balance.
"""

import json
import os
import uuid
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional


class CopyTradingEngine:
    """
    Core engine for copy trading.

    Each monitored wallet carries its own complete configuration
    (follow_mode, execution_mode, filters, execution params, limits).
    There is no separate "rules" concept — the wallet IS the rule.
    """

    FOLLOW_SIMPLE = 'simple'
    FOLLOW_DEEP   = 'deep'

    MODE_NOTIFY   = 'notify'
    MODE_SIMULATE = 'simulate'
    MODE_AUTO     = 'auto'
    MODE_PRECISE  = 'precise'

    # How often open simulation positions prices are refreshed (seconds)
    SIM_CHECK_INTERVAL = 5

    def __init__(self, config, wallet_monitor=None, notification_manager=None,
                 logging_engine=None, wallet_manager=None, token_detector=None,
                 db=None):
        """
        Initialize the Copy Trading Engine.

        Args:
            config:               ConfigManager instance
            wallet_monitor:       Optional WalletMonitor for real-time tx detection
            notification_manager: Optional NotificationManager for alerts
            logging_engine:       Optional LoggingEngine for structured logs
            wallet_manager:       Optional WalletManager (precise mode balance lookup)
            token_detector:       Optional TokenDetector (price fetching for sim P&L)
            db:                   Optional DatabaseManager for persistent storage
        """
        self.config               = config
        self.rules_dir            = config.get_data_path('rules')
        self.wallets_file         = self.rules_dir / 'ct_wallets.json'

        self.wallet_monitor        = wallet_monitor
        self.notification_manager  = notification_manager
        self.logging_engine        = logging_engine
        self.wallet_manager        = wallet_manager
        self.token_detector        = token_detector
        self.db                    = db

        self._lock = threading.Lock()
        self.is_running = False
        self._sim_thread: Optional[threading.Thread] = None

        # Persisted state — wallets still use JSON; positions/history use DB when available
        self.monitored_wallets: Dict[str, Dict] = self._load_monitored_wallets()
        self.history:           List[Dict]       = self._load_history()
        self.sim_positions:     List[Dict]       = self._load_sim_positions()

        # Register transaction callback with the wallet monitor
        if self.wallet_monitor:
            self.wallet_monitor.on_transaction(self._handle_transaction)

    # ------------------------------------------------------------------
    # Internal logging helper
    # ------------------------------------------------------------------

    def _log(self, level: str, message: str, data: Optional[Dict] = None) -> None:
        """Write a structured log entry and print to console."""
        print(f"[COPY_TRADING] {message}")
        if self.logging_engine:
            self.logging_engine.log(level=level, message=message,
                                    module='copy_trading', data=data or {})

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_monitored_wallets(self) -> Dict[str, Dict]:
        if os.path.exists(self.wallets_file):
            try:
                with open(self.wallets_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_monitored_wallets(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.wallets_file), exist_ok=True)
            with open(self.wallets_file, 'w') as f:
                json.dump(self.monitored_wallets, f, indent=2)
        except Exception as e:
            print(f"[COPY_TRADING] Error saving wallets: {e}")

    def _load_history(self) -> List[Dict]:
        """Load copy trading history from DB."""
        if self.db:
            try:
                return self.db.get_ct_history(limit=2000)
            except Exception:
                pass
        return []

    def _save_history(self) -> None:
        """History is persisted per-entry via DB in _add_history()."""
        pass

    def _load_sim_positions(self) -> List[Dict]:
        """Load copy trading simulation positions from DB."""
        if self.db:
            try:
                return self.db.get_ct_positions(limit=2000)
            except Exception:
                pass
        return []

    def _save_sim_positions(self) -> None:
        """Positions are persisted individually via DB on open/close."""
        pass

    # ------------------------------------------------------------------
    # Wallet CRUD
    # ------------------------------------------------------------------

    def add_wallet(self, data: Dict) -> Dict:
        """
        Add a wallet to monitor with its full configuration.

        Args:
            data: Dict with keys: address, label, follow_mode, execution_mode,
                  filters (dict), execution (dict), limits (dict)

        Returns:
            Created wallet config dict
        """
        address = data.get('address', '').strip()
        if not address:
            raise ValueError("address is required")

        wallet = {
            'address':        address,
            'label':          data.get('label', f"Wallet {address[:8]}"),
            'follow_mode':    data.get('follow_mode',    self.FOLLOW_SIMPLE),
            'execution_mode': data.get('execution_mode', self.MODE_NOTIFY),
            'enabled':        data.get('enabled', True),
            'filters': {
                'operation_types':   data.get('filters', {}).get('operation_types',   ['buy', 'sell']),
                'min_trade_sol':     data.get('filters', {}).get('min_trade_sol',     0.05),
                'min_liquidity':     data.get('filters', {}).get('min_liquidity',     0),
                'max_market_cap':    data.get('filters', {}).get('max_market_cap',    None),
                'allowed_platforms': data.get('filters', {}).get('allowed_platforms', []),
            },
            'execution': {
                'fixed_amount_sol': data.get('execution', {}).get('fixed_amount_sol', 0.1),
                'copy_percentage':  data.get('execution', {}).get('copy_percentage',  5.0),
                'max_slippage':     data.get('execution', {}).get('max_slippage',     5.0),
            },
            'limits': {
                'max_buys_per_hour': data.get('limits', {}).get('max_buys_per_hour', 5),
                'max_position_sol':  data.get('limits', {}).get('max_position_sol',  1.0),
            },
            'added_at':      datetime.utcnow().isoformat() + 'Z',
            'is_sub_wallet': False,
            'parent_wallet': None,
            'stats': {
                'trades_detected': 0,
                'trades_copied':   0,
                'last_trade_at':   None,
            },
        }

        self.monitored_wallets[address] = wallet
        self._save_monitored_wallets()

        if self.wallet_monitor and self.is_running:
            self.wallet_monitor.add_wallet(address, wallet['label'])

        self._log('INFO', f"Added wallet: {wallet['label']} ({address[:12]}…)",
                  {'address': address, 'follow_mode': wallet['follow_mode'],
                   'execution_mode': wallet['execution_mode']})

        return wallet

    def update_wallet(self, address: str, data: Dict) -> Optional[Dict]:
        """
        Update an existing wallet configuration.

        Nested sections (filters, execution, limits) are deep-merged so
        callers only need to send the fields they want to change.
        """
        wallet = self.monitored_wallets.get(address)
        if not wallet:
            return None

        for section in ('filters', 'execution', 'limits', 'stats'):
            if section in data and isinstance(data[section], dict):
                wallet[section].update(data.pop(section))

        wallet.update(data)
        wallet['updated_at'] = datetime.utcnow().isoformat() + 'Z'
        self._save_monitored_wallets()
        return wallet

    def remove_wallet(self, address: str) -> bool:
        """Remove a monitored wallet and unsubscribe from WalletMonitor."""
        if address not in self.monitored_wallets:
            return False

        label = self.monitored_wallets[address].get('label', address[:12])
        del self.monitored_wallets[address]
        self._save_monitored_wallets()

        if self.wallet_monitor:
            self.wallet_monitor.remove_wallet(address)

        self._log('INFO', f"Removed wallet: {label} ({address[:12]}…)")
        return True

    def get_wallet(self, address: str) -> Optional[Dict]:
        return self.monitored_wallets.get(address)

    def get_all_wallets(self) -> List[Dict]:
        return list(self.monitored_wallets.values())

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start monitoring and the simulation price-check thread."""
        self.is_running = True

        if self.wallet_monitor:
            try:
                for addr, w in self.monitored_wallets.items():
                    self.wallet_monitor.add_wallet(addr, w.get('label', ''))
                self.wallet_monitor.start()
            except Exception as e:
                self._log('ERROR', f"Error starting wallet monitor: {e}")

        self._sim_thread = threading.Thread(
            target=self._sim_loop,
            name="CTSimMonitor",
            daemon=True,
        )
        self._sim_thread.start()

        self._log('INFO', "Copy trading engine started",
                  {'wallets': len(self.monitored_wallets),
                   'sim_interval_s': self.SIM_CHECK_INTERVAL})
        return True

    def stop(self) -> bool:
        """Stop monitoring and the simulation thread."""
        self.is_running = False

        if self.wallet_monitor:
            try:
                self.wallet_monitor.stop()
            except Exception as e:
                self._log('ERROR', f"Error stopping wallet monitor: {e}")

        self._sim_thread = None
        self._log('INFO', "Copy trading engine stopped")
        return True

    # ------------------------------------------------------------------
    # Transaction handler
    # ------------------------------------------------------------------

    def _handle_transaction(self, trade_info: Dict) -> None:
        """
        Entry point called by WalletMonitor for every detected transaction.

        Dispatches to _process_buy / _process_sell / _process_transfer
        depending on the detected operation type.
        """
        if not self.is_running:
            return

        source_addr = trade_info.get('wallet_address', '')
        wallet_cfg  = self.monitored_wallets.get(source_addr)
        if not wallet_cfg or not wallet_cfg.get('enabled', True):
            return

        op_type      = trade_info.get('type', 'unknown')
        token_symbol = trade_info.get('token_symbol', '???')
        amount_sol   = float(trade_info.get('sol_change',
                             trade_info.get('amount_sol', 0.0)))

        self._log('INFO',
                  f"[{wallet_cfg['label']}] {op_type}: {token_symbol} ({amount_sol:.4f} SOL)",
                  {'wallet':    source_addr,
                   'type':      op_type,
                   'token':     token_symbol,
                   'amount':    amount_sol,
                   'signature': trade_info.get('signature', '')})

        # Update detection stats
        wallet_cfg['stats']['trades_detected'] += 1
        wallet_cfg['stats']['last_trade_at']    = datetime.utcnow().isoformat() + 'Z'

        if op_type == 'buy':
            self._process_buy(wallet_cfg, trade_info)
        elif op_type == 'sell':
            self._process_sell(wallet_cfg, trade_info)
        elif op_type in ('transfer', 'transfer_out'):
            self._process_transfer(wallet_cfg, trade_info)

        self._save_monitored_wallets()

    def _passes_filters(self, wallet_cfg: Dict, trade_info: Dict) -> bool:
        """Return True if the trade passes the wallet's configured filters."""
        filters    = wallet_cfg.get('filters', {})
        op_type    = trade_info.get('type', 'unknown')
        amount_sol = float(trade_info.get('sol_change', trade_info.get('amount_sol', 0.0)))
        liquidity  = trade_info.get('liquidity', 0)
        mc         = trade_info.get('market_cap', 0)
        platform   = trade_info.get('dex', trade_info.get('platform', ''))

        allowed_ops = filters.get('operation_types', ['buy', 'sell'])
        if op_type not in allowed_ops:
            return False

        min_sol = filters.get('min_trade_sol', 0)
        if amount_sol < min_sol:
            return False

        min_liq = filters.get('min_liquidity', 0)
        if min_liq and liquidity < min_liq:
            return False

        max_mc = filters.get('max_market_cap')
        if max_mc and mc and mc > max_mc:
            return False

        allowed_platforms = filters.get('allowed_platforms', [])
        if allowed_platforms and platform:
            if platform.lower() not in [p.lower() for p in allowed_platforms]:
                return False

        return True

    def _calculate_amount(self, wallet_cfg: Dict) -> float:
        """
        Calculate the SOL amount to invest for auto/precise modes.

        precise: our_amount = our_wallet_balance × copy_percentage / 100
        auto:    our_amount = fixed_amount_sol (constant per trade)
        Both are capped at limits.max_position_sol.
        """
        mode    = wallet_cfg['execution_mode']
        limits  = wallet_cfg.get('limits', {})
        max_pos = limits.get('max_position_sol', 1.0)

        if mode == self.MODE_PRECISE:
            copy_pct   = wallet_cfg['execution'].get('copy_percentage', 5.0)
            our_wallet = None
            if self.wallet_manager:
                our_wallet = self.wallet_manager.get_wallet_for_module('copy_trading')
            if our_wallet:
                our_balance = our_wallet.get('balance', {}).get('sol', 0.0)
                amount = our_balance * copy_pct / 100.0
            else:
                amount = wallet_cfg['execution'].get('fixed_amount_sol', 0.1)
        else:
            # MODE_AUTO or fallback
            amount = wallet_cfg['execution'].get('fixed_amount_sol', 0.1)

        return min(float(amount), float(max_pos))

    def _process_buy(self, wallet_cfg: Dict, trade_info: Dict) -> None:
        """Handle a buy detected from a monitored wallet."""
        if not self._passes_filters(wallet_cfg, trade_info):
            return

        mode   = wallet_cfg['execution_mode']
        symbol = trade_info.get('token_symbol', '???')

        if mode == self.MODE_NOTIFY:
            self._add_history(wallet_cfg, trade_info, 'buy', None)
            self._notify(wallet_cfg, trade_info, 'buy')

        elif mode == self.MODE_SIMULATE:
            amount = wallet_cfg['execution'].get('fixed_amount_sol', 0.1)
            self._open_sim_position(wallet_cfg, trade_info, amount)
            self._add_history(wallet_cfg, trade_info, 'buy', amount)
            self._notify(wallet_cfg, trade_info, 'buy')
            wallet_cfg['stats']['trades_copied'] += 1

        elif mode in (self.MODE_AUTO, self.MODE_PRECISE):
            amount = self._calculate_amount(wallet_cfg)
            self._open_sim_position(wallet_cfg, trade_info, amount)
            self._add_history(wallet_cfg, trade_info, 'buy', amount)
            self._notify(wallet_cfg, trade_info, 'buy')
            wallet_cfg['stats']['trades_copied'] += 1
            # Real tx execution is a future milestone
            self._log('WARNING',
                      f"[{wallet_cfg['label']}] {mode} buy recorded as sim — "
                      f"real execution not yet implemented",
                      {'symbol': symbol, 'amount_sol': amount})

    def _process_sell(self, wallet_cfg: Dict, trade_info: Dict) -> None:
        """
        Handle a sell detected from a monitored wallet.
        Closes any open simulation positions for the same wallet + token.
        """
        if not self._passes_filters(wallet_cfg, trade_info):
            return

        mode       = wallet_cfg['execution_mode']
        token_mint = trade_info.get('token_mint')

        # Close open sim positions for this wallet + token
        if token_mint and mode != self.MODE_NOTIFY:
            exit_price = float(trade_info.get('price', 0.0))
            if not exit_price:
                exit_price = self._get_current_price(token_mint) or 0.0
            self._close_sim_positions_for_token(
                wallet_cfg['address'], token_mint, exit_price, 'whale_sold'
            )

        self._add_history(wallet_cfg, trade_info, 'sell', None)
        self._notify(wallet_cfg, trade_info, 'sell')

        if mode != self.MODE_NOTIFY:
            wallet_cfg['stats']['trades_copied'] += 1

    def _process_transfer(self, wallet_cfg: Dict, trade_info: Dict) -> None:
        """
        Handle an outgoing transfer.

        In deep follow mode, the destination is automatically added as a
        sub-wallet inheriting the parent's configuration (but with
        follow_mode=simple to prevent infinite recursion).
        """
        if wallet_cfg.get('follow_mode') != self.FOLLOW_DEEP:
            return

        dest = (trade_info.get('destination')
                or trade_info.get('to_address', ''))
        if not dest or dest in self.monitored_wallets:
            return

        sub = dict(wallet_cfg)
        sub['address']       = dest
        sub['label']         = f"{wallet_cfg['label']} (sub-{dest[:6]})"
        sub['follow_mode']   = self.FOLLOW_SIMPLE   # no recursion
        sub['is_sub_wallet'] = True
        sub['parent_wallet'] = wallet_cfg['address']
        sub['added_at']      = datetime.utcnow().isoformat() + 'Z'
        sub['stats']         = {'trades_detected': 0,
                                'trades_copied':   0,
                                'last_trade_at':   None}

        self.monitored_wallets[dest] = sub
        self._save_monitored_wallets()

        if self.wallet_monitor and self.is_running:
            self.wallet_monitor.add_wallet(dest, sub['label'])

        self._log('INFO',
                  f"Deep follow: added sub-wallet {dest[:12]}… "
                  f"(parent: {wallet_cfg['label']})",
                  {'parent': wallet_cfg['address'], 'sub': dest})

    def _notify(self, wallet_cfg: Dict, trade_info: Dict, action: str) -> None:
        if not self.notification_manager:
            return
        try:
            self.notification_manager.notify_copy_trade({
                'mode':           wallet_cfg['execution_mode'],
                'action':         action,
                'token_symbol':   trade_info.get('token_symbol', '???'),
                'amount_sol':     trade_info.get('sol_change', 0.0),
                'wallet_address': wallet_cfg['address'],
                'wallet_label':   wallet_cfg['label'],
                'signature':      trade_info.get('signature', ''),
            })
        except Exception as e:
            self._log('ERROR', f"Notification error: {e}")

    def _add_history(self, wallet_cfg: Dict, trade_info: Dict,
                     action: str, our_amount: Optional[float]) -> None:
        """Record a copy trade action in the activity history."""
        entry = {
            'id':               str(uuid.uuid4()),
            'timestamp':        datetime.utcnow().isoformat() + 'Z',
            'source_wallet':    wallet_cfg['address'],
            'source_label':     wallet_cfg['label'],
            'execution_mode':   wallet_cfg['execution_mode'],
            'action':           action,
            'token_symbol':     trade_info.get('token_symbol', '???'),
            'token_mint':       trade_info.get('token_mint', ''),
            'whale_amount_sol': float(trade_info.get('sol_change',
                                      trade_info.get('amount_sol', 0.0))),
            'our_amount_sol':   our_amount,
            'price_usd':        float(trade_info.get('price', 0.0)),
            'signature':        trade_info.get('signature', ''),
            'dex':              trade_info.get('dex', 'unknown'),
        }
        self.history.insert(0, entry)
        if len(self.history) > 2000:
            self.history = self.history[:2000]
        if self.db:
            try:
                self.db.save_ct_history_entry(entry)
            except Exception as e:
                print(f"[COPY_TRADING] DB history save error: {e}")
        self._save_history()

    # ------------------------------------------------------------------
    # Simulation positions
    # ------------------------------------------------------------------

    def _open_sim_position(self, wallet_cfg: Dict, trade_info: Dict,
                            amount: float) -> None:
        """Open a new virtual position when a buy is copied."""
        fee_entry_pct = 1.25
        fee_entry_sol = round(amount * fee_entry_pct / 100, 6)
        amount_net    = round(amount - fee_entry_sol, 6)

        # trade_info.price is rarely populated by the wallet monitor; fetch live.
        entry_price = float(trade_info.get('price', 0.0))
        if not entry_price:
            entry_price = self._get_current_price(
                trade_info.get('token_mint', '')) or 0.0

        position = {
            'id':                str(uuid.uuid4()),
            'source_wallet':     wallet_cfg['address'],
            'source_label':      wallet_cfg['label'],
            'execution_mode':    wallet_cfg['execution_mode'],
            'token_mint':        trade_info.get('token_mint', ''),
            'token_symbol':      trade_info.get('token_symbol', '???'),
            'platform':          trade_info.get('dex', 'unknown'),
            'entry_price':       entry_price,
            'entry_time':        datetime.utcnow().isoformat() + 'Z',
            'entry_mc':          float(trade_info.get('market_cap', 0.0)),
            'simulated_sol':     amount,
            'simulated_sol_net': amount_net,
            'fee_entry_sol':     fee_entry_sol,
            'fee_entry_pct':     fee_entry_pct,
            'fee_exit_pct':      1.25,
            'current_price':     entry_price,
            'pnl_percent':       0.0,
            'pnl_sol':           0.0,
            'fees_sol':          fee_entry_sol,
            'status':            'open',
            'exit_price':        None,
            'exit_time':         None,
            'exit_reason':       None,
            'whale_tx':          trade_info.get('signature', ''),
            'last_updated':      datetime.utcnow().isoformat() + 'Z',
        }

        with self._lock:
            self.sim_positions.insert(0, position)
            if self.db:
                try:
                    self.db.save_ct_position(position)
                except Exception as e:
                    print(f"[COPY_TRADING] DB position save error: {e}")
            self._save_sim_positions()

        self._log('INFO',
                  f"SIM opened: {position['token_symbol']} @ ${entry_price:.8f} "
                  f"({amount} SOL gross / {amount_net:.4f} SOL net) "
                  f"[{wallet_cfg['label']}]",
                  {'symbol':      position['token_symbol'],
                   'mint':        position['token_mint'],
                   'entry_price': entry_price,
                   'amount':      amount,
                   'wallet':      wallet_cfg['label']})

    def _apply_close(self, pos: Dict, exit_price: float, reason: str) -> None:
        """Apply closing math to a position dict in-place."""
        entry_price  = pos.get('entry_price', 0.0)
        amount_net   = pos.get('simulated_sol_net', pos.get('simulated_sol', 0.1))
        amount_gross = pos.get('simulated_sol', 0.1)
        fee_exit_pct = pos.get('fee_exit_pct', 1.25)

        if entry_price and exit_price:
            change_pct       = (exit_price - entry_price) / entry_price * 100
            gross_return_sol = amount_net * change_pct / 100
            proceeds_sol     = amount_net + gross_return_sol
            fee_exit_sol     = round(proceeds_sol * fee_exit_pct / 100, 6)
            net_pnl_sol      = round(gross_return_sol - fee_exit_sol, 6)
            net_pnl_pct      = round(net_pnl_sol / amount_gross * 100, 2) if amount_gross else 0.0
        else:
            fee_exit_sol = 0.0
            net_pnl_sol  = 0.0
            net_pnl_pct  = 0.0

        pos['status']       = 'closed'
        pos['exit_price']   = exit_price
        pos['exit_time']    = datetime.utcnow().isoformat() + 'Z'
        pos['exit_reason']  = reason
        pos['fee_exit_sol'] = fee_exit_sol
        pos['pnl_sol']      = net_pnl_sol
        pos['pnl_percent']  = net_pnl_pct
        pos['fees_sol']     = round(pos.get('fee_entry_sol', 0.0) + fee_exit_sol, 6)

        if self.db:
            try:
                self.db.save_ct_position(pos)
            except Exception as e:
                print(f"[COPY_TRADING] DB position close error: {e}")

        self._log('INFO',
                  f"SIM closed ({reason}): {pos.get('token_symbol','?')} "
                  f"→ {net_pnl_pct:+.2f}% ({net_pnl_sol:+.4f} SOL)",
                  {'symbol':      pos.get('token_symbol'),
                   'mint':        pos.get('token_mint'),
                   'pnl_pct':     net_pnl_pct,
                   'pnl_sol':     net_pnl_sol,
                   'exit_reason': reason})

    def _close_sim_positions_for_token(self, source_wallet: str, token_mint: str,
                                        exit_price: float, reason: str) -> None:
        """Close all open sim positions for a given wallet + token."""
        changed = False
        with self._lock:
            for pos in self.sim_positions:
                if (pos.get('status') == 'open'
                        and pos.get('source_wallet') == source_wallet
                        and pos.get('token_mint')    == token_mint):
                    price = exit_price or pos.get('current_price', 0.0)
                    self._apply_close(pos, price, reason)
                    changed = True
            if changed:
                self._save_sim_positions()

    def _get_current_price(self, mint: str) -> Optional[float]:
        """
        Fetch the latest price for a token.
        Uses DexScreener first, falls back to pump.fun bonding curve.
        """
        if not self.token_detector or not mint:
            return None
        try:
            dex  = self.token_detector.dexscreener
            data = dex.get_token_data(mint, retries=1, retry_delay=0)
            if data and data.get('price_usd'):
                return float(data['price_usd'])
            rpc   = self.token_detector.rpc
            curve = rpc.get_pumpfun_bonding_curve(mint)
            if curve:
                sol_price = dex.get_sol_price_usd()
                return curve['price_sol'] * sol_price
        except Exception:
            pass
        return None

    def _check_sim_positions(self) -> None:
        """
        Refresh P&L for open simulation positions without auto-closing them.
        Copy trading positions close when the source wallet sells the token
        (or via manual close) — not at a fixed TP/SL level.
        """
        with self._lock:
            open_pos = [p for p in self.sim_positions if p.get('status') == 'open']

        if not open_pos:
            return

        changed = False
        for pos in open_pos:
            mint        = pos.get('token_mint', '')
            entry_price = pos.get('entry_price', 0.0)
            if not mint:
                continue

            current_price = self._get_current_price(mint)
            if current_price is None:
                continue

            # Backfill entry_price if it was 0 when the position was opened
            if not entry_price:
                pos['entry_price'] = current_price
                entry_price = current_price

            amount_net   = pos.get('simulated_sol_net', pos.get('simulated_sol', 0.1))
            amount_gross = pos.get('simulated_sol', 0.1)
            fee_exit_pct = pos.get('fee_exit_pct', 1.25)

            change_pct       = (current_price - entry_price) / entry_price * 100
            gross_return_sol = amount_net * change_pct / 100
            proceeds_sol     = amount_net + gross_return_sol
            fee_exit_sol     = round(proceeds_sol * fee_exit_pct / 100, 6)
            net_pnl_sol      = round(gross_return_sol - fee_exit_sol, 6)
            net_pnl_pct      = round(net_pnl_sol / amount_gross * 100, 2) if amount_gross else 0.0

            pos['current_price'] = current_price
            pos['pnl_percent']   = net_pnl_pct
            pos['pnl_sol']       = net_pnl_sol
            pos['fees_sol']      = round(pos.get('fee_entry_sol', 0.0) + fee_exit_sol, 6)
            pos['last_updated']  = datetime.utcnow().isoformat() + 'Z'
            changed = True

        if changed:
            with self._lock:
                self._save_sim_positions()

    def _sim_loop(self) -> None:
        """Background thread: refreshes sim position prices periodically."""
        print("[COPY_TRADING] Sim monitoring loop running")
        while self.is_running:
            try:
                self._check_sim_positions()
            except Exception as e:
                print(f"[COPY_TRADING] Sim check error: {e}")
            time.sleep(self.SIM_CHECK_INTERVAL)
        print("[COPY_TRADING] Sim monitoring loop stopped")

    # ------------------------------------------------------------------
    # Public API — simulation
    # ------------------------------------------------------------------

    def get_sim_positions(self, status_filter: Optional[str] = None,
                          limit: int = 200) -> List[Dict]:
        with self._lock:
            positions = list(self.sim_positions)
        if status_filter:
            positions = [p for p in positions if p.get('status') == status_filter]
        return positions[:limit]

    def get_sim_stats(self) -> Dict:
        with self._lock:
            positions = list(self.sim_positions)

        open_pos   = [p for p in positions if p['status'] == 'open']
        closed_pos = [p for p in positions if p['status'] == 'closed']

        realized_sol   = sum(p.get('pnl_sol', 0.0) for p in closed_pos)
        unrealized_sol = sum(p.get('pnl_sol', 0.0) for p in open_pos)
        total_invested = sum(p.get('simulated_sol', 0.0) for p in positions)

        wins    = [p for p in closed_pos if p.get('pnl_sol', 0.0) > 0]
        all_pnl = [p.get('pnl_percent', 0.0) for p in positions
                   if p.get('pnl_percent') is not None]

        return {
            'total_trades':       len(positions),
            'open_count':         len(open_pos),
            'closed_count':       len(closed_pos),
            'win_count':          len(wins),
            'win_rate':           round(len(wins) / len(closed_pos) * 100, 1) if closed_pos else 0.0,
            'realized_pnl_sol':   round(realized_sol, 4),
            'unrealized_pnl_sol': round(unrealized_sol, 4),
            'total_pnl_sol':      round(realized_sol + unrealized_sol, 4),
            'total_invested_sol': round(total_invested, 4),
            'roi_percent':        round((realized_sol + unrealized_sol) / total_invested * 100, 2)
                                  if total_invested else 0.0,
            'best_trade_pct':     round(max(all_pnl), 2) if all_pnl else 0.0,
            'worst_trade_pct':    round(min(all_pnl), 2) if all_pnl else 0.0,
            'avg_trade_pct':      round(sum(all_pnl) / len(all_pnl), 2) if all_pnl else 0.0,
        }

    def refresh_sim_prices(self) -> None:
        """Force an immediate price refresh for all open sim positions."""
        self._check_sim_positions()

    def reset_sim_positions(self) -> bool:
        """Clear all simulation positions."""
        with self._lock:
            self.sim_positions = []
            self._save_sim_positions()
        return True

    def close_sim_position(self, position_id: str) -> bool:
        """Manually close a single open simulation position at current price."""
        with self._lock:
            for pos in self.sim_positions:
                if pos.get('id') == position_id and pos.get('status') == 'open':
                    price = (self._get_current_price(pos.get('token_mint', ''))
                             or pos.get('current_price', 0.0))
                    self._apply_close(pos, price, 'manual')
                    self._save_sim_positions()
                    return True
        return False

    # ------------------------------------------------------------------
    # Public API — history / status
    # ------------------------------------------------------------------

    def get_history(self, limit: int = 50) -> List[Dict]:
        return self.history[:limit]

    def get_status(self) -> Dict:
        today = datetime.utcnow().date()
        trades_today = sum(
            1 for e in self.history
            if 'timestamp' in e
            and datetime.fromisoformat(e['timestamp'].replace('Z', '')).date() == today
        )
        return {
            'running':           self.is_running,
            'monitored_wallets': len([w for w in self.monitored_wallets.values()
                                      if not w.get('is_sub_wallet')]),
            'sub_wallets':       len([w for w in self.monitored_wallets.values()
                                      if w.get('is_sub_wallet')]),
            'total_wallets':     len(self.monitored_wallets),
            'trades_today':      trades_today,
        }

    def get_metrics(self) -> Dict:
        sim = self.get_sim_stats()
        return {
            'total_copied':      len(self.history),
            'open_simulations':  sim['open_count'],
            'sim_win_rate':      sim['win_rate'],
            'sim_total_pnl_sol': sim['total_pnl_sol'],
            'running':           self.is_running,
        }

    # ------------------------------------------------------------------
    # Legacy compatibility shims (old rules-based API)
    # Kept so existing app.py endpoints don't break during transition.
    # ------------------------------------------------------------------

    def get_all_rules(self) -> List[Dict]:
        return self.get_all_wallets()

    def get_rule(self, rule_id: str) -> Optional[Dict]:
        return self.monitored_wallets.get(rule_id)

    def create_rule(self, data: Dict) -> Dict:
        # map old field names to new
        if 'target_wallet' in data and 'address' not in data:
            data['address'] = data.pop('target_wallet')
        if 'name' in data and 'label' not in data:
            data['label'] = data.pop('name')
        return self.add_wallet(data)

    def update_rule(self, rule_id: str, data: Dict) -> Optional[Dict]:
        return self.update_wallet(rule_id, data)

    def delete_rule(self, rule_id: str) -> bool:
        return self.remove_wallet(rule_id)

    def toggle_rule(self, rule_id: str) -> bool:
        w = self.monitored_wallets.get(rule_id)
        if not w:
            return False
        w['enabled'] = not w.get('enabled', True)
        self._save_monitored_wallets()
        return True

    def get_monitored_wallets(self) -> List[Dict]:
        return self.get_all_wallets()

    def add_monitored_wallet(self, address: str, name: str,
                              rules: Optional[List] = None) -> Dict:
        return self.add_wallet({'address': address, 'label': name})

    def remove_monitored_wallet(self, wallet_address: str) -> bool:
        return self.remove_wallet(wallet_address)
