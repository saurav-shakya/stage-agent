"""Model provider abstraction, the mock provider used in replay, and the call wrapper the agent uses.

The model is advisory only. It is asked one narrow question: "of these rule-eligible goals, which fits this viewer
best, and why?". Its answer is parsed strictly and then checked by the verifier against the rule-eligible set; a
goal the rules did not find eligible is rejected no matter how confident the model sounds. Generated text is never
evidence and never approval. The viewer's free-text data (descriptions, search queries) is never sent to it.
"""
import hashlib
import json
import re
import time

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
        state = 'ok'
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
        if self.deps is not None and state == 'degraded':
            # Slow provider: some calls never answer, the rest answer with the window's latency profile. The client
            # applies its own timeout, so a slow answer costs at most model_timeout_ms.
            p95_ms, rate = self.deps.degradation('model_provider', now)
            key = ctx.get('trigger_event_id')
            if _h('model_to', key) < rate:
                raise ModelTimeout('no answer (dependency_degraded window)')
            latency = 200 + int(_h('model_lat', key) * (p95_ms - 200))
        cands = prompt['candidates']
        top = cands[0]
        return json.dumps({'goal_id': top['goal_id'], 'eligible': True, 'confidence': 0.7,
                           'reason': 'agrees with the highest-priority rule-eligible goal'}), latency


# A reference that names a viewer or one of their events ("profile:U-01234@...", "event:<uuid>").
_VIEWER_REF = re.compile(r'\b(?:profile|event|user|viewer):\S+|\bU-\d+\b')

CLAUDE_SYSTEM = (
    'You help a streaming app choose ONE next goal for a viewer. You receive goals that deterministic rules have '
    'already found eligible, each with the rule\'s reason, and a few facts about the viewer. Pick the goal most '
    'likely to be reached within its window. Do not invent facts; if nothing distinguishes them, prefer the first '
    '(highest-priority) goal. Your answer is advisory and will be checked against the rules.')


class ClaudeProvider(Provider):
    """Optional hosted provider (Anthropic Claude), used only with `--provider claude`. The mock stays the default
    so evaluators never need a key.

    Privacy: only goal ids, rule reasons with every viewer/event reference removed, and three booleans/enums are
    sent. No viewer id, title description, search query or event is sent. The pack's policy still applies: use this
    only with an account whose data-retention terms allow it (the API does not train on inputs by default).

    The SDK is imported lazily so the default path stays standard-library only (`pip install anthropic` to use it).
    Errors map to the same ModelTimeout / ModelUnavailable / ModelError the mock raises, so the circuit breaker and
    rule fallback behave identically.
    """
    name = 'claude'

    def __init__(self, model='claude-opus-5-5', timeout_ms=2500, client=None):
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError('--provider claude needs the Anthropic SDK: pip install anthropic') from e
        self.anthropic = anthropic
        self.model = model
        # Our own deadline, no SDK retries: a retry would only spend the decision's latency budget.
        self.client = client or anthropic.Anthropic(timeout=timeout_ms / 1000.0, max_retries=0)

    @staticmethod
    def scrub(text):
        return _VIEWER_REF.sub('[ref]', str(text))

    def request(self, prompt):
        cands = [{'goal_id': c['goal_id'], 'reason': self.scrub(c['reason'])} for c in prompt['candidates']]
        viewer = {k: v for k, v in prompt.get('viewer', {}).items()
                  if k in ('subscription_status', 'ever_played', 'ever_completed', 'days_since_signup')}
        schema = {
            'type': 'object',
            'properties': {
                'goal_id': {'type': 'string', 'enum': [c['goal_id'] for c in cands]},
                'eligible': {'type': 'boolean'},
                'confidence': {'type': 'number'},
                'reason': {'type': 'string'},
            },
            'required': ['goal_id', 'eligible', 'confidence', 'reason'],
            'additionalProperties': False,
        }
        return dict(
            model=self.model,
            max_tokens=1024,
            system=CLAUDE_SYSTEM,
            messages=[{'role': 'user', 'content': json.dumps({'eligible_goals': cands, 'viewer': viewer})}],
            output_config={'effort': 'low', 'format': {'type': 'json_schema', 'schema': schema}},
        )

    def complete(self, prompt, ctx):
        a = self.anthropic
        t0 = time.perf_counter()
        try:
            resp = self.client.messages.create(**self.request(prompt))
        except a.APITimeoutError as e:            # subclass of APIConnectionError: catch it first
            raise ModelTimeout('no answer within our timeout') from e
        except a.RateLimitError as e:
            raise ModelUnavailable('HTTP 429 rate limited') from e
        except a.APIStatusError as e:
            if e.status_code >= 500:
                raise ModelUnavailable('HTTP %d' % e.status_code) from e
            raise ModelError('HTTP %d' % e.status_code) from e
        except a.APIConnectionError as e:
            raise ModelUnavailable('connection error') from e
        latency = int((time.perf_counter() - t0) * 1000)
        if resp.stop_reason != 'end_turn':      # refusal / max_tokens: no answer; the rules decide
            raise ModelError('stop_reason=%s' % resp.stop_reason)
        text = next((b.text for b in resp.content if b.type == 'text'), '')
        return text, latency


