"""The Hermes plugin-catalog build (scripts/build_catalog.py).

Mirrors what the catalog's admission does to a pinned commit, without the
Hermes checkout: the tree is minimal and free of self-update / credential-file
code (rules 3, 11, 17), register() records exactly the tools and hooks the
manifest declares (rule 6, the capability probe), the key goes to Hermes'
plugin state rather than a file (rule 11), version reporting is opt-in
(rule 13), and requires_hermes is a SemVer floor (rule 14)."""
import importlib
import importlib.util
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_catalog  # noqa: E402

from tests.test_hermes_conformance import SpyContext  # noqa: E402


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("catalog") / "hermix"
    version = build_catalog.build(out)
    return out, version


def _load(out, name):
    for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[k]
    spec = importlib.util.spec_from_file_location(
        name, out / "__init__.py", submodule_search_locations=[str(out)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _manifest_list(text, key):
    block = re.search(rf"^{key}:\n((?:  - .*\n)+)", text, re.M)
    return {ln.strip()[2:] for ln in block.group(1).splitlines()} if block else set()


def test_build_passes_its_own_verification(built):
    out, _ = built
    assert build_catalog.verify(out) == []


def test_tree_is_minimal(built):
    out, _ = built
    names = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    for absent in ("install.sh", "_standalone.py", "sidecar.py", "conftest.py",
                   "skills/install-hermix/SKILL.md"):
        assert absent not in names
    assert not any(n.startswith(("backend/", "deploy/", "tests/", "site/", "evals/"))
                   for n in names)
    assert "curl" not in (out / "README.md").read_text()


def test_manifest_floor_and_version(built):
    out, version = built
    text = (out / "plugin.yaml").read_text()
    assert f'requires_hermes: "{build_catalog.CATALOG_REQUIRES_HERMES}"' in text
    assert re.fullmatch(r">=\d+\.\d+\.\d+", build_catalog.CATALOG_REQUIRES_HERMES)
    assert version == build_catalog.manifest_version((ROOT / "plugin.yaml").read_text())


def test_capability_probe_matches_manifest(built, monkeypatch, tmp_path):
    out, _ = built
    monkeypatch.setenv("HERMIX_HOME", str(tmp_path / "hx"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMIX_API_URL", "")
    pkg = _load(out, "hermix_catalog_probe")
    monkeypatch.setattr(importlib.import_module("hermix_catalog_probe.service"), "start",
                        lambda *a, **k: None)
    ctx = SpyContext()
    ctx.state = FakeState()
    pkg.register(ctx)
    text = (out / "plugin.yaml").read_text()
    assert set(ctx.tools) == _manifest_list(text, "provides_tools")
    assert set(ctx.hooks) == _manifest_list(text, "provides_hooks")


def test_catalog_build_keeps_the_key_in_plugin_state(built, monkeypatch, tmp_path):
    out, _ = built
    envf = tmp_path / "secrets.env"
    monkeypatch.setenv("HERMIX_ENV_FILE", str(envf))
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    monkeypatch.delenv("HERMIX_SHARE_VERSION", raising=False)
    pkg = _load(out, "hermix_catalog_state")
    cfg = importlib.import_module("hermix_catalog_state._config")
    state = FakeState()
    cfg.bind_state(state)
    cfg.persist_api_key("k-123")
    assert state.data["hub_api_key"] == "k-123"
    assert not envf.exists()
    cfg._KEY_CACHE.clear()
    monkeypatch.delenv("HERMIX_API_KEY", raising=False)
    assert cfg.api_key() == "k-123"           # survives a restart via state
    assert cfg.share_version() is False        # opt-in in the catalog
    upd = importlib.import_module("hermix_catalog_state.updater")
    assert upd.check_and_update(force=True)["updated"] is False
    cfg.bind_state(None)
    assert pkg is not None


def test_standalone_build_still_mirrors_to_env_and_reports_version(monkeypatch, tmp_path):
    from hermix import _config
    envf = tmp_path / ".env"
    monkeypatch.setenv("HERMIX_ENV_FILE", str(envf))
    monkeypatch.delenv("HERMIX_SHARE_VERSION", raising=False)
    state = FakeState()
    _config.bind_state(state)
    try:
        _config.persist_api_key("k-9")
    finally:
        _config.bind_state(None)
    assert state.data["hub_api_key"] == "k-9"
    assert "HERMIX_API_KEY=k-9" in envf.read_text()
    assert _config.share_version() is True
    monkeypatch.setenv("HERMIX_SHARE_VERSION", "0")
    assert _config.share_version() is False


def test_entry_capabilities_come_from_the_manifest(built, monkeypatch, tmp_path):
    out, version = built
    monkeypatch.setattr(build_catalog, "ENTRY", tmp_path / "hermix.yaml")
    text = build_catalog.write_entry("a" * 40, version, "b" * 40, out)
    manifest = (out / "plugin.yaml").read_text()
    for tool in _manifest_list(manifest, "provides_tools"):
        assert f"    - {tool}\n" in text
    assert f"sha: {'a' * 40}\n" in text and f'version: "{version}"' in text
    assert "tier: community" in text
