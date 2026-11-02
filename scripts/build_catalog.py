#!/usr/bin/env python3
"""Build the Hermes plugin-catalog distribution of Hermix.

The catalog pins one exact commit, scans it, and forbids self-updating code
(rule 3), credential-file access (rule 11) and default-on usage reporting
(rule 13). The GitHub repo is more than the plugin: hub server, deploy
scripts, installer, site, evals. So the catalog gets its own minimal tree:

  * the plugin's runtime modules and its five behavioural skills, nothing else;
  * updater.py replaced by an inert stub (updates = SHA bump + `hermes plugins update`);
  * _standalone.py left out (no .env mirroring; the key lives in ctx.state);
  * _distribution.CHANNEL = "catalog" (version headers become opt-in);
  * plugin.yaml's requires_hermes raised to the first release with ctx.state;
  * catalog/README.md as the README (no curl | bash).

Usage:
  python scripts/build_catalog.py                  # build into build/catalog and verify
  python scripts/build_catalog.py --commit         # also commit it to the `catalog` branch
                                                   # (a git worktree) and print the SHA to pin
"""
import argparse
import ast
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRANCH = "catalog"
CATALOG_REQUIRES_HERMES = ">=0.21.6"     # first SemVer release; ctx.state exists there

RUNTIME_MODULES = [
    "__init__", "_config", "_distribution", "briefing", "client", "commands",
    "dossier", "envoy", "envoy_profile", "judgement", "matchmaker", "migrate",
    "mock_backend", "profile", "remote_config", "render", "response",
    "sanitize", "service", "share", "throttle", "tools",
]
SKILLS = ["hermix-context", "hermix-voice", "hermix-onboarding",
          "hermix-envoy-protocol", "hermix-delivery"]
EXTRA_FILES = {"LICENSE": "LICENSE", "SECURITY.md": "SECURITY.md",
               "catalog/README.md": "README.md"}

UPDATER_STUB = '''"""Catalog build: Hermix never updates itself.

The Hermes plugin catalog pins an exact, reviewed commit. New versions arrive
as a reviewed SHA bump in the catalog plus `hermes plugins update hermix`.
This stub keeps the interface the rest of the plugin calls.
"""


def enabled() -> bool:
    return False


def active_version() -> str:
    return "{version}"


def local_revision() -> str:
    return ""


def check_and_update(force: bool = False) -> dict:
    return {{"updated": False,
             "reason": "catalog install: run `hermes plugins update hermix`"}}
'''

# Things the catalog build must never contain. Checked on the built tree.
FORBIDDEN = [
    (re.compile(r"\bsubprocess\b"), "spawns processes"),
    (re.compile(r"git\s+pull|\"pull\"|'pull'"), "self-update"),
    (re.compile(r"[\"'][^\"'\n]*\.env\b[^\"'\n]*[\"']"), "names a credential file"),
    (re.compile(r"curl[^\n|]*\|\s*(?:ba|z|k|da)?sh\b"), "curl | sh"),
    (re.compile(r"_standalone"), "references the standalone module",),
]
# `_standalone` is allowed only in _config's guarded import.
FORBIDDEN_ALLOW = {
    ("_config.py", "references the standalone module"),
    # The envoy profile is a Hermes profile the plugin creates for its envoy.
    # It writes that profile's .env EMPTY and re-checks it stays credential-free,
    # so the envoy can never hold a key. Disclosed in the catalog PR.
    ("envoy_profile.py", "names a credential file"),
}


def manifest_version(text: str) -> str:
    m = re.search(r"^version:\s*([^\s#]+)", text, re.M)
    return m.group(1).strip("\"'") if m else ""


def build(out: pathlib.Path) -> str:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for mod in RUNTIME_MODULES:
        shutil.copy2(ROOT / f"{mod}.py", out / f"{mod}.py")
    for sk in SKILLS:
        (out / "skills" / sk).mkdir(parents=True)
        shutil.copy2(ROOT / "skills" / sk / "SKILL.md", out / "skills" / sk / "SKILL.md")
    for src, dst in EXTRA_FILES.items():
        shutil.copy2(ROOT / src, out / dst)

    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    version = manifest_version(manifest)
    manifest, n = re.subn(r'^requires_hermes:.*$',
                          f'requires_hermes: "{CATALOG_REQUIRES_HERMES}"', manifest, flags=re.M)
    assert n == 1, "plugin.yaml must declare requires_hermes"
    manifest = manifest.replace(
        "or the optional hermix-sidecar systemd unit) ",
        "") .replace("thread per gateway (", "thread per gateway ")
    (out / "plugin.yaml").write_text(manifest, encoding="utf-8")

    dist = (out / "_distribution.py").read_text(encoding="utf-8")
    dist, n = re.subn(r'^CHANNEL = "standalone"$', 'CHANNEL = "catalog"', dist, flags=re.M)
    assert n == 1, "_distribution.CHANNEL not found"
    (out / "_distribution.py").write_text(dist, encoding="utf-8")
    (out / "updater.py").write_text(UPDATER_STUB.format(version=version), encoding="utf-8")
    return version


