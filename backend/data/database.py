"""
DatabaseManager
SQLite persistence layer for SolanaSentinel.

Replaces the growing JSON files for:
  - Detected tokens  (token_detector → detected_tokens table)
  - Sniper positions (sniper_engine  → sniper_positions table)
  - CT positions     (copy_trading   → ct_positions table)
  - CT history       (copy_trading   → ct_history table)

All token/evaluation data is stored comprehensively so it can be used
as a training dataset for future AI models (anti-scam scoring, entry
quality prediction, rug-pull pattern detection, etc.).
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any


class DatabaseManager:
    """
    Thread-safe SQLite wrapper.

    Uses a per-thread connection (check_same_thread=False with a lock)
    so Flask worker threads and background threads can all write safely.
    """

    # ---------------------------------------------------------------
    # Schema version — bump this when adding columns so _migrate() runs
    # ---------------------------------------------------------------
    SCHEMA_VERSION = 3

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.logger  = logging.getLogger(__name__)
        self._lock   = threading.Lock()

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads while writing
        self._conn.execute("PRAGMA foreign_keys=ON")

        self._create_tables()
        self._migrate()
        self.logger.info(f"Database initialised: {db_path}")

    # ---------------------------------------------------------------
    # Schema creation
    # ---------------------------------------------------------------

    def _create_tables(self) -> None:
        with self._lock:
            c = self._conn
            c.executescript("""
            -- ── Schema version tracker ──────────────────────────────────
            CREATE TABLE IF NOT EXISTS _meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            -- ── Detected tokens ──────────────────────────────────────────
            -- Every token detected by the token detector.
            -- Anti-scam results and sniper decisions are stored here too
            -- so the full lifecycle of each token is captured in one row.
            CREATE TABLE IF NOT EXISTS detected_tokens (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                signature       TEXT    UNIQUE NOT NULL,
                source          TEXT    NOT NULL,   -- pumpfun / raydium / raydium_cpmm
                detected_at     TEXT    NOT NULL,
                token_mint      TEXT,
                token_name      TEXT,
                token_symbol    TEXT,
                platform        TEXT,

                -- Market data at detection time
                initial_liquidity   REAL DEFAULT 0,
                market_cap          REAL DEFAULT 0,
                volume_1h           REAL DEFAULT 0,
                price_usd           REAL DEFAULT 0,

                -- pump.fun bonding curve state at detection
                bonding_curve_complete  INTEGER,    -- 0/1/NULL
                bonding_curve_real_sol  REAL,
                bonding_curve_mc_sol    REAL,
                bonding_curve_price_sol REAL,
                creator                 TEXT,

                -- Market data — last refresh (updated by refresh thread)
                latest_liquidity    REAL,
                latest_market_cap   REAL,
                latest_price_usd    REAL,
                latest_volume_1h    REAL,
                latest_updated_at   TEXT,

                -- Anti-scam evaluation (populated after sniper runs check_token)
                risk_score          INTEGER,
                risk_level          TEXT,
                risk_checks         TEXT,   -- JSON: full per-check results

                -- Sniper decision
                sniper_status       TEXT,   -- detected/passed/rejected/notified/simulated/bought
                sniper_action       TEXT,   -- the action taken
                reject_reason       TEXT,   -- filter that rejected it, if any

                -- URLs
                solscan_url         TEXT,
                dexscreener_url     TEXT,

                -- Full raw token_info dict — maximum data for AI training
                raw_json            TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tokens_mint      ON detected_tokens(token_mint);
            CREATE INDEX IF NOT EXISTS idx_tokens_detected  ON detected_tokens(detected_at);
            CREATE INDEX IF NOT EXISTS idx_tokens_source    ON detected_tokens(source);
            CREATE INDEX IF NOT EXISTS idx_tokens_symbol    ON detected_tokens(token_symbol);

            -- ── Sniper simulation positions ──────────────────────────────
            CREATE TABLE IF NOT EXISTS sniper_positions (
                id                  TEXT PRIMARY KEY,   -- UUID
                token_mint          TEXT,
                token_symbol        TEXT,
                platform            TEXT,
                entry_price         REAL DEFAULT 0,
                entry_time          TEXT,
                entry_mc            REAL DEFAULT 0,
                simulated_sol       REAL DEFAULT 0,
                simulated_sol_net   REAL DEFAULT 0,
                fee_entry_sol       REAL DEFAULT 0,
                fee_entry_pct       REAL DEFAULT 1.25,
                fee_exit_pct        REAL DEFAULT 1.25,
                current_price       REAL DEFAULT 0,
                pnl_percent         REAL DEFAULT 0,
                pnl_sol             REAL DEFAULT 0,
                fees_sol            REAL DEFAULT 0,
                status              TEXT DEFAULT 'open',
                exit_price          REAL,
                exit_time           TEXT,
                exit_reason         TEXT,
                fee_exit_sol        REAL DEFAULT 0,
                sniper_tx           TEXT,
                last_updated        TEXT,
                raw_json            TEXT    -- full position dict for AI
            );
            CREATE INDEX IF NOT EXISTS idx_sniper_pos_status ON sniper_positions(status);
            CREATE INDEX IF NOT EXISTS idx_sniper_pos_mint   ON sniper_positions(token_mint);

            -- ── Copy-trading simulation positions ────────────────────────
            CREATE TABLE IF NOT EXISTS ct_positions (
                id                  TEXT PRIMARY KEY,   -- UUID
                source_wallet       TEXT,
                source_label        TEXT,
                execution_mode      TEXT,
                token_mint          TEXT,
                token_symbol        TEXT,
                platform            TEXT,
                entry_price         REAL DEFAULT 0,
                entry_time          TEXT,
                entry_mc            REAL DEFAULT 0,
                simulated_sol       REAL DEFAULT 0,
                simulated_sol_net   REAL DEFAULT 0,
                fee_entry_sol       REAL DEFAULT 0,
                fee_entry_pct       REAL DEFAULT 1.25,
                fee_exit_pct        REAL DEFAULT 1.25,
                current_price       REAL DEFAULT 0,
                pnl_percent         REAL DEFAULT 0,
                pnl_sol             REAL DEFAULT 0,
                fees_sol            REAL DEFAULT 0,
                status              TEXT DEFAULT 'open',
                exit_price          REAL,
                exit_time           TEXT,
                exit_reason         TEXT,
                fee_exit_sol        REAL DEFAULT 0,
                whale_tx            TEXT,
                last_updated        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ct_pos_status ON ct_positions(status);
            CREATE INDEX IF NOT EXISTS idx_ct_pos_wallet ON ct_positions(source_wallet);
            CREATE INDEX IF NOT EXISTS idx_ct_pos_mint   ON ct_positions(token_mint);

            -- ── Copy-trading activity history ────────────────────────────
            CREATE TABLE IF NOT EXISTS ct_history (
                id                  TEXT PRIMARY KEY,
                timestamp           TEXT,
                source_wallet       TEXT,
                source_label        TEXT,
                execution_mode      TEXT,
                action              TEXT,   -- buy / sell
                token_symbol        TEXT,
                token_mint          TEXT,
                whale_amount_sol    REAL DEFAULT 0,
                our_amount_sol      REAL,
                price_usd           REAL DEFAULT 0,
                signature           TEXT,
                dex                 TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ct_hist_wallet ON ct_history(source_wallet);
            CREATE INDEX IF NOT EXISTS idx_ct_hist_time   ON ct_history(timestamp);
            """)
            c.commit()

    def _migrate(self) -> None:
        """Apply schema migrations for future column additions."""
        cur = self._conn.execute(
            "SELECT value FROM _meta WHERE key='schema_version'"
        ).fetchone()
        current = int(cur['value']) if cur else 0

        if current < 2:
            # v2: add outcome tracking columns to detected_tokens for AI training
            new_cols = [
                ("outcome_price_1h",    "REAL"),   # price at 1h after detection
                ("outcome_price_6h",    "REAL"),   # price at 6h after detection
                ("outcome_price_24h",   "REAL"),   # price at 24h after detection
                ("outcome_max_price",   "REAL"),   # rolling max across all checks
                ("outcome_max_gain_pct","REAL"),   # best % gain vs entry price
                ("outcome_complete",    "INTEGER DEFAULT 0"),  # 1 = 24h check done
            ]
            with self._lock:
                for col, col_type in new_cols:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE detected_tokens ADD COLUMN {col} {col_type}"
                        )
                    except Exception:
                        pass  # column already exists — safe to ignore
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tokens_outcome "
                    "ON detected_tokens(outcome_complete, detected_at)"
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta VALUES ('schema_version', '2')"
                )
                self._conn.commit()
            self.logger.info("DB schema migrated 1 → 2 (outcome tracking columns added)")

        if current < 3:
            # v3: persist ML pump-predictor scores alongside each token and position
            with self._lock:
                for col, col_type in [
                    ("ml_score",         "INTEGER"),  # 0-100 scaled pump probability
                    ("pump_probability",  "REAL"),     # raw model output 0.0-1.0
                ]:
                    try:
                        self._conn.execute(
                            f"ALTER TABLE detected_tokens ADD COLUMN {col} {col_type}"
                        )
                    except Exception:
                        pass  # already exists
                try:
                    self._conn.execute(
                        "ALTER TABLE sniper_positions ADD COLUMN ml_score INTEGER"
                    )
                except Exception:
                    pass  # already exists
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tokens_ml_score "
                    "ON detected_tokens(ml_score)"
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta VALUES ('schema_version', '3')"
                )
                self._conn.commit()
            self.logger.info("DB schema migrated 2 → 3 (ml_score + pump_probability columns added)")

        if current < self.SCHEMA_VERSION:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta VALUES ('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),)
                )
                self._conn.commit()
            self.logger.info(f"DB schema migrated {current} → {self.SCHEMA_VERSION}")

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _row_to_dict(self, row: Optional[sqlite3.Row]) -> Optional[Dict]:
        if row is None:
            return None
        d = dict(row)
        # Deserialise JSON columns
        for col in ('risk_checks', 'raw_json'):
            if col in d and d[col]:
                try:
                    d[col] = json.loads(d[col])
                except Exception:
                    pass
        return d

    def _rows_to_list(self, rows) -> List[Dict]:
        return [self._row_to_dict(r) for r in rows]

    # ---------------------------------------------------------------
    # detected_tokens
    # ---------------------------------------------------------------

    def save_detected_token(self, token_info: Dict) -> None:
        """
        Insert or ignore a detected token.
        Fields that are enriched later (risk_score, sniper_status, market
        refreshes) are updated via the dedicated update methods.
        """
        sig = token_info.get('signature') or token_info.get('sig_short', '')
        if not sig:
            return

        bc = token_info.get('bonding_curve') or {}

        with self._lock:
            try:
                self._conn.execute("""
                    INSERT OR IGNORE INTO detected_tokens (
                        signature, source, detected_at, token_mint, token_name,
                        token_symbol, platform, initial_liquidity, market_cap,
                        volume_1h, price_usd,
                        bonding_curve_complete, bonding_curve_real_sol,
                        bonding_curve_mc_sol, bonding_curve_price_sol,
                        creator, solscan_url, dexscreener_url, raw_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    sig,
                    token_info.get('source', 'unknown'),
                    token_info.get('detected_at', datetime.utcnow().isoformat()),
                    token_info.get('token_mint'),
                    token_info.get('token_name'),
                    token_info.get('token_symbol'),
                    token_info.get('platform', token_info.get('source')),
                    token_info.get('initial_liquidity', 0),
                    token_info.get('market_cap', 0),
                    token_info.get('volume_1h', 0),
                    token_info.get('price_usd', 0),
                    int(bc.get('complete', False)) if bc else None,
                    bc.get('real_sol') if bc else None,
                    bc.get('market_cap_sol') if bc else None,
                    bc.get('price_sol') if bc else None,
                    token_info.get('creator'),
                    token_info.get('solscan_url'),
                    token_info.get('dexscreener_url'),
                    json.dumps(token_info),
                ))
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"save_detected_token error: {e}")

    def update_token_market_data(self, mint: str, data: Dict) -> None:
        """Update latest market data for a token (called by refresh thread)."""
        with self._lock:
            try:
                self._conn.execute("""
                    UPDATE detected_tokens
                    SET latest_liquidity  = COALESCE(?, latest_liquidity),
                        latest_market_cap = COALESCE(?, latest_market_cap),
                        latest_price_usd  = COALESCE(?, latest_price_usd),
                        latest_volume_1h  = COALESCE(?, latest_volume_1h),
                        latest_updated_at = ?,
                        -- Backfill price_usd when it was saved as 0 at detection
                        -- time (DexScreener not yet indexed, bonding curve unavailable).
                        -- Uses the first real price we receive — closest proxy to the
                        -- detection-time price with no significant lookahead bias.
                        price_usd = CASE
                            WHEN (price_usd IS NULL OR price_usd = 0) AND ? IS NOT NULL
                            THEN ?
                            ELSE price_usd
                        END
                    WHERE token_mint = ?
                """, (
                    data.get('liquidity_usd') or data.get('initial_liquidity'),
                    data.get('market_cap'),
                    data.get('price_usd'),
                    data.get('volume_1h'),
                    datetime.utcnow().isoformat(),
                    data.get('price_usd'),  # CASE condition check
                    data.get('price_usd'),  # CASE value
                    mint,
                ))
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"update_token_market_data error: {e}")

    def update_token_sniper_decision(self, mint: str, status: str,
                                     action: Optional[str] = None,
                                     reject_reason: Optional[str] = None,
                                     risk_score: Optional[int] = None,
                                     risk_level: Optional[str] = None,
                                     risk_checks: Optional[Dict] = None,
                                     ml_score: Optional[int] = None,
                                     pump_probability: Optional[float] = None) -> None:
        """Record the sniper's decision for a token, including ML scores."""
        with self._lock:
            try:
                self._conn.execute("""
                    UPDATE detected_tokens
                    SET sniper_status    = ?,
                        sniper_action    = COALESCE(?, sniper_action),
                        reject_reason    = COALESCE(?, reject_reason),
                        risk_score       = COALESCE(?, risk_score),
                        risk_level       = COALESCE(?, risk_level),
                        risk_checks      = COALESCE(?, risk_checks),
                        ml_score         = COALESCE(?, ml_score),
                        pump_probability = COALESCE(?, pump_probability)
                    WHERE token_mint = ?
                """, (
                    status,
                    action,
                    reject_reason,
                    risk_score,
                    risk_level,
                    json.dumps(risk_checks) if risk_checks else None,
                    ml_score,
                    pump_probability,
                    mint,
                ))
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"update_token_sniper_decision error: {e}")

    # ---------------------------------------------------------------
    # Outcome tracking (AI training labels)
    # ---------------------------------------------------------------

    def get_tokens_pending_outcome(self, limit: int = 100) -> List[Dict]:
        """
        Return tokens that need a price checkpoint.
        A token is pending if outcome_complete = 0 and it was detected
        at least 1 hour ago (so at least the 1h checkpoint is due).
        """
        rows = self._conn.execute("""
            SELECT id, token_mint, detected_at, price_usd,
                   outcome_price_1h, outcome_price_6h, outcome_price_24h,
                   outcome_max_price, outcome_max_gain_pct, outcome_complete
            FROM detected_tokens
            WHERE outcome_complete = 0
              AND token_mint IS NOT NULL
              AND detected_at <= datetime('now', '-1 hours')
            ORDER BY detected_at ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return self._rows_to_list(rows)

    def update_token_outcome(self, token_id: int, checkpoint: str,
                             price: float, max_price: float,
                             max_gain_pct: float, complete: bool) -> None:
        """
        Update a single outcome checkpoint for a detected token.

        Args:
            token_id:     The row id in detected_tokens
            checkpoint:   '1h', '6h', or '24h'
            price:        Current price at this checkpoint
            max_price:    New rolling maximum price (across all checkpoints)
            max_gain_pct: Best % gain vs entry price seen so far
            complete:     True when the 24h checkpoint has been recorded
        """
        col = {
            '1h':  'outcome_price_1h',
            '6h':  'outcome_price_6h',
            '24h': 'outcome_price_24h',
        }.get(checkpoint)
        if not col:
            return
        with self._lock:
            try:
                self._conn.execute(f"""
                    UPDATE detected_tokens
                    SET {col}              = ?,
                        outcome_max_price  = ?,
                        outcome_max_gain_pct = ?,
                        outcome_complete   = ?
                    WHERE id = ?
                """, (price, max_price, max_gain_pct, 1 if complete else 0, token_id))
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"update_token_outcome error: {e}")

    def get_detected_tokens(self, limit: int = 200,
                            source: Optional[str] = None,
                            since_hours: Optional[float] = None) -> List[Dict]:
        """Fetch detected tokens, newest first."""
        params: List[Any] = []
        where = []
        if source:
            where.append("source = ?")
            params.append(source)
        if since_hours is not None:
            where.append("detected_at >= datetime('now', ?)")
            params.append(f"-{since_hours} hours")
        sql = "SELECT * FROM detected_tokens"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY detected_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_list(rows)

    # ---------------------------------------------------------------
    # Sniper simulation positions
    # ---------------------------------------------------------------

    def save_sniper_position(self, position: Dict) -> None:
        """Insert or replace a sniper simulation position."""
        with self._lock:
            try:
                self._conn.execute("""
                    INSERT OR REPLACE INTO sniper_positions (
                        id, token_mint, token_symbol, platform,
                        entry_price, entry_time, entry_mc,
                        simulated_sol, simulated_sol_net,
                        fee_entry_sol, fee_entry_pct, fee_exit_pct,
                        current_price, pnl_percent, pnl_sol, fees_sol,
                        status, exit_price, exit_time, exit_reason,
                        fee_exit_sol, sniper_tx, last_updated, ml_score, raw_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    position['id'],
                    position.get('token_mint'),
                    position.get('token_symbol'),
                    position.get('platform'),
                    position.get('entry_price', 0),
                    position.get('entry_time'),
                    position.get('entry_mc', 0),
                    position.get('simulated_sol', 0),
                    position.get('simulated_sol_net', 0),
                    position.get('fee_entry_sol', 0),
                    position.get('fee_entry_pct', 1.25),
                    position.get('fee_exit_pct', 1.25),
                    position.get('current_price', 0),
                    position.get('pnl_percent', 0),
                    position.get('pnl_sol', 0),
                    position.get('fees_sol', 0),
                    position.get('status', 'open'),
                    position.get('exit_price'),
                    position.get('exit_time'),
                    position.get('exit_reason'),
                    position.get('fee_exit_sol', 0),
                    position.get('sniper_tx') or position.get('whale_tx'),
                    position.get('last_updated', datetime.utcnow().isoformat()),
                    position.get('ml_score'),
                    json.dumps(position),
                ))
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"save_sniper_position error: {e}")

    def get_sniper_positions(self, status: Optional[str] = None,
                             limit: int = 200) -> List[Dict]:
        params: List[Any] = []
        sql = "SELECT * FROM sniper_positions"
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_list(rows)

    def clear_sniper_positions(self) -> None:
        """Delete all sniper simulation positions (used by reset)."""
        with self._lock:
            try:
                self._conn.execute("DELETE FROM sniper_positions")
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"clear_sniper_positions error: {e}")

    # ---------------------------------------------------------------
    # Copy-trading positions
    # ---------------------------------------------------------------

    def save_ct_position(self, position: Dict) -> None:
        """Insert or replace a copy-trading simulation position."""
        with self._lock:
            try:
                self._conn.execute("""
                    INSERT OR REPLACE INTO ct_positions (
                        id, source_wallet, source_label, execution_mode,
                        token_mint, token_symbol, platform,
                        entry_price, entry_time, entry_mc,
                        simulated_sol, simulated_sol_net,
                        fee_entry_sol, fee_entry_pct, fee_exit_pct,
                        current_price, pnl_percent, pnl_sol, fees_sol,
                        status, exit_price, exit_time, exit_reason,
                        fee_exit_sol, whale_tx, last_updated
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    position['id'],
                    position.get('source_wallet'),
                    position.get('source_label'),
                    position.get('execution_mode'),
                    position.get('token_mint'),
                    position.get('token_symbol'),
                    position.get('platform'),
                    position.get('entry_price', 0),
                    position.get('entry_time'),
                    position.get('entry_mc', 0),
                    position.get('simulated_sol', 0),
                    position.get('simulated_sol_net', 0),
                    position.get('fee_entry_sol', 0),
                    position.get('fee_entry_pct', 1.25),
                    position.get('fee_exit_pct', 1.25),
                    position.get('current_price', 0),
                    position.get('pnl_percent', 0),
                    position.get('pnl_sol', 0),
                    position.get('fees_sol', 0),
                    position.get('status', 'open'),
                    position.get('exit_price'),
                    position.get('exit_time'),
                    position.get('exit_reason'),
                    position.get('fee_exit_sol', 0),
                    position.get('whale_tx'),
                    position.get('last_updated', datetime.utcnow().isoformat()),
                ))
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"save_ct_position error: {e}")

    def get_ct_positions(self, status: Optional[str] = None,
                         limit: int = 200) -> List[Dict]:
        params: List[Any] = []
        sql = "SELECT * FROM ct_positions"
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY entry_time DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_list(rows)

    # ---------------------------------------------------------------
    # Copy-trading history
    # ---------------------------------------------------------------

    def save_ct_history_entry(self, entry: Dict) -> None:
        with self._lock:
            try:
                self._conn.execute("""
                    INSERT OR IGNORE INTO ct_history (
                        id, timestamp, source_wallet, source_label,
                        execution_mode, action, token_symbol, token_mint,
                        whale_amount_sol, our_amount_sol, price_usd,
                        signature, dex
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    entry['id'],
                    entry.get('timestamp'),
                    entry.get('source_wallet'),
                    entry.get('source_label'),
                    entry.get('execution_mode'),
                    entry.get('action'),
                    entry.get('token_symbol'),
                    entry.get('token_mint'),
                    entry.get('whale_amount_sol', 0),
                    entry.get('our_amount_sol'),
                    entry.get('price_usd', 0),
                    entry.get('signature'),
                    entry.get('dex'),
                ))
                self._conn.commit()
            except Exception as e:
                self.logger.error(f"save_ct_history_entry error: {e}")

    def get_ct_history(self, limit: int = 200,
                       wallet: Optional[str] = None) -> List[Dict]:
        params: List[Any] = []
        sql = "SELECT * FROM ct_history"
        if wallet:
            sql += " WHERE source_wallet = ?"
            params.append(wallet)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return self._rows_to_list(rows)

    # ---------------------------------------------------------------
    # Migration helpers (load existing JSON into DB on first run)
    # ---------------------------------------------------------------

    def migrate_json_file(self, json_path: Path, table: str) -> int:
        """
        One-time migration: load records from a JSON list file into DB.
        Returns the number of records inserted.

        Supported tables: ct_positions, ct_history, sniper_positions
        """
        if not json_path.exists():
            return 0

        try:
            with open(json_path, 'r') as f:
                records = json.load(f)
        except Exception as e:
            self.logger.warning(f"Could not read {json_path} for migration: {e}")
            return 0

        if not isinstance(records, list):
            return 0

        count = 0
        for rec in records:
            try:
                if table == 'ct_positions':
                    self.save_ct_position(rec)
                elif table == 'ct_history':
                    self.save_ct_history_entry(rec)
                elif table == 'sniper_positions':
                    self.save_sniper_position(rec)
                count += 1
            except Exception:
                pass

        self.logger.info(f"Migrated {count} records from {json_path.name} → {table}")
        return count

    # ---------------------------------------------------------------
    # Stats helpers
    # ---------------------------------------------------------------

    def get_stats(self) -> Dict:
        """Return row counts for the overview dashboard."""
        def count(table, where=''):
            sql = f"SELECT COUNT(*) FROM {table}"
            if where:
                sql += f" WHERE {where}"
            return self._conn.execute(sql).fetchone()[0]

        return {
            'detected_tokens':       count('detected_tokens'),
            'detected_tokens_today': count('detected_tokens',
                                           "detected_at >= date('now')"),
            'sniper_positions_open':  count('sniper_positions', "status='open'"),
            'sniper_positions_total': count('sniper_positions'),
            'ct_positions_open':      count('ct_positions', "status='open'"),
            'ct_positions_total':     count('ct_positions'),
            'ct_history_total':       count('ct_history'),
        }

    def close(self) -> None:
        self._conn.close()
