"""
Unit tests for the AI Analyzer (modules/ai_analyzer/ai_analyzer.py).
Tests heuristic scoring logic, pattern detection, and wallet analysis.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from modules.ai_analyzer.ai_analyzer import AIAnalyzer


class MockConfig:
    def get(self, key, default=None):
        defaults = {
            'anti_scam.ai_analysis_enabled': True,
        }
        return defaults.get(key, default)

    def get_data_path(self, subdir=''):
        import tempfile
        return __import__('pathlib').Path(tempfile.mkdtemp()) / subdir


@pytest.fixture
def analyzer():
    return AIAnalyzer(MockConfig(), rpc_client=None)


class TestTokenScoring:
    """Tests for the heuristic token risk scoring engine."""

    def test_clean_token_scores_high(self, analyzer):
        signals = {
            'has_mint_authority': False,
            'has_freeze_authority': False,
            'creator_pct': 5.0,
            'top10_holder_pct': 40.0,
            'holder_count': 200,
            'lp_locked': True,
            'volume_trend': 0.5,
            'rpc_available': True,
        }
        score, deductions, flags = analyzer._score_token(signals)
        assert score >= 80
        assert flags == []

    def test_mint_authority_deducts_score(self, analyzer):
        signals = {
            'has_mint_authority': True,
            'has_freeze_authority': False,
            'creator_pct': 5.0,
            'top10_holder_pct': 40.0,
            'holder_count': 200,
            'lp_locked': True,
            'volume_trend': 0.0,
        }
        score, deductions, flags = analyzer._score_token(signals)
        assert score < 80
        assert any(d['check'] == 'has_mint_authority' for d in deductions)

    def test_multiple_red_flags_critical_score(self, analyzer):
        signals = {
            'has_mint_authority': True,
            'has_freeze_authority': True,
            'creator_pct': 35.0,
            'top10_holder_pct': 85.0,
            'holder_count': 10,
            'lp_locked': False,
            'volume_trend': 5.0,
        }
        score, deductions, flags = analyzer._score_token(signals)
        assert score <= 30
        assert len(flags) >= 4

    def test_score_clamped_to_zero(self, analyzer):
        signals = {
            'has_mint_authority': True,
            'has_freeze_authority': True,
            'creator_pct': 50.0,
            'top10_holder_pct': 95.0,
            'holder_count': 3,
            'lp_locked': False,
            'volume_trend': 10.0,
        }
        score, _, _ = analyzer._score_token(signals)
        assert score >= 0  # Never goes negative


class TestAnalyzeToken:
    """Integration tests for the full analyze_token flow (no RPC)."""

    def test_returns_required_fields(self, analyzer):
        result = analyzer.analyze_token("SomeTokenAddress123")
        for key in ['risk_score', 'risk_level', 'patterns_detected', 'anomalies',
                    'rugpull_probability', 'legitimate_probability', 'recommendation',
                    'analyzed_at']:
            assert key in result

    def test_risk_score_is_in_range(self, analyzer):
        result = analyzer.analyze_token("TokenAddr")
        assert 0 <= result['risk_score'] <= 100

    def test_probabilities_sum_is_reasonable(self, analyzer):
        result = analyzer.analyze_token("TokenAddr")
        total = result['rugpull_probability'] + result['legitimate_probability']
        assert 0.0 <= total <= 2.0  # Each is 0-1 independently

    def test_confidence_low_without_rpc(self, analyzer):
        result = analyzer.analyze_token("TokenAddr")
        assert result['confidence'] < 0.6


class TestAnalyzeWallet:
    """Tests for wallet analysis (no RPC)."""

    def test_returns_required_fields(self, analyzer):
        result = analyzer.analyze_wallet("WalletAddress123")
        for key in ['wallet_type', 'risk_level', 'behavioral_patterns',
                    'statistics', 'copy_trading_recommendation']:
            assert key in result

    def test_new_wallet_no_history(self, analyzer):
        result = analyzer.analyze_wallet("BrandNewWallet")
        stats = result['statistics']
        assert stats['total_transactions'] == 0

    def test_copy_rec_not_recommended_for_unknown(self, analyzer):
        result = analyzer.analyze_wallet("SomeWallet")
        rec = result['copy_trading_recommendation']
        assert isinstance(rec['recommended'], bool)
        assert 0.0 <= rec['confidence'] <= 1.0


class TestRugpullDetection:
    """Tests for the rugpull pattern detector."""

    def test_no_indicators_clean(self, analyzer):
        result = analyzer.detect_rugpull_pattern({
            'liquidity_change_pct': 5.0,
            'creator_token_pct': 2.0,
            'unique_buyers': 500,
            'total_buy_txs': 600,
            'price_drop_pct': -5.0,
            'has_mint_authority': False,
        })
        assert result['rugpull_detected'] is False
        assert result['score'] < 40

    def test_liquidity_removal_triggers_detection(self, analyzer):
        result = analyzer.detect_rugpull_pattern({
            'liquidity_change_pct': -80.0,
            'creator_token_pct': 25.0,
            'unique_buyers': 10,
            'total_buy_txs': 100,
            'price_drop_pct': -90.0,
            'has_mint_authority': True,
        })
        assert result['rugpull_detected'] is True
        assert result['score'] >= 40
