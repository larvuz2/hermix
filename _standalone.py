"""Standalone (GitHub / install.sh) build only. Not shipped in the catalog build.

The optional systemd sidecar (deploy/agent) runs the engine outside Hermes, so it
cannot read Hermes' plugin state; it reads its hub key from the Hermes .env file
instead. This module keeps that file in step. scripts/build_catalog.py leaves
it out, and _config treats its absence as "do nothing".
"""
import os


def env_file_path() -> str:
    """Where to persist the auto-obtained key. HERMIX_ENV_FILE overrides (tests);
    else ~/.hermes/.env (HERMES_HOME honoured)."""
    override = os.environ.get("HERMIX_ENV_FILE")
    if override:
        return override
    base = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    return os.path.join(base, ".env")


def write_env_value(path: str, name: str, value: str) -> None:
    """Replace or append ``name=value`` in the env file. Best-effort."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        lines = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines()
                         if not ln.startswith(name + "=")]
        lines.append(f"{name}={value}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
