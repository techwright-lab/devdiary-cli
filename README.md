# devdiary

> [!IMPORTANT]
> **Using a control plane such as Paperclip? It needs its own setup, or its agents record nothing.**
> See [Integrations](docs/integrations/README.md) and the [Paperclip guide](docs/integrations/paperclip.md).

A runtime-dependency-free Python launcher for the DevDiary Agent Attribution Contract.
It keeps stable actor identity separate from runtime/model metadata, removes the
actor-bound ingest key from child environments, writes a read-only context file
(`0400` on POSIX), captures Git work references, and posts one idempotent terminal
envelope.

The CLI also exposes a durable, JSON-stdin `capture begin|finish|status|retry`
boundary for orchestrators and runtime-independent attesters. See the
[portable capture protocol](../_vault/products/devdiary/docs/contracts/portable-capture-protocol.md) in the canonical TechWright vault for the wire contract,
recovery rules, environment allowlist, and security boundaries. `devdiary-launcher run`
uses durable capture under `REGISTRY_PARENT/attribution-captures`; the legacy
HMAC spool remains supported alongside it.

This package is an early portable artifact. The launcher starts in `warn`
mode. A separate, explicitly consented **stock Claude/Codex observer** can
register local metadata hooks without wrapping ordinary tool commands. Optional
explicit upload is configured separately; hooks never perform HTTP. It does not
change Git identity, declare named work, or affect human minute derivation.

## Experimental Rust collector (separate executable; Linux and macOS)

`collector-rs` builds one `devdiary` binary: `devdiary setup` pairs this machine
with DevDiary for every tracked repository (installation scope), registers the
Claude Code or Codex hooks after you agree in the terminal, and a per-user
scheduler delivers what they record: a systemd user timer on Linux, a launchd
agent on macOS. Release one builds for Linux on x86_64 and macOS on Apple silicon;
both stay experimental. It is not a Python replacement. The commands that
worked on a single repository (`init`, `collect`, `status`, `repair`,
`claude-plan`, `claude-apply`, `claude-remove`, `hook-apply`, `hook-remove`,
`setup DIR`, `setup-plan`, `connection-status`, `connection-sync` and
`sync STATE KEY LIMIT`) are removed; they answer `invalid_command`.

The outbox is a Rust-only SQLite file under the connection folder. Its filename,
application ID and schema version refuse Python, foreign or incompatible state;
there is **no shared-writer mode**. Scope is frozen at pairing, including after
delivery. Owner-only state, no symlink ancestors or hardlinks, FULL SQLite
commits, 1400 ms lock waits, 10,000 retained rows and a 16 MiB database ceiling
bound storage. Capacity and lock failures leave existing evidence unchanged. A
delivered row is deleted in the same transaction that records its validated
receipt; the delivered counter, the last receipt and a small tombstone
(observation ID plus payload SHA-256, newest 4,000 kept) replace it, so a late
duplicate is still deduplicated or refused as a conflict. A lost response keeps
the row pending and the retry sends identical bytes. Admission reserves worst-case
SQLite table/index and overflow pages for every **pending** row, so capacity
returns as rows are acknowledged. The reservation still refuses new rows before the
physical file is full (several hundred pending rows); the 10,000-row limit is an
additional ceiling, not a promised capacity. Same-UID processes are trusted, not
sandboxed.

Frozen ASCII allowlisted wire bytes are capped at 16 KiB. Sync commits its
rotating cursor before HTTP, retains exact bytes after crash or response loss, and
acknowledges only the exact Rails receipt. 401/403 (revocation), 429, 5xx and
network failures stop the batch; row failures do not starve later rows. A
`Retry-After` on 429 (seconds or an HTTP date, at most one day) is saved in the
spool, and later syncs stop with `sync_rate_limited` before any request until it
passes. No redirects or ambient proxies; verified TLS except exact loopback HTTP;
each request has a five-second timeout including the response body. Only
`hook claude` and `hook codex` have the silent deadline/exit contract below. No
daemon, live host qualification, native Windows support, Developer ID signing,
notarization or Python retirement is included. Rails and human-minute attribution are unchanged.

Reproduce the checks (Rust 1.98.1, Python >=3.11). They run on Linux and on an
Apple-silicon Mac, where the install harness uses a fake `launchctl`:

