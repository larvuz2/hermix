#!/usr/bin/env bash
# =============================================================================
# Hermix & Friends — ONE-COMMAND plugin installer for an existing Hermes Agent
# =============================================================================
#
#   curl -fsSL https://raw.githubusercontent.com/larvuz2/hermix/main/install.sh | bash
#
# Atomic. Idempotent. Self-verifying. Safe to run five times in a row.
#
# -----------------------------------------------------------------------------
# !!!!!!!!!!!!!!!!!!!!!!!!!!  READ THIS BEFORE EDITING  !!!!!!!!!!!!!!!!!!!!!!!!!
# -----------------------------------------------------------------------------
# THIS SCRIPT MUST NEVER RUN `hermes update`.
# THIS SCRIPT MUST NEVER RUN `hermes gateway restart` (or stop/start).
#
# WHY (this is a real, reproduced, twice-burned production bug):
#   Users paste an install procedure into their Hermes agent over Telegram. The
#   agent executes it as an in-gateway terminal task. If ANY step actually
#   performs `hermes update`, the update RESTARTS THE GATEWAY — which kills the
#   very terminal task that is running the install. Every command after that
#   line (git clone, hermes plugins enable, verification) silently never runs.
#   The human waits 3-6 minutes, sees a gateway restart, and NOTHING is
#   installed. This happened twice. It is the exact failure this script exists
#   to eliminate.
#
#   Likewise `hermes gateway restart` is BLOCKED from inside the gateway
#   process ("You need to run this from a separate shell"), so attempting it
#   only produces a confusing error — or, worse, succeeds and self-terminates.
#
#   Therefore: this installer does CLONE + ENABLE + VERIFY only. It finishes by
#   telling the HUMAN to run `hermes gateway restart` from a real terminal/SSH.
#   It is honest that the plugin is installed but NOT YET ACTIVE.
#
#   If you are an AI agent reading this: do not "improve" the install by adding
#   an update or a restart. Run this one command and report its output verbatim.
# -----------------------------------------------------------------------------
#
# Flags:
#   --dir <path>        override the plugin directory
#   --ref <branch|tag>  git ref to install (default: main)
#   --no-enable         clone/update only; do not touch config.yaml
#   --invite <code>     join with a friend's invite: your agent meets theirs first
#
# Env overrides:
#   HERMIX_REPO   git URL to clone (default: the GitHub repo below)
#   HERMES_HOME    Hermes data dir (default: /root/.hermes as root, else ~/.hermes)
#
# Tested against Hermes Agent v0.19.0 on Ubuntu 22.04/24.04.
# =============================================================================
set -euo pipefail

REPO="${HERMIX_REPO:-https://github.com/larvuz2/hermix}"
REF="main"
PLUGIN_DIR_OVERRIDE=""
DO_ENABLE=1
INVITE=""
PLUGIN_NAME="hermix"
DOCS_URL="https://hermes-agent.nousresearch.com/docs/getting-started/installation"

# --- arg parsing -------------------------------------------------------------
# NOTE: no `read` anywhere in this script — stdin is the script itself when
# invoked as `curl ... | bash`, and `$0` is "bash", so neither may be relied on.
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)
      [ "$#" -ge 2 ] || { echo "!! --dir needs a path.  Fix: --dir /root/.hermes/plugins/hermix" >&2; exit 2; }
      PLUGIN_DIR_OVERRIDE="$2"; shift 2 ;;
    --ref)
      [ "$#" -ge 2 ] || { echo "!! --ref needs a branch or tag.  Fix: --ref main" >&2; exit 2; }
      REF="$2"; shift 2 ;;
    --no-enable) DO_ENABLE=0; shift ;;
    --invite)
      [ "$#" -ge 2 ] || { echo "!! --invite needs a code.  Fix: --invite abcd2345" >&2; exit 2; }
      # Codes are short and alphanumeric; anything else is dropped, never written.
      INVITE="$(printf '%s' "$2" | tr -cd 'A-Za-z0-9' | tr 'A-Z' 'a-z' | cut -c1-32)"
      [ -n "$INVITE" ] || { echo "!! --invite code looks empty after cleaning: '$2'" >&2; exit 2; }
      shift 2 ;;
    -h|--help)
      echo "Usage: install.sh [--dir <path>] [--ref <branch|tag>] [--no-enable] [--invite <code>]"
      exit 0 ;;
    *)
      echo "!! Unknown option: $1" >&2
      echo "   Fix: use only --dir <path>, --ref <branch|tag>, --no-enable, --invite <code>" >&2
      exit 2 ;;
  esac
