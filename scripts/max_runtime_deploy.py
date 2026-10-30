"""Reviewed MAX split and worker updates under the existing deploy owner's lock.

Never deploy from this module's CLI. `prepare` only records the exact transition
and creates a control secret. The pinned local_build_vps runner promotes it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

NAME = 'center-messenger-max-worker-1'
PLAN = 'max-runtime-transition.json'
UPDATE = 'max-runtime-update.json'


def run(*args, env=None, timeout=120):
    return subprocess.run(args, env=env, check=True, stdout=subprocess.PIPE, text=True, timeout=timeout).stdout.strip()


def digest(path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('Expected regular runtime configuration file')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value, mode=0o600):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        pending = Path(handle.name)
        try:
            os.fchmod(handle.fileno(), mode)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def save(root, plan):
    write(root / PLAN, (json.dumps(plan, indent=2) + '\n').encode())


def save_update(root, plan):
    write(root / UPDATE, (json.dumps(plan, indent=2) + '\n').encode())


def worker_contract(source):
    path = source / 'deploy/max-protection.json'
    return json.loads(path.read_text()).get('worker_runtime_contract') if path.exists() else None


def verify_pin(root):
    state = json.loads((root / PLAN).read_text())
    if state.get('phase') != 'complete':
        raise RuntimeError('MAX runtime transition requires explicit recovery/preparation')
    worker = inspect(NAME)
    if worker['Image'] != state['worker_id'] or worker['Config']['Image'] != state['worker_image']:
        raise RuntimeError('Pinned MAX worker identity changed outside its update procedure')
    values = [line.split('=', 1)[1].strip() for line in (root / '.env').read_text().splitlines()
              if line.startswith('CM_MAX_IMAGE=')]
    if values != [state['worker_image']]:
        raise RuntimeError('Pinned MAX image setting changed outside its update procedure')
    return state, worker


def update_for(root, source, sha):
    path = root / UPDATE
    if not path.exists():
        return None
    plan = json.loads(path.read_text())
    if plan.get('version') != 1:
        raise RuntimeError('Invalid MAX worker update plan')
    if plan['phase'] == 'complete':
        return None
    if plan['phase'] not in {'prepared', 'migrating'}:
        raise RuntimeError('MAX worker update requires explicit recovery/preparation')
    if plan['sha'] != sha or plan['compose'] != digest(source / 'compose.yaml'):
        raise RuntimeError('MAX worker update is for a different source SHA/Compose')
    if (plan['compose'] != digest(root / 'compose.yaml') or plan['environment'] != digest(root / '.env')
            or plan['runtime_state'] != digest(root / PLAN)):
        raise RuntimeError('MAX runtime configuration changed after update preparation')
    _, worker = verify_pin(root)
    if worker['Id'] != plan['old_container'] or worker['Image'] != plan['old_worker_id']:
        raise RuntimeError('MAX worker changed after update preparation')
    return plan


def plan_for(root, source, sha):
    path = root / PLAN
    if not path.exists():
        return None
    plan = json.loads(path.read_text())
    if plan.get('version') != 1:
        raise RuntimeError('Invalid MAX runtime transition')
    if plan['phase'] == 'complete':
        return None
    if plan['phase'] != 'prepared':
        raise RuntimeError('MAX transition requires recovery before another release')
    if plan['sha'] != sha or plan['new_compose'] != digest(source / 'compose.yaml'):
        raise RuntimeError('MAX transition is for a different source SHA/Compose')
    if plan['old_compose'] != digest(root / 'compose.yaml'):
        raise RuntimeError('Live Compose changed after MAX transition was prepared')
    return plan


def validate_transition(root, source):
    sha = run('git', '-C', str(source), 'rev-parse', 'HEAD')
    if plan_for(root, source, sha) is None:
        raise RuntimeError('Compose changed: explicit MAX runtime transition required')


def verify_runtime_layout(root, source):
    """A copied Compose is not proof that a safe split ever happened."""
    path = root / PLAN
    if not path.exists():
        raise RuntimeError('MAX runtime transition must be explicitly prepared')
    state = json.loads(path.read_text())
    if state.get('phase') == 'prepared':
        plan_for(root, source, run('git', '-C', str(source), 'rev-parse', 'HEAD'))
        return
    verify_pin(root)
    pending = update_for(root, source, run('git', '-C', str(source), 'rev-parse', 'HEAD')) if (root / UPDATE).exists() else None
    contract = worker_contract(source)
    if contract and state.get('worker_runtime_contract') != contract and pending is None:
        raise RuntimeError('Reviewed MAX worker update must be explicitly prepared before this API release')


def with_image(body, image):
    lines = body.decode().splitlines()
    values = [line for line in lines if not line.strip().startswith('CM_MAX_IMAGE=')]
    return ('\n'.join(values) + '\nCM_MAX_IMAGE=' + image + '\n').encode()


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def wait_worker(image, timeout=90):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        info = inspect(NAME)
        if info['Config']['Image'] != image or info['State']['Status'] != 'running':
            return False
        if info['State'].get('Health', {}).get('Status') == 'healthy':
            return True
        time.sleep(2)
    return False


def promote(root, source, sha, old_image, new_image, backup, promoter):
    plan = plan_for(root, source, sha)
    if plan is None:
        return promote_update(root, source, sha, old_image, new_image, backup, promoter)
    if old_image != plan['old_api'] or inspect('center-messenger-api-1')['Config']['Image'] != old_image:
        raise RuntimeError('API changed after MAX split preparation')
    if not Path(backup).is_file() or Path(backup).stat().st_size < 1024:
        raise RuntimeError('Verified database backup required before MAX split')
    # The new worker reuses the already tested image by ID. There is no second
    # build or mutable "latest" image and no source-code bind mount.
    image_id = json.loads(run('docker', 'image', 'inspect', new_image))[0]['Id']
    max_image = 'center-messenger:max-' + sha[:12]
    existing = subprocess.run(['docker', 'image', 'inspect', max_image], capture_output=True, text=True, timeout=30)
    if existing.returncode == 0 and json.loads(existing.stdout)[0]['Id'] != image_id:
        raise RuntimeError('Refusing to overwrite a different pinned MAX image')
    run('docker', 'tag', image_id, max_image)
    archive = Path(tempfile.mkdtemp(prefix='max-split-' + sha[:12] + '-', dir=root / 'rollback'))
    old_env = (root / '.env').read_bytes() if (root / '.env').exists() else b''
    old_compose = (root / 'compose.yaml').read_bytes()
    write(archive / 'compose.yaml', old_compose)
    write(archive / 'environment', old_env)
    next_env = with_image(old_env, max_image)
    plan.update(phase='promoting', backup_dir=str(archive), worker_image=max_image,
                worker_id=image_id, new_api=new_image, env_existed=(root / '.env').exists())
    plan.update(old_env_hash=hashlib.sha256(old_env).hexdigest(), new_env_hash=hashlib.sha256(next_env).hexdigest())
    save(root, plan)  # recovery intent before changing live files
    compose = ['docker', 'compose', '-p', 'center-messenger', '-f', str(root / 'compose.yaml')]
    try:
        write(root / '.env', next_env)
        write(root / 'compose.yaml', (source / 'compose.yaml').read_bytes(), mode=0o644)
        env = dict(os.environ, CM_IMAGE=new_image, CM_MAX_IMAGE=max_image)
        run(*compose, 'config', '--quiet', env=env)
        run(*compose, 'up', '-d', '--no-deps', '--no-build', 'max-worker', env=env)
        if not wait_worker(max_image):
            raise RuntimeError('Dedicated MAX worker did not become ready')
        # Short overlap with the old process is serialized by the existing DB
        # leases and durable before_post intent. Never clear these leases.
        promoter.deploy_api_with_rollback(old_image, new_image, backup)
        if not wait_worker(max_image):
            raise RuntimeError('MAX worker stopped during API promotion')
        plan.update(phase='complete', worker_runtime_contract=worker_contract(source))
        save(root, plan)
        print('MAX RUNTIME SPLIT COMPLETE', max_image, flush=True)
        return True
    except Exception:
        recover(root, promoter)
        raise


def recover(root, promoter):
    recover_update(root, promoter)
    path = root / PLAN
    if not path.exists():
        return
    plan = json.loads(path.read_text())
    if plan.get('phase') != 'promoting':
        return
    archive = Path(plan['backup_dir'])
    if (archive.parent != root / 'rollback' or archive.is_symlink()
        or not archive.name.startswith('max-split-' + plan['sha'][:12] + '-')):
        raise RuntimeError('Unexpected MAX transition recovery directory')
    current_env = (root / '.env').read_bytes() if (root / '.env').exists() else b''
    if digest(root / 'compose.yaml') not in {plan['old_compose'], plan['new_compose']}:
        raise RuntimeError('Compose was changed outside the MAX transition; manual review required')
    if hashlib.sha256(current_env).hexdigest() not in {plan['old_env_hash'], plan['new_env_hash']}:
        raise RuntimeError('Environment was changed outside the MAX transition; manual review required')
    if digest(archive / 'compose.yaml') != plan['old_compose']:
        raise RuntimeError('Original Compose recovery checksum mismatch')
    if digest(archive / 'environment') != plan['old_env_hash']:
        raise RuntimeError('Original environment recovery checksum mismatch')
    # Restore application configuration, never a DB dump: messages written
    # during the transition must survive rollback.
    write(root / 'compose.yaml', (archive / 'compose.yaml').read_bytes(), mode=0o644)
    if plan['env_existed']:
        write(root / '.env', (archive / 'environment').read_bytes())
    else:
        (root / '.env').unlink(missing_ok=True)
    env = dict(os.environ, CM_IMAGE=plan['old_api'])
    run('docker', 'compose', '-p', 'center-messenger', '-f', str(root / 'compose.yaml'),
        'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'api', env=env, timeout=300)
    if not promoter.wait_ready(plan['old_api']):
        raise RuntimeError('Previous API not ready after MAX split rollback')
    result = subprocess.run(['docker', 'inspect', NAME], capture_output=True, text=True, timeout=30)
    if result.returncode == 0:
        info = json.loads(result.stdout)[0]
        labels = info['Config'].get('Labels') or {}
        if info['Image'] != plan['worker_id'] or labels.get('com.docker.compose.project') != 'center-messenger':
            raise RuntimeError('Unexpected worker identity during rollback')
        run('docker', 'rm', '-f', NAME)
    plan['phase'] = 'failed'
    save(root, plan)
    print('MAX RUNTIME SPLIT ROLLED BACK; DATABASE PRESERVED', flush=True)


def prepare_update(root, source, sha, expected_api, expected_max, expected_policy, schema_from=None):
    if not re.fullmatch('[a-f0-9]{40}', sha) or run('git', '-C', str(source), 'rev-parse', 'HEAD') != sha:
        raise RuntimeError('Exact reviewed source SHA required')
    if digest(source / 'deploy/max-protection.json') != expected_policy:
        raise RuntimeError('Reviewed policy mismatch')
    if digest(source / 'compose.yaml') != digest(root / 'compose.yaml'):
        raise RuntimeError('Worker update cannot change Compose or the database schema')
    state, worker = verify_pin(root)
    api = inspect('center-messenger-api-1')
    if (api['Config']['Image'] != expected_api or worker['Config']['Image'] != expected_max
            or api['State']['Status'] != 'running' or worker['State']['Status'] != 'running'):
        raise RuntimeError('Unexpected active API/MAX images or state')
    if ('CM_RUNTIME_ROLE=api' not in (api['Config'].get('Env') or [])
            or 'CM_RUNTIME_ROLE=max' not in (worker['Config'].get('Env') or [])):
        raise RuntimeError('Worker update requires already separated API/MAX roles')
    old = root / UPDATE
    if old.exists() and json.loads(old.read_text()).get('phase') not in {'prepared', 'complete', 'failed'}:
        raise RuntimeError('Previous worker update requires recovery first')
    policy = json.loads((source / 'deploy/max-protection.json').read_text())
    revision = run('docker', 'exec', 'center-messenger-db-1', 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
                   '-U', 'cm_owner', '-d', 'center_messenger', '-c', 'SELECT version_num FROM alembic_version;')
    transition = None
    if schema_from is not None:
        transition = policy.get('paired_schema_update')
        if (not transition or schema_from != revision or transition.get('from') != revision
                or transition.get('to') != policy['schema_revision']
                or transition.get('rollback_preserves_data') is not True
                or digest(source / transition['migration']) != policy['protected'].get(transition['migration'])):
            raise RuntimeError('Exact reviewed paired schema update required')
    elif revision != policy['schema_revision']:
        raise RuntimeError('Worker-only update requires the same database schema')
    plan = {'version':1, 'phase':'prepared', 'sha':sha, 'old_api':expected_api,
        'old_worker':expected_max, 'old_worker_id':worker['Image'], 'old_container':worker['Id'],
        'compose':digest(root / 'compose.yaml'), 'environment':digest(root / '.env'),
        'runtime_state':digest(root / PLAN), 'schema_revision':revision}
    if transition:
        plan['schema_transition'] = transition
    save_update(root, plan)
    state_path = root / 'local-build-state.json'
    if state_path.exists():
        status = json.loads(state_path.read_text())
        if status.get('last_failed_sha') == sha:
            status.pop('last_failed_sha')
            write(state_path, (json.dumps(status, indent=2) + '\n').encode())
    print('MAX WORKER UPDATE PREPARED; start the existing local-build service', flush=True)


def schema_revision():
    return run('docker', 'exec', 'center-messenger-db-1', 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
               '-U', 'cm_owner', '-d', 'center_messenger', '-c', 'SELECT version_num FROM alembic_version;')


def allows_schema_update(root, source, sha, previous, target):
    plan = update_for(root, source, sha)
    transition = plan.get('schema_transition', {}) if plan else {}
    policy = json.loads((source / 'deploy/max-protection.json').read_text())
    return bool(plan and plan['phase'] == 'prepared' and transition
                and transition == policy.get('paired_schema_update')
                and transition.get('from') == previous and transition.get('to') == target)


def begin_schema_update(root, source, sha, image, backup):
    plan = update_for(root, source, sha)
    if not plan or not plan.get('schema_transition') or plan['phase'] != 'prepared':
        raise RuntimeError('Reviewed paired schema preparation missing')
    if (schema_revision() != plan['schema_transition']['from'] or
            not Path(backup).is_file() or Path(backup).stat().st_size < 1024):
        raise RuntimeError('Schema changed or verified backup missing')
    # Persist before DDL: a crash immediately after COMMIT must be recoverable.
    plan.update(phase='migrating', new_api=image, backup=str(backup),
                new_api_id=json.loads(run('docker', 'image', 'inspect', image))[0]['Id'])
    save_update(root, plan)


def restore_schema(root, plan):
    transition = plan.get('schema_transition')
    if not transition:
        return
    revision = schema_revision()
    if revision == transition['from']:
        return
    if revision != transition['to']:
        raise RuntimeError('Unrelated schema revision; refusing paired rollback')
    if json.loads(run('docker', 'image', 'inspect', plan['new_api']))[0]['Id'] != plan['new_api_id']:
        raise RuntimeError('Schema recovery image identity changed')
    env = dict(os.environ, CM_IMAGE=plan['new_api'])
    run('docker', 'compose', '-p', 'center-messenger', '-f', str(root / 'compose.yaml'),
        '--profile', 'tools', 'run', '--rm', '--no-deps', '--user', '0', 'migrate',
        'alembic', 'downgrade', transition['from'], env=env, timeout=600)
    if schema_revision() != transition['from']:
        raise RuntimeError('Paired schema rollback did not reach reviewed revision')


def promote_update(root, source, sha, old_api, new_api, backup, promoter):
    plan = update_for(root, source, sha)
    if plan is None:
        return False
    if old_api != plan['old_api'] or inspect('center-messenger-api-1')['Config']['Image'] != old_api:
        raise RuntimeError('API changed after worker update preparation')
    if not Path(backup).is_file() or Path(backup).stat().st_size < 1024:
        raise RuntimeError('Verified database backup required before MAX update')
    if plan.get('schema_transition') and (plan['phase'] != 'migrating' or
            schema_revision() != plan['schema_transition']['to']):
        raise RuntimeError('Paired migration must complete before API/MAX replacement')
    image_id = json.loads(run('docker', 'image', 'inspect', new_api))[0]['Id']
    if plan.get('schema_transition') and image_id != plan['new_api_id']:
        raise RuntimeError('Paired schema image identity changed before promotion')
    max_image = 'center-messenger:max-' + sha[:12]
    present = subprocess.run(['docker', 'image', 'inspect', max_image], capture_output=True, text=True, timeout=30)
    if present.returncode == 0 and json.loads(present.stdout)[0]['Id'] != image_id:
        raise RuntimeError('Refusing to overwrite a different pinned MAX image')
    run('docker', 'tag', image_id, max_image)
    archive = Path(tempfile.mkdtemp(prefix='max-update-' + sha[:12] + '-', dir=root / 'rollback'))
    old_env, old_state = (root / '.env').read_bytes(), (root / PLAN).read_bytes()
    next_env = with_image(old_env, max_image)
    next_state = json.loads(old_state)
    next_state.update(worker_image=max_image, worker_id=image_id, worker_source_sha=sha,
                      worker_runtime_contract=worker_contract(source))
    next_body = (json.dumps(next_state, indent=2) + '\n').encode()
    write(archive / 'environment', old_env)
    write(archive / 'runtime-state.json', old_state)
    plan.update(phase='promoting', archive=str(archive), worker_image=max_image, worker_id=image_id,
                new_api=new_api, new_environment=hashlib.sha256(next_env).hexdigest(),
                new_runtime_state=hashlib.sha256(next_body).hexdigest())
    save_update(root, plan)  # durable recovery intent before modifying the pin
    compose = ['docker','compose','-p','center-messenger','-f',str(root / 'compose.yaml')]
    try:
        write(root / '.env', next_env)
        env = dict(os.environ, CM_IMAGE=new_api, CM_MAX_IMAGE=max_image)
        run(*compose, 'config', '--quiet', env=env)
        run(*compose, 'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'max-worker', env=env, timeout=300)
        if not wait_worker(max_image):
            raise RuntimeError('Updated MAX worker did not become ready')
        if plan.get('schema_transition'):
            promoter.deploy_api_with_rollback(old_api, new_api, backup,
                before_rollback=lambda: restore_schema(root, plan))
        else:
            promoter.deploy_api_with_rollback(old_api, new_api, backup)
        if not wait_worker(max_image):
            raise RuntimeError('Updated MAX worker stopped during API promotion')
        write(root / PLAN, next_body)
        plan.update(phase='complete')
        save_update(root, plan)
        print('MAX WORKER UPDATE COMPLETE', max_image, flush=True)
        return True
    except Exception:
        recover_update(root, promoter)
        raise


def recover_update(root, promoter):
    path = root / UPDATE
    if not path.exists():
        return
    plan = json.loads(path.read_text())
    if plan.get('phase') == 'migrating':
        if (digest(root / 'compose.yaml') != plan['compose'] or digest(root / '.env') != plan['environment']
                or digest(root / PLAN) != plan['runtime_state']):
            raise RuntimeError('Runtime changed during paired migration; manual review required')
        _, worker = verify_pin(root)
        if (worker['Id'] != plan['old_container'] or worker['Image'] != plan['old_worker_id']
                or inspect('center-messenger-api-1')['Config']['Image'] != plan['old_api']):
            raise RuntimeError('Images changed during paired migration')
        restore_schema(root, plan)
        if not wait_worker(plan['old_worker']) or not promoter.wait_ready(plan['old_api']):
            raise RuntimeError('Original processes did not recover after paired schema rollback')
        plan['phase'] = 'failed'
        save_update(root, plan)
        print('PAIRED MIGRATION RECOVERED; ORIGINAL API/MAX AND DATA PRESERVED', flush=True)
        return
    if plan.get('phase') != 'promoting':
        return
    archive = Path(plan['archive'])
    if archive.parent != root / 'rollback' or archive.is_symlink() or not archive.name.startswith('max-update-' + plan['sha'][:12] + '-'):
        raise RuntimeError('Unexpected MAX update recovery directory')
    if (digest(root / 'compose.yaml') != plan['compose'] or
            digest(root / '.env') not in {plan['environment'], plan['new_environment']} or
            digest(root / PLAN) not in {plan['runtime_state'], plan['new_runtime_state']}):
        raise RuntimeError('MAX configuration changed outside its update; manual review required')
    if digest(archive / 'environment') != plan['environment'] or digest(archive / 'runtime-state.json') != plan['runtime_state']:
        raise RuntimeError('MAX recovery checksums disagree')
    present = run('docker', 'ps', '-a', '--format', '{{.Names}}').splitlines()
    if NAME in present:
        worker = inspect(NAME)
        if (worker['Image'] not in {plan['old_worker_id'], plan['worker_id']}
                or (worker['Config'].get('Labels') or {}).get('com.docker.compose.project') != 'center-messenger'):
            raise RuntimeError('Unexpected MAX worker identity during update recovery')
    if 'center-messenger-api-1' in present and inspect('center-messenger-api-1')['Config']['Image'] not in {plan['old_api'], plan['new_api']}:
        raise RuntimeError('Unexpected API identity during MAX update recovery')
    write(root / '.env', (archive / 'environment').read_bytes())
    # Old images use exact schema readiness; downgrade before waiting for them.
    restore_schema(root, plan)
    env = dict(os.environ, CM_IMAGE=plan['old_api'], CM_MAX_IMAGE=plan['old_worker'])
    compose = ['docker','compose','-p','center-messenger','-f',str(root / 'compose.yaml')]
    run(*compose, 'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'max-worker', env=env, timeout=300)
    if not wait_worker(plan['old_worker']):
        raise RuntimeError('Previous MAX worker did not recover readiness')
    run(*compose, 'up', '-d', '--no-deps', '--no-build', '--force-recreate', 'api', env=env, timeout=300)
    if not promoter.wait_ready(plan['old_api']):
        raise RuntimeError('Previous API did not recover readiness')
    write(root / PLAN, (archive / 'runtime-state.json').read_bytes())
    plan.update(phase='failed')
    save_update(root, plan)
    print('MAX WORKER UPDATE ROLLED BACK; DATABASE PRESERVED', flush=True)


def prepare(root, source, sha, expected_api, expected_policy):
    if not re.fullmatch('[a-f0-9]{40}', sha):
        raise RuntimeError('Exact source SHA required')
    if run('git', '-C', str(source), 'rev-parse', 'HEAD') != sha:
        raise RuntimeError('Reviewed source SHA mismatch')
    if digest(source / 'deploy/max-protection.json') != expected_policy:
        raise RuntimeError('Reviewed policy mismatch')
    api = inspect('center-messenger-api-1')
    if api['Config']['Image'] != expected_api or api['State']['Status'] != 'running':
        raise RuntimeError('Unexpected active API')
    if any(e.startswith('CM_RUNTIME_ROLE=api') for e in api['Config'].get('Env', [])):
        raise RuntimeError('API is already split; this installer is for the initial transition')
    memory = dict((line.split(':', 1)[0], int(line.split()[1]))
                  for line in Path('/proc/meminfo').read_text().splitlines())
    if memory['MemTotal'] < 3 * 1024 * 1024:
        raise RuntimeError('The initial MAX split requires at least 3 GiB server RAM')
    if memory.get('MemAvailable', 0) < 1024 * 1024:
        raise RuntimeError('The initial MAX split requires at least 1 GiB available RAM')
    present = subprocess.run(['docker', 'inspect', NAME], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    if present.returncode == 0:
        raise RuntimeError('A dedicated MAX container already exists')
    old = root / PLAN
    if old.exists() and json.loads(old.read_text()).get('phase') not in {'prepared', 'failed'}:
        raise RuntimeError('An earlier transition must be recovered first')
    state_path = root / 'local-build-state.json'
    if old.exists() and json.loads(old.read_text()).get('phase') == 'failed' and state_path.exists():
        state = json.loads(state_path.read_text())
        if state.get('last_failed_sha') == sha:
            state.pop('last_failed_sha')
            write(state_path, (json.dumps(state, indent=2) + '\n').encode())
    key = root / '.secrets/max_worker_key'
    key.parent.mkdir(mode=0o700, exist_ok=True)
    if not key.exists():
        write(key, (secrets.token_hex(32) + '\n').encode(), mode=0o400)
    elif key.is_symlink() or not re.fullmatch('[a-f0-9]{64}', key.read_text().strip()):
        raise RuntimeError('Unexpected MAX worker key file')
    os.chown(key, 10001, 10001)
    key.chmod(0o400)
    save(root, {'version': 1, 'phase': 'prepared', 'sha': sha, 'old_api': expected_api,
                'old_compose': digest(root / 'compose.yaml'), 'new_compose': digest(source / 'compose.yaml')})
    print('MAX RUNTIME TRANSITION PREPARED; start the existing local-build service', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--expected-api', required=True)
    parser.add_argument('--expected-policy', required=True)
    parser.add_argument('--update-worker', action='store_true')
    parser.add_argument('--expected-max')
    parser.add_argument('--schema-from', help='Explicit reviewed source revision for a paired schema update')
    options = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Root required')
    import max_release_guard as guard
    with guard.project_lock():
        if options.update_worker:
            if not options.expected_max:
                parser.error('--update-worker requires --expected-max')
            prepare_update(guard.ROOT, options.source.resolve(), options.sha, options.expected_api,
                           options.expected_max, options.expected_policy, options.schema_from)
        else:
            prepare(guard.ROOT, options.source.resolve(), options.sha, options.expected_api, options.expected_policy)
