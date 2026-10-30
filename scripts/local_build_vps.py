#!/usr/bin/env python3
"""Build and promote Center Messenger on VPS from GitHub main, without Actions.

Schema changes are applied here too: when the new image's migration head is
ahead of the database, the script verifies the database revision is in the
image's history, takes and verifies a backup, runs `alembic upgrade head` in
one transaction, then swaps the API. If the new API fails readiness, the
database is downgraded back and the previous API is restored.
"""
import argparse, fcntl, json, os, re, subprocess, sys, tempfile, time
import hashlib
import importlib.util
from contextlib import nullcontext
from pathlib import Path

ROOT=Path("/opt/center-messenger-github")
REPO="rastudiocmit-bot/ramessenger"
STATE=ROOT/"local-build-state.json"
COMPOSE=["docker","compose","-p","center-messenger","-f",str(ROOT/"compose.yaml")]
MAX_GUARD=Path("/etc/ramessenger-max-guard/active")

def enter_max_guard():
    # exec closes the original deploy-lock descriptor (Python opens it with
    # close-on-exec); the pinned entrypoint reacquires the same project lock.
    entry=MAX_GUARD/"scripts/max_release_guard.py"
    os.execv(sys.executable,[sys.executable,str(entry),"run"])

def bootstrap_max_guard(sha):
    """One-time establishment of trust from this explicitly published release.

    Future updates never reach this code: systemd runs the installed guard.
    The existing runner already owns the deploy lock during first bootstrap.
    No API replacement or DB migration takes place before this returns.
    """
    if MAX_GUARD.exists() or MAX_GUARD.is_symlink():
        enter_max_guard()
        return
    with tempfile.TemporaryDirectory(prefix="max-guard-bootstrap-",dir=ROOT) as td:
        source=Path(td)/"source"
        run("gh","repo","clone",REPO,str(source),"--","--depth","1","--branch","main",timeout=300)
        if run("git","-C",str(source),"rev-parse","HEAD")!=sha:
            raise RuntimeError("Main advanced before MAX guard bootstrap; retry")
        policy=source/"deploy/max-protection.json"
        body=policy.read_bytes()
        baseline=json.loads(body)["application_baseline"]
        spec=importlib.util.spec_from_file_location("max_guard_bootstrap",source/"scripts/max_release_guard.py")
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.install(source,hashlib.sha256(body).hexdigest(),baseline,lock_held=True)
    # Cleanup the clone before exec; never leave another source export behind.
    announce("MAX GUARD BOOTSTRAP COMPLETE: entering pinned release checks")
    enter_max_guard()

def main_sha():
    value=run("gh","api",f"repos/{REPO}/commits/main","--jq",".sha",timeout=60)
    if not re.fullmatch(r"[a-f0-9]{40}",value):
        raise RuntimeError("Invalid Git commit")
    return value

def promotion_fresh(sha, old_image):
    """fresh_source_before_promotion: fence stale builds before any DB/API write."""
    if main_sha()!=sha:
        announce("SUPERSEDED: main advanced during build; live API unchanged")
        return False
    if inspect("center-messenger-api-1")["Config"]["Image"]!=old_image:
        raise RuntimeError("Active image changed outside the project deploy lock")
    return True

def require_forward(state, current_image, sha):
    """A reset/force-pushed main must not silently downgrade the live release."""
    tag=re.fullmatch(r"center-messenger:(?:local|github)-([a-f0-9]{12})",current_image)
    if not tag:
        raise RuntimeError("Unknown current image provenance")
    previous=state.get("last_deployed_sha","")
    if not re.fullmatch(r"[a-f0-9]{40}",previous) or not previous.startswith(tag[1]):
        previous=run("gh","api",f"repos/{REPO}/commits/{tag[1]}","--jq",".sha",timeout=60)
    if not re.fullmatch(r"[a-f0-9]{40}",previous) or not previous.startswith(tag[1]):
        raise RuntimeError("Cannot verify current source commit")
    relation=run("gh","api",f"repos/{REPO}/compare/{previous}...{sha}","--jq",".status",timeout=60)
    if relation not in {"ahead","identical"}:
        raise RuntimeError("Main is not a descendant of the live release; manual review required")

