"""Persistent application settings (theme, last configuration)."""
from PyQt5.QtCore import QSettings

ORGANIZATION = "CanExpert"
APPLICATION = "CanExpert"
# Settings were stored under this name before the project was renamed.
LEGACY_ORGANIZATION, LEGACY_APPLICATION = "EZCan2", "KvaserCAN"
_MIGRATED_KEY = "migrated_legacy_settings"


def app_settings() -> QSettings:
    """CAN Expert settings; values saved under the legacy name are copied over once."""
    settings = QSettings(ORGANIZATION, APPLICATION)
    if not settings.value(_MIGRATED_KEY, False, type=bool):
        legacy = QSettings(LEGACY_ORGANIZATION, LEGACY_APPLICATION)
        for key in legacy.allKeys():
            if not settings.contains(key):
                settings.setValue(key, legacy.value(key))
        settings.setValue(_MIGRATED_KEY, True)
    return settings