```sh
cargo fmt --manifest-path collector-rs/Cargo.toml --check
cargo test --locked --manifest-path collector-rs/Cargo.toml
cargo clippy --locked --manifest-path collector-rs/Cargo.toml --all-targets -- -D warnings
PYTHONPATH=src python3 collector-rs/tests/conformance.py -v
python3 collector-rs/tests/install_script_test.py -v
cargo audit --file collector-rs/Cargo.lock
bin/test
```

The Rails/Puma/PostgreSQL interoperability harnesses (`rails_interop.rb`,
`pairing_rails.py`) still use the removed repository-scope commands and do not run
until they are requalified on installation scope:
[#57](https://github.com/techwright-lab/devdiary-cli/issues/57).

### Releases

`.github/workflows/release.yml` publishes a GitHub release when a tag `vX.Y.Z` is
pushed. The tag must equal the crate version in `collector-rs/Cargo.toml`, or the
job fails. It builds `aarch64-apple-darwin` natively on an Apple-silicon runner and
a static `x86_64-unknown-linux-musl` binary, both with Rust 1.98.1. The macOS
binary keeps the linker's ad hoc signature: the release profile does not strip,
and the job fails unless `codesign --verify` passes on the archived binary. The
release carries `devdiary-X.Y.Z-<target>.tar.gz` for each target, `SHA256SUMS` and
`install.sh`. The job writes the tag into the `RELEASE_TAG=unreleased` line of
`collector-rs/install.sh`, and fails when that file is missing. The script
downloads the archive for the machine's target from its own release, refuses it
unless it matches `SHA256SUMS`, and renames it into `~/.local/bin/devdiary`. With
a terminal open it then runs `devdiary setup` on that terminal and exits with
setup's status. Without one it installs only. `sh -s -- --version X.Y.Z` installs
another release. `collector-rs/tests/install_script_test.py` runs it under dash
and `bash --posix` against a local release fixture in a throwaway home. Each
archive has a GitHub artifact attestation:

```sh
gh attestation verify devdiary-X.Y.Z-<target>.tar.gz --repo techwright-lab/devdiary-cli
```

A pull request that changes the workflow, the crate manifest, the lockfile or
`install.sh` runs the same job as a dry run. So does a manual run. A dry run
builds, checks and assembles the files as a workflow artifact. It creates no
release and no attestation.

## Customer browser pairing (experimental client)

The ordinary origin is `https://devdiary.me`; no manual collector ref, ingest
endpoint, installation UUID or credential file contents are needed. This is
DevDiary-specific browser approval, not OAuth/device authorization.

`devdiary setup` runs the pairing described here, then the registration steps
under one-command setup. One pairing request covers every harness setup was
asked to connect. It names the harnesses, installation scope, a new installation
ID, the hostname, the OS name and version, the CPU architecture and the CLI
version. It names no local username, hardware ID or serial number. The terminal
prints an approval code such as `KBRT-WMPH` and the link
`<origin>/collector/approve?code=KBRT-WMPH`, and opens that link in the browser.
The approval code is not a secret: it only finds the request.

- Setup listens on `127.0.0.1` only, on a port the operating system picks, and
  names that port in the request. After approval, the browser returns to
  `http://127.0.0.1:<port>/callback` with a verifier. Setup takes it only from a
  request that names its own approval code, with the `Host` header
  `127.0.0.1:<port>`, and answers "Connected. Return to the terminal." once the
  connections are saved. If setup cannot listen, the request names no port, and
  the approval page shows the verifier behind "Terminal didn't connect?".
- The terminal also accepts the verifier typed in at any point while it waits.
  Case, hyphens and spaces do not matter. Presenting the verifier is the only way
  to receive connections: one per harness, each with its own credential, outbox
  and revocation. Setup then prints the name of the workspace it connected to.
- A denied request stops setup with `pairing_denied`. Five wrong verifiers deny
  the request too. Nothing local changes, and the next `setup` starts afresh.
- An expired request starts a fresh one and prints its new code and link. The
  start rate limit still applies, and its `Retry-After` cooldown survives a
  process restart. Status polls wait the advertised interval (at least five
  seconds) and honour an integer `Retry-After`.
- The client never falls back to the older pairing contract.
- Verified TLS, no redirect following, no ambient proxy use, bounded responses and
  five-second request deadlines. Alternate origins require both `--origin ORIGIN`
  and `--trust-origin`; HTTP is accepted only for numeric loopback development.
  `--no-browser` prints the link without opening it. No pairing or collector
  bearer is ever put in a URL, child argument, environment variable, vendor
  settings or diagnostic.
- Ctrl-C while setup waits leaves nothing behind. Only presenting a verifier can
  issue connections, so setup marks the folder before it does. If the answer to a
  presented verifier is lost, or a saved connection fails to write, setup refuses
  to pair again in that folder. Revoke unused connections in the browser, move the
  connection folder aside, and run `setup` again; it then pairs a new
  installation. There is no secret replay or recovery endpoint. If
  `connection.json` was committed, ordinary `setup` resumes local initialization
  without issuing another credential.
- `connection.json` atomically commits the token and frozen scope together (0600,
  fsync, no overwrite); credentials stay outside `outbox/`. Do not print or commit
  it. A killed writer can leave a private `.pairing-*.tmp`; these are not adopted
  as credentials. An existing connection cannot be re-paired/retargeted, even
  after delivery. To pair again, move the connection folder aside; it keeps its
  outbox. Revocation is server-side. This slice does not implement credential
  rotation.
- Browser approval never supplies local hook consent or Claude trust; setup asks
  for that separately in the terminal. Organization policy is respected, not
  deleted or bypassed. `doctor` reports host trust and runtime qualification as
  unknown.
- Sync treats 401/403 as revocation: it writes `connection/revoked`, stops, and
  refuses later syncs. The hook then stops spooling and counts a `revoked` drop.

Verification: `python3 collector-rs/tests/pairing.py -v` runs `devdiary setup`
against `collector-rs/tests/pairing_server.py`, a local server that applies the
pairing contract's rules. Debug builds also honour `DEVDIARY_DEBUG_OPENER` (the
program that opens the link) and `DEVDIARY_DEBUG_CALLBACK_PORT` (the port setup
tries to listen on); release builds read neither. The real Rails pairing harness
waits for [#57](https://github.com/techwright-lab/devdiary-cli/issues/57).

## One-command setup (systemd user timer on Linux, launchd agent on macOS)

The Rust binary is `devdiary`. It accepts `--vendor codex` on `setup`, `doctor`,
`upgrade`, `uninstall`, `outside-repos` and `orchestrators approve`. Claude remains
the default. Codex uses its active `CODEX_HOME/hooks.json` (otherwise
`~/.codex/hooks.json`), a separate connection/spool under `cli/codex`, and a
separate `devdiary-codex-sync.timer` (`me.devdiary.codex-sync` on macOS). Review changed definitions in
Codex's ordinary `/hooks` screen after setup or upgrade. Registration and
fixture success do not qualify stock lifecycle coverage or counted minutes.
C1 acceptance and current evidence live in the canonical vault plan:
`_vault/products/devdiary/docs/harness-plan.md` in the TechWright workspace.

Build the debug collector first with
`cargo build --locked --manifest-path collector-rs/Cargo.toml`.
Pinned stock qualification is opt-in through
`python3 collector-rs/tests/qualify_codex.py --help`; without provider consent it
runs no vendor process. It requires the complete Codex 0.166.0 package and pinned
SHA, preserves the resolver symlink target in its isolated filesystem, and uses
the ordinary hook-trust UI. CI runs only its network-inert control tests. Passing
capture gates does not establish server minute correctness.

`setup` runs the browser pairing above, then plans the hook entries, asks in the
terminal and applies them, for an installation-scope connection, and delivers the
spool from the scheduler. The hook contract does not change: it stays silent, exits 0
and never performs HTTP.

```sh
cargo build --release --locked --manifest-path collector-rs/Cargo.toml
collector-rs/target/release/devdiary setup \
  [--settings ~/.claude/settings.json] [--record-orchestrated-outside-repos] \
  [--origin ORIGIN --trust-origin] [--no-browser]
devdiary doctor
devdiary sync
devdiary upgrade [--yes]
devdiary uninstall
```

`setup` does these steps in order, and stops at the first failure:

1. Refuses at once (`consent_required`) when stdin is not a terminal; release one
   has no unattended setup. It then checks the machine before it sends anything:
   it refuses an OS or CPU that release one does not target
   (`platform_unsupported`), a scheduler it cannot reach
   (`scheduler_unreachable`: no `systemctl --user` session on Linux, no GUI login
   session on macOS, for example SSH only), and a running binary that is not this
   build's executable format (`executable_format_unsupported`: ELF on Linux, a
   64-bit thin or universal Mach-O on macOS). `upgrade` runs the same check. It refuses a relative `--settings` path (`unsupported_path`), a
   missing settings file (`settings_missing`), settings that are not valid JSON
   (`invalid_settings`), already hold a `devdiary` hook (`already_registered`) or a
   hook of the older `devdiary-collector` (`old_collector_registered`: run
   `devdiary-collector uninstall` first, then set up again with a new pairing), or
   register the Python observer. It warns, without stopping, when another
   `devdiary` comes first on `PATH`. An alternate `--origin` without `--trust-origin` is refused, as for
   `setup`. A saved connection that the server revoked is refused
   (`connection_revoked`): run `uninstall`, move the connection folder aside (its
   pending rows stay there) and run `setup` again. Nothing is paired or changed.
   If an earlier setup stopped while it wrote the settings, or before it saved its
   install record, it left a copy named `<settings file>.devdiary-backup`. Setup
   offers to restore that copy before it reads the settings. Declining keeps both
   files and stops with `backup_kept`.
