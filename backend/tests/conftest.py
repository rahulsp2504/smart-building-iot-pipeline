"""
Pytest configuration for smart-building backend tests.
All tests run against a live Docker stack — no mocking.
"""
import pytest

# Mark all tests in this package as asyncio
pytest_plugins = ["pytest_asyncio"]