def public_url():
    """The messenger's public origin: CM_PUBLIC_URL from the environment or the deploy .env."""
    value = os.environ.get("CM_PUBLIC_URL", "")
    env = ROOT / ".env"
    if not value and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("CM_PUBLIC_URL="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
    value = (value or "https://messenger.rastudio.org").rstrip("/")
    if not re.fullmatch(r"https://[a-z0-9.-]+", value):
        raise RuntimeError("Unexpected CM_PUBLIC_URL")
    return value

def run(*a, cwd=None, timeout=120):
    return subprocess.run(a,cwd=cwd,check=True,stdout=subprocess.PIPE,
                          text=True,timeout=timeout).stdout.strip()

def announce(*a):
    print(*a,flush=True)

def safe_state(s):
    p=STATE.with_suffix(".tmp")
    p.write_text(json.dumps(s,indent=2)+"\n")
    os.chmod(p,0o600)
    os.replace(p,STATE)

def inspect(name):
    return json.loads(run("docker","inspect",name))[0]

def active(unit):
    return subprocess.run(["systemctl","is-active","--quiet",unit]).returncode==0

def guard(src):
    if (src/"compose.yaml").read_bytes()!=(ROOT/"compose.yaml").read_bytes():
        from max_runtime_deploy import validate_transition
        validate_transition(ROOT, src)
    from max_runtime_deploy import verify_runtime_layout
    verify_runtime_layout(ROOT, src)
    for unit in ("center-messenger-gitverse-runner.service","center-messenger-deploy.timer",
                 "ramessenger-github-deploy.timer"):
        if active(unit):
            raise RuntimeError("Conflicting deploy unit active: "+unit)
    db=inspect("center-messenger-db-1")
    if db["State"]["Status"]!="running" or db["State"].get("Health",{}).get("Status")!="healthy":
        raise RuntimeError("PostgreSQL unhealthy")
    names=[m.get("Name") for m in db["Mounts"] if m["Type"]=="volume"]
    if "center-messenger_database" not in names:
        raise RuntimeError("Unexpected database volume")
    revision=run("docker","exec","center-messenger-db-1","psql","-U","cm_owner",
                 "-d","center_messenger","-Atc","SELECT version_num FROM alembic_version;")
    if not re.fullmatch(r"[0-9A-Za-z_]{1,64}",revision):
        raise RuntimeError("Unexpected migration revision: "+revision)
    api=inspect("center-messenger-api-1")
    if api["State"]["Status"]!="running" or api["State"].get("Health",{}).get("Status")!="healthy":
        raise RuntimeError("Current API unhealthy")
    import urllib.request
    for u in ("http://127.0.0.1:8100/health/ready",public_url()+"/health/ready"):
        with urllib.request.urlopen(u,timeout=10) as resp:
            if resp.status!=200 or json.load(resp).get("status")!="ready":
                raise RuntimeError("Current API not ready")
    return api["Config"]["Image"],revision

def alembic(image,*args,timeout=120):
    return run("docker","run","--rm","--network","none","--entrypoint","alembic",image,
               "-c","/app/alembic.ini",*args,timeout=timeout)

def migrate(image,*command):
    # Root inside the container only to read the owner-password secret (root-owned, 0400).
    env=dict(os.environ,CM_IMAGE=image)
    subprocess.run([*COMPOSE,"--profile","tools","run","--rm","--no-deps","--user","0","migrate",*command],
                   cwd=ROOT,env=env,check=True,timeout=600)

def restore_api(image):
    sys.path.insert(0,str(ROOT))
    from github_promote_vps import wait_ready
    env=dict(os.environ,CM_IMAGE=image)
    subprocess.run([*COMPOSE,"up","-d","--no-deps","--no-build","--force-recreate","api"],
                   cwd=ROOT,env=env,check=True,timeout=300)
    if not wait_ready(image):
        raise RuntimeError("Previous API not ready after schema rollback: manual intervention required")

SELF_FILES=(("scripts/local_build_vps.py",ROOT/"local_build_vps.py"),
            ("scripts/github_promote_vps.py",ROOT/"github_promote_vps.py"))

def self_update(sha):
    """Install this script and its helper from the exact commit being deployed.

    The server already builds and runs whatever main contains, so taking the
    deploy scripts from the same commit adds no new trust; it means changes to
    the deploy procedure ship like any other change. Re-executes once.
    """
    if os.environ.get("CM_SELF_UPDATED")==sha:
        return
    changed=False
    for source,dest in SELF_FILES:
        body=run("gh","api",f"repos/{REPO}/contents/{source}?ref={sha}",
                 "-H","Accept: application/vnd.github.raw",timeout=60)+"\n"
        compile(body,source,"exec")
        if dest.exists() and dest.read_text()==body:
            continue
        tmp=dest.with_suffix(".new")
        tmp.write_text(body)
        os.chmod(tmp,0o700)
        os.replace(tmp,dest)
        announce("SELF-UPDATED",dest.name,"from",sha[:12])
        changed=True
    if changed:
        os.execve(sys.executable,[sys.executable,str(ROOT/"local_build_vps.py"),*sys.argv[1:]],
                  dict(os.environ,CM_SELF_UPDATED=sha))

def backup():
    destdir=ROOT/"rollback"
    destdir.mkdir(mode=0o700,exist_ok=True)
    dest=destdir/("before-local-"+time.strftime("%Y%m%d-%H%M%S",time.gmtime())+".dump")
    with dest.open("wb") as f:
        result=subprocess.run(["docker","exec","center-messenger-db-1","pg_dump","-U",
                               "cm_owner","-d","center_messenger","-Fc"],stdout=f,timeout=240)
    os.chmod(dest,0o600)
    if result.returncode!=0 or dest.stat().st_size<1024:
        raise RuntimeError("Backup failed")
    with dest.open("rb") as source:
        validation=subprocess.run(["docker","exec","-i","center-messenger-db-1","pg_restore","--list"],stdin=source,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=False,timeout=60)
    if validation.returncode!=0:
        raise RuntimeError("Backup verification failed: "+validation.stderr.decode(errors="replace")[:500])
    return dest

def main(preview, *, verified_source=None, compatibility=None, helper_root=None):
    if os.geteuid()!=0:
        raise RuntimeError("Root required")
    s=json.loads(STATE.read_text()) if STATE.exists() else {"enabled":False}
    if not preview and s.get("enabled") is not True:
        announce("DISABLED: local-build-state enabled is not true")
        return
    # Only the independently installed MAX guard supplies verified_source.
    # The normal CLI performs the one-time bootstrap, then enters that guard.
    sha=verified_source[1] if verified_source else main_sha()
    current=inspect("center-messenger-api-1")["Config"]["Image"]
    target="center-messenger:local-"+sha[:12]
    announce("MAIN",sha,"CURRENT",current,"TARGET",target)
    if preview:
        announce("PREVIEW: no clone, build or changes")
        return
    if not verified_source and (MAX_GUARD.exists() or MAX_GUARD.is_symlink()):
        # Even a direct invocation of the old runner must not self-update once
        # the external guard has established trust.
        enter_max_guard()
        return
    if current!=target:
        # Validate before self-update too: otherwise a reset of main could
        # replace this protection with an older runner before it is checked.
        require_forward(s,current,sha)
    if not verified_source:
        self_update(sha)
        bootstrap_max_guard(sha)
        return
    # Finish an interrupted first split before planning any further promotion.
    if helper_root:
        sys.path.insert(0, str(helper_root))
        import github_promote_vps as promoter
        from max_runtime_deploy import recover
        recover(ROOT, promoter)
        current=inspect("center-messenger-api-1")["Config"]["Image"]
    if current==target:
        s["last_deployed_sha"]=sha
        safe_state(s)
        announce("UNCHANGED")
        return
    if sha==s.get("last_failed_sha"):
        announce("SKIP: SHA blocked after prior failed promotion")
        return
    context = nullcontext(None) if verified_source else tempfile.TemporaryDirectory(prefix="local-source-",dir=ROOT)
    with context as td:
        src=Path(verified_source[0]) if verified_source else Path(td)/"source"
        if not verified_source:
            run("gh","repo","clone",REPO,str(src),"--","--depth","1","--branch","main",timeout=300)
        if run("git","-C",str(src),"rev-parse","HEAD")!=sha:
            announce("Main advanced while cloning; will retry")
            return
        old,db_rev=guard(src)
        run("python3","-m","compileall","-q","app/backend/src","app/backend/migrations",
            "scripts",cwd=src,timeout=120)
        run("python3","-m","unittest","discover","-s","tests",
            "-p","test_github_deploy_rollback.py",cwd=src,timeout=120)
        run("python3","-m","unittest","discover","-s","tests",
            "-p","test_support_access.py",cwd=src,timeout=120)
        run("python3","-m","unittest","discover","-s","tests",
            "-p","test_max_runtime_deploy.py",cwd=src,timeout=120)
        announce("BUILD",target)
        subprocess.run(["docker","build","--platform","linux/amd64","--build-arg","CM_BUILD_SHA="+sha,"-t",target,"."],
                       cwd=src,check=True,timeout=1500)
        image=json.loads(run("docker","image","inspect",target))[0]
        if (image["Os"],image["Architecture"])!=("linux","amd64"):
            raise RuntimeError("Wrong image platform")
        heads=alembic(target,"heads").splitlines()
        if len(heads)!=1 or " (head)" not in heads[0]:
            raise RuntimeError("Unexpected migration heads: "+repr(heads))
        head=heads[0].split(" ")[0]
        runtime_revision=run("docker","run","--rm","--network","none",
                             "--entrypoint","/opt/venv/bin/python",target,"-c",
                             "from center_messenger.database import SCHEMA_REVISION; print(SCHEMA_REVISION)")
        if runtime_revision!=head:
            raise RuntimeError("Image readiness revision does not match migration head: "+
                               runtime_revision+" != "+head)
        if compatibility is not None:
            # Trusted tests run in the new image, before backup/migrations/API
            # replacement. They have no production secrets, DB or CRM network.
            compatibility(target)
        if head!=db_rev:
            transition = ROOT/'max-runtime-transition.json'
            if transition.exists():
                from max_runtime_deploy import allows_schema_update
                if not allows_schema_update(ROOT, src, sha, db_rev, head):
                    raise RuntimeError('Frozen MAX worker requires a separately reviewed paired schema update')
            # The database must be an ancestor of the new head (no branches, no downgrades).
            history=alembic(target,"history","-r",db_rev+":head")
            if not history or db_rev not in history:
                raise RuntimeError("Database revision "+db_rev+" is not in the image history")
        current_image,current_rev=guard(src)
        if current_image!=old or current_rev!=db_rev:
            raise RuntimeError("API or schema changed during build")
        if not promotion_fresh(sha,old):
            return
        dest=backup()
        announce("BACKUP VERIFIED",dest)
        # A second agent can publish while pg_dump runs. Once this check passes,
        # the serialized promotion is pinned to sha through migration/readiness/
        # rollback; a newer request is processed on the next service run.
        if not promotion_fresh(sha,old):
            return
        sys.path.insert(0,str(helper_root or ROOT))
        import github_promote_vps as promoter
        from github_promote_vps import deploy_api_with_rollback
        migrated=False
        paired_schema_started=False
        try:
            if head!=db_rev:
                if (ROOT/'max-runtime-transition.json').exists():
                    from max_runtime_deploy import begin_schema_update
                    begin_schema_update(ROOT, src, sha, target, dest)
                    paired_schema_started=True
                announce("MIGRATE",db_rev,"->",head)
                migrate(target)
                migrated=True
            from max_runtime_deploy import promote
            if not promote(ROOT,src,sha,old,target,dest,promoter):
                deploy_api_with_rollback(old,target,dest)
        except Exception:
            s["last_failed_sha"]=sha
            safe_state(s)
            from max_runtime_deploy import recover
            recover(ROOT, promoter)
            if migrated and not paired_schema_started:
                announce("SCHEMA ROLLBACK",head,"->",db_rev,"backup",dest)
                migrate(target,"alembic","downgrade",db_rev)
                restore_api(old)
                announce("PREVIOUS VERSION RESTORED",old)
            raise
        s["last_deployed_sha"]=sha
        s.pop("last_failed_sha",None)
        safe_state(s)
        announce("LOCAL DEPLOYED",sha)

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--preview",action="store_true")
    opts=p.parse_args()
    try:
        with (ROOT/".deploy.lock").open("a+") as handle:
            os.chmod(ROOT/".deploy.lock",0o600)
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            main(opts.preview)
    except Exception as exc:
        announce("LOCAL DEPLOY BLOCKED:",str(exc))
        sys.exit(1)
