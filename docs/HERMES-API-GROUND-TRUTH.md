# Hermes Plugin API — Ground Truth

Verified against the real Hermes Agent source cloned from
`https://github.com/NousResearch/hermes-agent` into
`C:\Users\manue\Claude\Project Setup\_hermes-agent-src` (shallow clone,
2026-07-24). Line numbers refer to that checkout.

The plugin system lives in **`hermes_cli/plugins.py`** (the `PluginContext`
facade + `PluginManager` loader). The tool registry is **`tools/registry.py`**.
The LLM facade is **`agent/plugin_llm.py`**.

Every entry below gives the verbatim real signature, the source `file:line`,
what our scaffold assumed, and the exact change. "OK" means our scaffold
already conforms and needs no change.

---

## 1. Plugin discovery & loading

**Manifest parsing** — `hermes_cli/plugins.py:1563` `_parse_manifest()` →
`PluginManifest` (dataclass at `:280`). The parser reads **only** these keys
from `plugin.yaml` (`:1635`):

```python
PluginManifest(
    name=data.get("name", plugin_dir.name),
    version=str(data.get("version", "")),
    description=data.get("description", ""),
    author=data.get("author", ""),
    requires_env=data.get("requires_env", []),
    provides_tools=data.get("provides_tools", []),
    provides_hooks=data.get("provides_hooks", []),
    source=source, path=str(plugin_dir), kind=kind, key=key,
)
```

Facts:
- **There is no `hooks:` field.** Hooks are declared to the yaml as
  `provides_hooks:` and even that is *purely informational* — it feeds
  `hermes plugins list`, nothing else. Hooks are wired **only** by calling
  `ctx.register_hook()` inside `register()`. A `hooks:` key is silently
  ignored; it is neither required nor consulted.
- `license:` is not parsed (ignored, harmless).
- Unknown `kind` warns and falls back to `standalone` (`:1587`). Default kind
  is `standalone`, which is what we want.
- Missing `name` defaults to the directory name.

**Module load** — `_load_directory_module()` `:1832`. The plugin dir is imported
as `hermes_plugins.<slug>` where `slug = key.replace("/","__").replace("-","_")`.
`__path__` and `__package__` are set, so **relative imports inside `__init__.py`
work** (`from . import profile, tools, ...`). Our `conftest.py` mirrors this
with `submodule_search_locations`, so tests exercise the same import shape.

**register() invocation** — `_load_plugin()` `:1748`, call at `:1792`:

```python
register_fn = getattr(module, "register", None)   # :1772
...
ctx = PluginContext(manifest, self)               # :1777
register_fn(ctx)                                   # :1792  -- SYNCHRONOUS
```

- `register(ctx)` is called **synchronously, positionally, once**, with a
  single `PluginContext` argument.
- The whole load is wrapped in `try/except Exception` (`:1824`): a plugin that
  raises is logged and disabled but **cannot crash the host**. So `register()`
  should return promptly; long-lived work must be handed to a thread/scheduler
  (see §8).

> Our scaffold: `def register(ctx):` — **OK**. Spawning the service thread from
> within `register()` (`service.start(...)`) returns immediately — OK.

---

## 2. `ctx.register_tool`

Real signature — `hermes_cli/plugins.py:391`:

```python
def register_tool(
    self,
    name: str,
    toolset: str,
    schema: dict,
    handler: Callable,
    check_fn: Callable | None = None,
    requires_env: list | None = None,
    is_async: bool = False,
    description: str = "",
    emoji: str = "",
    override: bool = False,
) -> None:
```

**`schema` shape** — the OpenAI function-tool object
`{"name", "description", "parameters": {json-schema}}`. Confirmed against the
bundled Spotify plugin (`plugins/spotify/tools.py:379` `SPOTIFY_SEARCH_SCHEMA`),
whose top-level `name` equals the registered tool name.

**Handler calling convention** — the registry dispatches at
`tools/registry.py:614` `dispatch(name, args, **kwargs)`:

```python
result = entry.handler(args, **kwargs)     # :631  (sync)
result = _run_async(entry.handler(args, **kwargs))  # :629 (is_async=True)
return self._normalize_handler_result(name, result)
```

