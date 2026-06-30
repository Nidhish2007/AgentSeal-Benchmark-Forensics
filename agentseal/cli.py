"""AgentSeal CLI — Typer-based command-line interface."""

from __future__ import annotations

from pathlib import Path
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .engine import AgentSealEngine
from .loaders import load_swebench_verified, load_swebench_sample
from .report import write_html, write_json, write_markdown
from .schemas import AuditConfig

app = typer.Typer(
    name="agentseal",
    help="AgentSeal — open-source contamination auditor for AI agent benchmarks.",
    no_args_is_help=False,
    rich_markup_mode="rich",
    add_completion=False,
    invoke_without_command=True,
)
console = Console()


@app.callback()
def main_callback(
    ctx: typer.Context,
) -> None:
    """AgentSeal — open-source contamination auditor for AI agent benchmarks.

    Running `agentseal` with no subcommand launches interactive mode.
    """
    if ctx.invoked_subcommand is not None:
        return
    # No subcommand → launch the TUI
    from .tui import run_tui
    run_tui()


@app.command()
def audit(
    benchmark: str = typer.Option(
        "swe-bench-verified",
        "--benchmark", "-b",
        help="Benchmark to audit (currently only 'swe-bench-verified' is supported).",
    ),
    data: Path = typer.Option(
        None,  # None = auto-detect bundled data
        "--data", "-d",
        help="Path to the SWE-bench Verified parquet file. If omitted, auto-detects.",
    ),
    sample: int = typer.Option(
        0,
        "--sample", "-n",
        help="Audit only the first N instances (0 = all). Useful for quick tests.",
    ),
    threshold: float = typer.Option(
        0.82,
        "--threshold", "-t",
        help="Near-duplicate Jaccard threshold.",
    ),
    out: Path = typer.Option(
        Path("examples/reports/agentseal_audit.json"),
        "--out", "-o",
        help="Output JSON report path.",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Disable GitHub PR diff caching (re-fetches all PRs).",
    ),
    no_independent_search: bool = typer.Option(
        False,
        "--no-independent-search",
        help="Skip independent-source GitHub code search (faster, but no cross-repo verification).",
    ),
    no_codeseal: bool = typer.Option(
        False,
        "--no-codeseal",
        help="Skip bundled CodeSeal MinHash/LSH background content-overlap check.",
    ),
    model: str = typer.Option(
        "stack-v2",
        "--model",
        "-m",
        help="Model to audit against (temporal alignment). Presets: gpt-4, claude-3.5, gemini-2, llama-3, stack-v2, etc. Use 'none' to disable.",
    ),
    model_cutoff: str = typer.Option(
        None,
        "--model-cutoff",
        help="Custom training cutoff date (YYYY-MM-DD). Overrides --model.",
    ),
) -> None:
    """Run a contamination audit on an agent benchmark."""
    from .model_cutoffs import get_model_cutoff
    if model_cutoff:
        cutoff_date = model_cutoff
        model_name = f"custom ({model_cutoff})"
    else:
        result = get_model_cutoff(model)
        if result is None:
            available = ", ".join(sorted(get_model_cutoff.__module__ and __import__('agentseal.model_cutoffs', fromlist=['MODEL_CUTOFFS']).MODEL_CUTOFFS.keys()))
            raise typer.BadParameter(
                f"Unknown model: '{model}'. Available: {available}"
            )
        cutoff_date, model_name = result
    console.print(f"[dim]Temporal alignment: {model_name} (cutoff: {cutoff_date})[/dim]")
    # Auto-detect data file if not specified
    if data is None:
        from .loaders import find_data_file
        detected = find_data_file("swebench_verified.parquet")
        if detected is None:
            raise typer.BadParameter(
                "Could not find swebench_verified.parquet. Pass --data /path/to/file.parquet\n"
                "Download from: https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified"
            )
        data = detected
    elif not data.exists():
        raise typer.BadParameter(
            f"Data file not found: {data}\n"
            f"Download SWE-bench Verified from:\n"
            f"  https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified\n"
            f"Save as: {data}"
        )

    console.print(Panel.fit(
        f"[bold]AgentSeal v5.0.0[/bold]\n"
        f"[dim]Auditing {benchmark} for contamination[/dim]",
        border_style="bright_black",
    ))

    from .github_auth import has_token, has_hf_token
    if not has_token() or not has_hf_token():
        console.print("[dim]Recommended tokens:[/dim]")
        if not has_token():
            console.print("  [yellow]• GitHub token not set[/yellow] — set GITHUB_TOKEN env var or run /token paste in the TUI.")
            console.print("    [dim]Unlocks 5000/hr API + code search (60/hr unauthenticated).[/dim]")
            console.print("    [dim]Get one: https://github.com/settings/tokens (classic, read-only)[/dim]")
        if not has_hf_token():
            console.print("  [yellow]• HuggingFace token not set[/yellow] — set HF_TOKEN env var or run /hf paste in the TUI.")
            console.print("    [dim]Needed for gated datasets (e.g. Multi-SWE-bench).[/dim]")
            console.print("    [dim]Get one: https://huggingface.co/settings/tokens (Read type)[/dim]")
        console.print("")

    # Load instances
    if sample > 0:
        instances = load_swebench_sample(data, n=sample)
        console.print(f"[green]Loaded {len(instances)} instances (sample)[/green]")
    else:
        instances = load_swebench_verified(data)
        console.print(f"[green]Loaded {len(instances)} instances[/green]")

    config = AuditConfig(
        benchmark=benchmark,
        corpus_source="github-pr-diffs",
        threshold=threshold,
        sample_size=sample,
        model_cutoff=cutoff_date,
        model_name=model_name,
    )

    # Auto-skip independent search for tiny smoke tests only when no GitHub
    # token is configured. If the user submitted a token, keep the pipeline
    # wired end-to-end even for sample<=10 so evidence/report links can be
    # validated on small runs.
    do_independent_search = not no_independent_search
    if do_independent_search and 0 < sample <= 10 and not has_token():
        console.print(f"[dim]Small sample ({sample} ≤ 10) and no GitHub token — skipping independent search.[/dim]")
        do_independent_search = False

    # Progress callback
    def on_progress(phase: str, completed: int, total: int, message: str) -> None:
        if completed % 10 == 0 or completed == total:
            console.print(f"  [dim]{phase:20s} {completed}/{total}[/dim]  {message}")

    def on_evidence(instance_id: str, risk, match_type, similarity: float, message: str) -> None:
        # Only print high-severity findings inline
        from .schemas import RiskLevel
        from rich.markup import escape as _rich_escape
        if risk in (RiskLevel.CRITICAL, RiskLevel.HIGH):
            color = "red" if risk == RiskLevel.CRITICAL else "yellow"
            # SECURITY: escape user-controlled fields (instance_id, message)
            # to prevent rich markup injection. Without this, an instance_id
            # containing [/red] would raise MarkupError and crash the audit.
            safe_id = _rich_escape(str(instance_id))
            safe_msg = _rich_escape(str(message))
            console.print(f"  [{color}]{safe_id}[/{color}]  {safe_msg}")

    engine = AgentSealEngine(
        instances=instances,
        config=config,
        on_progress=on_progress,
        on_evidence=on_evidence,
        independent_search=do_independent_search,
        codeseal=not no_codeseal,
    )

    if no_cache:
        from .github_fetch import clear_cache
        clear_cache()

    console.print("[bold]Starting audit…[/bold]")
    report = engine.run()

    # Write reports
    json_path = write_json(report, out)
    md_path = out.with_suffix(".md")
    html_path = out.with_suffix(".html")
    write_markdown(report, md_path)
    write_html(report, html_path)

    # Print summary
    console.print()
    _print_summary(report)
    console.print()
    console.print("[bold]Reports written:[/bold]")
    console.print(f"  [green]json     [/green] {json_path}")
    console.print(f"  [green]markdown [/green] {md_path}")
    console.print(f"  [green]html     [/green] {html_path}")


