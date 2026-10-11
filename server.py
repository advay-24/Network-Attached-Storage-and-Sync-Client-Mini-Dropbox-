"""Mini-Dropbox server (Member A).

Storage layout:
  storage/chunks/<hash>     raw chunk bytes
  storage/manifests.db      SQLite: path -> size, chunks (JSON), version
"""
import hashlib
import json
import os
import re
import sqlite3
import tempfile

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

STORAGE = os.environ.get("STORAGE_DIR", "storage")
CHUNK_DIR = os.path.join(STORAGE, "chunks")
DB_PATH = os.path.join(STORAGE, "manifests.db")
MAX_CHUNK = int(os.environ.get("MAX_CHUNK", 1024 * 1024))  # 1 MiB

HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")

os.makedirs(CHUNK_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # room for big manifests


# ---------- helpers ----------
def err(msg, code, **extra):
    return jsonify({"error": msg, **extra}), code


def valid_hash(h):
    return isinstance(h, str) and HASH_RE.match(h) is not None


def valid_path(p):
    if not isinstance(p, str) or not p:
        return False
    if p.startswith("/") or "\\" in p or "\x00" in p or DRIVE_RE.match(p):
        return False
    return all(seg not in ("", ".", "..") for seg in p.split("/"))


def chunk_path(h):
    return os.path.join(CHUNK_DIR, h)


def db():
    # isolation_level=None -> we control transactions explicitly
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS manifests ("
        "path TEXT PRIMARY KEY, size INTEGER NOT NULL, "
        "chunks TEXT NOT NULL, version INTEGER NOT NULL)"
    )
    return conn


# ---------- error handlers (always JSON) ----------
@app.errorhandler(HTTPException)
def http_err(e):
    return err(e.name.lower(), e.code)


@app.errorhandler(Exception)
def server_err(e):
    app.logger.exception(e)
    return err("internal error", 500)


# ---------- chunks ----------
@app.post("/chunks/missing")
def chunks_missing():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("hashes"), list):
        return err("expected {'hashes': [...]}", 400)
    seen, missing = set(), []
    for h in body["hashes"]:
        if not valid_hash(h):
            return err("invalid hash", 400)
        if h in seen:
            continue
        seen.add(h)
        if not os.path.exists(chunk_path(h)):
            missing.append(h)
    return jsonify({"missing": missing})


@app.put("/chunks/<h>")
def put_chunk(h):
    if not valid_hash(h):
        return err("invalid hash", 400)
    data = request.get_data()
    if len(data) > MAX_CHUNK:
        return err("chunk too large", 400)
    if hashlib.sha256(data).hexdigest() != h:
        return err("hash mismatch", 400)
    dest = chunk_path(h)
    if os.path.exists(dest):
        return jsonify({"status": "exists"})
    fd, tmp = tempfile.mkstemp(dir=CHUNK_DIR, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)  # atomic
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return jsonify({"status": "stored"})


@app.get("/chunks/<h>")
def get_chunk(h):
    if not valid_hash(h):
        return err("invalid hash", 400)
    p = chunk_path(h)
    if not os.path.exists(p):
        return err("not found", 404)
    with open(p, "rb") as f:
        data = f.read()
    return data, 200, {"Content-Type": "application/octet-stream"}


# ---------- files / manifests ----------
@app.put("/files")
def commit_file():
    m = request.get_json(silent=True)
    if not isinstance(m, dict):
        return err("expected JSON manifest", 400)
    path, size, chunks = m.get("path"), m.get("size"), m.get("chunks")
    base = m.get("base_version")

    if not valid_path(path):
        return err("invalid path", 400)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return err("invalid size", 400)
    if not isinstance(chunks, list) or not all(valid_hash(h) for h in chunks):
        return err("invalid chunks", 400)
    if base is not None and (not isinstance(base, int) or isinstance(base, bool) or base < 0):
        return err("invalid base_version", 400)

    missing = []
    for h in dict.fromkeys(chunks):  # unique, in order
        if not os.path.exists(chunk_path(h)):
            missing.append(h)
    if missing:
        return err("missing chunks", 400, missing=missing)

    total = sum(os.path.getsize(chunk_path(h)) for h in chunks)
    if total != size:
        return err("size mismatch", 400)

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT version FROM manifests WHERE path=?", (path,)).fetchone()
        current = row[0] if row else 0
        if base is not None and base != current:
            conn.execute("ROLLBACK")
            return err("conflict", 409, current_version=current)
        new_version = current + 1
        conn.execute(
            "INSERT OR REPLACE INTO manifests(path,size,chunks,version) VALUES(?,?,?,?)",
            (path, size, json.dumps(chunks), new_version),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return jsonify({"status": "committed", "version": new_version})


@app.get("/files")
def list_files():
    conn = db()
    rows = conn.execute("SELECT path,size,version FROM manifests ORDER BY path").fetchall()
    conn.close()
    return jsonify({"files": [{"path": p, "size": s, "version": v} for p, s, v in rows]})


@app.get("/files/manifest")
def get_manifest():
    path = request.args.get("path")
    if not valid_path(path):
        return err("invalid path", 400)
    conn = db()
    row = conn.execute(
        "SELECT size,chunks,version FROM manifests WHERE path=?", (path,)
    ).fetchone()
    conn.close()
    if not row:
        return err("not found", 404)
    return jsonify({"path": path, "size": row[0], "chunks": json.loads(row[1]), "version": row[2]})


@app.delete("/files")
def delete_file():
    path = request.args.get("path")
    if not valid_path(path):
        return err("invalid path", 400)
    conn = db()
    cur = conn.execute("DELETE FROM manifests WHERE path=?", (path,))
    conn.close()
    if cur.rowcount == 0:
        return err("not found", 404)
    return jsonify({"status": "deleted"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
