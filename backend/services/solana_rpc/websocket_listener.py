"""
Solana WebSocket Listener
Real-time monitoring of blockchain events via WebSocket connection.
Supports account changes, program logs, and signature notifications.
"""

import asyncio
import json
import logging
from typing import Dict, List, Callable, Optional, Any
from datetime import datetime
import websockets
from solders.pubkey import Pubkey
from solders.signature import Signature


class EventEmitter:
    """
    Simple event emitter for pub/sub pattern.
    Allows modules to subscribe to WebSocket events.
    """

    def __init__(self):
        self._listeners: Dict[str, List[Callable]] = {}
        self.logger = logging.getLogger(__name__)

    def on(self, event_name: str, callback: Callable):
        """
        Register an event listener.

        Args:
            event_name: Name of the event to listen for
            callback: Function to call when event occurs
        """
        if event_name not in self._listeners:
            self._listeners[event_name] = []

        self._listeners[event_name].append(callback)
        self.logger.debug(f"Registered listener for event: {event_name}")

    def off(self, event_name: str, callback: Callable):
        """
        Remove an event listener.

        Args:
            event_name: Name of the event
            callback: Function to remove
        """
        if event_name in self._listeners:
            self._listeners[event_name] = [
                cb for cb in self._listeners[event_name] if cb != callback
            ]

    def emit(self, event_name: str, data: Any):
        """
        Emit an event to all registered listeners.

        Args:
            event_name: Name of the event
            data: Event data to pass to listeners
        """
        if event_name in self._listeners:
            for callback in self._listeners[event_name]:
                try:
                    # Call callback with event data
                    if asyncio.iscoroutinefunction(callback):
                        asyncio.create_task(callback(data))
                    else:
                        callback(data)
                except Exception as e:
                    self.logger.error(f"Error in event listener for {event_name}: {e}")


