"""Admit the reviewed additive AI schema and all three process pins, no deployment."""
import argparse
from pathlib import Path
import shutil

import max_release_guard as guard
import max_runtime_deploy as runtime


def prepare(source,sha,live,api,max_image,phone,policy,previous):
    if (runtime.run('git','-C',str(source),'rev-parse','HEAD')!=sha
            or runtime.run('git','-C',str(source),'status','--porcelain')
            or guard.main_sha()!=sha
            or api!='center-messenger:local-'+guard.full_sha(live)[:12]):
        raise RuntimeError('Clean exact current main and expected live API required')
    with guard.project_lock():
        for name,image in [('center-messenger-api-1',api),('center-messenger-max-worker-1',max_image),
                           ('center-messenger-telephony-worker-1',phone)]:
            info=runtime.inspect(name)
            if info['Config']['Image']!=image or info['State'].get('Health',{}).get('Status')!='healthy':
                raise RuntimeError('Expected healthy API/MAX/telephony required before schema preparation')
        if shutil.disk_usage(guard.ROOT).free<4*1024**3:
            raise RuntimeError('The FFmpeg image build requires at least 4 GiB free; no copies were deleted')
        guard.install(source,policy,live,previous,lock_held=True)
        runtime.prepare_update(guard.ROOT,source,sha,api,max_image,policy,schema_from='0031_telephony_calls',
                               update_telephony=True,expected_telephony=phone)
    print('TELEPHONY AI SCHEMA/PINS PREPARED; existing local-build owner required; no AI work enabled',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','sha','expected-live','expected-api','expected-max','expected-telephony','expected-policy','previous-policy'):
        parser.add_argument('--'+name,required=True,type=Path if name=='source' else str)
    opts=parser.parse_args()
    prepare(opts.source.resolve(),opts.sha,opts.expected_live,opts.expected_api,opts.expected_max,
            opts.expected_telephony,opts.expected_policy,opts.previous_policy)
