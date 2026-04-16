"""
SolanaSentinel - Main Flask Application
Entry point for the backend API server.
Provides REST endpoints for wallet management, copy trading, sniper bot, and anti-scam analysis.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO
from datetime import datetime
import os
import sys

# Add backend directory to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config.config_manager import ConfigManager
from modules.wallet_manager.wallet_manager import WalletManager
from modules.copy_trading.copy_trading_engine import CopyTradingEngine
from modules.sniper.sniper_engine import SniperEngine
from modules.anti_scam.anti_scam_analyzer import AntiScamAnalyzer
from modules.ai_analyzer.ai_analyzer import AIAnalyzer
from modules.ai_analyzer.model_trainer import ModelTrainer
from modules.rule_engine.rule_engine import RuleEngine
from modules.logging_engine.logging_engine import LoggingEngine
from modules.notifications.notification_manager import NotificationManager
from services.solana_rpc.websocket_manager import WebSocketManager
from services.price_service import PriceService
from services.token_detection.token_detector import TokenDetector
from services.copy_trading.wallet_monitor import WalletMonitor
from utils.response_formatter import success_response, error_response
from data.database import DatabaseManager
import logging

# Initialize Flask app
app = Flask(__name__)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

CORS(app)  # Enable CORS for frontend communication

# Flask-SocketIO — threading mode, no eventlet/gevent required.
# cors_allowed_origins="*" mirrors the CORS policy above.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Rate limiter — memory backend, keyed by remote IP.
# Sensitive endpoints (swap, transfer) get tighter limits.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri="memory://",
)

# Initialize configuration
config = ConfigManager()

# Initialize database (SQLite, replaces growing JSON files)
_db_path = config.get_data_path('') / 'sentinel.db'
db = DatabaseManager(_db_path)

# Initialize modules
wallet_manager = WalletManager(config)

# Initialize WebSocket manager
ws_url = config.get('solana.ws_url', 'wss://api.mainnet-beta.solana.com')
ws_commitment = config.get('solana.commitment', 'confirmed')
websocket_manager = WebSocketManager(ws_url, ws_commitment)

# Initialize token detector
token_detector = TokenDetector(websocket_manager, wallet_manager.rpc_client, db=db)

# ── Startup: bulk-mark old tokens with no recoverable price as dead ──────────
# Any token detected >2h ago that still has no 1h outcome price is almost
# certainly rugged (DexScreener drops dead tokens).  Mark them price=0 now
# so the outcome tracker only needs to handle fresh tokens going forward.
if db:
    try:
        db._conn.execute("""
            UPDATE detected_tokens
            SET outcome_price_1h     = 0.0,
                outcome_max_gain_pct = CASE WHEN price_usd > 0 THEN -100.0 ELSE 0.0 END
            WHERE outcome_price_1h IS NULL
              AND detected_at <= datetime('now', '-2 hours')
        """)
        db._conn.execute("""
            UPDATE detected_tokens SET outcome_price_6h = 0.0
            WHERE outcome_price_6h IS NULL
              AND detected_at <= datetime('now', '-8 hours')
        """)
        db._conn.execute("""
            UPDATE detected_tokens
            SET outcome_price_24h = 0.0,
                outcome_complete  = 1
            WHERE outcome_price_24h IS NULL
              AND detected_at <= datetime('now', '-28 hours')
        """)
        db._conn.commit()
        print("[STARTUP] ✅ Dead-token backlog cleared")
    except Exception as _e:
        print(f"[STARTUP] ⚠️  Backlog clear failed: {_e}")

# Start the outcome tracker immediately so historical tokens get labelled
# even before the sniper (or copy-trading) is activated.
token_detector.start_outcome_tracker()

# Initialize wallet monitor for copy trading
wallet_monitor = WalletMonitor(websocket_manager, wallet_manager.rpc_client)

# Initialize notification manager and price service
notification_manager = NotificationManager(config)
price_service = PriceService()

# Initialize other modules (pass dependencies)
ai_analyzer = AIAnalyzer(config, rpc_client=wallet_manager.rpc_client)

# Model trainer — headless weekly retraining pipeline.
# on_complete reloads the new model into ai_analyzer without a restart.
model_trainer = ModelTrainer(
    db_path    = str(_db_path),
    models_dir = str(config.get_data_path("models")),
)

anti_scam_analyzer = AntiScamAnalyzer(config, wallet_manager.rpc_client)
logging_engine = LoggingEngine(config)
copy_trading_engine = CopyTradingEngine(config, wallet_monitor, notification_manager,
                                        logging_engine=logging_engine,
                                        wallet_manager=wallet_manager,
                                        token_detector=token_detector,
                                        db=db)
sniper_engine = SniperEngine(config, token_detector, notification_manager,
                             ai_analyzer=ai_analyzer, logging_engine=logging_engine,
                             anti_scam_analyzer=anti_scam_analyzer, db=db)
rule_engine = RuleEngine(config)


# ===========================
# REAL-TIME PUSH (SocketIO)
# ===========================

def _push(event: str, data: dict):
    """
    Emit a SocketIO event to all connected frontend clients.
    Safe to call from any background thread.
    """
    try:
        socketio.emit(event, data)
    except Exception as e:
        app.logger.debug(f"SocketIO emit error ({event}): {e}")


def _sanitize(obj):
    """
    Recursively convert any non-JSON-serializable values to strings
    so socketio.emit() never blows up on datetime objects, etc.
    """
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    try:
        import json
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# Wire push callbacks onto the existing event systems.
# Each callback is a thin lambda that sanitizes and emits — the engines
# themselves don't need to know anything about SocketIO.

# 1. New token detected by the sniper / token detector
token_detector.on_token_detected(
    lambda ti: _push('token:detected', _sanitize(ti))
)

# 2. Log entries — push INFO and above only (skip DEBUG to avoid noise/overhead)
def _push_log(entry):
    if entry.get('level', '').upper() not in ('DEBUG',):
        _push('log:entry', _sanitize(entry))

logging_engine.on_log_callbacks.append(_push_log)

# 3. Copy-trading transaction detected (wallet monitor callback)
wallet_monitor.on_transaction(
    lambda ti: _push('ct:trade', _sanitize(ti))
)


# SocketIO connection lifecycle events
@socketio.on('connect')
def on_ws_connect():
    app.logger.debug(f"Frontend client connected via WebSocket")

@socketio.on('disconnect')
def on_ws_disconnect():
    app.logger.debug(f"Frontend client disconnected from WebSocket")


# ===========================
# DIAGNOSTIC ENDPOINT
# ===========================

@app.route('/api/debug/pipeline', methods=['GET'])
def debug_pipeline():
    """
    Full pipeline health check — call this to diagnose why tokens aren't
    being detected.  Returns everything needed to pinpoint the problem.
    """
    ws_status   = websocket_manager.get_status()
    ws_listener = ws_status.get('listener') or {}
    recent_events = websocket_manager.get_recent_events(10)

    detector_status = token_detector.get_status()

    return success_response({
        # ── Sniper ──────────────────────────────────────────────────────
        'sniper_running': sniper_engine.is_running,

        # ── Token detector ──────────────────────────────────────────────
        'detector_running':    detector_status.get('running'),
        'detector_subs':       detector_status.get('active_sources', []),
        'detector_detected':   detector_status.get('detected_count', 0),
        'seen_signatures':     len(token_detector._seen_signatures),

        # ── WebSocket manager ───────────────────────────────────────────
        'ws_manager_running':  ws_status.get('running'),
        'ws_sub_count':        ws_status.get('subscriptions', 0),
        'ws_connection_count': websocket_manager._connection_count,

        # ── WebSocket listener ──────────────────────────────────────────
        'ws_connected':        ws_listener.get('connected'),
        'ws_listener_running': ws_listener.get('running'),
        'ws_active_subs':      ws_listener.get('active_subscriptions', 0),
        'ws_reconnect_attempts': ws_listener.get('reconnect_attempts', 0),
        'ws_url':              ws_listener.get('ws_url', ''),

        # ── Pending (auto-restore) subscriptions ───────────────────────
        'pending_subs': [
            {'type': s['type'], 'params': s.get('params', {})}
            for s in websocket_manager._pending_subscriptions
        ],

        # ── Recent raw events (last 10) ─────────────────────────────────
        # If this list is empty after >30s, no WS events are arriving at all.
        'recent_ws_events': recent_events,

        # ── Polling thread ──────────────────────────────────────────────
        'poll_thread_alive': (
            token_detector._poll_thread is not None and
            token_detector._poll_thread.is_alive()
        ),
        'poll_interval': token_detector._poll_interval,

        # ── pump.fun instruction diagnostics ────────────────────────────
        'pumpfun_events_received': token_detector._pumpfun_events_received,
        'instruction_stats': dict(
            sorted(token_detector._instruction_stats.items(), key=lambda x: -x[1])
        ),

        # ── Raydium instruction diagnostics ─────────────────────────────
        # If raydium_events_received=0 → subscription not firing (RPC filtering or no AMM activity)
        # If raydium_cpmm_events_received=0 → CPMM subscription not firing (check program ID)
        'raydium_events_received':      token_detector._raydium_events_received,
        'raydium_instruction_stats':    dict(
            sorted(token_detector._raydium_instruction_stats.items(), key=lambda x: -x[1])
        ),
        'raydium_cpmm_events_received': token_detector._raydium_cpmm_events_received,
        'raydium_cpmm_instruction_stats': dict(
            sorted(token_detector._raydium_cpmm_instruction_stats.items(), key=lambda x: -x[1])
        ),
    })


@app.route('/api/debug/outcomes', methods=['GET'])
def debug_outcomes():
    """
    AI training dataset progress.
    Shows how many tokens have each outcome checkpoint recorded,
    so you know when there's enough labelled data to train a model.
    """
    if not db:
        return error_response('Database not available', 503)

    rows = db._conn.execute("""
        SELECT
            COUNT(*)                                            AS total_tokens,
            SUM(CASE WHEN outcome_price_1h  IS NOT NULL THEN 1 ELSE 0 END) AS with_1h,
            SUM(CASE WHEN outcome_price_6h  IS NOT NULL THEN 1 ELSE 0 END) AS with_6h,
            SUM(CASE WHEN outcome_price_24h IS NOT NULL THEN 1 ELSE 0 END) AS with_24h,
            SUM(CASE WHEN outcome_complete  = 1         THEN 1 ELSE 0 END) AS complete,

            -- tokens that 2x'd within 1h
            SUM(CASE WHEN outcome_price_1h IS NOT NULL
                      AND price_usd > 0
                      AND outcome_price_1h / price_usd >= 2.0 THEN 1 ELSE 0 END) AS gain_2x_1h,
            -- tokens that 5x'd within 24h
            SUM(CASE WHEN outcome_price_24h IS NOT NULL
                      AND price_usd > 0
                      AND outcome_price_24h / price_usd >= 5.0 THEN 1 ELSE 0 END) AS gain_5x_24h,
            -- tokens that lost >80% by 24h (likely rugged)
            SUM(CASE WHEN outcome_price_24h IS NOT NULL
                      AND price_usd > 0
                      AND outcome_price_24h / price_usd <= 0.2 THEN 1 ELSE 0 END) AS loss_80pct_24h,

            -- average best gain across completed tokens
            AVG(outcome_max_gain_pct)                          AS avg_max_gain_pct,
            MAX(outcome_max_gain_pct)                          AS best_gain_ever_pct
        FROM detected_tokens
    """).fetchone()

    r = dict(rows)

    # Outcome tracker thread status
    outcome_thread_alive = (
        hasattr(token_detector, '_outcome_thread') and
        token_detector._outcome_thread is not None and
        token_detector._outcome_thread.is_alive()
    )

    # Tokens that need a checkpoint RIGHT NOW (deadline passed, not yet recorded).
    # Excludes tokens that have all due checkpoints recorded but are still waiting
    # for the 24h mark — those are in-progress, not pending.
    pending = db._conn.execute("""
        SELECT COUNT(*) AS n FROM detected_tokens
        WHERE outcome_complete = 0
          AND token_mint IS NOT NULL
          AND (
              (outcome_price_1h  IS NULL AND detected_at <= datetime('now', '-1 hours'))
           OR (outcome_price_6h  IS NULL AND detected_at <= datetime('now', '-6 hours'))
           OR (outcome_price_24h IS NULL AND detected_at <= datetime('now', '-24 hours'))
          )
    """).fetchone()['n']

    return success_response({
        'outcome_tracker_running': outcome_thread_alive,
        'check_interval_seconds':  getattr(token_detector, '_outcome_check_interval', 300),
        'dataset': {
            'total_tokens':     r['total_tokens'],
            'with_1h_outcome':  r['with_1h'],
            'with_6h_outcome':  r['with_6h'],
            'with_24h_outcome': r['with_24h'],
            'fully_complete':   r['complete'],
            'pending_checks':   pending,
        },
        'label_distribution': {
            'gain_2x_at_1h':      r['gain_2x_1h'],
            'gain_5x_at_24h':     r['gain_5x_24h'],
            'loss_80pct_at_24h':  r['loss_80pct_24h'],
        },
        'stats': {
            'avg_max_gain_pct':   round(r['avg_max_gain_pct'] or 0, 2),
            'best_gain_ever_pct': round(r['best_gain_ever_pct'] or 0, 2),
        },
        'readiness': {
            '1h_model_ready':  (r['with_1h'] or 0) >= 500,
            '24h_model_ready': (r['with_24h'] or 0) >= 2000,
            'note': (
                'Ready to train 1h model' if (r['with_1h'] or 0) >= 500
                else f"Need {500 - (r['with_1h'] or 0)} more 1h checkpoints"
            ),
        }
    })


@app.route('/api/debug/clear-dead-outcomes', methods=['POST'])
def debug_clear_dead_outcomes():
    """
    Bulk-mark all old tokens with unavailable prices as dead (price=0).
    Runs directly in the DB — no DexScreener calls, completes instantly.
    Useful to clear the backlog when most old tokens are rugged.
    """
    if not db:
        return error_response('Database not available', 503)

    now_iso = datetime.utcnow().isoformat()

    # Fill 1h checkpoint for tokens >2h old (1h deadline + 1h grace)
    r1 = db._conn.execute("""
        UPDATE detected_tokens
        SET outcome_price_1h     = 0.0,
            outcome_max_gain_pct = CASE WHEN price_usd > 0 THEN -100.0 ELSE 0.0 END
        WHERE outcome_price_1h IS NULL
          AND detected_at <= datetime('now', '-2 hours')
    """)

    # Fill 6h checkpoint for tokens >8h old (6h + 2h grace)
    r6 = db._conn.execute("""
        UPDATE detected_tokens
        SET outcome_price_6h = 0.0
        WHERE outcome_price_6h IS NULL
          AND detected_at <= datetime('now', '-8 hours')
    """)

    # Fill 24h checkpoint + mark complete for tokens >28h old (24h + 4h grace)
    r24 = db._conn.execute("""
        UPDATE detected_tokens
        SET outcome_price_24h = 0.0,
            outcome_complete  = 1
        WHERE outcome_price_24h IS NULL
          AND detected_at <= datetime('now', '-28 hours')
    """)

    db._conn.commit()

    return success_response({
        'filled_1h':  r1.rowcount,
        'filled_6h':  r6.rowcount,
        'filled_24h': r24.rowcount,
        'note': 'Dead tokens marked with price=0 (rugged label)'
    })


# ===========================
# HEALTH & STATUS ENDPOINTS
# ===========================

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint to verify API is running"""
    return success_response({
        'status': 'operational',
        'timestamp': datetime.utcnow().isoformat(),
        'version': '1.0.0'
    })


