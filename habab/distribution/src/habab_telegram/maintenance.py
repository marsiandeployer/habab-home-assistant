"""Explicit cold-start maintenance flag; absent means normal operation."""
from pathlib import Path

from .const import DOMAIN

MAINTENANCE_FILE = ".habab-telegram-maintenance"


def maintenance_path(hass):
    return hass.config.path(MAINTENANCE_FILE)


def maintenance_enabled(hass):
    manager = hass.data.get(DOMAIN)
    if manager is not None:
        return manager.maintenance
    return Path(maintenance_path(hass)).exists()
