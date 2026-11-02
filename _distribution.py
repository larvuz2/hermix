"""Which build of Hermix this is.

"standalone" is the install.sh distribution from GitHub: it may keep its own
checkout current (updater.py) and keeps its hub key in ~/.hermes/.env so the
optional systemd sidecar can read it.

"catalog" is the build pinned in the Hermes plugin catalog
(scripts/build_catalog.py rewrites this line). It never updates itself, which is
the catalog's rule: updates arrive as a reviewed SHA bump plus
`hermes plugins update hermix`. It stores its key only through Hermes
(ctx.state), and it sends version info to the hub only if the user opts in.
"""
CHANNEL = "catalog"


def is_catalog() -> bool:
    return CHANNEL == "catalog"