@app.route('/api/status', methods=['GET'])
def get_status():
    """Get overall system status"""
    return success_response({
        'modules': {
            'wallet_manager': wallet_manager.get_status(),
            'copy_trading': copy_trading_engine.get_status(),
            'sniper': sniper_engine.get_status(),
            'anti_scam': anti_scam_analyzer.get_status(),
            'websocket': websocket_manager.get_status()
        },
        'active_wallet': wallet_manager.get_active_wallet(),
        'network': config.get('solana.network', 'devnet'),
        'rpc_url': config.get('solana.rpc_url', ''),
        'timestamp': datetime.utcnow().isoformat()
    })


# ===========================
# WALLET MANAGEMENT ENDPOINTS
# ===========================

@app.route('/api/wallets', methods=['GET'])
def get_wallets():
    """Get all wallets (without private keys)"""
    wallets = wallet_manager.get_all_wallets()
    return success_response(wallets)


@app.route('/api/wallets', methods=['POST'])
def create_wallet():
    """Create a new internal wallet"""
    data = request.get_json()
    name = data.get('name', 'Wallet')

    wallet = wallet_manager.create_wallet(name)
    return success_response(wallet, status_code=201)


@app.route('/api/wallets/import', methods=['POST'])
def import_wallet():
    """Import an external wallet"""
    data = request.get_json()

    wallet = wallet_manager.import_wallet(
        private_key=data.get('private_key'),
        seed_phrase=data.get('seed_phrase'),
        name=data.get('name', 'Imported Wallet')
    )
    return success_response(wallet, status_code=201)


