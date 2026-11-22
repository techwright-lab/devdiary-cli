"""install.sh, piped to sh as `curl | sh` does, against a local release fixture in a throwaway home."""

import fcntl
import hashlib
import io
import os
import pty
import select
import shutil
import subprocess
import tarfile
import tempfile
import termios
import time
import unittest
from pathlib import Path

import isolation

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
DOWNLOADS = "https://github.com/techwright-lab/devdiary-cli/releases/download/"
LINUX = "x86_64-unknown-linux-musl"
MAC = "aarch64-apple-darwin"
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'

FAKE_CURL = f"""#!/bin/sh
out= url=
while [ $# -gt 0 ]; do
  case $1 in
    -o) out=$2; shift ;;
    --proto) shift ;;
    -*) ;;
    *) url=$1 ;;
  esac
  shift
done
printf '%s\\n' "$url" >> "$FIXTURE/curl.log"
path=${{url#{DOWNLOADS}}}
[ "$path" != "$url" ] && [ -f "$FIXTURE/releases/$path" ] || exit 22
cp "$FIXTURE/releases/$path" "$out"
"""
FAKE_UNAME = """#!/bin/sh
case $1 in
  -s) echo "$FAKE_UNAME_S" ;;
  -m) echo "$FAKE_UNAME_M" ;;
  *) exit 1 ;;
esac
"""


def stub(version, target):
    return f"""#!/bin/sh
# devdiary {version} {target}
{{ printf 'args=%s\\n' "$*"; if [ -t 0 ]; then echo stdin=tty; else echo stdin=other; fi; }} > "$SETUP_LOG"
exit "${{SETUP_STATUS:-0}}"
"""


def stamped(tag):
    text = SCRIPT.read_text()
    assert text.count("\nRELEASE_TAG=unreleased\n") == 1
    return text.replace("\nRELEASE_TAG=unreleased\n", f"\nRELEASE_TAG={tag}\n")


