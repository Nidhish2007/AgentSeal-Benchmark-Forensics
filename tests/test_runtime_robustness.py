from agentseal.runtime import iter_bounded_unordered
from agentseal.engine import AgentSealEngine
from agentseal.loaders import BenchmarkInstance
from agentseal.schemas import AuditConfig


def test_bounded_executor_yields_exceptions_without_stopping():
    def worker(value):
        if value == 2:
            raise ValueError("boom")
        return value * 10

    rows = list(iter_bounded_unordered(range(5), worker, max_workers=2, max_pending=2))

    assert sorted(item for item, _result, _exc in rows) == [0, 1, 2, 3, 4]
    assert sorted(result for _item, result, exc in rows if exc is None) == [0, 10, 30, 40]
    assert any(item == 2 and isinstance(exc, ValueError) for item, _result, exc in rows)


def test_bounded_executor_respects_cancel_check():
    seen = []

    def worker(value):
        seen.append(value)
        return value

    def cancel_check():
        return len(seen) >= 3

    rows = list(iter_bounded_unordered(range(100), worker, max_workers=2, max_pending=2, cancel_check=cancel_check))

    assert len(rows) <= 4
    assert len(seen) < 100


def test_engine_survives_fetcher_failures():
    instances = [
        BenchmarkInstance(
            instance_id=f"repo__demo-{i}",
            repo="repo/demo",
            patch="diff --git a/a.py b/a.py\n+print('x')\n",
            pr_number=i,
            pr_url=f"https://github.com/repo/demo/pull/{i}",
        )
        for i in range(1, 6)
    ]

    def broken_fetcher(_repo, _pr_number):
        raise RuntimeError("network broke")

    report = AgentSealEngine(
        instances=instances,
        config=AuditConfig(benchmark="robustness-test"),
        fetcher=broken_fetcher,
        independent_search=False,
        codeseal=False,
        max_workers=2,
    ).run()

    assert report.summary.total_instances == 5
    assert report.summary.instances_not_evaluated == 5
    assert len(report.instance_risks) == 5


def test_engine_survives_independent_search_failure(monkeypatch):
    import agentseal.independent_search as independent_search

    def broken_search(*_args, **_kwargs):
        raise RuntimeError("search backend broke")

    monkeypatch.setattr(independent_search, "search_independent_sources", broken_search)
    instances = [
        BenchmarkInstance(
            instance_id="repo__demo-1",
            repo="repo/demo",
            patch="diff --git a/a.py b/a.py\n+print('x')\n",
            pr_number=1,
            pr_url="https://github.com/repo/demo/pull/1",
        )
    ]

    report = AgentSealEngine(
        instances=instances,
        config=AuditConfig(benchmark="search-failure-test"),
        fetcher=lambda _repo, _pr: "diff --git a/a.py b/a.py\n+print('x')\n",
        independent_search=True,
        codeseal=False,
        max_workers=1,
    ).run()

    assert report.summary.total_instances == 1
    assert len(report.instance_risks) == 1


def test_large_engine_run_skips_merge_date_fetch_by_default(monkeypatch):
    import agentseal.github_fetch as github_fetch

    calls = []

    def merge_date_fetch(_repo, _pr):
        calls.append(_pr)
        return "2024-01-01T00:00:00Z"

    monkeypatch.setattr(github_fetch, "fetch_pr_merge_date", merge_date_fetch)
    monkeypatch.delenv("AGENTSEAL_FETCH_MERGE_DATES", raising=False)
    instances = [
        BenchmarkInstance(
            instance_id=f"repo__demo-{i}",
            repo="repo/demo",
            patch="diff --git a/a.py b/a.py\n+print('x')\n",
            pr_number=i,
            pr_url=f"https://github.com/repo/demo/pull/{i}",
        )
        for i in range(1, 251)
    ]

    report = AgentSealEngine(
        instances=instances,
        config=AuditConfig(benchmark="large-no-merge-dates"),
        fetcher=lambda _repo, _pr: "diff --git a/a.py b/a.py\n+print('x')\n",
        independent_search=False,
        codeseal=False,
        max_workers=4,
    ).run()

    assert report.summary.total_instances == 250
    assert calls == []


def test_large_engine_run_can_force_merge_date_fetch(monkeypatch):
    import agentseal.github_fetch as github_fetch

    calls = []

    def merge_date_fetch(_repo, _pr):
        calls.append(_pr)
        return "2024-01-01T00:00:00Z"

    monkeypatch.setattr(github_fetch, "fetch_pr_merge_date", merge_date_fetch)
    monkeypatch.setenv("AGENTSEAL_FETCH_MERGE_DATES", "1")
    instances = [
        BenchmarkInstance(
            instance_id=f"repo__demo-{i}",
            repo="repo/demo",
            patch="diff --git a/a.py b/a.py\n+print('x')\n",
            pr_number=i,
            pr_url=f"https://github.com/repo/demo/pull/{i}",
        )
        for i in range(1, 6)
    ]

    report = AgentSealEngine(
        instances=instances,
        config=AuditConfig(benchmark="large-force-merge-dates"),
        fetcher=lambda _repo, _pr: "diff --git a/a.py b/a.py\n+print('x')\n",
        independent_search=False,
        codeseal=False,
        max_workers=2,
    ).run()

    assert report.summary.total_instances == 5
    assert sorted(calls) == [1, 2, 3, 4, 5]
