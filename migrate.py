"""One-time migration for the Hermies -> Hermix rename.

The rename moved this plugin's data directory from ``$HERMES_HOME/hermies`` to
``$HERMES_HOME/hermix``. That directory holds the human's DOSSIER — the private
notes, contact identity and standing intents they built during onboarding —
plus the matchmaker state: every dig, verdict, block and finding.

Left alone, an upgrading user would look brand new: un-onboarded, no dossier,
no history, and their agent would start again from an empty card. Losing that
to a naming decision would be unforgivable, so the rename carries its data.

Deliberately conservative:

* Only ever runs when the NEW directory does not exist. If both are present the
  new one is authoritative and the old one is left untouched for the human to
  inspect or delete themselves.
* COPIES rather than moves, then leaves the original in place. A rename that
  half-completes must not be able to destroy the only copy.
* Never raises. A failed migration means an agent that looks new, which is bad;
  a raised exception means a plugin that will not load at all, which is worse.
"""
import logging
import os
import pathlib
import shutil

log = logging.getLogger("hermix.migrate")

LEGACY_DIR = "hermies"
CURRENT_DIR = "hermix"


def _home_root() -> pathlib.Path:
    """The Hermes home holding our data directory (not the profiles root)."""
    base = os.environ.get("HERMIX_HOME") or os.environ.get("HERMIES_HOME")
    if base:
        # Tests and custom deployments point HERMIX_HOME straight at the data
        # directory, so there is no legacy sibling to migrate from.
        return pathlib.Path(base)
    try:
        from hermes_constants import get_hermes_home
        return pathlib.Path(get_hermes_home())
    except Exception:
        return pathlib.Path(os.path.expanduser("~/.hermes"))


def run(now=None) -> dict:
    """Copy pre-rename plugin data into the new directory. Returns a summary."""
    result = {"migrated": False, "files": 0, "from": "", "to": "", "error": ""}
    if os.environ.get("HERMIX_HOME") or os.environ.get("HERMIES_HOME"):
        return result                      # explicit data dir: nothing to move
    try:
        root = _home_root()
        old, new = root / LEGACY_DIR, root / CURRENT_DIR
        result["from"], result["to"] = str(old), str(new)
        if not old.is_dir() or new.exists():
            return result
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old, new)
        result["files"] = sum(1 for p in new.rglob("*") if p.is_file())
        result["migrated"] = True
        log.warning("hermix: carried %d file(s) of your data across the rename "
                    "(%s -> %s). The originals are untouched.",
                    result["files"], old, new)
    except Exception as e:                 # never block plugin load
        result["error"] = str(e)[:200]
        log.debug("hermix migration skipped: %s", e)
    return result