@app.route('/api/wallets/<wallet_id>', methods=['GET'])
def get_wallet(wallet_id):
    """Get specific wallet details"""
    wallet = wallet_manager.get_wallet(wallet_id)
    if wallet:
        return success_response(wallet)
    return error_response('Wallet not found', status_code=404)


@app.route('/api/wallets/<wallet_id>', methods=['DELETE'])
def delete_wallet(wallet_id):
    """Delete a wallet"""
    success = wallet_manager.delete_wallet(wallet_id)
    if success:
        return success_response({'message': 'Wallet deleted successfully'})
    return error_response('Wallet not found', status_code=404)


@app.route('/api/wallets/<wallet_id>/activate', methods=['POST'])
def activate_wallet(wallet_id):
    """Set a wallet as active"""
    success = wallet_manager.set_active_wallet(wallet_id)
    if success:
        return success_response({'message': 'Wallet activated', 'wallet_id': wallet_id})
    return error_response('Wallet not found', status_code=404)


@app.route('/api/wallets/<wallet_id>/balance', methods=['GET'])
def get_wallet_balance(wallet_id):
    """Get wallet balance with live SOL/USD conversion."""
    balance = wallet_manager.get_balance(wallet_id)
    if balance is not None:
        # Enrich with live SOL price
        sol_price = price_service.get_sol_price()
        balance['usd'] = round(balance.get('sol', 0.0) * sol_price, 4)
        balance['sol_price_usd'] = sol_price
        # Add USD values for token balances
        for token in balance.get('tokens', []):
            token_price = price_service.get_token_price_usd(token.get('mint', ''))
            token['usd_value'] = round(token.get('ui_amount', 0.0) * (token_price or 0.0), 4)
        return success_response(balance)
    return error_response('Wallet not found', status_code=404)


@app.route('/api/wallets/<wallet_id>/airdrop', methods=['POST'])
def request_airdrop(wallet_id):
    """Request SOL airdrop on devnet (for testing)"""
    data = request.get_json() or {}
    amount = data.get('amount', 1.0)

    wallet = wallet_manager.get_wallet(wallet_id)
    if not wallet:
        return error_response('Wallet not found', status_code=404)

    try:
        signature = wallet_manager.wallet_ops.request_airdrop_devnet(
            wallet['address'],
            amount
        )

        return success_response({
            'signature': signature,
            'amount': amount,
            'address': wallet['address'],
            'message': (
                f'Airdrop of {amount} SOL requested. '
                'Balance will update once the network confirms the transaction '
                f'(sig: {signature[:20]}...)'
            )
        })
    except Exception as e:
        error_msg = str(e)

        # Provide helpful error messages based on the error
        if 'Internal error' in error_msg or '-32603' in error_msg:
            return error_response(
                'Devnet airdrop service is currently down. Please use https://faucet.solana.com (login with GitHub) to manually request SOL.',
                status_code=503
            )
        elif 'rate' in error_msg.lower() or 'limit' in error_msg.lower() or '429' in error_msg:
            return error_response(
                'Rate limited. Please use https://faucet.solana.com (login with GitHub) for higher limits.',
                status_code=429
            )
        else:
            return error_response(f'Airdrop failed: {error_msg}', status_code=500)


