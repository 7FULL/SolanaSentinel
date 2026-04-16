"""
Unit tests for NotificationManager (modules/notifications/notification_manager.py).
Uses mock HTTP to avoid real network calls.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from modules.notifications.notification_manager import NotificationManager


class MockConfig:
    def get(self, key, default=None):
        cfg = {
            'notifications.enabled': True,
            'notifications.telegram_bot_token': 'fake_token',
            'notifications.telegram_chat_id': '12345',
            'notifications.discord_webhook_url': 'https://discord.com/api/webhooks/fake',
        }
        return cfg.get(key, default)

    def get_data_path(self, subdir=''):
        import tempfile
        return __import__('pathlib').Path(tempfile.mkdtemp()) / subdir


class EmptyConfig:
    def get(self, key, default=None):
        return default

    def get_data_path(self, subdir=''):
        import tempfile
        return __import__('pathlib').Path(tempfile.mkdtemp()) / subdir


@pytest.fixture
def nm():
    return NotificationManager(MockConfig())


@pytest.fixture
def nm_empty():
    return NotificationManager(EmptyConfig())


class TestInitialization:
    def test_channels_detected(self, nm):
        status = nm.get_status()
        assert status['telegram_configured'] is True
        assert status['discord_configured'] is True
        assert status['enabled'] is True

    def test_no_channels_when_unconfigured(self, nm_empty):
        status = nm_empty.get_status()
        assert status['telegram_configured'] is False
        assert status['discord_configured'] is False


class TestGetStatus:
    def test_status_has_required_keys(self, nm):
        status = nm.get_status()
        for key in ['enabled', 'telegram_configured', 'discord_configured',
                    'sent_count', 'failed_count']:
            assert key in status

    def test_counts_start_at_zero(self, nm):
        status = nm.get_status()
        assert status['sent_count'] == 0
        assert status['failed_count'] == 0


class TestConfigure:
    def test_disable_notifications(self, nm):
        nm.configure(enabled=False)
        assert nm.enabled is False

    def test_clear_telegram(self, nm):
        nm.configure(telegram_token='', telegram_chat_id='')
        assert nm.get_status()['telegram_configured'] is False

    def test_set_discord(self, nm_empty):
        nm_empty.configure(discord_webhook_url='https://discord.com/api/webhooks/new')
        assert nm_empty.get_status()['discord_configured'] is True


class TestDispatch:
    """Verify dispatching calls the correct HTTP methods (mocked)."""

    @patch('modules.notifications.notification_manager.requests.post')
    def test_telegram_send_called(self, mock_post, nm):
        mock_post.return_value = MagicMock(status_code=200)
        nm.notify_custom("Test", "Hello", level="info")
        import time; time.sleep(0.1)  # give daemon thread a moment
        # Post should have been called for telegram at least
        assert mock_post.called

    @patch('modules.notifications.notification_manager.requests.post')
    def test_disabled_sends_nothing(self, mock_post, nm):
        nm.configure(enabled=False)
        nm.notify_custom("Test", "Hello")
        import time; time.sleep(0.1)
        assert not mock_post.called

    @patch('modules.notifications.notification_manager.requests.post')
    def test_sniper_notification_dispatched(self, mock_post, nm):
        mock_post.return_value = MagicMock(status_code=200)
        nm.notify_sniper_detection({
            'symbol': 'TEST',
            'name': 'Test Token',
            'platform': 'pump.fun',
            'liquidity': 5000,
            'market_cap': 25000,
            'risk_score': 75,
            'action_taken': 'notification',
            'address': 'TokenAddress123',
        })
        import time; time.sleep(0.1)
        assert mock_post.called

    @patch('modules.notifications.notification_manager.requests.post')
    def test_anti_scam_alert_dispatched(self, mock_post, nm):
        mock_post.return_value = MagicMock(status_code=200)
        nm.notify_anti_scam_alert(
            token_address="ScamToken123",
            risk_level="critical",
            message="Multiple rug indicators detected",
            red_flags=["Mint authority active", "Creator holds 40%"],
        )
        import time; time.sleep(0.1)
        assert mock_post.called


class TestRiskHelpers:
    """Tests for internal color/emoji helpers."""

    def test_risk_emoji_by_score(self):
        from modules.notifications.notification_manager import NotificationManager as NM
        assert NM._risk_emoji(90) == '🟢'
        assert NM._risk_emoji(65) == '🟡'
        assert NM._risk_emoji(40) == '🟠'
        assert NM._risk_emoji(20) == '🔴'

    def test_risk_color_by_score(self):
        from modules.notifications.notification_manager import NotificationManager as NM
        assert NM._risk_color(85) == 0x00C853
        assert NM._risk_color(60) == 0xFFD600
        assert NM._risk_color(35) == 0xFF6D00
        assert NM._risk_color(10) == 0xD32F2F
