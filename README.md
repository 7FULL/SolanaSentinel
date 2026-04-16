# SolanaSentinel

Advanced Solana blockchain monitoring and automation platform with real-time token detection, copy-trading, sniper bot, ML pump prediction, and anti-scam analysis.

## Features

- **Real-time Token Detection** — Monitors Pump.fun and Raydium via WebSocket for new token launches
- **Sniper Bot** — Filters tokens by liquidity, market cap, age, anti-scam score and ML signal; supports notification, simulation and auto-buy modes
- **ML Pump Predictor** — Trained Random Forest (Optuna-tuned) that scores every new token 0–100 for pump probability; retrains automatically every week
- **Anti-Scam Analyzer** — On-chain rule checks: mint authority, freeze authority, holder distribution, LP locked, creator %; configurable penalties and thresholds
- **Copy Trading** — Monitors target wallets and mirrors trades in 4 modes: notification, simulation, auto-execution, precise execution
- **Wallet Management** — Create or import wallets; private keys encrypted with Fernet at rest; per-module wallet assignment
- **Outcome Tracking** — Automatically checks 1h/6h/24h prices for every detected token and stores results in SQLite for AI training
- **Notifications** — Telegram and Discord alerts for sniper signals and copy-trade events
- **Real-time Dashboard** — WebSocket-powered frontend showing live token tracker, simulation positions, logs and metrics

## Architecture

```
SolanaSentinel/
├── backend/
│   ├── app.py                  # Flask + SocketIO entry point
│   ├── config/                 # ConfigManager (env vars + JSON)
│   ├── modules/
│   │   ├── ai_analyzer/        # Heuristic scorer + ML pump predictor + ModelTrainer
│   │   ├── anti_scam/          # On-chain rule engine
│   │   ├── copy_trading/       # Copy-trading engine
│   │   ├── sniper/             # Sniper bot engine
│   │   ├── wallet_manager/     # Wallet CRUD + encryption
│   │   ├── logging_engine/     # Centralised logging
│   │   └── notifications/      # Telegram / Discord
│   ├── services/
│   │   ├── solana_rpc/         # RPC client + WebSocket manager
│   │   ├── token_detection/    # TokenDetector + outcome tracker
│   │   ├── copy_trading/       # Wallet monitor
│   │   └── price_service/      # DexScreener price fetcher
│   ├── data/
│   │   ├── sentinel.db         # SQLite (tokens, positions, history)
│   │   ├── models/             # ML model + metadata
│   │   └── rules/              # Sniper config, anti-scam rules
│   └── utils/
├── frontend/
│   ├── index.html
│   ├── css/
│   └── js/
│       └── components/         # sniper.js, anti-scam.js, dashboard.js …
├── notebooks/
│   ├── 01_eda.ipynb            # Exploratory data analysis
│   ├── 02_train.ipynb          # Baseline RF training
│   ├── 03_tune.ipynb           # Optuna hyperparameter tuning
│   └── 04_advanced_models.ipynb# TabNet + MLP + Ensemble
└── requirements.txt
```

## Requirements

- Python 3.10+
- pip / virtual environment

Heavy ML dependencies (only needed for notebook retraining, optional for running):
- `torch`, `pytorch-tabnet`, `optuna`, `lightgbm`, `xgboost`

## Installation

```bash
# 1. Clone
git clone <repository-url>
cd SolanaSentinel

# 2. Virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — set your RPC URL, encryption password, and optionally Telegram/Discord tokens

# 5. Start
cd backend
python app.py
```

The API runs at `http://127.0.0.1:5005` and the frontend at `frontend/index.html`.

## Configuration

All sensitive values live in `.env` (never committed). See `.env.example` for the full list.

Key variables:

| Variable | Description |
|----------|-------------|
| `SOLANA_RPC_URL` | HTTP RPC endpoint (mainnet-beta recommended: use a paid provider for production) |
| `SOLANA_WS_URL` | WebSocket RPC endpoint |
| `ENCRYPTION_PASSWORD` | Password used to encrypt stored private keys |
| `TELEGRAM_BOT_TOKEN` | Optional — Telegram notification bot |
| `TELEGRAM_CHAT_ID` | Optional — Telegram chat/channel ID |
| `DISCORD_WEBHOOK_URL` | Optional — Discord webhook for alerts |

Runtime configuration (sniper filters, anti-scam rules, etc.) is managed via the dashboard UI and stored in `backend/data/rules/`.

## ML Pump Predictor

The model is a Random Forest trained with Optuna on historical detected tokens, predicting whether a token will gain ≥20% within 1 hour.

**Training pipeline** (notebooks):
1. `02_train.ipynb` — baseline model, feature engineering
2. `03_tune.ipynb` — Optuna tuning (60 trials, TimeSeriesSplit CV)
3. `04_advanced_models.ipynb` — TabNet, MLP + Focal Loss, ensemble

**Automatic retraining**: the backend retrains weekly using `ModelTrainer` (same pipeline as the notebooks, headless). A manual retrain can be triggered from the Anti-Scam → ML Pump Predictor card in the dashboard.

**Current model**: RF_tuned — ROC-AUC 0.824, PR-AUC 0.123, lift 3.4× over base rate.

## Key API Endpoints

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check |
| GET | `/api/status` | Full system status |

### Sniper
| Method | Path | Description |
|--------|------|-------------|
| GET/PUT | `/api/sniper/config` | Get / update sniper config |
| POST | `/api/sniper/start` | Start sniper |
| POST | `/api/sniper/stop` | Stop sniper |
| GET | `/api/sniper/detected-tokens` | Live token list |
| GET | `/api/sniper/simulation/positions` | Simulation positions |
| POST | `/api/sniper/simulation/reset` | Clear simulation |
| GET/PUT | `/api/sniper/config/ml` | ML filter config |

### Anti-Scam
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/anti-scam/analyze` | Analyze a token address |
| GET/PUT | `/api/anti-scam/config` | Rule configuration |
| GET/POST | `/api/anti-scam/blacklist` | Blacklist management |

### AI / ML
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/ai/status` | Model info (ROC-AUC, PR-AUC, threshold) |
| POST | `/api/ai/retrain` | Trigger manual retraining |
| GET | `/api/ai/retrain/status` | Training progress / last result |

### Copy Trading
| Method | Path | Description |
|--------|------|-------------|
| GET/PUT | `/api/copy-trading/config` | Configuration |
| POST | `/api/copy-trading/start` | Start engine |
| GET | `/api/copy-trading/monitored-wallets` | Tracked wallets |
| GET | `/api/copy-trading/history` | Trade history |

### Wallets
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/wallets` | List wallets |
| POST | `/api/wallets` | Create wallet |
| POST | `/api/wallets/import` | Import by private key / seed |
| DELETE | `/api/wallets/<id>` | Delete wallet |

## Security

- Private keys encrypted with Fernet symmetric encryption; salt stored separately
- No keys or credentials transmitted to external servers
- All sensitive files excluded from version control (see `.gitignore`)
- Configurable per-transaction limits (`MAX_TRANSACTION_AMOUNT`)
- Simulation mode available for all automated modules before committing real funds

## Disclaimer

**USE AT YOUR OWN RISK.**

Automated trading carries significant financial risk. The authors are not responsible for any losses. Always test with small amounts first. Never invest more than you can afford to lose.
