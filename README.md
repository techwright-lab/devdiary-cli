# devdiary

A runtime-dependency-free Python launcher for the DevDiary Agent Attribution Contract.
It keeps stable actor identity separate from runtime/model metadata, removes the
actor-bound ingest key from child environments, writes a read-only context file
(`0400` on POSIX), captures Git work references, and posts one idempotent terminal
envelope.

The CLI also exposes a durable, JSON-stdin `capture begin|finish|status|retry`
boundary for orchestrators and runtime-independent attesters. See the
[portable capture protocol](../_vault/products/devdiary/docs/contracts/portable-capture-protocol.md) in the canonical TechWright vault for the wire contract,
recovery rules, environment allowlist, and security boundaries. `devdiary run`
uses durable capture under `REGISTRY_PARENT/attribution-captures`; the legacy
HMAC spool remains supported alongside it.

This package is an early portable artifact. The launcher starts in `warn`
mode. A separate, explicitly consented **stock Claude/Codex observer** can
register local metadata hooks without wrapping ordinary tool commands. Optional
explicit upload is configured separately; hooks never perform HTTP. It does not
change Git identity, declare named work, or affect human minute derivation.

## Explicit observer upload

After installing a consented repository-scoped observer, optionally configure a
separate private connection. Provision a collector credential through the server's
approved workflow and put it in an owner-only regular file (0600, no symlink or
hardlink). Never put its contents in arguments, environment, vendor settings or
chat. No device authorization or automatic Git-remote mapping is performed.

```sh
devdiary observer connect --state-dir "$STATE" \
  --endpoint https://app.example.test/ingest/v1/observations \
  --collector-ref "$COLLECTOR_REF" \
  --repository-ref https://github.com/OWNER/REPO \
  --key-file "$PRIVATE_KEY_FILE" --consent
devdiary observer sync --state-dir "$STATE" --limit 100
devdiary observer health --state-dir "$STATE"
```

`connect` binds this installation's existing local repository root to the exact
canonical GitHub URL you approve. It stores the key **locator**, not the key, in
`STATE/upload-connection.json` (0600), separate from the vendor ownership manifest.
The collector ref must match the server's receipt. Actor bindings remain captured
local evidence; an explicit actor must already exist on the server. Unknown actors
are supported and never inferred.

Only `sync` performs HTTP: no startup daemon or hook upload. Each request has a
five-second wall deadline, rejects redirects, bypasses ambient proxies, and requires
TLS except exact localhost/loopback test endpoints. Sync freezes allowlisted JSON
bytes, endpoint, collector and installation/repository scope in the SQLite outbox
before sending. It preserves Codex `turn_id`, converts epoch timestamps to UTC with
six fractional digits, and removes local-only fields. Each batch scans at most 100
new rows by durable SQL cursor and retries at most 100 frozen pending rows. A separate
persisted attempt cursor rotates through pending rows, advancing before HTTP so a
crash or poison row cannot starve later observations. Row-specific failures remain
pending while the bounded batch continues; authentication/authorization failures
(401/403), rate limits (429), network failures and HTTP 5xx stop the batch.
HTTP 200/201 is acknowledged only with the exact four
receipt keys and matching IDs plus a positive integer record ID. 401, 409, redirects,
invalid receipts and uncertain responses remain pending; no server error bodies
are printed or stored. Health includes pending/delivered counts and safe last HTTP
failure codes. Local setup/key errors exit 2 with a generic diagnostic; sync delivery
failures exit 1.

Rotate key contents or rerun `connect` with a new protected key-file locator for
the **same** collector and scope. Once any payload is frozen, changed endpoint,
repository, installation or collector is rejected, even after acknowledgement.
Do not delete the SQLite spool to reset delivery: that destroys retry evidence.
Removing/recreating just the connection file with the identical scope preserves
receipts and pending retries. Revoke the collector server-side to disable delivery;
401 retains pending evidence. Removing hooks stops new collection, not explicit
sync of already-consented retained metadata. Delivered records are retained and
still count toward the bounded local spool; there is no automatic pruning yet.
This is metadata ingestion, not Sessions creation or human-minute attribution.

## Stock Claude/Codex observer: isolated POSIX pilot