So a handler is `handler(args: dict, **kwargs) -> str`. Returning a JSON string
is idiomatic (Spotify handlers are `(args: dict, **kw) -> str`). All exceptions
are caught inside `dispatch` and returned as `{"error": ...}`.

> Our scaffold `tools.py`: handlers are `def search_agents(params, **kwargs)`
> returning `json.dumps({...})`; specs pass `name/toolset/schema/handler/
> description` with schema `{name,description,parameters}`. **OK — matches
> reality exactly. No change.** (`params` is the positional `args` dict.)

---

## 3. `ctx.register_command`

Real signature — `hermes_cli/plugins.py:529`:

```python
def register_command(
    self,
    name: str,
    handler: Callable,
    description: str = "",
    args_hint: str = "",
) -> None:
```

Handler convention (docstring `:538`): **`fn(raw_args: str) -> str | None`**;
may be async. Dispatch call sites confirm a single positional string:
- CLI — `cli.py:9575`: `resolve_plugin_command_result(plugin_handler(user_args))`
- Gateway — `gateway/run.py:12039`: `result = plugin_handler(user_args)` then
  `await` if coroutine.

`user_args` is everything after the command token, `.strip()`ed. Async results
are awaited with a 30 s cap (`resolve_plugin_command_result`, `:2354`).
Name collisions with built-ins are rejected (`:563`).

> Our scaffold `__init__.py`: `ctx.register_command("hermix", handler, "…")`
> — positional (name, handler, description) — **OK**. Handler is
> `def handler(args: str = "", **kwargs) -> str` — called as `handler(user_args)`
> so `args` receives the raw string; `**kwargs` is harmless. **OK.**
> (Optional nicety: pass `args_hint="<sub> [args]"` for Discord/Telegram
> autocomplete — not required for conformance.)

---

## 4. `ctx.register_hook` + the `pre_tool_call` block contract

Real signature — `hermes_cli/plugins.py:1158`:

```python
def register_hook(self, hook_name: str, callback: Callable) -> None:
```

Unknown hook names **warn but are still stored** (`:1164`) — forward-compatible.

**Valid hook names** — `VALID_HOOKS` set `:135`. Confirmed present:
`pre_tool_call`, `post_tool_call`, `transform_tool_result`,
`transform_terminal_output`, `transform_llm_output`, `pre_llm_call`,
`post_llm_call`, `pre_verify`, `pre_api_request`, `post_api_request`,
`api_request_error`, `on_session_start`, `on_session_end`,
`on_session_finalize`, `on_session_reset`, `subagent_start`, `subagent_stop`,
`pre_gateway_dispatch`, `pre_approval_request`, `post_approval_response`,
`kanban_task_claimed/completed/blocked`.

**Callback invocation** — `PluginManager.invoke_hook()` `:1892`:

```python
ret = cb(**kwargs)          # :1917  -- ALWAYS keyword args
if ret is not None:
    results.append(ret)
```

Each callback is wrapped in its own `try/except` (`:1920`) — a raising hook is
logged, not fatal. All hooks are called **kwargs-only**, so a `def cb(**kwargs)`
signature is safe.

### CRITICAL: how a `pre_tool_call` hook BLOCKS a tool

`_get_pre_tool_call_directive_details()` `:2101`. The hook is invoked with these
kwargs (`:2145`): **`tool_name`, `args`, `task_id`, `session_id`,
`tool_call_id`, `turn_id`, `api_request_id`, `middleware_trace`**
(+ `telemetry_schema_version` injected by `invoke_hook`). Note: the arg dict
kwarg is **`args`**, and the tool name kwarg is **`tool_name`**.

The **return contract** (`:2116`, enforced `:2157`):

```python
{"action": "block",   "message": "Reason the tool was blocked"}   # veto
{"action": "approve", "message": "why", "rule_key": "optional"}   # escalate to human gate
```

Enforcement details:
- `action` must be exactly `"block"` or `"approve"` — anything else is ignored
  (`:2161`).
