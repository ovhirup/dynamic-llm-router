"""Run-job adapter contracts and the core dependency boundary."""

import ast
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from llm_router.adapters import NativeOrchestrator
import test_job_dispatch

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORKS = {'langgraph', 'crewai', 'autogen', 'agent_framework'}


class OrchestratorAdapterTests(unittest.TestCase):
    setUp = test_job_dispatch.JobTests.setUp
    tearDown = test_job_dispatch.JobTests.tearDown
    select = test_job_dispatch.JobTests.select
    worker = test_job_dispatch.JobTests.worker
    job = test_job_dispatch.JobTests.job

    def test_run_job_uses_adapter_for_each_phase(self):
        self.worker("open('n.txt', 'a').write('x\\n')")
        adapter = mock.Mock(wraps=NativeOrchestrator())
        reviewer = mock.Mock(return_value={'reviewer': 'claude', 'verdict': 'approve'})

        result = self.job(
            adapter=adapter,
            reviewer=reviewer,
            test_cmd='test "$(wc -l < n.txt)" -ge 2',
        )

        self.assertTrue(result['ok'], result)
        self.assertEqual([attempt['tests'] for attempt in result['attempts']], ['failed', 'passed'])
        self.assertEqual(
            [call[0] for call in adapter.mock_calls],
            ['plan', 'dispatch', 'test', 'fix', 'test', 'judge'],
        )
        reviewer.assert_called_once()
        self.assertEqual(result['review']['verdict'], 'approve')


class CoreImportTests(unittest.TestCase):
    def test_core_source_has_no_framework_imports(self):
        imports = []
        for path in sorted((ROOT / 'src' / 'llm_router').rglob('*.py')):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                imports.extend(
                    f'{path.relative_to(ROOT)}:{node.lineno} {name}'
                    for name in names
                    if name.split('.', 1)[0] in FRAMEWORKS
                )
        self.assertEqual(imports, [])

    def test_native_imports_work_with_frameworks_blocked(self):
        script = f"""
import sys
sys.path.insert(0, {str(ROOT / 'src')!r})
attempted = []

class BlockFrameworks:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] in {sorted(FRAMEWORKS)!r}:
            attempted.append(fullname)
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, BlockFrameworks())
import llm_router.cli
import llm_router.job_dispatch
from llm_router.adapters import NativeOrchestrator
assert not attempted, attempted
"""
        result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
