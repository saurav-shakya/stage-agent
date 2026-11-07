"""Model client resilience and the degraded-dependency profile. No data pack needed."""
import unittest
from datetime import timedelta

from src.llm import (DEFAULT_P95_MS, DEFAULT_TIMEOUT_RATE, Dependencies, MockProvider, ModelClient, ModelTimeout,
                     Provider, degradation)
from src.timeutil import ts

T0 = ts('2026-10-01T05:00:00.000Z')
CANDS = [{'goal_id': 'G02', 'reason': 'never completed'}]


class StubProvider(Provider):
    """Times out while `slow` is True, otherwise answers at once."""

    def __init__(self):
        self.slow = True
        self.calls = 0

    def complete(self, prompt, ctx):
        self.calls += 1
        if self.slow:
            raise ModelTimeout('stub')
        return '{"goal_id": "G02", "confidence": 0.6, "reason": "ok"}', 100


def ask(client, at, budget=None):
    return client.ask_goal(CANDS, {}, {'now': at, 'trigger_event_id': 'e-%s' % at.isoformat()},
                           budget or {'model_calls': 0})[1]


class DegradationProfileTests(unittest.TestCase):
    def test_reads_the_replay_detail(self):
        self.assertEqual(degradation('p95 read latency 8 s and 20% of reads time out'), (8000, 0.20))

    def test_milliseconds_and_defaults(self):
        self.assertEqual(degradation('p95 900 ms')[0], 900)
        self.assertEqual(degradation(None), (DEFAULT_P95_MS, DEFAULT_TIMEOUT_RATE))
        self.assertEqual(degradation('provider is slow'), (DEFAULT_P95_MS, DEFAULT_TIMEOUT_RATE))

    def test_mock_model_is_slow_only_inside_a_degraded_window(self):
        deps = Dependencies()
        deps.add({'type': 'dependency_degraded', 'dependency': 'model_provider', 'at': '2026-10-01T05:00:00.000Z',
                  'until': '2026-10-01T05:10:00.000Z', 'detail': 'p95 latency 30 s and 0% of calls time out'})
        mock = MockProvider(deps=deps)
        prompt = {'candidates': CANDS}
        inside = [mock.complete(prompt, {'now': T0 + timedelta(seconds=i), 'trigger_event_id': 'x%d' % i})[1]
                  for i in range(20)]
        after = [mock.complete(prompt, {'now': T0 + timedelta(minutes=11), 'trigger_event_id': 'x%d' % i})[1]
                 for i in range(20)]
        self.assertGreater(max(inside), 2500)   # the window is really slow
        self.assertLessEqual(max(after), 1200)  # normal latency once it ends


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self):
        self.p = StubProvider()
        self.c = ModelClient(self.p, timeout_ms=2500)

    def test_single_timeout_does_not_open_the_circuit(self):
        self.assertEqual(ask(self.c, T0)['outcome'], 'timeout')
        self.p.slow = False
        self.assertEqual(ask(self.c, T0 + timedelta(seconds=1))['outcome'], 'ok')

    def test_consecutive_timeouts_open_then_probe_and_recover(self):
        recs = [ask(self.c, T0 + timedelta(seconds=i)) for i in range(4)]
        self.assertEqual([r['outcome'] for r in recs], ['timeout'] * 3 + ['skipped_circuit_open'])
        self.assertEqual(self.p.calls, 3)                       # the 4th decision never called the provider
        # circuit closes after 60 s; a probe that still times out re-opens it at once
        r = ask(self.c, T0 + timedelta(seconds=63))
        self.assertEqual(r['outcome'], 'timeout')
        self.assertIn('breaker_opened', r)
        self.assertEqual(ask(self.c, T0 + timedelta(seconds=64))['outcome'], 'skipped_circuit_open')
        # provider recovers: next probe answers and the timeout streak resets
        self.p.slow = False
        self.assertEqual(ask(self.c, T0 + timedelta(seconds=130))['outcome'], 'ok')
        self.p.slow = True
        self.assertEqual(ask(self.c, T0 + timedelta(seconds=131))['outcome'], 'timeout')
        self.assertNotIn('breaker_opened', ask(self.c, T0 + timedelta(seconds=132)))


if __name__ == '__main__':
    unittest.main()
