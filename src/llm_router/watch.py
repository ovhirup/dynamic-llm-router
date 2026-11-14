"""`llm-router watch` (#288): a terminal view of every running job, side by side, with its gates.

Read-only: it renders `live_jobs_feed.build_live_jobs_feed()` and never starts, stops or approves anything.
`render(feed, width)` is a pure function; `run()` redraws it every `interval` seconds until Ctrl-C.
Plain ANSI only, and none at all when stdout is not a terminal.
"""
import shutil
import sys
import time

from . import live_jobs_feed

WIDE = 100          # below this many columns, lanes stack vertically
LANE_MIN = 34       # narrowest side-by-side lane
LANE_EVENTS = 4     # timeline rows per lane
SEPARATOR = ' │ '
SYMBOLS = {'passed': '✓', 'failed': '✗', 'blocked': '✗', 'running': '…', 'pending': '·', 'waiting': '?',
           'n/a': '-'}
COLORS = {'passed': '32', 'failed': '31', 'blocked': '31', 'running': '33', 'waiting': '33'}
GATES = (('tests', 'tests'), ('leak_check', 'leak'), ('review', 'review'), ('approval', 'approval'))


_clean = live_jobs_feed.normalize_text  # one field; secrets and private terms are masked on each assembled row


def _fit(text, width):
    text = str(text or '')
    if width <= 0:
        return ''
    return text if len(text) <= width else text[:width - 1] + '…'


def _pad(text, width):
    text = _fit(text, width)
    return text + ' ' * (width - len(text))


def _elapsed(seconds):
    minutes, secs = divmod(int(seconds or 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m' if hours else f'{minutes}m{secs:02d}s'


def gate_strip(gates):
    """`tests ✓  leak ✓  review -  approval ?` for a job's gates."""
    return '  '.join(f'{label} {SYMBOLS.get(str(((gates or {}).get(key) or {}).get("state")), "?")}'
                     for key, label in GATES)


def _colorize(line, gates):
    for key, label in GATES:
        state = str(((gates or {}).get(key) or {}).get('state'))
        token = f'{label} {SYMBOLS.get(state, "?")}'
        if state in COLORS:
            line = line.replace(token, f'{label} \x1b[{COLORS[state]}m{SYMBOLS[state]}\x1b[0m', 1)
    return line


def _workers(job):
    chain = job.get('workers') or ([job['worker']] if job.get('worker') else [])
    return '→'.join(_clean(name) for name in chain) or 'choosing worker'


def lane(job, width, terms=()):
    """The lines of one running job's lane, each exactly `width` characters."""
    events = (job.get('events') or [])[-LANE_EVENTS:]
    rows = [f'{_clean(job.get("repo")) or "?"} · {_workers(job)} · {_elapsed(job.get("elapsed_seconds"))}',
            f'▶ {_clean(job.get("now") or job.get("state") or "starting")}']
    rows += [f'  {_clean(event["kind"]):<7} {_clean(event["text"])}' for event in events]
    rows += [''] * (LANE_EVENTS - len(events))
    rows.append(gate_strip(job.get('gates')))
    return [_pad(live_jobs_feed.mask_text(row, terms), width) for row in rows]


def _finished_lines(job, width, terms=()):
    gates = job.get('gates') or {}
    state = 'done' if job.get('state') == 'done' else 'failed'
    lines = [_fit(live_jobs_feed.mask_text(
        f'{SYMBOLS["passed"] if state == "done" else SYMBOLS["failed"]} {_clean(job.get("repo")) or "?"} '
        f'{_clean(job.get("task"))} · {_workers(job)} · {_clean(job.get("status") or state)} · '
        f'{gate_strip(gates)}', terms), width)]
    approval = gates.get('approval') or {}
    if approval.get('state') == 'waiting':  # never cut: the owner approves this exact SHA
        lines.append(live_jobs_feed.mask_text(f'  {_clean(approval.get("detail"))}', terms))
        if approval.get('command'):
            lines.append(live_jobs_feed.mask_text(f'  {_clean(approval["command"])}', terms))
    return lines


def select_job(feed, job_id):
    """The feed narrowed to one job id. A finished job is shown under Recent, however old it is."""
    pick = {section: [job for job in feed.get(section) or () if job.get('job_id') == job_id]
            for section in ('running', 'recent', 'history')}
    return {**feed, 'running': pick['running'], 'recent': pick['recent'] + pick['history'], 'history': []}


def render(feed, width=WIDE, color=False, terms=()):
    """One frame of the watch view as a string. Pure: same feed, width and terms, same text."""
    width = max(40, int(width))
    running, recent = feed.get('running') or [], feed.get('recent') or []
    header = live_jobs_feed.mask_text(f'DLR watch · {_clean(feed.get("updated_at"))} · {len(running)} running', terms)
    out = [_fit(header, width), '']
    lanes = [(job, job.get('gates')) for job in running]
    if not lanes:
        out.append('No running jobs.')
    elif width >= WIDE and len(lanes) > 1:
        per_row = max(1, min(len(lanes), (width + len(SEPARATOR)) // (LANE_MIN + len(SEPARATOR))))
        column = (width - len(SEPARATOR) * (per_row - 1)) // per_row
        for start in range(0, len(lanes), per_row):
            group = lanes[start:start + per_row]
            cells = [lane(job, column, terms) for job, _ in group]
            for index in range(len(cells[0])):
                parts = [cells[n][index] for n in range(len(group))]
                if color:
                    parts = [_colorize(part, gates) for part, (_, gates) in zip(parts, group)]
                out.append(SEPARATOR.join(parts).rstrip())
            out.append('')
    else:
        for job, gates in lanes:
            rows = lane(job, width, terms)
            out += [(_colorize(row, gates) if color else row).rstrip() for row in rows] + ['']
    finished = recent
    if finished:
        out += [_fit('Recent', width)]
        for job in finished:
            lines = _finished_lines(job, width, terms)
            out += [_colorize(line, job.get('gates')) if color else line for line in lines]
        out.append('')
    out.append(_fit('Ctrl-C to quit. Read-only: watch never starts, stops or approves anything.', width))
    # After lanes are joined: a private term can be formed only by the separator between two lanes.
    return '\n'.join(live_jobs_feed.final_mask(line, terms) for line in out)


def run(policy=None, once=False, interval=2.0, job_id=None, as_json=False, stream=None, sleep=time.sleep,
        width=None, build=None):
    """Draw frames until Ctrl-C (or one with `once`). Returns the exit code."""
    from . import task_dispatch as td  # local: the terms come from the policy's own file, never config here
    stream = stream or sys.stdout
    build = build or live_jobs_feed.build_live_jobs_feed
    tty = bool(getattr(stream, 'isatty', lambda: False)())
    terms = tuple(td.private_terms(policy) or ()) if policy is not None else ()
    try:
        while True:
            feed = live_jobs_feed.clean_obj(build(policy=policy), terms)
            if job_id:
                feed = select_job(feed, job_id)
            if as_json:
                stream.write(live_jobs_feed.emit_feed_json(feed) + '\n')
                return 0
            columns = width or shutil.get_terminal_size((WIDE, 40)).columns
            frame = render(feed, columns, color=tty, terms=terms)
            if tty and not once:
                stream.write('\x1b[H\x1b[2J')
            stream.write(frame + '\n')
            stream.flush()
            if once:
                return 0
            sleep(max(0.5, float(interval)))
    except KeyboardInterrupt:
        stream.write('\n')
        stream.flush()
        return 0
