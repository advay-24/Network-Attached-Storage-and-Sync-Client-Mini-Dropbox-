#!/usr/bin/env python3
"""
Mini-Dropbox client (Member B: client and sync logic).

Commands:
    python client.py sync FOLDER    upload a folder to the server
    python client.py pull FOLDER    download everything from the server into FOLDER

Core idea (useful for the viva):
    file -> 1 MiB chunks -> SHA-256 of each chunk -> ask the server which
    hashes it is missing -> upload only those -> commit the manifest last.

VIVA NOTES
----------
A. Why chunks are named by their hash
   The SHA-256 hash is the chunk's content identity: same content -> same hash.
   So the server can keep exactly one copy of identical chunks (deduplication),
   even when they come from different files or repeat inside one file.

B. Why /chunks/missing is called first
   The client asks "which of these hashes do you NOT have?" and uploads only
   those. Chunks the server already has never cross the network again.

C. Why the manifest is committed last
   The manifest (path, size, ordered chunk hashes) points at chunks. If it were
   committed before all chunks existed, the server would hold a file that
   references data it does not have. Order is therefore:
       chunk -> hash -> missing check -> upload -> commit manifest

D. Why the second sync uploads 0 chunks
   After the first sync the server has every chunk hash, so /chunks/missing
   returns an empty list and no chunk PUT requests are needed. No local state
   is faked: the answer always comes from the server.
"""

import argparse
import hashlib
import os
import sys

import requests

# The ONLY place the server address is written. Change it here.
# (Optional overrides without editing code: set the MINI_DROPBOX_SERVER
#  environment variable, or pass --server URL on the command line.)
DEFAULT_SERVER = "http://localhost:5000"      # the team's real server
SERVER = os.environ.get("MINI_DROPBOX_SERVER", DEFAULT_SERVER).rstrip("/")
TIMEOUT = 10                 # seconds for every HTTP request
CHUNK_SIZE = 1_048_576       # exactly 1 MiB (not 1,000,000)

HEX_DIGITS = set("0123456789abcdef")


class ClientError(Exception):
    """A problem with one file or one request; the message is user-readable."""


class ServerUnavailable(ClientError):
    """The server cannot be reached; there is no point continuing."""


# --------------------------------------------------------------------------
# Task 1: chunking, hashing, manifest
# --------------------------------------------------------------------------

def chunk_file(path):
    """Yield the file in CHUNK_SIZE pieces. Never loads the whole file."""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


def sha256_hex(data):
    """Return the SHA-256 of data as 64 lowercase hex characters."""
    return hashlib.sha256(data).hexdigest()


