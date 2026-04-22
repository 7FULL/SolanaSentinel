"""
Token Detector
Monitors blockchain for new token launches on Pump.fun and Raydium.
Uses WebSocket subscriptions to detect pool creation and token mints in real-time.
"""

import logging
import re
import threading
import time
from typing import Dict, List, Callable, Optional, Set
from datetime import datetime
from solders.pubkey import Pubkey
from services.dexscreener_service import DexScreenerService


# Program IDs for different DEXes and token programs
RAYDIUM_AMM_PROGRAM_ID   = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
RAYDIUM_LIQUIDITY_POOL_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
# Raydium CPMM (Constant Product Market Maker) — used for most new pool creations in 2024+
# All direct launches on Raydium (not pump.fun migrations) go through this program.
RAYDIUM_CPMM_PROGRAM_ID  = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"  # Official Pump.fun program
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class TokenDetector:
    """
    Detects new token launches on Pump.fun and Raydium.
    Uses WebSocket subscriptions to monitor program logs.
    """

    def __init__(self, websocket_manager, rpc_client, db=None):
        """
        Initialize token detector.

        Args:
            websocket_manager: WebSocketManager instance
            rpc_client: SolanaRPCClient instance
            db: Optional DatabaseManager for persistent token storage
        """
        self.ws_manager = websocket_manager
        self.rpc = rpc_client
        self.db = db
        self.dexscreener = DexScreenerService()
        self.logger = logging.getLogger(__name__)

        # Subscription IDs
        self.subscriptions: Dict[str, int] = {}

        # Detected tokens cache
        self.detected_tokens: List[Dict] = []
        self.max_cache_size = 1000

        # Event callbacks
        self.on_token_detected_callbacks: List[Callable] = []

        # Running state
        self.is_running = False

        # Seen signatures — shared between the WebSocket handler and the polling
        # fallback so we never process the same transaction twice.
        self._seen_signatures: Set[str] = set()
        self._seen_signatures_max = 5000   # trim when it grows too large

        # Polling fallback for pump.fun (catches tokens missed by WebSocket)
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_interval: int = 10      # seconds between polls
        self._poll_batch:    int = 20      # signatures to fetch per poll
        # Timestamp of the last WS event from pump.fun subscription.
        # Polling is skipped while the WS is alive to avoid rate-limit exhaustion.
        self._last_ws_pumpfun_event: float = 0.0
        # How long the WS must be silent before polling re-activates (seconds)
        self._ws_silence_threshold: int = 45

        # Diagnostics — track what instruction strings arrive from pump.fun
        # so we can detect if the program changed its instruction names.
        self._pumpfun_events_received: int = 0   # total WS events from pumpfun sub
        self._instruction_stats: Dict[str, int] = {}  # "InstructionName" → count

        # Diagnostics — same for Raydium (AMM V4 + CPMM)
        self._raydium_events_received: int = 0
        self._raydium_cpmm_events_received: int = 0
        self._raydium_instruction_stats: Dict[str, int] = {}
        self._raydium_cpmm_instruction_stats: Dict[str, int] = {}

        # Background market-data refresh thread — periodically re-fetches
        # DexScreener data for recently detected tokens that still have
        # liquidity=0 (happens when the token was just created and DexScreener
        # hadn't indexed it yet at detection time).
        self._refresh_thread: Optional[threading.Thread] = None
        self._refresh_interval: int = 30    # seconds between refresh passes
        self._refresh_window:   int = 1800  # only refresh tokens from the last 30 min

        # AI outcome tracking thread — records price at 1h/6h/24h post-detection
        # for every token so the dataset can be labelled for ML training.
        # Uses its own _outcome_running flag so it can start before the WS detector.
        self._outcome_thread: Optional[threading.Thread] = None
        self._outcome_running: bool = False
        self._outcome_check_interval: int = 120  # seconds between passes

    def start(self, platforms: list = None):
        """
        Start monitoring for new tokens.

        Args:
            platforms: List of platform names to subscribe to.
                       Supported: 'pump.fun', 'raydium'.
                       Defaults to both if not provided.
        """
        if self.is_running:
            self.logger.warning("Token detector already running")
            return

        if platforms is None:
            platforms = ['pump.fun', 'raydium']

        enabled = {p.lower() for p in platforms}

        self.logger.info(f"Starting token detector (platforms: {sorted(enabled)})")
        print("\n" + "="*70)
        print(f"[TOKEN-DETECTOR] 🚀 Starting Token Detection System")
        print(f"[TOKEN-DETECTOR]    Platforms: {', '.join(sorted(enabled))}")
        print("="*70)
        self.is_running = True

        subscription_results = []

        try:
            step = 1

            if 'raydium' in enabled:
                # Raydium AMM V4 — mainly catches pump.fun pool migrations
                print(f"\n[TOKEN-DETECTOR] Step {step}: Subscribing to Raydium AMM V4...")
                try:
                    self._subscribe_raydium()
                    if 'raydium' in self.subscriptions:
                        print(f"[TOKEN-DETECTOR] Step {step}: ✅ Raydium AMM V4 subscription successful")
                        subscription_results.append(("Raydium AMM V4", True))
                    else:
                        print(f"[TOKEN-DETECTOR] Step {step}: ⚠️ Raydium AMM V4 subscription returned but no ID stored")
                        subscription_results.append(("Raydium AMM V4", False))
                except Exception as e:
                    print(f"[TOKEN-DETECTOR] Step {step}: ❌ Raydium AMM V4 subscription failed: {e}")
                    subscription_results.append(("Raydium AMM V4", False))
                step += 1

                # Raydium CPMM — direct launches on Raydium (not via pump.fun)
                print(f"\n[TOKEN-DETECTOR] Step {step}: Subscribing to Raydium CPMM...")
                try:
                    self._subscribe_raydium_cpmm()
                    if 'raydium_cpmm' in self.subscriptions:
                        print(f"[TOKEN-DETECTOR] Step {step}: ✅ Raydium CPMM subscription successful")
                        subscription_results.append(("Raydium CPMM", True))
                    else:
                        print(f"[TOKEN-DETECTOR] Step {step}: ⚠️ Raydium CPMM subscription returned but no ID stored")
                        subscription_results.append(("Raydium CPMM", False))
                except Exception as e:
                    print(f"[TOKEN-DETECTOR] Step {step}: ❌ Raydium CPMM subscription failed: {e}")
                    subscription_results.append(("Raydium CPMM", False))
                step += 1

            if 'pump.fun' in enabled:
                print(f"\n[TOKEN-DETECTOR] Step {step}: Subscribing to Pump.fun...")
                try:
                    self._subscribe_pumpfun()
                    if 'pumpfun' in self.subscriptions:
                        print(f"[TOKEN-DETECTOR] Step {step}: ✅ Pump.fun subscription successful")
                        subscription_results.append(("Pump.fun", True))
                    else:
                        print(f"[TOKEN-DETECTOR] Step {step}: ⚠️ Pump.fun subscription returned but no ID stored")
                        subscription_results.append(("Pump.fun", False))
                except Exception as e:
                    print(f"[TOKEN-DETECTOR] Step {step}: ❌ Pump.fun subscription failed: {e}")
                    subscription_results.append(("Pump.fun", False))
                step += 1

            # Token Program subscription (always optional — high volume, may be rate-limited)
            print(f"\n[TOKEN-DETECTOR] Step {step}: Subscribing to Token Program...")
            try:
                self._subscribe_token_mints()
                if 'token_mints' in self.subscriptions:
                    print(f"[TOKEN-DETECTOR] Step {step}: ✅ Token Program subscription successful")
                    subscription_results.append(("Token Program", True))
                else:
                    print(f"[TOKEN-DETECTOR] Step {step}: ⚠️ Token Program subscription returned but no ID stored")
                    subscription_results.append(("Token Program", False))
            except Exception as e:
                print(f"[TOKEN-DETECTOR] Step {step}: ⚠️ Token Program subscription failed: {e}")
                print("[TOKEN-DETECTOR] Note: Token Program generates massive logs and may be rate-limited by public RPC")
                subscription_results.append(("Token Program", False))

            # Print summary
            print("\n" + "="*70)
            print("[TOKEN-DETECTOR] 📊 SUBSCRIPTION SUMMARY")
            print("="*70)
            for name, success in subscription_results:
                status = "✅ Active" if success else "❌ Failed"
                print(f"  {name:20s} : {status}")

            successful = sum(1 for _, success in subscription_results if success)
            print(f"\n  Total: {successful}/{len(subscription_results)} subscriptions active")

            if successful == 0:
                print("\n  ⚠️ WARNING: No subscriptions active! Token detection will not work.")
                print("  This is likely due to public RPC rate limits or WebSocket restrictions.")
                print("  Consider using a premium RPC endpoint (Helius, QuickNode, Alchemy).")
            elif successful < len(subscription_results):
                print("\n  ⚠️ WARNING: Some subscriptions failed. Detection may be limited.")
                print("  Public RPC endpoints often block high-volume subscriptions.")

            print("="*70 + "\n")

            self.logger.info("Token detector started successfully")

            # Start the polling fallback for pump.fun regardless of whether
            # the WebSocket subscription succeeded — it acts as a safety net.
            if 'pump.fun' in enabled:
                self._poll_thread = threading.Thread(
                    target=self._poll_loop, daemon=True, name="pumpfun-poll"
                )
                self._poll_thread.start()
                print(f"[TOKEN-DETECTOR] 🔄 Polling fallback active "
                      f"(pump.fun every {self._poll_interval}s)")

            # Background market-data refresh for recently detected tokens.
            # Runs regardless of platform — fixes the "liquidity stays 0" problem
            # by re-querying DexScreener after the indexer has had time to catch up.
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop, daemon=True, name="token-refresh"
            )
            self._refresh_thread.start()
            print(f"[TOKEN-DETECTOR] 🔄 Market-data refresh active "
                  f"(every {self._refresh_interval}s, window {self._refresh_window}s)")

            # Background AI outcome tracker (started via start_outcome_tracker,
            # which is also called at app startup independently of the sniper)
            self.start_outcome_tracker()

            # Raydium health check: warn after 10 minutes if no WS events arrived.
            # Public RPC nodes routinely drop logsSubscribe events for high-volume
            # programs (Raydium processes thousands of txs/minute). The pump.fun
            # polling fallback already catches migrations, so this is purely advisory.
            if 'raydium' in enabled:
                import threading as _t
                def _raydium_health_check():
                    import time as _time
                    _time.sleep(600)   # wait 10 minutes
                    total = self._raydium_events_received + self._raydium_cpmm_events_received
                    if total == 0:
                        print("\n" + "="*70)
                        print("[TOKEN-DETECTOR] ⚠️  RAYDIUM WEBSOCKET HEALTH WARNING")
                        print("[TOKEN-DETECTOR]    No Raydium WS events in 10 minutes.")
                        print("[TOKEN-DETECTOR]    Cause: public RPC nodes rate-limit high-volume programs.")
                        print("[TOKEN-DETECTOR]    Impact: direct Raydium launches (CPMM) may be missed.")
                        print("[TOKEN-DETECTOR]    Pump.fun→Raydium migrations ARE caught via pump.fun poll.")
                        print("[TOKEN-DETECTOR]    Fix: use a paid RPC (Helius / QuickNode / Alchemy).")
                        print("="*70 + "\n")
                _t.Thread(target=_raydium_health_check, daemon=True,
                          name="raydium-health").start()

        except Exception as e:
            print(f"\n[TOKEN-DETECTOR] ❌ Critical error starting token detector: {e}")
            import traceback
            traceback.print_exc()
            self.logger.error(f"Failed to start token detector: {e}")
            self.is_running = False
            raise

    def start_outcome_tracker(self) -> None:
        """
        Start the AI outcome tracker thread independently of the WS detector.

        Safe to call multiple times — does nothing if the thread is already alive.
        Called at app startup (so outcomes are tracked even when the sniper is off)
        and also from start() when the sniper is activated.
        """
        if not self.db:
            return
        if self._outcome_thread and self._outcome_thread.is_alive():
            return  # already running
        self._outcome_running = True
        self._outcome_thread = threading.Thread(
            target=self._outcome_loop, daemon=True, name="outcome-tracker"
        )
        self._outcome_thread.start()
        print("[TOKEN-DETECTOR] 🧠 AI outcome tracker active (1h/6h/24h checkpoints)")

    def stop(self):
        """
        Stop monitoring for new tokens.
        Unsubscribes from all programs.
        """
        if not self.is_running:
            return

        self.logger.info("Stopping token detector")
        self.is_running = False
        self._outcome_running = False

        # Unsubscribe from all
        for name, sub_id in self.subscriptions.items():
            try:
                self.ws_manager.unsubscribe(sub_id)
                self.logger.debug(f"Unsubscribed from {name}")
            except Exception as e:
                self.logger.error(f"Failed to unsubscribe from {name}: {e}")

        self.subscriptions.clear()

        # The poll thread checks is_running and exits on its own.
        # We don't join it because it may be sleeping.
        self._poll_thread = None

        self.logger.info("Token detector stopped")

    def _subscribe_raydium(self):
        """
        Subscribe to Raydium program for new pool creation.
        """
        print(f"  → Program ID: {RAYDIUM_AMM_PROGRAM_ID}")
        self.logger.info(f"Subscribing to Raydium AMM: {RAYDIUM_AMM_PROGRAM_ID}")

        # Subscribe to logs mentioning Raydium
        sub_id = self.ws_manager.subscribe_logs(
            mentions=[RAYDIUM_AMM_PROGRAM_ID],
            callback=self._handle_raydium_log
        )

        if sub_id:
            self.subscriptions['raydium'] = sub_id
            print(f"  → Subscription ID: {sub_id}")
            self.logger.info(f"Subscribed to Raydium (sub_id: {sub_id})")
        else:
            print(f"  → ERROR: No subscription ID returned")
            self.logger.error("Failed to subscribe to Raydium - no sub_id returned")
            raise Exception("No subscription ID returned from WebSocket manager")

    def _subscribe_raydium_cpmm(self):
        """
        Subscribe to Raydium CPMM program for new pool creation.
        CPMM is the standard pool type for all new direct Raydium launches in 2024+.
        """
        print(f"  → Program ID: {RAYDIUM_CPMM_PROGRAM_ID}")
        self.logger.info(f"Subscribing to Raydium CPMM: {RAYDIUM_CPMM_PROGRAM_ID}")

        sub_id = self.ws_manager.subscribe_logs(
            mentions=[RAYDIUM_CPMM_PROGRAM_ID],
            callback=self._handle_raydium_cpmm_log
        )

        if sub_id:
            self.subscriptions['raydium_cpmm'] = sub_id
            print(f"  → Subscription ID: {sub_id}")
            self.logger.info(f"Subscribed to Raydium CPMM (sub_id: {sub_id})")
        else:
            print(f"  → ERROR: No subscription ID returned")
            self.logger.error("Failed to subscribe to Raydium CPMM - no sub_id returned")
            raise Exception("No subscription ID returned from WebSocket manager")

    def _subscribe_pumpfun(self):
        """
        Subscribe to Pump.fun program for new token launches.
        """
        print(f"  → Program ID: {PUMP_FUN_PROGRAM_ID}")
        self.logger.info(f"Subscribing to Pump.fun: {PUMP_FUN_PROGRAM_ID}")

        # Subscribe to logs mentioning Pump.fun
        sub_id = self.ws_manager.subscribe_logs(
            mentions=[PUMP_FUN_PROGRAM_ID],
            callback=self._handle_pumpfun_log
        )

        if sub_id:
            self.subscriptions['pumpfun'] = sub_id
            print(f"  → Subscription ID: {sub_id}")
            self.logger.info(f"Subscribed to Pump.fun (sub_id: {sub_id})")
        else:
            print(f"  → ERROR: No subscription ID returned")
            self.logger.error("Failed to subscribe to Pump.fun - no sub_id returned")
            raise Exception("No subscription ID returned from WebSocket manager")

    def _subscribe_token_mints(self):
        """
        Subscribe to token program for new token mints.
        NOTE: Token Program generates massive amounts of logs and is often
        rate-limited or blocked by public RPC endpoints.
        """
        print(f"  → Program ID: {TOKEN_PROGRAM_ID}")
        print(f"  → WARNING: High-volume logs, may be rate-limited")
        self.logger.info("Subscribing to token mints")

        # Subscribe to Token Program logs
        sub_id = self.ws_manager.subscribe_logs(
            mentions=[TOKEN_PROGRAM_ID],
            callback=self._handle_token_mint_log
        )

        if sub_id:
            self.subscriptions['token_mints'] = sub_id
            print(f"  → Subscription ID: {sub_id}")
            self.logger.info(f"Subscribed to token mints (sub_id: {sub_id})")
        else:
            print(f"  → ERROR: No subscription ID returned (likely rate-limited)")
            self.logger.error("Failed to subscribe to token mints - no sub_id returned")
            raise Exception("No subscription ID returned from WebSocket manager")

    # Raydium emits these ONLY when a new AMM pool is created.
    # "Instruction: Initialize" is intentionally excluded — it is a substring
    # of "Instruction: InitializeAccount" which the Token Program emits during
    # every swap (when creating a temporary output account).
    _RAYDIUM_POOL_CREATION_LOGS = (
        "Instruction: InitializeV4",  # Raydium AMM V4 pool creation
        "Instruction: Initialize2",   # Raydium AMM V2 pool creation
        "initialize2",                # lowercase variant in some RPC versions
    )

    def _handle_raydium_log(self, event_data: Dict):
        """
        Handle Raydium program log events — only act on new pool creation.

        Raydium new-pool instructions emit logs that contain one of the
        _RAYDIUM_POOL_CREATION_LOGS strings.  Regular swaps emit
        "Instruction: SwapBaseIn" / "SwapBaseOut" and are ignored here.

        Args:
            event_data: WebSocket event data
        """
        try:
            data = event_data.get('data', {})
            value = data.get('value', {})

            # Skip failed transactions — a failed pool init means no pool was created
            if value.get('err') is not None:
                return

            logs = value.get('logs', [])
            signature = value.get('signature')

            # ── Diagnostics ─────────────────────────────────────────────
            self._raydium_events_received += 1
            for log in logs:
                m = re.search(r'Instruction: (\w+)', log)
                if m:
                    key = m.group(1)
                    self._raydium_instruction_stats[key] = (
                        self._raydium_instruction_stats.get(key, 0) + 1
                    )
            if self._raydium_events_received == 1:
                print(f"[TOKEN-DETECTOR] 🔍 First Raydium AMM event ({len(logs)} log lines):")
                for i, line in enumerate(logs[:10]):
                    print(f"  [{i:02d}] {line}")
            if self._raydium_events_received % 200 == 0:
                top = sorted(self._raydium_instruction_stats.items(), key=lambda x: -x[1])[:8]
                print(f"[TOKEN-DETECTOR] 📊 Raydium AMM events={self._raydium_events_received} "
                      f"instructions: {dict(top)}")
            # ── End diagnostics ──────────────────────────────────────────

            # Must be a pool-creation instruction, not a swap or ATA init
            # endswith avoids matching "InitializeAccount" (Token Program) or
            # "InitializeV4SomeOtherVariant" that might appear in future versions
            is_pool_creation = any(
                log.endswith(creation_str)
                for log in logs
                for creation_str in self._RAYDIUM_POOL_CREATION_LOGS
            )
            if not is_pool_creation:
                return

            # Reject if any log reveals this is a swap, not a pool creation
            _SWAP_MARKERS = (
                "SwapBaseIn", "SwapBaseOut",
                "Instruction: Swap", "raydium:swap",
                "Instruction: SwapBase",
            )
            if any(marker in log for log in logs for marker in _SWAP_MARKERS):
                return

            self.logger.info(f"Raydium new pool detected: {signature}")
            print(f"[TOKEN-DETECTOR] 🆕 Raydium new pool: {signature[:20] if signature else '?'}…")

            token_info = self._parse_raydium_pool(value, signature)
            if token_info:
                self._emit_token_detected(token_info)

        except Exception as e:
            self.logger.error(f"Error handling Raydium log: {e}")

    # Raydium CPMM pool creation uses the "initialize" instruction.
    # This is safe to check here because:
    #   - We're subscribed to the CPMM program specifically, so every event
    #     involves CPMM being invoked.
    #   - CPMM swaps use "swapBaseInput" / "swapBaseOutput", not "initialize".
    #   - Token Program's "InitializeMint" ends with "InitializeMint", not "initialize".
    #   - Token Program's "InitializeAccount" ends with "InitializeAccount".
    # So endswith("initialize") uniquely identifies a CPMM pool creation.
    _RAYDIUM_CPMM_CREATION_LOGS = (
        "Instruction: initialize",   # Raydium CPMM pool creation
        "Instruction: createPool",   # alternative instruction name in some CPMM builds
    )

    # Pump.fun emits one of these when a token's bonding curve completes
    # (~$69 k raised) and the token is migrated to a Raydium AMM V4 pool.
    # We detect migrations from the pump.fun logsSubscribe because the
    # Raydium subscription is often rate-limited on public RPC nodes.
    _PUMPFUN_MIGRATE_INSTRUCTIONS = (
        "Instruction: Migrate",   # standard pump.fun migration (capital M, Anchor)
        "Instruction: migrate",   # lowercase variant in some builds
    )

    def _handle_raydium_cpmm_log(self, event_data: Dict):
        """
        Handle Raydium CPMM program log events — only act on new pool creation.

        CPMM pool creation emits "Instruction: initialize".  Swaps emit
        "Instruction: swapBaseInput" / "swapBaseOutput" and are ignored.

        Args:
            event_data: WebSocket event data
        """
        try:
            data = event_data.get('data', {})
            value = data.get('value', {})

            if value.get('err') is not None:
                return

            logs = value.get('logs', [])
            signature = value.get('signature')

            # ── Diagnostics ─────────────────────────────────────────────
            self._raydium_cpmm_events_received += 1
            for log in logs:
                m = re.search(r'Instruction: (\w+)', log)
                if m:
                    key = m.group(1)
                    self._raydium_cpmm_instruction_stats[key] = (
                        self._raydium_cpmm_instruction_stats.get(key, 0) + 1
                    )
            if self._raydium_cpmm_events_received == 1:
                print(f"[TOKEN-DETECTOR] 🔍 First Raydium CPMM event ({len(logs)} log lines):")
                for i, line in enumerate(logs[:10]):
                    print(f"  [{i:02d}] {line}")
            if self._raydium_cpmm_events_received % 200 == 0:
                top = sorted(self._raydium_cpmm_instruction_stats.items(), key=lambda x: -x[1])[:8]
                print(f"[TOKEN-DETECTOR] 📊 Raydium CPMM events={self._raydium_cpmm_events_received} "
                      f"instructions: {dict(top)}")
            # ── End diagnostics ──────────────────────────────────────────

            # Confirm this is a pool creation, not a swap
            is_pool_creation = any(
                log.endswith(creation_str)
                for log in logs
                for creation_str in self._RAYDIUM_CPMM_CREATION_LOGS
            )
            if not is_pool_creation:
                return

            # Reject if any log reveals this is a swap
            _SWAP_MARKERS = ("swapBaseInput", "swapBaseOutput", "Instruction: Swap",
                             "SwapBaseIn", "SwapBaseOut")
            if any(marker in log for log in logs for marker in _SWAP_MARKERS):
                return

            if signature and signature in self._seen_signatures:
                return

            self.logger.info(f"Raydium CPMM new pool detected: {signature}")
            print(f"[TOKEN-DETECTOR] 🆕 Raydium CPMM new pool: {signature[:20] if signature else '?'}…")

            token_info = self._parse_raydium_cpmm_pool(value, signature)
            if token_info:
                if signature:
                    self._mark_seen(signature)
                self._emit_token_detected(token_info)

        except Exception as e:
            self.logger.error(f"Error handling Raydium CPMM log: {e}")

    def _parse_raydium_cpmm_pool(self, tx_data: Dict, signature: str) -> Optional[Dict]:
        """
        Parse a Raydium CPMM pool-creation transaction.

        Raydium CPMM `initialize` account order (0-indexed):
          0  creator (fee-payer/signer)
          1  ammConfig
          2  authority (PDA)
          3  poolState (PDA)
          4  token0Mint   ← one of the two tokens (sorted by pubkey)
          5  token1Mint   ← the other token
          6  lpMint
          ...

        The non-quote token (not SOL/USDC/USDT) at index 4 or 5 is the new token.
        """
        try:
            sig_short = (signature[:20] + '...') if signature else '—'
            base = {
                'source': 'raydium',
                'signature': signature,
                'sig_short': sig_short,
                'solscan_url': f'https://solscan.io/tx/{signature}' if signature else None,
                'detected_at': datetime.utcnow().isoformat() + 'Z',
                'token_mint': None,
                'token_name': sig_short,
                'token_symbol': 'RAY-NEW',
                'initial_liquidity': 0.0,
                'market_cap': 0.0,
                'platform': 'raydium',
            }

            if not signature:
                return base

            accounts = self.rpc.get_transaction_accounts(signature)
            if not accounts or len(accounts) < 6:
                return base

            QUOTE_MINTS = {
                "So11111111111111111111111111111111111111112",   # Wrapped SOL
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
            }

            mint_address = None
            # CPMM: token mints at indices 4 and 5 (possibly also 6, 7 depending on version)
            for idx in (4, 5, 6, 7, 3):
                if idx < len(accounts):
                    pubkey = accounts[idx]['pubkey']
                    if pubkey and pubkey not in QUOTE_MINTS:
                        # Sanity check: pubkey looks like a valid base58 mint (32+ chars)
                        if len(pubkey) >= 32:
                            mint_address = pubkey
                            break

            if not mint_address:
                return base

            base['token_mint'] = mint_address
            base['solscan_url'] = f'https://solscan.io/tx/{signature}'
            base['dexscreener_url'] = f'https://dexscreener.com/solana/{mint_address}'

            # On-chain metadata (Token-2022 extension first, then Metaplex PDA)
            meta = self.rpc.get_token_metadata(mint_address)
            if meta:
                base['token_name']   = meta['name']
                base['token_symbol'] = meta['symbol']
                print(
                    f"[TOKEN-DETECTOR] 📋 CPMM on-chain metadata: "
                    f"{meta['symbol']} / {meta['name']}"
                )

            # DexScreener for market data
            dex_data = self.dexscreener.get_token_data(mint_address, retries=3, retry_delay=5.0)
            if dex_data:
                base.update({
                    'token_name':        dex_data['name']   if not meta else base['token_name'],
                    'token_symbol':      dex_data['symbol'] if not meta else base['token_symbol'],
                    'initial_liquidity': dex_data['liquidity_usd'],
                    'market_cap':        dex_data['market_cap'],
                    'volume_1h':         dex_data['volume_1h'],
                    'price_usd':         dex_data['price_usd'],
                    'dexscreener_url':   dex_data['dexscreener_url'],
                })

            return base

        except Exception as e:
            self.logger.error(
                f"Error parsing Raydium CPMM pool "
                f"({signature[:20] if signature else '?'}): {e}"
            )
            return None

    # Instruction strings that pump.fun emits when a NEW token is launched.
    # The set covers the original program ("Create") and any known variants so
    # that a program upgrade doesn't silently break detection.
    # NOTE: we use endswith() so "CreateAccount" and "CreateIdempotent" are
    # never matched — they don't end with any of these exact strings.
    _PUMPFUN_CREATE_INSTRUCTIONS = (
        "Instruction: Create",      # original pump.fun program
        "Instruction: CreateV2",    # pump.fun updated instruction (observed in prod)
        "Instruction: create",      # lowercase variant (some Anchor builds)
        "Instruction: Initialize",  # alternative init name in some versions
        "Instruction: Launch",      # potential future instruction name
        "Instruction: MintToken",   # potential future instruction name
        "Instruction: CreateToken", # potential future instruction name
    )

    def _handle_pumpfun_log(self, event_data: Dict):
        """
        Handle Pump.fun program log events — only act on new token launches.

        Pump.fun emits exactly "Program log: Instruction: Create" when a new
        token is launched.  Buy/Sell emit "Instruction: Buy" / "Instruction: Sell".
        The System Program emits "Instruction: CreateAccount" during buys (to
        open the buyer's token account) — that string ends with "CreateAccount",
        NOT "Create", so the endswith check below excludes it correctly.

        We also track all instruction names seen for diagnosis purposes (exposed
        via GET /api/debug/pipeline → instruction_stats).

        Args:
            event_data: WebSocket event data
        """
        try:
            data = event_data.get('data', {})
            value = data.get('value', {})

            # Skip failed transactions — a failed create means the token was never minted
            if value.get('err') is not None:
                return

            logs = value.get('logs', [])
            signature = value.get('signature')

            # Mark WS as alive — polling is suppressed while events keep coming
            self._last_ws_pumpfun_event = time.time()

            # ── Diagnostics ─────────────────────────────────────────────
            # Count every event and track all "Instruction: X" strings so we
            # can detect whether pump.fun changed their instruction names.
            self._pumpfun_events_received += 1
            for log in logs:
                m = re.search(r'Instruction: (\w+)', log)
                if m:
                    key = m.group(1)
                    self._instruction_stats[key] = self._instruction_stats.get(key, 0) + 1

            # First event: dump the full log lines so we can see the format
            if self._pumpfun_events_received == 1:
                print(
                    f"[TOKEN-DETECTOR] 🔍 First pump.fun event logs ({len(logs)} lines):"
                )
                for i, line in enumerate(logs[:15]):
                    print(f"  [{i:02d}] {line}")

            # Print a periodic summary every 500 events (diagnostic, low noise)
            if self._pumpfun_events_received % 500 == 0:
                top = sorted(self._instruction_stats.items(), key=lambda x: -x[1])[:10]
                print(
                    f"[TOKEN-DETECTOR] 📊 pump.fun events={self._pumpfun_events_received} "
                    f"instruction counts: {dict(top)}"
                )
            # ── End diagnostics ──────────────────────────────────────────

            # Check for any known create-instruction variant (endswith so we
            # never match "CreateAccount", "CreateIdempotent", etc.)
            is_create = any(
                log.endswith(ci)
                for log in logs
                for ci in self._PUMPFUN_CREATE_INSTRUCTIONS
            )
            if not is_create:
                # Check for pump.fun → Raydium migration instead
                is_migrate = any(
                    log.endswith(mi)
                    for log in logs
                    for mi in self._PUMPFUN_MIGRATE_INSTRUCTIONS
                )
                if is_migrate and signature and signature not in self._seen_signatures:
                    self.logger.info(f"Pump.fun → Raydium migration detected (WS): {signature}")
                    print(f"[TOKEN-DETECTOR] 🔀 Pump.fun→Raydium migration (WS): {signature[:20] if signature else '?'}…")
                    token_info = self._parse_pump_migration(value, signature)
                    if token_info:
                        self._mark_seen(signature)
                        self._emit_token_detected(token_info)
                return

            # Deduplicate: only _seen_signatures contains confirmed Creates.
            # We never mark buy/sell signatures as seen, so this set stays small
            # and a delayed WS delivery never gets falsely blocked.
            if signature and signature in self._seen_signatures:
                return

            self.logger.info(f"Pump.fun new token detected: {signature}")
            print(f"[TOKEN-DETECTOR] 🆕 Pump.fun new token (WS): {signature[:20] if signature else '?'}…")

            token_info = self._parse_pumpfun_token(value, signature)
            if token_info:
                # Mark as seen only after a successful parse so we don't
                # permanently block a signature that failed mid-parse.
                if signature:
                    self._mark_seen(signature)
                self._emit_token_detected(token_info)

        except Exception as e:
            self.logger.error(f"Error handling Pump.fun log: {e}")

    def _handle_token_mint_log(self, event_data: Dict):
        """
        Handle token program log events.
        Detects new token mints.

        Args:
            event_data: WebSocket event data
        """
        try:
            data = event_data.get('data', {})
            value = data.get('value', {})
            logs = value.get('logs', [])
            signature = value.get('signature')

            # Look for mint initialization
            for log in logs:
                if 'InitializeMint' in log or 'MintTo' in log:
                    self.logger.debug(f"Detected token mint activity: {signature}")
                    # Could parse and emit token mint events here
                    break

        except Exception as e:
            self.logger.error(f"Error handling token mint log: {e}")

    # ------------------------------------------------------------------
    # Polling fallback for pump.fun
    # ------------------------------------------------------------------

    def _poll_loop(self):
        """
        Background thread: polls the pump.fun program for recent signatures
        every _poll_interval seconds and processes any Create transactions
        that the WebSocket subscription missed.

        This is the main defence against event drops on busy RPC nodes.
        The WebSocket is still the fast path — polling catches stragglers.
        """
        self.logger.info("Pump.fun polling fallback started")
        # Wait one full interval before the first poll so we don't race with
        # the WebSocket's initial burst of events on startup.
        time.sleep(self._poll_interval)

        while self.is_running:
            # Skip polling while the WebSocket is delivering events.
            # The WS is the fast path; polling only burns rate-limit budget
            # when both are active on a public RPC node.
            ws_silence = time.time() - self._last_ws_pumpfun_event
            if ws_silence < self._ws_silence_threshold:
                time.sleep(self._poll_interval)
                continue
            try:
                self._poll_pumpfun_once()
            except Exception as e:
                self.logger.debug(f"Polling error (non-fatal): {e}")
            time.sleep(self._poll_interval)

        self.logger.info("Pump.fun polling fallback stopped")

    def _mark_seen(self, signature: str):
        """Add a signature to the seen-set and trim if needed."""
        self._seen_signatures.add(signature)
        if len(self._seen_signatures) > self._seen_signatures_max:
            # Keep the most recent half (sets are unordered so we just trim size)
            self._seen_signatures = set(
                list(self._seen_signatures)[self._seen_signatures_max // 2:]
            )

    def _poll_pumpfun_once(self):
        """
        Single poll cycle: fetch the _poll_batch most recent signatures for
        the pump.fun program, skip ones already seen, fetch their logs, and
        process any that are `Create` instructions.

        IMPORTANT: signatures are only added to _seen_signatures AFTER a
        successful emit.  We never pre-mark buy/sell transactions, so a
        WebSocket event that arrives with a slight delay is never falsely
        blocked.
        """
        sigs = self.rpc.get_signatures_for_address(
            PUMP_FUN_PROGRAM_ID, limit=self._poll_batch
        )
        if not sigs:
            return

        new_count = 0
        for sig_info in sigs:
            sig = sig_info.get('signature')
            # Skip: already processed as a Create by WS or a previous poll
            if not sig or sig in self._seen_signatures:
                continue

            # Small pause between fetches to avoid bursting the rate limit
            time.sleep(0.3)

            # Fetch full transaction to inspect logs
            tx = self.rpc.get_transaction(sig)
            if not tx:
                continue

            # Extract meta — handles both versioned (solders object) and legacy (dict) paths
            meta = tx.get('meta')
            if meta is None:
                continue

            # Extract log_messages from meta
            try:
                log_messages = getattr(meta, 'log_messages', None)
                if log_messages is None:
                    log_messages = (meta.get('logMessages') or meta.get('log_messages')) \
                        if isinstance(meta, dict) else []
                logs = list(log_messages or [])
            except Exception:
                logs = []

            # Skip failed transactions
            try:
                err = getattr(meta, 'err', None)
                if err is None and isinstance(meta, dict):
                    err = meta.get('err')
                if err is not None:
                    continue
            except Exception:
                pass

            # Not a Create — check for migration, otherwise skip.
            is_create = any(
                log.endswith(ci)
                for log in logs
                for ci in self._PUMPFUN_CREATE_INSTRUCTIONS
            )
            if not is_create:
                is_migrate = any(
                    log.endswith(mi)
                    for log in logs
                    for mi in self._PUMPFUN_MIGRATE_INSTRUCTIONS
                )
                if not is_migrate:
                    continue
                # Migration — emit as a Raydium token
                new_count += 1
                print(f"[TOKEN-DETECTOR] 🔀 Pump.fun→Raydium migration (poll): {sig[:20]}…")
                self.logger.info(f"Pump.fun migration caught by poller: {sig}")
                token_info = self._parse_pump_migration({'logs': logs, 'signature': sig}, sig)
                if token_info:
                    self._mark_seen(sig)
                    self._emit_token_detected(token_info)
                continue

            # This is a Create not yet seen — parse and emit
            new_count += 1
            print(f"[TOKEN-DETECTOR] 🆕 Pump.fun new token (poll): {sig[:20]}…")
            self.logger.info(f"Pump.fun new token caught by poller: {sig}")

            token_info = self._parse_pumpfun_token({'logs': logs, 'signature': sig}, sig)
            if token_info:
                self._mark_seen(sig)   # mark AFTER successful parse/emit
                self._emit_token_detected(token_info)

        if new_count:
            print(f"[TOKEN-DETECTOR] 🔄 Poller caught {new_count} missed token(s)")

    def _parse_pump_migration(self, tx_data: Dict, signature: str) -> Optional[Dict]:
        """
        Parse a pump.fun → Raydium migration transaction.

        When a pump.fun bonding curve completes (~$69 k raised), the pump.fun
        program migrates the token to a Raydium AMM V4 pool via a CPI.
        The migration transaction invokes pump.fun (outer) and Raydium AMM (inner).

        Pump.fun `migrate` account order (0-indexed in the transaction):
          0  global state
          1  fee recipient
          2  mint              ← the migrating token's mint address
          3  bondingCurve
          4  associatedBondingCurve (token account)
          5  Raydium pool state
          ...

        We identify the mint by scanning indices 2-6 for the first account
        that is neither a known system/quote program nor a known DEX program.
        """
        try:
            sig_short = (signature[:20] + '...') if signature else '—'
            base = {
                'source':   'raydium',
                'platform': 'raydium',
                'signature': signature,
                'sig_short': sig_short,
                'solscan_url': f'https://solscan.io/tx/{signature}' if signature else None,
                'detected_at': datetime.utcnow().isoformat() + 'Z',
                'token_mint':   None,
                'token_name':   sig_short,
                'token_symbol': 'MIGRATED',
                'initial_liquidity': 0.0,
                'market_cap':  0.0,
                'migration': True,
            }

            if not signature:
                return base

            accounts = self.rpc.get_transaction_accounts(signature)
            if not accounts or len(accounts) < 3:
                return base

            _SKIP = {
                "11111111111111111111111111111111",
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                PUMP_FUN_PROGRAM_ID,
                RAYDIUM_AMM_PROGRAM_ID,
                RAYDIUM_CPMM_PROGRAM_ID,
                "SysvarRent111111111111111111111111111111111",
                "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bRS",
                "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
                # Quote mints (SOL, USDC, USDT) are not the new token
                "So11111111111111111111111111111111111111112",
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
            }

            mint_address = None
            # Try the expected mint position first (index 2), then broaden
            for idx in (2, 3, 4, 1, 5, 6):
                if idx < len(accounts):
                    pubkey = accounts[idx]['pubkey']
                    if pubkey and len(pubkey) >= 32 and pubkey not in _SKIP:
                        mint_address = pubkey
                        break

            if not mint_address:
                return base

            base['token_mint']     = mint_address
            base['dexscreener_url'] = f'https://dexscreener.com/solana/{mint_address}'

            # On-chain metadata
            meta = self.rpc.get_token_metadata(mint_address)
            if meta:
                base['token_name']   = meta['name']
                base['token_symbol'] = meta['symbol']
                print(
                    f"[TOKEN-DETECTOR] 📋 Migration metadata: "
                    f"{meta['symbol']} / {meta['name']}"
                )

            # DexScreener — pool may already exist on Raydium
            dex_data = self.dexscreener.get_token_data(mint_address, retries=3, retry_delay=5.0)
            if dex_data:
                base.update({
                    'token_name':        dex_data['name']   if not meta else base['token_name'],
                    'token_symbol':      dex_data['symbol'] if not meta else base['token_symbol'],
                    'initial_liquidity': dex_data['liquidity_usd'],
                    'market_cap':        dex_data['market_cap'],
                    'volume_1h':         dex_data['volume_1h'],
                    'price_usd':         dex_data['price_usd'],
                    'dexscreener_url':   dex_data['dexscreener_url'],
                })

            return base

        except Exception as e:
            self.logger.error(
                f"Error parsing pump.fun migration "
                f"({signature[:20] if signature else '?'}): {e}"
            )
            return None

    def _parse_raydium_pool(self, tx_data: Dict, signature: str) -> Optional[Dict]:
        """
        Parse a Raydium pool-creation transaction.

        Fetches the full transaction via JSON-RPC to get account keys, then
        uses the known Raydium AMM InitializeV4 account layout to identify
        the base-token mint (index 8 in the instruction accounts).
        Falls back to DexScreener for market data once the mint is known.

        Raydium AMM Initialize2 account order (0-indexed):
          0  amm               5  lpMint          10 serumMarket
          1  ammAuthority       6  coinMint        11 serumBids
          2  ammOpenOrders      7  pcMint          12 serumAsks
          3  ammTargetOrders    8  coinVault        ...
          4  lpMint             ...

        The base (new) token is typically at coinMint (index 6) and
        the quote is pcMint (index 7).  We treat coinMint as the new token.
        """
        try:
            sig_short = (signature[:20] + '...') if signature else '—'
            base = {
                'source':   'raydium',
                'platform': 'raydium',
                'signature': signature,
                'sig_short': sig_short,
                'solscan_url': f'https://solscan.io/tx/{signature}' if signature else None,
                'detected_at': datetime.utcnow().isoformat() + 'Z',
                'token_mint': None,
                'token_name': sig_short,
                'token_symbol': 'RAY-NEW',
                'initial_liquidity': 0.0,
                'market_cap': 0.0,
            }

            if not signature:
                return base

            # Fetch account keys from the transaction
            accounts = self.rpc.get_transaction_accounts(signature)
            if not accounts or len(accounts) < 8:
                return base

            # Raydium AMM: coinMint is at position 6 in the transaction accounts
            # (standard InitializeV4 layout — skip index 0 which is the AMM itself)
            # We try indices 6 and 7; whichever is NOT the SOL/USDC mint is the new token.
            QUOTE_MINTS = {
                "So11111111111111111111111111111111111111112",   # Wrapped SOL
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
            }

            mint_address = None
            for idx in (6, 7, 8, 9):
                if idx < len(accounts):
                    pubkey = accounts[idx]['pubkey']
                    if pubkey and pubkey not in QUOTE_MINTS:
                        mint_address = pubkey
                        break

            if not mint_address:
                return base

            base['token_mint'] = mint_address
            base['solscan_url'] = f'https://solscan.io/tx/{signature}'
            base['dexscreener_url'] = f'https://dexscreener.com/solana/{mint_address}'

            # Step 1: Read name/symbol from on-chain Metaplex metadata (no external API)
            meta = self.rpc.get_token_metadata(mint_address)
            if meta:
                base['token_name']   = meta['name']
                base['token_symbol'] = meta['symbol']
                print(
                    f"[TOKEN-DETECTOR] 📋 On-chain metadata: "
                    f"{meta['symbol']} / {meta['name']}"
                )

            # Step 2: DexScreener for liquidity, market cap, price (retries internally)
            dex_data = self.dexscreener.get_token_data(mint_address, retries=3, retry_delay=5.0)
            if dex_data:
                base.update({
                    'token_name':        dex_data['name']   if not meta else base['token_name'],
                    'token_symbol':      dex_data['symbol'] if not meta else base['token_symbol'],
                    'initial_liquidity': dex_data['liquidity_usd'],
                    'market_cap':        dex_data['market_cap'],
                    'volume_1h':         dex_data['volume_1h'],
                    'price_usd':         dex_data['price_usd'],
                    'dexscreener_url':   dex_data['dexscreener_url'],
                })

            return base

        except Exception as e:
            self.logger.error(f"Error parsing Raydium pool ({signature[:20] if signature else '?'}): {e}")
            return None

    def _parse_pumpfun_token(self, tx_data: Dict, signature: str) -> Optional[Dict]:
        """
        Parse a Pump.fun token-launch transaction.

        Pump.fun `create` instruction account layout (0-indexed within the
        instruction's account list):
          0  mint            ← the new token's mint address (signer)
          1  mintAuthority
          2  bondingCurve
          3  associatedBondingCurve
          4  global
          5  mplTokenMetadata
          6  metadata
          7  user (creator)  ← signer
          ...

        In the transaction message accountKeys the mint is the FIRST account
        that is both writable and a signer (excluding the fee-payer).
        """
        try:
            sig_short = (signature[:20] + '...') if signature else '—'
            base = {
                'source': 'pumpfun',
                'signature': signature,
                'sig_short': sig_short,
                'solscan_url': f'https://solscan.io/tx/{signature}' if signature else None,
                'detected_at': datetime.utcnow().isoformat() + 'Z',
                'token_mint': None,
                'token_name': sig_short,
                'token_symbol': 'PUMP-NEW',
                'initial_liquidity': 0.0,
                'market_cap': 0.0,
                'bonding_curve': None,
                'creator': None,
            }

            if not signature:
                return base

            # Fetch full transaction JSON to get account keys (retried internally)
            accounts = self.rpc.get_transaction_accounts(signature)
            if not accounts:
                # Last-resort: emit with just the signature so the token appears
                # in the UI immediately; the refresh loop will fill in the rest.
                base['solscan_url'] = f'https://solscan.io/tx/{signature}'
                return base

            # The Pump.fun create instruction uses a fixed account order.
            # In the transaction message:
            #   account[0] = fee-payer/creator (signer, writable)
            #   account[1] = new mint          (signer in most cases)
            # We use signer flag when available to find the mint, then fall back
            # to positional search.
            _SKIP_ACCOUNTS = {
                "11111111111111111111111111111111",                  # System program
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",     # Token program
                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",     # Token-2022
                PUMP_FUN_PROGRAM_ID,
                "SysvarRent111111111111111111111111111111111",
                "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bRS",    # ATA program
                "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",     # Metaplex
            }

            mint_address = None
            creator = accounts[0]['pubkey'] if accounts else None

            # First pass: look for signer accounts (excluding fee-payer at index 0)
            for acc in accounts[1:]:
                pubkey = acc.get('pubkey', '')
                if acc.get('signer') and pubkey and pubkey not in _SKIP_ACCOUNTS:
                    mint_address = pubkey
                    break

            # Fallback: positional search at indices 1–3
            if not mint_address:
                for idx in (1, 2, 3):
                    if idx < len(accounts):
                        pubkey = accounts[idx]['pubkey']
                        if pubkey and len(pubkey) >= 32 and pubkey not in _SKIP_ACCOUNTS:
                            mint_address = pubkey
                            break

            if not mint_address:
                return base

            base['token_mint'] = mint_address
            base['creator']    = creator
            base['dexscreener_url'] = f'https://dexscreener.com/solana/{mint_address}'

            # Step 1: Read name/symbol from on-chain Metaplex metadata (no external API)
            meta = self.rpc.get_token_metadata(mint_address)
            if meta:
                base['token_name']   = meta['name']
                base['token_symbol'] = meta['symbol']
                print(
                    f"[TOKEN-DETECTOR] 📋 On-chain metadata: "
                    f"{meta['symbol']} / {meta['name']}"
                )

            # Step 2: Try DexScreener for market data (only works post-migration to DEX)
            dex_data = self.dexscreener.get_token_data(mint_address, retries=2, retry_delay=4.0)
            if dex_data:
                base.update({
                    'token_name':        dex_data['name']   if not meta else base['token_name'],
                    'token_symbol':      dex_data['symbol'] if not meta else base['token_symbol'],
                    'initial_liquidity': dex_data['liquidity_usd'],
                    'market_cap':        dex_data['market_cap'],
                    'volume_1h':         dex_data['volume_1h'],
                    'price_usd':         dex_data['price_usd'],
                    'dexscreener_url':   dex_data['dexscreener_url'],
                })
            else:
                # Step 3: Token is still on the bonding curve — read state directly
                # from the Pump.fun BondingCurve on-chain account (no external API).
                curve = self.rpc.get_pumpfun_bonding_curve(mint_address)
                if curve:
                    sol_price = self.dexscreener.get_sol_price_usd()
                    base.update({
                        'bonding_curve':     curve,
                        'price_usd':         curve['price_sol'] * sol_price,
                        'market_cap':        curve['market_cap_sol'] * sol_price,
                        # Liquidity approximation: real SOL raised × 2 (token side ≈ equal value)
                        'initial_liquidity': curve['real_sol'] * sol_price * 2,
                    })
                    print(
                        f"[TOKEN-DETECTOR] 📈 Bonding curve: "
                        f"MC=${base['market_cap']:,.0f} "
                        f"liq~${base['initial_liquidity']:,.0f} "
                        f"(SOL=${sol_price:.0f})"
                    )

            return base

        except Exception as e:
            self.logger.error(f"Error parsing Pump.fun token ({signature[:20] if signature else '?'}): {e}")
            return None

    def _refresh_loop(self) -> None:
        """
        Background thread: re-fetches DexScreener market data for recently
        detected tokens that still have liquidity=0 or market_cap=0.

        Problem: DexScreener typically takes 30–120 seconds to index a brand-new
        pool after it's created on-chain.  At detection time the data is often
        not yet available, so we store zeros.  This thread fixes those zeros
        once the indexer catches up.
        """
        self.logger.info("Market-data refresh thread started")
        # Short initial delay so we don't race with the first batch of detections
        time.sleep(self._refresh_interval)

        while self.is_running:
            try:
                self._refresh_recent_tokens()
            except Exception as e:
                self.logger.debug(f"Refresh pass error (non-fatal): {e}")
            time.sleep(self._refresh_interval)

        self.logger.info("Market-data refresh thread stopped")

    def _refresh_recent_tokens(self) -> None:
        """
        Single refresh pass: update market data for recently detected tokens
        that still have liquidity=0 or market_cap=0.
        """
        now = datetime.utcnow()
        updated = 0

        for token in self.detected_tokens:
            # Only consider tokens detected within the refresh window
            detected_at = token.get('detected_at')
            if detected_at:
                try:
                    age_s = (now - datetime.fromisoformat(
                        detected_at.rstrip('Z')
                    )).total_seconds()
                    if age_s > self._refresh_window:
                        continue  # too old — skip
                except Exception:
                    pass

            # Skip tokens that already have market data
            if token.get('initial_liquidity', 0) > 0 and token.get('market_cap', 0) > 0:
                continue

            mint = token.get('token_mint')
            if not mint:
                # If we have the signature, try once more to resolve the mint
                sig = token.get('signature')
                if not sig:
                    continue
                accounts = self.rpc.get_transaction_accounts(sig)
                if not accounts:
                    continue
                _SKIP = {
                    "11111111111111111111111111111111",
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                    PUMP_FUN_PROGRAM_ID,
                    "SysvarRent111111111111111111111111111111111",
                    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bRS",
                    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s",
                }
                creator = accounts[0]['pubkey'] if accounts else None
                for acc in accounts[1:]:
                    pk = acc.get('pubkey', '')
                    if acc.get('signer') and pk and pk not in _SKIP:
                        mint = pk
                        break
                if not mint:
                    for idx in (1, 2, 3):
                        if idx < len(accounts):
                            pk = accounts[idx]['pubkey']
                            if pk and len(pk) >= 32 and pk not in _SKIP:
                                mint = pk
                                break
                if not mint:
                    continue
                token['token_mint']      = mint
                token['creator']         = creator
                token['dexscreener_url'] = f'https://dexscreener.com/solana/{mint}'
                # Try to get on-chain metadata now that we have the mint
                meta = self.rpc.get_token_metadata(mint)
                if meta:
                    token['token_name']   = meta['name']
                    token['token_symbol'] = meta['symbol']
                self.logger.info(f"[REFRESH] Recovered mint for PUMP-NEW: {mint[:12]}…")

            # Avoid hammering RPC — small pause between lookups
            time.sleep(0.5)

            try:
                dex_data = self.dexscreener.get_token_data(mint, retries=1, retry_delay=0)
                if dex_data and (dex_data.get('liquidity_usd', 0) > 0
                                 or dex_data.get('market_cap', 0) > 0):
                    # Update in-place — the list is shared with the REST endpoint
                    if dex_data.get('liquidity_usd', 0) > 0:
                        token['initial_liquidity'] = dex_data['liquidity_usd']
                    if dex_data.get('market_cap', 0) > 0:
                        token['market_cap'] = dex_data['market_cap']
                    if dex_data.get('price_usd'):
                        token['price_usd'] = dex_data['price_usd']
                    if dex_data.get('volume_1h'):
                        token['volume_1h'] = dex_data['volume_1h']
                    if not token.get('token_name') or token['token_name'] == token.get('sig_short'):
                        token['token_name'] = dex_data.get('name', token['token_name'])
                    if not token.get('token_symbol') or token['token_symbol'] in ('PUMP-NEW', 'RAY-NEW'):
                        token['token_symbol'] = dex_data.get('symbol', token['token_symbol'])
                    updated += 1
                    if self.db:
                        self.db.update_token_market_data(mint, dex_data)
                    self.logger.debug(
                        f"Refreshed {token.get('token_symbol','?')}: "
                        f"liq=${token['initial_liquidity']:,.0f} "
                        f"mc=${token['market_cap']:,.0f}"
                    )
                elif token.get('source') == 'pumpfun':
                    # Still on bonding curve — read it directly
                    curve = self.rpc.get_pumpfun_bonding_curve(mint)
                    if curve and curve.get('real_sol', 0) > 0:
                        sol_price = self.dexscreener.get_sol_price_usd()
                        token['bonding_curve']     = curve
                        token['initial_liquidity'] = curve['real_sol'] * sol_price * 2
                        token['market_cap']        = curve['market_cap_sol'] * sol_price
                        token['price_usd']         = curve['price_sol'] * sol_price
                        updated += 1
                        if self.db:
                            self.db.update_token_market_data(mint, {
                                'liquidity_usd': token['initial_liquidity'],
                                'market_cap': token['market_cap'],
                                'price_usd': token['price_usd'],
                            })
            except Exception as e:
                self.logger.debug(f"Refresh error for {mint[:12]}…: {e}")

        if updated:
            self.logger.info(f"Market-data refresh: updated {updated} token(s)")

    def _emit_token_detected(self, token_info: Dict):
        """
        Emit token detected event to all registered callbacks.

        Args:
            token_info: Token information dictionary
        """
        # Add to in-memory cache (fast API responses)
        self.detected_tokens.insert(0, token_info)
        if len(self.detected_tokens) > self.max_cache_size:
            self.detected_tokens = self.detected_tokens[:self.max_cache_size]

        # Persist to database (all data, for AI training)
        if self.db:
            try:
                self.db.save_detected_token(token_info)
            except Exception as e:
                self.logger.error(f"DB save error for token {token_info.get('token_mint','?')}: {e}")

        # Log detection
        self.logger.info(
            f"New token detected from {token_info['source']}: "
            f"{token_info.get('token_symbol', 'UNKNOWN')} "
            f"(signature: {token_info['signature']})"
        )

        # Call registered callbacks
        for callback in self.on_token_detected_callbacks:
            try:
                callback(token_info)
            except Exception as e:
                self.logger.error(f"Error in token detected callback: {e}")

    def on_token_detected(self, callback: Callable):
        """
        Register a callback for token detection events.

        Args:
            callback: Function to call when token is detected
        """
        self.on_token_detected_callbacks.append(callback)
        self.logger.debug(f"Registered token detection callback")

    def get_detected_tokens(self, limit: int = 50, source: Optional[str] = None) -> List[Dict]:
        """
        Get recently detected tokens.

        Args:
            limit: Maximum number of tokens to return
            source: Optional filter by source ('raydium', 'pumpfun')

        Returns:
            List of detected token info
        """
        tokens = self.detected_tokens

        # Filter by source if specified
        if source:
            tokens = [t for t in tokens if t.get('source') == source]

        # Limit results
        return tokens[:limit]

    # ---------------------------------------------------------------
    # AI outcome tracking (price at 1h / 6h / 24h post-detection)
    # ---------------------------------------------------------------

    def _outcome_loop(self) -> None:
        """
        Background thread: records the price of every detected token at
        1 hour, 6 hours, and 24 hours after detection.

        These timestamps become the ground-truth labels for the ML training
        dataset.  A token that 10×'d by hour 1 is labelled very differently
        from one that lost 90% in the same window.

        Runs every _outcome_check_interval seconds (default 5 min).
        Requires self.db to be set — start() only spawns this thread when a
        DatabaseManager is available.
        """
        self.logger.info("AI outcome tracker started")
        # Short initial delay — let the server finish booting before first pass
        time.sleep(10)

        while self._outcome_running:
            try:
                self._check_outcomes()
            except Exception as e:
                import traceback
                print(f"[OUTCOME-TRACKER] ❌ Pass error: {e}")
                traceback.print_exc()
            time.sleep(self._outcome_check_interval)

        print("[OUTCOME-TRACKER] stopped")

    def _check_outcomes(self) -> None:
        """
        Single outcome-check pass.

        For each token detected more than 1 hour ago that still has
        outcome_complete=0, determine which checkpoints (1h/6h/24h) are
        now due and fetch the current price from DexScreener or the
        bonding curve RPC.

        If the price cannot be fetched and the token is well past the
        checkpoint deadline (grace period below), it is treated as dead/rugged
        and recorded with price=0.  This is a valid and valuable ML label.
        """
        if not self.db:
            return

        # Larger batch — drain the backlog of old tokens faster.
        pending = self.db.get_tokens_pending_outcome(limit=100)
        print(f"[OUTCOME-TRACKER] Pass: {len(pending)} pending tokens to check")
        if not pending:
            return

        # Grace period per checkpoint: how many extra hours we wait before
        # declaring a price-unavailable token as dead.
        DEAD_GRACE = {'1h': 1.0, '6h': 2.0, '24h': 4.0}

        updated = 0
        now = datetime.utcnow()

        for token in pending:
            token_id    = token.get('id')
            mint        = token.get('token_mint')
            entry_price = token.get('price_usd') or 0.0
            if not mint or not token_id:
                continue

            # Parse detection time
            try:
                detected_at = datetime.fromisoformat(
                    (token.get('detected_at') or '').rstrip('Z')
                )
            except Exception:
                continue

            age_hours = (now - detected_at).total_seconds() / 3600

            # Determine which checkpoints are due but not yet recorded
            checkpoints_due = []
            if age_hours >= 1  and token.get('outcome_price_1h')  is None:
                checkpoints_due.append('1h')
            if age_hours >= 6  and token.get('outcome_price_6h')  is None:
                checkpoints_due.append('6h')
            if age_hours >= 24 and token.get('outcome_price_24h') is None:
                checkpoints_due.append('24h')

            if not checkpoints_due:
                # All due checkpoints recorded but outcome_complete still 0
                if age_hours >= 24:
                    self.db.update_token_outcome(
                        token_id, '24h',
                        token.get('outcome_price_24h') or 0.0,
                        token.get('outcome_max_price') or 0.0,
                        token.get('outcome_max_gain_pct') or 0.0,
                        complete=True
                    )
                continue

            # Pause between tokens — outcome checks share the DexScreener rate
            # budget with the refresh loop and sim checker; 1.5s keeps us under
            # the free-tier limit even when all three are active simultaneously.
            time.sleep(1.5)
            current_price = self._fetch_outcome_price(mint)

            if current_price is None or current_price == 0:
                # Price unavailable — check whether we've waited long enough past
                # each checkpoint to declare the token dead (price = 0 / rugged).
                # Only handle the earliest pending checkpoint to keep logic simple;
                # the next pass will handle the rest.
                earliest_cp = checkpoints_due[0]
                cp_deadline = {'1h': 1, '6h': 6, '24h': 24}[earliest_cp]
                grace       = DEAD_GRACE[earliest_cp]

                if age_hours >= cp_deadline + grace:
                    # Token is dead — record 0 as outcome price (rugged label)
                    max_price = token.get('outcome_max_price') or 0.0
                    gain_pct  = round((max_price - entry_price) / entry_price * 100, 2) \
                                if entry_price > 0 else -100.0
                    complete  = (earliest_cp == '24h') or (age_hours >= 24 + DEAD_GRACE['24h'])

                    # Fill all overdue checkpoints with 0 in one pass
                    for cp in checkpoints_due:
                        cp_age = {'1h': 1, '6h': 6, '24h': 24}[cp]
                        if age_hours >= cp_age + DEAD_GRACE[cp]:
                            is_last = (cp == '24h') or (age_hours >= 24 + DEAD_GRACE['24h'])
                            self.db.update_token_outcome(
                                token_id, cp, 0.0, max_price, gain_pct, complete=is_last
                            )
                            self.logger.debug(
                                f"Outcome {cp} (dead): {mint[:12]}… "
                                f"age={age_hours:.1f}h — marked rugged"
                            )
                            updated += 1
                continue

            # Price fetched successfully — rolling max and best gain
            prev_max  = token.get('outcome_max_price') or 0.0
            new_max   = max(prev_max, current_price)
            gain_pct  = round((new_max - entry_price) / entry_price * 100, 2) \
                        if entry_price > 0 else 0.0

            for cp in checkpoints_due:
                complete = (cp == '24h') or (age_hours >= 24)
                self.db.update_token_outcome(
                    token_id, cp, current_price, new_max, gain_pct, complete
                )
                self.logger.debug(
                    f"Outcome {cp}: {mint[:12]}… "
                    f"price=${current_price:.2e} "
                    f"gain={gain_pct:+.1f}%"
                )
                updated += 1

        print(f"[OUTCOME-TRACKER] Pass done — updated {updated} checkpoint(s)")

    def _fetch_outcome_price(self, mint: str) -> Optional[float]:
        """
        Fetch the current USD price for a token.
        DexScreener is tried first; falls back to on-chain bonding curve
        for tokens still in the pump.fun pre-graduation phase.
        """
        try:
            # retries=0 — a single attempt; dead tokens will 429 on retry too
            data = self.dexscreener.get_token_data(mint, retries=0, retry_delay=0)
            if data and data.get('price_usd'):
                return data['price_usd']
        except Exception:
            pass

        try:
            curve = self.rpc.get_pumpfun_bonding_curve(mint)
            if curve and curve.get('price_sol', 0) > 0:
                sol_price = self.dexscreener.get_sol_price_usd()
                return curve['price_sol'] * sol_price
        except Exception:
            pass

        return None

    def get_status(self) -> Dict:
        """
        Get token detector status.

        Returns:
            Status dictionary
        """
        return {
            'running': self.is_running,
            'subscriptions': len(self.subscriptions),
            'detected_count': len(self.detected_tokens),
            'active_sources': list(self.subscriptions.keys())
        }
