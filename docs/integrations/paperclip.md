# Paperclip

> [!IMPORTANT]
> **Paperclip needs one setting, or its Claude agents record nothing.**
> Set `ACPX_CLAUDE_INCLUDE_USER_SETTINGS=1` in the Paperclip service environment and restart it.
> Nothing warns you locally when this is missing: no row, no drop, no error.

## Why

Paperclip starts new Claude agent sessions through `acpx`, with `--setting-sources=project,local`. That leaves out user settings, and `devdiary-collector install` writes its hooks to user settings, `~/.claude/settings.json`. So those sessions never run the hooks.

Resumed sessions do load user settings. A partly working install can therefore show a few agent rows and hide the gap.

## Setup

1. Install the collector, if you have not:

   ```sh
   devdiary-collector install --settings ~/.claude/settings.json --record-orchestrated-outside-repos
   ```

2. In DevDiary, open Settings, Collector, Control planes, and turn on Paperclip.

3. Approve the environment names:

   ```sh
   devdiary-collector orchestrators approve
   ```

4. Add the setting. With a systemd user service:

   ```sh
   mkdir -p ~/.config/systemd/user/paperclip.service.d
   cat > ~/.config/systemd/user/paperclip.service.d/50-acpx-user-settings.conf <<'CONF'
   [Service]
   Environment=ACPX_CLAUDE_INCLUDE_USER_SETTINGS=1
   CONF
   systemctl --user daemon-reload
   ```

   Other setups: set the variable wherever the Paperclip server process gets its environment. Setting it on one agent does not work, because the server process reads it.

5. Restart Paperclip when no agent is running. A restart stops runs in progress.

   ```sh
   systemctl --user restart paperclip
   ```

6. In DevDiary, bind each agent to an actor in Cast.

## What the setting changes

Every new Claude agent session loads all your user settings, not only the hooks: plugins, MCP servers, permissions and your global `CLAUDE.md`. Sessions start a little heavier. Resumed sessions already work this way.

## Check that it works

Wait for a new agent session to start, then look at its flags:

```sh
ps -eo args | grep -- '--setting-sources'
```

You want `--setting-sources=user,project,local`. If you see `project,local`, the setting did not reach the server process.

Then open Settings, Collector in DevDiary. The Paperclip row warns when it is on but no agent run has arrived in the last 7 days.

## If rows still do not arrive

- **Agents work in a folder outside any repository:** install with `--record-orchestrated-outside-repos`.
- **Rows stay `unknown`:** run `devdiary-collector orchestrators approve`.
- **Drops rise:** run `devdiary-collector doctor` and read `drops`.

## Not covered

Only Claude Code has collector hooks. Paperclip agents on Codex or Hermes record nothing through this path.
