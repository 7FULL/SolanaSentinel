"""
Unit tests for RuleEngine (modules/rule_engine/rule_engine.py).
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from modules.rule_engine.rule_engine import RuleEngine


class MockConfig:
    """Minimal config stub."""
    def get(self, key, default=None):
        return default

    def get_data_path(self, subdir=''):
        import tempfile
        return __import__('pathlib').Path(tempfile.mkdtemp()) / subdir


@pytest.fixture
def engine():
    return RuleEngine(MockConfig())


class TestSimpleRules:
    """Tests for single-condition rule evaluation."""

    def test_equal_operator(self, engine):
        rule = {'type': 'simple', 'field': 'action', 'operator': '==', 'value': 'buy'}
        assert engine.evaluate(rule, {'action': 'buy'}) is True
        assert engine.evaluate(rule, {'action': 'sell'}) is False

    def test_not_equal_operator(self, engine):
        rule = {'type': 'simple', 'field': 'risk', 'operator': '!=', 'value': 'high'}
        assert engine.evaluate(rule, {'risk': 'low'}) is True
        assert engine.evaluate(rule, {'risk': 'high'}) is False

    def test_greater_than(self, engine):
        rule = {'type': 'simple', 'field': 'liquidity', 'operator': '>', 'value': 1000}
        assert engine.evaluate(rule, {'liquidity': 1500}) is True
        assert engine.evaluate(rule, {'liquidity': 500}) is False

    def test_greater_than_or_equal(self, engine):
        rule = {'type': 'simple', 'field': 'score', 'operator': '>=', 'value': 50}
        assert engine.evaluate(rule, {'score': 50}) is True
        assert engine.evaluate(rule, {'score': 49}) is False

    def test_less_than(self, engine):
        rule = {'type': 'simple', 'field': 'market_cap', 'operator': '<', 'value': 100000}
        assert engine.evaluate(rule, {'market_cap': 50000}) is True
        assert engine.evaluate(rule, {'market_cap': 200000}) is False

    def test_in_operator(self, engine):
        rule = {'type': 'simple', 'field': 'platform', 'operator': 'in', 'value': ['pump.fun', 'raydium']}
        assert engine.evaluate(rule, {'platform': 'pump.fun'}) is True
        assert engine.evaluate(rule, {'platform': 'uniswap'}) is False

    def test_not_in_operator(self, engine):
        rule = {'type': 'simple', 'field': 'platform', 'operator': 'not_in', 'value': ['scam_dex']}
        assert engine.evaluate(rule, {'platform': 'raydium'}) is True
        assert engine.evaluate(rule, {'platform': 'scam_dex'}) is False

    def test_contains_operator(self, engine):
        rule = {'type': 'simple', 'field': 'name', 'operator': 'contains', 'value': 'sol'}
        assert engine.evaluate(rule, {'name': 'solana_inu'}) is True
        assert engine.evaluate(rule, {'name': 'ethereum_token'}) is False

    def test_nested_field_access(self, engine):
        rule = {'type': 'simple', 'field': 'filters.min_liquidity', 'operator': '>', 'value': 500}
        data = {'filters': {'min_liquidity': 1000}}
        assert engine.evaluate(rule, data) is True


class TestCompoundRules:
    """Tests for AND/OR compound rule evaluation."""

    def test_and_all_true(self, engine):
        rule = {
            'type': 'compound',
            'logic': 'AND',
            'conditions': [
                {'type': 'simple', 'field': 'x', 'operator': '>', 'value': 0},
                {'type': 'simple', 'field': 'y', 'operator': '<', 'value': 100},
            ],
        }
        assert engine.evaluate(rule, {'x': 5, 'y': 50}) is True

    def test_and_one_false(self, engine):
        rule = {
            'type': 'compound',
            'logic': 'AND',
            'conditions': [
                {'type': 'simple', 'field': 'x', 'operator': '>', 'value': 0},
                {'type': 'simple', 'field': 'y', 'operator': '<', 'value': 100},
            ],
        }
        assert engine.evaluate(rule, {'x': 5, 'y': 200}) is False

    def test_or_one_true(self, engine):
        rule = {
            'type': 'compound',
            'logic': 'OR',
            'conditions': [
                {'type': 'simple', 'field': 'flag_a', 'operator': '==', 'value': True},
                {'type': 'simple', 'field': 'flag_b', 'operator': '==', 'value': True},
            ],
        }
        assert engine.evaluate(rule, {'flag_a': False, 'flag_b': True}) is True

    def test_or_all_false(self, engine):
        rule = {
            'type': 'compound',
            'logic': 'OR',
            'conditions': [
                {'type': 'simple', 'field': 'a', 'operator': '==', 'value': 1},
                {'type': 'simple', 'field': 'b', 'operator': '==', 'value': 2},
            ],
        }
        assert engine.evaluate(rule, {'a': 0, 'b': 0}) is False

    def test_missing_field_evaluates_to_false(self, engine):
        rule = {'type': 'simple', 'field': 'nonexistent', 'operator': '==', 'value': 'x'}
        assert engine.evaluate(rule, {'other': 'y'}) is False
