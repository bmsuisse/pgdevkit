from __future__ import annotations

import shutil

import pytest

requires_podman = pytest.mark.skipif(
    shutil.which("podman") is None, reason="podman is not installed"
)
