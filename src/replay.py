"""Replay harness: feeds triggers, live events and control events to the runtime in delivery order.

The replay runs faster than real time. Each decision's time = measured processing time + simulated dependency
latency (model, data store). A small pool of workers models capacity, so bursts (offline-buffered opens, the
evening peak) show up as queueing delay in `publish_delay_ms`, exactly where they would hurt in production.
"""
import heapq
from datetime import timedelta
import json
import os

from .agent import Config, Runtime
from .goals import GoalBook
from .llm import ClaudeProvider, DataStore, Dependencies, MockProvider, ModelClient, load_faults
from .pack import Pack
from .store import Store
from .timeutil import ts
from .verifier import Verifier


def read_jsonl(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def build_runtime(pack_dir, db_path, config=None, pack=None):
    pack = pack or Pack(pack_dir)
    store = Store(db_path)
    goalbook = GoalBook(pack.goals_cfg, pack.titles, pack.slug_titles)
    deps = Dependencies()
    cfg = config or Config()
    if cfg.provider == 'claude':
        provider = ClaudeProvider(model=cfg.model_name, timeout_ms=cfg.model_timeout_ms)
    else:
        provider = MockProvider(load_faults(os.path.join(pack_dir, 'replay/model_faults.jsonl')), deps)
    rt = Runtime(pack, store, goalbook, Verifier(pack, goalbook),
                 ModelClient(provider, timeout_ms=cfg.model_timeout_ms, max_calls=cfg.max_model_calls),
                 DataStore(deps), cfg)
    return rt


def run_replay(pack_dir, out_dir, workers=4, config=None, log=print):
    os.makedirs(out_dir, exist_ok=True)
    db = os.path.join(out_dir, 'state.sqlite')
    if os.path.exists(db):
        os.remove(db)
    rt = build_runtime(pack_dir, db, config)
    log('loaded pack: %d titles, %d viewers with history, %d duplicate history events dropped'
        % (len(rt.pack.titles), len(rt.pack.history.by_user), rt.pack.dup_events))

    stream = []
    for i, c in enumerate(read_jsonl(os.path.join(pack_dir, 'replay/control_events.jsonl'))):
        stream.append((ts(c['at']), 0, i, 'control', c))
    for i, e in enumerate(read_jsonl(os.path.join(pack_dir, 'replay/live_events.jsonl'))):
        stream.append((ts(e['deliver_at']), 1, i, 'live', e))
    for i, t in enumerate(read_jsonl(os.path.join(pack_dir, 'replay/triggers.jsonl'))):
        stream.append((ts(t['deliver_at']), 2, i, 'trigger', t))
    stream.sort(key=lambda x: (x[0], x[1], x[2]))

    free = [stream[0][0]] * workers
    heapq.heapify(free)
    n = {'trigger': 0, 'live': 0, 'control': 0}
    for at, _, _, kind, item in stream:
        n[kind] += 1
        if kind == 'control':
            rt.on_control(item)
        elif kind == 'live':
            rt.on_live_event(item)
        else:
            w = heapq.heappop(free)
            start = max(at, w)
            wait_ms = int((start - at).total_seconds() * 1000)
            d = rt.on_trigger(item, start, wait_ms)
            heapq.heappush(free, start + timedelta(milliseconds=d.get('decided_in_ms', 0)))
    log('replayed %d triggers, %d live events, %d control events' % (n['trigger'], n['live'], n['control']))
    write_outputs(rt, out_dir)
    return rt


def write_outputs(rt, out_dir):
    st = rt.store
    decisions = st.all_decisions()
    with open(os.path.join(out_dir, 'decisions.jsonl'), 'w', encoding='utf-8') as f:
        for d in decisions:
            f.write(json.dumps(d, ensure_ascii=False) + '\n')
    # Actions the platform actually receives: published pages (including ones later superseded, which were live
    # until replaced) and scheduled messages that were not cancelled before their send time.
    acts = st.all_actions(('published', 'superseded', 'scheduled'))
    with open(os.path.join(out_dir, 'actions.jsonl'), 'w', encoding='utf-8') as f:
        for a in acts:
            f.write(json.dumps({k: v for k, v in a.items() if v is not None}, ensure_ascii=False) + '\n')
    st.commit()
    return decisions, acts
