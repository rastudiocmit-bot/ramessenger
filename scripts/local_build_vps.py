#!/usr/bin/env python3
"""Build and promote Center Messenger on VPS from GitHub main, without Actions.

Schema changes are applied here too: when the new image's migration head is
ahead of the database, the script verifies the database revision is in the
image's history, takes and verifies a backup, runs `alembic upgrade head` in
one transaction, then swaps the API. If the new API fails readiness, the
database is downgraded back and the previous API is restored.
"""
import argparse, fcntl, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

ROOT=Path("/opt/center-messenger-github")
REPO="rastudiocmit-bot/ramessenger"
STATE=ROOT/"local-build-state.json"
COMPOSE=["docker","compose","-p","center-messenger","-f",str(ROOT/"compose.yaml")]

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
        raise RuntimeError("Compose changed: manual review required")
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
    for u in ("http://127.0.0.1:8100/health/ready","https://messenger.rastudio.org/health/ready"):
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

def main(preview):
    if os.geteuid()!=0:
        raise RuntimeError("Root required")
    s=json.loads(STATE.read_text()) if STATE.exists() else {"enabled":False}
    if not preview and s.get("enabled") is not True:
        announce("DISABLED: local-build-state enabled is not true")
        return
    sha=run("gh","api",f"repos/{REPO}/commits/main","--jq",".sha",timeout=60)
    if not re.fullmatch(r"[a-f0-9]{40}",sha):
        raise RuntimeError("Invalid Git commit")
    current=inspect("center-messenger-api-1")["Config"]["Image"]
    target="center-messenger:local-"+sha[:12]
    announce("MAIN",sha,"CURRENT",current,"TARGET",target)
    if preview:
        announce("PREVIEW: no clone, build or changes")
        return
    self_update(sha)
    if current==target:
        s["last_deployed_sha"]=sha
        safe_state(s)
        announce("UNCHANGED")
        return
    if sha==s.get("last_failed_sha"):
        announce("SKIP: SHA blocked after prior failed promotion")
        return
    with tempfile.TemporaryDirectory(prefix="local-source-",dir=ROOT) as td:
        src=Path(td)/"source"
        run("gh","repo","clone",REPO,str(src),"--","--depth","1","--branch","main",timeout=300)
        if run("git","-C",str(src),"rev-parse","HEAD")!=sha:
            announce("Main advanced while cloning; will retry")
            return
        old,db_rev=guard(src)
        run("python3","-m","compileall","-q","app/backend/src","app/backend/migrations",
            "scripts",cwd=src,timeout=120)
        run("python3","-m","unittest","discover","-s","tests",
            "-p","test_github_deploy_rollback.py",cwd=src,timeout=120)
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
        if head!=db_rev:
            # The database must be an ancestor of the new head (no branches, no downgrades).
            history=alembic(target,"history","-r",db_rev+":head")
            if not history or db_rev not in history:
                raise RuntimeError("Database revision "+db_rev+" is not in the image history")
        _,db_rev=guard(src)
        dest=backup()
        announce("BACKUP VERIFIED",dest)
        sys.path.insert(0,str(ROOT))
        from github_promote_vps import deploy_api_with_rollback
        migrated=False
        try:
            if head!=db_rev:
                announce("MIGRATE",db_rev,"->",head)
                migrate(target)
                migrated=True
            deploy_api_with_rollback(old,target,dest)
        except Exception:
            s["last_failed_sha"]=sha
            safe_state(s)
            if migrated:
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
