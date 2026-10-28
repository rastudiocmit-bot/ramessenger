#!/usr/bin/python3
"""Unprivileged forced SSH entry. Never evaluates SSH_ORIGINAL_COMMAND."""
import os
import re
import subprocess
import sys


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', sys.argv[1]):
        raise SystemExit('invalid actor')
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    if len(command) > 160 or not command:
        raise SystemExit('Use: status | logs | deploy-logs | audit | deploy <main-sha>')
    # Passing a single argv value preserves metacharacters as data. The root
    # dispatcher performs both command and role validation before any operation.
    raise SystemExit(subprocess.run([
        '/usr/bin/sudo', '-n', '/usr/local/sbin/cm-support-control',
        '--actor', sys.argv[1], '--command', command,
    ], env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}).returncode)


if __name__ == '__main__':
    main()