def is_valid_hash(value):
    """True if value looks like a lowercase hex SHA-256 string."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in HEX_DIGITS for ch in value)
    )


def normalize_path(path):
    """
    Check and clean a relative path such as 'docs/a.txt'.
    Rejects absolute paths and anything containing '..' (path traversal).
    """
    if not isinstance(path, str) or not path:
        raise ClientError(f"invalid path: {path!r}")
    cleaned = path.replace("\\", "/")
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        raise ClientError(f"absolute paths are not allowed: {path!r}")
    parts = cleaned.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ClientError(f"unsafe path rejected: {path!r}")
    return "/".join(parts)


def build_manifest(root, filepath):
    """
    Return {"path": relative/path, "size": N, "chunks": [hash, ...]}.
    Chunk hashes stay in file order and repeated hashes are kept.
    """
    rel = os.path.relpath(filepath, root).replace("\\", "/")
    rel = normalize_path(rel)
    size = os.path.getsize(filepath)

    hashes = []
    bytes_read = 0
    for chunk in chunk_file(filepath):
        bytes_read += len(chunk)
        hashes.append(sha256_hex(chunk))

    if bytes_read != size:
        raise ClientError(f"{rel} changed while it was being read")
    return {"path": rel, "size": size, "chunks": hashes}


# --------------------------------------------------------------------------
# HTTP helpers (every request goes through api_request)
# --------------------------------------------------------------------------

def describe_error(response):
    """Turn an error response into a short readable message."""
    try:
        body = response.json()
    except ValueError:
        body = None

    if isinstance(body, dict):
        lines = []
        if "error" in body:
            lines.append(f"Server error: {body['error']}")
        if body.get("missing"):
            lines.append(f"Server reports missing chunks: {body['missing']}")
        if lines:
            return "\n  ".join(lines)

    text = response.text.strip()
    if not text or text.startswith("<"):
        return "(no readable error message from server)"
    return text[:200]


def status_kind(code):
    if code == 400:
        return "bad request (400)"
    if code == 404:
        return "not found (404)"
    if code == 409:
        return "conflict (409): the file changed on the server"
    if code >= 500:
        return f"server returned an internal error ({code})"
    return f"HTTP error ({code})"


def api_request(method, endpoint, **kwargs):
    """
    Send one request to SERVER + endpoint and check the status code.
    Returns the response only for 2xx; otherwise raises ClientError.
    """
    url = SERVER + endpoint
    try:
        response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.ConnectionError:
        raise ServerUnavailable(
            f"ERROR: Could not connect to server at {SERVER}\n"
            "Make sure the server is running."
        )
    except requests.Timeout:
        raise ServerUnavailable(
            f"ERROR: Timeout - server at {SERVER} did not respond within "
            f"{TIMEOUT} seconds."
        )
    except requests.RequestException as exc:
        raise ClientError(f"{method} {endpoint} failed: {exc}")

    if not 200 <= response.status_code < 300:
        raise ClientError(
            f"{method} {endpoint} failed: {status_kind(response.status_code)}\n"
            f"  {describe_error(response)}"
        )
    return response


def api_json(method, endpoint, **kwargs):
    """Like api_request, but also parses the JSON object in the reply."""
    response = api_request(method, endpoint, **kwargs)
    try:
        data = response.json()
    except ValueError:
        raise ClientError(f"{method} {endpoint}: server sent invalid JSON")
    if not isinstance(data, dict):
        raise ClientError(f"{method} {endpoint}: unexpected JSON from server")
    return data


def fetch_missing(hashes):
    """Ask the server which of these hashes it does not have (unique list)."""
    data = api_json("POST", "/chunks/missing", json={"hashes": hashes})
    missing = data.get("missing")
    if not isinstance(missing, list) or not all(isinstance(h, str) for h in missing):
        raise ClientError("POST /chunks/missing: reply has no valid 'missing' list")
    return set(missing)


def upload_chunk(chunk_hash, data):
    try:
        reply = api_json(
            "PUT",
            f"/chunks/{chunk_hash}",
            data=data,
            headers={"Content-Type": "application/octet-stream"},
        )
    except ServerUnavailable:
        raise
    except ClientError as exc:
        raise ClientError(f"ERROR: upload of chunk {chunk_hash} failed\n  {exc}")
    if reply.get("status") not in ("stored", "exists"):
        raise ClientError(
            f"ERROR: upload of chunk {chunk_hash}: unexpected reply {reply}"
        )


def download_chunk(chunk_hash):
    return api_request("GET", f"/chunks/{chunk_hash}").content


def commit_manifest(manifest):
    try:
        reply = api_json("PUT", "/files", json=manifest)
    except ServerUnavailable:
        raise
    except ClientError as exc:
        raise ClientError(f"ERROR committing {manifest['path']}:\n  {exc}")
    if reply.get("status") != "committed":
        raise ClientError(
            f"ERROR committing {manifest['path']}:\n  unexpected reply {reply}"
        )


def show_error(exc):
    text = str(exc)
    if not text.startswith("ERROR"):
        text = "ERROR: " + text
    print(text, file=sys.stderr)


# --------------------------------------------------------------------------
# Task 2: sync
# --------------------------------------------------------------------------

def sync_file(root, filepath, stats):
    """Upload one file's missing chunks, then commit its manifest."""
    manifest = build_manifest(root, filepath)
    path = manifest["path"]
    hashes = manifest["chunks"]
    stats["total"] += len(hashes)
    uploaded_here = 0

    if hashes:
        # Manifest keeps repeated hashes (A B A C A) but the request must
        # contain each hash once (A B C).
        unique = list(dict.fromkeys(hashes))
        wanted = set(unique)

        # Viva B: ask the server first, so only needed chunks are uploaded.
        missing = fetch_missing(unique) & wanted

        if missing:
            uploaded_hashes = set()   # a repeated chunk is uploaded only once
            # Re-read the file chunk by chunk (never the whole file in memory).
            for chunk in chunk_file(filepath):
                chunk_hash = sha256_hex(chunk)
                if chunk_hash not in wanted:
                    raise ClientError(f"ERROR: {path} changed during sync")
                if chunk_hash in missing and chunk_hash not in uploaded_hashes:
                    upload_chunk(chunk_hash, chunk)
                    uploaded_hashes.add(chunk_hash)
                    stats["uploaded"] += 1
                    uploaded_here += 1
            if missing - uploaded_hashes:
                raise ClientError(f"ERROR: {path} changed during sync")

    # Viva C: commit the manifest LAST, after every needed chunk is on the
    # server, so the server never holds a file pointing at missing chunks.
    commit_manifest(manifest)
    print(f"{path}: uploaded {uploaded_here} of {len(hashes)} chunks")


