import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from stacks.coordinator import database  # noqa: E402
from stacks.coordinator.queue_ops import QueueOperations  # noqa: E402


class IdleIoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_database_path = database.DATABASE_PATH
        self.original_runtime_path = database.RUNTIME_PATH
        self.original_heartbeat_path = database.HEARTBEAT_DATABASE_PATH
        database.DATABASE_PATH = root / "config" / "queue.db"
        database.RUNTIME_PATH = root / "runtime"
        database.HEARTBEAT_DATABASE_PATH = database.RUNTIME_PATH / "heartbeats.db"
        database.init_database()
        database.startup_cleanup()

    def tearDown(self):
        database.DATABASE_PATH = self.original_database_path
        database.RUNTIME_PATH = self.original_runtime_path
        database.HEARTBEAT_DATABASE_PATH = self.original_heartbeat_path
        self.temp_dir.cleanup()

    def test_idle_polling_does_not_modify_persistent_queue_database(self):
        before_hash = hashlib.sha256(database.DATABASE_PATH.read_bytes()).digest()
        before_mtime = database.DATABASE_PATH.stat().st_mtime_ns
        operations = QueueOperations()
        operations.heartbeat("download-0", "download")

        heartbeat_conn = database.get_heartbeat_connection()
        try:
            first_seen = heartbeat_conn.execute(
                "SELECT last_seen FROM worker_heartbeats WHERE worker_id = ?",
                ("download-0",),
            ).fetchone()[0]
        finally:
            heartbeat_conn.close()

        for _ in range(100):
            operations.heartbeat("download-0", "download")
            self.assertIsNone(operations.claim_download_job("download-0"))
            self.assertIsNone(operations.claim_scrape_job("scraper-1"))
            self.assertFalse(operations.is_paused())

        after_hash = hashlib.sha256(database.DATABASE_PATH.read_bytes()).digest()
        after_mtime = database.DATABASE_PATH.stat().st_mtime_ns
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(before_mtime, after_mtime)

        heartbeat_conn = database.get_heartbeat_connection()
        try:
            heartbeat_row = heartbeat_conn.execute(
                "SELECT COUNT(*), MAX(last_seen) FROM worker_heartbeats"
            ).fetchone()
        finally:
            heartbeat_conn.close()
        self.assertEqual(heartbeat_row[0], 1)
        self.assertEqual(heartbeat_row[1], first_seen)


if __name__ == "__main__":
    unittest.main()
