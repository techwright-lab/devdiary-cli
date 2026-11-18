#!/usr/bin/env python3
"""PROTOTYPE, throwaway. Not production code. Nothing here touches the machine.

Question (devdiary-cli issue "What does the guided terminal setup look like,
step by step?"): what does a person see from the install command to the first
received session, and which steps does the web page mirror?

Three structurally different flows, switchable with --variant:
  A  Checklist:      detect, show the whole route, pair once, consent per harness.
  B  One at a time:  pick a harness, finish it end to end, then offer the next.
  C  Local first:    every local question first, browser approval last.

Lines starting with "  web │" show what the DevDiary setup page would show at
that moment. They are candidates, not decisions.

Placeholders: the command is `devdiary` and the install URL is
devdiary.me/cli/install.sh. Both wait on other tickets on the map. The short
code the person matches in the browser (not pastes) stands in for whatever the
pairing ticket decides; macOS uses a launchd agent as the macOS ticket assumes.

Run:
  python3 collector-rs/prototypes/guided_setup_prototype.py --variant A
  python3 collector-rs/prototypes/guided_setup_prototype.py --variant C --scenario decline
  python3 collector-rs/prototypes/guided_setup_prototype.py --list
"""
import argparse
import itertools
import sys
import time

SCENARIOS = {
    "happy": "macOS, Claude Code and Codex both installed, everything works",
    "claude-only": "Ubuntu, only Claude Code installed",
    "decline": "the person answers no to the Claude Code hook entries",
    "no-tty": "piped installer with no terminal at all (CI, container)",
    "no-systemd": "Linux without a systemd user session",
    "pairing-expired": "nobody approves in the browser within 10 minutes",
    "invalid-settings": "~/.claude/settings.json is not valid JSON",
    "leftover-backup": "an earlier run was interrupted and left a settings backup",
    "self-test-failed": "hooks written, but the local test event is not recorded",
    "no-session": "setup works, but no session arrives while the terminal waits",
}

COLOR = sys.stdout.isatty()
FAST = False


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def bold(t): return c("1", t)
def dim(t): return c("2", t)
def green(t): return c("32", t)
def yellow(t): return c("33", t)
def red(t): return c("31", t)
def cyan(t): return c("36", t)


def pause(seconds):
    time.sleep(0 if FAST else seconds)


def say(text=""):
    print(text)


def web(text):
    print(c("35", f"  web │ {text}"))


def step(n, total, title):
    say()
    say(bold(f"[{n}/{total}] {title}"))


def ok(text): say(f"  {green('✓')} {text}")
def warn(text): say(f"  {yellow('!')} {text}")
def bad(text): say(f"  {red('✗')} {text}")


def spin(label, seconds):
    if not COLOR or FAST:
        say(f"  … {label}")
        pause(seconds)
        return
    end = time.time() + seconds
    for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
        if time.time() > end:
            break
        sys.stdout.write(f"\r  {ch} {label}")
        sys.stdout.flush()
        time.sleep(0.08)
    sys.stdout.write("\r" + " " * (len(label) + 4) + "\r")


def ask(question, default_no=True):
    hint = "[y/N]" if default_no else "[Y/n]"
    answer = input(f"  {cyan('?')} {question} {hint} ").strip().lower()
    if not answer:
        return not default_no
    return answer in ("y", "yes")


def choose(question, options):
    say(f"  {cyan('?')} {question}")
    for i, (label, _) in enumerate(options, 1):
        say(f"    {i}) {label}")
    raw = input("    Choice [1]: ").strip() or "1"
    try:
        return options[int(raw) - 1][1]
    except (ValueError, IndexError):
        return options[0][1]


def stop(code, what, changed, next_cmd):
    """Every exit says what happened, what changed, and one next command."""
    say()
    bad(what)
    say(f"    {changed}")
    say(f"    Next: {bold(next_cmd)}")
    say(dim(f"    devdiary_error: {code}"))
    sys.exit(1)


# ---------------------------------------------------------------- machine


