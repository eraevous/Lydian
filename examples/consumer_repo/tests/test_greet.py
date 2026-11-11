import unittest

from greet import greet


class GreetTests(unittest.TestCase):
    def test_greet(self):
        self.assertEqual(greet("Bob"), "hello, Bob")


if __name__ == "__main__":
    unittest.main()
