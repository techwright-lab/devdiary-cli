"""Runs a collector command with a terminal on stdin and answers its questions:
the [y/N] questions and setup's choice of harnesses."""

import os
import pty
import select
import subprocess
import time

PROMPT = b"[y/N]"
CHOICE = b"[both/claude/codex]"


def questions(out):
    return out.count(PROMPT) + out.count(CHOICE)


def answering(
    args, env, answers=(b"y\n",), later=b"n\n", timeout=40, on_prompt=None, typing=None
):
    """Answer each question in turn; questions past `answers` get `later`.

    `on_prompt(index)` runs while the command waits for that answer.
    `typing(stdout so far)` may return bytes to type at any other moment.

    Returns (exit status, stdout, stderr, number of questions asked).
    """
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [str(a) for a in args],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        os.close(slave)
        slave = None
        out, err, asked = b"", b"", 0
        streams = {process.stdout.fileno(): "out", process.stderr.fileno(): "err"}
        deadline = time.monotonic() + timeout
        while streams and time.monotonic() < deadline:
            ready, _, _ = select.select(list(streams), [], [], 0.5)
            for fd in ready:
                chunk = os.read(fd, 4096)
                if not chunk:
                    del streams[fd]
                elif streams[fd] == "out":
                    out += chunk
                else:
                    err += chunk
            while questions(out) > asked:
                if on_prompt is not None:
                    on_prompt(asked)
                os.write(master, answers[asked] if asked < len(answers) else later)
                asked += 1
            typed = typing(out) if typing is not None else None
            if typed:
                os.write(master, typed)
        if streams:
            process.kill()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
        return process.returncode, out, err, asked
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)