class Machine:
    def __init__(self, scenario):
        self.s = scenario
        linux = scenario in ("claude-only", "no-systemd")
        self.os = "Ubuntu 24.04" if linux else "macOS 27.2"
        self.scheduler = "systemd user timer" if linux else "launchd agent"
        self.harnesses = ["claude"] if scenario == "claude-only" else ["claude", "codex"]
        self.paths = {"claude": "~/.claude/settings.json", "codex": "~/.codex/hooks.json"}
        self.names = {"claude": "Claude Code", "codex": "Codex"}
        self.versions = {"claude": "2.3.1", "codex": "0.71.0"}


ENTRIES = {
    "claude": ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
               "PostToolUseFailure", "PermissionRequest", "Stop", "SubagentStart",
               "SubagentStop", "SessionEnd"],
    "codex": ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
              "PermissionRequest", "Stop", "SubagentStart", "SubagentStop",
              "SessionEnd", "Interrupt"],
}
BIN = "~/.local/share/devdiary/collector/0.4.0/devdiary"


def banner(m):
    target = "x86_64-unknown-linux-gnu" if m.os.startswith("Ubuntu") else "aarch64-apple-darwin"
    say(dim("$ curl -fsSL https://devdiary.me/cli/install.sh | sh"))
    pause(0.4)
    say(dim(f"  Downloading devdiary 0.4.0 for {target} … done (4.1 MB)"))
    say(dim("  Installed to ~/.local/bin/devdiary"))
    say(dim("  Starting setup. It reads your answers from this terminal."))
    pause(0.4)
    say()
    say(bold("DevDiary setup"))
    say("Records when your coding agents run, so DevDiary can show that work next to")
    say("your commits. It records session timing and tool names, never prompts,")
    say("transcripts or file contents. Nothing is changed until you say yes.")
    web("Waiting for your terminal…  (shown since the person copied the command)")


def detect(m):
    """What is found without asking."""
    spin("Checking this machine", 1.2)
    if m.s == "no-tty":
        stop("terminal_required",
             "Setup needs a terminal to ask before it changes harness settings.",
             "devdiary is installed. Nothing else was changed.",
             "run `devdiary setup` in a terminal")
    ok(f"{m.os}, scheduler: {m.scheduler}")
    if m.s == "no-systemd":
        stop("scheduler_unavailable",
             "No systemd user session, so sessions could be recorded but never sent.",
             "devdiary is installed. Nothing else was changed.",
             "log in to a desktop or SSH session with `loginctl enable-linger $USER`, then `devdiary setup`")
    for h in m.harnesses:
        ok(f"{m.names[h]} {m.versions[h]}, settings at {m.paths[h]}")
    if m.s == "claude-only":
        say(dim("  · Codex not found. Run `devdiary setup` again after installing it."))
    ok("No earlier DevDiary install")
    if m.s == "invalid-settings":
        stop("invalid_settings",
             f"{m.paths['claude']} is not valid JSON (line 14, column 3).",
             "Nothing was changed, and nothing was sent to DevDiary.",
             "fix the file or start Claude Code once, then `devdiary setup`")
    if m.s == "leftover-backup":
        warn(f"A backup from an interrupted setup exists: {m.paths['claude']}.devdiary-backup")
        say(f"    It was taken on 2026-11-18 at 09:41, before hook entries were added.")
        if ask(f"Restore it over {m.paths['claude']} now?", default_no=False):
            ok("Restored. The file is as it was before that run.")
        else:
            warn("Kept both files. Setup stops so you can compare them.")
            stop("backup_pending", "A backup from an earlier run is unresolved.",
                 "Nothing was changed.", "`devdiary setup` once you have chosen")


def pick_harnesses(m):
    if len(m.harnesses) == 1:
        return m.harnesses
    return choose("Which coding agents should DevDiary record?", [
        ("Claude Code and Codex", ["claude", "codex"]),
        ("Claude Code only", ["claude"]),
        ("Codex only", ["codex"]),
    ])


