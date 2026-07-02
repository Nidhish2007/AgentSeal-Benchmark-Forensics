"""Runtime helpers for long AgentSeal audits."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def iter_bounded_unordered(
    items: Iterable[T],
    worker: Callable[[T], R],
    *,
    max_workers: int = 10,
    max_pending: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Iterator[tuple[T, R | None, BaseException | None]]:
    """Run work in a bounded thread pool and yield completed results.

    Large audits can contain thousands of instances. Submitting every item to a
    ThreadPoolExecutor at once creates thousands of Future objects and callback
    closures before any results are consumed. This helper keeps only a bounded
    number of pending futures alive and returns exceptions as data so one bad
    item does not crash the whole audit.
    """
    workers = max(1, int(max_workers or 1))
    pending_limit = max(workers, int(max_pending or workers * 3))
    iterator = iter(items)
    pending = {}
    stopped = False

    def cancelled() -> bool:
        try:
            return bool(cancel_check and cancel_check())
        except Exception:
            return False

    def submit_until_full(executor: ThreadPoolExecutor) -> None:
        nonlocal stopped
        while not stopped and len(pending) < pending_limit and not cancelled():
            try:
                item = next(iterator)
            except StopIteration:
                stopped = True
                return
            pending[executor.submit(worker, item)] = item

    with ThreadPoolExecutor(max_workers=workers) as executor:
        submit_until_full(executor)
        while pending:
            if cancelled():
                for future in pending:
                    future.cancel()
                return
            done, _not_done = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                item = pending.pop(future)
                try:
                    yield item, future.result(), None
                except BaseException as exc:
                    yield item, None, exc
            submit_until_full(executor)


__all__ = ["iter_bounded_unordered"]
