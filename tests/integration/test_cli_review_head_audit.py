"""``cli pr-review-head-audit`` 的集成测试：**真 git 仓库** + 假 gh，验 0/1/2 三档与只读性。

判据是内容指纹（``patch-id -U0``），假 sha 造不出来，所以：

- **真 git**：``tmp_path`` 里造仓库（``main`` + ``feature`` 两条提交），head sha 与
  patch-id 全部由 git 现算（:func:`patch_id` 直接跑「git diff | git patch-id」管线，
  不复用被测模块的实现）；
- **假 gh**：只认本子命令要的两条 ``gh api repos/alsj213/team-board/... --paginate``，
  把 ``$FAKE_REVIEWS`` / ``$FAKE_COMMENTS`` 原样吐到 stdout（可多段数组模拟分页），
  按 ``$FAKE_*_RC`` / ``$FAKE_*_ERR`` 造失败，每次调用记进 ``$FAKE_LOG``。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from team_board import cli

GIT = shutil.which("git")
PR = 34
REVIEWS_PATH = f"repos/alsj213/team-board/pulls/{PR}/reviews"
COMMENTS_PATH = f"repos/alsj213/team-board/issues/{PR}/comments"
IDENT = ("-c", "user.name=engineer", "-c", "user.email=engineer@team-board.local")

GH_STUB = """#!/bin/sh
printf 'gh %s\\n' "$*" >> "$FAKE_LOG"
case "$2" in
  */reviews)
    payload="$FAKE_REVIEWS"; rc="${FAKE_REVIEWS_RC:-0}"; err="$FAKE_REVIEWS_ERR" ;;
  */comments)
    payload="$FAKE_COMMENTS"; rc="${FAKE_COMMENTS_RC:-0}"; err="$FAKE_COMMENTS_ERR" ;;
  *)
    printf 'fake gh: unexpected path: %s\\n' "$2" >&2; exit 9 ;;
esac
if [ "$rc" != "0" ]; then
  if [ -n "$err" ]; then printf '%s\\n' "$err" >&2; fi
  exit "$rc"