2. Prints the 10 hook entries and the settings file they go in, and asks `[y/N]` on
   the terminal. Declining sends nothing and changes nothing.
3. Pairs installation scope into `~/.local/share/devdiary/cli/claude/connection`
   (`cli/codex` for Codex). It never reads the old `devdiary/collector` folder.
   This path is fixed under `$HOME`; `$XDG_DATA_HOME` is not used, so every shell
   finds the same install. If setup stops after this step, the next run reuses the
   saved connection, asks again and does not pair again.
4. Copies the running binary to `~/.local/share/devdiary/cli/HARNESS/VERSION/` (0700),
   so the registered hook survives rebuilds and `cargo clean`.
5. Plans the hook entries for that copy and applies the plan. Entries that differ
   from the ones shown, or settings edited after step 1, give `stale_plan`, and the
   settings are left exactly as they are; run `setup` again. Before the write, setup
   copies the settings to `<settings file>.devdiary-backup` with the same mode. It
   removes the copy once the install record that names the write is saved. The
   plan never holds a copy of the settings.
6. Writes `devdiary-claude-sync.service` and `.timer` (`devdiary-codex-sync` for
   Codex) under
   `~/.config/systemd/user/` (`$XDG_CONFIG_HOME` when set), then
   `systemctl --user daemon-reload` and `enable --now` the timer. The timer runs
   `sync CONNECTION_DIR` 30 s after it starts and then 2 min after each run
   (`RandomizedDelaySec=30`, `Nice=10`). On macOS it writes
   `~/Library/LaunchAgents/me.devdiary.claude-sync.plist` (`me.devdiary.codex-sync`
   for Codex), owned by you and not writable by group or others, then runs
   `launchctl bootstrap gui/UID PLIST`. The agent runs `sync CONNECTION_DIR` at load
   and every 120 s (`ProcessType` Background, `Nice` 10, `LowPriorityIO`), with every
   path in `ProgramArguments` and no log paths. Before it asks, setup says that macOS
   shows a background-item notification and lists the agent under Login Items, and
   that switching it off there stops delivery. Runs missed during sleep are not made
   up; the outbox keeps the rows for the next run.
