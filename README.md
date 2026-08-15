# devdiary

A runtime-dependency-free Python launcher for the DevDiary Agent Attribution Contract.
It keeps stable actor identity separate from runtime/model metadata, removes the
actor-bound ingest key from child environments, writes a read-only context file
(`0400` on POSIX), captures Git work references, and posts one idempotent terminal
envelope.

This package is an early portable artifact. It intentionally starts in `warn`
mode and does not install hooks or enforce attribution for ordinary human Git
commands.

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
No entry is queued when a key is absent. Retry without minting a new event
identity:

```text
devdiary emit pending
```

Set the configured key environment variable (default:
`DEVDIARY_INGEST_KEY`) only in the launcher environment. The launcher consumes
it, scrubs the inherited native environment block on Linux, and removes it
before starting `your-command`. Runtime contexts retain only a generic command
classification and argument count, never raw command arguments. Never place a
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
