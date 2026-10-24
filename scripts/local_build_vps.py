#!/usr/bin/env python3
"""Build and promote Center Messenger on VPS from GitHub main, without Actions."""
import argparse, fcntl, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

ROOT=Path("/opt/center-messenger-github")
REPO="rastudiocmit-bot/ramessenger"
STATE=ROOT/"local-build-state.json"
REV="0013_ai_voice"

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
    if revision!=REV:
        raise RuntimeError("Migration revision incompatible: "+revision)
    api=inspect("center-messenger-api-1")
    if api["State"]["Status"]!="running" or api["State"].get("Health",{}).get("Status")!="healthy":
        raise RuntimeError("Current API unhealthy")
    import urllib.request
    for u in ("http://127.0.0.1:8100/health/ready","https://messenger.rastudio.org/health/ready"):
        with urllib.request.urlopen(u,timeout=10) as resp:
            if resp.status!=200 or json.load(resp).get("status")!="ready":
                raise RuntimeError("Current API not ready")
    return api["Config"]["Image"]

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
        old=guard(src)
        run("python3","-m","compileall","-q","app/backend/src","app/backend/migrations",
            "scripts",cwd=src,timeout=120)
        run("python3","-m","unittest","discover","-s","tests",
            "-p","test_github_deploy_rollback.py",cwd=src,timeout=120)
        announce("BUILD",target)
        subprocess.run(["docker","build","--platform","linux/amd64","-t",target,"."],
                       cwd=src,check=True,timeout=1500)
        image=json.loads(run("docker","image","inspect",target))[0]
        if (image["Os"],image["Architecture"])!=("linux","amd64"):
            raise RuntimeError("Wrong image platform")
        heads=run("docker","run","--rm","--entrypoint","alembic",target,
                  "-c","/app/alembic.ini","heads",timeout=120).splitlines()
        if len(heads)!=1 or not heads[0].startswith(REV+" "):
            raise RuntimeError("Unexpected migration head: "+repr(heads))
        guard(src)
        dest=backup()
        announce("BACKUP VERIFIED",dest)
        sys.path.insert(0,str(ROOT))
        from github_promote_vps import deploy_api_with_rollback
        try:
            deploy_api_with_rollback(old,target,dest)
        except Exception:
            s["last_failed_sha"]=sha
            safe_state(s)
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