@app.route('/api/wallets/assignments', methods=['GET'])
def get_wallet_assignments():
    """Get per-module wallet assignments, enriched with wallet info."""
    assignments = wallet_manager.get_module_assignments()
    wallets     = [wallet_manager._sanitize_wallet(w) for w in wallet_manager.wallets.values()]
    return success_response({'assignments': assignments, 'wallets': wallets})


@app.route('/api/wallets/assignments', methods=['PUT'])
def set_wallet_assignment():
    """
    Assign a wallet to a module (or clear the assignment).

    Request body: { "module": "sniper", "wallet_id": "uuid" | null }
    """
    data      = request.get_json() or {}
    module    = data.get('module')
    wallet_id = data.get('wallet_id')   # None = revert to active wallet

    if not module:
        return error_response('module is required', status_code=400)

    success = wallet_manager.set_module_assignment(module, wallet_id)
    if not success:
        return error_response('Invalid module or wallet not found', status_code=400)

    return success_response(wallet_manager.get_module_assignments())


# ===========================
# TRANSACTION ENDPOINTS
# ===========================

@app.route('/api/transactions/transfer-sol', methods=['POST'])
@limiter.limit("10 per minute")
def transfer_sol():
    """
    Transfer SOL from one wallet to another.

    Request body:
    {
        "from_wallet_id": "wallet-id",
        "to_address": "recipient-address",
        "amount": 0.1,
        "simulate_only": false (optional)
    }
    """
    data = request.get_json()

    # Validate required fields
    from_wallet_id = data.get('from_wallet_id')
    to_address = data.get('to_address')
    amount = data.get('amount')
    simulate_only = data.get('simulate_only', False)

    if not from_wallet_id or not to_address or amount is None:
        return error_response(
            'from_wallet_id, to_address, and amount are required',
            status_code=400
        )

    # Validate amount
    if amount <= 0:
        return error_response('Amount must be greater than 0', status_code=400)

    # Get wallet from storage
    wallet_data = wallet_manager.wallets.get(from_wallet_id)
    if not wallet_data:
        return error_response('Wallet not found', status_code=404)

    try:
        # Decrypt private key
        encrypted_private_key = wallet_data.get('private_key_encrypted')
        if not encrypted_private_key:
            return error_response('Wallet has no private key (external wallet?)', status_code=400)

        private_key = wallet_manager.crypto_manager.decrypt(encrypted_private_key)

        # Execute transfer
        result = wallet_manager.wallet_ops.send_sol(
            from_private_key=private_key,
            to_address=to_address,
            amount_sol=amount,
            simulate_only=simulate_only
        )

        if result.get('success'):
            # Log the transaction
            logging_engine.log(
                level='info',
                module='transactions',
                message=f"SOL transfer: {amount} SOL from {wallet_data['address']} to {to_address}",
                data=result
            )

            return success_response(result)
        else:
            return error_response(
                result.get('error', 'Transaction failed'),
                status_code=400
            )

    except Exception as e:
        return error_response(f'Transaction failed: {str(e)}', status_code=500)


@app.route('/api/transactions/swap', methods=['POST'])
@limiter.limit("10 per minute")
def swap_tokens():
    """
    Swap tokens using Jupiter Aggregator.

    Request body:
    {
        "wallet_id": "wallet-id",
        "input_mint": "token-mint-address",
        "output_mint": "token-mint-address",
        "amount": 1.0,
        "slippage_bps": 50 (optional, default 50 = 0.5%),
        "simulate_only": false (optional)
    }
    """
    data = request.get_json()

    # Validate required fields
    wallet_id = data.get('wallet_id')
    input_mint = data.get('input_mint')
    output_mint = data.get('output_mint')
    amount = data.get('amount')
    slippage_bps = data.get('slippage_bps', 50)
    simulate_only = data.get('simulate_only', False)

    if not all([wallet_id, input_mint, output_mint]) or amount is None:
        return error_response(
            'wallet_id, input_mint, output_mint, and amount are required',
            status_code=400
        )

    # Validate amount
    if amount <= 0:
        return error_response('Amount must be greater than 0', status_code=400)

    # Get wallet from storage
    wallet_data = wallet_manager.wallets.get(wallet_id)
    if not wallet_data:
        return error_response('Wallet not found', status_code=404)

    try:
        # Decrypt private key
        encrypted_private_key = wallet_data.get('private_key_encrypted')
        if not encrypted_private_key:
            return error_response('Wallet has no private key (external wallet?)', status_code=400)

        private_key = wallet_manager.crypto_manager.decrypt(encrypted_private_key)

        # Execute swap
        result = wallet_manager.wallet_ops.swap_tokens(
            from_private_key=private_key,
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            slippage_bps=slippage_bps,
            simulate_only=simulate_only
        )

        if result.get('success'):
            # Log the transaction
            logging_engine.log(
                level='info',
                module='transactions',
                message=f"Token swap: {amount} {input_mint} -> {output_mint}",
                data=result
            )

            return success_response(result)
        else:
            return error_response(
                result.get('error', 'Swap failed'),
                status_code=400
            )

    except Exception as e:
        return error_response(f'Swap failed: {str(e)}', status_code=500)


@app.route('/api/transactions/history/<wallet_id>', methods=['GET'])
def get_transaction_history(wallet_id):
    """
    Get recent transaction history for a wallet.

    Query params:
    - limit: Maximum number of transactions (default: 10)
    """
    limit = request.args.get('limit', 10, type=int)

    # Get wallet from storage
    wallet_data = wallet_manager.wallets.get(wallet_id)
    if not wallet_data:
        return error_response('Wallet not found', status_code=404)

    try:
        transactions = wallet_manager.wallet_ops.get_recent_transactions(
            wallet_data['address'],
            limit=limit
        )

        return success_response({
            'address': wallet_data['address'],
            'transactions': transactions,
            'count': len(transactions)
        })

    except Exception as e:
        return error_response(f'Failed to get transaction history: {str(e)}', status_code=500)


# ===========================
# WEBSOCKET ENDPOINTS
# ===========================

@app.route('/api/websocket/status', methods=['GET'])
def get_websocket_status():
    """Get WebSocket connection status"""
    return success_response(websocket_manager.get_status())