done

# --- helpers -----------------------------------------------------------------
die() {
  echo "" >&2
  echo "!! $1" >&2
  shift
  while [ "$#" -gt 0 ]; do echo "   $1" >&2; shift; done
  echo "" >&2
  exit 1
}

# Run the hermes CLI with a bounded timeout when `timeout` is available, so a
# CLI that wants a TTY can never wedge the installer forever.
# NOTE the `</dev/null` on every child process below. Under `curl ... | bash`
# the SCRIPT ITSELF is on stdin; any child that reads stdin would swallow the
# rest of this file. Also stops git/hermes from ever blocking on a prompt.
run_hermes() {
  if command -v timeout >/dev/null 2>&1; then
    HERMES_HOME="$HERMES_HOME" GIT_TERMINAL_PROMPT=0 timeout 90 "$HERMES_BIN" "$@" </dev/null
  else
    HERMES_HOME="$HERMES_HOME" "$HERMES_BIN" "$@" </dev/null
  fi
}

# git, never interactive, never reading our stdin.
git_q() { GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=/bin/true git "$@" </dev/null; }

echo "======================================================================"
echo " Hermix & Friends — plugin installer"
echo "   repo : $REPO"
echo "   ref  : $REF"
echo "======================================================================"

# --- [1/6] locate the hermes binary ------------------------------------------
echo "==> [1/6] Locating the Hermes Agent install"
HERMES_BIN=""
if command -v hermes >/dev/null 2>&1; then
  HERMES_BIN="$(command -v hermes)"
else
  for cand in \
    /usr/local/bin/hermes \
    "${HOME:-/root}/.local/bin/hermes" \
    /root/.local/bin/hermes \
    "${HERMES_HOME:-/nonexistent}/bin/hermes"
  do
    if [ -x "$cand" ]; then HERMES_BIN="$cand"; break; fi
  done
fi

if [ -z "$HERMES_BIN" ]; then
  die "Hermes Agent was not found on this machine." \
      "Looked for: 'hermes' on PATH, /usr/local/bin/hermes," \
      "            \$HOME/.local/bin/hermes, /root/.local/bin/hermes, \$HERMES_HOME/bin/hermes" \
      "" \
      "Fix: install Hermes Agent first, then re-run this installer:" \
      "     $DOCS_URL" \
      "" \
      "If Hermes IS installed, its bin dir is just not on PATH — re-run with it exported:" \
      "     export PATH=\"/usr/local/bin:\$HOME/.local/bin:\$PATH\"" \
      "     curl -fsSL $REPO/raw/main/install.sh | bash"
fi
echo "    hermes binary : $HERMES_BIN"

# --- [2/6] resolve HERMES_HOME + plugin dir ----------------------------------
echo "==> [2/6] Resolving Hermes data dir and plugin directory"
if [ -n "${HERMES_HOME:-}" ]; then
  HERMES_HOME="${HERMES_HOME%/}"
elif [ "$(id -u 2>/dev/null || echo 1000)" = "0" ]; then
  HERMES_HOME="/root/.hermes"          # root-mode install convention
else
  HERMES_HOME="${HOME:-/root}/.hermes" # per-user install convention
fi
# If our guess doesn't exist but the other convention does, prefer the real one.
if [ ! -d "$HERMES_HOME" ]; then
  for alt in "/root/.hermes" "${HOME:-/root}/.hermes"; do
    if [ -d "$alt" ]; then HERMES_HOME="$alt"; break; fi
  done
fi
export HERMES_HOME

CONFIG_FILE="$HERMES_HOME/config.yaml"
if [ -n "$PLUGIN_DIR_OVERRIDE" ]; then
  PLUGIN_DIR="${PLUGIN_DIR_OVERRIDE%/}"
