"""Persistent learning adapters for integrations and automation.

This package intentionally stays separate from the in-memory domain examples.
It demonstrates durable rows and transactional state changes on one local
process.  A production worker fleet additionally needs database-native row
locking, a broker/outbox and process-independent scheduler leadership.
"""

from app.runtime.service import PersistentRuntimeService, create_runtime_service

__all__ = ["PersistentRuntimeService", "create_runtime_service"]
