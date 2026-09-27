"""Prepare the dedicated Google publisher browser for managed-Chrome mode."""

import os
from pathlib import Path

from cptr.env import CONFIG_FILE
from cptr.utils.config import invalidate_config_cache, load_config, save_config


def remove_stale_chromium_locks() -> None:
    """Remove only process locks left behind when the container was replaced."""
    profiles = Path(os.environ.get("CPTR_DATA_DIR", "/data")) / "browser-profiles"
    for profile in profiles.glob("*") if profiles.exists() else ():
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock = profile / name
            if lock.is_symlink() or lock.is_file():
                lock.unlink(missing_ok=True)


def remove_stale_display_locks() -> None:
    """Remove only the fixed X99 runtime files left after an in-place restart."""
    for path in (Path("/tmp/.X99-lock"), Path("/tmp/.X11-unix/X99")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def remove_invalid_quality_profile_override() -> None:
    """Remove only the unsupported dict literal written by an earlier bootstrap."""
    if not CONFIG_FILE.exists():
        return
    lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    cleaned = [
        line for line in lines
        if not line.lstrip().startswith('"browser.quality.profiles"')
    ]
    if cleaned != lines:
        CONFIG_FILE.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
        invalidate_config_cache()


def main() -> None:
    remove_stale_chromium_locks()
    remove_stale_display_locks()
    remove_invalid_quality_profile_override()
    config = load_config()
    app_config = config.setdefault("app_config", {})
    app_config["browser.tab_default_mode"] = "chrome"
    app_config["browser.quality.default"] = "low"
    app_config["browser.quality.max_resolution"] = 720
    app_config["browser.quality.max_bitrate"] = 2_000_000
    save_config(config)


if __name__ == "__main__":
    main()
