"""
Tests for client.py (chunker, hashing, manifest, and client <-> mock server).

Run from the project folder:
    python -m unittest client/test_client.py
The tests start their own mock server on a free port, so a mock server that
you started by hand on port 5000 does not interfere.
"""

import contextlib
import importlib.util
import io
import logging
import os
import re
import socket
import tempfile
import threading
import unittest
from unittest import mock

import requests

from werkzeug.serving import make_server

HERE = os.path.dirname(os.path.abspath(__file__))
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # keep test output quiet


def load_module(name, path):
    """Load a module from a file path (avoids clashes with the 'client' folder name)."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = load_module("mini_client", os.path.join(HERE, "client.py"))
mock_server = load_module("mini_mock_server", os.path.join(HERE, "..", "mock_server.py"))

MIB = client.CHUNK_SIZE


def write_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def run_client(*argv):
    """Run client.main and return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = client.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def number_after(label, text):
    return int(re.search(re.escape(label) + r"\s*(\d+)", text).group(1))


# --------------------------------------------------------------------------
# Local tests (no server needed)
# --------------------------------------------------------------------------

class ChunkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_chunk_size_is_one_mib(self):
        self.assertEqual(client.CHUNK_SIZE, 1_048_576)

    def test_multi_chunk_file_sizes(self):
        path = os.path.join(self.dir, "f.bin")
        write_file(path, os.urandom(int(2.5 * MIB)))
        sizes = [len(c) for c in client.chunk_file(path)]
        self.assertEqual(sizes, [MIB, MIB, MIB // 2])

    def test_exact_multiple_has_no_extra_chunk(self):
        path = os.path.join(self.dir, "f.bin")
        write_file(path, os.urandom(2 * MIB))
        self.assertEqual(len(list(client.chunk_file(path))), 2)

    def test_chunk_file_is_a_generator(self):
        path = os.path.join(self.dir, "f.bin")
        write_file(path, b"abc")
        self.assertTrue(hasattr(client.chunk_file(path), "__next__"))

    def test_50_mib_file_has_50_chunks(self):                       # Test A
        path = os.path.join(self.dir, "big.bin")
        with open(path, "wb") as f:
            for i in range(50):
                f.write(bytes([i]) * MIB)
        self.assertEqual(os.path.getsize(path), 50 * 1024 * 1024)
        self.assertEqual(len(list(client.chunk_file(path))), 50)
        manifest = client.build_manifest(self.dir, path)
        self.assertEqual(len(manifest["chunks"]), 50)

    def test_empty_file_has_no_chunks(self):
        path = os.path.join(self.dir, "empty.txt")
        write_file(path, b"")
        self.assertEqual(list(client.chunk_file(path)), [])


class HashTests(unittest.TestCase):
    def test_known_value(self):
        self.assertEqual(
            client.sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_deterministic_and_identical_data(self):
        self.assertEqual(client.sha256_hex(b"hello"), client.sha256_hex(b"hello"))
        self.assertNotEqual(client.sha256_hex(b"hello"), client.sha256_hex(b"hellp"))

    def test_hash_is_64_lowercase_hex(self):                        # Test D
        h = client.sha256_hex(b"anything")
        self.assertEqual(len(h), 64)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", h))
        self.assertTrue(client.is_valid_hash(h))
        self.assertFalse(client.is_valid_hash(h.upper()))


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_path_with_forward_slashes(self):
        path = os.path.join(self.dir, "docs", "a.txt")
        write_file(path, b"hello")
        manifest = client.build_manifest(self.dir, path)
        self.assertEqual(manifest["path"], "docs/a.txt")
        self.assertNotIn("\\", manifest["path"])
        self.assertFalse(os.path.isabs(manifest["path"]))

    def test_size_and_ordered_hashes(self):
        a, b = os.urandom(MIB), os.urandom(100)
        path = os.path.join(self.dir, "f.bin")
        write_file(path, a + b)
        manifest = client.build_manifest(self.dir, path)
        self.assertEqual(manifest["size"], MIB + 100)
        self.assertEqual(manifest["chunks"], [client.sha256_hex(a), client.sha256_hex(b)])

    def test_repeated_chunks_are_kept(self):
        a, b = b"A" * MIB, b"B" * MIB
        path = os.path.join(self.dir, "f.bin")
        write_file(path, a + b + a)
        manifest = client.build_manifest(self.dir, path)
        ha, hb = client.sha256_hex(a), client.sha256_hex(b)
        self.assertEqual(manifest["chunks"], [ha, hb, ha])

    def test_empty_file_manifest(self):                              # Test C
        path = os.path.join(self.dir, "empty.txt")
        write_file(path, b"")
        manifest = client.build_manifest(self.dir, path)
        self.assertEqual(manifest["size"], 0)
        self.assertEqual(manifest["chunks"], [])

    def test_identical_files_have_identical_hashes(self):           # Test B
        data = os.urandom(int(1.5 * MIB))
        p1, p2 = os.path.join(self.dir, "one.bin"), os.path.join(self.dir, "two.bin")
        write_file(p1, data)
        write_file(p2, data)
        self.assertEqual(
            client.build_manifest(self.dir, p1)["chunks"],
            client.build_manifest(self.dir, p2)["chunks"],
        )

    def test_unsafe_paths_rejected(self):
        for bad in ["../x.txt", "a/../../x", "/etc/passwd", "C:/x.txt", "", "a//b"]:
            with self.assertRaises(client.ClientError, msg=bad):
                client.normalize_path(bad)
        self.assertEqual(client.normalize_path("docs\\a.txt"), "docs/a.txt")


# --------------------------------------------------------------------------
# Client <-> mock server tests
# --------------------------------------------------------------------------

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = mock_server.create_app()
        self.server = make_server("127.0.0.1", 0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.old_server = client.SERVER
        client.SERVER = f"http://127.0.0.1:{self.server.server_port}"

        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "folder1")
        self.dst = os.path.join(self.tmp.name, "folder2")

    def tearDown(self):
        client.SERVER = self.old_server
        self.server.shutdown()
        self.thread.join()
        self.tmp.cleanup()

    def make_sample_folder(self):
        dup = os.urandom(int(2.5 * MIB))          # 3 distinct chunks
        write_file(os.path.join(self.src, "small.txt"), b"small file")
        write_file(os.path.join(self.src, "docs", "a.txt"), b"docs a file")
        write_file(os.path.join(self.src, "empty.txt"), b"")
        write_file(os.path.join(self.src, "dup1.txt"), dup)
        write_file(os.path.join(self.src, "dup2.txt"), dup)   # same content

    def test_sync_twice_then_pull(self):
        self.make_sample_folder()

        code, out, _ = run_client("sync", self.src)
        self.assertEqual(code, 0, out)
        self.assertIn("docs/a.txt: uploaded 1 of 1 chunks", out)
        self.assertIn("empty.txt: uploaded 0 of 0 chunks", out)
        self.assertEqual(number_after("Files scanned:", out), 5)
        self.assertEqual(number_after("Chunks total:", out), 8)       # 1+1+0+3+3
        self.assertEqual(number_after("Chunks uploaded:", out), 5)    # 1+1+3 distinct
        self.assertEqual(number_after("Chunks skipped:", out), 3)
        self.assertEqual(self.app.stats["chunk_puts"], 5)
        self.assertEqual(len(self.app.chunks), 5)

        # Second sync: server already has everything -> 0 uploads.
        code, out, _ = run_client("sync", self.src)
        self.assertEqual(code, 0)
        self.assertEqual(number_after("Chunks uploaded:", out), 0)
        self.assertEqual(number_after("Chunks skipped:", out), 8)
        self.assertEqual(self.app.stats["chunk_puts"], 5)

        # Pull into a new folder and compare byte for byte.
        code, out, _ = run_client("pull", self.dst)
        self.assertEqual(code, 0, out)
        self.assertEqual(number_after("Files processed:", out), 5)
        self.assertEqual(number_after("Files downloaded:", out), 5)
        self.assertEqual(number_after("Files skipped:", out), 0)
        for rel in ["small.txt", "docs/a.txt", "empty.txt", "dup1.txt", "dup2.txt"]:
            self.assertEqual(
                read_file(os.path.join(self.src, *rel.split("/"))),
                read_file(os.path.join(self.dst, *rel.split("/"))),
                rel,
            )
        self.assertEqual(os.path.getsize(os.path.join(self.dst, "empty.txt")), 0)

        # Pulling again skips identical files.
        code, out, _ = run_client("pull", self.dst)
        self.assertEqual(code, 0)
        self.assertEqual(number_after("Files skipped:", out), 5)
        self.assertIn("already identical, skipped", out)

    def test_duplicate_content_files_store_chunks_once(self):
        data = os.urandom(MIB)
        write_file(os.path.join(self.src, "file1.txt"), data)
        write_file(os.path.join(self.src, "file2.txt"), data)
        code, out, _ = run_client("sync", self.src)
        self.assertEqual(code, 0)
        self.assertEqual(self.app.stats["chunk_puts"], 1)
        self.assertEqual(len(self.app.chunks), 1)
        self.assertEqual(number_after("Chunks total:", out), 2)
        self.assertEqual(number_after("Chunks uploaded:", out), 1)

    def test_repeated_chunk_inside_one_file_uploaded_once(self):
        a, b, c = b"A" * MIB, b"B" * MIB, b"C" * MIB
        write_file(os.path.join(self.src, "abac.bin"), a + b + a + c + a)
        code, out, _ = run_client("sync", self.src)
        self.assertEqual(code, 0)
        self.assertEqual(self.app.stats["chunk_puts"], 3)             # A once, not 3 times
        self.assertEqual(len(self.app.manifests["abac.bin"]["chunks"]), 5)
        self.assertIn("abac.bin: uploaded 3 of 5 chunks", out)

        code, _, _ = run_client("pull", self.dst)
        self.assertEqual(code, 0)
        self.assertEqual(
            read_file(os.path.join(self.dst, "abac.bin")), a + b + a + c + a
        )

    def test_commit_with_missing_chunk_is_reported(self):
        manifest = {"path": "x.txt", "size": 5, "chunks": ["a" * 64]}
        with self.assertRaises(client.ClientError) as ctx:
            client.commit_manifest(manifest)
        self.assertIn("missing chunks", str(ctx.exception))
        self.assertIn("a" * 64, str(ctx.exception))

    def test_corrupted_chunk_detected_on_pull(self):
        write_file(os.path.join(self.src, "f.txt"), b"important data")
        run_client("sync", self.src)
        for h in list(self.app.chunks):
            self.app.chunks[h] = b"corrupted!!!!!"
        code, _, err = run_client("pull", self.dst)
        self.assertEqual(code, 1)
        self.assertIn("chunk hash mismatch", err)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "f.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "f.txt.part")))

    def test_path_traversal_blocked_on_pull(self):
        data = b"evil"
        h = client.sha256_hex(data)
        self.app.chunks[h] = data
        self.app.manifests["../evil.txt"] = {"path": "../evil.txt", "size": 4, "chunks": [h]}
        code, _, err = run_client("pull", self.dst)
        self.assertEqual(code, 1)
        self.assertIn("unsafe path", err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "evil.txt")))

    def test_server_down_gives_clean_error(self):
        write_file(os.path.join(self.src, "f.txt"), b"data")
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]      # nothing listens here
        client.SERVER = f"http://127.0.0.1:{free_port}"
        code, _, err = run_client("sync", self.src)
        self.assertEqual(code, 1)
        self.assertIn("Could not connect to server", err)
        self.assertIn("Make sure the server is running", err)
        self.assertNotIn("Traceback", err)

    def test_bad_folder_arguments(self):
        code, _, err = run_client("sync", os.path.join(self.tmp.name, "nope"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        file_path = os.path.join(self.tmp.name, "afile")
        write_file(file_path, b"x")
        code, _, err = run_client("sync", file_path)
        self.assertEqual(code, 1)
        self.assertIn("not a folder", err)


    def test_missing_request_has_unique_hashes_and_manifest_is_last(self):
        a, b, c = b"A" * MIB, b"B" * MIB, b"C" * MIB
        write_file(os.path.join(self.src, "abac.bin"), a + b + a + c + a)
        code, _, _ = run_client("sync", self.src)
        self.assertEqual(code, 0)
        ha, hb, hc = (client.sha256_hex(x) for x in (a, b, c))
        # One /chunks/missing request, each hash once (A B C, not A B A C A).
        self.assertEqual(self.app.missing_requests, [[ha, hb, hc]])
        # Exactly PUT A, PUT B, PUT C - then the manifest commit comes last.
        self.assertEqual(
            self.app.events,
            [("put_chunk", ha), ("put_chunk", hb), ("put_chunk", hc),
             ("commit", "abac.bin")],
        )
        # The manifest itself keeps the repeats.
        self.assertEqual(self.app.manifests["abac.bin"]["chunks"], [ha, hb, ha, hc, ha])

    def test_second_sync_sends_no_chunk_puts(self):
        write_file(os.path.join(self.src, "f.bin"), os.urandom(int(1.5 * MIB)))
        run_client("sync", self.src)
        puts_before = self.app.stats["chunk_puts"]
        code, out, _ = run_client("sync", self.src)
        self.assertEqual(code, 0)
        self.assertEqual(number_after("Chunks uploaded:", out), 0)
        self.assertEqual(self.app.stats["chunk_puts"], puts_before)

    def test_size_mismatch_detected_on_pull(self):
        write_file(os.path.join(self.src, "f.txt"), b"important data")   # 14 bytes
        run_client("sync", self.src)
        # Server now claims 20 bytes although the one chunk holds only 14.
        # (20 still fits the "1 chunk" range, so only the final size check can catch it.)
        self.app.manifests["f.txt"]["size"] = 20
        code, out, err = run_client("pull", self.dst)
        self.assertEqual(code, 1)
        self.assertIn("size mismatch for f.txt", err)
        self.assertIn("Expected: 20", err)
        self.assertIn("Actual: 14", err)
        self.assertNotIn("Pulled: f.txt", out)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "f.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "f.txt.part")))

    def test_pull_does_not_skip_file_that_only_has_same_name(self):
        write_file(os.path.join(self.src, "f.txt"), b"server version")
        run_client("sync", self.src)
        write_file(os.path.join(self.dst, "f.txt"), b"different text")   # same name AND size, other content
        code, out, _ = run_client("pull", self.dst)
        self.assertEqual(code, 0)
        self.assertNotIn("already identical", out)
        self.assertEqual(read_file(os.path.join(self.dst, "f.txt")), b"server version")

    def test_empty_file_survives_sync_and_pull(self):
        write_file(os.path.join(self.src, "empty.txt"), b"")
        code, out, _ = run_client("sync", self.src)
        self.assertEqual(code, 0)
        self.assertIn("empty.txt: uploaded 0 of 0 chunks", out)
        self.assertEqual(self.app.manifests["empty.txt"]["chunks"], [])
        code, _, _ = run_client("pull", self.dst)
        self.assertEqual(code, 0)
        self.assertEqual(os.path.getsize(os.path.join(self.dst, "empty.txt")), 0)

    def test_nested_paths_use_forward_slashes_on_server(self):
        write_file(os.path.join(self.src, "docs", "deep", "x.txt"), b"x")
        run_client("sync", self.src)
        self.assertIn("docs/deep/x.txt", self.app.manifests)


class ErrorHandlingTests(unittest.TestCase):
    """HTTP problems must give a readable message, never a traceback."""

    class FakeResponse:
        def __init__(self, status, body=None, text=""):
            self.status_code, self._body, self.text = status, body, text

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    def call(self, response=None, exc=None):
        with mock.patch.object(client.requests, "request",
                               return_value=response, side_effect=exc):
            return client.api_json("GET", "/files")

    def message(self, **kw):
        with self.assertRaises(client.ClientError) as ctx:
            self.call(**kw)
        return str(ctx.exception)

    def test_http_400_shows_endpoint_status_error_and_missing(self):
        msg = self.message(response=self.FakeResponse(
            400, {"error": "missing chunks", "missing": ["h" * 5]}))
        self.assertIn("GET /files", msg)
        self.assertIn("400", msg)
        self.assertIn("missing chunks", msg)
        self.assertIn("hhhhh", msg)

    def test_http_404(self):
        msg = self.message(response=self.FakeResponse(404, {"error": "not found"}))
        self.assertIn("not found (404)", msg)

    def test_http_409_does_not_crash(self):
        msg = self.message(response=self.FakeResponse(409, {"error": "conflict"}))
        self.assertIn("409", msg)
        self.assertIn("conflict", msg)

    def test_http_500(self):
        msg = self.message(response=self.FakeResponse(500, None, "<html>boom</html>"))
        self.assertIn("internal error (500)", msg)
        self.assertNotIn("<html>", msg)

    def test_invalid_json(self):
        class Ok(self.FakeResponse):
            pass
        msg = self.message(response=Ok(200, None, "not json"))
        self.assertIn("invalid JSON", msg)

    def test_timeout_message(self):
        msg = self.message(exc=requests.exceptions.ReadTimeout())
        self.assertIn("Timeout", msg)

    def test_default_server_is_the_real_one(self):
        self.assertEqual(client.DEFAULT_SERVER, "http://localhost:5000")


if __name__ == "__main__":
    unittest.main()
