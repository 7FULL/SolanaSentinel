"""
AI Analyzer Module
Provides heuristic-based AI analysis for tokens and wallets.
Uses real on-chain data from Solana RPC combined with statistical
analysis (numpy) to detect patterns, anomalies and suspicious behavior.

Also loads the trained ML model (pump_predictor_*.joblib) if available
and exposes predict_pump_probability() for the sniper pipeline.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
import logging
import json
import math
import numpy as np
from pathlib import Path


# ---------------------------------------------------------------------------
# Weights used by the token-risk scoring engine
# Each entry: (field_key, weight, description)
# The score produced is 0-100 where 100 = safest.
# ---------------------------------------------------------------------------
TOKEN_RISK_WEIGHTS = {
    "has_mint_authority":      {"weight": -25, "label": "Mint authority present"},
    "has_freeze_authority":    {"weight": -15, "label": "Freeze authority present"},
    "creator_pct_high":        {"weight": -20, "label": "Creator holds >20% supply"},
    "top10_concentration":     {"weight": -15, "label": "Top-10 holders >70% supply"},
    "lp_not_locked":           {"weight": -10, "label": "LP not locked"},
    "low_holder_count":        {"weight": -10, "label": "Fewer than 50 holders"},
    "volume_spike_anomaly":    {"weight": -5,  "label": "Abnormal volume spike"},
}

# Baseline score — deductions are applied on top of this.
BASELINE_SCORE = 100

RISK_BANDS = [
    (80, "low",      "Token appears relatively safe based on on-chain signals."),
    (55, "medium",   "Some risk indicators detected. Proceed with caution."),
    (30, "high",     "Multiple risk factors detected. High risk of rug or scam."),
    (0,  "critical", "Critical risk indicators present. Very likely a scam."),
]


def _classify_risk(score: int) -> Tuple[str, str]:
    """Return (risk_level, recommendation) for a given 0-100 score."""
    for threshold, level, rec in RISK_BANDS:
        if score >= threshold:
            return level, rec
    return "critical", RISK_BANDS[-1][2]


class AIAnalyzer:
    """
    Heuristic AI analyzer for Solana tokens and wallets.

    Fetches real on-chain data via the injected RPC client and applies a
    transparent, weighted scoring model.  All intermediate signals are
    returned so the UI can show exactly why a score was assigned.
    """

    def __init__(self, config, rpc_client=None):
        """
        Initialize the AI Analyzer.

        Args:
            config:     ConfigManager instance
            rpc_client: Optional SolanaRPCClient; when provided enables
                        live on-chain data fetching.
        """
        self.config = config
        self.rpc = rpc_client
        self.enabled = config.get("anti_scam.ai_analysis_enabled", True)
        self.logger = logging.getLogger(__name__)
        self._analysis_count = 0

        # ML pump-prediction model (optional — loaded from disk if present)
        # For gradient_boosting: _ml_model is the sklearn estimator
        # For tabnet:            _ml_model is the TabNetClassifier
        # For ensemble:          _ml_model is {'gb': sklearn_model, 'tabnet': TabNetClassifier}
        self._ml_model      = None
        self._ml_scaler     = None   # StandardScaler for models that need scaling
        self._ml_meta: Dict = {}
        self.models_loaded  = False
        self._load_ml_model(config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_token(self, token_address: str) -> Dict:
        """
        Analyze a token using heuristic on-chain data.

        Fetches mint info, holder distribution and liquidity signals from
        the Solana RPC then produces a 0-100 risk score with full
        explanation.

        Args:
            token_address: Token mint address to analyze

        Returns:
            Analysis result dict with risk_score, patterns, anomalies,
            probabilities and recommendation.
        """
        self._analysis_count += 1
        self.logger.info(f"AI analyzing token: {token_address}")

        signals = self._fetch_token_signals(token_address)
        score, deductions, flags = self._score_token(signals)
        patterns = self._detect_token_patterns(signals)
        anomalies = self._detect_token_anomalies(signals)
        risk_level, recommendation = _classify_risk(score)

        # Derive simple probability estimates from the score
        rugpull_prob = round(max(0.0, (100 - score) / 100 * 0.85), 2)
        pump_dump_prob = round(max(0.0, (100 - score) / 100 * 0.60), 2)
        legit_prob = round(min(1.0, score / 100), 2)

        return {
            "token_address": token_address,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "ai_enabled": self.enabled,
            "data_source": "on_chain" if self.rpc else "heuristic_only",
            "confidence": self._compute_confidence(signals),
            "risk_score": score,
            "risk_level": risk_level,
            "score_deductions": deductions,
            "patterns_detected": patterns,
            "anomalies": anomalies,
            "red_flags": flags,
            "rugpull_probability": rugpull_prob,
            "pump_dump_probability": pump_dump_prob,
            "legitimate_probability": legit_prob,
            "recommendation": recommendation,
            "explanation": self._build_explanation(signals, deductions),
            "raw_signals": signals,
        }

    def analyze_wallet(self, wallet_address: str) -> Dict:
        """
        Analyze wallet behavior using on-chain transaction history.

        Fetches recent signatures, derives behavioral metrics (win rate,
        hold time, diversification) and classifies the wallet type.

        Args:
            wallet_address: Wallet public key to analyze

        Returns:
            Wallet analysis result with behavioral patterns and
            copy-trading recommendation.
        """
        self._analysis_count += 1
        self.logger.info(f"AI analyzing wallet: {wallet_address}")

        tx_signals = self._fetch_wallet_signals(wallet_address)
        wallet_type = self._classify_wallet_type(tx_signals)
        patterns = self._detect_wallet_patterns(tx_signals)
        red_flags = self._detect_wallet_red_flags(tx_signals)
        green_flags = self._detect_wallet_green_flags(tx_signals)
        stats = self._compute_wallet_stats(tx_signals)
        copy_rec = self._build_copy_trading_recommendation(tx_signals, stats)

        # Overall risk level for the wallet itself
        if len(red_flags) >= 3:
            risk_level = "high"
        elif len(red_flags) >= 1:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "wallet_address": wallet_address,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "ai_enabled": self.enabled,
            "data_source": "on_chain" if self.rpc else "heuristic_only",
            "confidence": self._compute_wallet_confidence(tx_signals),
            "wallet_type": wallet_type,
            "risk_level": risk_level,
            "behavioral_patterns": patterns,
            "red_flags": red_flags,
            "green_flags": green_flags,
            "statistics": stats,
            "copy_trading_recommendation": copy_rec,
        }

    def detect_rugpull_pattern(self, token_data: Dict) -> Dict:
        """
        Detect potential rugpull patterns from structured token data.

        Args:
            token_data: Dict containing liquidity, holder, and price data

        Returns:
            Rugpull detection result
        """
        indicators = {}
        score = 0

        # Sudden liquidity drop
        liq_change = token_data.get("liquidity_change_pct", 0)
        indicators["sudden_liquidity_removal"] = liq_change < -50
        if indicators["sudden_liquidity_removal"]:
            score += 35

        # Creator dumping pattern
        creator_pct = token_data.get("creator_token_pct", 0)
        indicators["creator_dump_pattern"] = creator_pct > 15
        if indicators["creator_dump_pattern"]:
            score += 25

        # Wallet clustering (many buys from same origin)
        unique_buyers = token_data.get("unique_buyers", 100)
        total_buys = token_data.get("total_buy_txs", 100)
        cluster_ratio = (total_buys / max(unique_buyers, 1))
        indicators["suspicious_wallet_clustering"] = cluster_ratio > 5
        if indicators["suspicious_wallet_clustering"]:
            score += 20

        # Price collapse
        price_drop = token_data.get("price_drop_pct", 0)
        indicators["rapid_price_decline"] = price_drop < -70
        if indicators["rapid_price_decline"]:
            score += 15

        # Mint authority still active
        indicators["smart_contract_vulnerability"] = token_data.get(
            "has_mint_authority", False
        )
        if indicators["smart_contract_vulnerability"]:
            score += 5

        rugpull_detected = score >= 40
        confidence = min(score / 100, 0.99)
        risk_level, _ = _classify_risk(100 - score)

        return {
            "rugpull_detected": rugpull_detected,
            "confidence": round(confidence, 2),
            "risk_level": risk_level,
            "indicators": indicators,
            "score": score,
            "recommendation": (
                "High rugpull risk detected — avoid this token."
                if rugpull_detected
                else "No significant rugpull indicators detected."
            ),
        }

    def get_market_sentiment(self, token_address: str) -> Dict:
        """
        Derive proxy market sentiment from on-chain activity.

        Without social-media access we use volume trend, holder growth,
        and buy/sell ratio as proxies for sentiment.

        Args:
            token_address: Token mint address

        Returns:
            Sentiment analysis dict
        """
        signals = self._fetch_token_signals(token_address)

        # Volume trend proxy
        vol_trend = signals.get("volume_trend", 0.0)  # +1 up, -1 down
        holder_growth = signals.get("holder_growth_rate", 0.0)

        # Aggregate sentiment score -1..1
        raw = (vol_trend * 0.6 + holder_growth * 0.4)
        raw = max(-1.0, min(1.0, raw))

        if raw > 0.3:
            overall = "positive"
            short_term = "bullish"
        elif raw < -0.3:
            overall = "negative"
            short_term = "bearish"
        else:
            overall = "neutral"
            short_term = "neutral"

        return {
            "token_address": token_address,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "overall_sentiment": overall,
            "sentiment_score": round(raw, 2),
            "sources": {
                "trading_volume": {
                    "sentiment": "positive" if vol_trend > 0 else "negative",
                    "score": round(vol_trend, 2),
                    "trend": "increasing" if vol_trend > 0 else "decreasing",
                },
                "holder_behavior": {
                    "sentiment": "positive" if holder_growth > 0 else "negative",
                    "score": round(holder_growth, 2),
                    "accumulation": holder_growth > 0.1,
                },
            },
            "prediction": {
                "short_term": short_term,
                "confidence": round(abs(raw), 2),
            },
        }

    def load_models(self) -> bool:
        """Attempt to reload the ML model from disk."""
        return self.reload_model()

    def reload_model(self) -> bool:
        """
        Reload the ML model from disk.

        Called automatically after a ModelTrainer run completes so the
        new model is picked up without restarting the backend process.
        Returns True if the model loaded successfully.
        """
        self._ml_model  = None
        self._ml_scaler = None
        self._ml_meta   = {}
        self.models_loaded = False
        self._load_ml_model(self.config)
        return self.models_loaded

    def get_status(self) -> Dict:
        """Return current analyzer status."""
        return {
            "enabled":        self.enabled,
            "models_loaded":  self.models_loaded,
            "data_source":    "on_chain_heuristic",
            "rpc_available":  self.rpc is not None,
            "total_analyses": self._analysis_count,
            "ml_model": {
                "loaded":    self._ml_model is not None,
                "name":      self._ml_meta.get("model_name", "none"),
                "roc_auc":   self._ml_meta.get("roc_auc"),
                "pr_auc":    self._ml_meta.get("pr_auc"),
                "threshold": self._ml_meta.get("threshold"),
                "base_rate": self._ml_meta.get("base_rate"),
            },
        }

    # ------------------------------------------------------------------
    # ML model loading + pump-probability prediction
    # ------------------------------------------------------------------

    def _load_ml_model(self, config) -> None:
        """
        Try to load the trained ML pump-predictor from disk.

        Looks for model_meta.json in <data_dir>/models/.  Supports three
        model_type values stored in the meta:

          gradient_boosting — standard sklearn estimator (.joblib)
          tabnet            — pytorch-tabnet model (.zip) + optional scaler
          ensemble          — sklearn GB + TabNet weighted average + scaler

        Skips silently if joblib is not installed or no files exist yet.
        """
        try:
            import joblib  # noqa: PLC0415

            models_dir = Path(config.get_data_path("models"))
            meta_path  = models_dir / "model_meta.json"

            if not meta_path.exists():
                self.logger.info("ML model not found at %s — skipping", meta_path)
                return

            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)

            model_type = meta.get("model_type", "gradient_boosting")

            # ----------------------------------------------------------
            # Load scaler (used by tabnet and ensemble)
            # ----------------------------------------------------------
            if meta.get("scaler_file"):
                scaler_path = models_dir / meta["scaler_file"]
                if scaler_path.exists():
                    self._ml_scaler = joblib.load(scaler_path)

            # ----------------------------------------------------------
            # Load model(s) based on type
            # ----------------------------------------------------------
            if model_type == "tabnet":
                self._ml_model = self._load_tabnet(
                    models_dir, meta.get("model_file", ""), meta
                )

            elif model_type == "ensemble":
                # Load the gradient boosting component (always present)
                gb_path = models_dir / meta["model_file"]
                if not gb_path.exists():
                    self.logger.warning("Ensemble GB model missing: %s", gb_path)
                    return
                gb_model = joblib.load(gb_path)
                ensemble: Dict = {"gb": gb_model}

                # Load TabNet component if saved alongside
                if meta.get("tabnet_file"):
                    tn = self._load_tabnet(models_dir, meta["tabnet_file"], meta)
                    if tn is not None:
                        ensemble["tabnet"] = tn

                self._ml_model = ensemble

            else:
                # gradient_boosting (or legacy files without model_type)
                model_path = models_dir / meta["model_file"]
                if not model_path.exists():
                    self.logger.warning("ML model file missing: %s", model_path)
                    return
                self._ml_model = joblib.load(model_path)

            self._ml_meta      = meta
            self.models_loaded = True
            self.logger.info(
                "ML model loaded: %s  type=%s  ROC-AUC=%.3f  PR-AUC=%.3f  threshold=%.3f",
                meta["model_name"],
                model_type,
                meta.get("roc_auc", 0),
                meta.get("pr_auc", 0),
                meta.get("threshold", 0.5),
            )
            print(
                f"[AI-ANALYZER] ML model loaded: {meta['model_name']} "
                f"type={model_type} "
                f"ROC-AUC={meta.get('roc_auc', 0):.3f} "
                f"PR-AUC={meta.get('pr_auc', 0):.3f} "
                f"threshold={meta.get('threshold', 0.5):.3f}"
            )

        except ImportError:
            self.logger.info("joblib not installed — ML pump prediction disabled")
        except Exception as exc:
            self.logger.error("Failed to load ML model: %s", exc)

    def _load_tabnet(self, models_dir: Path, model_file: str, meta: Dict):
        """Load a TabNetClassifier from disk.  Returns None on failure."""
        try:
            from pytorch_tabnet.tab_model import TabNetClassifier  # noqa: PLC0415
            tn = TabNetClassifier(device_name="cpu", verbose=0)
            # TabNet saves as <name>.zip — load_model() strips the extension itself
            zip_path = models_dir / model_file
            stem_path = str(zip_path.with_suffix(""))
            tn.load_model(stem_path)
            return tn
        except Exception as exc:
            self.logger.warning("TabNet load failed (%s): %s", model_file, exc)
            return None

    def _extract_ml_features(self, token_info: Dict) -> Optional[list]:
        """
        Build a feature vector from token_info, following the exact order
        stored in meta['feature_cols'].  This makes the code forward-
        compatible: retrain the notebook with any set of features, update
        model_meta.json, and inference adjusts automatically.

        Returns None when the token lacks enough data to produce a vector.
        """
        eps = 1e-12

        # Pre-compute all possible raw values
        mc    = float(token_info.get("market_cap") or 0)
        liq   = float(
            token_info.get("initial_liquidity")
            or token_info.get("liquidity")
            or 0
        )
        price = float(token_info.get("price_usd") or 0)

        # Require at least some price signal
        if price <= 0:
            return None

        vol       = float(token_info.get("volume_1h") or 0)
        bc_complete = token_info.get("bonding_curve_complete")
        on_curve  = float(bc_complete == 0) if bc_complete is not None else 0.0
        bc_sol    = float(token_info.get("bonding_curve_real_sol") or 0)
        bc_mc_sol = float(token_info.get("bonding_curve_mc_sol")   or 0)
        risk_score = float(token_info.get("risk_score") or 50)
        risk_level = token_info.get("risk_level", "")
        is_safe    = float(risk_level in ("safe", "low"))
        is_pumpfun = float(
            str(token_info.get("source", "")).lower() in ("pumpfun", "pump.fun")
        )

        detected_at = token_info.get("detected_at") or token_info.get("timestamp")
        if detected_at:
            try:
                if isinstance(detected_at, str):
                    from datetime import datetime as _dt  # noqa: PLC0415
                    dt = _dt.fromisoformat(detected_at.replace("Z", "+00:00"))
                else:
                    dt = detected_at
                hour = dt.hour + dt.minute / 60.0
            except Exception:
                hour = 12.0
        else:
            hour = 12.0

        # Feature name → computed value (supports all past and future features)
        lookup: Dict = {
            "f_log_mc":        math.log1p(max(mc,    0)),
            "f_log_liq":       math.log1p(max(liq,   0)),
            "f_log_price":     math.log1p(max(price,  0)),
            "f_log_vol":       math.log1p(max(vol,    0)),
            "f_liq_mc_ratio":  liq / (mc + eps),
            "f_on_curve":      on_curve,
            "f_log_bc_sol":    math.log1p(max(bc_sol,    0)),
            "f_log_bc_mc_sol": math.log1p(max(bc_mc_sol, 0)),
            "f_risk_score":    risk_score,
            "f_is_safe":       is_safe,
            "f_is_pumpfun":    is_pumpfun,
            "f_hour_sin":      math.sin(2 * math.pi * hour / 24),
            "f_hour_cos":      math.cos(2 * math.pi * hour / 24),
        }

        # Assemble in the exact order the model was trained with
        feature_cols = self._ml_meta.get("feature_cols") or list(lookup.keys())

        missing = [k for k in feature_cols if k not in lookup]
        if missing:
            self.logger.warning("Unknown feature(s) in model meta: %s", missing)
            return None

        return [lookup[k] for k in feature_cols]

    def predict_pump_probability(self, token_info: Dict) -> Dict:
        """
        Run the ML pump-predictor on a live token_info dict.

        Called by the sniper pipeline after anti-scam passes.  Avoids any
        additional RPC calls — uses only the fields already in token_info.
        Dispatches on model_type from model_meta.json:

          gradient_boosting — sklearn predict_proba on raw features
          tabnet            — TabNetClassifier on scaled features
          ensemble          — weighted average of GB + TabNet

        Returns a dict with:
            pump_probability  float  0-1  raw model probability
            pump_signal       bool   True if probability >= threshold
            ml_score          int    0-100 scaled version of probability
            threshold         float  decision boundary used
            model_name        str
            skipped           bool   True when model is not available
        """
        base: Dict = {
            "pump_probability": None,
            "pump_signal":      False,
            "ml_score":         None,
            "threshold":        self._ml_meta.get("threshold"),
            "model_name":       self._ml_meta.get("model_name", "none"),
            "skipped":          True,
            "skip_reason":      "model_not_loaded",
        }

        if self._ml_model is None:
            return base

        features = self._extract_ml_features(token_info)
        if features is None:
            base["skip_reason"] = "insufficient_token_data"
            return base

        try:
            X_raw = np.array(features, dtype=float).reshape(1, -1)
            model_type = self._ml_meta.get("model_type", "gradient_boosting")
            thr = float(self._ml_meta.get("threshold", 0.5))

            if model_type == "tabnet":
                # TabNet requires scaled float32 input
                X_in = (
                    self._ml_scaler.transform(X_raw).astype(np.float32)
                    if self._ml_scaler is not None
                    else X_raw.astype(np.float32)
                )
                prob = float(self._ml_model.predict_proba(X_in)[0, 1])

            elif model_type == "ensemble" and isinstance(self._ml_model, dict):
                # GB component (raw features, no scaling needed)
                gb_prob = float(self._ml_model["gb"].predict_proba(X_raw)[0, 1])
                if "tabnet" in self._ml_model and self._ml_scaler is not None:
                    X_sc = self._ml_scaler.transform(X_raw).astype(np.float32)
                    tn_prob = float(
                        self._ml_model["tabnet"].predict_proba(X_sc)[0, 1]
                    )
                    gb_w = float(self._ml_meta.get("gb_weight", 0.7))
                    tn_w = float(self._ml_meta.get("tn_weight", 0.3))
                    prob = gb_w * gb_prob + tn_w * tn_prob
                else:
                    # TabNet not saved — fall back to GB component only
                    prob = gb_prob

            else:
                # gradient_boosting (sklearn) — the common case
                prob = float(self._ml_model.predict_proba(X_raw)[0, 1])

            return {
                "pump_probability": round(prob, 4),
                "pump_signal":      prob >= thr,
                "ml_score":         int(round(prob * 100)),
                "threshold":        thr,
                "model_name":       self._ml_meta.get("model_name", "unknown"),
                "skipped":          False,
                "skip_reason":      None,
            }
        except Exception as exc:
            self.logger.error("ML prediction failed: %s", exc)
            base["skip_reason"] = f"prediction_error: {exc}"
            return base

    # ------------------------------------------------------------------
    # Signal Fetching (on-chain via RPC when available)
    # ------------------------------------------------------------------

    def _fetch_token_signals(self, token_address: str) -> Dict:
        """
        Fetch raw on-chain signals for a token.

        When an RPC client is available, performs live queries.
        Otherwise falls back to neutral defaults so scoring still works.
        """
        defaults = {
            "has_mint_authority": False,
            "has_freeze_authority": False,
            "creator_pct": 0.0,
            "top10_holder_pct": 0.0,
            "holder_count": 100,
            "lp_locked": True,
            "liquidity_usd": 0.0,
            "volume_24h": 0.0,
            "volume_trend": 0.0,
            "holder_growth_rate": 0.0,
            "age_hours": 24.0,
            "tx_count_1h": 0,
            "buy_sell_ratio": 1.0,
            "rpc_available": False,
        }

        if not self.rpc:
            return defaults

        try:
            from solders.pubkey import Pubkey

            pubkey = Pubkey.from_string(token_address)
            acct = self.rpc.get_account_info(pubkey)

            if acct is None:
                self.logger.warning(f"No account info for {token_address}")
                return defaults

            signals = dict(defaults)
            signals["rpc_available"] = True

            # Parse mint account data (165 bytes for standard SPL Mint)
            raw_data = acct.get("data")
            if raw_data and len(raw_data) >= 82:
                signals.update(self._parse_mint_data(raw_data))

            # Holder distribution via token accounts
            holder_info = self._get_holder_distribution(token_address)
            signals.update(holder_info)

            return signals

        except Exception as e:
            self.logger.error(f"Failed to fetch token signals for {token_address}: {e}")
            return defaults

    def _fetch_wallet_signals(self, wallet_address: str) -> Dict:
        """
        Fetch behavioral signals from a wallet's recent transaction history.
        """
        defaults = {
            "tx_count": 0,
            "recent_txs": [],
            "unique_tokens_traded": 0,
            "avg_hold_time_hours": 0.0,
            "profitable_tx_count": 0,
            "total_tx_analyzed": 0,
            "scam_token_interactions": 0,
            "mev_bot_signals": 0,
            "rpc_available": False,
        }

        if not self.rpc:
            return defaults

        try:
            from solders.pubkey import Pubkey

            pubkey = Pubkey.from_string(wallet_address)
            signatures = self.rpc.get_signatures_for_address(pubkey, limit=50)

            signals = dict(defaults)
            signals["rpc_available"] = True
            signals["tx_count"] = len(signatures)
            signals["total_tx_analyzed"] = len(signatures)
            signals["recent_txs"] = signatures

            # Derive timing metrics
            if signatures:
                block_times = [
                    s["block_time"] for s in signatures if s.get("block_time")
                ]
                if len(block_times) >= 2:
                    arr = np.array(sorted(block_times))
                    diffs_hours = np.diff(arr) / 3600.0
                    signals["avg_hold_time_hours"] = float(np.median(diffs_hours))

                # MEV bot heuristic: many txs in very short window
                if len(block_times) >= 5:
                    recent_window = max(block_times) - min(block_times)
                    if recent_window < 300 and len(block_times) >= 10:
                        signals["mev_bot_signals"] = 1

                # Profitable tx count approximation
                error_free = sum(1 for s in signatures if s.get("err") is None)
                signals["profitable_tx_count"] = error_free

            return signals

        except Exception as e:
            self.logger.error(f"Failed to fetch wallet signals for {wallet_address}: {e}")
            return defaults

    # ------------------------------------------------------------------
    # Mint data parsing
    # ------------------------------------------------------------------

    def _parse_mint_data(self, data: bytes) -> Dict:
        """
        Parse SPL Mint account data layout.

        SPL Mint layout (82 bytes):
          0-3   : mint_authority option (4 bytes)
          4-35  : mint_authority pubkey (32 bytes)
          36-39 : supply option (4 bytes)
          40-47 : supply (u64, 8 bytes)
          48    : decimals (u8)
          49    : is_initialized (bool)
          50-53 : freeze_authority option (4 bytes)
          54-85 : freeze_authority pubkey (32 bytes)
        """
        result = {}
        try:
            # mint_authority option: 0 = None, 1 = Some
            result["has_mint_authority"] = data[0] == 1

            # freeze_authority option at byte 50
            if len(data) >= 55:
                result["has_freeze_authority"] = data[50] == 1
            else:
                result["has_freeze_authority"] = False

            # supply (u64 little-endian at byte 40)
            if len(data) >= 48:
                import struct
                supply_raw = struct.unpack_from("<Q", data, 40)[0]
                decimals = data[48] if len(data) > 48 else 9
                result["supply"] = supply_raw / (10 ** decimals)
                result["decimals"] = decimals

        except Exception as e:
            self.logger.debug(f"Partial mint parse: {e}")

        return result

    def _get_holder_distribution(self, token_address: str) -> Dict:
        """
        Approximate holder distribution using token largest accounts endpoint.
        """
        result = {
            "holder_count": 0,
            "top10_holder_pct": 0.0,
            "creator_pct": 0.0,
        }

        try:
            from solders.pubkey import Pubkey
            pubkey = Pubkey.from_string(token_address)

            resp = self.rpc.client.get_token_largest_accounts(pubkey)
            if not resp or not resp.value:
                return result

            accounts = resp.value
            amounts = [float(a.amount) for a in accounts if a.amount]
            if not amounts:
                return result

            total = sum(amounts)
            if total == 0:
                return result

            # Approximate: largest account is often the creator/team wallet
            result["creator_pct"] = round((amounts[0] / total) * 100, 2)

            top10_sum = sum(amounts[:10])
            result["top10_holder_pct"] = round((top10_sum / total) * 100, 2)
            result["holder_count"] = len(amounts)

        except Exception as e:
            self.logger.debug(f"Holder distribution fetch failed: {e}")

        return result

    # ------------------------------------------------------------------
    # Scoring engine
    # ------------------------------------------------------------------

    def _score_token(self, signals: Dict) -> Tuple[int, List[Dict], List[str]]:
        """
        Apply weighted deductions to produce a 0-100 risk score.

        Returns (score, deductions_list, red_flags_list).
        """
        score = BASELINE_SCORE
        deductions = []
        red_flags = []

        checks = {
            "has_mint_authority": signals.get("has_mint_authority", False),
            "has_freeze_authority": signals.get("has_freeze_authority", False),
            "creator_pct_high": signals.get("creator_pct", 0.0) > 20.0,
            "top10_concentration": signals.get("top10_holder_pct", 0.0) > 70.0,
            "lp_not_locked": not signals.get("lp_locked", True),
            "low_holder_count": signals.get("holder_count", 100) < 50,
            "volume_spike_anomaly": abs(signals.get("volume_trend", 0.0)) > 3.0,
        }

        for key, triggered in checks.items():
            if triggered:
                weight = TOKEN_RISK_WEIGHTS[key]["weight"]
                label = TOKEN_RISK_WEIGHTS[key]["label"]
                score += weight  # weight is negative
                deductions.append({
                    "check": key,
                    "label": label,
                    "deduction": abs(weight),
                })
                red_flags.append(label)

        score = max(0, min(100, score))
        return score, deductions, red_flags

    # ------------------------------------------------------------------
    # Pattern & anomaly detection
    # ------------------------------------------------------------------

    def _detect_token_patterns(self, signals: Dict) -> List[Dict]:
        """Identify positive behavioral patterns in the token."""
        patterns = []

        if not signals.get("has_mint_authority") and not signals.get("has_freeze_authority"):
            patterns.append({
                "pattern": "immutable_supply",
                "confidence": 0.95,
                "description": "No mint or freeze authority — supply is immutable",
            })

        holder_count = signals.get("holder_count", 0)
        if holder_count >= 100:
            patterns.append({
                "pattern": "organic_holder_growth",
                "confidence": min(0.5 + holder_count / 1000, 0.95),
                "description": f"Token has {holder_count} holders, suggesting organic distribution",
            })

        top10 = signals.get("top10_holder_pct", 100.0)
        if top10 < 50:
            patterns.append({
                "pattern": "distributed_holdings",
                "confidence": round(1.0 - top10 / 100, 2),
                "description": f"Top-10 holders control only {top10:.1f}% — well distributed",
            })

        vol_trend = signals.get("volume_trend", 0.0)
        if 0 < vol_trend <= 2.0:
            patterns.append({
                "pattern": "healthy_volume_growth",
                "confidence": round(min(vol_trend / 2.0, 0.90), 2),
                "description": "Trading volume is growing steadily",
            })

        return patterns

    def _detect_token_anomalies(self, signals: Dict) -> List[Dict]:
        """Identify anomalous signals that warrant attention."""
        anomalies = []

        vol_trend = signals.get("volume_trend", 0.0)
        if vol_trend > 3.0:
            anomalies.append({
                "anomaly": "extreme_volume_spike",
                "severity": "high",
                "confidence": min(vol_trend / 10.0, 0.95),
                "description": "Abnormally high volume spike — possible wash trading",
                "recommendation": "Investigate buy/sell ratio before entry",
            })
        elif vol_trend < -2.0:
            anomalies.append({
                "anomaly": "volume_collapse",
                "severity": "medium",
                "confidence": 0.75,
                "description": "Volume dropping sharply — possible exit by team",
                "recommendation": "Monitor liquidity pool depth closely",
            })

        creator_pct = signals.get("creator_pct", 0.0)
        if creator_pct > 30.0:
            anomalies.append({
                "anomaly": "creator_concentration",
                "severity": "high",
                "confidence": min(creator_pct / 100.0, 0.95),
                "description": f"Largest holder controls {creator_pct:.1f}% — extreme concentration",
                "recommendation": "High dump risk; wait for creator to distribute",
            })

        holder_count = signals.get("holder_count", 0)
        if 0 < holder_count < 20:
            anomalies.append({
                "anomaly": "extremely_low_holders",
                "severity": "critical",
                "confidence": 0.90,
                "description": f"Only {holder_count} holders — likely brand new or honeypot",
                "recommendation": "Avoid until holder base grows organically",
            })

        return anomalies

    # ------------------------------------------------------------------
    # Wallet analysis helpers
    # ------------------------------------------------------------------

    def _classify_wallet_type(self, signals: Dict) -> str:
        """Classify wallet based on behavioral signals."""
        tx_count = signals.get("tx_count", 0)
        avg_hold = signals.get("avg_hold_time_hours", 0.0)
        mev = signals.get("mev_bot_signals", 0)

        if mev > 0:
            return "mev_bot"
        if tx_count > 200 and avg_hold < 1.0:
            return "high_frequency_trader"
        if tx_count > 50 and avg_hold < 24.0:
            return "active_trader"
        if tx_count > 10 and avg_hold >= 24.0:
            return "swing_trader"
        if tx_count <= 10:
            return "occasional_trader"
        return "unknown"

    def _detect_wallet_patterns(self, signals: Dict) -> List[Dict]:
        """Identify behavioral patterns from wallet tx history."""
        patterns = []
        tx_count = signals.get("tx_count", 0)
        avg_hold = signals.get("avg_hold_time_hours", 0.0)
        profitable = signals.get("profitable_tx_count", 0)
        total = max(signals.get("total_tx_analyzed", 1), 1)

        win_rate = (profitable / total) * 100

        if win_rate >= 65:
            patterns.append({
                "pattern": "consistent_profit_taking",
                "confidence": round(win_rate / 100, 2),
                "description": f"High success rate across {total} analyzed transactions",
            })

        if avg_hold >= 12 and avg_hold <= 72:
            patterns.append({
                "pattern": "swing_trading_strategy",
                "confidence": 0.80,
                "description": f"Average hold time of {avg_hold:.1f}h suggests swing strategy",
            })

        if tx_count >= 30:
            patterns.append({
                "pattern": "experienced_participant",
                "confidence": min(tx_count / 200, 0.95),
                "description": f"Wallet has executed {tx_count} transactions — experienced",
            })

        return patterns

    def _detect_wallet_red_flags(self, signals: Dict) -> List[str]:
        flags = []
        if signals.get("mev_bot_signals", 0) > 0:
            flags.append("Potential MEV bot or automated trading detected")
        if signals.get("scam_token_interactions", 0) > 0:
            flags.append(f"Interacted with {signals['scam_token_interactions']} known scam tokens")
        if signals.get("tx_count", 0) == 0:
            flags.append("No transaction history found — brand new wallet")
        return flags

    def _detect_wallet_green_flags(self, signals: Dict) -> List[str]:
        flags = []
        profitable = signals.get("profitable_tx_count", 0)
        total = max(signals.get("total_tx_analyzed", 1), 1)
        win_rate = (profitable / total) * 100

        if win_rate >= 60:
            flags.append(f"Strong success rate: {win_rate:.1f}% of transactions error-free")
        if signals.get("scam_token_interactions", 0) == 0 and signals.get("tx_count", 0) > 0:
            flags.append("No interactions with known scam tokens detected")
        if signals.get("tx_count", 0) >= 20:
            flags.append(f"Active wallet with {signals['tx_count']} recorded transactions")
        return flags

    def _compute_wallet_stats(self, signals: Dict) -> Dict:
        tx_count = signals.get("tx_count", 0)
        profitable = signals.get("profitable_tx_count", 0)
        total = max(signals.get("total_tx_analyzed", 1), 1)
        win_rate = round((profitable / total) * 100, 1)

        return {
            "win_rate": win_rate,
            "average_hold_time_hours": round(signals.get("avg_hold_time_hours", 0.0), 1),
            "total_transactions": tx_count,
            "total_tx_analyzed": total,
            "profitable_tx_count": profitable,
            "scam_token_exposure": signals.get("scam_token_interactions", 0),
            "mev_bot_signals": signals.get("mev_bot_signals", 0),
        }

    def _build_copy_trading_recommendation(self, signals: Dict, stats: Dict) -> Dict:
        """Build a copy-trading recommendation from wallet signals."""
        win_rate = stats.get("win_rate", 0.0)
        tx_count = signals.get("tx_count", 0)
        mev = signals.get("mev_bot_signals", 0)

        recommended = win_rate >= 55 and tx_count >= 10 and mev == 0
        confidence = round(min(win_rate / 100 * 1.2, 0.95), 2)

        if win_rate >= 70 and tx_count >= 30:
            mode = "auto_execution"
            risk = "medium"
        elif win_rate >= 55:
            mode = "simulation"
            risk = "medium"
        else:
            mode = "notification"
            risk = "high"

        reasons = []
        if win_rate >= 55:
            reasons.append(f"Win rate of {win_rate}% is above threshold")
        if tx_count >= 10:
            reasons.append(f"Sufficient transaction history ({tx_count} txs)")
        if mev == 0:
            reasons.append("No MEV bot behavior detected")
        if not recommended:
            reasons.append("Insufficient data or performance to recommend auto-copying")

        return {
            "recommended": recommended,
            "confidence": confidence,
            "suggested_mode": mode,
            "risk_level": risk,
            "reasons": reasons,
        }

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def _compute_confidence(self, signals: Dict) -> float:
        """Confidence in the analysis — higher when RPC data is available."""
        if not signals.get("rpc_available"):
            return 0.40
        fields_present = sum(
            1 for k in ["has_mint_authority", "holder_count", "top10_holder_pct"]
            if signals.get(k) is not None
        )
        return round(0.50 + fields_present * 0.15, 2)

    def _compute_wallet_confidence(self, signals: Dict) -> float:
        """Confidence for wallet analysis based on data richness."""
        if not signals.get("rpc_available"):
            return 0.30
        tx_count = signals.get("tx_count", 0)
        return round(min(0.40 + tx_count / 200, 0.95), 2)

    def _build_explanation(self, signals: Dict, deductions: List[Dict]) -> List[str]:
        """Build human-readable explanation list."""
        lines = []
        if not signals.get("rpc_available"):
            lines.append("On-chain data unavailable — using heuristic defaults")
        if not deductions:
            lines.append("No risk factors detected in available on-chain data")
        for d in deductions:
            lines.append(f"Risk factor: {d['label']} (-{d['deduction']} pts)")
        return lines