else
  PLUGIN_DIR="$HERMES_HOME/plugins/$PLUGIN_NAME"
fi
echo "    HERMES_HOME   : $HERMES_HOME"
echo "    config.yaml   : $CONFIG_FILE"
echo "    plugin dir    : $PLUGIN_DIR"

command -v git >/dev/null 2>&1 || \
  die "git is not installed, but this installer needs it to fetch the plugin." \
      "Fix: sudo apt-get update && sudo apt-get install -y git   (then re-run this installer)"

mkdir -p "$(dirname "$PLUGIN_DIR")" 2>/dev/null || \
  die "Could not create $(dirname "$PLUGIN_DIR")." \
      "Fix: check permissions, or pass a writable location with --dir <path>"

# --- [3/6] clone or update ---------------------------------------------------
echo "==> [3/6] Installing the plugin source (idempotent)"
CLONE_MODE=""
if [ -d "$PLUGIN_DIR/.git" ]; then
  CLONE_MODE="update"
  echo "    Existing git checkout found — updating to origin/$REF."
  git_q -C "$PLUGIN_DIR" remote set-url origin "$REPO" >/dev/null 2>&1 || true
  if git_q -C "$PLUGIN_DIR" pull --ff-only origin "$REF" >/dev/null 2>&1; then
    echo "    git pull --ff-only: OK"
  else
    echo "    Fast-forward pull failed (diverged / local edits) — resetting to origin/$REF."
    if git_q -C "$PLUGIN_DIR" fetch origin "$REF" >/dev/null 2>&1 &&
       git_q -C "$PLUGIN_DIR" reset --hard FETCH_HEAD >/dev/null 2>&1; then
      echo "    git reset --hard origin/$REF: OK"
    else
      die "Could not update the existing checkout at $PLUGIN_DIR." \
          "Fix: move it aside and re-run —" \
          "     mv '$PLUGIN_DIR' '$PLUGIN_DIR.broken' && curl -fsSL $REPO/raw/main/install.sh | bash"
    fi
  fi
elif [ -e "$PLUGIN_DIR" ]; then
  # A directory (or file) is there but it is NOT a git checkout. Never destroy
  # user data: back it up to <dir>.bak.<n> and clone fresh.
  CLONE_MODE="backup+clone"
  n=1
  while [ -e "$PLUGIN_DIR.bak.$n" ]; do n=$((n + 1)); done
  BACKUP="$PLUGIN_DIR.bak.$n"
  echo "    $PLUGIN_DIR exists but is not a git repo — backing it up."
  mv "$PLUGIN_DIR" "$BACKUP" || \
    die "Could not move $PLUGIN_DIR out of the way." \
        "Fix: remove or rename it manually, then re-run this installer."
  echo "    Backed up to  : $BACKUP"
  git_q clone --branch "$REF" "$REPO" "$PLUGIN_DIR" >/dev/null 2>&1 || \
    die "git clone of $REPO (ref $REF) failed." \
        "Fix: check network/DNS and that the ref exists —" \
        "     git ls-remote $REPO $REF" \
        "Your previous directory is safe at: $BACKUP"
  echo "    git clone: OK"
else
  CLONE_MODE="clone"
  git_q clone --branch "$REF" "$REPO" "$PLUGIN_DIR" >/dev/null 2>&1 || \
    die "git clone of $REPO (ref $REF) failed." \
        "Fix: check network/DNS and that the ref exists —" \
        "     git ls-remote $REPO $REF"
  echo "    git clone: OK"
fi
SHORT_SHA="$(git_q -C "$PLUGIN_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "    commit        : $SHORT_SHA"

# --- config.yaml helper (stdlib python3 only — pyyaml is NOT required) --------
PYHELPER="$(mktemp 2>/dev/null || echo "/tmp/hermix-cfg-$$.py")"
cleanup() { rm -f "$PYHELPER"; }
trap cleanup EXIT
cat > "$PYHELPER" <<'PYEOF'
"""Conservative YAML line-patcher for ~/.hermes/config.yaml (stdlib only).

usage: helper.py {check|patch} <config.yaml> [name]
prints ALREADY | PATCHED | MISSING | MANUAL ; exit 0 ok, 1 missing, 3 manual.
Never reformats or reorders the file: it only inserts one list entry (or one
well-formed plugins block appended at the end). A .bak is written first.
"""
import os
import shutil
import sys