@app.route('/api/websocket/start', methods=['POST'])
def start_websocket():
    """Start WebSocket listener"""
    try:
        websocket_manager.start()
        return success_response({
            'message': 'WebSocket listener started',
            'status': websocket_manager.get_status()
        })
    except Exception as e:
        return error_response(f'Failed to start WebSocket: {str(e)}', status_code=500)


@app.route('/api/websocket/stop', methods=['POST'])
def stop_websocket():
    """Stop WebSocket listener and cascade-stop dependent engines."""
    try:
        # Stop engines that depend on the WebSocket before tearing down the connection
        if sniper_engine.is_running:
            sniper_engine.stop()
        if copy_trading_engine.is_running:
            copy_trading_engine.stop()

        websocket_manager.stop()
        return success_response({
            'message': 'WebSocket listener stopped (Sniper and Copy Trading also stopped)'
        })
    except Exception as e:
        return error_response(f'Failed to stop WebSocket: {str(e)}', status_code=500)


@app.route('/api/websocket/subscriptions', methods=['GET'])
def get_websocket_subscriptions():
    """Get all active WebSocket subscriptions (JSON-safe, no callback functions)."""
    raw = websocket_manager.get_subscriptions()
    safe = {}
    for sub_id, info in raw.items():
        safe[str(sub_id)] = {
            'type': info.get('type'),
            'mentions': info.get('mentions'),
            'address': info.get('address'),
            'program_id': info.get('program_id'),
            'created_at': info.get('created_at'),
            'has_callback': info.get('callback') is not None,
        }
    return success_response({
        'subscriptions': safe,
        'count': len(safe)
    })


@app.route('/api/websocket/subscribe/account', methods=['POST'])
def subscribe_to_account():
    """
    Subscribe to account changes.

    Request body:
    {
        "account_address": "wallet-address"
    }
    """
    data = request.get_json()
    account_address = data.get('account_address')

    if not account_address:
        return error_response('account_address is required', status_code=400)

    try:
        subscription_id = websocket_manager.subscribe_account(account_address)

        if subscription_id:
            return success_response({
                'subscription_id': subscription_id,
                'account_address': account_address,
                'message': 'Subscribed to account updates'
            })
        else:
            return error_response('Failed to create subscription', status_code=500)

    except Exception as e:
        return error_response(f'Subscription failed: {str(e)}', status_code=500)


@app.route('/api/websocket/subscribe/program', methods=['POST'])
def subscribe_to_program():
    """
    Subscribe to program account changes.

    Request body:
    {
        "program_id": "program-address"
    }
    """
    data = request.get_json()
    program_id = data.get('program_id')

    if not program_id:
        return error_response('program_id is required', status_code=400)

    try:
        subscription_id = websocket_manager.subscribe_program(program_id)

        if subscription_id:
            return success_response({
                'subscription_id': subscription_id,
                'program_id': program_id,
                'message': 'Subscribed to program updates'
            })
        else:
            return error_response('Failed to create subscription', status_code=500)

    except Exception as e:
        return error_response(f'Subscription failed: {str(e)}', status_code=500)


@app.route('/api/websocket/subscribe/logs', methods=['POST'])
def subscribe_to_logs():
    """
    Subscribe to transaction logs.

    Request body:
    {
        "mentions": ["address1", "address2"] (optional)
    }
    """
    data = request.get_json() or {}
    mentions = data.get('mentions')

    try:
        subscription_id = websocket_manager.subscribe_logs(mentions)

        if subscription_id:
            return success_response({
                'subscription_id': subscription_id,
                'mentions': mentions,
                'message': 'Subscribed to transaction logs'
            })
        else:
            return error_response('Failed to create subscription', status_code=500)

    except Exception as e:
        return error_response(f'Subscription failed: {str(e)}', status_code=500)


@app.route('/api/websocket/unsubscribe/<int:subscription_id>', methods=['POST'])
def unsubscribe_websocket(subscription_id):
    """Unsubscribe from a WebSocket subscription"""
    try:
        websocket_manager.unsubscribe(subscription_id)
        return success_response({
            'message': f'Unsubscribed from subscription {subscription_id}'
        })
    except Exception as e:
        return error_response(f'Unsubscribe failed: {str(e)}', status_code=500)


@app.route('/api/websocket/events', methods=['GET'])
def get_websocket_events():
    """Return recent WebSocket events from the ring buffer (newest first)."""
    limit = min(int(request.args.get('limit', 20)), 50)
    events = websocket_manager.get_recent_events(limit=limit)
    return success_response({
        'events': events,
        'count': len(events),
    })


# ===========================
# COPY TRADING ENDPOINTS
# ===========================

@app.route('/api/copy-trading/rules', methods=['GET'])
def get_copy_trading_rules():
    """Get all copy trading rules"""
    rules = copy_trading_engine.get_all_rules()
    return success_response(rules)


@app.route('/api/copy-trading/rules', methods=['POST'])
def create_copy_trading_rule():
    """Create a new copy trading rule"""
    data = request.get_json()
    rule = copy_trading_engine.create_rule(data)
    return success_response(rule, status_code=201)


@app.route('/api/copy-trading/rules/<rule_id>', methods=['GET'])
def get_copy_trading_rule(rule_id):
    """Get specific copy trading rule"""
    rule = copy_trading_engine.get_rule(rule_id)
    if rule:
        return success_response(rule)
    return error_response('Rule not found', status_code=404)


@app.route('/api/copy-trading/rules/<rule_id>', methods=['PUT'])
def update_copy_trading_rule(rule_id):
    """Update a copy trading rule"""
    data = request.get_json()
    rule = copy_trading_engine.update_rule(rule_id, data)
    if rule:
        return success_response(rule)
    return error_response('Rule not found', status_code=404)


@app.route('/api/copy-trading/rules/<rule_id>', methods=['DELETE'])
def delete_copy_trading_rule(rule_id):
    """Delete a copy trading rule"""
    success = copy_trading_engine.delete_rule(rule_id)
    if success:
        return success_response({'message': 'Rule deleted successfully'})
    return error_response('Rule not found', status_code=404)


@app.route('/api/copy-trading/rules/<rule_id>/toggle', methods=['POST'])
def toggle_copy_trading_rule(rule_id):
    """Enable/disable a copy trading rule"""
    success = copy_trading_engine.toggle_rule(rule_id)
    if success:
        return success_response({'message': 'Rule toggled successfully'})
    return error_response('Rule not found', status_code=404)


@app.route('/api/copy-trading/monitored-wallets', methods=['GET'])
def get_monitored_wallets():
    """Get all monitored wallets"""
    wallets = copy_trading_engine.get_monitored_wallets()
    return success_response(wallets)


@app.route('/api/copy-trading/monitored-wallets', methods=['POST'])
def add_monitored_wallet():
    """Add a wallet to monitor"""
    data = request.get_json()
    wallet = copy_trading_engine.add_monitored_wallet(
        address=data.get('address'),
        name=data.get('name', 'Unknown'),
        rules=data.get('rules', [])
    )
    return success_response(wallet, status_code=201)


