# Learnings

Short, one-topic lessons from past DLR jobs so fixed failures never cost twice.

- **Test isolation (`test_security` / `pgrep`):** Run full test suites one at a time; concurrent test processes on the same machine can collide on process-group markers.
- **Leak-check fixtures:** Build fake token strings in tests at runtime via concatenation (`'sk-' + 'a' * 30`) and avoid `UPPER_SNAKE` variable names containing `TOKEN`/`KEY`/`SECRET` so `llm-router scan` and `test_leak_rules.py` stay clean.
- **Subclassing `JobTests`:** Keep `setUp`, `tearDown`, `select`, `worker`, and `job` directly on `JobTests.__dict__` because `tests/test_handoff.py` binds them via `vars(test_job_dispatch.JobTests)`.
- **Bytecode cache hygiene:** Untracked `__pycache__/*.pyc` files are removed before tests and excluded from `changed_paths` so interpreter caches never trigger false out-of-scope failures.