7. A local self-test: the installed hook receives a `SessionStart` whose session id
   starts with `devdiary-selftest-`. The hook records it only in a local
   `self_test` table, which sync never reads; the outbox refuses such ids.
8. Reads the scope endpoint once. If the server offers orchestrator names, prints
   them and asks `[y/N]`. Declining keeps the hooks.

`sync CONNECTION_DIR` sends batches of 100 at most 10 times and stops at the first
batch that a 401/403, 429, 5xx or network failure stops, when no full batch is
left, or once it has visited every row that was pending when it started. Rows the
server rejects (other 4xx, invalid receipts) stay pending and are sent at most once
per run. Server refusals and rejections end the run with exit 0 and are kept for
`doctor` as `last_sync_error` (for example `http_422`); local failures fail the
unit.
`sync` with no argument does the same now, for every installed harness.

Each sync batch refreshes the scope first. A
network failure, a non-200 reply, a read failure, or an oversized or invalid reply
keeps the last good refs, offer, approvals and declared-run answer, and spooled rows
are still delivered.
A refused orchestrator entry is reported the same way, but the new refs still apply,
and an offer is saved without the entry only when no saved offer has entries yet.
The last code is kept for `doctor` as `last_scope_refresh_error`, until a good
refresh clears it. Revocation and a
server-requested wait still stop the run. `setup` and `upgrade` fail on any
refresh error.