def _print_summary(report) -> None:
    s = report.summary
    table = Table(title="AgentSeal audit summary", show_header=True, header_style="bold")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Total instances", str(s.total_instances))
    table.add_row("Patch exposed", f"{s.instances_with_patch_exposure} ({s.patch_exposure_rate*100:.1f}%)")
    table.add_row("Problem statement exposed", f"{s.instances_with_problem_statement_exposure} ({s.problem_statement_exposure_rate*100:.1f}%)")
    table.add_row("Test patch exposed", f"{s.instances_with_test_patch_exposure} ({s.test_patch_exposure_rate*100:.1f}%)")
    table.add_row("Repos in training corpus", f"{s.instances_with_repo_in_corpus} ({s.repo_in_corpus_rate*100:.1f}%)")
    table.add_row("CodeSeal content matches", f"{s.instances_with_codeseal_content} ({s.codeseal_content_rate*100:.1f}%)")
    table.add_row("Contamination rate", f"{s.contamination_rate*100:.2f}%", style="bold red")
    table.add_row("Critical", str(s.critical_count), style="red")
    table.add_row("High", str(s.high_count), style="red")
    table.add_row("Medium", str(s.medium_count), style="yellow")
    table.add_row("Low", str(s.low_count), style="cyan")
    table.add_row("Clean", str(s.clean_count), style="dim")
    console.print(table)


