"""Loads the data pack into memory and keeps the per-viewer event history.

The pack is the only valid evidence about viewers and titles. Nothing here interprets text inside the data as
instructions: descriptions, titles, search queries and deeplinks are content (messaging_policy.md rule 6).
"""
import bisect
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import timedelta

from .timeutil import ts, true_time

# Text inside the catalogue that reads like an instruction to an automated agent. We never act on it; we flag the
# title so the flag is visible in evidence, and we never pass descriptions to the model.
INJECTION_PATTERNS = [
    r'ignore (all |your |any )?(previous |prior )?(rules|instructions)',
    r'system (note|prompt|message)',
    r'\bai agents?\b',
    r'(send|message|notify)\b.{0,60}\b(every|all) users?',
    r'opted[- ]out',
]
_INJ = re.compile('|'.join(INJECTION_PATTERNS), re.I)

DEEPLINK_RE = re.compile(r'^ott://c/([A-Za-z0-9_-]+)/(d[1-6])/title/(T\d{4})$')


def looks_like_instruction(text):
    return bool(text and _INJ.search(text))


class Pack:
    def __init__(self, root):
        self.root = root
        self.withdrawn = {}  # title_id -> datetime, from control events at runtime
        self._load_config()
        self._load_catalogue()
        self._load_profiles()
        self._load_arms()
        self.history = History()
        self.dup_events = self.history.load(os.path.join(root, 'data/events.jsonl'))
        self._popularity()

    # ------------------------------------------------------------------ loading
    def _path(self, rel):
        return os.path.join(self.root, rel)

    def _load_config(self):
        with open(self._path('config/goals.json'), encoding='utf-8') as f:
            self.goals_cfg = json.load(f)
        with open(self._path('config/interventions.json'), encoding='utf-8') as f:
            self.interventions = json.load(f)
        with open(self._path('config/pricing.json'), encoding='utf-8') as f:
            self.pricing = json.load(f)
        self.prices = {p['price_inr'] for p in self.pricing['plans']}
        self.plans = {p['plan_id']: p for p in self.pricing['plans']}

    def _load_catalogue(self):
        self.titles = {}
        self.slug_titles = defaultdict(list)
        with open(self._path('data/catalogue.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                t = {
                    'title_id': r['title_id'], 'slug': r['content_slug'], 'name': r['title'],
                    'dialect': r['dialect'], 'language': r['language'], 'type': r['content_type'],
                    'genres': [g for g in r['genres'].split('|') if g], 'release_date': r['release_date'],
                    'from': ts(r['available_from']), 'until': ts(r['available_until']),
                    'premium': r['is_premium'] == 'true',
                    'injection_flag': looks_like_instruction(r['description']),
                }
                self.titles[t['title_id']] = t
                self.slug_titles[t['slug']].append(t['title_id'])
        self.dialects = {t['dialect'] for t in self.titles.values()}
        self.episodes = defaultdict(list)
        self.episode_index = {}
        with open(self._path('data/episodes.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.episodes[r['content_slug']].append(
                    {'season': int(r['season']), 'episode': int(r['episode']), 'episode_id': r['episode_id'],
                     'minutes': int(r['minutes'])})
        for slug, eps in self.episodes.items():
            eps.sort(key=lambda e: (e['season'], e['episode']))
            for i, e in enumerate(eps):
                self.episode_index[e['episode_id']] = (slug, i)

    def _load_profiles(self):
        self.profile_rows = defaultdict(list)
        with open(self._path('data/profiles_snapshot.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.profile_rows[r['user_id']].append(r)

    def _load_arms(self):
        self.arms = {}
        with open(self._path('replay/arms.csv'), encoding='utf-8') as f:
            for r in csv.DictReader(f):
                self.arms[r['user_id']] = r['arm']

    def _popularity(self):
        """Play counts per content_slug over the 30-day history (a simple, inspectable popularity signal)."""
        self.popularity = Counter()
        for evs in self.history.by_user.values():
            for e in evs:
                if e['event'] == 'play_started':
                    t = self.titles.get(e['properties'].get('title_id'))
                    if t:
                        self.popularity[t['slug']] += 1

    # ------------------------------------------------------------------ catalogue queries
    def available(self, title_id, at):
        t = self.titles.get(title_id)
        if t is None or t['from'] is None or t['from'] > at:
            return False
        if t['until'] is not None and at >= t['until']:
            return False
        w = self.withdrawn.get(title_id)
        if w is not None and at >= w:
            return False
        return True

    def parse_deeplink(self, link):
        m = DEEPLINK_RE.match(link or '')
        if not m:
            return None
        return {'campaign': m.group(1), 'dialect': m.group(2), 'title_id': m.group(3)}


class History:
    """Per-viewer event history, deduplicated by event_id and ordered by server receive time."""

    def __init__(self):
        self.by_user = defaultdict(list)
        self._keys = defaultdict(list)
        self.seen = set()

    def load(self, path):
        dups = 0
        with open(path, encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    if not self.add(json.loads(line)):
                        dups += 1
        return dups

    def add(self, e):
        """Returns False if the event_id was already seen (duplicate delivery)."""
        if e['event_id'] in self.seen:
            return False
        self.seen.add(e['event_id'])
        e['_received'] = ts(e['received_at'])
        e['_true'] = true_time(e)
        u = e['user_id']
        i = bisect.bisect_right(self._keys[u], e['_received'])
        self._keys[u].insert(i, e['_received'])
        self.by_user[u].insert(i, e)
        return True

    def until(self, user_id, at):
        """Events for the viewer that our servers had received by `at` (no peeking into the future)."""
        keys = self._keys.get(user_id, [])
        return self.by_user.get(user_id, [])[:bisect.bisect_right(keys, at)]


def window(at, days):
    return at - timedelta(days=days)
