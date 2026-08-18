"""Tests for app/main.py."""

from app.main import get_buildguard_status


def test_get_buildguard_status_returns_expected_message() -> None:
    """The status function should report that the app is running."""
    result = get_buildguard_status()
    assert result == "BuildGuard application is running"