class ModelClient:
    """Budget, timeout and parsing around a provider. Never raises: returns (answer|None, record)."""

    BREAKER_S = 60                 # how long the circuit stays open
    BREAKER_AFTER_TIMEOUTS = 3     # consecutive timeouts that mean "the provider is slow", not one unlucky call

    def __init__(self, provider, timeout_ms=2500, max_calls=8):
        self.provider = provider
        self.timeout_ms = timeout_ms
        self.max_calls = max_calls
        # After a 503, or after several timeouts in a row, stop calling for a short while instead of paying the
        # timeout on every decision. The count resets only on an answer, so when the circuit closes a single probe
        # that times out again re-opens it at once.
        self.breaker_until = None
        self.consecutive_timeouts = 0

    def _timed_out(self, rec, now, detail=None):
        from datetime import timedelta
        self.consecutive_timeouts += 1
        rec.update(outcome='timeout', latency_ms=self.timeout_ms)
        if detail:
            rec['detail'] = detail
        if self.consecutive_timeouts >= self.BREAKER_AFTER_TIMEOUTS:
            self.breaker_until = now + timedelta(seconds=self.BREAKER_S)
            rec['breaker_opened'] = '%d consecutive timeouts' % self.consecutive_timeouts
        return None, rec

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
            return self._timed_out(rec, now, str(e))
        except ModelUnavailable as e:
            from datetime import timedelta
            self.breaker_until = now + timedelta(seconds=60)
            rec.update(outcome='unavailable', latency_ms=50, detail=str(e))
            return None, rec
        except ModelError as e:  # e.g. a refusal or a 4xx from a hosted provider
            rec.update(outcome='error', latency_ms=50, detail=str(e))
            return None, rec
        if lat > self.timeout_ms:
            return self._timed_out(rec, now)
        self.consecutive_timeouts = 0  # the provider answered in time
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


DEFAULT_P95_MS = 8000        # used when a degraded control event does not state its own numbers
DEFAULT_TIMEOUT_RATE = 0.20


def degradation(detail):
    """(p95_ms, timeout_rate) for a degraded window, read from the control event's operational detail
    ("p95 read latency 8 s and 20% of reads time out"). Missing numbers fall back to the defaults above.
    This configures the harness's simulated fault; it never reaches the agent's decisions."""
    import re
    p95_ms, rate = DEFAULT_P95_MS, DEFAULT_TIMEOUT_RATE
    m = re.search(r'p95[^0-9]{0,30}(\d+(?:\.\d+)?)\s*(ms|s)\b', detail or '', re.I)
    if m:
        p95_ms = float(m.group(1)) * (1 if m.group(2).lower() == 'ms' else 1000)
    m = re.search(r'(\d+(?:\.\d+)?)\s*%[^.;]{0,40}time', detail or '', re.I)
    if m:
        rate = float(m.group(1)) / 100
    return p95_ms, rate


def degraded_call(key, p95_ms, rate, timeout_ms):
    """One call in a degraded window: (ok, latency_ms). Deterministic per key."""
    if _h('to', key) < rate:
        return False, timeout_ms
    lat = 200 + int(_h('lat', key) * (p95_ms - 200))
    if lat > timeout_ms:
        return False, timeout_ms
    return True, lat


class Dependencies:
    """Dependency health over time, driven by control events. Shared by the mock model and the data store.

    Works for any dependency named in a control event: model_provider and user_data_store are both simulated as
    either unavailable or degraded (slow, with some calls timing out)."""

    def __init__(self):
        self.windows = []  # (dependency, start, end, state, detail)

    def add(self, c):
        state = 'unavailable' if c['type'] == 'dependency_unavailable' else 'degraded'
        self.windows.append((c['dependency'], ts(c['at']), ts(c['until']), state, c.get('detail')))

    def _window(self, dep, now):
        for w in self.windows:
            if w[0] == dep and w[1] <= now < w[2]:
                return w
        return None

    def state(self, dep, now):
        w = self._window(dep, now)
        return w[3] if w else 'ok'

    def degradation(self, dep, now):
        w = self._window(dep, now)
        return degradation(w[4] if w else None)


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
            p95_ms, rate = self.deps.degradation('user_data_store', now)
            return degraded_call(key, p95_ms, rate, self.timeout_ms)
        return True, 20 + int(_h('lat', key) * 60)
