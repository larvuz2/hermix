"""Catalog build: Hermix never updates itself.

The Hermes plugin catalog pins an exact, reviewed commit. New versions arrive
as a reviewed SHA bump in the catalog plus `hermes plugins update hermix`.
This stub keeps the interface the rest of the plugin calls.
"""


def enabled() -> bool:
    return False


def active_version() -> str:
    return "0.12.0"


def local_revision() -> str:
    return ""


def check_and_update(force: bool = False) -> dict:
    return {"updated": False,
             "reason": "catalog install: run `hermes plugins update hermix`"}
