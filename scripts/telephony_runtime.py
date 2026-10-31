"""Third-container admission/recovery helpers used only by the pinned deploy owner."""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

NAME='center-messenger-telephony-worker-1'
STATE='telephony-runtime.json'


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
    state=json.loads(path.read_text())
    info=runtime.inspect(NAME)
    values=[line.split('=',1)[1].strip() for line in (root/'.env').read_text().splitlines() if line.startswith('CM_TELEPHONY_IMAGE=')]
    if (state.get('phase')!='complete' or info['Image']!=state['worker_id']
        or info['Config']['Image']!=state['worker_image'] or values!=[state['worker_image']]
        or state.get('contract')!=policy['telephony_runtime_contract']
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