@app.command("auto")
def auto(
    benchmark: str = typer.Argument(..., help="Known benchmark name or HuggingFace dataset ID."),
    sample: int = typer.Option(
        0,
        "--sample", "-n",
        help="Audit a deterministic stratified sample of N instances (0 = all).",
    ),
    out: Path = typer.Option(
        Path("examples/reports/agentseal_auto.json"),
        "--out", "-o",
        help="Output JSON report path.",
    ),
    no_independent_search: bool = typer.Option(
        False,
        "--no-independent-search",
        help="Skip GitHub independent-source verification.",
    ),
    no_codeseal: bool = typer.Option(
        False,
        "--no-codeseal",
        help="Skip bundled CodeSeal MinHash/LSH background content-overlap check.",
    ),
    max_workers: int = typer.Option(
        10,
        "--max-workers",
        help="Parallel workers for source PR fetching.",
    ),
) -> None:
    """Discover, download, normalize, and audit a benchmark automatically."""
    from .auto_discover import run_auto

    def on_auto_progress(stage: str, message: str) -> None:
        console.print(f"  [dim]{stage:12s}[/dim] {message}")

    result = run_auto(benchmark, sample_size=sample, progress_callback=on_auto_progress)
    info = result["benchmark_info"]
    instances = result["instances"]

    console.print(Panel.fit(
        f"[bold]AgentSeal v5.0.0 — Auto Audit[/bold]\n"
        f"[dim]{info.name} · {len(instances)} instance(s) · {result['audit_type']}[/dim]",
        border_style="bright_black",
    ))

    config = AuditConfig(
        benchmark=info.name,
        corpus_source=f"auto-discovered {result['audit_type']} ({info.hf_id})",
        sample_size=sample,
        audit_type=result["audit_type"],
    )

    def on_progress(phase: str, completed: int, total: int, message: str) -> None:
        if completed % 10 == 0 or completed == total:
            console.print(f"  [dim]{phase:20s} {completed}/{total}[/dim]  {message}")

    engine = AgentSealEngine(
        instances=instances,
        config=config,
        on_progress=on_progress,
        independent_search=not no_independent_search,
        max_workers=max_workers,
        codeseal=not no_codeseal,
    )
    report_obj = engine.run()

    json_path = write_json(report_obj, out)
    md_path = out.with_suffix(".md")
    html_path = out.with_suffix(".html")
    write_markdown(report_obj, md_path)
    write_html(report_obj, html_path)

    _print_summary(report_obj)
    console.print()
    console.print("[bold]Reports written:[/bold]")
    console.print(f"  [green]json     [/green] {json_path}")
    console.print(f"  [green]markdown [/green] {md_path}")
    console.print(f"  [green]html     [/green] {html_path}")