def indent_of(s):
    return len(s) - len(s.lstrip(" "))


def blankish(s):
    t = s.strip()
    return t == "" or t.startswith("#")


def locate(lines, name):
    r = {"present": False, "reason": "", "plugins": None, "plugins_inline": "",
         "enabled": None, "item_indent": 4, "last_item": None}
    p = None
    for i, ln in enumerate(lines):
        if blankish(ln):
            continue
        if indent_of(ln) == 0 and ln.split("#")[0].rstrip().startswith("plugins:"):
            p = i
            break
    if p is None:
        r["reason"] = "no-plugins"
        return r
    r["plugins"] = p
    r["plugins_inline"] = lines[p].split("#")[0].strip()[len("plugins:"):].strip()

    end = len(lines)
    for i in range(p + 1, len(lines)):
        if blankish(lines[i]):
            continue
        if indent_of(lines[i]) == 0:
            end = i
            break

    e = None
    for i in range(p + 1, end):
        if blankish(lines[i]):
            continue
        if indent_of(lines[i]) > 0 and lines[i].split("#")[0].strip().startswith("enabled:"):
            e = i
            break
    if e is None:
        if r["plugins_inline"] in ("", "{}", "null", "~"):
            r["reason"] = "no-enabled"
        else:
            r["reason"] = "unparseable"
        return r

    r["enabled"] = e
    inline = lines[e].split("#")[0].strip()[len("enabled:"):].strip()
    if inline.startswith("["):
        items = [x.strip().strip("'\"") for x in inline.strip("[]").split(",") if x.strip()]
        r["present"] = name in items
        r["reason"] = "flow"
        return r
    if inline not in ("", "null", "~"):
        r["reason"] = "unparseable"
        return r

    ei = indent_of(lines[e])
    last = e
    item_indent = None
    for i in range(e + 1, end):
        if blankish(lines[i]):
            continue
        ind = indent_of(lines[i])
        st = lines[i].strip()
        if st.startswith("- ") and ind >= ei:
            item_indent = ind
            if st[2:].strip().strip("'\"") == name:
                r["present"] = True
            last = i
        elif ind <= ei:
            break
        else:
            last = i
    r["item_indent"] = ei + 2 if item_indent is None else item_indent
    r["last_item"] = last
    r["reason"] = "block"
    return r


def patch(lines, r, name):
    out = list(lines)
    reason = r["reason"]
    if reason == "no-plugins":
        if out and out[-1].strip() != "":
            out.append("")
        out.append("plugins:")
        out.append("  enabled:")
        out.append("    - %s" % name)
    elif reason == "no-enabled":
        p = r["plugins"]
        out[p] = "plugins:"
        out.insert(p + 1, "    - %s" % name)
        out.insert(p + 1, "  enabled:")
    elif reason == "flow":
        e = r["enabled"]
        head, _sep, tail = out[e].partition("enabled:")
        body = tail.split("#")[0]
        comment = tail[len(body):]
        inner = body.strip()[1:-1].strip()
        inner = (inner + ", " + name) if inner else name
        out[e] = "%senabled: [%s]%s" % (head, inner, comment)
    elif reason == "block":
        out.insert(r["last_item"] + 1, " " * r["item_indent"] + "- " + name)
    return out


def main():
    mode = sys.argv[1]
    path = sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else "hermix"

    text = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    r = locate(lines, name)
    if r["present"]:
        print("ALREADY")
        return 0
    if mode == "check":
        print("MISSING")
        return 1
    if r["reason"] == "unparseable":
        print("MANUAL")
        return 3

    out = patch(lines, r, name)
    if os.path.isfile(path):
        shutil.copyfile(path, path + ".bak")
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("PATCHED")
    return 0


sys.exit(main())
PYEOF