def sync_folder(folder):
    if not os.path.exists(folder):
        raise ClientError(f"folder does not exist: {folder}")
    if not os.path.isdir(folder):
        raise ClientError(f"not a folder: {folder}")

    stats = {"files": 0, "total": 0, "uploaded": 0, "failed": 0}
    print(f"Scanning: {folder}\n")

    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames.sort()                       # deterministic demo output
        for name in sorted(filenames):
            filepath = os.path.join(dirpath, name)
            if not os.path.isfile(filepath):
                continue
            stats["files"] += 1
            try:
                sync_file(folder, filepath, stats)
            except ServerUnavailable:
                raise
            except (ClientError, OSError) as exc:
                stats["failed"] += 1
                show_error(exc)

    skipped = stats["total"] - stats["uploaded"]
    print("\nSync complete")
    print("------------")
    print(f"Files scanned: {stats['files']}")
    print(f"Chunks total: {stats['total']}")
    print(f"Chunks uploaded: {stats['uploaded']}")
    print(f"Chunks skipped: {skipped}")
    if stats["failed"]:
        print(f"Files failed: {stats['failed']}")
        return 1
    return 0


# --------------------------------------------------------------------------
# Task 3: pull
# --------------------------------------------------------------------------

def validate_manifest(manifest, expected_path):
    """Basic sanity checks on a manifest received from the server."""
    problem = None
    if manifest.get("path") != expected_path:
        problem = "path does not match the request"
    elif (not isinstance(manifest.get("size"), int)
          or isinstance(manifest.get("size"), bool)
          or manifest["size"] < 0):
        problem = "invalid size"
    elif not isinstance(manifest.get("chunks"), list):
        problem = "'chunks' is not a list"
    elif not all(is_valid_hash(h) for h in manifest["chunks"]):
        problem = "contains an invalid chunk hash"
    else:
        count, size = len(manifest["chunks"]), manifest["size"]
        if count == 0 and size != 0:
            problem = "size is not 0 but there are no chunks"
        elif count > 0 and not ((count - 1) * CHUNK_SIZE < size <= count * CHUNK_SIZE):
            problem = "size does not match the number of chunks"
    if problem:
        raise ClientError(f"malformed manifest for {expected_path}: {problem}")


def safe_destination(dest_root, path):
    """Local file path for a server path; must stay inside dest_root."""
    root = os.path.realpath(dest_root)
    target = os.path.realpath(os.path.join(root, *path.split("/")))
    if target == root or os.path.commonpath([root, target]) != root:
        raise ClientError(f"refusing to write outside the destination: {path}")
    return target