- A `block` **requires** a non-empty `message` (it becomes the tool result the
  model sees); a block with no message is skipped (`:2167`).
- First valid directive wins; observer-only hooks returning other shapes are
  silently ignored (`:2134`).
- `{"allow": bool}` is **not a thing** — there is no `allow`/`reason` contract
  anywhere in the source. To *permit* a call, return `None`.

> Our scaffold `commands.py::install_gate` returned
> `{"allow": False, "reason": "…"}` and read params from
> `kwargs.get("params") or kwargs.get("arguments")`. **Both wrong.**
> Fixes applied:
> - read args via `kwargs.get("args")` (tool_name via `kwargs.get("tool_name")`
>   which was already correct),
> - return `{"action": "block", "message": "…"}` to deny,
> - return `None` to allow.

---

## 5. `ctx.llm` — plugin-initiated LLM calls

`ctx.llm` is a lazily-built `agent.plugin_llm.PluginLlm` facade
(`hermes_cli/plugins.py:350` property; class at `agent/plugin_llm.py:598`).

Real `complete` — `agent/plugin_llm.py:622` — **synchronous**:

```python
def complete(
    self,
    messages: List[Dict[str, Any]],   # OpenAI shape: [{"role","content"}, ...]
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    agent_id: Optional[str] = None,
    profile: Optional[str] = None,
    purpose: Optional[str] = None,
) -> PluginLlmCompleteResult:
```

