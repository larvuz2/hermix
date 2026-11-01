#!/usr/bin/env bash
# =============================================================================
# Real harness for install.sh — runs in Git Bash on Windows and on Ubuntu.
# =============================================================================
# Builds a throwaway world per case:
#   * a fake $HERMES_HOME containing a config.yaml with unrelated keys
#   * a fake `hermes` executable on PATH that logs argv and emulates
#     `plugins enable` / `plugins list` (and can be forced to FAIL)
#   * a local bare git repo standing in for GitHub, injected through the
#     HERMIX_REPO env var that install.sh honours
#
# Usage:  bash tests/test_install_sh.sh
# Exit 0 only when every case passes.
# =============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

PASSES=0
FAILURES=0
CURRENT=""

pass() { PASSES=$((PASSES + 1)); echo "  PASS  $CURRENT :: $1"; }
fail() { FAILURES=$((FAILURES + 1)); echo "  FAIL  $CURRENT :: $1"; }
check() { if [ "$1" = "0" ]; then pass "$2"; else fail "$2"; fi; }

case_start() {
  CURRENT="$1"
  echo ""
  echo "--- CASE: $CURRENT"
}

# -----------------------------------------------------------------------------
# World builder
# -----------------------------------------------------------------------------
ROOT="$(mktemp -d 2>/dev/null || echo "/tmp/hermix-tests-$$")"
mkdir -p "$ROOT"
cleanup_all() { rm -rf "$ROOT" 2>/dev/null || true; }
trap cleanup_all EXIT

# --- the fake upstream repo (seeded with a valid plugin) ----------------------
make_remote() {
  # $1 = remote name, $2 = "valid" | "broken"
  local remote="$ROOT/remotes/$1.git"
  local work="$ROOT/work/$1"
  rm -rf "$remote" "$work"
  mkdir -p "$remote" "$work"
  git init -q --bare "$remote"
  git -C "$remote" symbolic-ref HEAD refs/heads/main
  git init -q "$work"
  git -C "$work" symbolic-ref HEAD refs/heads/main
  git -C "$work" config user.email "harness@example.com"
  git -C "$work" config user.name "harness"
  git -C "$work" config commit.gpgsign false
  echo "# hermix test fixture" > "$work/README.md"
  if [ "$2" = "valid" ]; then
    printf 'name: hermix\nversion: 0.0.0-test\n' > "$work/plugin.yaml"
    printf 'def register(ctx):\n    return None\n' > "$work/__init__.py"
  fi
  git -C "$work" add -A >/dev/null
  git -C "$work" commit -qm "seed" >/dev/null
  git -C "$work" push -q "$remote" main >/dev/null 2>&1
  echo "$remote"
}

REMOTE_OK="$(make_remote plugin valid)"
REMOTE_BROKEN="$(make_remote brokenplugin broken)"

# --- the fake `hermes` CLI ----------------------------------------------------
# Emulates v0.19.0 behaviour we care about:
#   plugins enable <n>  -> appends <n> under plugins.enabled in $HERMES_HOME/config.yaml
#                          (exit 1 without writing when FAKE_HERMES_FAIL_ENABLE=1)
#   plugins list        -> lists names found under plugins.enabled
BINDIR="$ROOT/bin"
mkdir -p "$BINDIR"
cat > "$BINDIR/hermes" <<'FAKEEOF'
#!/usr/bin/env bash
set -uo pipefail
LOG="${FAKE_HERMES_LOG:-/dev/null}"
echo "$*" >> "$LOG"
CFG="${HERMES_HOME:-/nonexistent}/config.yaml"

