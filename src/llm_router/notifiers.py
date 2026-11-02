"""Notifier plugins (#78): redacted Slack and email summaries for jobs and daily meter digests.

All notifier plugins are opt-in (`enabled: False` by default), never store tokens or webhook URLs
in the repository or config, and redact every outgoing subject, body, and structured field through
`security.redact`.
"""
from . import security

SLACK_MANIFEST = {
    'name': 'slack-notifier',
    'capabilities': ['notifier'],
    'transport': 'mcp_stdio',
    'command': ['slack-mcp'],
    'description': 'Opt-in Slack summary notifier for finished router jobs and daily meter digests.',
    'enabled': False,
    'permissions': ['post_summary'],
}

EMAIL_MANIFEST = {
    'name': 'email-notifier',
    'capabilities': ['notifier'],
    'transport': 'mcp_stdio',
    'command': ['email-mcp'],
    'description': 'Opt-in email summary notifier for finished router jobs and daily meter digests.',
    'enabled': False,
    'permissions': ['send_summary'],
}


def format_job_summary(job):
    """Build a redacted Slack + email summary payload from a `run_job` result dict."""
    if not isinstance(job, dict):
        raise ValueError('job summary must be a dict')
    job_id = security.redact(str(job.get('job_id') or 'unknown-job'))
    status = security.redact(str(job.get('status') or ('committed' if job.get('ok') else 'failed')))
    workers = ', '.join(security.redact(str(w)) for w in (job.get('workers') or [])) or 'none'
    tests = security.redact(str(job.get('tests_status') or 'not_run'))
    branch = security.redact(str(job.get('branch') or 'none'))
    commit = security.redact(str(job.get('commit') or 'none')[:12])
    task = security.redact(str(job.get('task') or 'task'))
    subject = f'[DLR] {job_id}: {status} ({workers})'
    lines = [
        f'Job: {job_id} ({task})',
        f'Status: {status} | Workers: {workers} | Tests: {tests}',
        f'Branch: {branch} | Commit: {commit}',
    ]
    if job.get('warnings'):
        warnings = '; '.join(security.redact(str(w)) for w in job['warnings'])
        lines.append(f'Warnings: {warnings}')
    body = '\n'.join(lines)
    return security.redact_obj({
        'kind': 'job_summary',
        'job_id': job_id,
        'subject': subject,
        'slack_text': body,
        'email_body': body,
    })


def format_daily_summary(rows, open_alerts=()):
    """Build a redacted daily meter and alert digest for Slack and email."""
    meter_lines = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        worker = security.redact(str(row.get('worker') or '?'))
        used = security.redact(str(row.get('used') or 'unknown'))
        fresh = security.redact(str(row.get('freshness') or 'unknown'))
        meter_lines.append(f'- {worker}: {used} ({fresh})')
    alert_lines = [f'- {security.redact(str(a.get("message") if isinstance(a, dict) else a))}'
                   for a in (open_alerts or ())]
    parts = ['DLR Daily Meter Summary', '']
    parts += meter_lines or ['- No metered workers configured.']
    if alert_lines:
        parts += ['', 'Open alerts:'] + alert_lines
    body = '\n'.join(parts)
    return security.redact_obj({
        'kind': 'daily_summary',
        'subject': '[DLR] Daily meter & alert summary',
        'slack_text': body,
        'email_body': body,
    })


def dispatch_notification(manifest, payload, sender=None):
    """Send a redacted summary payload through an enabled notifier plugin; refuses disabled plugins."""
    if not isinstance(manifest, dict) or 'notifier' not in (manifest.get('capabilities') or ()):
        raise ValueError('manifest must declare the notifier capability')
    if not manifest.get('enabled', False):
        return {'sent': False, 'plugin': manifest.get('name'), 'reason': 'plugin_disabled'}
    clean_payload = security.redact_obj(payload)
    if sender is not None:
        response = sender(manifest['name'], clean_payload)
        return {'sent': True, 'plugin': manifest['name'], 'payload': clean_payload,
                'response': security.redact_obj(response)}
    return {'sent': True, 'plugin': manifest['name'], 'payload': clean_payload}