def pair(m, harnesses, step_label=""):
    """One browser approval. Code shown for the person to match, not to paste."""
    names = " and ".join(m.names[h] for h in harnesses)
    say(f"  Opening https://devdiary.me/connect in your browser.{step_label}")
    say(f"  Check that the page shows this code, then approve: {bold('WXKP-4729')}")
    say(dim(f"  It asks for: this machine (work-laptop), {names}, all repositories"))
    say(dim("  Browser didn't open? Visit the address above. The code expires in 10 minutes."))
    web(f"New connection request from work-laptop · {names} · code WXKP-4729  [Approve] [Deny]")
    if m.s == "pairing-expired":
        spin("Waiting for approval", 2.0)
        stop("pairing_expired", "Nobody approved the request within 10 minutes.",
             "Nothing was changed on this machine. The request was withdrawn.",
             "`devdiary setup`")
    spin("Waiting for approval", 2.5)
    ok("Approved in the browser by you@example.com for workspace Acme")
    web("Approved. Return to your terminal to finish.")


def show_and_ask(m, h, also_scheduler=True):
    entries = ENTRIES[h]
    say(f"  These {len(entries)} hook entries go in {m.paths[h]}:")
    for e in entries:
        say(dim(f"    {e:<20} → {BIN} {h}-hook {e}"))
    say(f"  A copy of the file is saved first as {m.paths[h]}.devdiary-backup.")
    what = f"Add these to {m.names[h]}"
    if also_scheduler:
        what += f" and start the {m.scheduler} that sends them"
    yes = ask(what + "?")
    if m.s == "decline" and h == "claude":
        yes = False
    return yes


def apply(m, h):
    spin(f"Writing {m.paths[h]}", 0.6)
    ok(f"{m.names[h]}: {len(ENTRIES[h])} entries added, backup removed")
    if h == "codex":
        warn("Codex asks you to trust new hooks. Open Codex, run /hooks and trust the 10 DevDiary entries.")
        web("Codex: waiting for you to trust the hooks in Codex (/hooks)")


def self_test(m, h):
    spin(f"Testing the {m.names[h]} hook", 0.8)
    if m.s == "self-test-failed" and h == "claude":
        stop("self_test_failed",
             "The hook ran but did not record the local test event.",
             f"Hooks and the {m.scheduler} are in place. Run the command below, or `devdiary uninstall`.",
             "`devdiary doctor`")
    ok(f"{m.names[h]} hook recorded a local test event (never uploaded)")


def scheduler(m):
    spin(f"Starting the {m.scheduler}", 0.6)
    ok(f"{m.scheduler.capitalize()} sends recorded sessions every 2 minutes")
    web("This machine is connected. Waiting for its first session…")


def wait_for_session(m, can_leave=True):
    say("  Start a session in any repository. Setup waits here for it to arrive.")
    if can_leave:
        say(dim("  Press Ctrl+C to leave; the setup page keeps waiting in your browser."))
    if m.s == "no-session":
        spin("Waiting for the first session", 2.0)
        say()
        warn("No session arrived in 15 minutes. Setup is finished; recording is on.")
        say("    The first session appears in DevDiary about 2 minutes after it ends.")
        say(f"    Next: {bold('`devdiary doctor`')} if nothing appears after your next session.")
        web("Connected, no sessions yet. [Check again]  ·  Troubleshoot: run `devdiary doctor`")
        sys.exit(0)
    spin("Waiting for the first session", 3.0)
    ok("First session received: devdiary-cli · Claude Code · 4 min")
    web("First session received ✓  devdiary-cli · Claude Code · 4 min  [Open your diary]")


def outro(m, done, skipped):
    say()
    say(bold(green("Setup complete.")))
    for h in done:
        say(f"  {green('✓')} {m.names[h]}  recording")
    for h in skipped:
        say(f"  {dim('·')} {m.names[h]}  not set up. Add it later: `devdiary setup --agent {h}`")
    say("  See your sessions: https://devdiary.me/diary")
    say(f"  Check this machine at any time: {bold('`devdiary doctor`')}")


def declined(m, h, others_left):
    warn(f"{m.names[h]} skipped. Its settings were not changed.")
    if not others_left:
        stop("consent_declined", "No coding agent was set up.",
             "Settings were not changed. The approved connection stays unused until you run setup again.",
             "`devdiary setup`")


# ---------------------------------------------------------------- variants