if [ "${1:-}" = "plugins" ] && [ "${2:-}" = "enable" ]; then
  if [ "${FAKE_HERMES_FAIL_ENABLE:-0}" = "1" ]; then
    echo "error: could not enable plugin (needs a TTY)" >&2
    exit 1
  fi
  NAME="${3:-}"
  if grep -qE "^[[:space:]]*-[[:space:]]*${NAME}[[:space:]]*$" "$CFG" 2>/dev/null; then
    exit 0
  fi
  if grep -qE '^plugins:' "$CFG" 2>/dev/null; then
    # insert under the existing plugins.enabled list
    awk -v name="$NAME" '
      { print }
      /^[[:space:]]+enabled:[[:space:]]*$/ && !done && inplugins { print "    - " name; done=1 }
      /^plugins:/ { inplugins=1 }
      /^[^[:space:]#]/ && !/^plugins:/ { inplugins=0 }
    ' "$CFG" > "$CFG.tmp" && mv "$CFG.tmp" "$CFG"
  else
    printf 'plugins:\n  enabled:\n    - %s\n' "$NAME" >> "$CFG"
  fi
  exit 0
fi

if [ "${1:-}" = "plugins" ] && [ "${2:-}" = "list" ]; then
  if [ "${FAKE_HERMES_FAIL_LIST:-0}" = "1" ]; then
    echo "error: unknown command" >&2
    exit 1
  fi
  echo "Plugins:"
  sed -n '/^plugins:/,/^[^[:space:]#]/p' "$CFG" 2>/dev/null \
    | grep -E '^[[:space:]]*-[[:space:]]' \
    | sed -E 's/^[[:space:]]*-[[:space:]]*//' \
    | while read -r n; do [ -n "$n" ] && echo "  $n  (enabled)"; done
  exit 0
fi

echo "fake hermes: unhandled command: $*" >&2
exit 2
FAKEEOF
chmod +x "$BINDIR/hermes"

# --- a fresh fake HERMES_HOME -------------------------------------------------
# NOTE: called via $(new_home), i.e. in a subshell — so it must not rely on
# mutating shell state for uniqueness. mktemp -d gives us that for free.
new_home() {
  mkdir -p "$ROOT/homes"
  local base
  base="$(mktemp -d "$ROOT/homes/hXXXXXX")"
  local home="$base/.hermes"
  mkdir -p "$home/plugins"
  cat > "$home/config.yaml" <<'CFGEOF'
# Hermes config — unrelated keys that MUST survive the installer
model: anthropic/claude-opus-4
provider: anthropic
gateway:
  port: 8080
  host: 127.0.0.1
tools:
  enabled:
    - shell
    - browser
CFGEOF
  echo "$home"
}

# Keep the minimum system PATH needed for git/python on this box (env -i wipes it).
SYS_PATH=""
for tool in git python3 python; do
  p="$(command -v "$tool" 2>/dev/null || true)"
  [ -n "$p" ] && SYS_PATH="$SYS_PATH:$(dirname "$p")"
done
SYS_PATH="${SYS_PATH#:}"

# Variables that must survive `env -i` or the toolchain breaks. On Windows,
# python.exe refuses to start without SYSTEMROOT; git wants a few of these too.
PASSTHRU=()
for v in SYSTEMROOT WINDIR COMSPEC TEMP TMP USERPROFILE LOCALAPPDATA APPDATA \
         PATHEXT MSYSTEM LANG LC_ALL SSL_CERT_FILE SSL_CERT_DIR; do
  eval "vv=\${$v:-}"
  [ -n "${vv:-}" ] && PASSTHRU+=("$v=$vv")
done

# --- run install.sh in an isolated env ----------------------------------------
# run_install <home> <repo> [extra install.sh args...]
OUT=""
RC=0
_run() {
  local path="$1" home="$2" repo="$3"
  shift 3
  local outfile="$ROOT/out.$$.txt"
  RC=0
  env -i "${PASSTHRU[@]}" \
    PATH="$path" \
    HOME="$(dirname "$home")" \
    HERMES_HOME="$home" \
    HERMIX_REPO="$repo" \
    FAKE_HERMES_LOG="${FAKE_HERMES_LOG:-/dev/null}" \
    FAKE_HERMES_FAIL_ENABLE="${FAKE_HERMES_FAIL_ENABLE:-0}" \
    FAKE_HERMES_FAIL_LIST="${FAKE_HERMES_FAIL_LIST:-0}" \
    TMPDIR="$ROOT" \
    bash "$INSTALL_SH" "$@" >"$outfile" 2>&1 || RC=$?
  OUT="$(cat "$outfile")"
  rm -f "$outfile"
  if [ "${HARNESS_DEBUG:-0}" = "1" ]; then
    echo "----- install.sh output (rc=$RC) -----"
    echo "$OUT"
    echo "--------------------------------------"
  fi
  return 0
}

run_install() {
  _run "$BINDIR:/usr/bin:/bin:/usr/local/bin:$SYS_PATH" "$@"
}

# run_install_nohermes — same, but with `hermes` absent from PATH
run_install_nohermes() {
  _run "/usr/bin:/bin:$SYS_PATH" "$@"
}

# Count occurrences of a `- hermix` list entry in a config file.
# (`grep -c` prints 0 AND exits 1 on no-match, so the fallback must not echo.)
count_hermix() {
  local n
  n="$(grep -cE '^[[:space:]]*-[[:space:]]*hermix[[:space:]]*$' "$1" 2>/dev/null)" || n=0
  echo "${n:-0}"
}

contains() { case "$OUT" in *"$1"*) return 0 ;; *) return 1 ;; esac; }

