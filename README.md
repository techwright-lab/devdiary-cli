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

## Experimental Rust collector (separate executable; Linux evidence only)

`collector-rs` provides an opt-in **normalized-metadata** control path plus the
separate Linux Claude raw-hook adapter below. It is not a Python replacement.
The control path reads the local observation
shape in `collector-rs/tests/fixtures/local-observation.json`: caller-owned UUIDs,
exact repository/installation scope, numeric epoch `observed_at`, vendor IDs and
`attribution_basis: unknown`. It rejects named actor claims; no identity inference.
Null optional fields are omitted like Python's upload projection. Timestamp conversion
uses decimal half-even rounding to UTC microseconds (years 1000–9999; earlier
year padding differs across Python platforms and is refused). Unlisted/private fields are
discarded before storage; IDs are validated, never repaired. Caller-assigned observation
UUIDs provide replay identity for the normalized control path; the Claude adapter
creates observation IDs and applies vendor-correlation dedup before collection.

```sh
cargo build --locked --manifest-path collector-rs/Cargo.toml
COLLECTOR="$PWD/collector-rs/target/debug/devdiary-collector"
# Use a NEW empty absolute directory, never Python's observer/capture spool.
umask 077
mkdir "$RUST_STATE"
# Review scope.json: exactly endpoint, collector_ref, repository (local root),
# repository_ref (canonical GitHub URL), installation_id (UUID). Fixture examples
# are synthetic; no credential goes in this JSON.
"$COLLECTOR" init "$RUST_STATE" --consent < scope.json
"$COLLECTOR" collect "$RUST_STATE" < normalized-local-metadata.json
"$COLLECTOR" status "$RUST_STATE"
"$COLLECTOR" sync "$RUST_STATE" "$PRIVATE_COLLECTOR_KEY_FILE" 100
```

The Rust-only SQLite filename, application ID and schema version refuse Python,
foreign or incompatible state; there is **no migration/shared-writer mode**. Scope
is immutable from initialization, including after delivery. Keys stay in separate
0600 regular files and are read only by explicit sync; rotating the key file does
not retarget evidence. Owner-only state, no symlink ancestors or hardlinks, FULL
SQLite commits, 250 ms lock waits, 10,000 retained rows and a 16 MiB database ceiling
bound storage. Capacity/lock failures leave existing evidence unchanged; callers
must retry failed collection with the same UUID. Retained receipts count toward
capacity; no pruning yet. Admission reserves worst-case SQLite table/index and
overflow pages for every retained row, including future receipts, failures and
cursor updates. This conservative reservation intentionally refuses new rows
before the physical file is full (currently hundreds, not thousands, of rows);
the 10,000-row limit is an additional ceiling, not a promised capacity. Existing
pre-fix spools are not rewritten or pruned; previously over-admitted spools may
still lack update space and require a separately designed recovery/migration.
Same-UID processes are trusted, not sandboxed.

Input is capped at 64 KiB with a 700 ms stdin deadline (not a whole-hook SLA).
Frozen ASCII allowlisted wire bytes are capped at 16 KiB. Explicit sync attempts
1–100 rows, commits its rotating cursor before HTTP, retains exact bytes after
crash/response loss, and acknowledges only the exact Rails receipt. 401/403/429,
5xx and network failures stop the batch; row failures do not starve later rows.
No redirects or ambient proxies; verified TLS except exact loopback HTTP; each
request has a five-second timeout including the response body. Exit 0 means the
command succeeded, sync exit 1 means pending evidence remains, and exit 2 is a
private/generic local error. These control commands are **not host-failure-neutral**;
never register `collect` as a vendor hook. Only `claude-hook` has the separate
silent deadline/exit contract below. No daemon, live host qualification, surveillance,
stock-tool qualification, native Windows support, signing/update/installer or
Python retirement is included. Rails and human-minute attribution are unchanged.

Reproduce Linux checks (Rust 1.98.1, Python >=3.11):

```sh
cargo fmt --manifest-path collector-rs/Cargo.toml --check
cargo test --locked --manifest-path collector-rs/Cargo.toml
cargo clippy --locked --manifest-path collector-rs/Cargo.toml --all-targets -- -D warnings
PYTHONPATH=src python3 collector-rs/tests/conformance.py -v
cargo audit --file collector-rs/Cargo.lock
bin/test
```

