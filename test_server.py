"""
Server contract tests (Member A).

Usage (server must already be running):
    python test_server.py                              # uses http://172.16.242.33:5000
    python test_server.py http://localhost:5000        # a server on your own machine

Safe to run against the shared server: every manifest it creates lives under
a random "_servertest_xxxxxxxx/" prefix and is deleted at the end, and all
chunk data is random, so it never touches the demo files.
"""
import hashlib
import os
import sys

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://172.16.242.33:5000").rstrip("/")
TIMEOUT = 10
sha = lambda b: hashlib.sha256(b).hexdigest()

PFX = "_servertest_" + os.urandom(4).hex()
P_DOC, P_EMPTY, P_DEL = f"{PFX}/docs/a.txt", f"{PFX}/empty.txt", f"{PFX}/del.txt"

a, b = os.urandom(1000), os.urandom(2000)
ha, hb = sha(a), sha(b)
ghost = sha(b"never uploaded " + os.urandom(8))


def put(h, data):
    return requests.put(f"{BASE}/chunks/{h}", data=data, timeout=TIMEOUT,
                        headers={"Content-Type": "application/octet-stream"})


def missing(hashes):
    r = requests.post(f"{BASE}/chunks/missing", json={"hashes": hashes}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["missing"]


def commit(manifest):
    return requests.put(f"{BASE}/files", json=manifest, timeout=TIMEOUT)


passed = 0


def check(name, cond, detail=""):
    global passed
    if not cond:
        print(f"FAIL  {name} {detail}")
        sys.exit(1)
    passed += 1
    print(f"ok    {name}")


try:
    r = requests.get(f"{BASE}/files", timeout=TIMEOUT)
except requests.RequestException as exc:
    print(f"ERROR: Could not connect to server at {BASE}\nMake sure the server is running.\n({exc})")
    sys.exit(2)
check("GET /files reachable and returns a 'files' list",
      r.status_code == 200 and isinstance(r.json().get("files"), list))

# --- /chunks/missing ---
m = missing([ha, ha, hb])
check("missing: unknown hashes reported, de-duplicated", m == [ha, hb], m)
check("missing: empty request gives empty list", missing([]) == [])
r = requests.post(f"{BASE}/chunks/missing", json={"hashes": ["nothex"]}, timeout=TIMEOUT)
check("missing: invalid hash -> 400", r.status_code == 400)
r = requests.post(f"{BASE}/chunks/missing", json={"wrong": 1}, timeout=TIMEOUT)
check("missing: bad body -> 400", r.status_code == 400)

# --- PUT /chunks/<hash> ---
check("upload: new chunk -> stored", put(ha, a).json() == {"status": "stored"})
check("upload: same chunk again -> exists", put(ha, a).json() == {"status": "exists"})
check("missing: stored chunk no longer missing", missing([ha]) == [])
r = put(hb, a)
check("upload: hash mismatch -> 400 'hash mismatch'",
      r.status_code == 400 and r.json()["error"] == "hash mismatch")
check("upload: mismatching data was not stored", missing([hb]) == [hb])
check("upload: invalid hash in URL -> 400", put("not-a-hash", a).status_code == 400)
big = os.urandom(1_048_576 + 1)
check("upload: chunk larger than 1 MiB -> 400", put(sha(big), big).status_code == 400)
exact = os.urandom(1_048_576)
check("upload: exactly 1 MiB accepted", put(sha(exact), exact).status_code == 200)

# --- GET /chunks/<hash> ---
r = requests.get(f"{BASE}/chunks/{ha}", timeout=TIMEOUT)
check("download: returns the raw bytes", r.status_code == 200 and r.content == a)
r = requests.get(f"{BASE}/chunks/{ghost}", timeout=TIMEOUT)
check("download: unknown chunk -> 404 'not found'",
      r.status_code == 404 and r.json()["error"] == "not found")
check("upload second chunk", put(hb, b).status_code == 200)

# --- PUT /files ---
man = {"path": P_DOC, "size": 3000, "chunks": [ha, hb, ha]}      # real total is 4000
r = commit(man)
check("commit: wrong size -> 400 'size mismatch'",
      r.status_code == 400 and r.json()["error"] == "size mismatch")
r = commit({**man, "chunks": [ha, ghost]})
check("commit: missing chunk -> 400 + 'missing' list",
      r.status_code == 400 and r.json()["error"] == "missing chunks"
      and r.json()["missing"] == [ghost])
good = {"path": P_DOC, "size": 4000, "chunks": [ha, hb, ha]}     # repeated chunk A
r = commit(good)
check("commit: valid manifest (repeated chunk) -> committed",
      r.status_code == 200 and r.json()["status"] == "committed", r.text)
v = r.json()["version"]

for bad in ["../x", "/abs", "a//b", "a/./b", "C:/x", "a\\b", ""]:
    r = commit({"path": bad, "size": 0, "chunks": []})
    check(f"commit: unsafe path {bad!r} rejected", r.status_code == 400)
r = commit({"path": f"{PFX}/bad.txt", "size": 0, "chunks": ["xyz"]})
check("commit: invalid chunk hash -> 400", r.status_code == 400)

r = commit({"path": P_EMPTY, "size": 0, "chunks": []})
check("commit: empty file (no chunks) accepted", r.status_code == 200)

# --- GET /files, GET /files/manifest ---
files = {f["path"]: f for f in requests.get(f"{BASE}/files", timeout=TIMEOUT).json()["files"]}
check("list: committed files appear with sizes",
      files[P_DOC]["size"] == 4000 and files[P_EMPTY]["size"] == 0)
r = requests.get(f"{BASE}/files/manifest", params={"path": P_DOC}, timeout=TIMEOUT)
check("manifest: chunks returned in order with repeats", r.json()["chunks"] == [ha, hb, ha])
r = requests.get(f"{BASE}/files/manifest", params={"path": P_EMPTY}, timeout=TIMEOUT)
check("manifest: empty file -> size 0, no chunks",
      r.json()["size"] == 0 and r.json()["chunks"] == [])
r = requests.get(f"{BASE}/files/manifest", params={"path": f"{PFX}/nope"}, timeout=TIMEOUT)
check("manifest: unknown path -> 404", r.status_code == 404)

# --- versions / conflicts ---
r = commit({**good, "base_version": v})
check("version: matching base_version accepted, version+1",
      r.status_code == 200 and r.json()["version"] == v + 1)
r = commit({**good, "base_version": v})                          # stale
check("version: stale base_version -> 409",
      r.status_code == 409 and r.json()["current_version"] == v + 1)

# --- DELETE ---
check("delete: setup", commit({"path": P_DEL, "size": 0, "chunks": []}).status_code == 200)
check("delete: existing file -> 200",
      requests.delete(f"{BASE}/files", params={"path": P_DEL}, timeout=TIMEOUT).status_code == 200)
check("delete: again -> 404",
      requests.delete(f"{BASE}/files", params={"path": P_DEL}, timeout=TIMEOUT).status_code == 404)

# --- clean up the manifests this test created ---
for p in (P_DOC, P_EMPTY):
    requests.delete(f"{BASE}/files", params={"path": p}, timeout=TIMEOUT)
left = [f["path"] for f in requests.get(f"{BASE}/files", timeout=TIMEOUT).json()["files"]
        if f["path"].startswith(PFX)]
check("cleanup: test manifests removed", left == [], left)

print(f"\nALL TESTS PASSED ({passed} checks) against {BASE}")