echo "======================================================================"
echo " install.sh harness"
echo "   script : $INSTALL_SH"
echo "   remote : $REMOTE_OK"
echo "   sandbox: $ROOT"
echo "======================================================================"

[ -f "$INSTALL_SH" ] || { echo "FATAL: $INSTALL_SH not found"; exit 2; }

# =============================================================================
# CASE 1 — fresh install
# =============================================================================
case_start "fresh install"
H1="$(new_home)"
export FAKE_HERMES_LOG="$ROOT/hermes1.log"; : > "$FAKE_HERMES_LOG"
run_install "$H1" "$REMOTE_OK"
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
[ -f "$H1/plugins/hermix/plugin.yaml" ] && [ -f "$H1/plugins/hermix/__init__.py" ]
check $? "plugin.yaml + __init__.py cloned into \$HERMES_HOME/plugins/hermix"
[ -d "$H1/plugins/hermix/.git" ]; check $? "clone is a git checkout"
contains "HERMIX INSTALLED"; check $? "prints the HERMIX INSTALLED banner"
contains "not active yet"; check $? "banner is honest: '(not active yet)'"
contains "hermes gateway restart"; check $? "tells the human to run 'hermes gateway restart'"
contains "NOT from your agent chat"; check $? "warns it must be a separate shell"
[ "$(count_hermix "$H1/config.yaml")" = "1" ]; check $? "config.yaml lists hermix exactly once"
grep -q "model: anthropic/claude-opus-4" "$H1/config.yaml"; check $? "unrelated key 'model' preserved"
grep -q "port: 8080" "$H1/config.yaml"; check $? "unrelated nested key 'gateway.port' preserved"
grep -q "    - browser" "$H1/config.yaml"; check $? "unrelated 'tools.enabled' list preserved"
grep -q "plugins enable hermix" "$FAKE_HERMES_LOG"; check $? "invoked 'hermes plugins enable hermix'"
! grep -qE '(^| )update' "$FAKE_HERMES_LOG"; check $? "NEVER invoked 'hermes update'"
! grep -q "gateway" "$FAKE_HERMES_LOG"; check $? "NEVER invoked 'hermes gateway ...'"

# =============================================================================
# CASE 2 — re-run is idempotent and uses git pull
# =============================================================================
case_start "re-run (idempotent)"
: > "$FAKE_HERMES_LOG"
run_install "$H1" "$REMOTE_OK"
[ "$RC" = "0" ]; check $? "second run exits 0 (got $RC)"
contains "Existing git checkout found"; check $? "detected the existing checkout"
contains "git pull --ff-only: OK"; check $? "updated via git pull --ff-only"
[ "$(count_hermix "$H1/config.yaml")" = "1" ]; check $? "hermix still listed exactly once (no duplicate)"
! ls -d "$H1/plugins/hermix.bak."* >/dev/null 2>&1; check $? "no spurious .bak. backup dir created"
run_install "$H1" "$REMOTE_OK"
run_install "$H1" "$REMOTE_OK"
run_install "$H1" "$REMOTE_OK"
[ "$RC" = "0" ]; check $? "runs 3,4,5 also exit 0 (safe to run 5 times)"
[ "$(count_hermix "$H1/config.yaml")" = "1" ]; check $? "still exactly one hermix entry after 5 runs"
! grep -qE '(^| )update|gateway' "$FAKE_HERMES_LOG"; check $? "still never runs update/gateway"