@app.route('/api/copy-trading/monitored-wallets/<wallet_address>', methods=['DELETE'])
def remove_monitored_wallet(wallet_address):
    """Remove a monitored wallet"""
    success = copy_trading_engine.remove_monitored_wallet(wallet_address)
    if success:
        return success_response({'message': 'Monitored wallet removed'})
    return error_response('Wallet not found', status_code=404)


@app.route('/api/copy-trading/history', methods=['GET'])
def get_copy_trading_history():
    """Get copy trading operation history"""
    limit = request.args.get('limit', 50, type=int)
    history = copy_trading_engine.get_history(limit)
    return success_response(history)


# --- New wallet-centric copy trading endpoints ---

@app.route('/api/copy-trading/status', methods=['GET'])
def get_copy_trading_status():
    """Get copy trading engine status and today's trade count."""
    return success_response(copy_trading_engine.get_status())


@app.route('/api/copy-trading/start', methods=['POST'])
def start_copy_trading():
    """Start the copy trading engine."""
    success = copy_trading_engine.start()
    return success_response({'running': success})


@app.route('/api/copy-trading/stop', methods=['POST'])
def stop_copy_trading():
    """Stop the copy trading engine."""
    success = copy_trading_engine.stop()
    return success_response({'running': False})


@app.route('/api/copy-trading/wallets', methods=['GET'])
def get_ct_wallets():
    """Get all monitored wallets with their full configuration."""
    return success_response(copy_trading_engine.get_all_wallets())


@app.route('/api/copy-trading/wallets', methods=['POST'])
def add_ct_wallet():
    """Add a wallet to monitor with full configuration."""
    data = request.get_json() or {}
    try:
        wallet = copy_trading_engine.add_wallet(data)
        return success_response(wallet, status_code=201)
    except ValueError as e:
        return error_response(str(e), status_code=400)


@app.route('/api/copy-trading/wallets/<address>', methods=['PUT'])
def update_ct_wallet(address):
    """Update a monitored wallet configuration."""
    data   = request.get_json() or {}
    wallet = copy_trading_engine.update_wallet(address, data)
    if wallet:
        return success_response(wallet)
    return error_response('Wallet not found', status_code=404)


@app.route('/api/copy-trading/wallets/<address>', methods=['DELETE'])
def remove_ct_wallet(address):
    """Remove a monitored wallet."""
    success = copy_trading_engine.remove_wallet(address)
    if success:
        return success_response({'message': 'Wallet removed'})
    return error_response('Wallet not found', status_code=404)


@app.route('/api/copy-trading/wallets/<address>/toggle', methods=['POST'])
def toggle_ct_wallet(address):
    """Enable or disable a monitored wallet."""
    success = copy_trading_engine.toggle_rule(address)
    if success:
        return success_response(copy_trading_engine.get_wallet(address))
    return error_response('Wallet not found', status_code=404)


@app.route('/api/copy-trading/sim/positions', methods=['GET'])
def get_ct_sim_positions():
    """Get copy trading simulation positions."""
    status_filter = request.args.get('status')
    limit         = request.args.get('limit', 200, type=int)
    positions     = copy_trading_engine.get_sim_positions(status_filter, limit)
    stats         = copy_trading_engine.get_sim_stats()
    return success_response({'positions': positions, 'stats': stats})


@app.route('/api/copy-trading/sim/refresh', methods=['POST'])
def refresh_ct_sim_prices():
    """Force a price refresh on all open copy trading sim positions."""
    copy_trading_engine.refresh_sim_prices()
    positions = copy_trading_engine.get_sim_positions()
    stats     = copy_trading_engine.get_sim_stats()
    return success_response({'positions': positions, 'stats': stats})


@app.route('/api/copy-trading/sim/reset', methods=['POST'])
def reset_ct_sim():
    """Clear all copy trading simulation positions."""
    copy_trading_engine.reset_sim_positions()
    return success_response({'message': 'Simulation reset'})


@app.route('/api/copy-trading/sim/positions/<position_id>/close', methods=['POST'])
def close_ct_sim_position(position_id):
    """Manually close a single open simulation position."""
    success = copy_trading_engine.close_sim_position(position_id)
    if success:
        return success_response({'message': 'Position closed'})
    return error_response('Position not found or already closed', status_code=404)


# ===========================
# SNIPER BOT ENDPOINTS
# ===========================

@app.route('/api/sniper/config', methods=['GET'])
def get_sniper_config():
    """Get current sniper configuration"""
    config = sniper_engine.get_config()
    return success_response(config)


@app.route('/api/sniper/config', methods=['PUT'])
def update_sniper_config():
    """Update sniper configuration"""
    data = request.get_json()
    config = sniper_engine.update_config(data)
    return success_response(config)


@app.route('/api/sniper/start', methods=['POST'])
def start_sniper():
    """Start the sniper bot"""
    success = sniper_engine.start()
    if success:
        return success_response({'message': 'Sniper bot started', 'status': 'running'})
    return error_response('Failed to start sniper bot', status_code=500)


@app.route('/api/sniper/stop', methods=['POST'])
def stop_sniper():
    """Stop the sniper bot"""
    success = sniper_engine.stop()
    if success:
        return success_response({'message': 'Sniper bot stopped', 'status': 'stopped'})
    return error_response('Failed to stop sniper bot', status_code=500)


@app.route('/api/sniper/detected-tokens', methods=['GET'])
def get_detected_tokens():
    """Get recently detected tokens, optionally filtered by status."""
    limit         = request.args.get('limit', 50, type=int)
    status_filter = request.args.get('status', None)
    tokens = sniper_engine.get_detected_tokens(limit, status_filter=status_filter)
    return success_response(tokens)


@app.route('/api/sniper/token/<string:mint>', methods=['GET'])
def get_token_detail(mint):
    """Get full detail for a single token including price history."""
    token = sniper_engine.get_token_detail(mint)
    if not token:
        return error_response('Token not found', status_code=404)
    return success_response(token)


@app.route('/api/sniper/history', methods=['GET'])
def get_sniper_history():
    """Get sniper action history (tokens that passed filters)."""
    limit = request.args.get('limit', 50, type=int)
    history = sniper_engine.get_history(limit)
    return success_response(history)


@app.route('/api/sniper/simulation/positions', methods=['GET'])
def get_sim_positions():
    """Get all simulation positions with current P&L."""
    status = request.args.get('status', None)
    limit  = request.args.get('limit', 200, type=int)
    return success_response(sniper_engine.get_sim_positions(status, limit))


