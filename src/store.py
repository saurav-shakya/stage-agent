"""Durable per-viewer state in SQLite.

Every row carries the viewer id, so the full story for one viewer (facts with provenance, decisions, actions,
goals, outcomes, journal) can be reconstructed at any moment. Rows are append-only except for status fields on
goals and actions, and every status change is also written to the journal so nothing is silently overwritten.
"""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT, decision_id TEXT, key TEXT, value TEXT, source TEXT, ref TEXT,
  observed_at TEXT, age_s INTEGER, conflicts TEXT, recorded_at TEXT);
CREATE INDEX IF NOT EXISTS facts_user ON facts(user_id);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id TEXT PRIMARY KEY, user_id TEXT, trigger_event_id TEXT, status TEXT, at TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS decisions_user ON decisions(user_id);

CREATE TABLE IF NOT EXISTS processed_triggers (event_id TEXT PRIMARY KEY, decision_id TEXT);
CREATE TABLE IF NOT EXISTS sessions (user_id TEXT, session_id TEXT, decision_id TEXT, PRIMARY KEY(user_id, session_id));

CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY, decision_id TEXT, user_id TEXT, type TEXT, status TEXT,
  published_at TEXT, send_at TEXT, live_until TEXT, dialect TEXT, title_ids TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS actions_user ON actions(user_id);

CREATE TABLE IF NOT EXISTS goals (
  instance_id TEXT PRIMARY KEY, user_id TEXT, goal_id TEXT, arm TEXT, status TEXT, rule_version INTEGER,
  started_at TEXT, deadline TEXT, decision_id TEXT, params TEXT, closed_at TEXT, close_reason TEXT);
CREATE INDEX IF NOT EXISTS goals_user ON goals(user_id);

CREATE TABLE IF NOT EXISTS outcomes (
  instance_id TEXT PRIMARY KEY, user_id TEXT, goal_id TEXT, arm TEXT, status TEXT, body TEXT);

CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, user_id TEXT, kind TEXT, body TEXT);
CREATE INDEX IF NOT EXISTS journal_user ON journal(user_id);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()

    # ------------------------------------------------------------------ idempotency
    def trigger_seen(self, event_id):
        r = self.db.execute('SELECT decision_id FROM processed_triggers WHERE event_id=?', (event_id,)).fetchone()
        return r['decision_id'] if r else None

    def session_seen(self, user_id, session_id):
        r = self.db.execute('SELECT decision_id FROM sessions WHERE user_id=? AND session_id=?',
                            (user_id, session_id)).fetchone()
        return r['decision_id'] if r else None

    def mark_trigger(self, event_id, user_id, session_id, decision_id, session_too=True):
        self.db.execute('INSERT OR IGNORE INTO processed_triggers VALUES (?,?)', (event_id, decision_id))
        if session_too and session_id:
            self.db.execute('INSERT OR IGNORE INTO sessions VALUES (?,?,?)', (user_id, session_id, decision_id))

    # ------------------------------------------------------------------ facts / decisions
    def save_facts(self, user_id, decision_id, facts, recorded_at):
        rows = []
        for k, f in facts.items():
            if not isinstance(f, dict) or 'source' not in f:
                continue
            rows.append((user_id, decision_id, k, json.dumps(f.get('value'), default=str), f.get('source'),
                         f.get('ref'), f.get('observed_at'), f.get('age_s'),
                         json.dumps(f.get('conflicts') or []), recorded_at))
        self.db.executemany('INSERT INTO facts (user_id, decision_id, key, value, source, ref, observed_at, age_s, '
                            'conflicts, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)', rows)

    def save_decision(self, d):
        self.db.execute('INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?)',
                        (d['decision_id'], d['viewer_id'], d['trigger'].get('event_id'), d['status'],
                         d['trigger'].get('deliver_at'), json.dumps(d)))

    def decisions_for(self, user_id):
        return [json.loads(r['body']) for r in
                self.db.execute('SELECT body FROM decisions WHERE user_id=? ORDER BY rowid', (user_id,))]

    def all_decisions(self):
        return [json.loads(r['body']) for r in self.db.execute('SELECT body FROM decisions ORDER BY rowid')]

    # ------------------------------------------------------------------ actions
    def save_action(self, a, status, live_until=None):
        titles = []
        if a['type'] == 'home_page':
            titles = ([a['hero_title_id']] if a.get('hero_title_id') else []) + \
                     [t for r in a.get('rows', []) for t in r['title_ids']]
        elif a.get('title_id'):
            titles = [a['title_id']]
        self.db.execute('INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (a['action_id'], a['decision_id'], a['user_id'], a['type'], status, a.get('published_at'),
                         a.get('send_at'), live_until, a.get('dialect'), json.dumps(titles), json.dumps(a)))

    def set_action_status(self, action_id, status):
        self.db.execute('UPDATE actions SET status=? WHERE action_id=?', (status, action_id))

    def actions_for(self, user_id, statuses=None):
        rows = self.db.execute('SELECT * FROM actions WHERE user_id=? ORDER BY published_at', (user_id,)).fetchall()
        return [dict(r) for r in rows if statuses is None or r['status'] in statuses]

    def actions_with_title(self, title_id):
        like = '%"' + title_id + '"%'
        return [dict(r) for r in self.db.execute('SELECT * FROM actions WHERE title_ids LIKE ?', (like,))]

    def all_actions(self, statuses):
        q = 'SELECT body, status FROM actions WHERE status IN (%s) ORDER BY published_at' % ','.join('?' * len(statuses))
        return [json.loads(r['body']) for r in self.db.execute(q, tuple(statuses))]

    # ------------------------------------------------------------------ goals
    def open_goal(self, user_id):
        r = self.db.execute("SELECT * FROM goals WHERE user_id=? AND status='open' ORDER BY started_at DESC LIMIT 1",
                            (user_id,)).fetchone()
        return self._goal(r)

    def _goal(self, r):
        if r is None:
            return None
        g = dict(r)
        g['params'] = json.loads(g['params'] or '{}')
        return g

    def save_goal(self, g):
        self.db.execute('INSERT OR REPLACE INTO goals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (g['instance_id'], g['user_id'], g['goal_id'], g['arm'], g['status'], g['rule_version'],
                         g['started_at'], g['deadline'], g['decision_id'], json.dumps(g.get('params') or {}),
                         g.get('closed_at'), g.get('close_reason')))

    def goals(self, where='1=1', args=()):
        return [self._goal(r) for r in self.db.execute('SELECT * FROM goals WHERE %s ORDER BY started_at' % where, args)]

    # ------------------------------------------------------------------ outcomes / journal
    def save_outcome(self, o):
        self.db.execute('INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?)',
                        (o['instance_id'], o['user_id'], o['goal_id'], o['arm'], o['status'], json.dumps(o)))

    def outcomes(self):
        return [json.loads(r['body']) for r in self.db.execute('SELECT body FROM outcomes ORDER BY rowid')]

    def journal(self, at, user_id, kind, body):
        self.db.execute('INSERT INTO journal (at, user_id, kind, body) VALUES (?,?,?,?)',
                        (at, user_id, kind, json.dumps(body, default=str)))

    def journal_for(self, user_id):
        return [{'at': r['at'], 'kind': r['kind'], **json.loads(r['body'])} for r in
                self.db.execute('SELECT * FROM journal WHERE user_id=? ORDER BY id', (user_id,))]

    def journal_kind(self, kind):
        return [{'at': r['at'], 'user_id': r['user_id'], **json.loads(r['body'])} for r in
                self.db.execute('SELECT * FROM journal WHERE kind=? ORDER BY id', (kind,))]

    def facts_for(self, user_id):
        return [dict(r) for r in self.db.execute('SELECT * FROM facts WHERE user_id=? ORDER BY id', (user_id,))]

    def learned(self, user_id, key):
        r = self.db.execute("SELECT value FROM facts WHERE user_id=? AND key=? ORDER BY id DESC LIMIT 1",
                            (user_id, 'learned:' + key)).fetchone()
        return json.loads(r['value']) if r else None

    def users(self):
        return [r[0] for r in self.db.execute('SELECT DISTINCT user_id FROM decisions ORDER BY user_id')]