# =============================================================================
# CASE 3 — dir exists but is NOT a git repo -> backed up, then cloned fresh
# =============================================================================
case_start "existing non-git dir backed up"
H3="$(new_home)"
mkdir -p "$H3/plugins/hermix"
echo "precious user data" > "$H3/plugins/hermix/USER_DATA.txt"
export FAKE_HERMES_LOG="$ROOT/hermes3.log"; : > "$FAKE_HERMES_LOG"
run_install "$H3" "$REMOTE_OK"
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
contains "not a git repo"; check $? "announced the non-git directory"
[ -f "$H3/plugins/hermix.bak.1/USER_DATA.txt" ]; check $? "old dir moved to hermix.bak.1 with contents intact"
[ -d "$H3/plugins/hermix/.git" ]; check $? "fresh git clone in place"
[ -f "$H3/plugins/hermix/plugin.yaml" ]; check $? "plugin.yaml present after re-clone"
# second non-git collision increments the suffix
rm -rf "$H3/plugins/hermix"
mkdir -p "$H3/plugins/hermix"; echo two > "$H3/plugins/hermix/USER_DATA.txt"
run_install "$H3" "$REMOTE_OK"
[ -d "$H3/plugins/hermix.bak.2" ]; check $? "second collision backs up to hermix.bak.2"

# =============================================================================
# CASE 4 — `hermes plugins enable` FAILS -> config.yaml fallback
# =============================================================================
case_start "hermes plugins enable fails -> config.yaml fallback"
H4="$(new_home)"
export FAKE_HERMES_LOG="$ROOT/hermes4.log"; : > "$FAKE_HERMES_LOG"
export FAKE_HERMES_FAIL_ENABLE=1
run_install "$H4" "$REMOTE_OK"
export FAKE_HERMES_FAIL_ENABLE=0
[ "$RC" = "0" ]; check $? "exit code 0 despite CLI failure (got $RC)"
contains "returned non-zero"; check $? "reported the CLI failure"
contains "config fallback"; check $? "announced the config.yaml fallback path"
contains "config.yaml fallback"; check $? "final banner names the fallback path used"
[ "$(count_hermix "$H4/config.yaml")" = "1" ]; check $? "hermix written under plugins.enabled exactly once"
[ -f "$H4/config.yaml.bak" ]; check $? "a .bak of config.yaml was written first"
grep -q "model: anthropic/claude-opus-4" "$H4/config.yaml"; check $? "unrelated keys preserved by the fallback"
grep -q "    - browser" "$H4/config.yaml"; check $? "unrelated tools.enabled list untouched"
contains "[c] config.yaml plugins.enabled         : PASS"
check $? "verification (c) still PASSes"

# re-running with the CLI still failing must not duplicate
export FAKE_HERMES_FAIL_ENABLE=1
run_install "$H4" "$REMOTE_OK"
export FAKE_HERMES_FAIL_ENABLE=0
[ "$RC" = "0" ]; check $? "fallback path is idempotent (exit 0)"
[ "$(count_hermix "$H4/config.yaml")" = "1" ]; check $? "fallback path did not duplicate the entry"

# =============================================================================
# CASE 5 — config.yaml ALREADY contains hermix -> untouched, no duplicate
# =============================================================================
case_start "config.yaml already has hermix"
H5="$(new_home)"
cat >> "$H5/config.yaml" <<'EOF'
plugins:
  enabled:
    - someotherplugin
    - hermix
