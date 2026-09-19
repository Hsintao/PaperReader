from __future__ import annotations

import os
import stat
import tempfile
import threading
from pathlib import Path


_LOCK = threading.RLock()


def desktop_app_root() -> Path:
    """Return the per-user, cross-platform PaperReader application directory."""
    override = os.environ.get("PAPERREADER_APP_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "PaperReader"
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / "PaperReader"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "PaperReader"


def sys_platform() -> str:
    import sys

    return sys.platform


def desktop_config_path() -> Path:
    configured = os.environ.get("PAPERREADER_ENV_FILE", "").strip()
    return Path(configured).expanduser().resolve() if configured else desktop_app_root() / ".config.env"


def read_env_file(path: Path | None = None) -> dict[str, str]:
    target = path or desktop_config_path()
    if not target.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _encode(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if any(char.isspace() for char in text) or any(char in text for char in "#='\""):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def _hide_on_windows(path: Path) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
    except Exception:
        pass


def write_env_values(
    updates: dict[str, object],
    *,
    clear: set[str] | None = None,
    path: Path | None = None,
) -> Path:
    """Atomically update the hidden env file while preserving unrelated keys."""
    target = path or desktop_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        values = read_env_file(target)
        for key in clear or set():
            values.pop(key, None)
        values.update({key: str(value) if not isinstance(value, bool) else _encode(value) for key, value in updates.items()})
        lines = [
            "# PaperReader machine-level configuration. Provider settings live in the data directory.",
            *[f"{key}={_encode(value)}" for key, value in sorted(values.items())],
            "",
        ]
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary, target)
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            _hide_on_windows(target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return target


def ensure_desktop_config(path: Path | None = None) -> Path:
    """Create a secure first-run config containing only machine-level settings."""
    target = path or desktop_config_path()
    values = read_env_file(target)
    updates: dict[str, object] = {}
    if not values.get("DATA_DIR"):
        updates["DATA_DIR"] = str(desktop_app_root() / "data")
    if not values.get("APP_ENV"):
        updates["APP_ENV"] = "desktop"
    return write_env_values(updates, path=target) if updates or not target.exists() else target
