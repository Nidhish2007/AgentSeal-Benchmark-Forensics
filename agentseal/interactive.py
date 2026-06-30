"""AgentSeal interactive mode — Rich-based dashboard.

Launches when the user types `agentseal` with no subcommand.

Features:
- Auto-detects bundled SWE-bench data files
- Lets users load THEIR OWN benchmark data (parquet/JSONL)
- Runs audits with live progress bars
- Shows results in formatted tables
- Writes JSON/Markdown/HTML reports
- Opens HTML report in browser
"""

from __future__ import annotations

import os
import platform
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
    TimeElapsedColumn,
)
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table
from rich.text import Text

console = Console()

# Solid filled block wordmark (pyfiglet 'ansi_shadow' font) — heavy █████ blocks
LOGO_LINES = [
    " █████╗  ██████╗ ███████╗███╗   ██╗████████╗███████╗███████╗ █████╗ ██╗     ",
    "██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝██╔════╝██╔════╝██╔══██╗██║     ",
    "███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ███████╗█████╗  ███████║██║     ",
    "██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ╚════██║██╔══╝  ██╔══██║██║     ",
    "██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ███████║███████╗██║  ██║███████╗",
    "╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚══════╝╚═╝  ╚═╝╚══════╝",
    "                                                                            ",
    "  ◆━━  contamination auditor for ai agent benchmarks  ━━◆                  ",
]

LOGO = "\n".join(LOGO_LINES)

def find_data_files() -> dict[str, Path]:
    """Auto-detect SWE-bench data files using the shared find_data_file helper."""
    from .loaders import find_data_file
    files = {}
    targets = {
        "swe-bench-verified": "swebench_verified.parquet",
        "swe-bench-pro": "swebench_pro.parquet",
    }
    for name, filename in targets.items():
        p = find_data_file(filename)
        if p is not None:
            files[name] = p
    return files


def _render_logo_gradient() -> str:
    """Render the logo with a smooth per-character horizontal gradient.

    Renders every non-space character individually based on its column position, producing a smooth
    left-to-right color transition (cream → amber → orange) across the
    wordmark. Spaces stay transparent so the letter shapes pop.
    """
    # Three-stop gradient (cream → amber → deep orange)
    cream = (255, 235, 205)
    amber = (255, 170, 102)
    orange = (255, 140, 66)
    stops = [(0.0, cream), (0.5, amber), (1.0, orange)]

    def _interp(c1, c2, t):
        return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))

    def _color_at(t):
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                local = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
                return _interp(c0, c1, local)
        return stops[-1][1]

    max_len = max(len(line) for line in LOGO_LINES)
    out_lines = []
    for line in LOGO_LINES:
        padded = line.ljust(max_len)
        chars = []
        # Bold on for the whole line
        chars.append("\x1b[1m")
        current_code = None
        for i, ch in enumerate(padded):
            if ch == " ":
                if current_code is not None:
                    chars.append("\x1b[0m")
                    current_code = None
                chars.append(" ")
            else:
                t = i / max(1, max_len - 1)
                r, g, b = _color_at(t)
                code = f"\x1b[38;2;{r};{g};{b}m"
                if code != current_code:
                    chars.append(code)
                    current_code = code
                chars.append(ch)
        if current_code is not None:
            chars.append("\x1b[0m")
        out_lines.append("".join(chars))
    return "\n".join(out_lines)


def run_interactive():
    """Main interactive loop. Called when user types `agentseal` with no subcommand."""
    # Print logo with per-character gradient (cream → amber → orange)
    console.print()
    console.print(_render_logo_gradient(), markup=False, highlight=False)
    console.print()
    console.print(Text("  AgentSeal — Contamination Auditor for AI Agent Benchmarks", style="bold white"))
    console.print(Text("  Deterministic · Local · No AI API · No network calls to models", style="dim"))
    console.print()

    # Check for existing reports
    reports = {}
    for d in ["examples/reports", "reports", "."]:
        p = Path(d)
        if p.exists():
            for f in p.glob("*.json"):
                if "audit" in f.name.lower():
                    reports[f.stem] = f

    while True:
        data_files = find_data_files()

        # Build menu
        options = []
        if "swe-bench-pro" in data_files:
            options.append(("1", "Audit SWE-bench Pro (all instances)"))
        if "swe-bench-verified" in data_files:
            options.append(("2", "Audit SWE-bench Verified (all instances)"))
        options.append(("3", "Load YOUR OWN benchmark data (parquet/JSONL)"))
        if reports:
            options.append(("4", f"View existing report ({len(reports)} found)"))
        options.append(("q", "Quit"))

        console.print(Panel.fit(
            "[bold]What do you want to do?[/bold]",
            border_style="bright_black",
        ))
        for key, label in options:
            console.print(f"  [bold cyan]{key}[/bold cyan]  {label}")

        default = "1" if "1" in [o[0] for o in options] else "3"
        choice = Prompt.ask("\n  Choice", default=default)

        if choice == "1" and "swe-bench-pro" in data_files:
            _run_pro_audit(data_files["swe-bench-pro"])
        elif choice == "2" and "swe-bench-verified" in data_files:
            _run_verified_audit(data_files["swe-bench-verified"])
        elif choice == "3":
            _load_custom_data()
        elif choice == "4" and reports:
            _view_report(reports)
        elif choice in ("q", "quit", "exit"):
            console.print("\n[dim]Goodbye.[/dim]\n")
            break
        else:
            console.print("[yellow]Invalid choice.[/yellow]")
        console.print()


