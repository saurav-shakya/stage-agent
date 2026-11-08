"""Read KEY=VALUE lines from a `.env` file into the environment (standard library only).

Used only for the optional hosted model provider, so an API key can live in a git-ignored `.env` instead of the
shell history. A variable already set in the environment wins over the file.
"""
import os

from .pack import REPO_ROOT

DEFAULT_PATH = os.path.join(REPO_ROOT, '.env')


def load_env(path=DEFAULT_PATH):
    """Returns the names of the variables it set (never their values)."""
    if not os.path.isfile(path):
        return []
    set_now = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            if key.startswith('export '):
                key = key[len('export '):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                value = value[1:-1]
            if key and value and key not in os.environ:
                os.environ[key] = value
                set_now.append(key)
    return set_now