# Pick a *working* interpreter. The first `python3` on PATH is not always usable
# (shims/stubs exist that only print an install hint), so walk every match and
# probe each one for real.
PY=""
while IFS= read -r cand; do
  [ -n "$cand" ] || continue
  if "$cand" -c 'import sys,os,shutil' >/dev/null 2>&1; then PY="$cand"; break; fi
done < <(type -aP python3 python 2>/dev/null || true)

config_check() { [ -n "$PY" ] && "$PY" "$PYHELPER" check "$CONFIG_FILE" "$PLUGIN_NAME" >/dev/null 2>&1 </dev/null; }

# --- [4/6] enable ------------------------------------------------------------
echo "==> [4/6] Enabling the plugin"
ENABLE_PATH="skipped"
if [ "$DO_ENABLE" -eq 0 ]; then
  echo "    --no-enable given — leaving config.yaml untouched."
else
  if run_hermes plugins enable "$PLUGIN_NAME" >/dev/null 2>&1; then
    echo "    'hermes plugins enable $PLUGIN_NAME': OK"
    ENABLE_PATH="hermes CLI"
  else
    echo "    'hermes plugins enable $PLUGIN_NAME' returned non-zero."
    ENABLE_PATH="pending"
  fi

  # Whether or not the CLI claimed success, config.yaml is the source of truth.
  if config_check; then
    [ "$ENABLE_PATH" = "pending" ] && ENABLE_PATH="hermes CLI"
    echo "    Verified in config.yaml: $PLUGIN_NAME is under plugins.enabled."
  else
    echo "    Not present in config.yaml — applying direct config fallback."
    if [ -z "$PY" ]; then
      die "python3 is required for the config.yaml fallback but was not found." \
          "Fix: sudo apt-get install -y python3 && re-run, OR add it by hand to $CONFIG_FILE:" \
          "     plugins:" \
          "       enabled:" \
          "         - $PLUGIN_NAME"
    fi
    RC=0
    OUT="$("$PY" "$PYHELPER" patch "$CONFIG_FILE" "$PLUGIN_NAME" 2>&1 </dev/null)" || RC=$?
    case "$OUT" in
      PATCHED|ALREADY)
        echo "    config.yaml patched (backup at $CONFIG_FILE.bak)."
        ENABLE_PATH="config.yaml fallback" ;;
      *)
        die "Could not safely patch $CONFIG_FILE (helper said: ${OUT:-rc=$RC})." \
            "Your config has a 'plugins:' key in a shape this installer will not rewrite." \
            "Fix: edit $CONFIG_FILE by hand so it contains —" \
            "     plugins:" \
            "       enabled:" \
            "         - $PLUGIN_NAME" ;;
    esac
  fi
fi

# --- [5/6] verify ------------------------------------------------------------
echo "==> [5/6] Verifying the install"
V_FILES="FAIL"
V_LIST="FAIL"
V_CONFIG="FAIL"

# (a) plugin source present and structurally a Hermes plugin — HARD requirement
if [ -f "$PLUGIN_DIR/plugin.yaml" ] && [ -f "$PLUGIN_DIR/__init__.py" ]; then
  V_FILES="PASS"
fi

# (b) hermes plugins list mentions us — best-effort, formats vary by version
if run_hermes plugins list 2>/dev/null | grep -qi "$PLUGIN_NAME"; then
  V_LIST="PASS"
elif grep -qi "$PLUGIN_NAME" "$CONFIG_FILE" 2>/dev/null; then
  V_LIST="PASS (via config.yaml)"
fi

# (c) config.yaml really lists it under plugins.enabled — HARD requirement
if [ "$DO_ENABLE" -eq 0 ]; then
  V_CONFIG="SKIP (--no-enable)"
elif config_check; then
  V_CONFIG="PASS"
fi

echo "    [a] plugin.yaml + __init__.py present   : $V_FILES"
echo "    [b] 'hermes plugins list' shows hermix : $V_LIST"
echo "    [c] config.yaml plugins.enabled         : $V_CONFIG"

FAILED=0
[ "$V_FILES" = "PASS" ] || FAILED=1
case "$V_CONFIG" in PASS|SKIP*) ;; *) FAILED=1 ;; esac

