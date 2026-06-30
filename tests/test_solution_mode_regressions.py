from agentseal.engine import AgentSealEngine
from agentseal.report import write_html, write_markdown
from agentseal.schemas import AuditConfig, BenchmarkInstance


def test_solution_mode_without_pr_diff_is_not_marked_not_evaluated(tmp_path):
    instance = BenchmarkInstance(
        instance_id="HumanEval/0",
        repo="unknown/repo",
        patch=(
            "def has_close_elements(numbers, threshold):\n"
            "    for idx, left in enumerate(numbers):\n"
            "        for right in numbers[idx + 1:]:\n"
            "            if abs(left - right) < threshold:\n"
            "                return True\n"
            "    return False\n"
        ),
        problem_statement="Return True if any two numbers are closer than threshold.",
    )
    config = AuditConfig(
        benchmark="humaneval",
        corpus_source="auto-discovered (openai/openai_humaneval)",
        sample_size=1,
        audit_type="solution",
    )

    progress_events = []
    report = AgentSealEngine(
        instances=[instance],
        config=config,
        on_progress=lambda phase, _completed, _total, message: progress_events.append((phase, message)),
        fetcher=lambda _repo, _pr_number: None,
        independent_search=False,
        codeseal=False,
    ).run()

    assert report.config.audit_type == "solution"
    assert report.summary.total_instances == 1
    assert report.summary.instances_not_evaluated == 0
    assert report.instance_risks[0].not_evaluated is False
    assert not any(phase == "fetching PR diffs" for phase, _message in progress_events)
    assert any(phase == "source baseline" for phase, _message in progress_events)

    md = write_markdown(report, tmp_path / "solution.md").read_text(encoding="utf-8")
    html = write_html(report, tmp_path / "solution.html").read_text(encoding="utf-8")
    combined = md + "\n" + html

    assert "solution-mode audit path" in combined
    assert "no source PR diff baseline" in combined
    assert "Gold patch solutions are publicly available in their source GitHub repositories" not in combined
    forbidden_structural_phrase = "gold patches ARE the merged" + " PR diffs"
    assert forbidden_structural_phrase not in combined
    assert "regular audit path" not in combined