For opt-in real Rails/Puma/PostgreSQL interoperability, use a disposable Rails
checkout at the revision in `collector-rs/tests/fixtures/provenance.json`. Set
`RAILS_ENV=test CI=true` and a **new local** `DATABASE_URL` whose database starts
`devdiary_rust_collector_interop_`; use only test DB credentials. From that Rails
checkout, run `bin/rails db:create db:schema:load`, then
`bundle exec ruby "$CLI_CHECKOUT/collector-rs/tests/rails_interop.rb"`.
The harness refuses a nonempty workspace/observation database, provisions only a
disposable collector credential, tests actual response-loss replay and revocation,
and leaves the isolated database for inspection. Drop only that test DB afterward.
The regular Rust CI job runs the subprocess/HTTP conformance suite; it does not
claim to run this cross-repository Rails gate.

## Customer browser pairing (experimental Linux client)

Requires the backend contract in [DevDiary PR #571](https://github.com/techwright-lab/DevDiary/pull/571)
**merged and deployed separately**. This client PR does not deploy/enable it. The
ordinary origin is `https://devdiary.me`; no manual collector ref, ingest endpoint,
installation UUID or credential file contents are needed. This is DevDiary-specific
browser approval, not OAuth/device authorization.

```sh
# Build/install the native binary privately as described below. Choose a private
# directory OUTSIDE source control; its parent must already be owner-only.
CONNECTION="$HOME/.config/devdiary/customer-project"
"$COLLECTOR" setup "$CONNECTION" "$REPO"
# Multiple distinct GitHub remotes? Repeat with --remote origin (or your choice).
# Paste the displayed one-time code into the browser, choose the matching active
# workspace repository, and explicitly approve metadata collection.

# Approval saves a credential but DOES NOT touch Claude settings.
# SETTINGS must be an existing private JSON file; do not overwrite customer data.
"$COLLECTOR" setup-plan "$CONNECTION" "$SETTINGS" "$PLAN"
# Review the private plan: repository, executable/hash, settings hash, hook entries.
# Stop concurrent settings editors; consent separately to the exact local changes:
"$COLLECTOR" claude-apply "$PLAN" --consent
# Use ordinary Claude with its normal trust/organization policy. No bypass flags.
"$COLLECTOR" connection-status "$CONNECTION" "$PLAN"
"$COLLECTOR" connection-sync "$CONNECTION" 100
"$COLLECTOR" claude-remove "$PLAN" --consent
```

- `setup` creates a 0700 connection directory under an existing private parent.
  It pins the installation UUID, exact origin, local root and canonical GitHub
  scope **before** starting HTTP. Only local read-only Git commands are run: no
  fetch, credential helper, shell, alias/URL rewrite or global identity changes.
  SSH/scp/HTTPS GitHub remotes map to exact canonical URLs; ambiguity needs
  `--remote NAME`, and other hosts/aliases/query paths are refused, not guessed.
- Verified TLS, no redirect following, no ambient proxy use, bounded responses and
  five-second request deadlines. Alternate origins require both `--origin ORIGIN`
  and `--trust-origin`; HTTP is accepted only for numeric loopback development.
  `--no-browser` prints the same constant browser URL instead of opening it.
  Neither approval code nor pairing/collector bearer is ever put in a URL, child
  argument, environment variable, vendor settings or diagnostic. Only the user
  code is displayed for pasting; do not capture/share terminal logs containing it.
- Polling waits the advertised interval (at least five seconds), honors integer
  `Retry-After`, and stops at expiry. Start-rate cooldowns survive process restart.
  Ctrl-C leaves no hooks and no persisted pairing bearer. If exchange might have
  consumed the credential (network loss, interruption, terminal response, failed
  persistence), inspect/revoke unused connections in the browser, then repeat with
  **`--new-pair`**. There is no secret replay/recovery endpoint. The same pinned
  installation is reused. If `connection.json` was committed, ordinary `setup`
  resumes local initialization without issuing another credential.
- `connection.json` atomically commits the token and frozen scope together (0600,
  fsync, no overwrite); credentials stay outside `outbox/`. Do not print or commit
  it. A killed writer can leave a private `.pairing-*.tmp`; these are not adopted
  as credentials. An existing connection cannot be re-paired/retargeted, even
  after delivery. For another scope use a new directory and preserve old outboxes;
  revocation is server-side. This slice does not implement credential rotation.
- `setup-plan` rechecks the approved local Git remote and uses the existing
  reversible ownership-preserving planner. Plan/apply are separate explicit
  actions; browser approval never supplies local hook consent or Claude trust.
  Organization policy is respected, not deleted or bypassed. Ordinary customer
  integration does **not** require proving policy absence. Health reports host
  trust/effective managed policy as unknown, and only reports a target-file block
  when inspected settings explicitly disable hooks or allow managed hooks only.
- Health separates saved connection, local registration evidence (when PLAN is
  supplied), observed/pending counts and deliveries with validated receipts.
  Zero observations is not proof of host coverage. Current server revocation and
  last receipt are shown by the browser, not invented from local registration.
  Unknown attribution remains unknown; no authorship or human minutes change.

Verification:

```sh
python3 collector-rs/tests/pairing.py -v  # mock transport/subprocess fixtures
# Optional actual Rails HTTP/PostgreSQL: reviewed credential-free server checkout,
# installed test bundle/Ruby on PATH, local CREATE/DROP test role. No vendor run.
python3 collector-rs/tests/pairing_rails.py \
  --rails-checkout "$REVIEWED_RAILS_CHECKOUT" --collector "$COLLECTOR"
```

The real interop harness uses an owned, random, verified-absent loopback database,
retains the database-target guard through schema/migrations/fixtures, and verifies
DROP in the parent even on failure. It performs actual Rust pairing/exchange and
scoped ingest against Puma/Rails; only login is a Warden test fixture. Browser
approval uses real cookies and CSRF (missing token rejected); lost exchange requires
new pairing, lost ingest response replays exact bytes with one remote record, and
scope/actor changes are rejected. Disposable plan/apply/remove preserves settings.
This is **fixture-driven application interoperability**, not live stock-Claude
qualification. The stricter qualification harness below remains blocked, unchanged.

## Rust Claude raw-hook adapter: Linux fixture-qualified only

This is a distinct `claude-hook PLAN` command, not registration of the normalized
`collect` command. It accepts stock Claude command-hook stdin for SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, Stop, SubagentStart,
SubagentStop and SessionEnd, following `src/devdiary/observer_hook.py`. It uses
only the existing isolated Rust outbox and explicit `sync`; it never invokes
Python, reads a transcript, makes a network call, or reads provider credentials.

**Qualification:** executable/shell fixtures, Python-reference metadata parity,
local HTTP delivery, concurrency and failure tests are not a successful stock
Claude session. No live Claude settings were changed to develop this slice.
A separately consented smoke still requires an unmodified Claude installation,
disposable settings/repository, existing subscription authentication in normal
HOME, and explicit provider-usage approval. Verify real lifecycle/tool events,
unknown attribution, ordinary host output, delivery and clean removal. Do not
bypass a trust prompt or call this runtime-qualified before that gate. Windows
is unsupported (the crate requires Unix); macOS is unqualified and this adapter
refuses setup/collection outside Linux. Codex is deliberately deferred: its
future adapter must implement its own events, turn/child correlation and exact
host trust/setup contract rather than aliasing the Claude command.

Example **disposable** setup, after reviewing and initializing the scope above:

```sh
cargo build --release --locked --manifest-path collector-rs/Cargo.toml
# PRIVATE_INSTALL and REGISTRATION must already be owner-only absolute dirs.
# SETTINGS must already be a private JSON object file in an owner-only directory;
# explicitly create {} under umask 077 if it does not exist. Do not overwrite it.
install -m 700 collector-rs/target/release/devdiary-collector "$PRIVATE_INSTALL/collector"
COLLECTOR="$PRIVATE_INSTALL/collector"
PLAN="$REGISTRATION/claude-plan.json"  # new file, outside RUST_STATE
"$COLLECTOR" claude-plan "$RUST_STATE" "$SETTINGS" "$PLAN"
# Review PLAN locally: exact scope, executable SHA-256, settings hash and hooks.
# Stop Claude and other settings editors before applying/removing.
"$COLLECTOR" claude-apply "$PLAN" --consent
# Start Claude normally and approve its normal hook/trust UI yourself.
"$COLLECTOR" status "$RUST_STATE"
"$COLLECTOR" sync "$RUST_STATE" "$PRIVATE_COLLECTOR_KEY_FILE" 100
"$COLLECTOR" claude-remove "$PLAN" --consent
```

- The executable is an absolute, owner-only native ELF file with private/trusted
  ancestors, shell-quoted paths and a SHA-256 pin verified on apply and capture.
  Symlinks, hardlinks and shared state/config permissions are refused, not repaired.
  Updates require removal and a freshly reviewed plan; removal remains possible
  after executable/spool failures. Same-UID processes remain trusted.
- Plan and settings are bounded to 64 KiB. The plan stores only ownership, hashes
  and scope, **not a copy of settings secrets**. Both mutations require explicit
  `--consent`. Apply validates the complete proposal, including original key
  presence against the hash-matched settings, before persisting ownership. Unknown
  fields, duplicate keys and inconsistent removal metadata are refused. The private
  `$PLAN.registration.json` receipt binds all validated plan fields by hash; retain
  it with the plan. Apply rejects changed settings/scope/binary; retry of an already
  exact installation is harmless. Edited, duplicate or partially missing owned
  entries block removal instead of deleting customer changes. Pre-receipt
  experimental installations are not adopted automatically.
- Setup/removal use a stable, nonblocking advisory lock and private fsynced atomic
  replacement, with an immediate pre-rename content check. Cooperating hooks hold
  a shared lock. Noncooperating editors cannot be made transactional: keep the
  host/settings editors quiescent. A crash leaves the old or complete new JSON;
  retry apply or removal. Removal records its target hash before replacing settings
  and retains a final removal receipt, so retry after the settings rename completes
  without reinstalling hooks. A prepared-but-unapplied registration can be removed.
  If settings change after an interrupted removal's rename, recovery fails closed
  for inspection rather than guessing ownership. A private temporary file can
  survive a killed writer. Unrelated values/hooks survive removal; restoration is
  JSON-semantic, not original whitespace/order. The private plan, hash-only receipt,
  lock and collected outbox are intentionally retained. Deterministic test-only
  interruption seams cover durable transitions; no production fault switches exist.
- The raw allowlist is applied before persistence. Only validated session,
  prompt/tool/child IDs, event-specific model/source/reason/tool name and a local
  observation timestamp survive. Actor is always unknown; incoming actor claims,
  prompts, transcripts, tool arguments/results and extra fields are discarded.
  CWD must resolve to the exact consented repository or a descendant; sibling
  worktrees and symlink escapes are excluded, without invoking Git.
- The dedup tuple matches Python (installation, session, child, event, documented
  correlation ID), represented as a deterministic UUID in this isolated spool.
  The first committed bytes win, including their timestamp. Lifecycle events
  without delivery IDs get random UUIDs: repeated starts/resumes/ends are not
  collapsed. Stop remains a **turn** observation, not SessionEnd; no completion,
  authorship, human minutes or duration is inferred. No binding support is added.
- Raw stdin is capped at 64 KiB. A whole-process 700 ms watchdog covers stdin,
  validation, filesystem and SQLite work; the registered command also has a
  two-second host timeout. The command exits zero with no stdout/stderr on malformed
  input, missing registration, timeout, lock/full-spool/permission failures or panic.
  A shell neutralizer also silences missing/broken executable failures. OS-level
  scheduling stalls remain outside an application deadline guarantee. Failed
  captures are dropped, not retried by the hook: zero exit is **not** proof of
  capture, and `status` counts are not a complete coverage/host-health signal.
  Conservative capacity reservation remains unchanged (hundreds of retained rows).

Additional Linux fixture gate:

```sh
PYTHONPATH=src python3 collector-rs/tests/claude_hook.py -v
```

## Repeatable stock-Claude qualification (live mode currently blocked)

**Live qualification now fails closed before any vendor execution.** Local
managed files (including Linux `/etc/claude-code/managed-settings.d/`) are refused.
Even their absence cannot establish absence of cached or freshly delivered
server policy. There is no supported pre-launch remote-policy proof implemented
here, and no bypass flag. The opt-in command below documents the intended interface,
not a currently available live run. Fake-vendor tests explicitly stub this gate.
The historical one-Read runtime evidence belongs to source `a5463de`, not this
head; no new provider run is claimed.

Official references: [managed settings](https://code.claude.com/docs/en/managed-settings)
and [server delivery/caching](https://code.claude.com/docs/en/server-managed-settings).
Remote settings can contain hooks, apply from cache at startup, and refresh later;
`--setting-sources ''`, an auth-method projection, and init tools/MCP/plugin lists
are not proof that hooks or policy are isolated. Supporting live qualification
again requires a reviewed, vendor-supported policy-proof design, not cache removal
or disabling organizational policy.

```sh
# Network-inert default: no authentication probe, child process or model call.
python3 collector-rs/tests/qualify_claude.py
# Ordinary CI runs only these fake-vendor control-flow tests, NOT qualification:
python3 collector-rs/tests/qualification_test.py -v
# After explicit approval for ONE subscription-backed print session:
cargo build --release --locked --manifest-path collector-rs/Cargo.toml
python3 collector-rs/tests/qualify_claude.py \
  --consent-provider-use --max-model-runs 1 --claude-version 2.1.280 \
  --collector "$PWD/collector-rs/target/release/devdiary-collector" \
  --rails-checkout "$DISPOSABLE_RAILS_CHECKOUT" --pg-user "$USER"
```

Linux prerequisites: already installed mise-selected native Claude and Ruby,
installed Rails test bundle, and loopback PostgreSQL with a local test role allowed
CREATE/DROP DATABASE (no password discovery/fallback; optional `--pg-port`). Use a
reviewed Rails checkout without dotenv/credential key files. No installation or
update is attempted. CI refuses live mode. Each invocation has one non-retried host
launch, two turns maximum and a 100-second deadline; rerunning needs fresh consent.
This bounds host sessions/turns, not vendor-internal HTTP retries or a dollar tariff.

The harness pins source SHAs, native executable SHA-256/version and the collector
hash; verifies subscription auth using only boolean/method/provider projection;
uses normal HOME solely for vendor-owned auth with empty setting sources, private
explicit settings, strict empty MCP and only synthetic Read permission. It refuses
managed-policy files rather than bypassing them. No live hooks are installed.
It verifies committed lifecycle/tool IDs and metadata privacy, then sends those
exact rows through actual Rails/Puma and a newly owned PostgreSQL database. A lost
response must replay identical bytes; persisted receipts must match remote IDs.
Cleanup removes registration, verifies live settings hashes, revokes the disposable
collector, deletes its key, stops owned processes, drops and checks the exact DB.
SIGINT/SIGTERM use cleanup; SIGKILL/power loss cannot run finally blocks. On cleanup
failure the sanitized report identifies the owned DB for manual investigation.

Database safety does not rely on `DATABASE_URL` precedence: the Ruby process
installs a PostgreSQL connection-target guard before application boot, rejects
additional/hidden test configurations and routing overrides, verifies every
application pool plus the actual server database, loads `db/schema.rb` in that
same process, and rechecks before fixtures. Only the exact owned loopback
host/port/database/user is accepted. The random name stays below PostgreSQL's
63-byte identifier limit. This is protection against configuration mistakes in a
reviewed checkout, not a sandbox for malicious Ruby application/schema code.
Cleanup stages are independent; failures are aggregated, make the result fail,
and cannot prevent later cleanup attempts or the parent process-group/DB fallback.

Standalone safety regressions (Ruby 3.4, ActiveRecord 8.1.3.1, pg, minitest):

```sh
ruby collector-rs/tests/qualification_safety_test.rb
ruby collector-rs/tests/qualification_cleanup_test.rb
# Optional loopback-only real PG target/schema/drop proof; no vendor calls:
QUALIFICATION_PG_TEST=1 QUALIFICATION_RUBY="$(mise which ruby)" \
  python3 collector-rs/tests/qualification_postgres_test.py -v
```

The optional proof uses synthetic application/schema files, not captured vendor
rows or product HTTP ingestion; it does not renew the historical runtime evidence.

Only a sanitized stage/result JSON goes to stdout; raw host output stays bounded
in memory, scratch settings/spool are deleted, and provider transcripts are never
read/copied. Exit zero requires all gates. Fixture failure/child/resume/repeated-start
regressions do **not** qualify real interactive/resume/child behavior. No Codex,
named actor, original-work reconciliation, human-minute, platform or release claim.
Canonical design and dated runtime evidence remain in the
[DevDiary vault](../_vault/products/devdiary/docs/customer-owned-attribution-design.md).

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
