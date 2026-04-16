"""
Response Formatter Utilities
Standardizes API response format across all endpoints.
"""

from flask import jsonify
from typing import Any, Dict, Optional
from datetime import datetime


def success_response(data: Any, message: Optional[str] = None, status_code: int = 200) -> tuple:
    """
    Format a successful API response.

    Args:
        data: Response data
        message: Optional success message
        status_code: HTTP status code (default: 200)

    Returns:
        Tuple of (JSON response, status code)
    """
    response = {
        'success': True,
        'data': data,
        'timestamp': datetime.utcnow().isoformat()
    }

    if message:
        response['message'] = message

    return jsonify(response), status_code


def error_response(error: str, details: Optional[Dict] = None, status_code: int = 400) -> tuple:
    """
    Format an error API response.

    Args:
        error: Error message
        details: Optional additional error details
        status_code: HTTP status code (default: 400)

    Returns:
        Tuple of (JSON response, status code)
    """
    response = {
        'success': False,
        'error': error,
        'timestamp': datetime.utcnow().isoformat()
    }

    if details:
        response['details'] = details

    return jsonify(response), status_code


def paginated_response(data: list, total: int, page: int = 1, per_page: int = 50) -> tuple:
    """
    Format a paginated API response.

    Args:
        data: List of items for current page
        total: Total number of items
        page: Current page number
        per_page: Items per page

    Returns:
        Tuple of (JSON response, status code)
    """
    total_pages = (total + per_page - 1) // per_page

    response = {
        'success': True,
        'data': data,
        'pagination': {
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages,
            'has_next': page < total_pages,
            'has_prev': page > 1
        },
        'timestamp': datetime.utcnow().isoformat()
    }

    return jsonify(response), 200
