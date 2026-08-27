"""Isolated, deterministic CI control-plane learning service.

The package exposes an application factory instead of importing the module
level deployment app.  Tests and local lessons can therefore inject a
temporary SQLite path, a test-only machine credential, and a deterministic
clock without reading process configuration.
"""

from app.ci_lab.app import create_app, create_ci_lab_app
from app.ci_lab.registry import DEFAULT_DEFINITION_REGISTRY

__all__ = [
    "DEFAULT_DEFINITION_REGISTRY",
    "create_app",
    "create_ci_lab_app",
]
