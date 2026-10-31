"""Admit a reviewed API/telephony update; the existing deploy owner promotes it."""
import argparse
from pathlib import Path
import json

import max_release_guard as guard
import max_runtime_deploy as runtime
import telephony_runtime as phone


def prepare(source,sha,api,max_image,telephony,live,policy,previous):
    if (runtime.run('git','-C',str(source),'rev-parse','HEAD')!=sha or
            runtime.run('git','-C',str(source),'status','--porcelain')):
        raise RuntimeError('A clean exact reviewed checkout is required')
    if guard.main_sha()!=sha or api!='center-messenger:local-'+guard.full_sha(live)[:12]:
        raise RuntimeError('Reviewed main and live API SHA/image are required')
    with guard.project_lock():
        guard.install(source,policy,live,previous,lock_held=True)
        phone.prepare_update(guard.ROOT,source,sha,api,telephony,max_image,policy,runtime)
        path=guard.ROOT/'local-build-state.json'
        if path.exists():
            value=json.loads(path.read_text())
            if value.get('last_failed_sha')==sha:
                value.pop('last_failed_sha')
                runtime.write(path,(json.dumps(value,indent=2)+'\n').encode())
    print('TELEPHONY UPDATE ADMITTED; start ramessenger-local-build.service',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','sha','expected-api','expected-max','expected-telephony','expected-live','expected-policy','previous-policy'):
        parser.add_argument('--'+name,required=True,type=Path if name=='source' else str)
    opts=parser.parse_args()
    prepare(opts.source.resolve(),opts.sha,opts.expected_api,opts.expected_max,opts.expected_telephony,
            opts.expected_live,opts.expected_policy,opts.previous_policy)