class InstallScriptTest(unittest.TestCase):
    shell = ("/bin/sh",)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="install-script-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.home / ".local/bin"
        self.installed = self.bin / "devdiary"
        self.setup_log = self.root / "setup.log"
        fakes = self.root / "fakes"
        fakes.mkdir()
        for name, text in (("curl", FAKE_CURL), ("uname", FAKE_UNAME)):
            (fakes / name).write_text(text)
            (fakes / name).chmod(0o755)
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{fakes}:/usr/bin:/bin",
            "SHELL": "/bin/bash",
            "TMPDIR": str(self.root),
            "FIXTURE": str(self.root),
            "FAKE_UNAME_S": "Linux",
            "FAKE_UNAME_M": "x86_64",
            "SETUP_LOG": str(self.setup_log),
        }
        self.publish("v0.4.0")

    def publish(self, tag, tamper=None):
        release = self.root / "releases" / tag
        release.mkdir(parents=True)
        version = tag.removeprefix("v")
        sums = []
        for target in (MAC, LINUX):
            name = f"devdiary-{version}-{target}.tar.gz"
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                data = stub(version, target).encode()
                member = tarfile.TarInfo("devdiary")
                member.size = len(data)
                member.mode = 0o755
                archive.addfile(member, io.BytesIO(data))
            sums.append(f"{hashlib.sha256(buffer.getvalue()).hexdigest()}  {name}\n")
            (release / name).write_bytes(
                buffer.getvalue() + (b"x" if target == tamper else b"")
            )
        (release / "install.sh").write_text(stamped(tag))
        (release / "SHA256SUMS").write_text("".join(sums))

    def run_script(self, *args, script=None):
        """No terminal: a new session without a controlling terminal, as in CI or an agent's shell."""
        return subprocess.run(
            [*self.shell, "-s", "--", *args],
            input=script or stamped("v0.4.0"),
            capture_output=True,
            check=False,
            text=True,
            env=self.env,
            start_new_session=True,
            timeout=30,
        )

    def run_in_terminal(self, *args, answer=None):
        """A controlling terminal for /dev/tty, with the script still on stdin as `curl | sh` gives it.

        Returns (exit status, stdout, stderr, terminal output).
        """
        master, slave = pty.openpty()

        def take_terminal():
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        process = subprocess.Popen(
            [*self.shell, "-s", "--", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            start_new_session=True,
            preexec_fn=take_terminal,  # noqa: PLW1509 - these tests start no threads
        )
        process.stdin.write(stamped("v0.4.0").encode())
        process.stdin.close()
        out, err = process.stdout.fileno(), process.stderr.fileno()
        output = {out: b"", err: b"", master: b""}
        streams = set(output)
        answered = False
        deadline = time.monotonic() + 30
        while streams - {master} and time.monotonic() < deadline:
            ready, _, _ = select.select(list(streams), [], [], 0.5)
            for fd in ready:
                chunk = os.read(fd, 4096)
                if chunk:
                    output[fd] += chunk
                else:
                    streams.discard(fd)
            if not answered and b"[y/N]" in output[master]:
                os.write(master, answer)
                answered = True
        if streams - {master}:
            process.kill()
        process.wait(timeout=10)
        while master in streams and select.select([master], [], [], 0)[0]:
            chunk = os.read(master, 4096)
            output[master] += chunk
            if not chunk:
                break
        os.close(slave)
        os.close(master)
        process.stdout.close()
        process.stderr.close()
        return (
            process.returncode,
            output[out].decode(),
            output[err].decode(),
            output[master].decode(),
        )

    def downloads(self):
        log = self.root / "curl.log"
        return log.read_text().splitlines() if log.exists() else []

    def assert_nothing_installed(self):
        self.assertFalse(self.installed.exists())
        self.assertEqual(list(self.bin.iterdir()) if self.bin.exists() else [], [])

    def test_without_a_terminal_installs_only_and_names_setup(self):
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed.read_text(), stub("0.4.0", LINUX))
        self.assertEqual(self.installed.stat().st_mode & 0o777, 0o755)
        self.assertEqual([p.name for p in self.bin.iterdir()], ["devdiary"])
        self.assertEqual(
            self.downloads(),
            [
                f"{DOWNLOADS}v0.4.0/devdiary-0.4.0-{LINUX}.tar.gz",
                f"{DOWNLOADS}v0.4.0/SHA256SUMS",
            ],
        )
        self.assertIn("devdiary setup", result.stdout)
        self.assertFalse(self.setup_log.exists())
        self.assertFalse((self.home / ".bashrc").exists())

    def test_an_apple_silicon_mac_gets_the_darwin_binary(self):
        self.env.update(FAKE_UNAME_S="Darwin", FAKE_UNAME_M="arm64")

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed.read_text(), stub("0.4.0", MAC))

    def test_any_other_platform_is_named_and_stops_before_downloading(self):
        for system, machine in (
            ("Darwin", "x86_64"),
            ("Linux", "aarch64"),
            ("FreeBSD", "amd64"),
        ):
            with self.subTest(system=system, machine=machine):
                self.env.update(FAKE_UNAME_S=system, FAKE_UNAME_M=machine)

                result = self.run_script()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{system} {machine}", result.stderr)
                self.assertEqual(self.downloads(), [])
                self.assert_nothing_installed()

    def test_a_checksum_mismatch_installs_nothing_and_names_release_and_target(self):
        shutil.rmtree(self.root / "releases")
        self.publish("v0.4.0", tamper=LINUX)
        self.bin.mkdir(parents=True)
        self.installed.write_text("older devdiary\n")

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("v0.4.0", result.stderr)
        self.assertIn(LINUX, result.stderr)
        self.assertEqual(self.installed.read_text(), "older devdiary\n")
        self.assertEqual([p.name for p in self.bin.iterdir()], ["devdiary"])

    def test_version_option_installs_that_release(self):
        self.publish("v0.5.1")

        result = self.run_script("--version", "0.5.1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed.read_text(), stub("0.5.1", LINUX))
        self.assertTrue(
            all(url.startswith(f"{DOWNLOADS}v0.5.1/") for url in self.downloads())
        )

    def test_a_malformed_version_stops_before_downloading(self):
        result = self.run_script("--version", "latest")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("latest", result.stderr)
        self.assertEqual(self.downloads(), [])

    def test_a_release_that_does_not_exist_installs_nothing(self):
        result = self.run_script("--version", "9.9.9")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("v9.9.9", result.stderr)
        self.assert_nothing_installed()

    def test_the_unreleased_script_needs_a_version(self):
        result = self.run_script(script=SCRIPT.read_text())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--version", result.stderr)
        self.assertEqual(self.downloads(), [])

    def test_an_existing_binary_is_replaced_by_rename(self):
        self.bin.mkdir(parents=True)
        self.installed.write_text("older devdiary\n")
        old = self.installed.stat().st_ino

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.installed.read_text(), stub("0.4.0", LINUX))
        self.assertNotEqual(self.installed.stat().st_ino, old)

    def test_with_a_terminal_runs_setup_on_the_terminal_and_exits_with_its_status(self):
        self.env["PATH"] = f"{self.bin}:{self.env['PATH']}"
        self.env["SETUP_STATUS"] = "2"

        status, _, err, terminal = self.run_in_terminal()

        self.assertEqual(status, 2, err)
        self.assertEqual(self.setup_log.read_text(), "args=setup\nstdin=tty\n")
        self.assertNotIn("[y/N]", terminal)

    def test_off_path_yes_adds_the_named_line_to_the_startup_file(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text("alias ll='ls -l'")

        status, _, err, terminal = self.run_in_terminal(answer=b"y\n")

        self.assertEqual(status, 0, err)
        self.assertIn("~/.bashrc", terminal)
        self.assertIn(PATH_LINE, terminal)
        self.assertEqual(bashrc.read_text(), f"alias ll='ls -l'\n{PATH_LINE}\n")
        self.assertEqual(self.setup_log.read_text(), "args=setup\nstdin=tty\n")

    def test_off_path_no_leaves_the_startup_file_alone(self):
        status, _, err, terminal = self.run_in_terminal(answer=b"n\n")

        self.assertEqual(status, 0, err)
        self.assertIn("[y/N]", terminal)
        self.assertFalse((self.home / ".bashrc").exists())
        self.assertEqual(self.setup_log.read_text(), "args=setup\nstdin=tty\n")

    def test_a_startup_file_that_has_the_line_is_not_changed_again(self):
        bashrc = self.home / ".bashrc"
        bashrc.write_text(f"{PATH_LINE}\n")

        status, _, err, terminal = self.run_in_terminal(answer=b"y\n")

        self.assertEqual(status, 0, err)
        self.assertNotIn("[y/N]", terminal)
        self.assertEqual(bashrc.read_text(), f"{PATH_LINE}\n")

    def test_the_startup_file_follows_the_login_shell(self):
        cases = (
            ("Linux", "x86_64", "/usr/bin/zsh", ".zshrc", PATH_LINE),
            ("Darwin", "arm64", "/bin/bash", ".bash_profile", PATH_LINE),
            (
                "Linux",
                "x86_64",
                "/usr/bin/fish",
                ".config/fish/config.fish",
                "fish_add_path $HOME/.local/bin",
            ),
            ("Linux", "x86_64", "/bin/ksh", ".profile", PATH_LINE),
        )
        for system, machine, shell, startup, line in cases:
            with self.subTest(shell=shell, system=system):
                self.env.update(FAKE_UNAME_S=system, FAKE_UNAME_M=machine, SHELL=shell)

                status, _, err, terminal = self.run_in_terminal(answer=b"y\n")

                self.assertEqual(status, 0, err)
                self.assertIn(f"~/{startup}", terminal)
                self.assertEqual((self.home / startup).read_text(), f"{line}\n")


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class BashPosixInstallScriptTest(InstallScriptTest):
    """macOS runs `sh` as bash in POSIX mode; Ubuntu and Debian run it as dash."""

    shell = ("bash", "--posix")


def setUpModule():
    isolation.guard()


if __name__ == "__main__":
    unittest.main()