def variant_a(m):
    """Checklist: whole route up front, one pairing, consent per harness."""
    banner(m)
    total = 5
    step(1, total, "Check this machine")
    detect(m)
    chosen = pick_harnesses(m)
    say()
    say(dim("  Route: 2 approve in browser · 3 add hooks, one agent at a time ·"))
    say(dim("         4 test and start sending · 5 wait for your first session"))
    step(2, total, "Approve this machine in DevDiary")
    pair(m, chosen)
    step(3, total, "Add hooks")
    done = []
    for i, h in enumerate(chosen):
        say(bold(f"  {m.names[h]}"))
        if show_and_ask(m, h, also_scheduler=False):
            apply(m, h)
            done.append(h)
        else:
            declined(m, h, others_left=done or i + 1 < len(chosen))
    step(4, total, "Test and start sending")
    for h in done:
        self_test(m, h)
    scheduler(m)
    step(5, total, "Wait for your first session")
    wait_for_session(m)
    outro(m, done, [h for h in chosen if h not in done])


def variant_b(m):
    """One at a time: each harness is its own full setup, with its own approval."""
    banner(m)
    detect(m)
    order = pick_harnesses(m) if len(m.harnesses) > 1 else m.harnesses
    first = order[0]
    done = []
    for h in [first]:
        say()
        say(bold(f"Set up {m.names[h]}"))
        step(1, 3, f"Approve {m.names[h]} on this machine")
        pair(m, [h])
        step(2, 3, "Add hooks")
        if not show_and_ask(m, h):
            declined(m, h, others_left=False)
        apply(m, h)
        self_test(m, h)
        scheduler(m)
        done.append(h)
        step(3, 3, "Wait for your first session")
        wait_for_session(m)
    rest = [h for h in order if h not in done]
    for h in rest:
        say()
        if ask(f"{m.names[h]} is also installed. Set it up now? It needs a second approval"):
            step(1, 2, f"Approve {m.names[h]} on this machine")
            pair(m, [h])
            step(2, 2, "Add hooks")
            if show_and_ask(m, h):
                apply(m, h)
                self_test(m, h)
                done.append(h)
            else:
                declined(m, h, others_left=True)
    outro(m, done, [h for h in m.harnesses if h not in done])


def variant_c(m):
    """Local first: all questions on this machine, then one approval, then apply."""
    banner(m)
    total = 4
    step(1, total, "Check this machine")
    detect(m)
    step(2, total, "Choose what DevDiary may change")
    chosen = pick_harnesses(m)
    consented = []
    for h in chosen:
        say(bold(f"  {m.names[h]}"))
        if show_and_ask(m, h):
            consented.append(h)
        else:
            warn(f"{m.names[h]} skipped. Its settings will not be changed.")
    if not consented:
        stop("consent_declined", "No coding agent was chosen.",
             "Nothing was changed, and nothing was sent to DevDiary.", "`devdiary setup`")
    say(dim("  Nothing has been written yet. It happens after you approve in the browser."))
    step(3, total, "Approve this machine in DevDiary")
    pair(m, consented)
    for h in consented:
        apply(m, h)
    for h in consented:
        self_test(m, h)
    scheduler(m)
    step(4, total, "Wait for your first session")
    wait_for_session(m)
    outro(m, consented, [h for h in m.harnesses if h not in consented])


VARIANTS = {"A": variant_a, "B": variant_b, "C": variant_c}


def main():
    global FAST
    p = argparse.ArgumentParser(description="PROTOTYPE of the guided devdiary setup")
    p.add_argument("--variant", choices=VARIANTS, default="A")
    p.add_argument("--scenario", choices=SCENARIOS, default="happy")
    p.add_argument("--fast", action="store_true", help="no pauses or spinners")
    p.add_argument("--list", action="store_true", help="list scenarios")
    a = p.parse_args()
    if a.list:
        for k, v in SCENARIOS.items():
            print(f"  {k:<18} {v}")
        return
    FAST = a.fast
    print(dim(f"[prototype · variant {a.variant} · scenario {a.scenario}]"))
    try:
        VARIANTS[a.variant](Machine(a.scenario))
    except KeyboardInterrupt:
        say()
        warn("Stopped. Anything already written stays; `devdiary doctor` shows the state.")
        sys.exit(130)


if __name__ == "__main__":
    main()
