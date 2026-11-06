"""Model provider abstraction, the mock provider used in replay, and the call wrapper the agent uses.

The model is advisory only. It is asked one narrow question: "of these rule-eligible goals, which fits this viewer
best, and why?". Its answer is parsed strictly and then checked by the verifier against the rule-eligible set; a
goal the rules did not find eligible is rejected no matter how confident the model sounds. Generated text is never
evidence and never approval. The viewer's free-text data (descriptions, search queries) is never sent to it.
"""
import hashlib
import json

from .timeutil import ts


class ModelError(Exception):
    kind = 'error'


class ModelTimeout(ModelError):
    kind = 'timeout'


class ModelUnavailable(ModelError):
    kind = 'unavailable'


class Provider:
    """Interface. `complete` returns (raw_text, simulated_latency_ms) or raises ModelError."""
    name = 'base'

    def complete(self, prompt, ctx):  # pragma: no cover - interface
        raise NotImplementedError


def _h(*parts):
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class MockProvider(Provider):
    """Deterministic stand-in for a hosted model.

    Normal answer: agrees with the highest-priority rule candidate. Reproduces replay/model_faults.jsonl at the named
    triggers, and outages from control events (dependency_unavailable / dependency_degraded for model_provider).
    """
    name = 'mock'

    def __init__(self, faults=None, deps=None):
        self.faults = {f['trigger_event_id']: f for f in (faults or [])}
        self.deps = deps

    def complete(self, prompt, ctx):
        now = ctx['now']
        if self.deps is not None:
            state = self.deps.state('model_provider', now)
            if state == 'unavailable':
                raise ModelUnavailable('HTTP 503 (dependency_unavailable window)')
        f = self.faults.get(ctx.get('trigger_event_id'))
        if f:
            if f['fault'] == 'timeout':
                raise ModelTimeout('no answer in %ss' % f.get('seconds'))
            out = f['model_output']
            return (out if isinstance(out, str) else json.dumps(out)), 900
        latency = 300 + int(_h('lat', ctx.get('trigger_event_id')) * 900)
        cands = prompt['candidates']
        top = cands[0]
        return json.dumps({'goal_id': top['goal_id'], 'eligible': True, 'confidence': 0.7,
                           'reason': 'agrees with the highest-priority rule-eligible goal'}), latency


class ModelClient:
    """Budget, timeout and parsing around a provider. Never raises: returns (answer|None, record)."""

    def __init__(self, provider, timeout_ms=2500, max_calls=8):
        self.provider = provider
        self.timeout_ms = timeout_ms
        self.max_calls = max_calls
        self.breaker_until = None  # after a 503 we stop calling for a short while instead of paying the timeout

    def ask_goal(self, candidates, summary, ctx, budget):
        rec = {'called': False, 'outcome': None, 'latency_ms': 0}
        now = ctx['now']
        if self.breaker_until is not None and now < self.breaker_until:
            rec['outcome'] = 'skipped_circuit_open'
            return None, rec
        if budget['model_calls'] >= self.max_calls:
            rec['outcome'] = 'skipped_budget'
            return None, rec
        budget['model_calls'] += 1
        rec['called'] = True
        prompt = {'task': 'pick_goal', 'candidates': [{'goal_id': c['goal_id'], 'reason': c['reason']}
                                                      for c in candidates], 'viewer': summary}
        try:
            raw, lat = self.provider.complete(prompt, ctx)
        except ModelTimeout as e:
            rec.update(outcome='timeout', latency_ms=self.timeout_ms, detail=str(e))
            return None, rec
        except ModelUnavailable as e:
            from datetime import timedelta
            self.breaker_until = now + timedelta(seconds=60)
            rec.update(outcome='unavailable', latency_ms=50, detail=str(e))
            return None, rec
        except ModelError as e:  # pragma: no cover
            rec.update(outcome='error', latency_ms=50, detail=str(e))
            return None, rec
        if lat > self.timeout_ms:
            rec.update(outcome='timeout', latency_ms=self.timeout_ms)
            return None, rec
        rec['latency_ms'] = lat
        try:
            ans = json.loads(raw)
            if not isinstance(ans, dict) or not isinstance(ans.get('goal_id'), str):
                raise ValueError('missing goal_id')
            conf = float(ans.get('confidence', 0))
        except (ValueError, TypeError) as e:
            rec.update(outcome='malformed_output', detail=str(e)[:80], raw=raw[:120])
            return None, rec
        rec.update(outcome='ok', answer={'goal_id': ans['goal_id'], 'confidence': conf,
                                         'reason': str(ans.get('reason', ''))[:200]})
        return rec['answer'], rec


def load_faults(path):
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


class Dependencies:
    """Dependency health over time, driven by control events. Shared by the mock model and the data store."""

    def __init__(self):
        self.windows = []  # (dependency, start, end, state, detail)

    def add(self, c):
        state = 'unavailable' if c['type'] == 'dependency_unavailable' else 'degraded'
        self.windows.append((c['dependency'], ts(c['at']), ts(c['until']), state, c.get('detail')))

    def state(self, dep, now):
        for d, s, e, st, _ in self.windows:
            if d == dep and s <= now < e:
                return st
        return 'ok'


class DataStore:
    """The user data store read. Normal reads are fast; in a degraded window reads are slow and some time out.

    Latencies are simulated (the replay runs faster than real time) and added to the decision's measured time.
    """

    def __init__(self, deps, timeout_ms=1500):
        self.deps = deps
        self.timeout_ms = timeout_ms

    def read(self, user_id, now, key):
        st = self.deps.state('user_data_store', now)
        if st == 'unavailable':
            return False, 50
        if st == 'degraded':
            if _h('to', key) < 0.20:
                return False, self.timeout_ms
            lat = 200 + int(_h('lat', key) * 7800)  # p95 around 8 s, per the control event
            if lat > self.timeout_ms:
                return False, self.timeout_ms
            return True, lat
        return True, 20 + int(_h('lat', key) * 60)