`doctor` prints the registration state, `systemctl --user is-enabled/is-active` for
the timer (on macOS, `loaded` from the exit status of
`launchctl print gui/UID/LABEL`, whose output it never reads), pending and
delivered counts, drops, the last receipt, `last_sync_error`, `last_sync_at` (the
last sync run, from the sync status file) and `last_scope_refresh_error`. When the harness settings file
is not valid JSON, the registration reads `settings_unreadable` with the file's
path, the error's line and column, and any backup setup left beside it. `doctor`
still runs the other checks, names the fix on stderr, writes nothing and exits 1.
`upgrade` first checks that the settings would accept a fresh
plan once the old entries are gone and that the connection was not revoked, prints
the 10 new entries and asks once; only then does it remove the old registration, copy
the new binary over the pinned one and install the fresh plan. Declining leaves the
running hooks and their pinned binary untouched. The install
record is replaced only on success, so after a failed upgrade `uninstall` still
finds and disables the timer. `uninstall` disables and deletes the timer units,
then removes the hook entries. Without a user systemd bus it still deletes the unit
files and removes the hooks, then exits 2 with `timer_unconfirmed`. On macOS it runs
`launchctl bootout gui/UID/LABEL`, deletes the plist, then removes the hooks. An
agent that is not loaded counts as stopped, and one still loaded after bootout
gives `timer_unconfirmed`. `upgrade` rewrites the plist for the new pinned copy,
then unloads and reloads the agent. Leftover units without an install record are
also disabled and deleted. If setup stopped before saving its install record,
`uninstall` removes that run's hook entries and settings backup. A second
`uninstall` is a no-op. Settings keep their key order and their compact or
pretty-printed layout;
string escapes and number spellings come back in canonical JSON form (`\u00e9`
returns as `é`, `1.50` as `1.5`), so such files keep their meaning but not their
sha256. The connection
and any pending rows stay; revoke the connection in the browser to retire the
credential. Headless machines need `loginctl enable-linger` for the timer to run
while logged out.

### Sessions outside repositories

By default your own sessions outside a git repository are not recorded.
`devdiary outside-repos on`, in a terminal, turns that on. A row holds the event, the time and the tool name, never a path. Rows
under `/tmp`, `/var/tmp` and the system temp folder stay out, because background tools
run throwaway sessions there. A run with an unanswered orchestrator offer stays out too.
`devdiary outside-repos off` turns it off, with no terminal needed. `doctor`
shows the choice as `outside_repos`. It is a local file, so the saved scope does not
change and no new pairing is needed. The server must accept rows with no repository
first; older servers refuse them and they stay pending.

### Orchestrator names need local approval

Approval alone is not enough. Each control plane must also start sessions that load the
collector hooks. Paperclip needs one service setting: see the
[Paperclip guide](docs/integrations/paperclip.md).

A control plane (an orchestrator, a tool that starts agents) marks its runs with
environment variables. The collector holds no list of these tools. A workspace
manager turns one on in DevDiary, and the scope endpoint then offers its variable
names, for example `PAPERCLIP_AGENT_ID` for the `paperclip` preset:

```json
{"orchestrators": [{"key": "paperclip", "env": {
  "agent_ref": "PAPERCLIP_AGENT_ID", "run_ref": "PAPERCLIP_RUN_ID",
  "task_ref": "PAPERCLIP_TASK_ID",
  "checkout_path": ["PAPERCLIP_WORKSPACE_WORKTREE_PATH", "PAPERCLIP_WORKSPACE_CWD"]}}]}
```

Every scope refresh stores this list as `orchestrators-offered.json` (0600) in the
outbox. The hook takes no names from it. Of the names a server can choose, the hook
reads only those in `orchestrators-approved.json` (0600). The one fixed name it also
reads is `DEVDIARY_RUN_REF`, below. Three commands write that file, always after
the same prompt on a terminal: `setup`, `upgrade` and:

```sh
devdiary orchestrators approve [CONNECTION_DIR]
```

The prompt prints each key with its refs and its checkout paths as separate groups.
When an approved list already exists it also prints what was added and removed. It
asks `[y/N]` and saves what it printed. Without a terminal `orchestrators approve`
refuses with `approval_required`, and it has no `--yes`. `upgrade --yes` finishes
without a terminal and leaves the change waiting; its `--yes` covers only the hook
entries. So a server cannot widen what the hook reads unless a person agrees.

