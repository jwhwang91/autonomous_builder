from __future__ import annotations

from autonomous_builder.repository.git import GitError, GitMonitor


def test_clean_tree(target):
    g = GitMonitor(target.root)
    s = g.snapshot()
    assert s.is_repo and not s.dirty
    assert s.branch == "main"
    assert s.head


def test_dirty_tree(target):
    target.make_dirty()
    s = GitMonitor(target.root).snapshot()
    assert s.dirty
    assert "dirty.txt" in s.untracked_files


def test_not_a_repo(tmp_path):
    (tmp_path / "plain").mkdir()
    s = GitMonitor(tmp_path / "plain").snapshot()
    assert s.exists and not s.is_repo


def test_missing_root(tmp_path):
    s = GitMonitor(tmp_path / "nope").snapshot()
    assert not s.exists


def test_head_changes_after_commit(target):
    g = GitMonitor(target.root)
    before = g.head()
    target.simulate_packet("RT-D", "RT-E")
    after = g.head()
    assert before != after
    assert g.commit_exists(after)


def test_origin_reported_when_set(target):
    import subprocess
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/x/y.git"],
                   cwd=str(target.root), check=True, capture_output=True)
    s = GitMonitor(target.root).snapshot()
    assert s.origin_url == "https://github.com/x/y.git"


def test_read_only_guard():
    g = GitMonitor("/tmp")
    try:
        g._run("reset", "--hard")
        assert False, "should have refused"
    except GitError:
        pass