def verify(out: pathlib.Path) -> list:
    problems = []
    for f in sorted(out.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        try:
            ast.parse(text)
        except SyntaxError as e:
            problems.append(f"{f.name}: does not parse ({e})")
        for i, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for rx, why in FORBIDDEN:
                if rx.search(line) and (f.name, why) not in FORBIDDEN_ALLOW:
                    problems.append(f"{f.name}:{i}: {why}: {line.strip()[:90]}")
    if (out / "_standalone.py").exists():
        problems.append("_standalone.py must not ship")
    if 'CHANNEL = "catalog"' not in (out / "_distribution.py").read_text():
        problems.append("_distribution.CHANNEL is not catalog")
    for f in sorted(out.rglob("*")):
        if f.is_file() and f.suffix not in (".py", ".md", ".yaml", "") :
            problems.append(f"unexpected file type: {f.relative_to(out)}")
    return problems


ENTRY = ROOT / "catalog" / "hermix.yaml"


def write_entry(sha: str, version: str, image_sha: str, out: pathlib.Path) -> str:
    """The file to add as plugin-catalog/hermix.yaml in NousResearch/hermes-agent.
    Capabilities are read from the built manifest so they cannot drift (rule 6)."""
    manifest = (out / "plugin.yaml").read_text(encoding="utf-8")

    def lst(key):
        m = re.search(rf"^{key}:\n((?:  - .*\n)+)", manifest, re.M)
        items = [ln.strip()[2:] for ln in m.group(1).splitlines()] if m else []
        return "\n".join(f"    - {i}" for i in items) or "    []"

    text = f"""name: hermix
title: Hermix
repo: https://github.com/larvuz2/hermix
sha: {sha}
description: Social media for agents. Your agent meets other people's agents and only comes back when it finds something real. The private assistant never joins. No API key.
maintainer: larvuz2
tier: community
category: general
requires_hermes: "{CATALOG_REQUIRES_HERMES}"
docs_url: https://hermix.dev
version: "{version}"
image: https://raw.githubusercontent.com/larvuz2/hermix/{image_sha}/catalog/banner.png
platforms: [linux, macos]
known_issues:
  - "Needs a gateway restart (or a new CLI session) after install before /hermix appears."
capabilities:
  provides_tools:
{lst("provides_tools")}
  provides_hooks:
{lst("provides_hooks")}
  provides_middleware: []
  requires_env: []
"""
    ENTRY.write_text(text, encoding="utf-8")
    return text


def _git(*args, cwd=ROOT):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(out: pathlib.Path, version: str) -> str:
    """Commit the built tree to the `catalog` branch via a throwaway worktree."""
    src_sha = _git("rev-parse", "HEAD")
    wt = ROOT / "build" / "catalog-worktree"
    if wt.exists():
        _git("worktree", "remove", "--force", str(wt))
    have_local = subprocess.run(["git", "rev-parse", "--verify", "-q", BRANCH],
                                cwd=ROOT, capture_output=True).returncode == 0
    have_remote = subprocess.run(["git", "rev-parse", "--verify", "-q", f"origin/{BRANCH}"],
                                 cwd=ROOT, capture_output=True).returncode == 0
    if have_local:
        _git("worktree", "add", str(wt), BRANCH)
    elif have_remote:
        _git("worktree", "add", "-b", BRANCH, str(wt), f"origin/{BRANCH}")
    else:
        _git("worktree", "add", "--detach", str(wt))
        _git("checkout", "--orphan", BRANCH, cwd=wt)
    for item in wt.iterdir():
        if item.name == ".git":
            continue
        shutil.rmtree(item) if item.is_dir() else item.unlink()
    shutil.copytree(out, wt, dirs_exist_ok=True)
    _git("add", "-A", cwd=wt)
    if _git("status", "--porcelain", cwd=wt):
        _git("commit", "-q", "-m",
             f"Catalog build {version} from {src_sha[:12]}\n\n"
             f"Generated by scripts/build_catalog.py from {src_sha}.", cwd=wt)
    sha = _git("rev-parse", "HEAD", cwd=wt)
    _git("worktree", "remove", "--force", str(wt))
    return sha


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "build" / "catalog"))
    ap.add_argument("--commit", action="store_true",
                    help=f"commit the build to the '{BRANCH}' branch and print the SHA")
    args = ap.parse_args()
    out = pathlib.Path(args.out)
    version = build(out)
    problems = verify(out)
    if problems:
        print("Catalog build FAILED verification:", *problems, sep="\n  ")
        return 1
    files = sum(1 for f in out.rglob("*") if f.is_file())
    print(f"Built catalog tree {version} at {out} ({files} files) — verification passed.")
    if args.commit:
        src_sha = _git("rev-parse", "HEAD")
        sha = commit(out, version)
        write_entry(sha, version, src_sha, out)
        print(f"Committed to branch '{BRANCH}': {sha}")
        print(f"Wrote {ENTRY.relative_to(ROOT)} pinned to it (banner from {src_sha[:12]}).")
        print(f"Next:  git push origin {BRANCH}  and commit {ENTRY.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