When the offer changes, the approved list keeps working unchanged, also when the
offer gets smaller. `doctor` (`orchestrators`) reports that a change waits for
approval. `refused` counts entries that were refused. When a
preset is offered and not approved, `doctor` prints a line on stderr, and `notice`
holds the same text.

A bad offered file stops no command. `doctor` reports `offered_list_unreadable`;
the next scope reply that has the field replaces the file. `orchestrators approve`
refuses with `offered_list_unreadable` until then. A file with the wrong owner or mode
is different: as for all state, every command refuses until it is fixed.

The collector refuses a whole entry if one name does not match
`^[A-Z][A-Z0-9_]{0,63}$`, or contains `SECRET`, `KEY`, `AUTH`, `COOKIE`, `PASS`, `PWD`,
`CRED`, `TOK`, `DSN`, `BEARER`, `JWT`, `PRIVATE`, `SESSION` or `DATABASE_URL`, or has
`PAT` as a whole `_`-separated segment (`GH_PAT`, not `WORK_PATH`), or is exactly one
of `HOME`, `PATH`, `USER`, `LOGNAME`, `PWD`, `OLDPWD`, `SHELL`, `TERM`, `LANG`,
`HOSTNAME` or `TMPDIR`, which are set in every shell. It also refuses an entry with
more than 8 names or without a ref, and a list with more than 8 entries. The approved file is checked
again on each read: if it fails, the hook reads no names and drops the row (`error`).

If two approved entries match the environment, the first in the list wins. Values
are never logged. A ref is sent only when it is a UUID. Any matching variable, even
with a malformed value, marks the run as orchestrated: the row carries `orchestrator`
with the preset key, so the server does not treat it as the machine owner's own work.
Rows from other runs have no `orchestrator` field. `checkout_path` values are local
hints used when `cwd` is not a repository; they never leave the machine.

An offer nobody answered fails closed. While the approved list is empty or unreadable
and the offer has an entry, a refused entry or cannot be read, every row carries
`orchestrator: "unapproved"`. The hook reads no offered name for these rows, so
they have no refs and use no checkout path. The server keeps them with binding
unknown, so an agent run never reads as the machine owner's work, and `doctor` says
runs stay unknown until approved. Outside a repository such a row is dropped
(`outside_scope`), also with `--record-orchestrated-outside-repos`. `unapproved` is
reserved and cannot be a preset key. With an unreadable approved list and no offer the
hook drops the row, as above.

Once an approved list exists, a preset added to the offer is not read until approved.
Its runs can look like ordinary runs, and `doctor` says so.

### Runs started by `devdiary-launcher run`

`devdiary-launcher run` exports `DEVDIARY_RUN_REF` (`urn:devdiary:run:<uuid>`) and declares a
session with that ref. The hook sends the same ref in the optional v2 field
`declared_run_ref`, so the server can match the observed visit to the declared
session and show one run once.

The scope request lists the optional reply keys this collector understands:

```text
DevDiary-Scope-Keys: accepts_declared_run_ref
```

The server must send `accepts_declared_run_ref` only to a request with that header.
Older collectors do not send it, and they refuse a scope reply with a key they do not
know (`scope_refresh_invalid_response`), which keeps their refs and offer stale until
`upgrade`.

The field needs a `true` answer in the scope reply:

```json
{"accepts_declared_run_ref": true}
```

Each good refresh saves the answer in the outbox database. A missing key or `false`
turns the field off, and a reply that does not list schema v2 does too. A non-boolean
value makes the reply invalid (`scope_refresh_invalid_response`), so the last good
answer stays. Until the first `true`, the hook does not read the variable, and the outbox
refuses a row with the field.

The name is fixed and is DevDiary's own, so it needs no local approval and the server
cannot change it. The hook sends the value only when it exactly matches
`urn:devdiary:run:` plus a lowercase UUID. Otherwise it leaves the field out and keeps
the row. The field is independent of `orchestrator`, so an orchestrated or `unapproved`
row can carry it. Schema v1 rows never send it.

On Claude Code 2.1.314, a stock `claude -p` run passed the variable to the command
hooks for SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop and
SessionEnd.

