"""Read-only vendor capability probes, not hook trust or runtime qualification.

Only --version/--help are executed, in a disposable HOME/cwd with no credentials
or inherited vendor configuration. Never inspect auth files, sessions or trust DBs.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time

LIMIT = 65536
TIMEOUT = 3


def classify(vendor, version_output, help_output):
    pattern = r'codex-cli (\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)' if vendor == 'codex' else r'(\d+\.\d+\.\d+) \(Claude Code\)'
    match = re.fullmatch(pattern, version_output.strip())
    version = match.group(1) if match else None
    # This flag is only an interface signal. We NEVER pass it to a process.
    detected = '--dangerously-bypass-hook-trust' in help_output if vendor == 'codex' else all(flag in help_output for flag in ('--settings', '--setting-sources'))
    return {'vendor': vendor, 'runtime': 'codex' if vendor == 'codex' else 'claude-code',
            'version': version, 'support': ('unknown_version' if not version else
                'documented_interface_detected_runtime_unqualified' if detected else 'unsupported_interface'),
            'runtime_qualified': False, 'trust': 'not_inspected',
            'feature_policy': 'not_inspected', 'registration': 'not_inspected',
            'evidence': 'isolated_version_and_help_only',
            'minimum_supported_version': None}


def probe(executable, argument, home):
    env = {'HOME': home, 'CODEX_HOME': home, 'CLAUDE_CONFIG_DIR': home,
           'XDG_CONFIG_HOME': home, 'XDG_CACHE_HOME': home, 'XDG_DATA_HOME': home,
           'PATH': os.environ.get('PATH', os.defpath), 'LANG': 'C.UTF-8',
           'NO_COLOR': '1', 'DISABLE_AUTOUPDATER': '1'}
    process = subprocess.Popen([executable, argument], cwd=home, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ValueError('probe_timeout')
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > LIMIT:
                    raise ValueError('probe_overflow')
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise ValueError('probe_failed')
        return output.decode('utf-8', errors='replace')
    finally:
        # Include descendants holding pipes, even if the direct child has exited.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if process.stdout is not None:
            process.stdout.close()


def discover(vendor=None):
    from devdiary.observer import require_platform, trusted_executable, vendor_name
    require_platform()
    vendors = [vendor_name(vendor)] if vendor is not None else ['claude', 'codex']
    results = []
    for name in vendors:
        result = classify(name, '', '')
        executable = shutil.which(name)
        result.update(executable=executable, support='not_installed', evidence='not_probed')
        if executable:
            try:
                resolved = trusted_executable(Path(executable))
                result['resolved_executable'] = resolved
                # A package-manager shim can install/update even for --version.
                # Do not run scripts. Only probe an already-installed native binary.
                with open(resolved, 'rb') as binary:
                    magic = binary.read(4)
                if magic not in (b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                                 b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
                    result['support'] = 'script_wrapper_not_probed'
                    results.append(result)
                    continue
                with tempfile.TemporaryDirectory(prefix='devdiary-discovery-') as home:
                    version = probe(resolved, '--version', home)
                    result.update(classify(name, version, ''))
                    result['evidence'] = 'isolated_version_only'
                    help_output = probe(resolved, '--help', home)
                result.update(classify(name, version, help_output))
            except (OSError, ValueError, subprocess.SubprocessError):
                result['support'] = 'probe_unavailable_or_unsafe'
        results.append(result)
    return results
