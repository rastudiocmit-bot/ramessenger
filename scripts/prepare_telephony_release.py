"""Prepare an exact reviewed journal/container release. Never deploy containers here."""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import subprocess

import full_backup_split as backup
import max_release_guard as guard
import max_runtime_deploy as runtime


def verified_backup(directory,expected_api,expected_max,clock=lambda:datetime.now(timezone.utc)):
    manifest=backup.verify_integrity(directory)
    finished=datetime.fromisoformat(manifest['finished_at'])
    if finished.tzinfo is None or not timedelta(0)<=clock()-finished<=timedelta(hours=24):
        raise RuntimeError('A verified full backup from the last 24 hours is required')
    # The full backup must preserve the actual processes being replaced, not
    # merely an older healthy release from the same day.
    metadata=json.loads((directory/'recovery-metadata.json').read_text())
    images={item['Name'].lstrip('/'):item['Config']['Image'] for item in metadata.get('containers',[])}
    if images.get('center-messenger-api-1')!=expected_api or images.get('center-messenger-max-worker-1')!=expected_max:
        raise RuntimeError('Full backup does not contain the current API/MAX images; make a fresh full backup')
    return manifest


def prepare(source,sha,expected_api,expected_max,expected_live,expected_policy,previous_policy,directory):
    if runtime.run('git','-C',str(source),'rev-parse','HEAD')!=sha:raise RuntimeError('Exact reviewed checkout required')
    if runtime.run('git','-C',str(source),'status','--porcelain'):raise RuntimeError('Reviewed checkout must be clean')
    if guard.main_sha()!=sha:raise RuntimeError('The reviewed release must still be current main')
    if expected_api!='center-messenger:local-'+expected_live[:12]:raise RuntimeError('Live API SHA/image mismatch')
    verified_backup(directory,expected_api,expected_max)
    # Use the same project lock and the existing owner. Installing the guard
    # is explicit, checksum-pinned and independent of a candidate's deployment.
    with guard.project_lock():
        guard.install(source,expected_policy,expected_live,previous_policy,lock_held=True)
        runtime.prepare_update(guard.ROOT,source,sha,expected_api,expected_max,expected_policy,
                               schema_from='0030_novofon_settings',install_telephony=True)
        destination=guard.ROOT/'support/full-backup-split.py'
        runtime.write(destination,(source/'scripts/full_backup_split.py').read_bytes(),mode=0o700)
    print('TELEPHONY RELEASE PREPARED; run systemctl start --no-block ramessenger-local-build.service',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','sha','expected-api','expected-max','expected-live','expected-policy','previous-policy','backup'):
        parser.add_argument('--'+name,required=True,type=Path if name in ('source','backup') else str)
    options=parser.parse_args()
    prepare(options.source.resolve(),options.sha,options.expected_api,options.expected_max,options.expected_live,
            options.expected_policy,options.previous_policy,options.backup.resolve())