@app.route('/api/sniper/simulation/stats', methods=['GET'])
def get_sim_stats():
    """Get aggregate simulation statistics."""
    return success_response(sniper_engine.get_sim_stats())


@app.route('/api/sniper/simulation/reset', methods=['POST'])
def reset_sim_positions():
    """Clear all simulation positions to start fresh."""
    sniper_engine.reset_sim_positions()
    return success_response({'message': 'Simulation reset successfully'})


@app.route('/api/sniper/simulation/refresh-prices', methods=['POST'])
def refresh_sim_prices():
    """Force-refresh current prices and P&L for all open simulation positions."""
    sniper_engine.refresh_sim_prices()
    stats     = sniper_engine.get_sim_stats()
    positions = sniper_engine.get_sim_positions()
    return success_response({'stats': stats, 'positions': positions})


# ===========================
# ANTI-SCAM ANALYZER ENDPOINTS
# ===========================

@app.route('/api/anti-scam/analyze', methods=['POST'])
@limiter.limit("30 per minute")
def analyze_token():
    """Analyze a token for scam indicators"""
    data = request.get_json()
    token_address = data.get('token_address')

    if not token_address:
        return error_response('token_address is required', status_code=400)

    analysis = anti_scam_analyzer.analyze_token(token_address)
    return success_response(analysis)


@app.route('/api/anti-scam/rules', methods=['GET'])
def get_anti_scam_rules():
    """Get all anti-scam rules"""
    rules = anti_scam_analyzer.get_rules()
    return success_response(rules)


@app.route('/api/anti-scam/rules', methods=['PUT'])
def update_anti_scam_rules():
    """Update anti-scam rules"""
    data = request.get_json()
    rules = anti_scam_analyzer.update_rules(data)
    return success_response(rules)


@app.route('/api/anti-scam/config', methods=['GET'])
def get_anti_scam_config():
    """Get anti-scam module configuration (checks, thresholds, penalties)"""
    cfg = anti_scam_analyzer.get_config()
    return success_response(cfg)


@app.route('/api/anti-scam/config', methods=['PUT'])
def update_anti_scam_config():
    """Update anti-scam module configuration"""
    data = request.get_json()
    cfg = anti_scam_analyzer.update_config(data)
    return success_response(cfg)


@app.route('/api/anti-scam/blacklist', methods=['GET'])
def get_blacklist():
    """Get blacklisted tokens/wallets"""
    blacklist = anti_scam_analyzer.get_blacklist()
    return success_response(blacklist)


@app.route('/api/anti-scam/blacklist', methods=['POST'])
def add_to_blacklist():
    """Add token/wallet to blacklist"""
    data = request.get_json()
    success = anti_scam_analyzer.add_to_blacklist(
        address=data.get('address'),
        reason=data.get('reason', 'Manual blacklist'),
        type_=data.get('type', 'token')
    )
    return success_response({'message': 'Added to blacklist'}, status_code=201)


@app.route('/api/anti-scam/blacklist/<address>', methods=['DELETE'])
def remove_from_blacklist(address):
    """Remove from blacklist"""
    success = anti_scam_analyzer.remove_from_blacklist(address)
    if success:
        return success_response({'message': 'Removed from blacklist'})
    return error_response('Address not found in blacklist', status_code=404)


# ===========================
# AI ANALYZER ENDPOINTS
# ===========================

@app.route('/api/ai/analyze-token', methods=['POST'])
def ai_analyze_token():
    """Analyze token using AI models"""
    data = request.get_json()
    token_address = data.get('token_address')

    analysis = ai_analyzer.analyze_token(token_address)
    return success_response(analysis)


@app.route('/api/ai/analyze-wallet', methods=['POST'])
def ai_analyze_wallet():
    """Analyze wallet behavior using AI"""
    data = request.get_json()
    wallet_address = data.get('wallet_address')

    analysis = ai_analyzer.analyze_wallet(wallet_address)
    return success_response(analysis)


@app.route('/api/ai/status', methods=['GET'])
def ai_status():
    """Return AI analyzer status, including ML model info."""
    return success_response(ai_analyzer.get_status())


@app.route('/api/ai/retrain', methods=['POST'])
def ai_retrain():
    """
    Trigger a manual model retraining in the background.
    Returns 409 if training is already running.
    """
    started = model_trainer.start_training(
        on_complete=lambda _: ai_analyzer.reload_model()
    )
    if not started:
        return error_response("Training already in progress", status_code=409)
    return success_response({"message": "Training started", "status": model_trainer.get_status()})


@app.route('/api/ai/retrain/status', methods=['GET'])
def ai_retrain_status():
    """Return current trainer status (is_training, last_trained, last_result)."""
    return success_response(model_trainer.get_status())


@app.route('/api/sniper/config/ml', methods=['GET'])
def get_sniper_ml_config():
    """Return just the ML section of the sniper config."""
    cfg = sniper_engine.sniper_config.get('ml', {'enabled': False, 'min_pump_score': 0})
    return success_response(cfg)


@app.route('/api/sniper/config/ml', methods=['PUT'])
def update_sniper_ml_config():
    """Update the ML section of the sniper config."""
    data = request.get_json() or {}
    ml = sniper_engine.sniper_config.get('ml', {})
    if 'enabled' in data:
        ml['enabled'] = bool(data['enabled'])
    if 'min_pump_score' in data:
        ml['min_pump_score'] = max(0, min(100, int(data['min_pump_score'])))
    sniper_engine.sniper_config['ml'] = ml
    sniper_engine._save_config()
    return success_response(ml)


# ===========================
# LOGGING ENDPOINTS
# ===========================

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """Get system logs"""
    level = request.args.get('level', 'all')
    limit = request.args.get('limit', 100, type=int)
    module = request.args.get('module', None)

    logs = logging_engine.get_logs(level=level, limit=limit, module=module)
    return success_response(logs)


@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear today's logs (archives are preserved)."""
    success = logging_engine.clear_logs()
    if success:
        return success_response({'message': 'Logs cleared'})
    return error_response('Failed to clear logs', status_code=500)


# ===========================
# METRICS ENDPOINTS
# ===========================

@app.route('/api/metrics/overview', methods=['GET'])
def get_metrics_overview():
    """Get overall metrics"""
    metrics = {
        'copy_trading': copy_trading_engine.get_metrics(),
        'sniper': sniper_engine.get_metrics(),
        'wallets': wallet_manager.get_metrics()
    }
    return success_response(metrics)


# ===========================
# PRICE ENDPOINTS
# ===========================

@app.route('/api/prices/sol', methods=['GET'])
@limiter.limit("60 per minute")
def get_sol_price():
    """Get current SOL/USD price from CoinGecko."""
    price = price_service.get_sol_price()
    return success_response({'sol_usd': price})


