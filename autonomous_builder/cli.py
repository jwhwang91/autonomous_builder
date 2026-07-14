"""Command-line interface.

Subcommands: init, validate, run, resume, status, doctor, stop, final-test-deck.
Runs as both ``autonomous-builder <cmd>`` and ``python -m autonomous_builder <cmd>``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from autonomous_builder import __version__
from autonomous_builder.config import (
    ConfigError,
    ProjectProfile,
    find_project_config,
    list_projects,
    load_profile,
)

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    _console = Console()
except Exception:  # pragma: no cover - rich always installed, but degrade gracefully
    _console = None


def _print(msg: str = "") -> None:
    if _console:
        _console.print(msg)
    else:  # pragma: no cover
        print(msg)


def _default_projects_dir() -> Path:
    # search cwd upward for a projects/ dir, else fall back to package-relative
    here = Path.cwd()
    for base in [here, *here.parents]:
        if (base / "projects").is_dir():
            return base / "projects"
    pkg_root = Path(__file__).resolve().parent.parent
    return pkg_root / "projects"


def _load(args) -> ProjectProfile:
    projects_dir = Path(args.projects_dir) if getattr(args, "projects_dir", None) else _default_projects_dir()
    config_path = find_project_config(args.project, projects_dir)
    slug = args.project if (projects_dir / args.project).exists() else Path(config_path).parent.name
    return load_profile(config_path, slug=slug)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    from autonomous_builder.templates_data import EXAMPLE_CONFIG_YAML, EXAMPLE_STARTING_PROMPT
    projects_dir = Path(args.projects_dir) if args.projects_dir else _default_projects_dir()
    slug = args.project or "example"
    target = projects_dir / slug
    target.mkdir(parents=True, exist_ok=True)
    cfg = target / "config.yaml"
    sp = target / "starting_prompt.md"
    if cfg.exists() and not args.force:
        _print(f"[yellow]config already exists:[/] {cfg} (use --force to overwrite)")
    else:
        cfg.write_text(EXAMPLE_CONFIG_YAML, encoding="utf-8")
        _print(f"[green]wrote[/] {cfg}")
    if not sp.exists() or args.force:
        sp.write_text(EXAMPLE_STARTING_PROMPT, encoding="utf-8")
        _print(f"[green]wrote[/] {sp}")
    _print("\nNext: edit the config (set git_repo_url, expected_branch), then run "
           f"`autonomous-builder validate --project {slug}`")
    return 0


def cmd_validate(args) -> int:
    profile = _load(args)
    report = profile.validate()
    if _console:
        table = Table(title=f"Validation — {profile.project.name}", show_lines=False)
        table.add_column("Check")
        table.add_column("OK")
        table.add_column("Detail", overflow="fold")
        for name, ok, detail in report.checks:
            table.add_row(name, "[green]✓[/]" if ok else "[red]✗[/]", detail)
        _console.print(table)
    else:  # pragma: no cover
        for name, ok, detail in report.checks:
            print(f"  [{'OK' if ok else 'XX'}] {name}: {detail}")
    for w in report.warnings:
        _print(f"[yellow]warning:[/] {w}")
    for e in report.errors:
        _print(f"[red]error:[/] {e}")
    if report.ok:
        _print(f"[green]VALIDATION PASSED[/] ({len(report.warnings)} warning(s))")
        return 0
    _print(f"[red]VALIDATION FAILED[/] ({len(report.errors)} error(s))")
    return 1


def cmd_doctor(args) -> int:
    from autonomous_builder.repository.git import GitMonitor
    from autonomous_builder.repository.ledger import LedgerParser
    from autonomous_builder.repository.reports import ReportScanner
    from autonomous_builder.execution.discovery import PacketDiscovery
    from autonomous_builder.models import find_packet_ids

    profile = _load(args)
    _print(Panel.fit(f"[bold]Doctor — {profile.project.name}[/]") if _console else "Doctor")
    report = profile.validate()
    for e in report.errors:
        _print(f"[red]error:[/] {e}")
    for w in report.warnings:
        _print(f"[yellow]warning:[/] {w}")

    root = str(profile.resolve(profile.project.root_dir) or profile.project.root_dir)
    git = GitMonitor(root)
    gs = git.snapshot()
    _print(f"git: repo={gs.is_repo} branch={gs.branch} head={gs.short_head} "
           f"dirty={gs.dirty} origin={gs.origin_url}")

    # dry-run discovery (no Claude session)
    try:
        ledger = LedgerParser().parse_file(profile.resolve(profile.plan.execution_ledger_path))
        plan_path = profile.resolve(profile.plan.path)
        from autonomous_builder.repository.ledger import LedgerParse
        plan = LedgerParser().parse_file(plan_path) if plan_path and plan_path.exists() else LedgerParse()
        reports = ReportScanner(profile.resolve(profile.plan.reports_dir))
        disc = PacketDiscovery(require_audit_pass=profile.finalization.require_runtime_audit_pass)
        result = disc.discover(
            ledger=ledger, plan=plan,
            report_hints=reports.next_packet_hints(4),
            report_completions=[i.header_packets[0] for i in reports.scan() if i.header_packets],
            audits=reports.audits(),
            git_recent_packets=find_packet_ids(git.last_commit_message() or ""),
        )
        _print(f"[bold]discovery:[/] outcome={result.outcome.value} "
               f"next={result.next_packet} source={result.authority_source}")
        for d in result.disagreements:
            _print(f"  [yellow]disagreement:[/] {d}")
        if result.ambiguity_reason:
            _print(f"  [yellow]ambiguity:[/] {result.ambiguity_reason}")
        _print(f"  completed: {', '.join(result.completed_packets) or 'none'}")
        _print(f"  superseded: {', '.join(result.superseded_packets) or 'none'}")
    except Exception as exc:
        _print(f"[red]discovery dry-run failed:[/] {exc}")

    return 0 if report.ok else 1


def cmd_run(args) -> int:
    from autonomous_builder.runner import Runner
    profile = _load(args)
    report = profile.validate()
    if not report.ok:
        _print("[red]cannot run: validation failed[/]")
        for e in report.errors:
            _print(f"  [red]error:[/] {e}")
        return 1
    runner = Runner(profile, max_packets=args.max_packets)
    _print(f"[green]starting run[/] for {profile.project.name} "
           f"(max_packets={args.max_packets or '∞'})")
    state = runner.run(resume=False)
    return _report_final(state)


def cmd_resume(args) -> int:
    from autonomous_builder.runner import Runner
    profile = _load(args)
    runner = Runner(profile, max_packets=args.max_packets)
    _print(f"[green]resuming run[/] for {profile.project.name}")
    state = runner.run(resume=True)
    return _report_final(state)


def cmd_status(args) -> int:
    from autonomous_builder.state.store import StateStore
    profile = _load(args)
    store = StateStore(profile)
    state = store.load_state()
    if state is None:
        _print("[yellow]no run state found[/] — nothing has run yet")
        return 0
    _print(f"[bold]{profile.project.name}[/]  run=[cyan]{state.run_id}[/]  status=[bold]{state.status}[/]")
    _print(f"current packet: {state.current_packet or '—'} (attempt {state.attempt})")
    _print(f"next authoritative packet: {state.next_packet or '—'}")
    _print(f"completed: {', '.join(state.completed_packets) or 'none'}")
    _print(f"plan complete: {state.plan_complete}")
    _print(f"stop reason: {state.stop_reason}" + (f" — {state.stop_detail}" if state.stop_detail else ""))
    if state.last_graphify_update:
        g = state.last_graphify_update
        _print(f"last graphify: {g.get('packet')} @ {(g.get('commit') or '')[:8]} "
               f"{'OK' if g.get('success') else 'FAILED'}")
    dash = store.dashboard_dir / "dashboard.md"
    if dash.exists():
        _print(f"\ndashboard: {dash}")
    return 0


def cmd_stop(args) -> int:
    from autonomous_builder.state.store import StateStore
    profile = _load(args)
    store = StateStore(profile)
    path = store.request_stop("stop requested via CLI")
    _print(f"[green]stop requested[/] — the run will halt at the next packet boundary.\n  {path}")
    if args.force:
        state = store.load_state()
        if state and state.claude_pid:
            _print(f"[yellow]--force:[/] attempting to terminate claude pid {state.claude_pid} "
                   "(this may leave the target tree dirty)")
            _force_kill(state.claude_pid)
    return 0


def cmd_final_test_deck(args) -> int:
    from autonomous_builder.runner import Runner
    profile = _load(args)
    if not profile.finalization.create_test_deck and not args.force:
        _print("[yellow]final test deck is disabled in config[/] "
               "(finalization.create_test_deck: false). Use --force to run anyway.")
        return 0
    if args.force:
        profile.finalization.create_test_deck = True
    runner = Runner(profile)
    result = runner.run_final_test_deck()
    _print(f"final test deck: {getattr(result, 'status', 'n/a')}")
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _report_final(state) -> int:
    from autonomous_builder.models import StopReason, RunStatus
    _print("")
    _print(f"[bold]run finished[/] status={state.status} stop={state.stop_reason}")
    if state.stop_detail:
        _print(f"  detail: {state.stop_detail}")
    if state.status == RunStatus.COMPLETE.value:
        return 0
    if state.stop_reason in (StopReason.NONE.value, StopReason.USER_STOP.value,
                             StopReason.PLAN_COMPLETE.value):
        return 0
    return 2


def _force_kill(pid: int) -> None:
    try:
        import psutil
        p = psutil.Process(pid)
        for c in p.children(recursive=True):
            try:
                c.kill()
            except psutil.Error:
                pass
        p.kill()
    except Exception as exc:  # pragma: no cover
        _print(f"[red]could not kill pid {pid}:[/] {exc}")


# ---------------------------------------------------------------------------
# arg parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autonomous-builder",
        description="Autonomously drive fresh Claude Code sessions through a long "
                    "implementation plan, one authoritative packet at a time.",
    )
    parser.add_argument("--version", action="version", version=f"autonomous-builder {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_project(sp, required=True):
        sp.add_argument("--project", required=required, help="project slug (projects/<slug>/config.yaml) or a config path")
        sp.add_argument("--projects-dir", default=None, help="override the projects directory")

    p_init = sub.add_parser("init", help="scaffold a new project profile")
    p_init.add_argument("--project", default=None, help="new project slug (default: example)")
    p_init.add_argument("--projects-dir", default=None)
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.set_defaults(func=cmd_init)

    p_val = sub.add_parser("validate", help="validate a project profile and its paths")
    add_project(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_doc = sub.add_parser("doctor", help="deep diagnostics + dry-run packet discovery (no Claude)")
    add_project(p_doc)
    p_doc.set_defaults(func=cmd_doctor)

    p_run = sub.add_parser("run", help="start an autonomous run")
    add_project(p_run)
    p_run.add_argument("--max-packets", type=int, default=None, help="stop after N packets (default: run to completion)")
    p_run.set_defaults(func=cmd_run)

    p_res = sub.add_parser("resume", help="resume from persisted state + repository truth")
    add_project(p_res)
    p_res.add_argument("--max-packets", type=int, default=None)
    p_res.set_defaults(func=cmd_resume)

    p_st = sub.add_parser("status", help="show the current run status")
    add_project(p_st)
    p_st.set_defaults(func=cmd_status)

    p_stop = sub.add_parser("stop", help="request a graceful stop at the next packet boundary")
    add_project(p_stop)
    p_stop.add_argument("--force", action="store_true", help="also try to kill the live Claude process (may dirty the tree)")
    p_stop.set_defaults(func=cmd_stop)

    p_ftd = sub.add_parser("final-test-deck", help="run the optional finalization test-deck phase")
    add_project(p_ftd)
    p_ftd.add_argument("--force", action="store_true", help="run even if disabled in config")
    p_ftd.set_defaults(func=cmd_final_test_deck)

    p_list = sub.add_parser("projects", help="list available project profiles")
    p_list.add_argument("--projects-dir", default=None)
    p_list.set_defaults(func=cmd_projects)

    return parser


def cmd_projects(args) -> int:
    projects_dir = Path(args.projects_dir) if args.projects_dir else _default_projects_dir()
    names = list_projects(projects_dir)
    if not names:
        _print(f"no projects found under {projects_dir}")
        return 0
    _print(f"projects in {projects_dir}:")
    for n in names:
        _print(f"  - {n}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _print(f"[red]config error:[/] {exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        _print("\n[yellow]interrupted[/]")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