EOF
BEFORE="$(cat "$H5/config.yaml")"
export FAKE_HERMES_LOG="$ROOT/hermes5.log"; : > "$FAKE_HERMES_LOG"
run_install "$H5" "$REMOTE_OK"
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
[ "$(count_hermix "$H5/config.yaml")" = "1" ]; check $? "hermix NOT duplicated"
grep -q "    - someotherplugin" "$H5/config.yaml"; check $? "sibling plugin 'someotherplugin' preserved"
[ "$BEFORE" = "$(cat "$H5/config.yaml")" ]; check $? "config.yaml byte-identical (nothing rewritten)"

# =============================================================================
# CASE 6 — plugins: block exists but with NO enabled: key
# =============================================================================
case_start "plugins block without enabled: key"
H6="$(new_home)"
printf 'plugins:\n' >> "$H6/config.yaml"
export FAKE_HERMES_LOG="$ROOT/hermes6.log"; : > "$FAKE_HERMES_LOG"
export FAKE_HERMES_FAIL_ENABLE=1
run_install "$H6" "$REMOTE_OK"
export FAKE_HERMES_FAIL_ENABLE=0
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
[ "$(count_hermix "$H6/config.yaml")" = "1" ]; check $? "an enabled: list was created with hermix"
grep -qE '^  enabled:' "$H6/config.yaml"; check $? "well-formed 'enabled:' key inserted under plugins:"
grep -q "provider: anthropic" "$H6/config.yaml"; check $? "unrelated keys preserved"

# =============================================================================
# CASE 7 — missing hermes binary -> exit 1 + docs pointer
# =============================================================================
case_start "hermes binary missing"
H7="$(new_home)"
run_install_nohermes "$H7" "$REMOTE_OK"
[ "$RC" = "1" ]; check $? "exit code 1 (got $RC)"
contains "Hermes Agent was not found"; check $? "clear 'not found' message"
contains "https://hermes-agent.nousresearch.com/docs/getting-started/installation"
check $? "points at the official installation docs"
[ ! -d "$H7/plugins/hermix" ]; check $? "did not clone anything before failing"

# =============================================================================
# CASE 8 — verification failure (repo lacks plugin.yaml/__init__.py) -> non-zero
# =============================================================================
case_start "verification failure -> non-zero exit"
H8="$(new_home)"
export FAKE_HERMES_LOG="$ROOT/hermes8.log"; : > "$FAKE_HERMES_LOG"
run_install "$H8" "$REMOTE_BROKEN"
[ "$RC" != "0" ]; check $? "non-zero exit (got $RC)"
contains "HERMIX INSTALL FAILED"; check $? "prints the loud failure banner"
contains "[a] plugin.yaml + __init__.py present   : FAIL"
check $? "verification (a) reported FAIL"
contains "Do NOT restart the gateway yet"; check $? "tells the user not to restart"
! contains "HERMIX INSTALLED"; check $? "never claims success when verification failed"

# =============================================================================
# CASE 9 — --no-enable clones only
# =============================================================================
case_start "--no-enable"
H9="$(new_home)"
export FAKE_HERMES_LOG="$ROOT/hermes9.log"; : > "$FAKE_HERMES_LOG"
run_install "$H9" "$REMOTE_OK" --no-enable
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
[ -f "$H9/plugins/hermix/plugin.yaml" ]; check $? "plugin still cloned"
[ "$(count_hermix "$H9/config.yaml")" = "0" ]; check $? "config.yaml NOT modified"
[ ! -s "$FAKE_HERMES_LOG" ] || ! grep -q "plugins enable" "$FAKE_HERMES_LOG"
check $? "'hermes plugins enable' never called"

# =============================================================================
# CASE 9b — --invite stores the code for first registration, sanitised
# =============================================================================
case_start "--invite"
H9B="$(new_home)"
printf 'OTHER=keep\nHERMIX_INVITE=old\n' > "$H9B/.env"
run_install "$H9B" "$REMOTE_OK" --invite 'AbCd-23;rm -rf /'
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
grep -qx 'HERMIX_INVITE=abcd23rmrf' "$H9B/.env"; check $? "code lower-cased, non-alphanumerics dropped"
[ "$(grep -c '^HERMIX_INVITE=' "$H9B/.env")" = "1" ]; check $? "previous invite replaced, not duplicated"
grep -qx 'OTHER=keep' "$H9B/.env"; check $? "unrelated .env lines kept"
contains "Invite :"; check $? "summary mentions the invite"

