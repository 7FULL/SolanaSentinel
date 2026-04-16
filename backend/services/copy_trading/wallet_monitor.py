"""
Wallet Monitor for Copy Trading
Real-time monitoring of target wallets using WebSocket.
Detects and parses transactions (buys, sells, swaps) for copy trading.
"""

import logging
import time
from typing import Dict, List, Callable, Optional
from datetime import datetime
from solders.pubkey import Pubkey


class WalletMonitor:
    """
    Monitors target wallets for trading activity.
    Uses WebSocket subscriptions to detect transactions in real-time.
    """

    def __init__(self, websocket_manager, rpc_client):
        """
        Initialize wallet monitor.

        Args:
            websocket_manager: WebSocketManager instance
            rpc_client: SolanaRPCClient instance
        """
        self.ws_manager = websocket_manager
        self.rpc = rpc_client
        self.logger = logging.getLogger(__name__)

        # Monitored wallets
        self.monitored_wallets: Dict[str, Dict] = {}  # address -> wallet info
        self.subscriptions: Dict[str, int] = {}  # address -> subscription_id

        # Detected transactions cache
        self.detected_transactions: List[Dict] = []
        self.max_cache_size = 1000

        # Event callbacks
        self.on_transaction_callbacks: List[Callable] = []

        # Running state
        self.is_running = False

        # Per-wallet fetch debounce: address -> last fetch timestamp (monotonic)
        # Short cooldown so rapid trades (many account updates per second) don't
        # hammer the RPC, but long enough to batch multiple back-to-back updates.
        self._last_fetch: Dict[str, float] = {}
        self._fetch_cooldown: float = 1.0  # seconds between fetches per wallet

        # Last processed signature per wallet — used to avoid reprocessing the
        # same transaction when multiple account-update events fire for one tx,
        # and to catch up on all new signatures since the last fetch.
        self._last_signature: Dict[str, str] = {}

    def start(self):
        """
        Start wallet monitoring.
        Subscribes to all monitored wallets.
        """
        if self.is_running:
            self.logger.warning("Wallet monitor already running")
            return

        self.logger.info("Starting wallet monitor")
        self.is_running = True

        # Subscribe to all monitored wallets
        for address, wallet_info in self.monitored_wallets.items():
            self._subscribe_wallet(address)

        self.logger.info(f"Wallet monitor started ({len(self.monitored_wallets)} wallets)")

    def stop(self):
        """
        Stop wallet monitoring.
        Unsubscribes from all wallets.
        """
        if not self.is_running:
            return

        self.logger.info("Stopping wallet monitor")
        self.is_running = False

        # Unsubscribe from all wallets
        for address, sub_id in list(self.subscriptions.items()):
            try:
                self.ws_manager.unsubscribe(sub_id)
                del self.subscriptions[address]
                self.logger.debug(f"Unsubscribed from wallet: {address}")
            except Exception as e:
                self.logger.error(f"Failed to unsubscribe from {address}: {e}")

        self.logger.info("Wallet monitor stopped")

    def add_wallet(self, address: str, name: str = "Unknown", rules: Optional[List[str]] = None):
        """
        Add a wallet to monitor.

        Args:
            address: Wallet public key
            name: Display name for the wallet
            rules: List of rule IDs to apply to this wallet's transactions

        Returns:
            Wallet info dictionary
        """
        if address in self.monitored_wallets:
            self.logger.warning(f"Wallet already monitored: {address}")
            return self.monitored_wallets[address]

        wallet_info = {
            'address': address,
            'name': name,
            'rules': rules or [],
            'added_at': datetime.utcnow().isoformat(),
            'transactions_detected': 0,
            'last_activity': None
        }

        self.monitored_wallets[address] = wallet_info

        # Subscribe if monitor is running
        if self.is_running:
            self._subscribe_wallet(address)

        self.logger.info(f"Added wallet to monitor: {name} ({address})")
        return wallet_info

    def remove_wallet(self, address: str) -> bool:
        """
        Remove a wallet from monitoring.

        Args:
            address: Wallet public key

        Returns:
            True if removed, False if not found
        """
        if address not in self.monitored_wallets:
            return False

        # Unsubscribe
        if address in self.subscriptions:
            try:
                self.ws_manager.unsubscribe(self.subscriptions[address])
                del self.subscriptions[address]
            except Exception as e:
                self.logger.error(f"Failed to unsubscribe from {address}: {e}")

        # Remove from monitored list
        del self.monitored_wallets[address]
        self.logger.info(f"Removed wallet from monitoring: {address}")

        return True

    def _subscribe_wallet(self, address: str):
        """
        Subscribe to a wallet's account changes.

        Args:
            address: Wallet public key
        """
        try:
            self.logger.info(f"Subscribing to wallet: {address}")

            # Subscribe to account changes
            sub_id = self.ws_manager.subscribe_account(
                address,
                callback=lambda event: self._handle_wallet_update(address, event)
            )

            if sub_id:
                self.subscriptions[address] = sub_id
                self.logger.info(f"Subscribed to wallet: {address} (sub_id: {sub_id})")
            else:
                self.logger.error(f"Failed to subscribe to wallet: {address}")

        except Exception as e:
            self.logger.error(f"Error subscribing to wallet {address}: {e}")

    def _handle_wallet_update(self, address: str, event_data: Dict):
        """
        Handle wallet account update event.
        Detects and parses transactions.

        Args:
            address: Wallet address
            event_data: WebSocket event data
        """
        try:
            if not self.is_running:
                return

            self.logger.debug(f"Wallet update for {address}")

            # Get wallet info
            wallet_info = self.monitored_wallets.get(address)
            if not wallet_info:
                return

            # Extract transaction data
            data = event_data.get('data', {})
            value = data.get('value', {})

            # Check if this is a transaction (lamports changed)
            lamports = value.get('lamports')
            if lamports is not None:
                # Debounce: skip if we fetched for this wallet recently.
                # Account-update events can fire multiple times per transaction
                # (pre-flight, confirmation, finalization) — we only need one fetch.
                now = time.monotonic()
                if now - self._last_fetch.get(address, 0) < self._fetch_cooldown:
                    self.logger.debug(f"Skipping fetch for {address} (cooldown)")
                    return
                self._last_fetch[address] = now

                # Fetch recent signatures to get transaction details
                self._fetch_recent_transactions(address, wallet_info)

        except Exception as e:
            self.logger.error(f"Error handling wallet update for {address}: {e}")

    def _fetch_recent_transactions(self, address: str, wallet_info: Dict):
        """
        Fetch recent transactions for a wallet and parse them.

        Fetches up to 10 signatures and processes only those that are newer
        than the last processed signature, so rapid back-to-back trades are
        all captured even when account-update events fire infrequently.

        Args:
            address: Wallet address
            wallet_info: Wallet information
        """
        try:
            pubkey = Pubkey.from_string(address)
            # Fetch the 10 most recent signatures so we don't miss a burst of
            # rapid trades between two account-update event deliveries.
            signatures = self.rpc.get_signatures_for_address(pubkey, limit=10)

            if not signatures:
                return

            # Determine which signatures are new since the last processed one.
            # The API returns signatures newest-first; stop when we hit the last
            # one we already processed.
            last_seen = self._last_signature.get(address)
            new_sigs = []
            for sig_info in signatures:
                sig = sig_info.get('signature') or sig_info.get('signatureStatus', {})
                if isinstance(sig, dict):
                    sig = sig.get('signature', '')
                sig = str(sig)
                if sig == last_seen:
                    break
                new_sigs.append(sig)

            if not new_sigs:
                return

            # Process in chronological order (oldest first) so that detected
            # events appear in the correct sequence in the UI.
            for signature in reversed(new_sigs):
                try:
                    tx_details = self.rpc.get_transaction(signature)
                    if not tx_details:
                        continue

                    trade_info = self._parse_transaction(address, signature, tx_details, wallet_info)
                    if trade_info:
                        wallet_info['transactions_detected'] += 1
                        wallet_info['last_activity'] = datetime.utcnow().isoformat()
                        self._emit_transaction_detected(trade_info)
                except Exception as tx_err:
                    self.logger.error(f"Error processing signature {signature[:20]}… for {address[:8]}…: {tx_err}")

            # Mark the most recent signature as processed (first item in the
            # API response, which is newest-first).
            newest = str(signatures[0].get('signature', ''))
            if newest:
                self._last_signature[address] = newest

        except Exception as e:
            self.logger.error(f"Error fetching transactions for {address}: {e}")

    @staticmethod
    def _attr(obj, *keys, default=None):
        """
        Safely read an attribute from a solders object or a plain dict,
        trying each key in order until one succeeds.

        Solders objects use attribute access; plain dicts use key access.
        This helper lets the same code work with both.
        """
        for key in keys:
            try:
                # Try attribute access first (solders objects)
                val = getattr(obj, key, None)
                if val is not None:
                    return val
            except Exception:
                pass
            try:
                # Fallback to dict key access
                val = obj[key]
                if val is not None:
                    return val
            except Exception:
                pass
        return default

    def _parse_transaction(
        self,
        wallet_address: str,
        signature: str,
        tx_data: Dict,
        wallet_info: Dict
    ) -> Optional[Dict]:
        """
        Parse transaction data into a normalised trade_info dict.

        tx_data['meta'] is a solders UiTransactionStatusMeta object
        (not a plain dict), so all field access goes through _attr().

        Current classification is SOL-balance-based (buy = SOL out,
        sell = SOL in).  Token mint extraction requires a deeper parse
        of the inner instruction data and is left as a future improvement.
        """
        try:
            meta       = tx_data.get('meta')       if isinstance(tx_data, dict) else getattr(tx_data, 'meta', None)
            slot       = tx_data.get('slot')       if isinstance(tx_data, dict) else getattr(tx_data, 'slot', None)
            block_time = tx_data.get('block_time') if isinstance(tx_data, dict) else getattr(tx_data, 'block_time', None)

            if meta is None:
                return None

            # Bail on failed transactions
            err = self._attr(meta, 'err')
            if err is not None:
                self.logger.debug(f"Transaction failed (err={err}): {signature[:20]}…")
                return None

            # SOL balances are stored in lamports (1 SOL = 1e9 lamports)
            pre_balances  = self._attr(meta, 'pre_balances',  'preBalances',  default=[])
            post_balances = self._attr(meta, 'post_balances', 'postBalances', default=[])

            # Convert solders sequence types to plain lists if needed
            try:
                pre_balances  = list(pre_balances)
                post_balances = list(post_balances)
            except Exception:
                pre_balances  = []
                post_balances = []

            sol_change = 0.0
            if pre_balances and post_balances:
                sol_change = (post_balances[0] - pre_balances[0]) / 1_000_000_000

            # Basic classification: SOL out → buy, SOL in → sell.
            # Threshold of 0.001 SOL (1M lamports) filters out fee-only
            # transactions where the only SOL change is the ~5–20k lamport fee.
            if sol_change < -0.001:
                tx_type = 'buy'
            elif sol_change > 0.001:
                tx_type = 'sell'
            else:
                # No meaningful SOL movement — likely a fee-only tx, skip
                return None

            # --- Token mint extraction ---
            # Strategy:
            #   1. Build a dict of account_index → token balance for pre and post.
            #   2. Find entries whose `owner` matches the monitored wallet — these
            #      are the wallet's own ATAs.  The ATA is never at index 0 (that's
            #      the SOL account); filtering by owner is the correct approach.
            #   3. Among those, pick the mint whose balance changed the most
            #      (largest absolute delta) — that's the token being traded.
            #   4. If no owner field is present (older RPC format), fall back to
            #      the entry with the largest balance change across all accounts.
            token_mint   = None
            token_amount = 0.0
            try:
                pre_token  = self._attr(meta, 'pre_token_balances',  'preTokenBalances',  default=[])
                post_token = self._attr(meta, 'post_token_balances', 'postTokenBalances', default=[])
                pre_token  = list(pre_token)
                post_token = list(post_token)

                # Index pre balances by account_index for quick lookup
                pre_by_idx: dict = {}
                for tb in pre_token:
                    idx = self._attr(tb, 'account_index', 'accountIndex', default=-1)
                    pre_by_idx[idx] = tb

                best_delta = 0.0

                def _ui_amount(tb) -> float:
                    """Extract the raw ui_amount float from a token-balance entry."""
                    ua = self._attr(tb, 'ui_token_amount', 'uiTokenAmount')
                    if ua is None:
                        return 0.0
                    v = self._attr(ua, 'ui_amount', 'uiAmount')
                    if v is not None:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
                    return 0.0

                for tb in post_token:
                    owner = str(self._attr(tb, 'owner', default='') or '')
                    idx   = self._attr(tb, 'account_index', 'accountIndex', default=-1)
                    mint  = str(self._attr(tb, 'mint', default='') or '')
                    if not mint:
                        continue

                    post_amt = _ui_amount(tb)
                    pre_amt  = _ui_amount(pre_by_idx[idx]) if idx in pre_by_idx else 0.0
                    delta    = abs(post_amt - pre_amt)

                    # Prefer entries owned by the monitored wallet; otherwise
                    # accept any entry and choose the one with the largest change.
                    is_ours = (owner == wallet_address)
                    if is_ours and delta > best_delta:
                        token_mint   = mint
                        token_amount = post_amt - pre_amt
                        best_delta   = delta
                    elif not token_mint and delta > best_delta:
                        # No owner-matched entry yet — keep as fallback
                        token_mint   = mint
                        token_amount = post_amt - pre_amt
                        best_delta   = delta

                # Also check pre_token entries that have no post equivalent
                # (the balance went to 0 — relevant for sells)
                if not token_mint:
                    for tb in pre_token:
                        owner = str(self._attr(tb, 'owner', default='') or '')
                        idx   = self._attr(tb, 'account_index', 'accountIndex', default=-1)
                        mint  = str(self._attr(tb, 'mint', default='') or '')
                        if not mint:
                            continue
                        # Only consider entries whose post balance is 0 or absent
                        post_exists = any(
                            self._attr(p, 'account_index', 'accountIndex') == idx
                            for p in post_token
                        )
                        if not post_exists:
                            pre_amt = _ui_amount(tb)
                            if pre_amt > best_delta:
                                token_mint   = mint
                                token_amount = -pre_amt   # negative = sold
                                best_delta   = pre_amt

            except Exception as te:
                self.logger.debug(f"Token mint extraction failed for {signature[:20]}…: {te}")

            # Require a token mint — without one this is just SOL movement
            # (e.g. a transfer or a fee refund) and not a meaningful swap to copy.
            if not token_mint:
                self.logger.debug(
                    f"Skipping {tx_type} for {wallet_address[:8]}…: "
                    f"no token mint found in {signature[:20]}…"
                )
                return None

            # Resolve token symbol from on-chain metadata (Token-2022 extension
            # first, then Metaplex PDA).  Falls back to the first 8 chars of the
            # mint address if metadata isn't available yet.
            token_symbol = 'UNKNOWN'
            try:
                meta_info = self.rpc.get_token_metadata(token_mint)
                if meta_info:
                    token_symbol = meta_info.get('symbol') or meta_info.get('name') or 'UNKNOWN'
            except Exception:
                token_symbol = token_mint[:8] + '…'

            trade_info = {
                'signature':     signature,
                'wallet_address': wallet_address,
                'wallet_name':   wallet_info.get('name', wallet_info.get('label', '')),
                'timestamp':     datetime.utcfromtimestamp(block_time).isoformat()
                                 if block_time else None,
                'slot':          slot,
                'type':          tx_type,
                'sol_change':    abs(sol_change),
                'token_mint':    token_mint,
                'token_symbol':  token_symbol,
                'token_amount':  abs(token_amount),
                'price':         0.0,
                'dex':           'unknown',
                'detected_at':   datetime.utcnow().isoformat(),
            }

            return trade_info

        except Exception as e:
            self.logger.error(f"Error parsing transaction {signature}: {e}")
            return None

    def _emit_transaction_detected(self, trade_info: Dict):
        """
        Emit transaction detected event to all registered callbacks.

        Args:
            trade_info: Trade information dictionary
        """
        # Add to cache
        self.detected_transactions.insert(0, trade_info)

        # Trim cache if too large
        if len(self.detected_transactions) > self.max_cache_size:
            self.detected_transactions = self.detected_transactions[:self.max_cache_size]

        # Log detection
        self.logger.info(
            f"Transaction detected - {trade_info['wallet_name']}: "
            f"{trade_info['type']} {trade_info['token_symbol']} "
            f"({trade_info['signature'][:8]}...)"
        )

        # Call registered callbacks
        for callback in self.on_transaction_callbacks:
            try:
                callback(trade_info)
            except Exception as e:
                self.logger.error(f"Error in transaction callback: {e}")

    def on_transaction(self, callback: Callable):
        """
        Register a callback for transaction detection events.

        Args:
            callback: Function to call when transaction is detected
        """
        self.on_transaction_callbacks.append(callback)
        self.logger.debug("Registered transaction callback")

    def get_monitored_wallets(self) -> List[Dict]:
        """
        Get list of monitored wallets.

        Returns:
            List of wallet information
        """
        return list(self.monitored_wallets.values())

    def get_wallet(self, address: str) -> Optional[Dict]:
        """
        Get information about a specific monitored wallet.

        Args:
            address: Wallet address

        Returns:
            Wallet info or None
        """
        return self.monitored_wallets.get(address)

    def get_detected_transactions(self, limit: int = 50, wallet_address: Optional[str] = None) -> List[Dict]:
        """
        Get recently detected transactions.

        Args:
            limit: Maximum number of transactions to return
            wallet_address: Optional filter by wallet address

        Returns:
            List of detected transaction info
        """
        transactions = self.detected_transactions

        # Filter by wallet if specified
        if wallet_address:
            transactions = [tx for tx in transactions if tx.get('wallet_address') == wallet_address]

        # Limit results
        return transactions[:limit]

    def get_status(self) -> Dict:
        """
        Get wallet monitor status.

        Returns:
            Status dictionary
        """
        return {
            'running': self.is_running,
            'monitored_wallets': len(self.monitored_wallets),
            'subscriptions': len(self.subscriptions),
            'transactions_detected': len(self.detected_transactions)
        }