The observer implements documented command hooks from
[Claude's official hook reference](https://code.claude.com/docs/en/hooks):
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `Stop`, `SubagentStart`, `SubagentStop`, `SessionEnd`.
Documentation/schema verification and fixture tests **are not stock-runtime
qualification**. Windows (structured `observer_platform_unsupported`), remote/container hosts,
managed-policy resolution and device authorization are not implemented. Health intentionally reports `runtime_qualified: false`.

`devdiary observer discover [--vendor claude|codex]` checks PATH executables and
runs only `--version`/`--help` in temporary HOME/config/cwd with a minimal,
credential-free environment. It never enables hooks or reads vendor credentials,
private sessions, trust databases, or user config. This is read-only discovery of
the installation; temporary probe files are removed. Results distinguish missing,
unsafe/failed probes, unknown version, absent supported interface, and a detected
documented interface **without claiming qualified support or a minimum release**.
Health includes this current tool probe separately from registration and trust.
Script/package-manager wrappers are reported as `script_wrapper_not_probed`:
even `--version` can trigger an update in a wrapper. Put the already-installed
native vendor binary on PATH to probe it; DevDiary does not install tools.

For Codex, use `--vendor codex` and select its documented **hooks.json** location
(e.g. `$CODEX_HOME/hooks.json` or `REPO/.codex/hooks.json`); do not target
`config.toml`. One private STATE belongs to one vendor/repository installation.
Claude remains the default for old commands/manifests; `--vendor claude` is
explicitly supported. `apply`/`remove --vendor` optionally assert the saved vendor.
Both preserve unrelated existing JSON hooks and fail on conflicting owned edits.

Codex hooks follow the [official reference](https://developers.openai.com/codex/hooks):
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`,
`SubagentStart`, `SubagentStop`, `SessionEnd`, `Interrupt`. No Claude-only
`PostToolUseFailure` registration is sent to Codex. Codex `turn_id` is retained
instead of Claude `prompt_id`; tool IDs and child/turn pairs support deduplication.
Both use the same bounded schema with `runtime`/`source_tag` (`claude-code` or
`codex`) and unknown actor by default. These tags are not actor identities.
`Interrupt`, `Stop`, and `SubagentStop` never imply parent SessionEnd.

After applying Codex hooks, open ordinary Codex and use **`/hooks` to review and
trust the exact definitions yourself**. Project-local layers also require normal
workspace trust. Definitions changed later need renewed review. DevDiary never
writes trust state, passes a trust-bypass switch, or changes feature/policy flags.
`features.hooks=false`, managed-only policy, inactive config layers or disabled
hooks can prevent collection; their resolution is deliberately not inspected.
An installed registration or recorded event does not prove current trust.
Hosted tools are not covered by Codex local-function hooks.

Compatibility evidence: installed `codex-cli 0.153.4 --version/--help` and the
official `rust-v0.153.4` generated input schemas were checked; generated commands
are fixture-tested with isolated local storage. No Codex model call, live install,
trust approval, or full stock event lifecycle is claimed by this qualification.
For a runtime qualification use a disposable repo and CODEX_HOME/hooks.json,
normal interactive `/hooks` approval, and inspect observations after normal exit.
No launcher or vendor/orchestrator patch is required.

Use absolute paths. `--executable` is a trusted **Python interpreter**, not the
vendor binary. The interpreter, hook script and their resolved ancestors must
be owned by the current user or root and not group/world-writable (sticky temporary
ancestors are allowed). Shared writable toolcache interpreters are rejected, not
implicitly trusted. Use a private copied virtualenv (`python -m venv --copies ...`)
or a suitably protected installation. This path check is not a Python dependency
sandbox: the interpreter's standard library and base installation must also be trusted.
In an installed package, use its stable Python environment. From
this source checkout, prefix commands with `PYTHONPATH=src python3 -m devdiary` in
place of `devdiary`. Only `apply --consent` and `remove --consent` change settings.
Planning writes nothing; redirect its JSON output to a private plan file yourself.
Do not point a fixture run at live user settings.

```sh
# REPO, SETTINGS, STATE, PLAN, PYTHON are absolute, user-selected fixture paths.
# REPO must already exist. STATE must be absent or private (0700).
devdiary observer plan --vendor claude --repository "$REPO" --settings "$SETTINGS" \
  --state-dir "$STATE" --executable "$PYTHON" > "$PLAN"
# Inspect the plan, stop concurrent settings editors, then approve:
devdiary observer apply --plan "$PLAN" --consent
devdiary observer health --state-dir "$STATE"
devdiary observer observations --state-dir "$STATE" --limit 100
# Retains local metadata; stale loaded registrations become no-ops:
devdiary observer remove --state-dir "$STATE" --consent
```

To qualify a disposable stock Claude session, launch **from REPO** with a
separate temporary `CLAUDE_CONFIG_DIR`, `--setting-sources ''` and
`--settings "$SETTINGS"`. Use normal interactive Claude and accept its workspace
trust dialog, or a separately approved `--print` fixture. Do not use `--bare` or
`--safe-mode`: they disable hooks. On supported releases, `--strict-mcp-config`
and an empty explicit MCP configuration isolate unrelated MCP integrations.
`--print --no-session-persistence` does not itself prove hook execution. Inspect
actual observations, including SessionEnd, after exit; silently ignored settings
and a successful Claude process are not evidence of coverage. This package does
not launch Claude or bypass its authentication/trust controls.

The generated command is a shell-quoted absolute Python `-I` invocation of
`src/devdiary/observer_hook.py --state-dir STATE --installation-id ID` (ID is in
the private ownership manifest). It returns exit 0 with empty stdout/stderr for
all supported events and failures, never a host decision or added context. A
0.7-second internal deadline covers blocked stdin; each registered synchronous
hook has a 1-second host timeout. Imports/startup are still subject to the host
budget. Input is capped at 64 KiB. Oversize inputs are dropped, not truncated.

Storage is a private SQLite metadata spool, with FULL synchronous commits,
50 ms lock wait, 10,000-row and 16 MiB ceilings. No hook HTTP, daemon, prompts, tool
arguments/results, assistant messages, transcript reads/paths, environment dumps,
or content hashes. Plans/manifests contain no copy of existing customer settings
(which could contain credentials). Setup/removal therefore preserve unrelated
JSON **semantics**, not formatting; idempotent re-apply leaves settings bytes
unchanged. Full spools drop new events; removal retains old observations and the
manifest. There is no automatic retention deletion command in this slice.

Actor identity defaults to unknown. Optionally pass `--actor-ref EXACT_REF
--binding-session EXACT_VENDOR_SESSION_ID` to **plan**, with an existing registry
selected via global `--config`. `--binding-agent-id EXACT_CHILD_ID` restricts it
to one child; without it only main-session events match. Plan and apply require
an exact registered actor ref, never an alias, tool/model mapping, shared account,
or repository-derived identity. Binding is local user-supplied evidence, **not an
authenticated declaration**. Other sessions/children remain unknown. Rebinding
requires remove/reinstall; old records retain their observed binding snapshot.

Tool/prompt/child correlation IDs deduplicate applicable event kinds separately.
Claude does not expose a unique delivery ID on every lifecycle hook: starts,
resumes, ends, or turns without IDs are retained as ambiguous observations, not
collapsed by payload hash. `Stop` is a turn stop, not session completion;
`SubagentStop` never ends the parent. Start/resume reopens the session summary;
missing start/end remains incomplete. Timestamps are collector observation times,
not authoritative host completion times. No durations or Git work claims are
inferred. Sibling worktrees require their own consented repository scope.

Ownership manifests, nonblocking cooperative locks, stale-plan hashes and a
second hash check immediately before atomic replace guard against conflicting
edits. **Portable filesystems cannot atomically CAS an unrelated application's
settings write**: keep the host/settings editors quiescent during setup/removal.
Uninstall refuses modified/missing/duplicate owned groups rather than deleting
customer edits. Prepared/removing manifests expose interrupted operations and
support replay after settings writes; re-plan an interrupted apply. Changes to
registration definitions require removal/reinstall and renewed host approval.

Health separates registration, target-file disable flags, storage, dropped
inputs and observed/missing lifecycle evidence. It cannot inspect host trust,
other settings tiers or policy, or count failures that occur before storage can
be opened; no events means awaiting events, not a healthy connected runtime.
`unsafe_permissions` means the private state, manifest, lock or database is not
owner-only (directory 0700 / files 0600); health does not repair permissions.
Qualification still needs pinned-release normal commands, trust/restart/resume,
tool/subagent events, concurrent worktrees, offline/crash behavior, and measured
hook latency. Original work attribution/human-day reconciliation is separate.


## Install

Python 3.11 or newer is required.

```text
uv tool install .
# or: pipx install .
uv build .  # build sdist and wheel artifacts
```

## First run

```text
devdiary init \
  --namespace urn:acme \
  --principal-ref urn:acme:human:owner \
  --endpoint https://app.example.test/ingest/v1/sessions

devdiary actor add \
  --ref urn:acme:actor:reviewer \
  --display-name "Code Reviewer" \
  --attester-ref urn:acme:attester:local-launcher \
  --lane review \
  --identity paperclip_agent_id=agent-123

devdiary adapter add \
  --name my-runtime \
  --runtime custom-cli \
  --session-ref-env DEVDIARY_RUNTIME_SESSION_REF_MY_RUNTIME \
  --provider-env DEVDIARY_RUNTIME_PROVIDER_MY_RUNTIME \
  --model-env DEVDIARY_RUNTIME_MODEL_MY_RUNTIME

devdiary doctor --actor urn:acme:actor:reviewer

devdiary run --actor urn:acme:actor:reviewer -- your-command
```

Known outputs may be declared explicitly before the command, for example with
`--pull-request`, `--issue`, or `--artifact`. Git repositories and commits made
while the wrapped process runs are collected automatically; explicit values are
deduplicated with discovered values.

Automatic commit discovery uses Git reflog entries created while the command
runs. It survives resets and avoids treating a branch checkout as new work.
Unusual plumbing that creates commits without reflog entries must provide an
explicit `--commit` reference or use a future runtime-adapter output.

If an ingest key is present but the endpoint is unavailable in `warn` or
`enforce` mode, the exact terminal envelope is queued as a user-controlled JSON
file beside the registry (`0600` on POSIX; inherited user ACLs on Windows). Each
queue entry is authenticated with HMAC-SHA-256;
modified, injected, or key-rotation-stale entries are retained but never sent.
No legacy HMAC entry is queued when a key is absent. The durable capture still
retains its frozen terminal envelope, including when delivery is disabled; it is
not HMAC-authenticated and relies on the private state directory/OS access controls.
Retry durable records with JSON stdin to `capture retry --state-dir STATE --json`
using the original `capture_id` and the configured credential. To retry legacy
HMAC entries without minting a new event identity:

```text
devdiary emit pending
```

Set the configured key environment variable (default:
`DEVDIARY_INGEST_KEY`) only in the launcher environment. The launcher consumes
it, scrubs the inherited native environment block on Linux, and removes it
before starting `your-command`. Runtime contexts retain only a generic command
classification (legacy contexts also include an argument count), never raw command arguments. Never place a
key in the Cast Registry, command arguments, runtime metadata, work-reference
URLs, or exported aliases.

Environment stripping is defense in depth, not a same-UID sandbox. A malicious
process running as the same operating-system identity may be able to inspect
other process memory or a long-lived parent shell on permissive systems. Pass
the key as a one-command environment assignment rather than exporting it in a
long-lived shell, and run untrusted workloads under a different UID/container
or through a separately isolated attester.

Wrapped process trees are fail-closed. POSIX interruption handlers are armed
before process creation and commands run in isolated process groups. On Windows,
the command starts suspended, is assigned to a kill-on-close Job Object, and is
resumed only after assignment succeeds. Unexpected child crashes are declared
failed; only launcher/user interruption is declared cancelled.

The default registry is the user-controlled
`~/.config/devdiary/attribution.json` (or the platform's XDG/AppData equivalent).
It is ordinary editable JSON and contains actor mappings plus the *name* of the
key environment variable, but never the key value. The registry is non-secret
but security-sensitive because it selects the ingest destination. Do not point
`--config` at an unreviewed repository file; copy project actor mappings into a
trusted user registry instead.
