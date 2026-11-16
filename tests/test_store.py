# tests/test_store.py
"""Paths under the data root, atomic durable writes, and the write lock."""
import os
import pathlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
import store  # noqa: E402

#: The child takes the lock, says so on stdout, then holds it for HOLD_S.
CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    import store
    with store.write_lock():
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
""")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Not created yet: data_root() must create it.
        self.data = os.path.join(self.tmp, "data")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": self.data})
        patcher.start()
        self.addCleanup(patcher.stop)
        saved = store.LOCK_WAIT_S
        self.addCleanup(setattr, store, "LOCK_WAIT_S", saved)

    def _spawn_holder(self, hold_s):
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", CHILD, str(SERVER), str(hold_s)],
            stdout=subprocess.PIPE, text=True,
            env={**os.environ, "CLAUDE_PLUGIN_DATA": self.data})
        self.addCleanup(child.wait)
        self.addCleanup(child.stdout.close)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.assertEqual(child.stdout.readline().strip(), "held")
        return child

    def test_data_root_created_private(self):
        root = store.data_root()
        self.assertEqual(root, self.data)
        self.assertEqual(stat.S_IMODE(os.stat(root).st_mode), 0o700)

    def test_write_json_roundtrip_and_no_temp_left(self):
        obj = {"number": "INV-2026-0001", "total": "13000.00", "lines": [1, 2],
               "name": "Café"}
        store.write_json("invoices/INV-2026-0001.json", obj)
        self.assertEqual(store.read_json("invoices/INV-2026-0001.json"), obj)
        self.assertEqual(os.listdir(os.path.join(self.data, "invoices")),
                         ["INV-2026-0001.json"])
        # Overwrite replaces whole-file and still leaves no temp behind.
        store.write_json("invoices/INV-2026-0001.json", {"v": 2})
        self.assertEqual(store.read_json("invoices/INV-2026-0001.json"), {"v": 2})
        self.assertEqual(store.listdir("invoices"), ["INV-2026-0001.json"])

    def test_write_bytes_roundtrip(self):
        store.write_bytes("invoices/a.pdf", b"%PDF-1.4\n\x00\xff")
        with open(os.path.join(self.data, "invoices", "a.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-1.4\n\x00\xff")
        self.assertEqual(store.listdir("invoices"), ["a.pdf"])

    def test_read_helpers_on_missing(self):
        self.assertIsNone(store.read_json("nope.json"))
        self.assertEqual(store.read_json("nope.json", default={}), {})
        self.assertFalse(store.exists("nope.json"))
        self.assertEqual(store.listdir("drafts"), [])
        store.write_json("drafts/b.json", {})
        store.write_json("drafts/a.json", {})
        self.assertTrue(store.exists("drafts/a.json"))
        self.assertEqual(store.listdir("drafts"), ["a.json", "b.json"])

    def _count_fsyncs(self, fn):
        """Run `fn` with os.fsync counted; -> (files, dirs) fsynced."""
        real = os.fsync
        seen = {"file": 0, "dir": 0}

        def counting(fd):
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            seen[kind] += 1
            return real(fd)

        with mock.patch.object(store.os, "fsync", side_effect=counting):
            fn()
        return seen["file"], seen["dir"]

    def test_write_fsyncs_file_and_dir(self):
        store.data_root()
        for write in (lambda: store.write_json("company.json", {"a": 1}),
                      lambda: store.write_bytes("x.pdf", b"pdf")):
            files, dirs = self._count_fsyncs(write)
            self.assertGreaterEqual(files, 1)
            self.assertGreaterEqual(dirs, 1)
        # A write that creates its directory also fsyncs that directory's
        # parent, so the new directory entry is durable too.
        files, dirs = self._count_fsyncs(
            lambda: store.write_json("finalizing/INV-2026-0001.json", {}))
        self.assertGreaterEqual(dirs, 2)

    def test_delete_fsyncs_dir(self):
        store.write_json("drafts/d1.json", {"id": "d1"})
        files, dirs = self._count_fsyncs(lambda: store.delete("drafts/d1.json"))
        self.assertGreaterEqual(dirs, 1)
        self.assertFalse(store.exists("drafts/d1.json"))
        self.assertEqual(store.listdir("drafts"), [])
        # Deleting what is not there is not an error.
        store.delete("drafts/d1.json")

    def test_path_refuses_escape(self):
        for parts in (("..", "x"), ("/etc",), ("drafts/../../x",), ("a", "/b")):
            with self.subTest(parts=parts):
                with self.assertRaises(store.StoreError):
                    store.path(*parts)
        self.assertEqual(store.path("invoices", "a.json"),
                         os.path.join(self.data, "invoices", "a.json"))

    def test_unset_data_root_refuses(self):
        for value in (None, ""):
            with self.subTest(value=value):
                env = {k: v for k, v in os.environ.items()
                       if k != "CLAUDE_PLUGIN_DATA"}
                if value is not None:
                    env["CLAUDE_PLUGIN_DATA"] = value
                with mock.patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(store.StoreError):
                        store.data_root()
                    with self.assertRaises(store.StoreError):
                        store.write_json("company.json", {})

    def test_write_lock_excludes_other_process(self):
        self._spawn_holder(2)
        store.LOCK_WAIT_S = 0.3
        with self.assertRaises(store.StoreError) as cm:
            with store.write_lock():
                self.fail("acquired a lock another process holds")
        self.assertIn("busy", str(cm.exception))
        self.assertTrue(os.path.isfile(os.path.join(self.data, "state.lock")))

    def test_write_lock_released_on_exit(self):
        with store.write_lock(wait_s=0.2):
            pass
        with self.assertRaises(RuntimeError):
            with store.write_lock(wait_s=0.2):
                raise RuntimeError("tool failed")
        # A separate open of the lock file conflicts even within one
        # process, so this succeeds only if both blocks released it.
        with store.write_lock(wait_s=0.2):
            pass

    def test_write_lock_released_when_holder_dies(self):
        child = self._spawn_holder(60)
        store.LOCK_WAIT_S = 0.3
        with self.assertRaises(store.StoreError):
            with store.write_lock():
                pass
        child.send_signal(signal.SIGKILL)
        child.wait()
        store.LOCK_WAIT_S = 5.0
        entered = False
        with store.write_lock():
            entered = True
        self.assertTrue(entered)


if __name__ == "__main__":
    unittest.main()
