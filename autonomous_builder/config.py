"""Project profile configuration: YAML loading, path resolution, validation.

The profile is the single user-facing input model. It is intentionally forgiving
on load (missing optional sections get sane defaults) and strict on
:meth:`ProjectProfile.validate`, which is what ``validate``/``doctor`` surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


class ConfigError(Exception):
    """Raised when a profile cannot be loaded at all (unreadable / invalid YAML)."""


# ---------------------------------------------------------------------------
# Nested config sections
# ---------------------------------------------------------------------------


@dataclass
class ProjectConfig:
    name: str = "Unnamed Project"
    root_dir: str = ""
    git_repo_url: Optional[str] = None
    expected_branch: Optional[str] = None


@dataclass
class PlanConfig:
    path: str = ""
    execution_ledger_path: str = ""
    reports_dir: str = ""
    starting_prompt_path: Optional[str] = None


@dataclass
class AssetsConfig:
    test_asset_dir: Optional[str] = None


@dataclass
class BootstrapStep:
    """One interactive command sent while bringing a session up to readiness."""

    send: str
    description: str = ""
    expect: Optional[str] = None      # regex to confirm the step landed
    required: bool = False            # if True and expect not seen -> bootstrap failure
    settle_seconds: float = 1.5       # small pause after sending
    press_enter: bool = True          # send a newline after the text


@dataclass
class ClaudeConfig:
    executable: str = "/opt/homebrew/bin/claude"
    model: str = "opus"
    effort: str = "ultracode"
    fresh_session_per_packet: bool = True
    max_retries_per_packet: int = 2
    idle_timeout_minutes: int = 45
    hard_timeout_minutes: int = 180
    poll_interval_seconds: float = 2.0
    permission_mode: str = "default"  # normal interactive behaviour by default
    dangerously_skip_permissions: bool = False
    extra_args: list[str] = field(default_factory=list)
    ready_patterns: list[str] = field(default_factory=lambda: [
        r">\s*$",           # a bare prompt
        r"Try \"",           # Claude Code welcome hint
        r"esc to interrupt",
        r"\?\s+for shortcuts",
    ])
    completion_sentinel: str = "END_AUTONOMOUS_BUILDER_RESULT"
    result_start_sentinel: str = "AUTONOMOUS_BUILDER_RESULT"
    # Claude Code renders the pasted prompt back into the transcript, and the
    # prompt template contains the result sentinels — so after submitting we drain
    # + discard that echo, and ignore any sentinel match for a warm-up window, to
    # complete on CLAUDE's result block rather than the echoed template.
    echo_settle_seconds: float = 8.0
    min_packet_result_seconds: float = 15.0
    # Known interactive prompts -> the key/line to send. Empty by default:
    # the builder uses normal interactive behaviour and does not auto-approve.
    interactive_responses: dict[str, str] = field(default_factory=dict)
    bootstrap_steps: list[BootstrapStep] = field(default_factory=list)


@dataclass
class GraphifyConfig:
    update_after_commit: bool = True
    command: str = "/graphify . --update"
    run_at_bootstrap: bool = False
    stop_on_failure: bool = True
    timeout_minutes: int = 30
    success_patterns: list[str] = field(default_factory=lambda: [
        r"Graph complete",
        r"Report updated",
        r"nodes,\s*\d+\s*edges",
        r"Extraction complete",
        r"All time:",
    ])
    failure_patterns: list[str] = field(default_factory=lambda: [
        r"ERROR: Graph is empty",
        r"refused to shrink",
        r"Traceback \(most recent call last\)",
        r"No supported files found",
        r"GRAPH HEALTH WARNING",
    ])


@dataclass
class ExecutionConfig:
    require_clean_tree_before_packet: bool = True
    require_commit_after_packet: bool = True
    stop_on_failed_tests: bool = True
    stop_on_blocker: bool = True
    stop_when_plan_complete: bool = True
    push: bool = False
    test_commands: list[str] = field(default_factory=lambda: [
        "npm run typecheck",
        "npm run test",
        "npm run build",
        "npm run build:editor",
    ])
    max_reports_in_prompt: int = 6
    # Paths whose changes are IGNORED by the pre-packet clean-tree check. The
    # builder-run `/graphify . --update` regenerates graphify-out/, which would
    # otherwise dirty the tree and stop the next packet. Real user/source changes
    # elsewhere still trigger the dirty-tree stop.
    ignore_dirty_paths: list[str] = field(default_factory=lambda: ["graphify-out/"])


@dataclass
class FinalizationConfig:
    create_test_deck: bool = False
    require_runtime_audit_pass: bool = True
    do_not_start_dev_server: bool = True


@dataclass
class StateConfig:
    data_dir: str = "runtime_data"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)  # (name, ok, detail)

    def add_check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Aggregate profile
# ---------------------------------------------------------------------------


DEFAULT_BOOTSTRAP_STEPS = [
    BootstrapStep(
        send="/model opus",
        description="Select the Opus model for this session",
        expect=None, required=False, settle_seconds=2.0,
    ),
    BootstrapStep(
        send="/effort ultracode",
        description="Set effort to ultracode",
        expect=None, required=False, settle_seconds=2.0,
    ),
]


@dataclass
class ProjectProfile:
    project: ProjectConfig
    plan: PlanConfig
    assets: AssetsConfig
    claude: ClaudeConfig
    graphify: GraphifyConfig
    execution: ExecutionConfig
    finalization: FinalizationConfig
    state: StateConfig
    source_path: Optional[str] = None
    slug: Optional[str] = None
    repo_root: Optional[str] = None

    # -- derived paths ------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        base = Path(self.repo_root) if self.repo_root else Path.cwd()
        p = Path(self.state.data_dir)
        return p if p.is_absolute() else base / p

    def resolve(self, path: Optional[str]) -> Optional[Path]:
        if not path:
            return None
        p = Path(path).expanduser()
        if p.is_absolute():
            return p
        base = Path(self.repo_root) if self.repo_root else Path.cwd()
        return (base / p).resolve()

    # -- validation ---------------------------------------------------------
    def validate(self) -> ValidationReport:
        r = ValidationReport()

        # project root
        root = self.resolve(self.project.root_dir)
        if not self.project.root_dir:
            r.error("project.root_dir is required")
        elif root and root.exists():
            r.add_check("project root exists", True, str(root))
            git_dir = root / ".git"
            if git_dir.exists():
                r.add_check("project root is a git repo", True, str(git_dir))
            else:
                r.warn(f"project root {root} is not a git repository (.git missing)")
                r.add_check("project root is a git repo", False, "no .git directory")
        else:
            r.error(f"project.root_dir does not exist: {root}")
            r.add_check("project root exists", False, str(root))

        # git repo url is a placeholder we must not invent
        if not self.project.git_repo_url or "CONFIGURE_ME" in str(self.project.git_repo_url):
            r.warn("project.git_repo_url is a placeholder; origin verification will be skipped")

        # plan + ledger + reports
        for label, value, must in (
            ("plan.path", self.plan.path, True),
            ("plan.execution_ledger_path", self.plan.execution_ledger_path, True),
            ("plan.reports_dir", self.plan.reports_dir, True),
        ):
            resolved = self.resolve(value)
            if not value:
                (r.error if must else r.warn)(f"{label} is required")
                r.add_check(label, False, "missing")
            elif resolved and resolved.exists():
                r.add_check(label, True, str(resolved))
            else:
                (r.error if must else r.warn)(f"{label} does not exist: {resolved}")
                r.add_check(label, False, str(resolved))

        # starting prompt (optional but validated if set)
        if self.plan.starting_prompt_path:
            sp = self.resolve(self.plan.starting_prompt_path)
            if sp and sp.exists():
                r.add_check("starting prompt exists", True, str(sp))
            else:
                r.warn(f"starting_prompt_path does not exist: {sp}")
                r.add_check("starting prompt exists", False, str(sp))

        # test asset dir (only required if finalization test deck enabled)
        if self.assets.test_asset_dir:
            ad = self.resolve(self.assets.test_asset_dir)
            if ad and ad.exists():
                r.add_check("test asset dir exists", True, str(ad))
            elif self.finalization.create_test_deck:
                r.error(f"assets.test_asset_dir does not exist: {ad}")
            else:
                r.warn(f"assets.test_asset_dir does not exist: {ad}")
        elif self.finalization.create_test_deck:
            r.error("finalization.create_test_deck is true but assets.test_asset_dir is unset")

        # claude executable
        exe = Path(self.claude.executable).expanduser()
        if exe.exists():
            r.add_check("claude executable exists", True, str(exe))
        else:
            r.error(f"claude.executable not found: {exe}")
            r.add_check("claude executable exists", False, str(exe))

        # timeouts
        if self.claude.idle_timeout_minutes <= 0:
            r.error("claude.idle_timeout_minutes must be > 0")
        if self.claude.hard_timeout_minutes <= 0:
            r.error("claude.hard_timeout_minutes must be > 0")
        if self.claude.hard_timeout_minutes < self.claude.idle_timeout_minutes:
            r.error(
                "claude.hard_timeout_minutes must be >= idle_timeout_minutes "
                f"({self.claude.hard_timeout_minutes} < {self.claude.idle_timeout_minutes})"
            )
        if self.claude.max_retries_per_packet < 0:
            r.error("claude.max_retries_per_packet must be >= 0")
        if self.claude.poll_interval_seconds <= 0:
            r.error("claude.poll_interval_seconds must be > 0")
        if self.graphify.timeout_minutes <= 0:
            r.error("graphify.timeout_minutes must be > 0")

        # test commands present
        if not self.execution.test_commands:
            r.warn("execution.test_commands is empty; packet-specific tests still apply")

        # safety: skip-permissions must not be silently on
        if self.claude.dangerously_skip_permissions:
            r.warn(
                "claude.dangerously_skip_permissions is TRUE — the builder will pass "
                "--dangerously-skip-permissions. This is not the safe default."
            )

        return r


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _repo_root_for(config_path: Path) -> Path:
    """Nearest ancestor of *config_path* containing pyproject.toml, else cwd."""
    for parent in [config_path.parent, *config_path.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _section(data: dict, key: str) -> dict:
    val = data.get(key) or {}
    if not isinstance(val, dict):
        raise ConfigError(f"config section '{key}' must be a mapping, got {type(val).__name__}")
    return val


def _as_int(section: dict, key: str, default: int) -> int:
    val = section.get(key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        raise ConfigError(f"config field '{key}' must be an integer, got {val!r}")


def _as_float(section: dict, key: str, default: float) -> float:
    val = section.get(key, default)
    try:
        return float(val)
    except (TypeError, ValueError):
        raise ConfigError(f"config field '{key}' must be a number, got {val!r}")


def _bootstrap_steps_from(raw: Any) -> list[BootstrapStep]:
    # None -> let the runner synthesise a default (an /effort step from config,
    # while model is passed as a reliable CLI flag rather than via the /model
    # TUI menu). An explicit (possibly empty) list is honoured verbatim.
    if raw is None:
        return []
    steps: list[BootstrapStep] = []
    for item in raw:
        if isinstance(item, str):
            steps.append(BootstrapStep(send=item))
        elif isinstance(item, dict):
            steps.append(BootstrapStep(
                send=item["send"],
                description=item.get("description", ""),
                expect=item.get("expect"),
                required=bool(item.get("required", False)),
                settle_seconds=float(item.get("settle_seconds", 1.5)),
                press_enter=bool(item.get("press_enter", True)),
            ))
        else:
            raise ConfigError(f"invalid bootstrap step: {item!r}")
    return steps


def load_profile(config_path: str | Path, slug: Optional[str] = None) -> ProjectProfile:
    """Load and construct a :class:`ProjectProfile` from a YAML file."""
    path = Path(config_path).expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - message passthrough
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping in {path}")

    p = _section(raw, "project")
    pl = _section(raw, "plan")
    a = _section(raw, "assets")
    c = _section(raw, "claude")
    g = _section(raw, "graphify")
    e = _section(raw, "execution")
    fin = _section(raw, "finalization")
    st = _section(raw, "state")

    claude = ClaudeConfig(
        executable=c.get("executable", ClaudeConfig.executable),
        model=c.get("model", "opus"),
        effort=c.get("effort", "ultracode"),
        fresh_session_per_packet=bool(c.get("fresh_session_per_packet", True)),
        max_retries_per_packet=_as_int(c, "max_retries_per_packet", 2),
        idle_timeout_minutes=_as_int(c, "idle_timeout_minutes", 45),
        hard_timeout_minutes=_as_int(c, "hard_timeout_minutes", 180),
        poll_interval_seconds=_as_float(c, "poll_interval_seconds", 2.0),
        permission_mode=c.get("permission_mode", "default"),
        dangerously_skip_permissions=bool(c.get("dangerously_skip_permissions", False)),
        extra_args=list(c.get("extra_args", []) or []),
        completion_sentinel=c.get("completion_sentinel", "END_AUTONOMOUS_BUILDER_RESULT"),
        result_start_sentinel=c.get("result_start_sentinel", "AUTONOMOUS_BUILDER_RESULT"),
        interactive_responses=dict(c.get("interactive_responses", {}) or {}),
        bootstrap_steps=_bootstrap_steps_from(c.get("bootstrap_steps")),
    )
    if "ready_patterns" in c and c["ready_patterns"]:
        claude.ready_patterns = list(c["ready_patterns"])

    graphify = GraphifyConfig(
        update_after_commit=bool(g.get("update_after_commit", True)),
        command=g.get("command", "/graphify . --update"),
        run_at_bootstrap=bool(g.get("run_at_bootstrap", False)),
        stop_on_failure=bool(g.get("stop_on_failure", True)),
        timeout_minutes=_as_int(g, "timeout_minutes", 30),
    )
    if g.get("success_patterns"):
        graphify.success_patterns = list(g["success_patterns"])
    if g.get("failure_patterns"):
        graphify.failure_patterns = list(g["failure_patterns"])

    execution = ExecutionConfig(
        require_clean_tree_before_packet=bool(e.get("require_clean_tree_before_packet", True)),
        require_commit_after_packet=bool(e.get("require_commit_after_packet", True)),
        stop_on_failed_tests=bool(e.get("stop_on_failed_tests", True)),
        stop_on_blocker=bool(e.get("stop_on_blocker", True)),
        stop_when_plan_complete=bool(e.get("stop_when_plan_complete", True)),
        push=bool(e.get("push", False)),
        max_reports_in_prompt=_as_int(e, "max_reports_in_prompt", 6),
    )
    if e.get("test_commands") is not None:
        execution.test_commands = list(e["test_commands"])
    if e.get("ignore_dirty_paths") is not None:
        execution.ignore_dirty_paths = list(e["ignore_dirty_paths"])

    profile = ProjectProfile(
        project=ProjectConfig(
            name=p.get("name", "Unnamed Project"),
            root_dir=p.get("root_dir", ""),
            git_repo_url=p.get("git_repo_url"),
            expected_branch=p.get("expected_branch"),
        ),
        plan=PlanConfig(
            path=pl.get("path", ""),
            execution_ledger_path=pl.get("execution_ledger_path", ""),
            reports_dir=pl.get("reports_dir", ""),
            starting_prompt_path=pl.get("starting_prompt_path"),
        ),
        assets=AssetsConfig(test_asset_dir=a.get("test_asset_dir")),
        claude=claude,
        graphify=graphify,
        execution=execution,
        finalization=FinalizationConfig(
            create_test_deck=bool(fin.get("create_test_deck", False)),
            require_runtime_audit_pass=bool(fin.get("require_runtime_audit_pass", True)),
            do_not_start_dev_server=bool(fin.get("do_not_start_dev_server", True)),
        ),
        state=StateConfig(data_dir=st.get("data_dir", "runtime_data")),
        source_path=str(path),
        slug=slug,
        repo_root=str(_repo_root_for(path)),
    )
    return profile


def find_project_config(slug: str, projects_dir: Path) -> Path:
    """Map a --project slug to ``projects/<slug>/config.yaml``."""
    candidate = projects_dir / slug / "config.yaml"
    if candidate.exists():
        return candidate
    # allow passing a direct path too
    direct = Path(slug).expanduser()
    if direct.exists() and direct.is_file():
        return direct
    raise ConfigError(
        f"no config found for project '{slug}': expected {candidate}"
    )


def list_projects(projects_dir: Path) -> list[str]:
    if not projects_dir.exists():
        return []
    return sorted(
        p.name for p in projects_dir.iterdir()
        if p.is_dir() and (p / "config.yaml").exists()
    )
