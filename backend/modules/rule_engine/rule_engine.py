"""
Rule Engine Module
Evaluates dynamic rules and conditions for filtering and decision making.
Used across copy trading, sniper, and anti-scam modules.
"""

from typing import Dict, List, Any, Optional
from datetime import datetime


class RuleEngine:
    """
    Flexible rule evaluation engine.
    Supports complex conditions with AND/OR logic, comparisons, and custom functions.
    """

    def __init__(self, config):
        """
        Initialize the Rule Engine.

        Args:
            config: ConfigManager instance
        """
        self.config = config

    def evaluate_rule(self, rule: Dict, context: Dict) -> bool:
        """
        Evaluate a rule against provided context data.

        Args:
            rule: Rule definition with conditions
            context: Data to evaluate against

        Returns:
            True if rule passes, False otherwise
        """
        if not rule or not context:
            return False

        # Handle different rule types
        rule_type = rule.get('type', 'simple')

        if rule_type == 'simple':
            return self._evaluate_simple_rule(rule, context)
        elif rule_type == 'compound':
            return self._evaluate_compound_rule(rule, context)
        else:
            return False

    def _evaluate_simple_rule(self, rule: Dict, context: Dict) -> bool:
        """
        Evaluate a simple rule with a single condition.

        Args:
            rule: Simple rule definition
            context: Context data

        Returns:
            True if condition passes
        """
        field = rule.get('field')
        operator = rule.get('operator')
        value = rule.get('value')

        if not field or not operator:
            return False

        # Get field value from context
        context_value = self._get_nested_value(context, field)

        if context_value is None:
            return False

        # Evaluate based on operator
        return self._compare(context_value, operator, value)

    def _evaluate_compound_rule(self, rule: Dict, context: Dict) -> bool:
        """
        Evaluate a compound rule with multiple conditions.

        Args:
            rule: Compound rule with AND/OR logic
            context: Context data

        Returns:
            True if compound condition passes
        """
        logic = rule.get('logic', 'AND')
        conditions = rule.get('conditions', [])

        if not conditions:
            return False

        results = [self.evaluate_rule(cond, context) for cond in conditions]

        if logic == 'AND':
            return all(results)
        elif logic == 'OR':
            return any(results)
        else:
            return False

    def _get_nested_value(self, data: Dict, path: str) -> Any:
        """
        Get a value from nested dictionary using dot notation.

        Args:
            data: Dictionary to search
            path: Dot-separated path (e.g., 'user.profile.age')

        Returns:
            Value at path or None
        """
        keys = path.split('.')
        value = data

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None

        return value

    def _compare(self, left: Any, operator: str, right: Any) -> bool:
        """
        Compare two values using an operator.

        Args:
            left: Left operand
            operator: Comparison operator
            right: Right operand

        Returns:
            Comparison result
        """
        operators = {
            '==': lambda l, r: l == r,
            '!=': lambda l, r: l != r,
            '>': lambda l, r: l > r,
            '>=': lambda l, r: l >= r,
            '<': lambda l, r: l < r,
            '<=': lambda l, r: l <= r,
            'in': lambda l, r: l in r,
            'not_in': lambda l, r: l not in r,
            'contains': lambda l, r: r in l,
            'starts_with': lambda l, r: str(l).startswith(str(r)),
            'ends_with': lambda l, r: str(l).endswith(str(r)),
        }

        if operator in operators:
            try:
                return operators[operator](left, right)
            except (TypeError, AttributeError):
                return False

        return False

    def create_rule(self, rule_definition: Dict) -> Dict:
        """
        Create a new rule.

        Args:
            rule_definition: Rule configuration

        Returns:
            Created rule with metadata
        """
        rule = {
            **rule_definition,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat()
        }

        return rule

    def validate_rule(self, rule: Dict) -> tuple[bool, Optional[str]]:
        """
        Validate a rule definition.

        Args:
            rule: Rule to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not rule:
            return False, "Rule is empty"

        rule_type = rule.get('type', 'simple')

        if rule_type == 'simple':
            if 'field' not in rule:
                return False, "Simple rule must have 'field'"
            if 'operator' not in rule:
                return False, "Simple rule must have 'operator'"

        elif rule_type == 'compound':
            if 'conditions' not in rule or not rule['conditions']:
                return False, "Compound rule must have 'conditions'"
            if 'logic' not in rule:
                return False, "Compound rule must have 'logic' (AND/OR)"

        return True, None

    def test_rule(self, rule: Dict, test_context: Dict) -> Dict:
        """
        Test a rule against sample context (for debugging).

        Args:
            rule: Rule to test
            test_context: Test data

        Returns:
            Test results
        """
        result = self.evaluate_rule(rule, test_context)

        return {
            'rule': rule,
            'context': test_context,
            'passed': result,
            'tested_at': datetime.utcnow().isoformat()
        }