@app.command()
def report(
    input: Path = typer.Option(..., "--input", "-i", help="Input JSON report path."),
    format: str = typer.Option("markdown", "--format", "-f", help="Output format: markdown or html."),
    out: Path = typer.Option(..., "--out", "-o", help="Output report path."),
) -> None:
    """Convert a JSON report to Markdown or HTML."""
    from .report import read_json
    fmt = format.lower()
    if fmt not in {"markdown", "html"}:
        raise typer.BadParameter(f"Unsupported format: {fmt}. Use 'markdown' or 'html'.")
    if not input.exists():
        raise typer.BadParameter(f"Input file not found: {input}")
    r = read_json(input)
    if fmt == "markdown":
        p = write_markdown(r, out)
    else:
        p = write_html(r, out)
    console.print(f"[green]{fmt} report written:[/green] {p}")


@app.command("audit-pro")
def audit_pro(
    data: Path = typer.Option(
        None,  # None = auto-detect bundled data
        "--data", "-d",
        help="Path to the SWE-bench Pro parquet file. If omitted, auto-detects.",
    ),
    sample: int = typer.Option(
        0,
        "--sample", "-n",
        help="Audit only the first N instances (0 = all).",
    ),
    out: Path = typer.Option(
        Path("examples/reports/swebench_pro_audit.json"),
        "--out", "-o",
        help="Output JSON report path.",
    ),
    no_independent_search: bool = typer.Option(
        False,
        "--no-independent-search",
        help="Skip independent-source GitHub code search (faster, but no cross-repo verification).",
    ),
    no_codeseal: bool = typer.Option(
        False,
        "--no-codeseal",
        help="Skip bundled CodeSeal MinHash/LSH background content-overlap check.",
    ),
) -> None:
    """Audit SWE-bench Pro for main-branch contamination.

    For each instance, AgentSeal fetches the source file at base_commit
    (before state) AND at HEAD/main (after state), then checks which
    gold-patch fix lines appear at HEAD but NOT at base_commit. Only
    those lines count as contamination.
    """
    # Auto-detect data file if not specified
    if data is None:
        from .loaders import find_data_file
        detected = find_data_file("swebench_pro.parquet")
        if detected is None:
            raise typer.BadParameter(
                "Could not find swebench_pro.parquet. Pass --data /path/to/file.parquet\n"
                "Download from: https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro"
            )
        data = detected
    elif not data.exists():
        raise typer.BadParameter(
            f"Data file not found: {data}\n"
            f"Download SWE-bench Pro from:\n"
            "  https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro"
        )

    console.print(Panel.fit(
        f"[bold]AgentSeal v5.0.0 — SWE-bench Pro Audit[/bold]\n"
        f"[dim]Checking if gold patch solutions are in public main branches[/dim]",
        border_style="bright_black",
    ))

    from .pro_audit import audit_swebench_pro, results_to_report

    def on_progress(current: int, total: int, msg: str) -> None:
        console.print(f"  [dim][{current}/{total}][/dim]  {msg}")

    console.print(f"[bold]Auditing {'all' if sample == 0 else f'first {sample}'} instances…[/bold]")
    results = audit_swebench_pro(
        data, sample_size=sample, on_progress=on_progress,
        independent_search=not no_independent_search,
    )

    console.print("\n[bold]Building report…[/bold]")
    report = results_to_report(results, codeseal=not no_codeseal)

    json_path = write_json(report, out)
    md_path = out.with_suffix(".md")
    html_path = out.with_suffix(".html")
    write_markdown(report, md_path)
    write_html(report, html_path)

    _print_summary(report)
    console.print()
    console.print("[bold]Reports written:[/bold]")
    console.print(f"  [green]json     [/green] {json_path}")
    console.print(f"  [green]markdown [/green] {md_path}")
    console.print(f"  [green]html     [/green] {html_path}")


if __name__ == "__main__":
    app()