if [ "$FAILED" -ne 0 ]; then
  echo ""
  echo "========================================" >&2
  echo " HERMIX INSTALL FAILED" >&2
  echo "========================================" >&2
  [ "$V_FILES" = "PASS" ] || {
    echo " [a] FAILED — $PLUGIN_DIR is missing plugin.yaml and/or __init__.py." >&2
    echo "     Fix: rm -rf '$PLUGIN_DIR' && re-run this installer." >&2
  }
  case "$V_CONFIG" in PASS|SKIP*) ;; *)
    echo " [c] FAILED — $PLUGIN_NAME is not under plugins.enabled in $CONFIG_FILE." >&2
    echo "     Fix: add this to $CONFIG_FILE, then re-run this installer to verify —" >&2
    echo "          plugins:" >&2
    echo "            enabled:" >&2
    echo "              - $PLUGIN_NAME" >&2
  ;; esac
  echo " Nothing was activated. Do NOT restart the gateway yet." >&2
  echo "========================================" >&2
  exit 1
fi

# --- invite --------------------------------------------------------------------
# Stored for the plugin to present once, when it first registers. Never sent
# anywhere by this script.
INVITE_LINE=""
if [ -n "$INVITE" ]; then
  ENV_FILE="$HERMES_HOME/.env"
  mkdir -p "$HERMES_HOME"
  if [ -f "$ENV_FILE" ]; then
    grep -v '^HERMIX_INVITE=' "$ENV_FILE" > "$ENV_FILE.hermix.tmp" 2>/dev/null || true
    mv "$ENV_FILE.hermix.tmp" "$ENV_FILE"
  fi
  echo "HERMIX_INVITE=$INVITE" >> "$ENV_FILE"
  INVITE_LINE=" Invite : $INVITE saved — your agent's first conversation is with whoever invited you."
fi

# --- auto-update supervisor --------------------------------------------------
# Approved ONCE, here. After this the user never runs an update command: the
# supervisor activates approved releases during idle windows and rolls back by
# itself if one fails. Skipped when not root or when systemd isn't available;
# opt out entirely with HERMIX_AUTO_UPDATE=0.
ACTIVATOR_LINE="not installed (needs root + systemd)"
if [ "$(id -u)" = "0" ] && command -v systemctl >/dev/null 2>&1 \
   && [ "${HERMIX_AUTO_UPDATE:-1}" != "0" ]; then
  if [ -f "$PLUGIN_DIR/deploy/agent/hermix-activate.sh" ]; then
    chmod +x "$PLUGIN_DIR/deploy/agent/hermix-activate.sh" 2>/dev/null || true
    if HERMES_HOME="$HERMES_HOME" "$PLUGIN_DIR/deploy/agent/hermix-activate.sh" \
         --install >/dev/null 2>&1; then
      ACTIVATOR_LINE="installed (hourly, idle-aware, auto-rollback)"
    else
      ACTIVATOR_LINE="install failed (updates will still download, activation is manual)"
    fi
  fi
fi

# --- [6/6] done --------------------------------------------------------------
echo "==> [6/6] Done (mode: $CLONE_MODE)"
if [ "$DO_ENABLE" -eq 0 ]; then
  ENABLED_LINE="no (--no-enable was passed; run this installer again without it)"
else
  ENABLED_LINE="yes ($ENABLE_PATH)"
fi

echo ""
echo "========================================"
echo " HERMIX INSTALLED ✓   (not active yet)"
echo "========================================"
echo " Plugin : $PLUGIN_DIR (commit $SHORT_SHA)"
echo " Enabled: $ENABLED_LINE"
echo " Updates: $ACTIVATOR_LINE"
[ -n "$INVITE_LINE" ] && echo "$INVITE_LINE"
echo ""
echo " NEXT — the gateway must restart to load it."
echo " Hermes BLOCKS restarting from inside a chat, so run this from a"
echo " terminal/SSH (NOT from your agent chat):"
echo ""
echo "     hermes gateway restart"
echo ""
echo " (Only use the hermes CLI or desktop app, no gateway? Just open a new session.)"
echo ""
echo " ...then send your agent any message and it will start a 2-minute setup."
echo " No API key needed — it joins the network automatically."
echo "========================================"
