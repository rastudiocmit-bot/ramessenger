"""Third-container admission/recovery helpers used only by the pinned deploy owner."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

NAME='center-messenger-telephony-worker-1'
STATE='telephony-runtime.json'
UPDATE='telephony-runtime-update.json'


def _save(root,plan,runtime):
    runtime.write(root/UPDATE,(json.dumps(plan,indent=2)+'\n').encode())


def _max_identity(runtime):
    info=runtime.inspect('center-messenger-max-worker-1')
    return {k:info[k] for k in ('Id','Image')} | {'image':info['Config']['Image']}


def _check_max(plan,runtime):
    if _max_identity(runtime)!=plan['max_identity']:
        raise RuntimeError('Pinned MAX changed during telephony update; manual review required')


def _pin(root,runtime):
    state=json.loads((root/STATE).read_text());info=runtime.inspect(NAME)
    pins=[line.split('=',1)[1].strip() for line in (root/'.env').read_text().splitlines() if line.startswith('CM_TELEPHONY_IMAGE=')]
    if (state.get('phase')!='complete' or pins!=[state['worker_image']]
            or info['Config']['Image']!=state['worker_image'] or info['Image']!=state['worker_id']):
        raise RuntimeError('Existing telephony pin changed')
    return state,info


def prepared(root,source,sha,runtime):
    path=root/UPDATE
    if not path.exists():return None
    plan=json.loads(path.read_text())
    if plan.get('phase')!='prepared':return None
    if (plan.get('version')!=1 or plan.get('sha')!=sha
            or runtime.digest(source/'deploy/max-protection.json')!=plan['policy']):
        raise RuntimeError('Exact reviewed telephony update source required')
    if (runtime.digest(root/'compose.yaml')!=plan['compose'] or
            runtime.digest(source/'compose.yaml')!=plan['compose'] or
            runtime.digest(root/'.env')!=plan['environment'] or
            runtime.digest(root/STATE)!=plan['runtime_state']):
        raise RuntimeError('Runtime changed after telephony update preparation')
    state,info=_pin(root,runtime)
    if (state['worker_image']!=plan['old_phone'] or info['Id']!=plan['old_container']
            or info['Image']!=plan['old_phone_id']):
        raise RuntimeError('Telephony process changed after preparation')
    _check_max(plan,runtime)
    policy=json.loads((source/'deploy/max-protection.json').read_text())
    if runtime.schema_revision()!=plan['schema_revision'] or policy['schema_revision']!=plan['schema_revision']:
        raise RuntimeError('Telephony-only update cannot change the database schema')
    return plan


def prepare_update(root,source,sha,api,phone,max_image,policy_hash,runtime):
    if not re.fullmatch('[a-f0-9]{40}',sha) or runtime.run('git','-C',str(source),'rev-parse','HEAD')!=sha:
        raise RuntimeError('Exact telephony source SHA required')
    policy=json.loads((source/'deploy/max-protection.json').read_text())
    if (runtime.digest(source/'deploy/max-protection.json')!=policy_hash or
            runtime.digest(source/'compose.yaml')!=runtime.digest(root/'compose.yaml')):
        raise RuntimeError('Telephony update requires the reviewed unchanged Compose')
    previous=root/UPDATE
    if previous.exists() and json.loads(previous.read_text()).get('phase') not in ('prepared','failed','complete'):
        raise RuntimeError('Interrupted telephony update must be recovered first')
    state,worker=_pin(root,runtime);current=runtime.inspect('center-messenger-api-1')
    if (current['Config']['Image']!=api or worker['Config']['Image']!=phone or
            _max_identity(runtime)['image']!=max_image or
            current['State']['Status']!='running' or worker['State']['Status']!='running' or
            'CM_RUNTIME_ROLE=api' not in current['Config'].get('Env',[]) or
            'CM_RUNTIME_ROLE=telephony' not in worker['Config'].get('Env',[])):
        raise RuntimeError('Unexpected active API/telephony/MAX processes')
    revision=runtime.schema_revision()
    if revision!=state['schema_revision'] or revision!=policy['schema_revision']:
        raise RuntimeError('Telephony-only update requires the existing database schema')
    plan={'version':1,'phase':'prepared','sha':sha,'policy':policy_hash,'old_api':api,
          'old_phone':phone,'old_phone_id':worker['Image'],'old_container':worker['Id'],
          'max_identity':_max_identity(runtime),'schema_revision':revision,
          'compose':runtime.digest(root/'compose.yaml'),'environment':runtime.digest(root/'.env'),
          'runtime_state':runtime.digest(root/STATE)}
    _save(root,plan,runtime)
    print('TELEPHONY UPDATE PREPARED; MAX PIN UNCHANGED',flush=True)


def promote_update(root,source,sha,old_api,new_api,backup,promoter,runtime):
    plan=prepared(root,source,sha,runtime)
    if plan is None:return False
    if old_api!=plan['old_api'] or runtime.inspect('center-messenger-api-1')['Config']['Image']!=old_api:
        raise RuntimeError('API changed after telephony preparation')
    if not Path(backup).is_file() or Path(backup).stat().st_size<1024:
        raise RuntimeError('Verified database backup required before telephony update')
    image_id=json.loads(runtime.run('docker','image','inspect',new_api))[0]['Id']
    phone=tag(runtime,image_id,sha)
    archive=Path(tempfile.mkdtemp(prefix='telephony-update-'+sha[:12]+'-',dir=root/'rollback'))
    runtime.write(archive/'environment',(root/'.env').read_bytes())
    runtime.write(archive/'runtime-state.json',(root/STATE).read_bytes())
    new_env=environment((root/'.env').read_bytes(),phone)
    policy=json.loads((source/'deploy/max-protection.json').read_text())
    next_state={'phase':'complete','sha':sha,'worker_image':phone,'worker_id':image_id,
                'contract':policy['telephony_runtime_contract'],'schema_revision':policy['schema_revision']}
    next_state_bytes=(json.dumps(next_state,indent=2)+'\n').encode()
    plan.update(phase='promoting',archive=str(archive),new_api=new_api,new_phone=phone,new_phone_id=image_id,
                new_environment=hashlib.sha256(new_env).hexdigest(),new_state=hashlib.sha256(next_state_bytes).hexdigest())
    _save(root,plan,runtime)
    try:
        runtime.write(root/'.env',new_env)
        env=dict(os.environ,CM_IMAGE=old_api,CM_TELEPHONY_IMAGE=phone)
        runtime.run('docker','compose','-p','center-messenger','-f',str(root/'compose.yaml'),
                    'up','-d','--no-deps','--no-build','--force-recreate','telephony-worker',env=env,timeout=300)
        if not wait(runtime,phone):raise RuntimeError('New telephony worker is not ready')
        _check_max(plan,runtime)
        runtime.write(root/STATE,next_state_bytes)
        promoter.deploy_api_with_rollback(old_api,new_api,backup)
        _check_max(plan,runtime)
        plan['phase']='complete';_save(root,plan,runtime)
        print('TELEPHONY WORKER UPDATE COMPLETE; MAX UNCHANGED',phone,flush=True)
        return True
    except BaseException:
        recover_update(root,promoter,runtime)
        raise


def recover_update(root,promoter,runtime):
    path=root/UPDATE
    if not path.exists():return
    plan=json.loads(path.read_text())
    if plan.get('phase')!='promoting':return
    _check_max(plan,runtime)
    archive=Path(plan['archive'])
    if archive.parent!=root/'rollback' or archive.is_symlink() or not archive.name.startswith('telephony-update-'+plan['sha'][:12]+'-'):
        raise RuntimeError('Unexpected telephony recovery directory')
    if (runtime.digest(root/'compose.yaml')!=plan['compose'] or
            runtime.digest(root/'.env') not in (plan['environment'],plan['new_environment']) or
            runtime.digest(archive/'environment')!=plan['environment'] or
            runtime.digest(archive/'runtime-state.json')!=plan['runtime_state'] or
            runtime.digest(root/STATE) not in (plan['runtime_state'],plan['new_state'])):
        raise RuntimeError('Telephony recovery inputs changed; manual review required')
    present=runtime.run('docker','ps','-a','--filter','name=^/'+NAME+'$','--format','{{.Names}}').splitlines()
    if present not in ([],[NAME]):raise RuntimeError('Unexpected telephony recovery process')
    if present:
        current=runtime.inspect(NAME)
        if (current['Image'] not in (plan['old_phone_id'],plan['new_phone_id']) or
                current['Config']['Image'] not in (plan['old_phone'],plan['new_phone'])):
            raise RuntimeError('Unrelated telephony process; refusing recovery')
    api=runtime.inspect('center-messenger-api-1')['Config']['Image']
    if api not in (plan['old_api'],plan['new_api']):
        raise RuntimeError('Unrelated API release; refusing recovery')
    runtime.write(root/'.env',(archive/'environment').read_bytes())
    runtime.write(root/STATE,(archive/'runtime-state.json').read_bytes())
    env=dict(os.environ,CM_IMAGE=plan['old_api'],CM_TELEPHONY_IMAGE=plan['old_phone'])
    command=('docker','compose','-p','center-messenger','-f',str(root/'compose.yaml'),
             'up','-d','--no-deps','--no-build','--force-recreate')
    runtime.run(*command,'telephony-worker',env=env,timeout=300)
    if not wait(runtime,plan['old_phone']):raise RuntimeError('Previous telephony worker is not ready')
    if api!=plan['old_api']:runtime.run(*command,'api',env=env,timeout=300)
    if not promoter.wait_ready(plan['old_api']):raise RuntimeError('Previous API is not ready')
    _check_max(plan,runtime)
    plan['phase']='failed';_save(root,plan,runtime)
    print('TELEPHONY UPDATE ROLLED BACK; MAX AND DATABASE PRESERVED',flush=True)


def prepare(root,source,policy,runtime):
    review=policy.get('telephony_install',{})
    if review.get('from_compose')!=runtime.digest(root/'compose.yaml'):
        raise RuntimeError('Exact reviewed source Compose required for telephony installation')
    if runtime.digest(source/'compose.yaml')!=policy['protected'].get('compose.yaml'):
        raise RuntimeError('Reviewed telephony Compose mismatch')
    if (root/STATE).exists():raise RuntimeError('Telephony is already installed; separate update required')
    present=runtime.run('docker','ps','-a','--format','{{.Names}}').splitlines()
    if NAME in present:raise RuntimeError('Unexpected existing telephony container')
    if any(line.startswith('CM_TELEPHONY_IMAGE=') for line in (root/'.env').read_text().splitlines()):
        raise RuntimeError('Unexpected existing telephony pin')
    return {'telephony_install':True,'new_compose':runtime.digest(source/'compose.yaml')}


def install_key(root,runtime):
    key=root/'.secrets/telephony_worker_key'
    key.parent.mkdir(mode=0o700,exist_ok=True)
    if not key.exists():runtime.write(key,(secrets.token_hex(32)+'\n').encode(),mode=0o400)
    if key.is_symlink() or not re.fullmatch('[a-f0-9]{64}',key.read_text().strip()):
        raise RuntimeError('Unexpected telephony control key')
    os.chown(key,10001,10001);key.chmod(0o400)


def environment(body,image):
    lines=[line for line in body.decode().splitlines() if not line.strip().startswith('CM_TELEPHONY_IMAGE=')]
    return ('\n'.join(lines)+'\nCM_TELEPHONY_IMAGE='+image+'\n').encode()


def tag(runtime,image_id,sha):
    image='center-messenger:telephony-'+sha[:12]
    existing=subprocess.run(['docker','image','inspect',image],capture_output=True,text=True,timeout=30)
    if existing.returncode==0 and json.loads(existing.stdout)[0]['Id']!=image_id:
        raise RuntimeError('Refusing to overwrite a pinned telephony image')
    runtime.run('docker','tag',image_id,image)
    return image


def wait(runtime,image,timeout=90):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        info=runtime.inspect(NAME)
        if info['Config']['Image']!=image or info['State']['Status']!='running':return False
        if info['State'].get('Health',{}).get('Status')=='healthy':return True
        time.sleep(2)
    return False


def verify(root,source,runtime,pending=None):
    if not (source/'deploy/max-protection.json').exists():return
    policy=json.loads((source/'deploy/max-protection.json').read_text())
    if not policy.get('telephony_runtime_contract'):return
    if pending and pending.get('telephony_install'):return
    path=root/STATE
    if not path.exists():raise RuntimeError('Reviewed telephony installation must be explicitly prepared')
    update=None
    if (root/UPDATE).exists():
        update=prepared(root,source,runtime.run('git','-C',str(source),'rev-parse','HEAD'),runtime)
    state=json.loads(path.read_text())
    info=runtime.inspect(NAME)
    values=[line.split('=',1)[1].strip() for line in (root/'.env').read_text().splitlines() if line.startswith('CM_TELEPHONY_IMAGE=')]
    if (state.get('phase')!='complete' or info['Image']!=state['worker_id']
        or info['Config']['Image']!=state['worker_image'] or values!=[state['worker_image']]
        or (state.get('contract')!=policy['telephony_runtime_contract'] and update is None)
        or state.get('schema_revision')!=policy['schema_revision']):
        raise RuntimeError('Pinned telephony worker requires a reviewed update; ordinary API release blocked')


def complete(root,source,sha,image,image_id,runtime):
    policy=json.loads((source/'deploy/max-protection.json').read_text())
    state={'phase':'complete','sha':sha,'worker_image':image,'worker_id':image_id,
           'contract':policy['telephony_runtime_contract'],'schema_revision':policy['schema_revision']}
    runtime.write(root/STATE,(json.dumps(state,indent=2)+'\n').encode())


def remove_new(root,plan,runtime):
    present=runtime.run('docker','ps','-a','--format','{{.Names}}').splitlines()
    if NAME in present:
        info=runtime.inspect(NAME)
        if (info['Image']!=plan['worker_id'] or info['Config']['Image']!=plan['telephony_image']
            or (info['Config'].get('Labels') or {}).get('com.docker.compose.project')!='center-messenger'):
            raise RuntimeError('Telephony identity changed during rollback; manual review required')
        runtime.run('docker','rm','-f',info['Id'])
    path=root/STATE
    if path.exists():
        state=json.loads(path.read_text())
        if state.get('sha')!=plan['sha'] or state.get('worker_id')!=plan['worker_id']:
            raise RuntimeError('Telephony runtime metadata changed during rollback')
        path.unlink()