# =============================================================================
# CASE 10 — --dir override
# =============================================================================
case_start "--dir override"
H10="$(new_home)"
ALT="$ROOT/altplugins/hermix"
export FAKE_HERMES_LOG="$ROOT/hermes10.log"; : > "$FAKE_HERMES_LOG"
run_install "$H10" "$REMOTE_OK" --dir "$ALT"
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
[ -f "$ALT/plugin.yaml" ]; check $? "cloned into the overridden directory"
[ ! -d "$H10/plugins/hermix" ]; check $? "default directory left alone"

# =============================================================================
# CASE 11 — inline (flow-style) enabled: [a, b] list
# =============================================================================
case_start "flow-style enabled: [a, b]"
H11="$(new_home)"
printf 'plugins:\n  enabled: [alpha, beta]\n' >> "$H11/config.yaml"
export FAKE_HERMES_LOG="$ROOT/hermes11.log"; : > "$FAKE_HERMES_LOG"
export FAKE_HERMES_FAIL_ENABLE=1
run_install "$H11" "$REMOTE_OK"
export FAKE_HERMES_FAIL_ENABLE=0
[ "$RC" = "0" ]; check $? "exit code 0 (got $RC)"
grep -q 'enabled: \[alpha, beta, hermix\]' "$H11/config.yaml"
check $? "hermix appended into the flow list without losing alpha/beta"

# =============================================================================
# CASE 12 — piped to bash (the real `curl -fsSL ... | bash` shape)
# =============================================================================
case_start "curl | bash (script on stdin)"
H12="$(new_home)"
export FAKE_HERMES_LOG="$ROOT/hermes12.log"; : > "$FAKE_HERMES_LOG"
PIPEOUT="$ROOT/piped.txt"
PRC=0
cat "$INSTALL_SH" | env -i "${PASSTHRU[@]}" \
  PATH="$BINDIR:/usr/bin:/bin:/usr/local/bin:$SYS_PATH" \
  HOME="$(dirname "$H12")" HERMES_HOME="$H12" HERMIX_REPO="$REMOTE_OK" \
  FAKE_HERMES_LOG="$FAKE_HERMES_LOG" TMPDIR="$ROOT" \
  bash >"$PIPEOUT" 2>&1 || PRC=$?
OUT="$(cat "$PIPEOUT")"
[ "$PRC" = "0" ]; check $? "exit code 0 when piped to bash (got $PRC)"
contains "HERMIX INSTALLED"; check $? "full banner reached (script not eaten from stdin)"
[ -f "$H12/plugins/hermix/plugin.yaml" ]; check $? "plugin cloned"
[ "$(count_hermix "$H12/config.yaml")" = "1" ]; check $? "hermix enabled exactly once"
! grep -q "read" "$INSTALL_SH" || ! grep -nE '^[^#]*\bread\b[[:space:]]+-?[a-zA-Z]*[[:space:]]*[A-Z_]+' "$INSTALL_SH" | grep -v 'read -r cand' >/dev/null
check $? "no interactive \`read\` prompting on stdin"

# =============================================================================
# CASE 13 — the script itself never mentions running update/gateway restart
# =============================================================================
case_start "script contains no update/restart invocation"
! grep -nE '^[^#]*hermes[^#]*\b(update)\b' "$INSTALL_SH" | grep -v 'echo' >/dev/null
check $? "no executable 'hermes update' line"
! grep -nE '^[^#]*\brun_hermes[[:space:]]+(gateway|update)' "$INSTALL_SH" >/dev/null
check $? "no run_hermes gateway/update call"
grep -q "MUST NEVER RUN \`hermes update\`" "$INSTALL_SH"
check $? "loud WHY comment present at the top"

# =============================================================================
echo ""
echo "======================================================================"
echo " RESULT: $PASSES passed, $FAILURES failed"
echo "======================================================================"
[ "$FAILURES" -eq 0 ] || exit 1
exit 0
