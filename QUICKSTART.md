# Quick Start Guide

## Prerequisites

- Python 3.10+
- A Solana RPC endpoint (public mainnet or paid provider like Helius / QuickNode)

---

## 1. Install

```bash
git clone <repository-url>
cd SolanaSentinel

python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

---

## 2. Configure

```bash
cp .env.example .env
```

Open `.env` and set at minimum:

```env
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com
SOLANA_WS_URL=wss://api.mainnet-beta.solana.com
ENCRYPTION_PASSWORD=choose_a_strong_password
```

For notifications (optional):
```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

---

## 3. Start the backend

```bash
cd backend
python app.py
```

You should see:
```
=========================================
     SolanaSentinel Backend API
=========================================
  Status: Running
  Host:   127.0.0.1
  Port:   5005
=========================================
[AI-ANALYZER] ML model loaded: RF_tuned  ROC-AUC=0.824  PR-AUC=0.123
[SCHEDULER] Weekly ML retrain scheduled (every 7 days)
```

---

## 4. Open the dashboard

Open `frontend/index.html` directly in your browser (no server needed — it talks to the Flask API at port 5005).

---

## 5. Set up your wallet

1. Go to **Wallets** section
2. Click **Create Wallet** or **Import** (paste private key or seed phrase)
3. Set it as the active wallet

---

## 6. Start the Sniper (simulation mode)

1. Go to **Sniper** section
2. Click the settings icon → verify mode is set to **Simulation**
3. Click **Start Sniper**
4. New tokens from Pump.fun and Raydium will appear in the token tracker with their ML score (0–100)

Run in simulation for a few days before enabling real auto-buy.

---

## 7. ML Pump Predictor

The ML model is pre-trained and loaded automatically. To check its status:

- Go to **Anti-Scam** section → **ML Pump Predictor** card
- Shows model metrics (ROC-AUC, PR-AUC, threshold) and last training date
- Click **Retrain Now** to trigger a manual retraining with your collected data
- Retraining also runs automatically every 7 days

To train from notebooks (first time or after major data collection):
```bash
cd notebooks
jupyter notebook
# Run in order: 02_train.ipynb → 03_tune.ipynb → 04_advanced_models.ipynb
```

---

## Common Issues

**Port already in use**
```
Address already in use: 5005
```
Change `FLASK_PORT=5005` in `.env` or kill the existing process.

**ML model not loading**
```
ML model not found — skipping
```
Run `notebooks/02_train.ipynb` first. The model needs at least 500 labeled tokens (tokens with 24h outcome data). Let the sniper run for 1–2 days to collect enough data.

**WebSocket disconnects frequently**
Switch to a paid RPC provider. Public endpoints rate-limit WebSocket connections heavily.

**No tokens detected**
Check that the sniper is running (green dot) and that the RPC WebSocket URL is correct. Pump.fun tokens appear within seconds of launch on a healthy connection.