def already_identical(dest, manifest):
    """True if the local file already matches the manifest exactly."""
    try:
        if not os.path.isfile(dest) or os.path.getsize(dest) != manifest["size"]:
            return False
        local = [sha256_hex(chunk) for chunk in chunk_file(dest)]
    except OSError:
        return False
    return local == manifest["chunks"]


def pull_file(dest_root, path):
    """Download one file. Returns 'downloaded' or 'skipped'."""
    path = normalize_path(path)
    manifest = api_json("GET", "/files/manifest", params={"path": path})
    validate_manifest(manifest, path)
    dest = safe_destination(dest_root, path)

    if already_identical(dest, manifest):
        print(f"{path}: already identical, skipped")
        return "skipped"

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    temp = dest + ".part"        # written fully and verified before renaming
    try:
        with open(temp, "wb") as out:
            for expected in manifest["chunks"]:          # exact manifest order
                data = download_chunk(expected)
                actual = sha256_hex(data)
                if actual != expected:
                    raise ClientError(
                        f"ERROR: chunk hash mismatch for {path}\n"
                        f"Expected: {expected}\nReceived: {actual}"
                    )
                out.write(data)                          # write incrementally

        actual_size = os.path.getsize(temp)
        if actual_size != manifest["size"]:
            raise ClientError(
                f"ERROR: size mismatch for {path}\n"
                f"Expected: {manifest['size']}\nActual: {actual_size}"
            )
        os.replace(temp, dest)
    except BaseException:
        if os.path.exists(temp):
            os.remove(temp)      # never leave corrupted or partial data behind
        raise

    print(f"Pulled: {path}")
    return "downloaded"


def pull_folder(folder):
    if os.path.exists(folder) and not os.path.isdir(folder):
        raise ClientError(f"not a folder: {folder}")
    os.makedirs(folder, exist_ok=True)

    listing = api_json("GET", "/files")
    files = listing.get("files")
    if not isinstance(files, list):
        raise ClientError("GET /files: reply has no valid 'files' list")

    stats = {"processed": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    for entry in files:
        stats["processed"] += 1
        try:
            if not isinstance(entry, dict) or "path" not in entry:
                raise ClientError(f"malformed file entry from server: {entry!r}")
            result = pull_file(folder, entry["path"])
            stats[result] += 1
        except ServerUnavailable:
            raise
        except (ClientError, OSError) as exc:
            stats["failed"] += 1
            show_error(exc)

    print("\nPull complete")
    print("-------------")
    print(f"Files processed: {stats['processed']}")
    print(f"Files downloaded: {stats['downloaded']}")
    print(f"Files skipped: {stats['skipped']}")
    if stats["failed"]:
        print(f"Files failed: {stats['failed']}")
        return 1
    return 0


# --------------------------------------------------------------------------
# Task 4: command line
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="client.py",
        description="Mini-Dropbox client: sync a folder to the server or pull it back.",
    )
    parser.add_argument(
        "--server", metavar="URL", default=None,
        help=f"server address (default: {SERVER})",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{sync,pull}")
    sync_p = sub.add_parser("sync", help="upload FOLDER (only missing chunks)")
    sync_p.add_argument("folder", metavar="FOLDER", help="folder to upload")
    pull_p = sub.add_parser("pull", help="download all server files into FOLDER")
    pull_p.add_argument("folder", metavar="FOLDER", help="destination folder")
    return parser


def main(argv=None):
    global SERVER
    args = build_parser().parse_args(argv)
    if args.server:
        SERVER = args.server.rstrip("/")
    try:
        if args.command == "sync":
            return sync_folder(args.folder)
        return pull_folder(args.folder)
    except ClientError as exc:          # includes ServerUnavailable
        show_error(exc)
        return 1
    except OSError as exc:
        print(f"ERROR: file system problem: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