def _load_custom_data():
    """Let the user load their own benchmark data file."""
    console.print()
    console.print(Panel.fit(
        "[bold]Load Custom Benchmark Data[/bold]\n"
        "[dim]Audit YOUR OWN benchmark for contamination[/dim]",
        border_style="bright_black",
    ))
    console.print()
    console.print("[dim]You can drag and drop a file into this terminal to paste its path.[/dim]")
    console.print("[dim]Supported formats: .parquet, .jsonl[/dim]")
    console.print()

    # Get file path
    file_path = Prompt.ask("  Path to your benchmark file")
    file_path = file_path.strip().strip('"').strip("'").strip()

    if not file_path:
        console.print("[yellow]No path provided.[/yellow]")
        return

    p = Path(file_path).expanduser()
    if not p.exists():
        console.print(f"[red]File not found: {p}[/red]")
        return

    # Detect format
    ext = p.suffix.lower()
    if ext not in (".parquet", ".jsonl"):
        console.print(f"[red]Unsupported format: {ext}. Use .parquet or .jsonl[/red]")
        return

    console.print(f"[green]Found: {p}[/green]")

    # Ask what kind of audit to run
    console.print()
    console.print("[bold]What kind of audit?[/bold]")
    console.print("  [cyan]1[/cyan]  SWE-bench-style audit (compare patches against GitHub PRs)")
    console.print("  [cyan]2[/cyan]  SWE-bench Pro-style audit (check if fix code is in repo's default branch)")
    console.print("  [cyan]3[/cyan]  Custom text-overlap audit (compare against a reference corpus)")

    audit_choice = Prompt.ask("\n  Audit type", default="2")

    if audit_choice == "1":
        _run_custom_swebench_verified(p)
    elif audit_choice == "2":
        _run_custom_pro_audit(p)
    elif audit_choice == "3":
        console.print("[yellow]Custom text-overlap audit is coming in v0.2.[/yellow]")
        console.print("[dim]Standalone text-only benchmark auditing is experimental in this beta.[/dim]")
    else:
        console.print("[yellow]Invalid choice.[/yellow]")


