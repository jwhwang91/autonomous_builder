from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from autonomous_builder.config import ConfigError, load_profile


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _valid_body(root: str, ledger: str, plan: str, reports: str) -> str:
    return f"""
    project:
      name: Test
      root_dir: {root}
      git_repo_url: <CONFIGURE_ME>
    plan:
      path: {plan}
      execution_ledger_path: {ledger}
      reports_dir: {reports}
    claude:
      executable: /bin/echo
      idle_timeout_minutes: 45
      hard_timeout_minutes: 180
    """


def test_valid_profile(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    (root / ".git").mkdir()
    plan = root / "plan.md"; plan.write_text("x")
    reports = root / "reports"; reports.mkdir()
    ledger = reports / "ledger.md"; ledger.write_text("x")
    cfg = _write(tmp_path, _valid_body(str(root), str(ledger), str(plan), str(reports)))
    profile = load_profile(cfg, slug="t")
    report = profile.validate()
    assert report.ok, report.errors
    # placeholder url yields a warning, not an error
    assert any("git_repo_url" in w for w in report.warnings)


def test_missing_files_are_errors(tmp_path):
    root = tmp_path / "repo"; root.mkdir()
    cfg = _write(tmp_path, _valid_body(str(root), str(root / "nope.md"),
                                       str(root / "no_plan.md"), str(root / "no_reports")))
    profile = load_profile(cfg, slug="t")
    report = profile.validate()
    assert not report.ok
    assert any("execution_ledger_path" in e for e in report.errors)
    assert any("plan.path" in e for e in report.errors)


def test_bad_root_path(tmp_path):
    cfg = _write(tmp_path, _valid_body("/definitely/not/here", "/x/l.md", "/x/p.md", "/x/r"))
    profile = load_profile(cfg, slug="t")
    report = profile.validate()
    assert not report.ok
    assert any("root_dir does not exist" in e for e in report.errors)


def test_invalid_timeouts(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    plan = root / "p.md"; plan.write_text("x")
    reports = root / "r"; reports.mkdir()
    ledger = reports / "l.md"; ledger.write_text("x")
    body = _valid_body(str(root), str(ledger), str(plan), str(reports))
    body = body.replace("idle_timeout_minutes: 45", "idle_timeout_minutes: 0")
    body = body.replace("hard_timeout_minutes: 180", "hard_timeout_minutes: 10")
    cfg = _write(tmp_path, body)
    profile = load_profile(cfg, slug="t")
    report = profile.validate()
    assert not report.ok
    assert any("idle_timeout_minutes must be > 0" in e for e in report.errors)


def test_hard_less_than_idle(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    plan = root / "p.md"; plan.write_text("x")
    reports = root / "r"; reports.mkdir()
    ledger = reports / "l.md"; ledger.write_text("x")
    body = _valid_body(str(root), str(ledger), str(plan), str(reports))
    body = body.replace("hard_timeout_minutes: 180", "hard_timeout_minutes: 30")
    body = body.replace("idle_timeout_minutes: 45", "idle_timeout_minutes: 45")
    cfg = _write(tmp_path, body)
    report = load_profile(cfg, slug="t").validate()
    assert any("hard_timeout_minutes must be >= idle" in e for e in report.errors)


def test_missing_config_file():
    with pytest.raises(ConfigError):
        load_profile("/no/such/config.yaml")


def test_non_numeric_timeout_raises_config_error(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    plan = root / "p.md"; plan.write_text("x")
    reports = root / "r"; reports.mkdir()
    ledger = reports / "l.md"; ledger.write_text("x")
    body = _valid_body(str(root), str(ledger), str(plan), str(reports))
    body = body.replace("idle_timeout_minutes: 45", "idle_timeout_minutes: forty-five")
    cfg = _write(tmp_path, body)
    with pytest.raises(ConfigError):
        load_profile(cfg, slug="t")


def test_graphify_timeout_validated(tmp_path):
    root = tmp_path / "repo"; root.mkdir(); (root / ".git").mkdir()
    plan = root / "p.md"; plan.write_text("x")
    reports = root / "r"; reports.mkdir()
    ledger = reports / "l.md"; ledger.write_text("x")
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "project:\n  name: T\n  root_dir: %s\nplan:\n  path: %s\n"
        "  execution_ledger_path: %s\n  reports_dir: %s\n"
        "claude:\n  executable: /bin/echo\ngraphify:\n  timeout_minutes: 0\n"
        % (root, plan, ledger, reports),
        encoding="utf-8",
    )
    report = load_profile(cfg, slug="t").validate()
    assert any("graphify.timeout_minutes must be > 0" in e for e in report.errors)


def test_deckflip_profile_loads():
    # the real shipped profile must load and parse cleanly
    root = Path(__file__).resolve().parent.parent
    cfg = root / "projects" / "deckflip-runtime" / "config.yaml"
    profile = load_profile(cfg, slug="deckflip-runtime")
    assert profile.project.name == "DeckFlip Runtime"
    assert profile.claude.model == "opus"
    assert profile.claude.effort == "xhigh"  # ultracode is not a valid --effort flag value
    assert "npm run build:editor" in profile.execution.test_commands
    assert profile.finalization.create_test_deck is False
    assert profile.execution.push is False
