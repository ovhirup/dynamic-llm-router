"""Tests for Jev decision layer prototype (#59): task classify + escalate-by-confidence behind interface."""
import io
import json
import unittest

from llm_router import jev


class FakeResponse:
    def __init__(self, payload):
        self._buf = io.BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.requests = []

    def open(self, request, timeout=10):
        self.requests.append(request.data.decode('utf-8'))
        if self.error:
            raise self.error
        return FakeResponse(self.payload)


class JevDecisionLayerTests(unittest.TestCase):
    def test_heuristic_layer_classifies_and_escalates_on_low_confidence(self):
        bug = jev.classify_and_decide(
            'Fix crash bug and regression traceback in parser.py when input fails',
            confidence_threshold=0.70,
        )
        self.assertEqual(bug['task_class'], 'bugfix')
        self.assertEqual(bug['size'], 'full')
        self.assertEqual(bug['source'], 'heuristic')
        self.assertGreaterEqual(bug['confidence'], 0.70)
        self.assertEqual(bug['action'], 'auto_proceed')
        self.assertFalse(bug['escalated'])

        vague = jev.classify_and_decide('Check something maybe', confidence_threshold=0.70)
        self.assertLess(vague['confidence'], 0.70)
        self.assertEqual(vague['action'], 'escalate')
        self.assertTrue(vague['escalated'])

    def test_jev_client_redacts_secrets_and_falls_back_on_error(self):
        secret = 'sk-' + 'n' * 30
        opener = FakeOpener(payload={
            'choice': 'review',
            'confidence': 0.88,
            'probabilities': {'review': 0.88, 'feature': 0.12},
        })
        layer = jev.JevClientDecisionLayer(endpoint='http://127.0.0.1:9999/decide', opener=opener)
        res = jev.classify_and_decide(f'Review PR diff with key {secret}', layer=layer, confidence_threshold=0.75)
        self.assertEqual(res['source'], 'jev')
        self.assertEqual(res['task_class'], 'review')
        self.assertEqual(res['action'], 'auto_proceed')
        self.assertNotIn(secret, opener.requests[0])

        failing_opener = FakeOpener(error=OSError('connection refused'))
        fallback_layer = jev.JevClientDecisionLayer(endpoint='http://127.0.0.1:9999/decide', opener=failing_opener)
        fallback_res = jev.classify_and_decide('Fix bug crash in parser', layer=fallback_layer)
        self.assertEqual(fallback_res['source'], 'heuristic')
        self.assertEqual(fallback_res['task_class'], 'bugfix')


if __name__ == '__main__':
    unittest.main()
