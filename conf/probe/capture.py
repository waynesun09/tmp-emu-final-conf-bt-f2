"""Run the conformance probe workflows on one server (real GitHub or the emulator) and capture responses.

Env:
  API      REST root (https://api.github.com or https://ghemu.localhost/api/v3)
  TOKEN    token with admin on the repo
  OWNER, REPO
  OUT      output directory (capture.jsonl, behaviour.json, logs/)
  PUSH     1 to render the templates and push them (needs GIT_URL), 0 if already pushed
  GIT_URL  clone URL with credentials (PUSH=1)
  TEMPLATES template directory
Writes capture.jsonl in validate_rest.py's format: {method, path, status, body}.
"""
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ["API"].rstrip("/")
TOKEN = os.environ["TOKEN"]
OWNER, REPO = os.environ["OWNER"], os.environ["REPO"]
OUT = pathlib.Path(os.environ["OUT"])
R = f"/repos/{OWNER}/{REPO}"
(OUT / "logs").mkdir(parents=True, exist_ok=True)
CAP = open(OUT / "capture.jsonl", "a")
NOTES = []
PROBES = ["p-context", "p-artifacts", "p-reusable", "p-unresolved", "p-container", "p-cancel", "p-apicancel"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def req(method, path, body=None, auth=True, url=None, ctype="application/json"):
    u = url or (API + path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(u, data=data, method=method)
    if auth:
        r.add_header("Authorization", f"Bearer {TOKEN}")
        r.add_header("Accept", "application/vnd.github+json")
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None and ctype:
        r.add_header("Content-Type", ctype)
    try:
        resp = OPENER.open(r, timeout=60)
        status, headers, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, headers, raw = e.code, dict(e.headers), e.read()
    except Exception as e:  # network error
        return 0, {}, str(e).encode()
    return status, headers, raw


def jbody(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def rec(method, path, status, raw, note=None):
    line = {"method": method, "path": path, "status": status, "body": jbody(raw)}
    if note:
        line["note"] = note
    CAP.write(json.dumps(line) + "\n")
    CAP.flush()
    return line["body"]


def call(method, path, body=None, note=None):
    s, h, raw = req(method, path, body)
    b = rec(method, path, s, raw, note)
    return s, h, b


def push():
    tpl = pathlib.Path(os.environ["TEMPLATES"])
    work = pathlib.Path(tempfile.mkdtemp())
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    subprocess.run(["git", "-c", "credential.helper=", "clone", "-q", os.environ["GIT_URL"], str(work / "r")], check=True, env=env)
    wf = work / "r" / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    for f in tpl.glob("*.yml"):
        (wf / f.name).write_text(f.read_text().replace("__OWNER__", OWNER).replace("__REPO__", REPO))
    (work / "r" / "conf" / "action").mkdir(parents=True, exist_ok=True)
    shutil.copy(tpl / "action" / "action.yml", work / "r" / "conf" / "action" / "action.yml")
    g = ["git", "-C", str(work / "r"), "-c", "user.name=conf-probe", "-c", "user.email=conf-probe@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "conformance probe workflows"], check=True)
    subprocess.run(g + ["-c", "credential.helper=", "push", "-q", "origin", "HEAD:main"], check=True, env=env)


def workflows():
    names = {}
    for _ in range(30):
        s, _, b = req("GET", f"{R}/actions/workflows?per_page=100")
        names = {pathlib.Path(w["path"]).stem: w for w in (jbody(b) or {}).get("workflows", [])}
        if all(p in names for p in PROBES):
            rec("GET", f"{R}/actions/workflows?per_page=100", s, b)
            return names
        time.sleep(5)
    sys.exit("workflows not registered: " + json.dumps(sorted(names)))


def dispatch(name):
    s, h, raw = req("POST", f"{R}/actions/workflows/{name}.yml/dispatches", {"ref": "main"})
    rec("POST", f"{R}/actions/workflows/{name}.yml/dispatches", s, raw)
    return s, jbody(raw)


def runs_of(wf_id, since):
    s, _, raw = req("GET", f"{R}/actions/workflows/{wf_id}/runs?per_page=20")
    out = [r for r in (jbody(raw) or {}).get("workflow_runs", []) if r["created_at"] >= since]
    return sorted(out, key=lambda r: r["id"])


def wait_run(wf_id, since, n=1, cond=lambda r: True, timeout=600):
    end = time.time() + timeout
    while time.time() < end:
        rs = runs_of(wf_id, since)
        if len(rs) >= n and cond(rs[n - 1]):
            return rs[n - 1]
        time.sleep(5)
    return None


def jobs(run_id):
    s, _, raw = req("GET", f"{R}/actions/runs/{run_id}/jobs?per_page=100")
    return (jbody(raw) or {}).get("jobs", [])


def log_text(job_id):
    s, h, raw = req("GET", f"{R}/actions/jobs/{job_id}/logs")
    loc = h.get("Location") or h.get("location")
    info = {"status": s, "redirect_host": urllib.parse.urlparse(loc).hostname if loc else None,
            "content_type": h.get("Content-Type") or h.get("content-type")}
    if s in (301, 302, 307) and loc:
        s2, _, raw = req("GET", None, auth=False, url=loc)
        info["followed_status"] = s2
    return info, raw.decode("utf-8", "replace")


TS = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ?")


def parse_log(text):
    lines = [TS.sub("", l) for l in text.splitlines()]
    marks = sorted(set(m for l in lines if not l.startswith(("echo ", "##[group]Run", "  echo")) for m in re.findall(r"MARK-[A-Za-z0-9_.-]+", l)))
    blocks, cur, name = {}, None, None
    for l in lines:
        m = re.fullmatch(r"BEGIN-([A-Z]+)", l.strip())
        if m:
            name, cur = m.group(1), []
            continue
        if name and l.strip() == f"END-{name}":
            parsed = []
            for x in cur:
                try:
                    parsed.append(json.loads(x))
                except ValueError:
                    pass
            blocks[name] = parsed
            name = None
            continue
        if name is not None:
            cur.append(l.strip())
    annotations = [l for l in lines if re.match(r"##\[(notice|warning|error)\]", l) or "::notice" in l or "::warning" in l]
    return marks, blocks, annotations[:10]


def main():
    if os.environ.get("PUSH") == "1":
        push()
    wfs = workflows()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    beh = {"side": os.environ.get("SIDE"), "api": API, "dispatch": {}}
    for p in ["p-context", "p-artifacts", "p-reusable", "p-unresolved", "p-container"]:
        beh["dispatch"][p] = dispatch(p)
    # 5c + 4g: first p-cancel run; list jobs while slow runs; then supersede.
    beh["dispatch"]["p-cancel#1"] = dispatch("p-cancel")
    cid = wfs["p-cancel"]["id"]
    r1 = wait_run(cid, since, 1, lambda r: r["status"] == "in_progress", 300)
    if r1:
        for _ in range(40):
            js = jobs(r1["id"])
            if any(j["name"] == "slow" and j["status"] == "in_progress" for j in js):
                break
            time.sleep(3)
        time.sleep(5)
        s, _, b = call("GET", f"{R}/actions/runs/{r1['id']}/jobs?per_page=100", note="mid-run, slow in progress")
        beh["midrun_jobs"] = [{"name": j["name"], "status": j["status"], "conclusion": j["conclusion"]} for j in (b or {}).get("jobs", [])]
        beh["dispatch"]["p-cancel#2"] = dispatch("p-cancel")
    # 4g: API cancel.
    beh["dispatch"]["p-apicancel"] = dispatch("p-apicancel")
    aid = wfs["p-apicancel"]["id"]
    ra = wait_run(aid, since, 1, lambda r: r["status"] == "in_progress", 300)
    if ra:
        for _ in range(40):
            if any(j["status"] == "in_progress" for j in jobs(ra["id"])):
                break
            time.sleep(3)
        time.sleep(10)
        beh["apicancel"] = call("POST", f"{R}/actions/runs/{ra['id']}/cancel")[0]
    # Wait for everything.
    end = time.time() + 1200
    allruns = {}
    while time.time() < end:
        allruns = {p: runs_of(wfs[p]["id"], since) for p in PROBES}
        pending = [p for p, rs in allruns.items() if any(r["status"] != "completed" for r in rs)]
        if not pending:
            break
        time.sleep(10)
    beh["runs"] = {}
    for p, rs in allruns.items():
        beh["runs"][p] = []
        for r in rs:
            _, _, rb = call("GET", f"{R}/actions/runs/{r['id']}")
            rb = rb or {}
            _, _, jb = call("GET", f"{R}/actions/runs/{r['id']}/jobs?per_page=100")
            call("GET", f"{R}/actions/runs/{r['id']}/artifacts")
            entry = {"id": r["id"], "status": rb.get("status"), "conclusion": rb.get("conclusion"),
                     "run_keys": sorted(rb.keys()) if rb else None, "jobs": []}
            for j in (jb or {}).get("jobs", []):
                info, text = log_text(j["id"])
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{p}-{r['id']}-{j['name']}")
                (OUT / "logs" / f"{safe}.txt").write_text(text)
                marks, blocks, ann = parse_log(text)
                entry["jobs"].append({"name": j["name"], "status": j["status"], "conclusion": j["conclusion"],
                                      "steps": [(s["name"], s["conclusion"]) for s in j.get("steps") or []],
                                      "log": info, "marks": marks, "blocks": blocks, "annotations": ann})
            beh["runs"][p].append(entry)
    call("GET", f"{R}/actions/runs?per_page=10")
    for p in ["p-context", "p-artifacts"]:
        call("GET", f"{R}/actions/workflows/{wfs[p]['id']}")
        call("GET", f"{R}/actions/workflows/{wfs[p]['id']}/runs?per_page=5")
    # Artifacts.
    _, _, ab = call("GET", f"{R}/actions/artifacts?per_page=20")
    beh["artifacts"] = []
    for a in (ab or {}).get("artifacts", [])[:2]:
        call("GET", f"{R}/actions/artifacts/{a['id']}")
        s, h, raw = req("GET", f"{R}/actions/artifacts/{a['id']}/zip")
        loc = h.get("Location") or h.get("location")
        beh["artifacts"].append({"name": a["name"], "size_in_bytes": a.get("size_in_bytes"), "digest": a.get("digest"),
                                 "zip_status": s, "zip_redirect_host": urllib.parse.urlparse(loc).hostname if loc else None,
                                 "zip_content_type": h.get("Content-Type") or h.get("content-type")})
    # Secrets and variables (admin token).
    _, _, pk = call("GET", f"{R}/actions/secrets/public-key")
    try:
        from nacl import encoding, public
        box = public.SealedBox(public.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
        enc = base64.b64encode(box.encrypt(b"conf-probe-value")).decode()
        call("PUT", f"{R}/actions/secrets/CONF_PROBE", {"encrypted_value": enc, "key_id": pk["key_id"]})
        call("PUT", f"{R}/actions/secrets/CONF_PROBE", {"encrypted_value": enc, "key_id": pk["key_id"]}, note="update")
        call("GET", f"{R}/actions/secrets/CONF_PROBE")
        call("DELETE", f"{R}/actions/secrets/CONF_PROBE")
        call("GET", f"{R}/actions/secrets/CONF_PROBE", note="after delete")
    except Exception as e:  # pynacl missing
        NOTES.append(f"secrets skipped: {e}")
    call("POST", f"{R}/actions/variables", {"name": "CONF_PROBE", "value": "one"})
    call("POST", f"{R}/actions/variables", {"name": "CONF_PROBE", "value": "dup"}, note="duplicate")
    call("GET", f"{R}/actions/variables/CONF_PROBE")
    call("PATCH", f"{R}/actions/variables/CONF_PROBE", {"name": "CONF_PROBE", "value": "two"})
    call("GET", f"{R}/actions/variables?per_page=10")
    call("DELETE", f"{R}/actions/variables/CONF_PROBE")
    call("GET", f"{R}/actions/variables/CONF_PROBE", note="after delete")
    call("PATCH", f"{R}/actions/variables/CONF_MISSING", {"name": "CONF_MISSING", "value": "x"}, note="missing")
    _, _, perm = call("GET", f"{R}/actions/permissions")
    if perm:
        call("PUT", f"{R}/actions/permissions", {"enabled": perm.get("enabled", True)})
    # Unauthenticated: run and jobs of a public repo / emulator repo.
    rid = (beh["runs"].get("p-context") or [{}])[0].get("id")
    if rid:
        s, _, raw = req("GET", f"{R}/actions/runs/{rid}", auth=False)
        rec("GET", f"{R}/actions/runs/{rid}", s, raw, note="anonymous")
    beh["notes"] = NOTES
    (OUT / "behaviour.json").write_text(json.dumps(beh, indent=1))
    print("captured", sum(1 for _ in open(OUT / "capture.jsonl")), "responses")


if __name__ == "__main__":
    main()
