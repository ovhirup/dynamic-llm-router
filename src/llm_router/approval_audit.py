"""Approval audit log (who/when/what) to job log and Obsidian vault (#85).

Parent: #81 (Notch actionable approvals). Related: #47, #48.
Whenever an approval or denial is recorded (`pr_gate.approve`, `pr_gate.deny`, or an external
surface such as DLR Notch / chat), records a redacted audit entry:
  - `who` (`actor`: e.g. `'owner'`, `'chat'`, `'notch'`, `'cli'`)
  - `when` (`at`: ISO-8601 timestamp)
  - `what` (`type`: `'approval'` | `'denial'`, `action`: `'push'` | `'merge'`, `job`, `sha`, `branch`, `note`)
into both:
  1. The job's JSON log (`<jobs_dir>/<job_id>.json` under `approval_audit: [...]`), and
  2. The Obsidian vault (`<vault_dir>/jobs/<job_id>-approvals.md` and `<vault_dir>/approvals-audit.md`).
"""
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path

from . import config, security


def _vault_dir(policy):
    return config.policy_path(policy or {}, 'run_job', 'vault_dir', 'vault')


def _jobs_dir(policy):
    return config.policy_path(policy or {}, 'run_job', 'jobs_dir', 'logs/jobs')


def record_approval_audit(policy, record, actor='owner', now=None):
    """Append a redacted approval/denial audit record to the job JSON log and the Obsidian vault."""
    if not isinstance(record, dict) or not record.get('job'):
        raise ValueError('approval record requires a job id')
    stamp = str(record.get('at') or (now or datetime.now(timezone.utc)).isoformat(timespec='seconds'))
    entry = security.redact_obj({
        'who': str(record.get('who') or actor or 'owner'),
        'when': stamp,
        'what': {
            'type': str(record.get('type') or 'approval'),
            'action': str(record.get('action') or 'push'),
            'job': str(record['job']),
            'sha': str(record.get('sha') or ''),
            'branch': str(record.get('branch') or ''),
            'note': str(record.get('note') or ''),
        },
    })

    # 1. Update job JSON log (<jobs_dir>/<job_id>.json) if present
    job_path = _jobs_dir(policy) / f"{entry['what']['job']}.json"
    if job_path.is_file():
        try:
            job_data = json.loads(job_path.read_text())
            if isinstance(job_data, dict):
                job_data.setdefault('approval_audit', []).append(entry)
                job_path.write_text(json.dumps(security.redact_obj(job_data), indent=2))
        except (OSError, json.JSONDecodeError):
            pass

    # 2. Append to Obsidian vault (<vault_dir>/approvals-audit.md and <vault_dir>/jobs/<job_id>-approvals.md)
    vault_root = _vault_dir(policy)
    vault_jobs = vault_root / 'jobs'
    vault_jobs.mkdir(parents=True, exist_ok=True)
    what = entry['what']
    line = (
        f"- **{entry['when']}** | who: `{entry['who']}` | what: `{what['type']}` `{what['action']}` "
        f"on `{what['job']}` (`{what['branch']}` @ `{what['sha'][:12]}`)"
        + (f" — {what['note']}" if what['note'] else '')
        + '\n'
    )
    for md_path, header in (
        (vault_root / 'approvals-audit.md', '# Approval Audit Log\n\n'),
        (vault_jobs / f"{what['job']}-approvals.md", f"# Approval Audit: {what['job']}\n\n"),
    ):
        with md_path.open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(header)
            handle.write(line)

    return entry
