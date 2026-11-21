import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from support import ROOT
sys.path.insert(0, os.path.join(ROOT, "tools"))
import local_build


class LocalBuildTests(unittest.TestCase):
    def test_increment_same_base(self):
        v, n = local_build.next_version("0.3.0-dev.1", {"base": "0.3.0-dev.1", "n": 2})
        self.assertEqual(v, "0.3.0-dev.1-local.3")
        self.assertEqual(n, 3)

    def test_reset_on_new_base(self):
        v, n = local_build.next_version("0.3.0-dev.2", {"base": "0.3.0-dev.1", "n": 7})
        self.assertEqual(v, "0.3.0-dev.2-local.1")
        self.assertEqual(n, 1)

    def test_first_build(self):
        v, n = local_build.next_version("0.3.0-dev.1", None)
        self.assertEqual(v, "0.3.0-dev.1-local.1")

    def test_source_version_present(self):
        self.assertRegex(local_build.source_version(), r"^\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
