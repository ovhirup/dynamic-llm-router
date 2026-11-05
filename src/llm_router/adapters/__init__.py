"""Framework-free hooks for driving the existing run-job phases."""

from typing import Callable, ParamSpec, Protocol, TypeVar

P = ParamSpec('P')
T = TypeVar('T')


class Orchestrator(Protocol):
    """Run DLR operations synchronously and return their results.

    The operation and its arguments belong to the active job. Call it once
    within the hook and preserve its result and exceptions. DLR owns the order
    of phases, validation, and worktree lifetime.
    """

    def plan(self, operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Prepare repository context for the first worker attempt."""
        ...

    def dispatch(self, operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Run the initial worker cycle."""
        ...

    def test(self, operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Run a router test command, including a fail-first baseline when required."""
        ...

    def judge(self, operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Prepare and perform the stronger-model review when required."""
        ...

    def fix(self, operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        """Run a correction or retry cycle with DLR's feedback."""
        ...


class NativeOrchestrator:
    """Execute every phase directly, with no framework dependency."""

    @staticmethod
    def _invoke(operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        return operation(*args, **kwargs)

    plan = dispatch = test = judge = fix = _invoke
