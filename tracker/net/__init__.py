"""Outbound HTTP, subject to the Phase 0 constraints."""

from tracker.net.client import FetchResult, PoliteClient, RobotsDisallowed
from tracker.net.robots import RobotsPolicy

__all__ = ["FetchResult", "PoliteClient", "RobotsDisallowed", "RobotsPolicy"]
