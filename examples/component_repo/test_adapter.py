import unittest
from adapter import consume


class DemoTests(unittest.TestCase):
    def test_keeps_false_and_zero(self):
        self.assertEqual(consume({"schema": "kit-demo@1", "payload": {"enabled": False, "n": 0}}),
                         {"enabled": False, "n": 0})

    def test_rejects_wrong_version(self):
        with self.assertRaises(ValueError):
            consume({"schema": "kit-demo@2", "payload": {}})
