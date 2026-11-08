"""The `.env` reader used for the optional hosted provider's key."""
import os
import tempfile
import unittest

from src.envfile import load_env


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.keys = ('OUA_TEST_A', 'OUA_TEST_B', 'OUA_TEST_C', 'OUA_TEST_EMPTY', 'OUA_TEST_SET')
        self.saved = {k: os.environ.pop(k, None) for k in self.keys}
        fd, self.path = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as f:
            f.write('# comment\n\nOUA_TEST_A=plain\nexport OUA_TEST_B="quoted value"\n'
                    "OUA_TEST_C='single'\nOUA_TEST_EMPTY=\nnot a pair\nOUA_TEST_SET=from_file\n")

    def tearDown(self):
        os.remove(self.path)
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def test_reads_pairs_strips_quotes_and_skips_noise(self):
        os.environ['OUA_TEST_SET'] = 'from_shell'
        names = load_env(self.path)
        self.assertEqual(sorted(names), ['OUA_TEST_A', 'OUA_TEST_B', 'OUA_TEST_C'])
        self.assertEqual(os.environ['OUA_TEST_A'], 'plain')
        self.assertEqual(os.environ['OUA_TEST_B'], 'quoted value')
        self.assertEqual(os.environ['OUA_TEST_C'], 'single')
        self.assertNotIn('OUA_TEST_EMPTY', os.environ)
        self.assertEqual(os.environ['OUA_TEST_SET'], 'from_shell')   # the shell wins over the file

    def test_missing_file_is_fine(self):
        self.assertEqual(load_env(self.path + '.missing'), [])


if __name__ == '__main__':
    unittest.main()
