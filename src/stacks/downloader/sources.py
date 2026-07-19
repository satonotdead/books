"""Helpers for selecting which download sources Stacks may use."""


def is_slow_download_mirror(mirror: dict) -> bool:
    """Return True when a mirror points at Anna's Archive slow_download."""
    mirror_type = mirror.get('type')
    url = mirror.get('url', '')
    return mirror_type == 'slow_download' or '/slow_download/' in url


def filter_mirrors_for_policy(mirrors: list, allow_external_mirrors: bool = False) -> list:
    """Filter mirrors according to the configured external mirror policy."""
    if allow_external_mirrors:
        return list(mirrors)
    return [mirror for mirror in mirrors if is_slow_download_mirror(mirror)]