Verification: `python3 collector-rs/tests/install_test.py -v` (temp HOME, a mock
pairing/ingest server and a fake systemctl, or a fake launchctl on macOS; debug
builds only honour `DEVDIARY_COLLECTOR_SYSTEMCTL` and `DEVDIARY_COLLECTOR_LAUNCHCTL`,
and release builds run only `/bin/launchctl` on macOS; release builds run the first of `/usr/bin/systemctl`,
`/bin/systemctl` and `/run/current-system/sw/bin/systemctl`, never a `PATH` lookup).

## Rust Claude raw-hook adapter: fixture-qualified only

`devdiary setup` registers `hook claude PLAN` (or `hook codex PLAN`). It accepts stock Claude command-hook stdin for SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse, PostToolUseFailure, PermissionRequest, Stop, SubagentStart,
SubagentStop and SessionEnd, following `src/devdiary/observer_hook.py`. It uses
only the installation's Rust outbox, which the timer's `sync` delivers; it never invokes
Python, reads a transcript, makes a network call, or reads provider credentials.

**Qualification:** executable/shell fixtures, Python-reference metadata parity,
local HTTP delivery, concurrency and failure tests are not a successful stock
Claude session. No live Claude settings were changed to develop this slice.
A separately consented smoke still requires an unmodified Claude installation,
disposable settings/repository, existing subscription authentication in normal
HOME, and explicit provider-usage approval. Verify real lifecycle/tool events,
unknown attribution, ordinary host output, delivery and clean removal. Do not
bypass a trust prompt or call this runtime-qualified before that gate. Windows
is unsupported (the crate requires Unix). macOS on Apple silicon passes the same
fixtures but stays experimental: it has no CI job or recorded real-Mac run yet. Codex is deliberately deferred: its
future adapter must implement its own events, turn/child correlation and exact
host trust/setup contract rather than aliasing the Claude command.

Registration, review and removal go through `devdiary setup` (which prints the
entries and asks before writing them), `devdiary doctor` and `devdiary uninstall`.

- The executable is an absolute, owner-only native ELF file with private/trusted
  ancestors, shell-quoted paths and a SHA-256 pin verified at plan and apply. The
  hook compares a stat fingerprint (device, inode, size, mtime and ctime in ns)
  saved in the outbox after the last full hash; any replacement, write or chmod forces
  a full re-hash. Each sync hashes its own binary and saves that fingerprint (or
  discards the saved one when hashing fails). Symlinks, hardlinks and
  shared state permissions are refused, not repaired. Settings and their directory
  only need to be owned by you and not group/world-writable, with exactly one link
  and no symlinked ancestor; a replaced settings file keeps its original mode.
  Setup refuses settings that already register the Python observer hook
  (`python_observer_registered`).
  Updates require removal and a freshly reviewed plan; removal remains possible
  after executable/spool failures. Same-UID processes remain trusted.
- Plan and settings are bounded to 64 KiB. The plan stores only ownership, hashes
  and scope, **not a copy of settings secrets**. Setup applies only after your
  answer in the terminal. Apply validates the complete proposal, including original key
  presence against the hash-matched settings, before persisting ownership. Unknown
  fields, duplicate keys and inconsistent removal metadata are refused. The private
  `$PLAN.registration.json` receipt binds all validated plan fields by hash; retain
  it with the plan. Apply rejects changed settings/scope/binary; retry of an already
  exact installation is harmless. Edited, duplicate or partially missing owned
  entries block removal instead of deleting customer changes. Pre-receipt
  experimental installations are not adopted automatically.