Return value is a `PluginLlmCompleteResult` dataclass (`:128`) whose text is on
**`.text`** (not the return value itself). A system prompt is just a
`{"role": "system", "content": ...}` entry in `messages` — there is **no
`system=`/`user=` kwarg**, and `messages` is **not** a plain string.
(`complete_structured(*, instructions, input, json_schema=…, system_prompt=…)`
`:683` exists for structured output but we don't need it.)

> Our scaffold `__init__.py::llm` did
> `ctx.llm.complete(system=…, user=…)` with a `complete("<str>")` fallback —
> **both wrong** (raises `TypeError`, and never reads `.text`). Fixed to:
> ```python
> result = ctx.llm.complete(messages=[
>     {"role": "system", "content": system},
>     {"role": "user", "content": user},
> ])
> return result.text
> ```
> Override of provider/model/profile is fail-closed behind
> `plugins.entries.<id>.llm.allow_*_override` config — we pass none, so we run
> on the user's active model. Good.

---

## 6. `ctx.inject_message`

Real signature — `hermes_cli/plugins.py:476`:

```python
def inject_message(self, content: str, role: str = "user") -> bool:
```

Behavior: routes into the active conversation via the CLI reference. If the
agent is running it interrupts (`_interrupt_queue`); if idle it queues
(`_pending_input`). **In gateway mode `_cli_ref is None`, so it logs a warning
and returns `False`** (`:488`) — it does **not** raise. Returns `True` on
success.

> Our scaffold `__init__.py::inject` calls `ctx.inject_message(content,
> role=role)` inside a `try/except`. **OK.** The except is effectively dead
> (gateway returns False rather than raising) but harmless; kept as-is. Note for
> Phase 1: in gateway deployments injected digests won't surface — prefer the
> cron+`--deliver` path (see §8) for gateway delivery.

---

## 7. `ctx.register_skill` + config access

Real `register_skill` — `hermes_cli/plugins.py:1198`:

```python
def register_skill(self, name: str, path: Path, description: str = "") -> None:
```

- `name` must **not** contain `:` and must match `[a-zA-Z0-9_-]+` (`:1218`);
  the namespace `<plugin_name>:<name>` is derived automatically.
- `path` must exist (points at the skill's `SKILL.md`), else `FileNotFoundError`.
- Plugin skills are **opt-in explicit loads only** — they are *not* added to the
  flat `~/.hermes/skills/` tree and *not* listed in `<available_skills>`
  (`:1204`). Resolvable via `skill_view()` as `<plugin>:<name>`.

> Our scaffold does not call `register_skill` yet (skill install is a gated
> tool, Phase 1). No change.

**Config / secrets access.** Two supported surfaces:
- `~/.hermes/config.yaml` via `from hermes_cli.config import load_config`
  (used internally e.g. `plugins.py:463`); `cfg_get(config, "a", "b",
  default=…)` for nested reads.
- `~/.hermes/.env` — loaded into `os.environ` at startup; the blessed helper is
  `hermes_cli.config.get_env_value_prefer_dotenv(VAR)` (used across
  `hermes_cli/auth.py`). Plain `os.getenv` also works once `.env` is loaded.

> Our scaffold `_config.py` uses `os.getenv("HERMIX_API_URL" / "…_KEY")`.
> **OK** for conformance (env is populated from `.env`). Optional hardening:
> switch to `get_env_value_prefer_dotenv` so a value written to `.env`
> mid-session is seen without a restart. Not required.

---

## 8. Background / periodic work (design answers)

**How real plugins do background work.** Two patterns exist in-tree:

1. **Daemon threads spawned from `register()`/`connect()`** — used by
   `plugins/memory/supermemory/__init__.py:840`,
   `plugins/memory/retaindb/__init__.py:338`,
   `plugins/google_meet/meet_bot.py:337` (all `threading.Thread(..., daemon=True)`).
   Good for best-effort continuous work tied to the host process lifetime.

2. **The built-in cron scheduler** — `cron/scheduler.py`, `cron/jobs.py`. This
   is the *blessed* mechanism for reliable, recurring, unattended runs with
   delivery to any platform. Programmatic entry point:
   `cron/jobs.py:1198 create_job(prompt, schedule, name=…, repeat=…,
   deliver=…, script=…, skills=…, model=…, no_agent=…, …)` — the same function
   the `hermes cron create` CLI and the agent's `cronjob` tool call. Schedules
   accept cron expressions **and** human intervals ("every 1h"). Delivery
   targets: telegram/discord/slack/sms/email/github_comment/webhook/local.

### 8a. Can a plugin schedule work "several times a day" reliably while the gateway runs?

**Yes — via the cron scheduler**, not via a hand-rolled `time.sleep` thread. Use
`cron.jobs.create_job("...", schedule="every 8h", deliver="telegram", ...)`
(e.g. `0 */8 * * *`). The scheduler runs inside the gateway process, persists
jobs to `~/.hermes/cron/jobs.json`, survives individual turn boundaries, and
delivers output even when no interactive CLI is attached (unlike
`inject_message`, which is a no-op in gateway mode — §6). Our current
`service.start()` daemon-thread poll loop *works* while the process is up, but
it is best-effort: no persistence, no delivery in gateway mode, and it dies with
the process. **Recommendation (Phase 1, not a conformance blocker):** register
the periodic signals digest as a cron job with `--deliver`, keeping the thread
only for the low-latency outward mailbox poll.

### 8b. Does anything kill long-lived plugin threads?

- **`daemon=True` threads die when the host process exits** — no graceful
  shutdown, no `on_session_end` for them. Our `hermix-service` thread is
  `daemon=True`, so it vanishes on host exit (acceptable, but state must be
  flushed eagerly, not on shutdown).
- **`HERMES_SAFE_MODE=1`** skips plugin discovery entirely
  (`plugins.py:1288`) — the plugin (and its thread) never loads.
- **Process model:** kanban/subagent workers run as **separate
  `hermes … chat -q` subprocesses**; a thread started in one process does not
  exist in the others. `register()` runs per-process, so each process that loads
  the plugin gets its own thread. Nothing in the host proactively `join()`s or
  cancels a plugin thread — the risk is duplication across processes, not
  premature kill.
- The `cron/lifecycle_guard.py` guard only blocks cron *specs* that contain
  gateway-restart commands; it does not touch plugin threads.

### 8c. How do bundled plugins persist small state?

**Plain files under `$HERMES_HOME/<plugin-name>/`** (default
`~/.hermes/<plugin-name>/`). Canonical example: `plugins/disk-cleanup/` writes
`tracked.json` to `get_hermes_home()/"disk-cleanup"/` with an **atomic
write + `.bak` backup** pattern (`disk_cleanup.py:48` `get_state_dir`, `:127`
`save_tracked`: write `.json.tmp` → copy old to `.json.bak` → `os.replace`).
State is deliberately kept **out of** `$HERMES_HOME/logs/`. `HERMES_HOME` is
resolved via `from hermes_constants import get_hermes_home`.

> Our scaffold `profile.py` persists the card — it should follow the same
> convention (a file under `$HERMES_HOME/hermix/…` via `get_hermes_home()`,
> atomic write). Verify in a follow-up; not part of this conformance pass.

---

## Summary of conformance changes applied

| Surface | File | Was | Now |
|---|---|---|---|
| Hook block contract | `commands.py` | `{"allow": False, "reason": …}`, reads `params`/`arguments` | `{"action": "block", "message": …}`, reads `args` |
| LLM adapter | `__init__.py` | `complete(system=…, user=…)` + str fallback | `complete(messages=[…]).text` |
| Manifest fields | `plugin.yaml` | `hooks:` (ignored) | `provides_hooks:` (+ `provides_tools:`) |
| Tool handlers/schema | `tools.py` | — | OK, unchanged (already conformant) |
| register_command/tool/inject | `__init__.py` | — | OK, unchanged (already conformant) |

---

## Appendix: the Desktop UI plugin SDK — a DIFFERENT surface

**Confidence: LOW. Unverified against source or official docs.** Everything in
this appendix comes from a third-party walkthrough video, not from the Hermes
checkout above. It is recorded here so nobody re-derives it from scratch, and
so nobody mistakes it for the verified material in the rest of this file.
Attempts to fetch `hermes-agent.nousresearch.com/docs/` were blocked (401 from
the browser endpoint, 403 through the proxy), so it could not be confirmed.

**Hermix does not use this SDK and does not depend on any of it.**

### It is not the same thing as §1

Two distinct plugin systems appear to exist:

| | This document, §1-§9 | The Desktop UI SDK |
|---|---|---|
| Shape | headless: `register(ctx)` | HTML/JS panel in the desktop shell |
| Registers | tools, commands, hooks, skills | a UI surface at a named placement |
| Runs | daemon + cron, host-independent | inside the desktop app only |
| Hermix uses it | yes, entirely | no |

### Placements (reported, ~25)

`pane` main/left/right/top/bottom · `workspace` top/bottom/left/right/center ·
`status_bar` left/right · `title_bar` left/center/right · `popover`
top/bottom/left/right · `composer` top/bottom/leading/actions/attachments ·
`workspace route + sidebar`.

Forms are said to follow function: **compact** (ambient state), **anchored**
(detail without leaving the conversation), **media**, **expansive** (full
application), **declarative** (plugin supplies structured data, the host
decides how it renders).

### The three limits that would constrain us

These are the reason this appendix exists at all — each one invalidates an
assumption someone might otherwise make while designing a Hermix UI:

1. **A desktop plugin is not always running.** It lives and dies with the
   desktop app. So it can never be the delivery mechanism — only a *window*
   onto state the daemon and cron already maintain. This vindicates the
   execution/delivery plane split (§ cron): a UI would be a third, purely
   observational plane.
2. **Plugin storage is small JSON state, not a database.** Our dossier,
   matchmaker state and outbox stay exactly where they are; a UI would read
   them, never own them.
3. **A plugin cannot invent a shell region.** It may only occupy placements the
   host already consumes, so any Hermix surface has to fit one of the names
   above rather than a layout of our choosing.

### Why it is interesting for Hermix anyway

Our sharpest product tension is that **silence is indistinguishable from
breakage**. We currently answer that with a one-time check-in a few hours after
install (`_config.checkin_after_hours`), which costs an interruption to prove we
are alive.

A `status_bar` compact item would answer it permanently and for free — "2
conversations, nothing worth your time yet" — with no interruption at all. And
the `declarative` form, if it works as described, would consume exactly the
validated `response.packet` we already build, keeping the compiler boundary in
`render.py` intact rather than creating a second place where attribution can be
lost.

**Verify before building anything on this.**
