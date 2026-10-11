# Mini-Dropbox (CN Project)

A Dropbox-style file synchronisation system built from:

- **1 MiB chunks** (`1_048_576` bytes; the last chunk may be smaller)
- **SHA-256** hashes (64 lowercase hex characters) as chunk identity
- **content-addressed chunk storage** on the server (`storage/chunks/<hash>`)
- **manifests** (path, size, ordered chunk hashes) stored in SQLite
- **deduplication** (a chunk with a given hash is stored and uploaded only once)

| Member | Part | Files |
|---|---|---|
| A | Flask server | `server.py`, `test_server.py` |
| B | Python client | `client/client.py`, `client/test_client.py` |
| B | Mock server for client testing | `mock_server.py` |

## Architecture

```text
Folder 1 --sync--> Client --HTTP--> Server (chunk store + manifest store) --HTTP--> Client --pull--> Folder 2
```

Sync pipeline (client side):

```text
file -> 1 MiB chunks -> SHA-256 per chunk -> POST /chunks/missing (unique hashes)
     -> PUT only the missing chunks (each once) -> PUT /files (manifest, LAST)
```

## Project layout

```text
CNProject/
├── server.py          real Flask server (Member A)
├── test_server.py     contract tests for any running server
├── mock_server.py     in-memory server with the same API (for client tests)
├── requirements.txt
├── README.md
└── client/
    ├── client.py      sync / pull client (Member B)
    ├── test_client.py unit + integration tests (start their own mock server)
    └── test_folder/   small.txt, empty.txt, docs/a.txt (demo data)
```

## Installation

```bash
pip install -r requirements.txt
```

## Server address

The client's default server is the team server:

```text
http://172.16.242.33:5000
```

It is defined **once**, as `DEFAULT_SERVER` / `SERVER` at the top of `client/client.py`.
To use another server without editing code:

```bash
# Linux / macOS
MINI_DROPBOX_SERVER=http://localhost:5000 python client/client.py sync ./client/test_folder
# Windows PowerShell
$env:MINI_DROPBOX_SERVER="http://localhost:5000"
# or, on any OS, per command
python client/client.py --server http://localhost:5000 sync ./client/test_folder
```

## Running the real server

The team server may already be running at `http://172.16.242.33:5000`. To run your own:

```bash
python server.py
```

It listens on `0.0.0.0:5000` (set `PORT` to change it, `STORAGE_DIR` to move the data;
delete the `storage/` folder to reset).

## Running the client

```bash
python client/client.py sync ./client/test_folder
python client/client.py sync ./client/test_folder      # 2nd time: Chunks uploaded: 0
python client/client.py pull ./folder2
python client/client.py --help
```

## Running the mock server

```bash
python mock_server.py          # http://localhost:5000, data kept in memory
python client/client.py --server http://localhost:5000 sync ./client/test_folder
```

## Running the tests

Client tests (need no server; they start their own mock server on a free port):

```bash
python -m unittest client/test_client.py
```

Server tests (server must be running; they clean up after themselves and are safe on the shared server):

```bash
python test_server.py                              # real team server
python test_server.py http://localhost:5000        # your own server
```

## API

| Method | Endpoint | Purpose | Success | Errors |
|---|---|---|---|---|
| POST | `/chunks/missing` | body `{"hashes": [...]}` -> hashes the server lacks | `{"missing": [...]}` | 400 |
| PUT | `/chunks/<hash>` | raw bytes (`application/octet-stream`); server re-hashes | `{"status": "stored"}` or `{"status": "exists"}` | 400 `{"error": "hash mismatch"}` |
| GET | `/chunks/<hash>` | download raw bytes | 200 + bytes | 404 `{"error": "not found"}` |
| PUT | `/files` | commit manifest `{path, size, chunks}` | `{"status": "committed", "version": n}` | 400 `{"error": "missing chunks", "missing": [...]}`, 400 size mismatch / invalid path |
| GET | `/files` | list files | `{"files": [{path, size, version}]}` | |
| GET | `/files/manifest?path=docs/a.txt` | get one manifest | `{path, size, chunks, version}` | 404 |

(The server also supports `DELETE /files?path=...` and an optional `base_version` for conflict detection (409); the client does not need them.)

Paths: relative, `/` separators, no leading `/`, no `..`, no drive letters; the client converts `\` to `/`.

## Demo sequence

```bash
python server.py                                   # skip if using 172.16.242.33
python client/client.py sync ./client/test_folder  # first sync: chunks uploaded > 0
python client/client.py sync ./client/test_folder  # second sync: Chunks uploaded: 0
python client/client.py pull ./folder2             # rebuild into another folder
diff -r client/test_folder folder2                 # Windows: fc / Compare-Object; no output = identical
```

## Viva explanation

1. **Why chunks are identified by hash.** The SHA-256 of the content *is* its name: same content gives the same hash, so identical chunks (in one file, across files, across syncs) map to one stored copy. It also lets anyone check integrity.
2. **Why `/chunks/missing` comes first.** The client sends only hashes (tiny) and the server answers which it lacks, so data the server already has never crosses the network.
3. **Why repeated chunks are uploaded once.** For a file `A B A C A` the manifest stays `[A,B,A,C,A]` (needed to rebuild it), but the client sends unique hashes `[A,B,C]` to `/chunks/missing` and does `PUT A, PUT B, PUT C` only.
4. **Why the manifest is committed last.** The manifest points at chunks. Committing it first could leave a file on the server that references chunks that do not exist. The server also rejects such a manifest (400 `missing chunks`).
5. **Why the second sync uploads zero chunks.** The server now has every hash, so `/chunks/missing` returns `[]`. No local state is used; the answer always comes from the server.
6. **How pull rebuilds a file.** `GET /files` -> for each file `GET /files/manifest?path=...` -> `GET /chunks/<hash>` for each hash in manifest order, writing each chunk straight to a `.part` file (never the whole file in memory) -> verify -> rename to the final name.
7. **How corruption is detected.** Every downloaded chunk is re-hashed and compared with the requested hash; the final file size is compared with the manifest size. On any mismatch an error is printed and the partial file is deleted. The server likewise re-hashes every uploaded chunk and checks the total size on commit.
