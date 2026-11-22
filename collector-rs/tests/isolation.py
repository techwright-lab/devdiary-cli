"""Keeps every harness off the real ~/.claude and ~/.codex.

Before #68, `install_test.py` passed CODEX_HOME through from the caller, and a
run with it set wrote DevDiary hooks into the real Codex hooks file. A test
module calls `guard()` from `setUpModule`. From then on the harness process
has no HOME, CODEX_HOME or CLAUDE_CONFIG_DIR of the caller's: HOME is an empty
folder made for the run, and the other two are unset. Every process the
harness starts is checked first, and one whose environment leads to the real
home folder does not start.

The live qualification scripts read the vendor login from the real home on
purpose, so they do not call `guard()`; they pass each environment they build
through `checked()` instead.
"""

import atexit
import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

# The variables that tell a harness, or DevDiary, where its settings live.
NAMES = ("HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR")


class RealHome(RuntimeError):
    """A harness was about to start a process that could reach the real home."""


def real_home():
    """The home folder the account database names, whatever HOME says."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def inherited(environ=None):
    """The caller's environment without the variables that locate a home."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if k not in NAMES}


def homes(env):
    """Where a process started with `env` finds its home, Claude Code's folder
    and Codex's. Without HOME, Claude Code and Codex use the account's home."""
    home = Path(env["HOME"]) if env.get("HOME") else real_home()
    return {
        "HOME": home,
        "CLAUDE_CONFIG_DIR": Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
        "CODEX_HOME": Path(env.get("CODEX_HOME") or home / ".codex"),
    }


def checked(env):
    """`env`, once none of its homes is the real home folder or inside the
    real ~/.claude or ~/.codex. Raises RealHome otherwise."""
    real = real_home()
    private = (real / ".claude", real / ".codex")
    for name, path in homes(env).items():
        resolved = path.resolve()
        if resolved == real or any(
            resolved == p or p in resolved.parents for p in private
        ):
            raise RealHome(
                f"{name} leads to the real home folder ({resolved}): "
                "the harness refused to start the process"
            )
    return env


class Guarded(subprocess.Popen):
    """subprocess.Popen that starts nothing the real home is reachable from."""

    def __init__(self, args, *rest, env=None, **options):
        checked(os.environ if env is None else env)
        super().__init__(args, *rest, env=env, **options)


def guard():
    """Takes the caller's homes out of this process and checks every process
    it starts from now on. Calling it again changes nothing."""
    if subprocess.Popen is Guarded:
        return
    for name in NAMES:
        os.environ.pop(name, None)
    home = tempfile.mkdtemp(prefix="harness-home-")
    atexit.register(shutil.rmtree, home, ignore_errors=True)
    os.environ["HOME"] = home
    subprocess.Popen = Guarded
