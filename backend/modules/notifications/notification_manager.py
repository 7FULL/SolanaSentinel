"""
Notification Manager Module
Sends real-time alerts to Telegram and/or Discord when the sniper detects
a token, copy-trading fires an operation, or the anti-scam engine raises a
critical flag.

Configuration is read from environment variables / config:
  TELEGRAM_BOT_TOKEN  — Telegram bot token
  TELEGRAM_CHAT_ID    — Chat/group id to post to
  DISCORD_WEBHOOK_URL — Discord incoming webhook URL

All network calls are fire-and-forget (non-blocking) so a slow webhook
never delays trade execution.  Failures are logged and silently swallowed.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, List
import requests


# Maximum message length accepted by Telegram (4096) and Discord (2000)
_TELEGRAM_MAX = 4000
_DISCORD_MAX  = 1900


class NotificationManager:
    """
    Unified notification hub for Telegram and Discord.

    Usage:
        nm = NotificationManager(config)
        nm.notify_sniper_detection(token_data)
        nm.notify_copy_trade(operation_data)
        nm.notify_anti_scam_alert(token_address, risk_level, message)
    """

    def __init__(self, config):
        """
        Initialize the notification manager.

        Args:
            config: ConfigManager instance — reads TELEGRAM_* and DISCORD_* keys.
        """
        self.config = config
        self.logger = logging.getLogger(__name__)

        # Persistent config file: backend/data/config/notifications.json
        try:
            self._config_file: Path = config.get_data_path('config') / 'notifications.json'
        except Exception:
            self._config_file = Path(__file__).parent.parent.parent / 'data' / 'config' / 'notifications.json'

        # Load saved settings first, then fall back to env/config
        saved = self._load_saved_config()

        self.enabled = saved.get(
            'enabled', config.get("notifications.enabled", True)
        )
        self._tg_token: Optional[str] = (
            saved.get('telegram_bot_token')
            or config.get("notifications.telegram_bot_token")
            or None
        )
        self._tg_chat: Optional[str] = (
            saved.get('telegram_chat_id')
            or config.get("notifications.telegram_chat_id")
            or None
        )
        self._discord_url: Optional[str] = (
            saved.get('discord_webhook_url')
            or config.get("notifications.discord_webhook_url")
            or None
        )

        self._sent_count = 0
        self._failed_count = 0

        self.logger.info(
            f"NotificationManager initialized — "
            f"Telegram: {'OK' if self._tg_token and self._tg_chat else 'disabled'}, "
            f"Discord: {'OK' if self._discord_url else 'disabled'}"
        )

    # ------------------------------------------------------------------
    # Public event methods
    # ------------------------------------------------------------------

    def notify_sniper_detection(self, token_data: Dict) -> None:
        """
        Send a notification when the sniper detects a new token.

        Args:
            token_data: Dict with keys: symbol, name, platform, liquidity,
                        market_cap, risk_score, action_taken, address
        """
        symbol     = token_data.get("symbol", "???")
        name       = token_data.get("name", "Unknown")
        platform   = token_data.get("platform", "?")
        liquidity  = token_data.get("liquidity", 0)
        market_cap = token_data.get("market_cap", 0)
        risk_score = token_data.get("risk_score", 0)
        action     = token_data.get("action_taken", "detected")
        address    = token_data.get("address", "")

        risk_emoji = self._risk_emoji(risk_score)

        tg_text = (
            f"🔍 *Sniper — Token Detected*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"*{symbol}* ({name})\n"
            f"Platform: `{platform}`\n"
            f"Liquidity: `${liquidity:,.0f}`\n"
            f"Market Cap: `${market_cap:,.0f}`\n"
            f"Risk Score: {risk_emoji} `{risk_score}/100`\n"
            f"Action: `{action}`\n"
            f"Address: `{address}...`"
        )

        discord_embed = {
            "title": f"🔍 Sniper — {symbol} Detected",
            "color": self._risk_color(risk_score),
            "fields": [
                {"name": "Token",       "value": f"{symbol} ({name})",      "inline": True},
                {"name": "Platform",    "value": platform,                  "inline": True},
                {"name": "Risk Score",  "value": f"{risk_emoji} {risk_score}/100", "inline": True},
                {"name": "Liquidity",   "value": f"${liquidity:,.0f}",      "inline": True},
                {"name": "Market Cap",  "value": f"${market_cap:,.0f}",     "inline": True},
                {"name": "Action",      "value": action,                    "inline": True},
                {"name": "Address",     "value": f"`{address}...`",    "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self._dispatch(tg_text, discord_embed)

    def notify_copy_trade(self, operation_data: Dict) -> None:
        """
        Send a notification when copy-trading executes or detects an operation.

        Args:
            operation_data: Dict with keys: mode, action, token_symbol,
                            amount_sol, wallet_address, rule_name, success
        """
        mode    = operation_data.get("mode", "notification")
        action  = operation_data.get("action", "buy")
        symbol  = operation_data.get("token_symbol", "???")
        amount  = operation_data.get("amount_sol", 0.0)
        wallet  = operation_data.get("wallet_address", "")
        rule    = operation_data.get("rule_name", "")
        success = operation_data.get("success", True)
        sig     = operation_data.get("signature", "")

        status_emoji = "✅" if success else "❌"
        action_emoji = "🟢" if action == "buy" else "🔴"

        tg_text = (
            f"{status_emoji} *Copy Trade — {mode.replace('_', ' ').title()}*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Token: *{symbol}*\n"
            f"Action: {action_emoji} `{action.upper()}`\n"
            f"Amount: `{amount:.4f} SOL`\n"
            f"Rule: `{rule}`\n"
            f"Wallet: `{wallet[:16]}...`"
            + (f"\nTx: `{sig[:16]}...`" if sig else "")
        )

        discord_embed = {
            "title": f"{status_emoji} Copy Trade — {symbol} {action.upper()}",
            "color": 0x00C853 if success else 0xD32F2F,
            "fields": [
                {"name": "Mode",    "value": mode.replace("_", " ").title(), "inline": True},
                {"name": "Action",  "value": f"{action_emoji} {action.upper()}", "inline": True},
                {"name": "Amount",  "value": f"{amount:.4f} SOL",            "inline": True},
                {"name": "Rule",    "value": rule,                           "inline": True},
                {"name": "Wallet",  "value": f"`{wallet[:20]}...`",          "inline": False},
            ] + ([{"name": "Tx Sig", "value": f"`{sig[:32]}...`", "inline": False}] if sig else []),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self._dispatch(tg_text, discord_embed)

    def notify_anti_scam_alert(
        self,
        token_address: str,
        risk_level: str,
        message: str,
        red_flags: Optional[List[str]] = None,
    ) -> None:
        """
        Send an alert when the anti-scam engine flags a token.

        Args:
            token_address: Mint address of the flagged token
            risk_level: 'critical' | 'high' | 'medium' | 'low'
            message: Human-readable explanation
            red_flags: Optional list of specific risk flags detected
        """
        risk_emoji = {"critical": "🚨", "high": "⚠️", "medium": "🔶", "low": "🔵"}.get(
            risk_level, "⚠️"
        )

        flags_text = ""
        if red_flags:
            flags_text = "\n" + "\n".join(f"  • {f}" for f in red_flags[:5])

        tg_text = (
            f"{risk_emoji} *Anti-Scam Alert — {risk_level.upper()}*\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"Token: `{token_address[:20]}...`\n"
            f"Risk: `{risk_level.upper()}`\n"
            f"Reason: {message}"
            + flags_text
        )

        discord_embed = {
            "title": f"{risk_emoji} Anti-Scam Alert — {risk_level.upper()}",
            "description": message,
            "color": self._risk_color_by_level(risk_level),
            "fields": [
                {"name": "Token",      "value": f"`{token_address[:32]}...`", "inline": False},
                {"name": "Risk Level", "value": risk_level.upper(),            "inline": True},
            ] + ([
                {"name": "Red Flags", "value": "\n".join(f"• {f}" for f in red_flags[:5]), "inline": False}
            ] if red_flags else []),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        self._dispatch(tg_text, discord_embed)

    def notify_custom(self, title: str, message: str, level: str = "info") -> None:
        """
        Send a generic notification.

        Args:
            title: Notification title
            message: Notification body
            level: 'info' | 'warning' | 'error'
        """
        emoji = {"info": "ℹ️", "warning": "⚠️", "error": "🚨"}.get(level, "ℹ️")
        tg_text = f"{emoji} *{title}*\n{message}"
        discord_embed = {
            "title": f"{emoji} {title}",
            "description": message,
            "color": {"info": 0x2196F3, "warning": 0xFF9800, "error": 0xF44336}.get(
                level, 0x2196F3
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._dispatch(tg_text, discord_embed)

    # ------------------------------------------------------------------
    # Configuration management
    # ------------------------------------------------------------------

    def configure(
        self,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        """
        Update notification configuration at runtime.

        Args:
            telegram_token: Telegram bot token
            telegram_chat_id: Telegram chat id
            discord_webhook_url: Discord webhook URL
            enabled: Master on/off switch
        """
        if telegram_token is not None:
            self._tg_token = telegram_token or None
        if telegram_chat_id is not None:
            self._tg_chat = telegram_chat_id or None
        if discord_webhook_url is not None:
            self._discord_url = discord_webhook_url or None
        if enabled is not None:
            self.enabled = enabled

        self._save_config()

        self.logger.info(
            "Notification config updated — "
            f"Telegram: {'OK' if self._tg_token and self._tg_chat else 'off'}, "
            f"Discord: {'OK' if self._discord_url else 'off'}"
        )

    def get_status(self) -> Dict:
        """Return current notification status."""
        return {
            "enabled": self.enabled,
            "telegram_configured": bool(self._tg_token and self._tg_chat),
            "discord_configured": bool(self._discord_url),
            "sent_count": self._sent_count,
            "failed_count": self._failed_count,
        }

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_saved_config(self) -> Dict:
        """Load notification credentials from the persistent JSON file."""
        try:
            if self._config_file.exists():
                with open(self._config_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            self.logger.debug(f"Could not load notification config: {e}")
        return {}

    def _save_config(self) -> None:
        """Persist current notification credentials to JSON file."""
        try:
            os.makedirs(self._config_file.parent, exist_ok=True)
            data = {
                'enabled': self.enabled,
                'telegram_bot_token': self._tg_token or '',
                'telegram_chat_id': self._tg_chat or '',
                'discord_webhook_url': self._discord_url or '',
            }
            with open(self._config_file, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            self.logger.error(f"Failed to save notification config: {e}")

    # ------------------------------------------------------------------
    # Delivery internals
    # ------------------------------------------------------------------

    def _dispatch(self, tg_text: str, discord_embed: Dict) -> None:
        """
        Fire notifications to all configured channels in background threads.
        """
        if not self.enabled:
            return

        if self._tg_token and self._tg_chat:
            t = threading.Thread(
                target=self._send_telegram,
                args=(tg_text,),
                daemon=True,
            )
            t.start()

        if self._discord_url:
            t = threading.Thread(
                target=self._send_discord,
                args=(discord_embed,),
                daemon=True,
            )
            t.start()

    def _send_telegram(self, text: str) -> None:
        """Send a message via Telegram Bot API (MarkdownV2 not used to avoid escaping)."""
        try:
            # Use Markdown parse mode for basic formatting
            url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
            payload = {
                "chat_id": self._tg_chat,
                "text": text[:_TELEGRAM_MAX],
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                self._sent_count += 1
                self.logger.debug("Telegram notification sent")
            else:
                self._failed_count += 1
                self.logger.warning(
                    f"Telegram API error {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            self._failed_count += 1
            self.logger.error(f"Telegram send failed: {e}")

    def _send_discord(self, embed: Dict) -> None:
        """Send an embed to a Discord webhook."""
        try:
            payload = {"embeds": [embed]}
            resp = requests.post(self._discord_url, json=payload, timeout=10)
            if resp.status_code in (200, 204):
                self._sent_count += 1
                self.logger.debug("Discord notification sent")
            else:
                self._failed_count += 1
                self.logger.warning(
                    f"Discord webhook error {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as e:
            self._failed_count += 1
            self.logger.error(f"Discord send failed: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _risk_emoji(score: int) -> str:
        if score >= 80: return "🟢"
        if score >= 55: return "🟡"
        if score >= 30: return "🟠"
        return "🔴"

    @staticmethod
    def _risk_color(score: int) -> int:
        if score >= 80: return 0x00C853
        if score >= 55: return 0xFFD600
        if score >= 30: return 0xFF6D00
        return 0xD32F2F

    @staticmethod
    def _risk_color_by_level(level: str) -> int:
        return {
            "low": 0x00C853,
            "medium": 0xFFD600,
            "high": 0xFF6D00,
            "critical": 0xD32F2F,
        }.get(level, 0x2196F3)
