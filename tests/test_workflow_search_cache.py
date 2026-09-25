import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SERVER = Path(__file__).resolve().parents[1] / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from routers import workflows


class WorkflowSearchCacheTests(unittest.TestCase):
    def setUp(self):
        with workflows._workflow_search_cache_lock:
            workflows._workflow_search_cache.clear()

    def test_cached_document_invalidates_when_file_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps({"1": {"class_type": "FirstNode", "inputs": {}}}))
            _data, graph_format, _mode, fields = workflows._cached_search_document(
                path, path.name,
            )
            self.assertEqual(graph_format, "api")
            self.assertIn("FirstNode", fields["node_classes"])

            path.write_text(json.dumps({"1": {"class_type": "SecondNode", "inputs": {}}}))
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            _data, _graph_format, _mode, fields = workflows._cached_search_document(
                path, path.name,
            )
            self.assertIn("SecondNode", fields["node_classes"])
            self.assertNotIn("FirstNode", fields["node_classes"])


if __name__ == "__main__":
    unittest.main()
