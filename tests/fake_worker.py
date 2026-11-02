"""Fake agent CLI for routing tests: succeeds, fails or hits quota on demand, with no real CLI or network.

Configure by flags or environment variables (flags win over env; env wins over defaults):

    --mode    FAKE_WORKER_MODE    ok | fail | quota | auth | timeout | no_change   (default ok)
    --file    FAKE_WORKER_FILE    file written in the cwd by `ok`                   (default out.txt)
    --text    FAKE_WORKER_TEXT    text written to that file                         (default "hi\\n")
    --exit    FAKE_WORKER_EXIT    exit code                    (default per mode: 0, 3, 1, 1, 0, 0)
    --stderr  FAKE_WORKER_STDERR  message printed to stderr    (default per mode, e.g. a quota line)
    --sleep   FAKE_WORKER_SLEEP   seconds `timeout` sleeps                          (default 30)

Run it as a subprocess (`python fake_worker.py --mode quota`, or `python -m fake_worker` from tests/),
through `FakeWorkerHarness(...).argv()` / `.install(policy, worker)`, or in-process with `.run()`.
Extra positional arguments (the router's `-` / prompt-file tail) are accepted and ignored; the prompt
on stdin is read so the router's pipe never blocks.

This module never imports the router or `helpers`, so running it as a subprocess has no side effects.
"""
import argparse
import io
import os
from pathlib import Path
import sys
import time

PATH = str(Path(__file__).resolve())
MODES = ('ok', 'fail', 'quota', 'auth', 'timeout', 'no_change')
DEFAULT_EXIT = {'ok': 0, 'fail': 3, 'quota': 1, 'auth': 1, 'timeout': 0, 'no_change': 0}
DEFAULT_STDERR = {
    'fail': 'fake worker: task failed',
    'quota': "Usage limit reached. You've hit your usage limit; try again later.",
    'auth': 'Failed to authenticate: OAuth session expired and could not be refreshed',
}
ENV = {'mode': 'FAKE_WORKER_MODE', 'file': 'FAKE_WORKER_FILE', 'text': 'FAKE_WORKER_TEXT',
       'exit': 'FAKE_WORKER_EXIT', 'stderr': 'FAKE_WORKER_STDERR', 'sleep': 'FAKE_WORKER_SLEEP'}


def settings(argv=None, env=None):
    """Resolve flags > env > defaults into a dict (mode, file, text, exit, stderr, sleep)."""
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog='fake_worker', add_help=False)
    for key in ENV:
        parser.add_argument('--' + key, default=None)
    known, _ = parser.parse_known_args([] if argv is None else list(argv))
    raw = {key: getattr(known, key) if getattr(known, key) is not None else env.get(name)
           for key, name in ENV.items()}
    mode = raw['mode'] or 'ok'
    if mode not in MODES:
        raise ValueError(f'unknown FAKE_WORKER_MODE {mode!r}; expected one of {", ".join(MODES)}')
    return {'mode': mode, 'file': raw['file'] or 'out.txt',
            'text': 'hi\n' if raw['text'] is None else raw['text'],
            'exit': DEFAULT_EXIT[mode] if raw['exit'] in (None, '') else int(raw['exit']),
            'stderr': DEFAULT_STDERR.get(mode, '') if raw['stderr'] is None else raw['stderr'],
            'sleep': 30.0 if raw['sleep'] in (None, '') else float(raw['sleep'])}


def main(argv=None, env=None, stdin=None, stdout=None, stderr=None, cwd=None, sleep=time.sleep):
    """Behave like one worker run; returns the exit code. Defaults to the real process streams."""
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    opts = settings(argv, env)
    try:
        if not stdin.isatty():
            stdin.read()
    except (OSError, ValueError):
        pass
    mode = opts['mode']
    print(f'fake worker: mode={mode}', file=stdout, flush=True)
    if opts['stderr']:  # before any sleep, so a timed-out worker's output is still captured
        print(opts['stderr'], file=stderr, flush=True)
    if mode == 'ok':
        Path(cwd or '.', opts['file']).write_text(opts['text'])
    elif mode == 'timeout':
        sleep(opts['sleep'])
    return opts['exit']


class FakeWorkerHarness:
    """One configured fake worker: subprocess argv, policy installer, or in-process runner."""

    def __init__(self, mode='ok', file=None, text=None, exit=None, stderr=None, sleep=None):
        self.options = {'mode': mode, 'file': file, 'text': text, 'exit': exit, 'stderr': stderr, 'sleep': sleep}
        settings(self.flags(), env={})  # validate eagerly

    def flags(self):
        out = []
        for key, value in self.options.items():
            if value is not None:
                out += ['--' + key, str(value)]
        return out

    def argv(self, python=sys.executable):
        return [python, PATH, *self.flags()]

    def install(self, policy, worker='codex'):
        """Replace `worker`'s command in policy['run_task'], keeping the router's prompt-delivery tail."""
        commands = policy['run_task']['commands']
        tail = [a for a in commands.get(worker, []) if a in ('-', '{prompt_file}', '{prompt_pointer}')]
        commands[worker] = self.argv() + tail
        return self

    def run(self, cwd='.', stdin='', env=None, sleep=time.sleep):
        """In-process run; returns {'returncode', 'stdout', 'stderr'} like a finished subprocess."""
        out, err = io.StringIO(), io.StringIO()
        code = main(self.flags(), env={} if env is None else env, stdin=io.StringIO(stdin),
                    stdout=out, stderr=err, cwd=cwd, sleep=sleep)
        return {'returncode': code, 'stdout': out.getvalue(), 'stderr': err.getvalue()}


def fake_worker_argv(mode='ok', **options):
    return FakeWorkerHarness(mode, **options).argv()


if __name__ == '__main__':
    sys.exit(main())
