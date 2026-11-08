
import json
import sys
import types
import unittest
from datetime import datetime, timezone

from src.llm import ClaudeProvider, ModelClient, ModelError, ModelTimeout, ModelUnavailable


def fake_sdk():
    """Just enough of the `anthropic` module: the client and the exception classes the provider maps."""
    m = types.ModuleType('anthropic')

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APITimeoutError(APIConnectionError):
        pass

    class APIStatusError(APIError):
        def __init__(self, status_code):
            super().__init__('HTTP %d' % status_code)
            self.status_code = status_code

    class RateLimitError(APIStatusError):
        def __init__(self):
            super().__init__(429)

    m.APIError, m.APIConnectionError, m.APITimeoutError = APIError, APIConnectionError, APITimeoutError
    m.APIStatusError, m.RateLimitError = APIStatusError, RateLimitError
    m.Anthropic = lambda **kw: None
    return m


class FakeClient:
    def __init__(self, answer=None, error=None, stop_reason='end_turn'):
        self.sent, self.answer, self.error, self.stop_reason = [], answer, error, stop_reason
        self.messages = self

    def create(self, **kw):
        self.sent.append(kw)
        if self.error:
            raise self.error
        block = types.SimpleNamespace(type='text', text=json.dumps(self.answer))
        return types.SimpleNamespace(stop_reason=self.stop_reason, content=[block])


PROMPT = {'task': 'pick_goal',
          'candidates': [{'goal_id': 'G04', 'reason': 'never paid per profile:U-02905@2026-09-29T13:11:00.669Z'},
                         {'goal_id': 'G02', 'reason': 'no episode_completed in the supplied history'}],
          'viewer': {'subscription_status': 'never', 'ever_played': True, 'ever_completed': False}}
CTX = {'now': datetime(2026, 10, 1, 12, tzinfo=timezone.utc), 'trigger_event_id': 'e1'}


class ClaudeProviderTests(unittest.TestCase):
    def setUp(self):
        self.sdk = fake_sdk()
        self._saved = sys.modules.get('anthropic')
        sys.modules['anthropic'] = self.sdk

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop('anthropic', None)
        else:
            sys.modules['anthropic'] = self._saved

    def provider(self, **kw):
        return ClaudeProvider(client=FakeClient(**kw))

    def test_request_sends_no_viewer_reference_and_limits_answer_to_eligible_goals(self):
        p = self.provider(answer={'goal_id': 'G02', 'eligible': True, 'confidence': 0.6, 'reason': 'r'})
        text, _ = p.complete(PROMPT, CTX)
        sent = p.client.sent[0]
        body = json.dumps(sent)
        self.assertNotIn('U-02905', body)
        self.assertNotIn('profile:', body)
        self.assertEqual(sent['model'], 'claude-opus-5-5')
        schema = sent['output_config']['format']['schema']
        self.assertEqual(schema['properties']['goal_id']['enum'], ['G04', 'G02'])
        self.assertEqual(json.loads(text)['goal_id'], 'G02')

    def test_errors_map_to_the_same_failures_as_the_mock(self):
        sdk = self.sdk
        for err, kind in ((sdk.APITimeoutError(), ModelTimeout), (sdk.RateLimitError(), ModelUnavailable),
                          (sdk.APIStatusError(503), ModelUnavailable), (sdk.APIConnectionError(), ModelUnavailable),
                          (sdk.APIStatusError(400), ModelError)):
            with self.assertRaises(kind, msg=type(err).__name__):
                self.provider(error=err).complete(PROMPT, CTX)

    def test_refusal_is_no_answer_and_the_rules_decide(self):
        client = ModelClient(self.provider(answer={}, stop_reason='refusal'))
        answer, rec = client.ask_goal(PROMPT['candidates'], PROMPT['viewer'], CTX, {'model_calls': 0})
        self.assertIsNone(answer)
        self.assertEqual(rec['outcome'], 'error')

    def test_timeouts_still_open_the_circuit_breaker(self):
        client = ModelClient(self.provider(error=self.sdk.APITimeoutError()))
        for _ in range(ModelClient.BREAKER_AFTER_TIMEOUTS):
            client.ask_goal(PROMPT['candidates'], PROMPT['viewer'], CTX, {'model_calls': 0})
        _, rec = client.ask_goal(PROMPT['candidates'], PROMPT['viewer'], CTX, {'model_calls': 0})
        self.assertEqual(rec['outcome'], 'skipped_circuit_open')

    def test_missing_sdk_gives_a_clear_error(self):
        sys.modules['anthropic'] = None   # makes `import anthropic` raise ImportError
        with self.assertRaises(RuntimeError) as cm:
            ClaudeProvider()
        self.assertIn('pip install anthropic', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
