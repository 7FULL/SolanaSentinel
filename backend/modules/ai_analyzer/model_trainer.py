"""
Model Trainer
=============
Headless, thread-safe training pipeline for the ML pump-predictor.

Replicates the logic from notebooks/02_train.ipynb and 03_tune.ipynb:
  1. Load labeled tokens from SQLite (outcome_complete = 1)
  2. Build the same 12 features used during notebook training
  3. Run Optuna Bayesian optimisation (N_TRIALS trials) on Random Forest
  4. Evaluate final model on a temporal 20% hold-out set
  5. Persist model.joblib + model_meta.json to the models directory
  6. Call an optional on_complete(result) callback when done

Designed to be called:
  - Automatically by APScheduler (weekly cron in app.py)
  - Manually via POST /api/ai/retrain
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Training constants ────────────────────────────────────────────────────────
FEATURE_COLS = [
    "f_log_mc",
    "f_log_liq",
    "f_log_price",
    "f_liq_mc_ratio",
    "f_on_curve",
    "f_log_bc_sol",
    "f_log_bc_mc_sol",
    "f_risk_score",
    "f_is_safe",
    "f_is_pumpfun",
    "f_hour_sin",
    "f_hour_cos",
]

LABEL_THRESHOLD = 1.20  # token is a "pump" if price_1h / price_entry >= 1.20x
N_TRIALS        = 60    # Optuna trials — matches notebook setting
N_CV_SPLITS     = 4     # TimeSeriesSplit folds for cross-validation
MIN_SAMPLES     = 500   # refuse to retrain when fewer labeled rows exist


class ModelTrainer:
    """
    Manages a single background training run at a time.

    Usage::

        trainer = ModelTrainer(db_path, models_dir)
        trainer.start_training(on_complete=lambda r: ai_analyzer.reload_model())

    After training the result dict is available at ``trainer.last_result``.
    """

    def __init__(self, db_path: str, models_dir: str):
        self.db_path    = Path(db_path)
        self.models_dir = Path(models_dir)

        self._lock   = threading.Lock()
        self._thread: Optional[threading.Thread] = None

        self.is_training:  bool            = False
        self.last_result:  Optional[Dict]  = None
        self.last_trained: Optional[str]   = None  # ISO UTC timestamp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> Dict:
        """Return current trainer status (safe to poll from Flask)."""
        return {
            "is_training":   self.is_training,
            "last_trained":  self.last_trained,
            "last_result":   self.last_result,
        }

    def start_training(
        self,
        on_complete: Optional[Callable[[Dict], None]] = None,
    ) -> bool:
        """
        Kick off a background training run.

        Returns False immediately if a run is already in progress.
        Otherwise starts a daemon thread and returns True.

        Args:
            on_complete: Optional callback invoked with the result dict
                         after training finishes (or on failure).
        """
        with self._lock:
            if self.is_training:
                logger.info("Training already in progress — skipping new request")
                return False
            self.is_training = True

        self._thread = threading.Thread(
            target=self._train_worker,
            args=(on_complete,),
            daemon=True,
            name="ml-trainer",
        )
        self._thread.start()
        return True

    # ------------------------------------------------------------------
    # Internal: background thread
    # ------------------------------------------------------------------

    def _train_worker(self, on_complete: Optional[Callable]) -> None:
        """Executes in a background thread — never raises."""
        result: Dict = {}
        try:
            result = self._run_pipeline()
            self.last_result  = result
            self.last_trained = datetime.now(timezone.utc).isoformat()
            logger.info(
                "[ML-TRAINER] Done: %s  ROC-AUC=%.3f  PR-AUC=%.3f  "
                "threshold=%.3f  lift=%.1fx",
                result.get("model_name"),
                result.get("roc_auc", 0),
                result.get("pr_auc", 0),
                result.get("threshold", 0),
                result.get("lift", 0),
            )
        except Exception as exc:
            logger.error("[ML-TRAINER] Training failed: %s", exc, exc_info=True)
            result = {"success": False, "error": str(exc)}
            self.last_result = result
        finally:
            self.is_training = False

        if on_complete:
            try:
                on_complete(result)
            except Exception as exc:
                logger.error("[ML-TRAINER] on_complete callback failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal: full training pipeline
    # ------------------------------------------------------------------

    def _run_pipeline(self) -> Dict:
        """
        Full pipeline:
          load data → feature engineering → Optuna RF tuning →
          final eval → save model + meta → return result dict
        """
        import pandas as pd
        import optuna
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import (
            average_precision_score,
            precision_recall_curve,
            roc_auc_score,
        )
        from sklearn.model_selection import TimeSeriesSplit
        import joblib

        optuna.logging.set_verbosity(optuna.logging.WARNING)

        # ── 1. Load labeled data ─────────────────────────────────────────
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

        conn = sqlite3.connect(str(self.db_path))
        try:
            df = pd.read_sql_query(
                """
                SELECT id, source, detected_at,
                       initial_liquidity, market_cap, volume_1h,
                       COALESCE(NULLIF(price_usd, 0), latest_price_usd) AS price_usd,
                       bonding_curve_complete, bonding_curve_real_sol,
                       bonding_curve_mc_sol, risk_score, risk_level,
                       outcome_price_1h
                FROM detected_tokens
                WHERE outcome_complete = 1
                  AND COALESCE(NULLIF(price_usd, 0), latest_price_usd) IS NOT NULL
                  AND COALESCE(NULLIF(price_usd, 0), latest_price_usd) >  0
                  AND outcome_price_1h IS NOT NULL
                """,
                conn,
            )
        finally:
            conn.close()

        total = len(df)
        if total < MIN_SAMPLES:
            raise RuntimeError(
                f"Insufficient labeled samples ({total} < {MIN_SAMPLES}). "
                "Collect more data before retraining."
            )

        logger.info("[ML-TRAINER] Loaded %d labeled samples from DB", total)

        # ── 2. Feature engineering ───────────────────────────────────────
        eps = 1e-12
        df["ratio_1h"] = df["outcome_price_1h"] / (df["price_usd"] + eps)
        df["label"]    = (df["ratio_1h"] >= LABEL_THRESHOLD).astype(int)

        df["f_log_mc"]        = np.log1p(df["market_cap"].clip(lower=0))
        df["f_log_liq"]       = np.log1p(df["initial_liquidity"].clip(lower=0))
        df["f_log_price"]     = np.log1p(df["price_usd"].clip(lower=0))
        df["f_liq_mc_ratio"]  = df["initial_liquidity"] / (df["market_cap"] + eps)
        df["f_on_curve"]      = (df["bonding_curve_complete"] == 0).astype(float)
        df["f_log_bc_sol"]    = np.log1p(
            df["bonding_curve_real_sol"].fillna(0).clip(lower=0)
        )
        df["f_log_bc_mc_sol"] = np.log1p(
            df["bonding_curve_mc_sol"].fillna(0).clip(lower=0)
        )
        df["f_risk_score"]    = df["risk_score"].fillna(50)
        df["f_is_safe"]       = df["risk_level"].isin(["safe", "low"]).astype(float)
        df["f_is_pumpfun"]    = (df["source"] == "pumpfun").astype(float)
        dt = pd.to_datetime(df["detected_at"])
        df["f_hour_sin"]      = np.sin(2 * np.pi * dt.dt.hour / 24)
        df["f_hour_cos"]      = np.cos(2 * np.pi * dt.dt.hour / 24)

        # Temporal sort — critical to avoid data leakage
        df_s = df.sort_values("detected_at").reset_index(drop=True)
        X    = df_s[FEATURE_COLS].fillna(0).values
        y    = df_s["label"].values

        split              = int(len(df_s) * 0.80)
        X_train, X_test    = X[:split], X[split:]
        y_train, y_test    = y[:split], y[split:]

        n_pos = int(y_train.sum())
        n_neg = int((y_train == 0).sum())
        base  = float(y_test.mean())

        logger.info(
            "[ML-TRAINER] Train=%d (%d pos, %.1f%%)  Test=%d (%.1f%% pos)",
            len(X_train), n_pos, y_train.mean() * 100,
            len(X_test),  base  * 100,
        )

        # ── 3. Optuna RF hyperparameter tuning ───────────────────────────
        tscv = TimeSeriesSplit(n_splits=N_CV_SPLITS)

        def _cv_pr_auc(params: Dict) -> float:
            """Mean PR-AUC across temporal CV folds."""
            scores = []
            for tr_idx, val_idx in tscv.split(X_train):
                m = RandomForestClassifier(**params)
                m.fit(X_train[tr_idx], y_train[tr_idx])
                proba = m.predict_proba(X_train[val_idx])[:, 1]
                scores.append(average_precision_score(y_train[val_idx], proba))
            return float(np.mean(scores))

        def _objective(trial: optuna.Trial) -> float:
            params = dict(
                n_estimators     = trial.suggest_int("n_estimators", 200, 800, step=100),
                max_depth        = trial.suggest_int("max_depth", 4, 16),
                min_samples_leaf = trial.suggest_int("min_samples_leaf", 1, 20),
                max_features     = trial.suggest_float("max_features", 0.3, 1.0),
                class_weight     = "balanced_subsample",
                random_state     = 42,
                n_jobs           = -1,
            )
            return _cv_pr_auc(params)

        logger.info("[ML-TRAINER] Starting Optuna RF tuning (%d trials)...", N_TRIALS)
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42),
        )
        study.optimize(_objective, n_trials=N_TRIALS)

        best_params = dict(study.best_params)
        best_params.update(
            {"class_weight": "balanced_subsample", "random_state": 42, "n_jobs": -1}
        )
        logger.info(
            "[ML-TRAINER] Optuna done — best CV PR-AUC=%.4f  params=%s",
            study.best_value, best_params,
        )

        # ── 4. Final evaluation on hold-out test set ─────────────────────
        model = RandomForestClassifier(**best_params)
        model.fit(X_train, y_train)
        proba = model.predict_proba(X_test)[:, 1]

        roc = float(roc_auc_score(y_test, proba))
        ap  = float(average_precision_score(y_test, proba))

        prec_arr, _, thresh_arr = precision_recall_curve(y_test, proba)
        valid     = prec_arr[:-1] >= base * 3  # threshold for 3x base-rate precision
        threshold = float(thresh_arr[valid][0]) if valid.any() else 0.5
        signals   = int((proba >= threshold).sum())
        lift      = round(ap / max(base, 1e-9), 2)

        logger.info(
            "[ML-TRAINER] RF_tuned  ROC-AUC=%.4f  PR-AUC=%.4f  "
            "lift=%.1fx  threshold=%.4f  signals=%d/%d",
            roc, ap, lift, threshold, signals, len(y_test),
        )

        # ── 5. Persist model + metadata ──────────────────────────────────
        self.models_dir.mkdir(parents=True, exist_ok=True)
        model_file = "pump_predictor_rf_tuned.joblib"
        joblib.dump(model, self.models_dir / model_file)

        meta: Dict = {
            "model_name":      "RF_tuned",
            "model_file":      model_file,
            "model_type":      "gradient_boosting",
            "feature_cols":    FEATURE_COLS,
            "needs_scaling":   False,
            "threshold":       threshold,
            "label":           "pump_20pct_1h",
            "label_threshold": float(LABEL_THRESHOLD),
            "roc_auc":         roc,
            "pr_auc":          ap,
            "base_rate":       base,
            "train_samples":   int(len(X_train)),
            "test_samples":    int(len(X_test)),
            "total_samples":   total,
            "tuned":           True,
            "tuner":           "optuna",
            "n_trials":        N_TRIALS,
            "best_params":     best_params,
            "trained_at":      datetime.now(timezone.utc).isoformat(),
        }
        with open(self.models_dir / "model_meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        logger.info(
            "[ML-TRAINER] Saved %s to %s", model_file, self.models_dir
        )

        return {**meta, "success": True, "lift": lift, "signals": signals}
