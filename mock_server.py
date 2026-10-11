"""
Small in-memory mock of the Mini-Dropbox server, for testing the client.

Run:  python mock_server.py      (listens on http://localhost:5000)
Data is kept in Python dictionaries and is lost when the server stops.

It implements the SAME API and the same core validation as server.py
(hash format, hash match, 1 MiB limit, path rules, missing chunks, size
check, versions) so the client can be tested without the real server.
"""

import hashlib
import re

from flask import Flask, Response, jsonify, request

MAX_CHUNK = 1_048_576
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DRIVE_RE = re.compile(r"^[A-Za-z]:")


def valid_hash(h):
    return isinstance(h, str) and HASH_RE.match(h) is not None


def valid_path(p):
    if not isinstance(p, str) or not p:
        return False
    if p.startswith("/") or "\\" in p or "\x00" in p or DRIVE_RE.match(p):
        return False
    return all(seg not in ("", ".", "..") for seg in p.split("/"))


def create_app():
    app = Flask(__name__)
    chunks = {}      # hash -> raw bytes
    manifests = {}   # path -> manifest dict (path, size, chunks, version)
    app.chunks = chunks
    app.manifests = manifests
    # Lets tests inspect what the client did:
    app.stats = {"chunk_puts": 0}
    app.missing_requests = []   # list of hash lists sent to /chunks/missing
    app.events = []             # ordered log: ("put_chunk", h) / ("commit", path)

    def error(message, code, **extra):
        return jsonify(error=message, **extra), code

    @app.post("/chunks/missing")
    def chunks_missing():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("hashes"), list):
            return error("expected {'hashes': [...]}", 400)
        if not all(valid_hash(h) for h in body["hashes"]):
            return error("invalid hash", 400)
        app.missing_requests.append(list(body["hashes"]))
        missing = [h for h in dict.fromkeys(body["hashes"]) if h not in chunks]
        return jsonify(missing=missing)

    @app.put("/chunks/<chunk_hash>")
    def put_chunk(chunk_hash):
        if not valid_hash(chunk_hash):
            return error("invalid hash", 400)
        data = request.get_data()
        if len(data) > MAX_CHUNK:
            return error("chunk too large", 400)
        if hashlib.sha256(data).hexdigest() != chunk_hash:
            return error("hash mismatch", 400)
        app.stats["chunk_puts"] += 1
        app.events.append(("put_chunk", chunk_hash))
        status = "exists" if chunk_hash in chunks else "stored"
        chunks[chunk_hash] = data
        return jsonify(status=status)

    @app.get("/chunks/<chunk_hash>")
    def get_chunk(chunk_hash):
        if chunk_hash not in chunks:
            return error("not found", 404)
        return Response(chunks[chunk_hash], mimetype="application/octet-stream")

    @app.put("/files")
    def commit_file():
        m = request.get_json(silent=True)
        if not isinstance(m, dict):
            return error("expected JSON manifest", 400)
        path, size, chunk_list = m.get("path"), m.get("size"), m.get("chunks")
        base = m.get("base_version")
        if not valid_path(path):
            return error("invalid path", 400)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return error("invalid size", 400)
        if not isinstance(chunk_list, list) or not all(valid_hash(h) for h in chunk_list):
            return error("invalid chunks", 400)
        missing = [h for h in dict.fromkeys(chunk_list) if h not in chunks]
        if missing:
            return error("missing chunks", 400, missing=missing)
        if sum(len(chunks[h]) for h in chunk_list) != size:
            return error("size mismatch", 400)
        current = manifests[path]["version"] if path in manifests else 0
        if base is not None and base != current:
            return error("conflict", 409, current_version=current)
        manifests[path] = {"path": path, "size": size,
                           "chunks": chunk_list, "version": current + 1}
        app.events.append(("commit", path))
        return jsonify(status="committed", version=current + 1)

    @app.get("/files")
    def list_files():
        return jsonify(files=[
            {"path": p, "size": m["size"], "version": m.get("version", 1)}
            for p, m in sorted(manifests.items())
        ])

    @app.get("/files/manifest")
    def get_manifest():
        path = request.args.get("path", "")
        if not valid_path(path):
            return error("invalid path", 400)
        if path not in manifests:
            return error("not found", 404)
        return jsonify(manifests[path])

    @app.delete("/files")
    def delete_file():
        path = request.args.get("path", "")
        if not valid_path(path):
            return error("invalid path", 400)
        if manifests.pop(path, None) is None:
            return error("not found", 404)
        return jsonify(status="deleted")

    return app


if __name__ == "__main__":
    create_app().run(host="localhost", port=5000)
