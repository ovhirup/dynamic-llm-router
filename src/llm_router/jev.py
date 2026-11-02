"""Prototype Jev decision layer (#59): task classify + escalate-by-confidence behind an interface.

Parent: #57 (Jev decision layer spike).
Provides a clean `DecisionLayer` interface (`classify_and_decide(task_text, policy=None, ...)`):
  - Returns typed decisions:
    `{'task_class', 'size', 'confidence', 'action': 'auto_proceed'|'escalate', 'probabilities', 'source'}`
  - When `confidence < confidence_threshold` (default `0.70`), `action` becomes `'escalate'` so
    ambiguous or low-confidence tasks escalate to a stronger planner/orchestrator (`claude` / `grok-bot`)
    rather than auto-proceeding on a cheaper worker.
  - Implementations:
    1. `HeuristicDecisionLayer`: zero-cost, offline, deterministic classifier + calibrated confidence.
    2. `JevClientDecisionLayer`: calls a configured Jev-compatible HTTP endpoint (redacting secrets
       before send, no redirects/proxies) and automatically falls back to `HeuristicDecisionLayer`
       when disabled, unreachable, or unconfigured.
"""
import json
import os
import re
import urllib.request

from . import config, security

DEFAULT_CONFIDENCE_THRESHOLD = 0.70

CLASS_CUES = {
    'bugfix': ('fix', 'bug', 'error', 'crash', 'regression', 'fail', 'broken', 'traceback', 'exception'),
    'review': ('review', 'audit', 'inspect', 'verdict', 'approve', 'reject', 'critique'),
    'mechanical': ('rename', 'format', 'lint', 'typo', 'bump', 'cleanup', 'mechanical', 'move'),
    'draft': ('draft', 'wording', 'copy', 'summary', 'short note', 'rephrase'),
    'feature': ('add', 'implement', 'create', 'build', 'support', 'feature', 'introduce', 'prototype'),
}
WORD_RE = re.compile(r'[a-z][a-z0-9_-]{1,30}')


def _words(text):
    return WORD_RE.findall((text or '').lower())


class HeuristicDecisionLayer:
    """Zero-cost local heuristic decision layer with calibrated confidence."""

    def classify(self, task_text, draft_max_chars=4000):
        words = _words(task_text)
        word_set = set(words)
        raw_scores = {}
        for cls in config.TASK_CLASSES:
            cues = CLASS_CUES.get(cls, ())
            hits = sum(1 for cue in cues if cue in word_set)
            raw_scores[cls] = (1.5 * hits) + (0.4 if cls == 'feature' else 0.1)
        total = sum(raw_scores.values()) or 1.0
        probs = {cls: round(raw_scores[cls] / total, 4) for cls in config.TASK_CLASSES}
        best_class = max(config.TASK_CLASSES, key=lambda c: (probs[c], c == 'feature'))
        hit_count = sum(1 for cue in CLASS_CUES.get(best_class, ()) if cue in word_set)
        confidence = min(0.96, round(max(probs[best_class], 0.45 + 0.18 * hit_count), 4))
        if not words:
            confidence = 0.25
        size = 'draft' if (best_class == 'draft' or (len(task_text or '') <= draft_max_chars and 'draft' in word_set)) else 'full'
        return {
            'task_class': best_class,
            'size': size,
            'confidence': confidence,
            'probabilities': probs,
            'source': 'heuristic',
        }


class JevClientDecisionLayer:
    """Typed-decision client behind the same interface; falls back to `HeuristicDecisionLayer`."""

    def __init__(self, endpoint=None, api_key_env=None, opener=None, timeout=10, fallback=None):
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.opener = opener
        self.timeout = timeout
        self.fallback = fallback or HeuristicDecisionLayer()

    def classify(self, task_text, draft_max_chars=4000):
        if not self.endpoint:
            return self.fallback.classify(task_text, draft_max_chars=draft_max_chars)
        redacted_text = security.redact(str(task_text or ''))[:8000]
        headers = {'Content-Type': 'application/json'}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if key:
                headers['Authorization'] = 'Bearer ' + key
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps({'text': redacted_text, 'options': list(config.TASK_CLASSES)}).encode(),
            headers=headers,
            method='POST',
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = self.opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode('utf-8', 'replace'))
            choice = data.get('choice') or data.get('task_class')
            conf = float(data.get('confidence', 0.0))
            if choice not in config.TASK_CLASSES or not (0.0 <= conf <= 1.0):
                raise ValueError('invalid Jev response')
            probs = {
                cls: round(float((data.get('probabilities') or {}).get(cls, 0.0)), 4)
                for cls in config.TASK_CLASSES
            }
            size = data.get('size') if data.get('size') in ('draft', 'full') else (
                'draft' if choice == 'draft' else 'full'
            )
            return {
                'task_class': choice,
                'size': size,
                'confidence': round(conf, 4),
                'probabilities': probs,
                'source': 'jev',
            }
        except Exception:
            return self.fallback.classify(task_text, draft_max_chars=draft_max_chars)


def classify_and_decide(task_text, policy=None, layer=None, confidence_threshold=None):
    """Classify `task_text` and decide `'auto_proceed'` vs `'escalate'` by confidence threshold."""
    policy = policy or {}
    jev_cfg = policy.get('jev') or {}
    threshold = float(
        confidence_threshold if confidence_threshold is not None
        else jev_cfg.get('confidence_threshold', DEFAULT_CONFIDENCE_THRESHOLD)
    )
    if layer is None:
        if jev_cfg.get('enabled') and jev_cfg.get('endpoint'):
            layer = JevClientDecisionLayer(
                endpoint=jev_cfg.get('endpoint'),
                api_key_env=jev_cfg.get('api_key_env'),
            )
        else:
            layer = HeuristicDecisionLayer()
    draft_max = ((policy.get('developer') or {}).get('draft_max_chars', 4000))
    decision = dict(layer.classify(task_text, draft_max_chars=draft_max))
    conf = float(decision.get('confidence', 0.0))
    escalate = conf < threshold
    decision.update(
        confidence_threshold=round(threshold, 4),
        action='escalate' if escalate else 'auto_proceed',
        escalated=escalate,
    )
    return decision
