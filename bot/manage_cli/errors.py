"""Errors shared by Manage CLI capability owners."""

from __future__ import annotations


class InstallLifecycleError(RuntimeError):
    """Raised when install, uninstall, or purge cannot finish safely."""
