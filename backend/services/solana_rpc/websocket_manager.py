"""
WebSocket Manager
Manages WebSocket listener lifecycle and provides synchronous interface for Flask integration.
Runs async WebSocket in a separate thread.
"""

import asyncio
import threading
import time
import logging
from collections import deque
from datetime import datetime
from typing import Dict, List, Callable, Optional, Any
from .websocket_listener import SolanaWebSocketListener


class WebSocketManager:
    """
    Manages Solana WebSocket listener.
    Provides thread-safe interface for Flask integration.
    """

    def __init__(self, ws_url: str, commitment: str = 'confirmed'):
        """
        Initialize WebSocket manager.

        Args:
            ws_url: WebSocket endpoint URL
            commitment: Commitment level
        """
        self.ws_url = ws_url
        self.commitment = commitment
        self.logger = logging.getLogger(__name__)

        # WebSocket listener
        self.listener: Optional[SolanaWebSocketListener] = None

        # Event loop and thread
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.is_running = False

        # Subscription tracking
        self.subscription_callbacks: Dict[int, Callable] = {}

        # Pending subscriptions — stored so they can be re-created automatically
        # after a disconnect/reconnect cycle.  Each entry is a dict with keys:
        #   type      : 'logs' | 'account' | 'program'
        #   callback  : the original callback function
        #   params    : type-specific params (e.g. mentions, account_address)
        self._pending_subscriptions: List[Dict] = []

        # How many times we've (re)connected — 0 means first connect
        self._connection_count: int = 0

        # Seconds to wait before each reconnect attempt (doubles up to 60s)
        self._reconnect_delay: float = 5.0

        # Ring buffer of recent events (last 50) — exposed via API for the UI
        self._recent_events: deque = deque(maxlen=50)

    def _store_event(self, event_data: Any):
        """
        Store an incoming WebSocket event in the recent-events ring buffer.
        Called automatically for every event regardless of subscription type.
        """
        try:
            logs = []
            result = event_data.get('data') or {}
            if isinstance(result, dict):
                value = result.get('value', result)
                if isinstance(value, dict):
                    logs = value.get('logs', [])
                    sig = value.get('signature', '')
                    err = value.get('err')
                else:
                    sig = ''
                    err = None
            else:
                sig = ''
                err = None

            self._recent_events.append({
                'timestamp': event_data.get('timestamp', datetime.utcnow().isoformat()),
                'type': event_data.get('type', 'unknown'),
                'method': event_data.get('method', ''),
                'subscription_id': event_data.get('subscription_id'),
                'signature': sig,
                'err': err,
                'log_preview': logs[0] if logs else None,
                'log_count': len(logs),
            })
        except Exception as e:
            self.logger.debug(f"Could not store event in ring buffer: {e}")

    def _run_event_loop(self):
        """
        Run the WebSocket event loop in a background thread with automatic
        reconnection.  When the connection drops (network blip, server-side
        timeout, etc.) we wait _reconnect_delay seconds and try again.
        All previously registered subscriptions are re-created on each
        reconnect so callers never need to know about disconnections.
        """
        delay = self._reconnect_delay

        while self.is_running:
            attempt = self._connection_count + 1
            self.logger.info(
                f"WebSocket connecting (attempt #{attempt}) to {self.url}"
                if hasattr(self, 'url') else
                f"WebSocket connecting (attempt #{attempt})"
            )

            try:
                # Each reconnect gets a fresh event loop and listener
                self.loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self.loop)
                self.listener = SolanaWebSocketListener(self.ws_url, self.commitment)
                self.listener.events.on('update', self._store_event)

                async def _connect_and_listen():
                    await self.listener.connect()
                    self.logger.info("WebSocket connected")

                    # Re-register subscriptions after reconnect
                    if self._connection_count > 0 and self._pending_subscriptions:
                        self.logger.info(
                            f"Restoring {len(self._pending_subscriptions)} subscription(s)…"
                        )
                        for sub in list(self._pending_subscriptions):
                            try:
                                await self._resubscribe(sub)
                            except Exception as se:
                                self.logger.error(f"Re-subscription failed: {se}")

                    self._connection_count += 1
                    delay_ref[0] = self._reconnect_delay  # reset backoff on success
                    await self.listener.listen()

                delay_ref = [self._reconnect_delay]
                self.loop.run_until_complete(_connect_and_listen())

            except Exception as e:
                self.logger.error(f"WebSocket error: {e}")
            finally:
                try:
                    self.loop.close()
                except Exception:
                    pass

            if not self.is_running:
                break

            self.logger.warning(
                f"WebSocket disconnected — reconnecting in {delay:.0f}s…"
            )
            time.sleep(delay)
            # Exponential backoff, cap at 60s
            delay = min(delay * 2, 60.0)

    async def _resubscribe(self, sub: Dict):
        """Re-create a single subscription on the current listener after reconnect."""
        t        = sub['type']
        callback = sub['callback']
        params   = sub.get('params', {})

        if t == 'logs':
            sub_id = await self.listener.subscribe_logs(params.get('mentions'), callback)
        elif t == 'account':
            sub_id = await self.listener.subscribe_account(params['address'], callback)
        elif t == 'program':
            sub_id = await self.listener.subscribe_program(params['program_id'], callback)
        else:
            return

        if sub_id and callback:
            self.subscription_callbacks[sub_id] = callback

    def start(self):
        """
        Start WebSocket listener in background thread.
        """
        if self.is_running:
            self.logger.warning("WebSocket manager already running")
            return

        self.logger.info("Starting WebSocket manager")
        self.is_running = True

        # Start event loop in separate thread
        self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.thread.start()

        # Wait a moment for connection to establish
        import time
        time.sleep(2)

        self.logger.info("WebSocket manager started")

    def stop(self):
        """
        Stop WebSocket listener and cleanup.
        """
        if not self.is_running:
            return

        self.logger.info("Stopping WebSocket manager")
        self.is_running = False

        # Stop listener
        if self.listener and self.loop:
            asyncio.run_coroutine_threadsafe(
                self.listener.stop(),
                self.loop
            )

        # Wait for thread to finish
        if self.thread:
            self.thread.join(timeout=5)

        self.logger.info("WebSocket manager stopped")

    def subscribe_account(self, account_address: str, callback: Optional[Callable] = None) -> Optional[int]:
        """
        Subscribe to account changes.

        Args:
            account_address: Account public key
            callback: Callback function for updates

        Returns:
            Subscription ID or None if failed
        """
        if not self.listener or not self.loop:
            self.logger.error("WebSocket listener not initialized")
            return None

        # Store so the subscription survives reconnects
        self._pending_subscriptions.append({
            'type': 'account', 'callback': callback,
            'params': {'address': account_address},
        })

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.listener.subscribe_account(account_address, callback),
                self.loop
            )
            subscription_id = future.result(timeout=10)
            if callback:
                self.subscription_callbacks[subscription_id] = callback
            return subscription_id
        except Exception as e:
            self.logger.error(f"Failed to subscribe to account: {e}")
            return None

    def subscribe_program(self, program_id: str, callback: Optional[Callable] = None) -> Optional[int]:
        """
        Subscribe to program account changes.

        Args:
            program_id: Program public key
            callback: Callback function for updates

        Returns:
            Subscription ID or None if failed
        """
        if not self.listener or not self.loop:
            self.logger.error("WebSocket listener not initialized")
            return None

        # Store so the subscription survives reconnects
        self._pending_subscriptions.append({
            'type': 'program', 'callback': callback,
            'params': {'program_id': program_id},
        })

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.listener.subscribe_program(program_id, callback),
                self.loop
            )
            subscription_id = future.result(timeout=10)
            if callback:
                self.subscription_callbacks[subscription_id] = callback
            return subscription_id
        except Exception as e:
            self.logger.error(f"Failed to subscribe to program: {e}")
            return None

    def subscribe_logs(self, mentions: Optional[List[str]] = None, callback: Optional[Callable] = None) -> Optional[int]:
        """
        Subscribe to transaction logs.

        Args:
            mentions: Optional list of addresses to filter
            callback: Callback function for updates

        Returns:
            Subscription ID or None if failed
        """
        if not self.listener or not self.loop:
            self.logger.error("WebSocket listener not initialized")
            return None

        # Store so the subscription survives reconnects
        self._pending_subscriptions.append({
            'type': 'logs', 'callback': callback,
            'params': {'mentions': mentions},
        })

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.listener.subscribe_logs(mentions, callback),
                self.loop
            )
            subscription_id = future.result(timeout=30)
            if callback:
                self.subscription_callbacks[subscription_id] = callback
            return subscription_id
        except asyncio.TimeoutError:
            self.logger.error(f"Timeout subscribing to logs (mentions: {mentions})")
            return None
        except Exception as e:
            self.logger.error(f"Failed to subscribe to logs: {e}")
            return None

    def subscribe_signature(self, signature: str, callback: Optional[Callable] = None) -> Optional[int]:
        """
        Subscribe to signature notifications.

        Args:
            signature: Transaction signature
            callback: Callback function for updates

        Returns:
            Subscription ID or None if failed
        """
        if not self.listener or not self.loop:
            self.logger.error("WebSocket listener not initialized")
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.listener.subscribe_signature(signature, callback),
                self.loop
            )

            subscription_id = future.result(timeout=10)

            if callback:
                self.subscription_callbacks[subscription_id] = callback

            return subscription_id

        except Exception as e:
            self.logger.error(f"Failed to subscribe to signature: {e}")
            return None

    def unsubscribe(self, subscription_id: int):
        """
        Unsubscribe from a subscription.

        Args:
            subscription_id: Subscription ID
        """
        if not self.listener or not self.loop:
            self.logger.error("WebSocket listener not initialized")
            return

        try:
            asyncio.run_coroutine_threadsafe(
                self.listener.unsubscribe(subscription_id),
                self.loop
            )

            # Remove callback
            if subscription_id in self.subscription_callbacks:
                del self.subscription_callbacks[subscription_id]

        except Exception as e:
            self.logger.error(f"Failed to unsubscribe: {e}")

    def on(self, event_name: str, callback: Callable):
        """
        Register event listener.

        Args:
            event_name: Event name (e.g., 'account_update', 'logs_update')
            callback: Callback function
        """
        if not self.listener:
            self.logger.error("WebSocket listener not initialized")
            return

        self.listener.events.on(event_name, callback)

    def off(self, event_name: str, callback: Callable):
        """
        Remove event listener.

        Args:
            event_name: Event name
            callback: Callback function
        """
        if not self.listener:
            return

        self.listener.events.off(event_name, callback)

    def get_status(self) -> Dict:
        """
        Get WebSocket manager status.

        Returns:
            Status dictionary
        """
        if self.listener:
            return {
                'running': self.is_running,
                'listener': self.listener.get_status(),
                'subscriptions': len(self.subscription_callbacks)
            }
        else:
            return {
                'running': self.is_running,
                'listener': None,
                'subscriptions': 0
            }

    def get_subscriptions(self) -> Dict:
        """
        Get all active subscriptions.

        Returns:
            Dictionary of subscriptions
        """
        if not self.listener:
            return {}

        return self.listener.get_all_subscriptions()

    def get_recent_events(self, limit: int = 20) -> List[Dict]:
        """
        Return the most recent WebSocket events from the ring buffer.

        Args:
            limit: Maximum number of events to return (newest first)

        Returns:
            List of event summary dicts
        """
        events = list(self._recent_events)
        events.reverse()  # newest first
        return events[:limit]
