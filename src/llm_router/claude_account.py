"""Which Claude account is Claude Code signed in to? (issue #22)

Reads only non-secret profile fields (emailAddress, organizationType) from ~/.claude.json's
`oauthAccount`. Never reads or returns tokens. Result: 'personal', 'work' or 'unknown'.
Only 'personal' may be routed to; work or unknown makes `claude` ineligible when the check is on.

Policy (opt-in):
  "claude_account_check": {"enabled": true,
                           "personal_emails": ["me@example.com"],          # exact matches, optional
                           "personal_domains": ["gmail.com", ...]}         # optional, defaults below
"""
import json
from pathlib import Path

DEFAULT_PERSONAL_DOMAINS = ('gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com',
                            'icloud.com', 'me.com', 'yahoo.com', 'proton.me', 'protonmail.com')


def default_path():
    return Path.home() / '.claude.json'


def read_profile(path=None):
    """{'email': str|None, 'org_type': str|None} from oauthAccount, or None if unreadable."""
    try:
        data = json.loads(Path(path or default_path()).read_text())
    except (OSError, ValueError):
        return None
    account = data.get('oauthAccount') if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    email = account.get('emailAddress')
    return {'email': email.strip().lower() if isinstance(email, str) and '@' in email else None,
            'org_type': account.get('organizationType') if isinstance(account.get('organizationType'), str) else None}


def classify(profile, options=None):
    options = options or {}
    if not profile or not profile.get('email'):
        return 'unknown'
    email = profile['email']
    emails = {e.lower() for e in options.get('personal_emails') or []}
    domains = {d.lower() for d in (options.get('personal_domains') or DEFAULT_PERSONAL_DOMAINS)}
    if email in emails or email.rsplit('@', 1)[1] in domains:
        return 'personal'
    return 'work'


def check(policy, profile_loader=None):
    """None when the check is off; else 'personal' | 'work' | 'unknown'."""
    options = (policy or {}).get('claude_account_check') or {}
    if not options.get('enabled'):
        return None
    profile = (profile_loader or (lambda: read_profile(options.get('path'))))()
    return classify(profile, options)