class SolanaWebSocketListener:
    """
    WebSocket client for real-time Solana blockchain monitoring.
    Handles subscriptions to accounts, programs, and signatures.
    """

    def __init__(self, ws_url: str, commitment: str = 'confirmed'):
        """
        Initialize WebSocket listener.

        Args:
            ws_url: WebSocket endpoint URL
            commitment: Commitment level (processed, confirmed, finalized)
        """
        self.ws_url = ws_url
        self.commitment = commitment
        self.websocket = None
        self.is_connected = False
        self.is_running = False

        # Subscription tracking
        # key = server_sub_id after confirmation; key = request_id before confirmation
        self.subscriptions: Dict[int, Dict] = {}
        self.next_id = 1
        # Maps client request_id → server_sub_id once confirmed
        self._req_to_server: Dict[int, int] = {}

        # Event emitter for pub/sub
        self.events = EventEmitter()

        # Logger
        self.logger = logging.getLogger(__name__)

        # Reconnection settings
        self.reconnect_delay = 5  # seconds
        self.max_reconnect_attempts = 10
        self.reconnect_attempts = 0

    async def connect(self):
        """
        Establish WebSocket connection to Solana.
        """
        try:
            print(f"[WS-LISTENER] 🔌 Connecting to WebSocket: {self.ws_url}")
            self.logger.info(f"Connecting to WebSocket: {self.ws_url}")
            self.websocket = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=10
            )
            self.is_connected = True
            self.reconnect_attempts = 0
            print("[WS-LISTENER] ✅ WebSocket connected successfully")
            self.logger.info("WebSocket connected successfully")

            # Emit connection event
            self.events.emit('connected', {'timestamp': datetime.utcnow().isoformat()})

        except Exception as e:
            print(f"[WS-LISTENER] ❌ Failed to connect: {e}")
            self.logger.error(f"Failed to connect to WebSocket: {e}")
            self.is_connected = False
            raise

    async def disconnect(self):
        """
        Close WebSocket connection.
        """
        self.is_running = False
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            self.logger.info("WebSocket disconnected")

            # Emit disconnection event
            self.events.emit('disconnected', {'timestamp': datetime.utcnow().isoformat()})

    async def _send_request(self, method: str, params: List) -> int:
        """
        Send a JSON-RPC request via WebSocket.

        Args:
            method: RPC method name
            params: Method parameters

        Returns:
            Request ID
        """
        if not self.is_connected or not self.websocket:
            raise ConnectionError("WebSocket not connected")

        request_id = self.next_id
        self.next_id += 1

        request = {
            'jsonrpc': '2.0',
            'id': request_id,
            'method': method,
            'params': params
        }

        await self.websocket.send(json.dumps(request))
        self.logger.debug(f"Sent request: {method} (ID: {request_id})")

        return request_id

    async def subscribe_account(self, account_address: str, callback: Optional[Callable] = None) -> int:
        """
        Subscribe to account changes.

        Args:
            account_address: Account public key
            callback: Optional callback for account updates

        Returns:
            Subscription ID
        """
        try:
            # Validate address
            pubkey = Pubkey.from_string(account_address)

            # Send subscription request
            request_id = await self._send_request(
                'accountSubscribe',
                [
                    str(pubkey),
                    {
                        'encoding': 'jsonParsed',
                        'commitment': self.commitment
                    }
                ]
            )

            # Store subscription info
            self.subscriptions[request_id] = {
                'type': 'account',
                'address': account_address,
                'callback': callback,
                'created_at': datetime.utcnow().isoformat()
            }

            self.logger.info(f"Subscribed to account: {account_address}")
            return request_id

        except Exception as e:
            self.logger.error(f"Failed to subscribe to account {account_address}: {e}")
            raise

    async def subscribe_program(self, program_id: str, callback: Optional[Callable] = None) -> int:
        """
        Subscribe to program account changes.

        Args:
            program_id: Program public key
            callback: Optional callback for program updates

        Returns:
            Subscription ID
        """
        try:
            # Validate program ID
            pubkey = Pubkey.from_string(program_id)

            # Send subscription request
            request_id = await self._send_request(
                'programSubscribe',
                [
                    str(pubkey),
                    {
                        'encoding': 'jsonParsed',
                        'commitment': self.commitment
                    }
                ]
            )

            # Store subscription info
            self.subscriptions[request_id] = {
                'type': 'program',
                'program_id': program_id,
                'callback': callback,
                'created_at': datetime.utcnow().isoformat()
            }

            self.logger.info(f"Subscribed to program: {program_id}")
            return request_id

        except Exception as e:
            self.logger.error(f"Failed to subscribe to program {program_id}: {e}")
            raise

    async def subscribe_logs(self, mentions: Optional[List[str]] = None, callback: Optional[Callable] = None) -> int:
        """
        Subscribe to transaction logs.

        Args:
            mentions: Optional list of addresses to filter logs
            callback: Optional callback for log updates

        Returns:
            Subscription ID
        """
        try:
            # Build filter
            if mentions:
                filter_param = {'mentions': mentions}
            else:
                filter_param = 'all'

            print(f"[WS-LISTENER] 📤 Sending logsSubscribe request (mentions: {mentions})")

            # Send subscription request
            request_id = await self._send_request(
                'logsSubscribe',
                [
                    filter_param,
                    {
                        'commitment': self.commitment
                    }
                ]
            )

            # Store subscription info
            self.subscriptions[request_id] = {
                'type': 'logs',
                'mentions': mentions,
                'callback': callback,
                'created_at': datetime.utcnow().isoformat()
            }

            print(f"[WS-LISTENER] ✅ logsSubscribe request sent with request_id: {request_id}")
            print(f"[WS-LISTENER] 🕐 Waiting for server confirmation...")
            self.logger.info(f"Subscribed to logs (mentions: {mentions})")
            return request_id

        except Exception as e:
            print(f"[WS-LISTENER] ❌ Failed to subscribe to logs: {e}")
            self.logger.error(f"Failed to subscribe to logs: {e}")
            raise

    async def subscribe_signature(self, signature: str, callback: Optional[Callable] = None) -> int:
        """
        Subscribe to signature notifications.

        Args:
            signature: Transaction signature
            callback: Optional callback for signature updates

        Returns:
            Subscription ID
        """
        try:
            # Validate signature
            sig = Signature.from_string(signature)

            # Send subscription request
            request_id = await self._send_request(
                'signatureSubscribe',
                [
                    str(sig),
                    {
                        'commitment': self.commitment
                    }
                ]
            )

            # Store subscription info
            self.subscriptions[request_id] = {
                'type': 'signature',
                'signature': signature,
                'callback': callback,
                'created_at': datetime.utcnow().isoformat()
            }

            self.logger.info(f"Subscribed to signature: {signature}")
            return request_id

        except Exception as e:
            self.logger.error(f"Failed to subscribe to signature {signature}: {e}")
            raise

    async def unsubscribe(self, subscription_id: int):
        """
        Unsubscribe from a subscription.

        Args:
            subscription_id: Client request_id OR confirmed server_sub_id
        """
        # Resolve client request_id → server_sub_id if needed
        actual_id = self._req_to_server.get(subscription_id, subscription_id)

        if actual_id not in self.subscriptions:
            self.logger.warning(
                f"Subscription not found: requested={subscription_id} resolved={actual_id}"
            )
            return

        sub_info = self.subscriptions[actual_id]
        subscription_id = actual_id  # use the real ID from here on
        sub_type = sub_info['type']

        # Determine unsubscribe method
        method_map = {
            'account': 'accountUnsubscribe',
            'program': 'programUnsubscribe',
            'logs': 'logsUnsubscribe',
            'signature': 'signatureUnsubscribe'
        }

        method = method_map.get(sub_type)
        if not method:
            self.logger.error(f"Unknown subscription type: {sub_type}")
            return

        try:
            await self._send_request(method, [subscription_id])
            del self.subscriptions[subscription_id]
            # Remove reverse mapping entries that pointed to this server_sub_id
            stale_keys = [k for k, v in self._req_to_server.items() if v == subscription_id]
            for k in stale_keys:
                del self._req_to_server[k]
            self.logger.info(f"Unsubscribed from {sub_type} (ID: {subscription_id})")
        except Exception as e:
            self.logger.error(f"Failed to unsubscribe {subscription_id}: {e}")

    # Solana sends these method names for push notifications.
    # The old code incorrectly checked for 'subscription' which never matches.
    _NOTIFICATION_METHODS = {
        'logsNotification':      'logs',
        'accountNotification':   'account',
        'programNotification':   'program',
        'signatureNotification': 'signature',
        'slotNotification':      'slot',
        'rootNotification':      'root',
        'voteNotification':      'vote',
        'blockNotification':     'block',
    }

    async def _handle_message(self, message: Dict):
        """
        Handle an incoming WebSocket message from Solana.

        Solana uses JSON-RPC 2.0 over WebSocket.  There are two message types:
          1. Subscription confirmations — response to a *Subscribe request:
               {"jsonrpc":"2.0","id":<req_id>,"result":<server_sub_id>}
          2. Push notifications — unsolicited events for active subscriptions:
               {"jsonrpc":"2.0","method":"logsNotification",
                "params":{"result":{...},"subscription":<server_sub_id>}}

        Args:
            message: Parsed JSON-RPC message dict
        """
        try:
            method = message.get('method', '')

            # ── 1. Subscription confirmation ────────────────────────────────
            if 'result' in message and 'id' in message and method == '':
                request_id = message['id']
                server_sub_id = message['result']

                if not isinstance(server_sub_id, int):
                    # Some RPCs return error objects in 'result' — skip
                    return

                self.logger.debug(
                    f"Subscription confirmed: req={request_id} -> sub={server_sub_id}"
                )
                print(
                    f"[WS-LISTENER] ✅ Sub confirmed: req_id={request_id} "
                    f"-> server_sub_id={server_sub_id}"
                )

                if request_id in self.subscriptions:
                    sub_info = self.subscriptions.pop(request_id)
                    self.subscriptions[server_sub_id] = sub_info
                    # Track the mapping so callers using the old request_id can still unsubscribe
                    self._req_to_server[request_id] = server_sub_id
                else:
                    # Confirmation for a request we didn't track (e.g. unsubscribe ack)
                    self.logger.debug(
                        f"Confirmation for unknown req_id {request_id} (may be unsubscribe ack)"
                    )

            # ── 2. Push notification ─────────────────────────────────────────
            elif method in self._NOTIFICATION_METHODS:
                params = message.get('params', {})
                server_sub_id = params.get('subscription')
                result = params.get('result')

                # Derive our internal sub type from the method name
                sub_type = self._NOTIFICATION_METHODS[method]

                # print(
                #     f"[WS-LISTENER] 🔔 {method} "
                #     f"(sub_id={server_sub_id})"
                # )

                event_data = {
                    'subscription_id': server_sub_id,
                    'type': sub_type,
                    'method': method,
                    'data': result,
                    'timestamp': datetime.utcnow().isoformat(),
                }

                # Dispatch to registered callback (if any)
                sub_info = self.subscriptions.get(server_sub_id)
                if sub_info and sub_info.get('callback'):
                    callback = sub_info['callback']
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(event_data)
                        else:
                            callback(event_data)
                    except Exception as cb_err:
                        self.logger.error(
                            f"Callback error for {method}: {cb_err}"
                        )
                elif not sub_info:
                    self.logger.debug(
                        f"No subscription info for server_sub_id={server_sub_id}; "
                        f"known={list(self.subscriptions.keys())}"
                    )

                # Always emit generic events for listeners registered via .on()
                self.events.emit(f'{sub_type}_update', event_data)
                self.events.emit('update', event_data)

            # ── 3. RPC error response ────────────────────────────────────────
            elif 'error' in message:
                error = message['error']
                print(f"[WS-LISTENER] ❌ RPC ERROR: {error}")
                self.logger.error(f"WebSocket RPC error: {error}")

            # ── 4. Unknown / unhandled ───────────────────────────────────────
            else:
                self.logger.debug(
                    f"Unhandled WS message keys={list(message.keys())} method={method!r}"
                )

        except Exception as e:
            print(f"[WS-LISTENER] ❌ Exception handling message: {e}")
            import traceback
            traceback.print_exc()
            self.logger.error(f"Error handling message: {e}")

    async def _restore_subscriptions(self):
        """
        Re-send all subscription requests after a reconnect.
        Called every time a new WebSocket connection is established.
        The subscriptions dict still holds the previous server_sub_ids as keys;
        we migrate them to new server_sub_ids as confirmations arrive.
        """
        if not self.subscriptions:
            return

        saved = list(self.subscriptions.values())
        # Clear all old IDs — they are invalid on the new connection
        self.subscriptions.clear()
        self._req_to_server.clear()

        print(f"[WS-LISTENER] 🔄 Restoring {len(saved)} subscription(s) after reconnect…")
        for sub_info in saved:
            try:
                sub_type = sub_info.get('type')
                callback  = sub_info.get('callback')
                if sub_type == 'logs':
                    await self.subscribe_logs(sub_info.get('mentions'), callback)
                elif sub_type == 'account':
                    await self.subscribe_account(sub_info['address'], callback)
                elif sub_type == 'program':
                    await self.subscribe_program(sub_info['program_id'], callback)
            except Exception as e:
                self.logger.error(f"Failed to restore {sub_info.get('type')} subscription: {e}")

    async def listen(self):
        """
        Start listening for WebSocket messages.
        Reconnects automatically on disconnection and re-registers all
        subscriptions so callers never notice a drop.
        """
        self.is_running = True
        message_count = 0
        last_heartbeat = datetime.utcnow()

        print("[WS-LISTENER] 👂 Starting to listen for messages...")

        while self.is_running:
            try:
                # (Re)connect if needed
                if not self.is_connected:
                    # Capture the flag BEFORE connect() — connect() resets it to 0,
                    # so checking after the call always returns False.
                    needs_restore = self.reconnect_attempts > 0
                    await self.connect()
                    if needs_restore:
                        await self._restore_subscriptions()

                # Read messages until the connection closes
                async for message in self.websocket:
                    message_count += 1

                    now = datetime.utcnow()
                    if (now - last_heartbeat).total_seconds() > 300:
                        print(
                            f"[WS-LISTENER] 💓 Heartbeat: {message_count} msgs | "
                            f"subs: {list(self.subscriptions.keys())}"
                        )
                        last_heartbeat = now

                    try:
                        data = json.loads(message)
                        await self._handle_message(data)
                    except json.JSONDecodeError as e:
                        self.logger.error(f"Failed to parse message: {e}")

            except websockets.exceptions.ConnectionClosed as e:
                self.is_connected = False
                self.reconnect_attempts += 1
                self.logger.warning(
                    f"WebSocket closed ({e.code}), reconnecting in "
                    f"{self.reconnect_delay}s (attempt #{self.reconnect_attempts})…"
                )
                print(
                    f"[WS-LISTENER] ⚠️  Connection closed — reconnecting in "
                    f"{self.reconnect_delay}s (attempt #{self.reconnect_attempts})"
                )
                if self.is_running:
                    await asyncio.sleep(self.reconnect_delay)

            except Exception as e:
                self.is_connected = False
                self.reconnect_attempts += 1
                self.logger.error(f"WebSocket error: {e}")
                if self.is_running:
                    await asyncio.sleep(self.reconnect_delay)
                else:
                    break

        await self.disconnect()

    async def start(self):
        """
        Start the WebSocket listener in the background.
        """
        self.logger.info("Starting WebSocket listener")
        await self.connect()

        # Create background task
        asyncio.create_task(self.listen())

    async def stop(self):
        """
        Stop the WebSocket listener.
        """
        self.logger.info("Stopping WebSocket listener")
        await self.disconnect()

    def get_subscription_info(self, subscription_id: int) -> Optional[Dict]:
        """
        Get information about a subscription.

        Args:
            subscription_id: Subscription ID

        Returns:
            Subscription info or None
        """
        return self.subscriptions.get(subscription_id)

    def get_all_subscriptions(self) -> Dict[int, Dict]:
        """
        Get all active subscriptions.

        Returns:
            Dictionary of subscription ID to subscription info
        """
        return self.subscriptions.copy()

    def get_status(self) -> Dict:
        """
        Get listener status.

        Returns:
            Status dictionary
        """
        return {
            'connected': self.is_connected,
            'running': self.is_running,
            'ws_url': self.ws_url,
            'commitment': self.commitment,
            'active_subscriptions': len(self.subscriptions),
            'reconnect_attempts': self.reconnect_attempts
        }
