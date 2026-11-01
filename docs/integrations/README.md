# Integrations

A control plane is a tool that starts agents for you. The collector tracks harnesses such as Claude Code. A control plane only labels which agent ran.

**Each control plane may need its own setup. Enabling it in DevDiary is not enough.** If a control plane starts sessions that do not load the collector hooks, its agents record nothing, and nothing warns you locally.

| Control plane | Setup needed | Guide |
|---|---|---|
| Paperclip | Yes: one service setting | [paperclip.md](paperclip.md) |

## Every control plane needs three things

1. Enable it in DevDiary, under Settings, Collector, Control planes.
2. Approve its environment names on the machine: `devdiary-collector orchestrators approve`.
3. Make sure its sessions load the settings file that holds the collector hooks.

Step 3 is the one that fails quietly. The collector cannot see a session that never ran its hooks.