@app.route('/api/prices/token/<mint_address>', methods=['GET'])
@limiter.limit("30 per minute")
def get_token_price(mint_address):
    """Get USD price for an SPL token by mint address."""
    price = price_service.get_token_price_usd(mint_address)
    if price is not None:
        return success_response({'mint': mint_address, 'usd': price})
    return error_response('Price not available for this token', status_code=404)


@app.route('/api/prices/convert', methods=['GET'])
def convert_sol():
    """
    Convert SOL amount to USD.
    Query params: amount (float)
    """
    amount = request.args.get('amount', 0.0, type=float)
    usd = price_service.convert_sol_to_usd(amount)
    return success_response({'sol': amount, 'usd': round(usd, 4)})


# ===========================
# NOTIFICATION ENDPOINTS
# ===========================

@app.route('/api/notifications/status', methods=['GET'])
def get_notification_status():
    """Get notification system status and channel configuration."""
    return success_response(notification_manager.get_status())


@app.route('/api/notifications/configure', methods=['POST'])
def configure_notifications():
    """
    Configure notification channels at runtime.

    Request body (all fields optional):
    {
        "telegram_bot_token": "...",
        "telegram_chat_id": "...",
        "discord_webhook_url": "https://...",
        "enabled": true
    }
    """
    data = request.get_json() or {}

    notification_manager.configure(
        telegram_token=data.get('telegram_bot_token'),
        telegram_chat_id=data.get('telegram_chat_id'),
        discord_webhook_url=data.get('discord_webhook_url'),
        enabled=data.get('enabled'),
    )
    return success_response({'message': 'Notification configuration updated'})


@app.route('/api/notifications/test', methods=['POST'])
def test_notification():
    """
    Send a test notification to verify channels are working.

    Request body (optional):
    {
        "channel": "all" | "telegram" | "discord"
    }
    """
    notification_manager.notify_custom(
        title='SolanaSentinel — Test Notification',
        message='If you receive this, your notification channels are configured correctly.',
        level='info',
    )
    return success_response({'message': 'Test notification dispatched'})


# ===========================
# ERROR HANDLERS
# ===========================

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors"""
    return error_response('Endpoint not found', status_code=404)


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    return error_response('Internal server error', status_code=500)


@app.errorhandler(Exception)
def handle_exception(error):
    """Handle all other exceptions"""
    app.logger.error(f'Unhandled exception: {str(error)}')
    return error_response(f'An error occurred: {str(error)}', status_code=500)


# ===========================
# SETTINGS ENDPOINTS
# ===========================

@app.route('/api/settings/rpc', methods=['GET'])
def get_rpc_settings():
    """Return the current Solana RPC / WebSocket configuration."""
    return success_response({
        'rpc_url':    config.get('solana.rpc_url', ''),
        'ws_url':     config.get('solana.ws_url',  ''),
        'network':    config.get('solana.network',    'mainnet-beta'),
        'commitment': config.get('solana.commitment', 'confirmed'),
    })


@app.route('/api/settings/rpc', methods=['PUT'])
def update_rpc_settings():
    """
    Update Solana RPC / WebSocket URLs and persist to settings.json.

    The running WebSocket manager is reconnected immediately so the new
    endpoint is used for wallet monitoring without a server restart.
    Sniper / copy-trading engines are restarted if they were running.
    """
    data = request.get_json() or {}

    rpc_url    = data.get('rpc_url',    '').strip()
    ws_url     = data.get('ws_url',     '').strip()
    network    = data.get('network',    config.get('solana.network',    'mainnet-beta'))
    commitment = data.get('commitment', config.get('solana.commitment', 'confirmed'))

    if not rpc_url or not ws_url:
        return error_response('rpc_url and ws_url are required', status_code=400)

    # Persist to settings.json
    config.set('solana.rpc_url',    rpc_url)
    config.set('solana.ws_url',     ws_url)
    config.set('solana.network',    network)
    config.set('solana.commitment', commitment)
    config.save_config()

    # Reconnect the WebSocket manager with the new URL
    was_sniper_running = sniper_engine.is_running
    was_ct_running     = copy_trading_engine.is_running

    try:
        if was_sniper_running:
            sniper_engine.stop()
        if was_ct_running:
            copy_trading_engine.stop()

        websocket_manager.stop()
        websocket_manager.url        = ws_url
        websocket_manager.commitment = commitment
        websocket_manager.start()

        # Update the HTTP RPC client URL so future calls use the new endpoint
        wallet_manager.rpc_client.rpc_url = rpc_url
        wallet_manager.rpc_client.client  = wallet_manager.rpc_client._create_client(rpc_url)

        if was_sniper_running:
            sniper_engine.start()
        if was_ct_running:
            copy_trading_engine.start()

        return success_response({
            'rpc_url':    rpc_url,
            'ws_url':     ws_url,
            'network':    network,
            'commitment': commitment,
            'restarted':  True,
        })
    except Exception as e:
        return error_response(f'Settings saved but reconnect failed: {e}', status_code=500)


# ===========================
# WEEKLY RETRAINING SCHEDULER
# ===========================

def _weekly_retrain_job():
    """
    APScheduler job: fires every week to retrain the ML model.
    Skipped silently if training is already running.
    After training completes the new model is hot-reloaded into
    ai_analyzer without restarting the backend process.
    """
    logging.getLogger(__name__).info(
        "[SCHEDULER] Weekly retrain job fired"
    )
    started = model_trainer.start_training(
        on_complete=lambda result: (
            ai_analyzer.reload_model()
            if result.get("success")
            else None
        )
    )
    if not started:
        logging.getLogger(__name__).warning(
            "[SCHEDULER] Weekly retrain skipped — training already running"
        )


try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func    = _weekly_retrain_job,
        trigger = IntervalTrigger(weeks=1),
        id      = "weekly_retrain",
        name    = "Weekly ML Retrain",
        replace_existing = True,
        misfire_grace_time = 3600,  # allow up to 1h late if backend was offline
    )
    _scheduler.start()
    print("[SCHEDULER] Weekly ML retrain scheduled (every 7 days)")
except ImportError:
    print("[SCHEDULER] apscheduler not installed — weekly retrain disabled. "
          "Run: pip install apscheduler")


# ===========================
# MAIN
# ===========================

if __name__ == '__main__':
    # Get configuration
    host = os.getenv('FLASK_HOST', '127.0.0.1')
    port = int(os.getenv('FLASK_PORT', 5005))
    debug = os.getenv('FLASK_DEBUG', 'True').lower() == 'true'

    print(f"""
    =========================================
         SolanaSentinel Backend API
    =========================================
      Status: Running
      Host: {host}
      Port: {port}
    =========================================
    """)

    # socketio.run() replaces app.run() — it starts the WebSocket server
    # alongside the regular HTTP server on the same port.
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