- Setup/removal use a stable, nonblocking advisory lock (`outbox/claude-settings.lock`,
  so the settings directory need not be private) and private fsynced atomic
  replacement, with an immediate pre-rename content check. Cooperating hooks hold
  a shared lock. Noncooperating editors cannot be made transactional: keep the
  host/settings editors quiescent. A crash leaves the old or complete new JSON;
  run `setup` or `uninstall` again. Removal records its target hash before replacing settings
  and retains a final removal receipt, so retry after the settings rename completes
  without reinstalling hooks. A prepared-but-unapplied registration can be removed.
  If settings change after an interrupted removal's rename, recovery fails closed
  for inspection rather than guessing ownership. A private temporary file can
  survive a killed writer. Unrelated values/hooks survive removal. Apply keeps the
  file's key order, indentation and trailing newline and appends new keys last, so
  removal restores the original bytes for files written as `JSON.stringify(v, null,
  indent)` (Claude's own format); other layouts are restored as the same JSON. The private plan, hash-only receipt,
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
- Raw stdin is capped at 16 MiB; fields outside the allowlist (prompts, tool input
  and responses) are skipped without being parsed into memory. The hook runs its
  cheap checks first: parse and event allowlist, plan, registration receipt,
  revocation, repository scope, executable fingerprint, and only then the settings
  re-parse and SQLite. A whole-process 700 ms watchdog covers stdin,
  validation, filesystem and SQLite work; the registered command also has a
  two-second host timeout. The command exits zero with no stdout/stderr on malformed
  input, missing registration, timeout, lock/full-spool/permission failures or panic.
  A shell neutralizer also silences missing/broken executable failures. OS-level
  scheduling stalls remain outside an application deadline guarantee. Failed
  captures are dropped, not retried by the hook: zero exit is **not** proof of
  capture. Every refusal after the plan loads is counted by reason
  (`outside_scope`, `spool_full`, `permission`, `invalid`, `observation_conflict`,
  `revoked`, `stale_plan`, `busy`, `error`) as one byte appended to `outbox/drops.log`
  without opening SQLite; sync folds it into the spool's `drops` table, and
  `doctor` prints the totals. Drops before the plan loads cannot be attributed to
  an outbox and are not counted.

Additional Linux fixture gate:

```sh
PYTHONPATH=src python3 collector-rs/tests/claude_hook.py -v
```

## Repeatable stock-Claude qualification (live mode currently blocked)

**The collector stages are removed until they are requalified on installation
scope ([#57](https://github.com/techwright-lab/devdiary-cli/issues/57)).** After its
preflight checks the harness stops at `installation_scope_requalification_required`,
before any collector command or model run. The registration, spool and Rails
descriptions below record the repository-scope design that #57 replaces.

**Live qualification also fails closed before any vendor execution.** Local
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
  --collector "$PWD/collector-rs/target/release/devdiary" \
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
devdiary-launcher observer connect --state-dir "$STATE" \
  --endpoint https://app.example.test/ingest/v1/observations \
  --collector-ref "$COLLECTOR_REF" \
  --repository-ref https://github.com/OWNER/REPO \
  --key-file "$PRIVATE_KEY_FILE" --consent
devdiary-launcher observer sync --state-dir "$STATE" --limit 100
devdiary-launcher observer health --state-dir "$STATE"
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

`devdiary-launcher observer discover [--vendor claude|codex]` checks PATH executables and
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
place of `devdiary-launcher`. Only `apply --consent` and `remove --consent` change settings.
Planning writes nothing; redirect its JSON output to a private plan file yourself.
Do not point a fixture run at live user settings.

```sh
# REPO, SETTINGS, STATE, PLAN, PYTHON are absolute, user-selected fixture paths.
# REPO must already exist. STATE must be absent or private (0700).
devdiary-launcher observer plan --vendor claude --repository "$REPO" --settings "$SETTINGS" \
  --state-dir "$STATE" --executable "$PYTHON" > "$PLAN"
# Inspect the plan, stop concurrent settings editors, then approve:
devdiary-launcher observer apply --plan "$PLAN" --consent
devdiary-launcher observer health --state-dir "$STATE"
devdiary-launcher observer observations --state-dir "$STATE" --limit 100
# Retains local metadata; stale loaded registrations become no-ops:
devdiary-launcher observer remove --state-dir "$STATE" --consent
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
devdiary-launcher init \
  --namespace urn:acme \
  --principal-ref urn:acme:human:owner \
  --endpoint https://app.example.test/ingest/v1/sessions

devdiary-launcher actor add \
  --ref urn:acme:actor:reviewer \
  --display-name "Code Reviewer" \
  --attester-ref urn:acme:attester:local-launcher \
  --lane review \
  --identity paperclip_agent_id=agent-123

devdiary-launcher adapter add \
  --name my-runtime \
  --runtime custom-cli \
  --session-ref-env DEVDIARY_RUNTIME_SESSION_REF_MY_RUNTIME \
  --provider-env DEVDIARY_RUNTIME_PROVIDER_MY_RUNTIME \
  --model-env DEVDIARY_RUNTIME_MODEL_MY_RUNTIME

devdiary-launcher doctor --actor urn:acme:actor:reviewer

devdiary-launcher run --actor urn:acme:actor:reviewer -- your-command
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
devdiary-launcher emit pending
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