def _run_custom_pro_audit(data_path: Path):
    """Run a SWE-bench Pro-style audit on custom data."""
    import pandas as pd

    # Verify the file has the right columns
    try:
        df = pd.read_parquet(data_path) if data_path.suffix == ".parquet" else _read_jsonl(data_path)
    except Exception as e:
        console.print(f"[red]Failed to load: {e}[/red]")
        return

    required = ["instance_id", "repo", "base_commit", "patch"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        console.print(f"[red]Missing required columns: {missing}[/red]")
        console.print(f"[dim]Required: {required}[/dim]")
        console.print(f"[dim]Found: {list(df.columns)}[/dim]")
        return

    total = len(df)
    console.print(f"[green]Loaded {total} instances from {data_path.name}[/green]")
    console.print(f"[dim]Repos: {df['repo'].nunique()} unique repos[/dim]")
    console.print()

    sample = 0
    if total > 100:
        if Confirm.ask(f"  Audit ALL {total} instances? (may take several minutes)", default=False):
            pass
        else:
            sample = IntPrompt.ask("  How many to sample", default=min(50, total))
    else:
        sample = 0  # audit all

    _run_pro_audit(data_path, sample=sample, custom=True)


def _run_custom_swebench_verified(data_path: Path):
    """Run a SWE-bench Verified-style audit on custom data."""
    from .engine import AgentSealEngine
    from .loaders import BenchmarkInstance, extract_pr_number, github_pr_url_from_instance
    from .report import write_json, write_markdown, write_html
    from .schemas import AuditConfig
    import pandas as pd
    import re

    try:
        df = pd.read_parquet(data_path) if data_path.suffix == ".parquet" else _read_jsonl(data_path)
    except Exception as e:
        console.print(f"[red]Failed to load: {e}[/red]")
        return

    required = ["instance_id", "repo", "patch"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        console.print(f"[red]Missing required columns: {missing}[/red]")
        return

    # Convert to BenchmarkInstance objects
    instances = []
    for _, row in df.iterrows():
        pr_num, pr_url = github_pr_url_from_instance(str(row["instance_id"]), str(row["repo"]))
        instances.append(BenchmarkInstance(
            instance_id=str(row["instance_id"]),
            repo=str(row["repo"]),
            base_commit=str(row.get("base_commit", "")),
            patch=str(row.get("patch", "")),
            test_patch=str(row.get("test_patch", "")),
            problem_statement=str(row.get("problem_statement", "")),
            pr_number=pr_num,
            pr_url=pr_url,
        ))

    total = len(instances)
    console.print(f"[green]Loaded {total} instances[/green]")
    console.print()

    config = AuditConfig(
        benchmark=str(data_path.stem),
        corpus_source="github-pr-diffs",
    )

    def on_progress(phase, completed, total_count, message):
        if completed % 20 == 0 or completed == total_count:
            console.print(f"  [dim]{phase}: {completed}/{total_count}[/dim]")

    def on_evidence(instance_id, risk, match_type, similarity, message):
        if risk.value in ("critical", "high"):
            from rich.markup import escape as _rich_escape
            color = "red" if risk.value == "critical" else "yellow"
            # SECURITY: escape user-controlled fields to prevent rich markup
            # injection (MarkupError crash on unclosed tags like [/red]).
            safe_id = _rich_escape(str(instance_id))
            safe_msg = _rich_escape(str(message))
            console.print(f"  [{color}]{safe_id} — {safe_msg}[/{color}]")

    console.print("[bold]Running audit...[/bold]")
    start = time.time()
    engine = AgentSealEngine(
        instances=instances, config=config,
        on_progress=on_progress, on_evidence=on_evidence,
    )
    report = engine.run()
    elapsed = time.time() - start

    _show_results_and_save(report, f"custom_audit_{data_path.stem}", elapsed)


def _read_jsonl(path: Path):
    """Read a JSONL file into a DataFrame."""
    import pandas as pd
    import json
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return pd.DataFrame(rows)


def _run_pro_audit(data_path: Path, sample: int = 0, custom: bool = False):
    """Run SWE-bench Pro audit with live progress."""
    from .pro_audit import audit_swebench_pro, results_to_report
    from .report import write_json, write_markdown, write_html

    label = "custom" if custom else "SWE-bench Pro"
    console.print()
    console.print(f"[bold]Starting {label} audit...[/bold]")
    console.print(f"[dim]Data: {data_path}[/dim]")
    console.print()

    start = time.time()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )

    with progress:
        total_guess = 100 if sample == 0 and not custom else (sample if sample > 0 else 100)
        task = progress.add_task("Auditing...", total=total_guess)

        def on_progress(current, total, msg):
            progress.update(task, completed=current, total=total, description=msg)

        results = audit_swebench_pro(data_path, sample_size=sample, on_progress=on_progress)

    elapsed = time.time() - start
    report = results_to_report(results)
    report.config.benchmark = f"{label} ({data_path.name})" if custom else "swe-bench-pro"

    _show_results_and_save(report, "swebench_pro_audit" if not custom else f"custom_pro_{data_path.stem}", elapsed)


def _run_verified_audit(data_path: Path):
    """Run SWE-bench Verified audit."""
    from .engine import AgentSealEngine
    from .loaders import load_swebench_verified
    from .report import write_json, write_markdown, write_html
    from .schemas import AuditConfig

    console.print()
    console.print("[bold]Starting SWE-bench Verified audit...[/bold]")
    console.print(f"[dim]Data: {data_path}[/dim]")
    console.print()

    start = time.time()
    instances = load_swebench_verified(data_path)
    console.print(f"[green]Loaded {len(instances)} instances[/green]")

    config = AuditConfig(benchmark="swe-bench-verified", corpus_source="github-pr-diffs")

    def on_progress(phase, completed, total, message):
        if completed % 50 == 0 or completed == total:
            console.print(f"  [dim]{phase}: {completed}/{total}[/dim]  {message}")

    def on_evidence(instance_id, risk, match_type, similarity, message):
        if risk.value in ("critical", "high"):
            from rich.markup import escape as _rich_escape
            color = "red" if risk.value == "critical" else "yellow"
            safe_id = _rich_escape(str(instance_id))
            safe_msg = _rich_escape(str(message))
            console.print(f"  [{color}]{safe_id} — {safe_msg}[/{color}]")

    console.print("[bold]Running audit...[/bold]")
    engine = AgentSealEngine(instances=instances, config=config, on_progress=on_progress, on_evidence=on_evidence)
    report = engine.run()
    elapsed = time.time() - start

    _show_results_and_save(report, "agentseal_audit", elapsed)


def _show_results_and_save(report, name: str, elapsed: float):
    """Show results table and save reports."""
    from .report import write_json, write_markdown, write_html

    s = report.summary

    console.print()
    console.print(Panel.fit(
        f"[bold green]Audit Complete[/bold green] ({elapsed:.1f}s)",
        border_style="green",
    ))
    console.print()

    # Summary table
    table = Table(title=f"{report.config.benchmark} — Contamination Summary", show_header=True, header_style="bold")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Total instances", str(s.total_instances))
    table.add_row("Contaminated", f"{s.instances_with_patch_exposure} ({s.contamination_rate*100:.1f}%)", style="bold red")
    table.add_row("Critical (≥50%)", str(s.critical_count), style="red")
    table.add_row("High (≥20%)", str(s.high_count), style="yellow")
    table.add_row("Medium (1-19%)", str(s.medium_count), style="cyan")
    table.add_row("Clean", str(s.clean_count), style="green")
    console.print(table)

    # Per-repo table (if we have repo data)
    repos = {}
    for ir in report.instance_risks:
        repo = ir.repo
        if repo not in repos:
            repos[repo] = {"total": 0, "contam": 0}
        repos[repo]["total"] += 1
        if ir.risk.value != "clean":
            repos[repo]["contam"] += 1

    if repos:
        console.print()
        repo_table = Table(title="Per-Repository Breakdown", show_header=True, header_style="bold")
        repo_table.add_column("Repository", style="dim")
        repo_table.add_column("Contaminated", justify="right")
        repo_table.add_column("Total", justify="right")
        repo_table.add_column("Rate", justify="right")
        for repo in sorted(repos.keys(), key=lambda x: -repos[x]["contam"] / max(repos[x]["total"], 1)):
            d = repos[repo]
            rate = 100 * d["contam"] / d["total"] if d["total"] > 0 else 0
            style = "bold red" if rate >= 80 else "yellow" if rate >= 50 else "green"
            repo_table.add_row(repo, str(d["contam"]), str(d["total"]), f"{rate:.0f}%", style=style)
        console.print(repo_table)

    # Write reports
    out_dir = Path("examples/reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{name}.json"
    md_path = out_dir / f"{name}.md"
    html_path = out_dir / f"{name}.html"
    write_json(report, json_path)
    write_markdown(report, md_path)
    write_html(report, html_path)

    console.print()
    console.print("[bold]Reports written:[/bold]")
    console.print(f"  [green]JSON[/green]     {json_path}")
    console.print(f"  [green]Markdown[/green] {md_path}")
    console.print(f"  [green]HTML[/green]     {html_path}")

    if Confirm.ask("\n  Open HTML report in browser?", default=True):
        _open_file(html_path)


def _view_report(reports: dict[str, Path]):
    """Let user pick and view an existing report."""
    console.print("\n[bold]Existing reports:[/bold]")
    keys = list(reports.keys())
    for i, name in enumerate(keys, 1):
        console.print(f"  [cyan]{i}[/cyan]  {name}")

    choice = Prompt.ask("\n  View which report (number)", default="1")
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(keys):
            path = reports[keys[idx]]
            html_path = path.with_suffix(".html")
            if html_path.exists():
                _open_file(html_path)
            else:
                console.print(f"[yellow]No HTML version found. JSON: {path}[/yellow]")
    except (ValueError, IndexError):
        console.print("[yellow]Invalid choice.[/yellow]")


def _open_file(path: Path):
    """Open a file with the system default application.

    SECURITY: Uses subprocess.run with a list (shell=False) to prevent
    command injection via malicious filenames. The previous implementation
    used os.system(f"open '{path}'") which was vulnerable to shell injection
    via single quotes, backticks, $(), semicolons, pipes, and newlines in
    the path.
    """
    import subprocess
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(str(path))
        elif system == "Darwin":
            # shell=False: the path is passed as a single argv element,
            # not interpreted by a shell. No injection possible.
            subprocess.run(["open", str(path)], check=False, timeout=10)
        else:
            subprocess.run(["xdg-open", str(path)], check=False, timeout=10)
    except Exception:
        console.print(f"[yellow]Could not auto-open. File: {path}[/yellow]")


__all__ = ["run_interactive"]
