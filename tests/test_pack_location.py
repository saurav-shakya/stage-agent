"""Finding the data pack: both unzip layouts work, and a missing or wrong pack is an error, never a silent skip."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from src import pack as pack_mod
from src.pack import PackNotFound, find_pack


def make_pack(root):
    for rel in ('config/goals.json', 'replay/triggers.jsonl'):
        os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
        open(os.path.join(root, rel), 'w').close()
    return root


class PackLocationTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='oua_repo_')
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        os.environ.pop('OUA_PACK', None)
        self.root = mock.patch.object(pack_mod, 'REPO_ROOT', self.repo)
        self.root.start()

    def tearDown(self):
        self.root.stop()
        self.env.stop()
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_default_readme_layout(self):
        p = make_pack(os.path.join(self.repo, 'data_pack', 'one_user_one_agent_pack'))
        self.assertEqual(find_pack(), os.path.normpath(p))

    def test_default_flat_layout(self):
        p = make_pack(os.path.join(self.repo, 'data_pack'))
        self.assertEqual(find_pack(), os.path.normpath(p))

    def test_explicit_path_accepts_pack_or_its_parent(self):
        p = make_pack(os.path.join(self.repo, 'elsewhere', 'one_user_one_agent_pack'))
        self.assertEqual(find_pack(p), os.path.normpath(p))
        self.assertEqual(find_pack(os.path.dirname(p)), os.path.normpath(p))

    def test_env_var_is_used(self):
        p = make_pack(os.path.join(self.repo, 'via_env'))
        os.environ['OUA_PACK'] = p
        self.assertEqual(find_pack(), os.path.normpath(p))

    def test_missing_pack_raises(self):
        with self.assertRaises(PackNotFound) as cm:
            find_pack()
        self.assertIn('data_pack', str(cm.exception))

    def test_wrong_explicit_path_never_falls_back_to_default(self):
        make_pack(os.path.join(self.repo, 'data_pack'))  # a valid default exists...
        with self.assertRaises(PackNotFound):          # ...but a typo in --pack must still fail
            find_pack(os.path.join(self.repo, 'typo'))
        os.environ['OUA_PACK'] = os.path.join(self.repo, 'typo')
        with self.assertRaises(PackNotFound):
            find_pack()


if __name__ == '__main__':
    unittest.main()
