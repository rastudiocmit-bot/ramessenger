#!/usr/bin/env python3
"""Read-only preview of a pinned recipient fix inside the existing API image.

Run on the messenger VPS: python3 preview_native_recipient.py --ref FULL_SHA.
Only this child process imports the candidate modules. The running API, image,
packages, history and send queue are unchanged. The fixed GET-only probe rejects
POST and prints redacted metadata; no recipient test message is submitted.
"""
import argparse
import json
import re
import subprocess

REPO = 'rastudiocmit-bot/ramessenger'
FILES = (
    'app/backend/src/center_messenger/alfa_max_browser.py',
    'app/backend/src/center_messenger/history_receipts.py',
    'app/backend/src/center_messenger/alfa_shared_parent.py',
    'app/backend/src/center_messenger/alfa_repair.py',
    'app/backend/src/center_messenger/native_relink.py',
    'app/backend/src/center_messenger/alfa_max.py',
    'app/backend/src/center_messenger/customer_updates.py',
    'scripts/diagnose_max_recipients.py',
)
BOOTSTRAP = '''import json,sys,types
import center_messenger
data=json.load(sys.stdin)
for path in ''' + repr(FILES[:-1]) + ''':
    short=path.rsplit('/',1)[-1][:-3]
    name='center_messenger.'+short
    module=types.ModuleType(name)
    module.__file__='<recipient-preview>/'+path
    module.__package__='center_messenger'
    sys.modules[name]=module
    setattr(center_messenger,short,module)
    exec(compile(data[path],module.__file__,'exec'),module.__dict__)
sys.argv=['recipient-preview']
exec(compile(data['scripts/diagnose_max_recipients.py'],'<recipient-preview>/audit','exec'),
     {'__name__':'__main__'})
'''


def download(ref):
    if not re.fullmatch(r'[a-f0-9]{40}', ref):
        raise ValueError('Use an exact 40-character commit SHA')
    packet = {}
    for path in FILES:
        print('Recipient preview: loading ' + path.rsplit('/', 1)[-1], flush=True)
        result = subprocess.run(['gh', 'api', f'repos/{REPO}/contents/{path}?ref={ref}',
            '-H', 'Accept: application/vnd.github.raw+json'], check=True, text=True,
            stdout=subprocess.PIPE, timeout=60)
        if len(result.stdout.encode()) > 2_000_000:
            raise ValueError('Candidate source exceeds size limit')
        compile(result.stdout, path, 'exec')
        packet[path] = result.stdout
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', required=True)
    args = parser.parse_args(argv)
    packet = download(args.ref)
    print('Recipient preview: GET-only check; no deployment or messages', flush=True)
    subprocess.run(['docker', 'exec', '-i', 'center-messenger-api-1',
                    '/opt/venv/bin/python', '-c', BOOTSTRAP],
                   input=json.dumps(packet), text=True, check=True, timeout=360)


if __name__ == '__main__':
    main()
