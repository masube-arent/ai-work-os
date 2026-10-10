import unittest

from greet import greet


class GreetTest(unittest.TestCase):
    def test_greets_by_name(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")

    def test_greets_in_english_explicitly(self):
        self.assertEqual(greet("Ada", lang="en"), "Hello, Ada!")

    def test_greets_in_japanese(self):
        self.assertEqual(greet("Ada", lang="ja"), "こんにちは、Adaさん")

    def test_unsupported_lang_raises(self):
        with self.assertRaises(ValueError):
            greet("Ada", lang="fr")


if __name__ == "__main__":
    unittest.main()
