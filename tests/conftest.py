"""Shared pytest fixtures ensuring clean cross-test isolation."""
from __future__ import annotations

import pytest

from autoclip_studio.download_state import download_coordinator


@pytest.fixture(autouse=True)
def _reset_download_coordinator() -> None:
    download_coordinator().reset_for_tests()
    yield
    download_coordinator().reset_for_tests()