fi
printf '%s' "$payload"
exit 0
"""


# ----------------------------------------------------------------- 真 git 造仓库


def _git_bin() -> str:
    assert GIT, "本测试要真 git：PATH 里找不到 git"
    return GIT


def git(cwd: Path, *args: str) -> str:
    """跑一条 git 并返回 stdout（失败直接抛断言，避免假绿）。"""

    proc = subprocess.run(  # nosec B603 B607
        [_git_bin(), "-C", str(cwd), *IDENT, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} 失败: {proc.stderr.strip()}"
    return proc.stdout.strip()


def patch_id(cwd: Path, base: str, head: str) -> str:
    """独立算一次内容指纹：``git diff -U0 <mb> <head> | git patch-id --stable``。"""

    merge_base = git(cwd, "merge-base", base, head)
    diff = subprocess.run(  # nosec B603 B607
        [_git_bin(), "-C", str(cwd), "diff", "-U0", "--no-color", merge_base, head],
        capture_output=True,
        check=False,
    )
    assert diff.returncode == 0, diff.stderr.decode("utf-8", "replace")
    pid = subprocess.run(  # nosec B603 B607
        [_git_bin(), "-C", str(cwd), "patch-id", "--stable"],
        input=diff.stdout,
        capture_output=True,
        check=False,
    )
    assert pid.returncode == 0, pid.stderr.decode("utf-8", "replace")
    return pid.stdout.decode("utf-8").split()[0]


@dataclass
class Repo:
    """一个真仓库：``main`` 上的分叉点 + ``feature`` 上的提交 + 它的内容指纹。"""

    path: Path
    base_sha: str
    head: str
    pid: str


def make_repo(path: Path) -> Repo:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "keep.txt").write_text("keep\n", encoding="utf-8")
    (path / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    base_sha = git(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "-b", "feature")
    (path / "src.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "feature: 改 f")
    head = git(path, "rev-parse", "HEAD")
    return Repo(path=path, base_sha=base_sha, head=head, pid=patch_id(path, "main", head))


def rebase_onto_new_main(repo: Repo) -> str:
    """在 ``main`` 上追加一条**改别的文件**的提交，再把 feature 纯 rebase 上去。

    内容没变（``src.py`` 的 diff 一模一样）、sha 全变 —— 这就是「纯 rebase」。
    """

    git(repo.path, "checkout", "-q", "main")
    (repo.path / "keep.txt").write_text("keep\nmore\n", encoding="utf-8")
    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "-m", "main: 改 keep")
    git(repo.path, "checkout", "-q", "feature")
    git(repo.path, "rebase", "-q", "main")
    return git(repo.path, "rev-parse", "HEAD")


def amend_new_content(repo: Repo) -> str:
    """把 feature 的提交内容改掉（amend），返回新 sha。"""

    (repo.path / "src.py").write_text("def f():\n    return 3\n", encoding="utf-8")
    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "--amend", "--no-edit")
    return git(repo.path, "rev-parse", "HEAD")


def orphan_commit(repo: Repo) -> str:
    """造一条与本仓库主线**无共同祖先**的提交（``merge-base`` 必然失败）。"""

    git(repo.path, "checkout", "-q", "--orphan", "orphan")
    git(repo.path, "rm", "-q", "-r", "-f", "--cached", ".")
    (repo.path / "only.py").write_text("x = 1\n", encoding="utf-8")
    git(repo.path, "add", "-A")
    git(repo.path, "commit", "-q", "-m", "orphan root")
    sha = git(repo.path, "rev-parse", "HEAD")
    git(repo.path, "checkout", "-q", "feature")
    return sha


@pytest.fixture()
def repo(tmp_path: Path) -> Repo:
    return make_repo(tmp_path / "repo")


# --------------------------------------------------------------------- 假 gh


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def patched(pid: str, kind: str) -> dict[str, Any]:
    """一条带 ``REVIEW-PATCH`` 标记的 GitHub Review / issue 评论对象。"""

    return {
        "id": abs(hash((pid, kind))) % 10_000,
        "user": {"login": "alsj213"},
        "state": "COMMENTED",
        "body": f"## 双问\n\nREVIEW-PATCH: {pid}\nREVIEW-KIND: {kind}\n",
    }


def headed(sha: str, kind: str) -> dict[str, Any]:
    """一条旧口径 ``REVIEW-HEAD`` 标记（只读兼容，按内容复用）。"""

    return {
        "id": abs(hash((sha, kind))) % 10_000,
        "user": {"login": "alsj213"},
        "state": "COMMENTED",
        "body": f"## 双问\n\nREVIEW-HEAD: {sha}\nREVIEW-KIND: {kind}\n",
    }


def text_only(body: str) -> dict[str, Any]:
    return {"id": abs(hash(body)) % 10_000, "user": {"login": "alsj213"}, "body": body}


@dataclass
class Stub:
    bindir: Path
    log: Path
    setenv: Any

    def reviews(self, items: list[dict[str, Any]], *, pages: int = 1) -> None:
        self._pages("FAKE_REVIEWS", items, pages)

    def comments(self, items: list[dict[str, Any]], *, pages: int = 1) -> None:
        self._pages("FAKE_COMMENTS", items, pages)

    def _pages(self, name: str, items: list[dict[str, Any]], pages: int) -> None:
        chunk = max(1, (len(items) + pages - 1) // pages) if items else 1
        blocks = [
            json.dumps(items[i : i + chunk], ensure_ascii=False)
            for i in range(0, len(items), chunk)
        ] or ["[]"]
        self.setenv(name, "\n".join(blocks))

    def failing(self, endpoint: str, *, rc: int = 1, err: str = "HTTP 404: Not Found") -> None:
        name = "FAKE_REVIEWS" if endpoint == "reviews" else "FAKE_COMMENTS"
        self.setenv(f"{name}_RC", str(rc))
        self.setenv(f"{name}_ERR", err)

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line]


def _install_gh(bindir: Path) -> None:
    _write_exe(bindir / "gh", GH_STUB)


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    _install_gh(bindir)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_REVIEWS", "[]")
    monkeypatch.setenv("FAKE_COMMENTS", "[]")
    return Stub(bindir=bindir, log=log, setenv=monkeypatch.setenv)


def run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = cli.main(["pr-review-head-audit", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def once(
    capsys: pytest.CaptureFixture[str],
    repo: Repo,
    head: str | None = None,
    *extra: str,
) -> tuple[int, str, str]:
    """以真仓库为 ``--repo`` 跑一次（``extra`` 追加其它参数）。"""

    return run(
        capsys,
        "--pr",
        str(PR),
        "--head",
        head or repo.head,
        "--repo",
        str(repo.path),
        *extra,
    )


# ------------------------------------------------------------------------ rc0


class TestRc0:
    def test_pure_rebase_keeps_the_fingerprint(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """纯 rebase：sha 变了、内容没变 → 同一个 ``REVIEW-PATCH`` 仍然有效（rc0）。"""

        rebased = rebase_onto_new_main(repo)
        assert rebased != repo.head
        assert patch_id(repo.path, "main", rebased) == repo.pid  # 前提：内容确实等价

        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        code, out, err = once(capsys, repo, rebased)
        assert code == 0
        assert err == ""
        lines = out.splitlines()
        assert lines[0].startswith(
            f"algo=patch-id-U0-v1 current={repo.pid[:12]} base=main mb="
        )
        assert lines[1] == "扫描正文 2 条；带标记行 2 条"
        assert f"  OK {repo.pid[:12]} defect" in lines
        assert f"  OK {repo.pid[:12]} regression" in lines
        assert lines[-1] == (
            f"结论: 内容指纹 {repo.pid[:12]} 的 defect + regression 各 >=1 条 → 绑定成立"
        )

    def test_legacy_review_head_mark_is_reused_by_content(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """旧 ``REVIEW-HEAD`` 指向纯 rebase 前的 sha：按内容复用 → LEGACY-OK → rc0。"""

        rebased = rebase_onto_new_main(repo)
        stub.reviews([headed(repo.head, "defect")])
        stub.comments([headed(repo.head, "regression")])
        code, out, _ = once(capsys, repo, rebased)
        assert code == 0
        assert f"  LEGACY-OK {repo.head[:12]} defect" in out
        assert f"  LEGACY-OK {repo.head[:12]} regression" in out
        assert out.splitlines()[-1].endswith("绑定成立")

    def test_one_body_gives_one_mark(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """同一条正文只取每条正则的第一个匹配：两个 ``REVIEW-KIND`` 也只算第一条。"""

        body = f"REVIEW-PATCH: {repo.pid}\nREVIEW-KIND: defect\nREVIEW-KIND: regression\n"
        stub.comments([text_only(body)])
        code, out, _ = once(capsys, repo)
        assert code == 1
        assert "带标记行 1 条" in out
        assert "缺 regression" in out.splitlines()[-1]

    def test_defect_and_regression_in_two_bodies(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """正常形态：Review 一条 defect、issue 评论一条 regression → rc0。"""

        stub.reviews([text_only(f"REVIEW-PATCH: {repo.pid}\nREVIEW-KIND: defect\n")])
        stub.comments([text_only(f"REVIEW-PATCH: {repo.pid}\nREVIEW-KIND: regression\n")])
        code, out, _ = once(capsys, repo)
        assert code == 0
        assert "带标记行 2 条" in out

    def test_paginated_output_is_concatenated(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        other = "b" * 40
        stub.reviews([patched(repo.pid, "defect"), patched(other, "defect")], pages=2)
        stub.comments([patched(repo.pid, "regression")], pages=2)
        code, out, _ = once(capsys, repo)
        assert code == 0
        assert "扫描正文 3 条；带标记行 3 条" in out
        assert f"  STALE {other[:12]} defect" in out

    def test_default_base_falls_back_to_main(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """不给 ``--base``：先试 ``origin/main``（本仓库没有远端）→ 回退 ``main``，rc0。"""

        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        code, out, _ = once(capsys, repo)
        assert code == 0
        assert " base=main " in out.splitlines()[0]

    def test_default_repo_is_the_cwd(
        self,
        stub: Stub,
        repo: Repo,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(repo.path)
        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        code, _, _ = run(capsys, "--pr", str(PR), "--head", repo.head)
        assert code == 0


# ------------------------------------------------------------------------ rc1


class TestRc1:
    def test_content_changed_makes_marks_stale(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """内容改过：旧 ``REVIEW-PATCH`` 全部 STALE → 缺两类 → rc1。"""

        changed = amend_new_content(repo)
        changed_pid = patch_id(repo.path, "main", changed)
        assert changed_pid != repo.pid
        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        code, out, err = once(capsys, repo, changed)
        assert code == 1
        assert err == ""
        assert f"  STALE {repo.pid[:12]} defect" in out
        assert out.splitlines()[-1] == (
            f"结论: 内容指纹 {changed_pid[:12]} 缺 defect,regression → 内容变了要重跑双问"
        )

    def test_missing_regression(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([patched(repo.pid, "defect")])
        code, out, err = once(capsys, repo)
        assert code == 1
        assert err == ""
        assert out.splitlines()[-1] == (
            f"结论: 内容指纹 {repo.pid[:12]} 缺 regression → 内容变了要重跑双问"
        )

    def test_missing_defect(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.comments([patched(repo.pid, "regression")])
        code, out, _ = once(capsys, repo)
        assert code == 1
        assert "缺 defect" in out.splitlines()[-1]

    def test_no_marks_at_all(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([text_only("LGTM")])
        stub.comments([text_only("已自查")])
        code, out, err = once(capsys, repo)
        assert code == 1
        assert err == ""
        assert out.splitlines()[-1] == (
            "结论: 无结构化标记 → 该 PR 的双问不可机器绑定（按缺证据处理）"
        )

    def test_indented_marker_is_not_counted(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.comments(
            [
                text_only(f"  REVIEW-PATCH: {repo.pid}\n  REVIEW-KIND: defect\n"),
                text_only(f"  REVIEW-PATCH: {repo.pid}\n  REVIEW-KIND: regression\n"),
            ]
        )
        code, out, _ = once(capsys, repo)
        assert code == 1
        assert "带标记行 0 条" in out

    def test_unfetchable_head_is_rc1_and_named(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """head 对象取不到（本仓库没有 origin 远端可供按 sha fetch）→ rc1、点名该 sha。"""

        missing = "c" * 40
        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        code, out, err = once(capsys, repo, missing)
        assert code == 1
        assert err == ""
        lines = out.splitlines()
        assert lines[0].startswith("algo=patch-id-U0-v1 current=- base=main mb=-")
        assert lines[-1] == (
            f"结论: 取不到 {missing}，无法按内容绑定；"
            "先 git fetch 或在新 head 上重贴 REVIEW-PATCH"
        )

    def test_unknown_remote_makes_the_object_unfetchable(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = "d" * 40
        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        code, out, _ = once(capsys, repo, missing, "--remote", "nosuch")
        assert code == 1
        assert f"结论: 取不到 {missing}" in out.splitlines()[-1]


class TestRemote:
    def test_missing_object_is_fetched_from_the_given_remote(
        self,
        stub: Stub,
        repo: Repo,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--remote`` 真有作用：本地没有的对象能按 sha 从指定远端取回来再判。"""

        clone = tmp_path / "clone"
        subprocess.run(  # nosec B603 B607
            [_git_bin(), "clone", "-q", str(repo.path), str(clone)], check=True
        )
        git(clone, "checkout", "-q", "main")  # 克隆只带 origin/main，先建本地 main
        # 克隆之后再在源仓库里造一条**分支上**的提交：克隆里没有它
        git(repo.path, "checkout", "-q", "-b", "topic2", "main")
        (repo.path / "src.py").write_text("def f():\n    return 4\n", encoding="utf-8")
        git(repo.path, "add", "-A")
        git(repo.path, "commit", "-q", "-m", "topic2: 改 f")
        fresh = git(repo.path, "rev-parse", "HEAD")
        probe = subprocess.run(  # nosec B603 B607
            [_git_bin(), "-C", str(clone), "cat-file", "-e", f"{fresh}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        assert probe.returncode != 0, "克隆里不该有这条新提交"
        expected = patch_id(repo.path, "main", fresh)

        stub.reviews([patched(expected, "defect")])
        stub.comments([patched(expected, "regression")])
        code, out, _ = run(
            capsys,
            "--pr",
            str(PR),
            "--head",
            fresh,
            "--repo",
            str(clone),
            "--base",
            "main",
            "--remote",
            "origin",
        )
        assert code == 0
        assert f"current={expected[:12]}" in out.splitlines()[0]
        fetched = subprocess.run(  # nosec B603 B607
            [_git_bin(), "-C", str(clone), "cat-file", "-e", f"{fresh}^{{commit}}"],
            capture_output=True,
            check=False,
        )
        assert fetched.returncode == 0, "--remote 那次 fetch 应该把对象取回来"


# ------------------------------------------------------------------------ rc2


class TestRc2Params:
    @pytest.mark.parametrize(
        "head",
        ["", " ", "d2be80d", "a" * 39, "a" * 41, "A" * 40, "z" * 40],
    )
    def test_bad_head(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str], head: str
    ) -> None:
        code, out, err = run(
            capsys, "--pr", str(PR), "--head", head, "--repo", str(repo.path)
        )
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: --head 非法：")
        assert "Traceback" not in err
        assert stub.calls() == []

    @pytest.mark.parametrize(
        "pr",
        ["0", "-1", "+1", "1_0", "abc", "", "1793160000000", "99999999999999999999"],
    )
    def test_bad_pr(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str], pr: str
    ) -> None:
        code, out, err = run(
            capsys, f"--pr={pr}", "--head", repo.head, "--repo", str(repo.path)
        )
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: PR 号")
        assert "Traceback" not in err
        assert stub.calls() == []

    def test_bad_base_is_rc2_and_gh_is_not_called(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = once(capsys, repo, None, "--base", "nosuch")
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: 找不到 base")
        assert "Traceback" not in err
        assert stub.calls() == []

    def test_repo_is_not_a_git_worktree(
        self, stub: Stub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``.git`` 指向不存在的 gitdir：``rev-parse --git-dir`` 失败 → rc2。"""

        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /nonexistent/place\n", encoding="utf-8")
        code, out, err = run(
            capsys, "--pr", str(PR), "--head", "a" * 40, "--repo", str(broken)
        )
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: --repo 不是 git 工作树")
        assert stub.calls() == []

    def test_repo_does_not_exist(
        self, stub: Stub, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, out, err = run(
            capsys,
            "--pr",
            str(PR),
            "--head",
            "a" * 40,
            "--repo",
            str(tmp_path / "nope"),
        )
        assert code == 2
        assert out == ""
        assert err.startswith("错误: --repo 不是目录")
        assert stub.calls() == []

    def test_missing_required_args_is_argparse_rc2(self, stub: Stub) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["pr-review-head-audit", "--pr", str(PR)])
        assert excinfo.value.code == 2


class TestRc2Environment:
    def test_legacy_mark_with_unrelated_history_is_rc2(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """旧 ``REVIEW-HEAD`` 指向无共同祖先的提交：解析期 git 失败 → rc2（不是 traceback）。"""

        orphan = orphan_commit(repo)
        stub.reviews([headed(orphan, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        code, out, err = once(capsys, repo)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: git merge-base")
        assert "Traceback" not in err

    def test_git_missing_from_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repo: Repo,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        bindir = tmp_path / "gh-only"
        bindir.mkdir()
        _install_gh(bindir)
        monkeypatch.setenv("PATH", str(bindir))
        code, out, err = once(capsys, repo)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert "找不到 git 可执行文件" in err

    def test_reviews_call_fails(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.failing("reviews", rc=1, err="HTTP 404: Not Found")
        code, out, err = once(capsys, repo)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err == f"错误: gh api pulls/{PR}/reviews 失败 rc=1: HTTP 404: Not Found\n"
        assert len(stub.calls()) == 1

    def test_comments_call_fails(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([patched(repo.pid, "defect")])
        stub.failing("comments", rc=4, err="HTTP 502: Bad Gateway")
        code, out, err = once(capsys, repo)
        assert code == 2
        assert out == ""
        assert f"gh api issues/{PR}/comments 失败 rc=4: HTTP 502" in err
        assert len(stub.calls()) == 2

    def test_gh_missing_from_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        repo: Repo,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """PATH 里有 git、没有 gh：git 侧全过，取数阶段 → rc2（不是 rc1）。"""

        bindir = tmp_path / "git-only"
        bindir.mkdir()
        (bindir / "git").symlink_to(_git_bin())
        monkeypatch.setenv("PATH", str(bindir))
        code, out, err = once(capsys, repo)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert "找不到 gh 可执行文件" in err

    def test_invalid_json(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.setenv("FAKE_REVIEWS", "not json at all")
        code, out, err = once(capsys, repo)
        assert code == 2
        assert out == ""
        assert "不是合法 JSON" in err

    def test_non_array_json(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.setenv("FAKE_REVIEWS", '{"body": "x"}')
        code, out, err = once(capsys, repo)
        assert code == 2
        assert out == ""
        assert "不是对象数组" in err


# -------------------------------------------------------------------- 只读性


class TestReadOnly:
    def test_exactly_two_api_get_calls(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        once(capsys, repo)
        assert stub.calls() == [
            f"gh api {REVIEWS_PATH} --paginate",
            f"gh api {COMMENTS_PATH} --paginate",
        ]

    def test_no_write_subcommand_even_on_rc1(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        once(capsys, repo)
        assert [call.split()[1] for call in stub.calls()] == ["api", "api"]
        assert all("--method" not in call and " -X " not in call for call in stub.calls())

    def test_working_tree_is_untouched(
        self, stub: Stub, repo: Repo, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([patched(repo.pid, "defect")])
        stub.comments([patched(repo.pid, "regression")])
        before = git(repo.path, "status", "--porcelain", "--untracked-files=all")
        once(capsys, repo)
        after = git(repo.path, "status", "--porcelain", "--untracked-files=all")
        assert before == after == ""
