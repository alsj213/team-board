"""``cli pr-review-head-audit`` 的集成测试：假 gh 挂在 PATH 上，验 0/1/2 三档与只读性。

假 gh 只认本子命令要的两条 ``gh api repos/alsj213/team-board/... --paginate``：
把 ``$FAKE_REVIEWS`` / ``$FAKE_COMMENTS`` 原样吐到 stdout（可以放多段数组模拟分页），
按 ``$FAKE_*_RC`` / ``$FAKE_*_ERR`` 造失败，并把每次调用记进 ``$FAKE_LOG``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from team_board import cli

HEAD = "d2be80da5e30f8560579f6010217f05349127498"
HEAD32 = HEAD[:12]
OTHER = "b" * 40
HEAD_64 = "0123456789abcdef" * 4
REVIEWS_PATH = "repos/alsj213/team-board/pulls/34/reviews"
COMMENTS_PATH = "repos/alsj213/team-board/issues/34/comments"

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


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def mark(sha: str, kind: str) -> dict[str, Any]:
    """一条 GitHub Review / issue 评论对象（只带 body，另塞几个无关字段）。"""

    return {
        "id": hash((sha, kind)) & 0xFFFF,
        "user": {"login": "alsj213"},
        "state": "COMMENTED",
        "body": f"## 双问\n\nREVIEW-HEAD: {sha}\nREVIEW-KIND: {kind}\n",
    }


def text_only(body: str) -> dict[str, Any]:
    return {"id": hash(body) & 0xFFFF, "user": {"login": "alsj213"}, "body": body}


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


@pytest.fixture()
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stub:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir()
    _write_exe(bindir / "gh", GH_STUB)
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


def once(capsys: pytest.CaptureFixture[str], stub: Stub, head: str = HEAD) -> tuple[int, str, str]:
    return run(capsys, "--pr", "34", "--head", head)


class TestRc0:
    def test_both_kinds_on_head(self, stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
        stub.reviews([mark(HEAD, "defect")])
        stub.comments([mark(HEAD, "regression")])
        code, out, err = once(capsys, stub)
        assert code == 0
        assert err == ""
        assert out.splitlines() == [
            "扫描正文 2 条；带标记行 2 条",
            f"  OK {HEAD32} defect",
            f"  OK {HEAD32} regression",
            f"结论: head {HEAD32} 的 defect + regression 各 >=1 条 → 绑定成立",
        ]

    def test_64_hex_head(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([mark(HEAD_64, "defect")])
        stub.comments([mark(HEAD_64, "regression")])
        code, out, _ = once(capsys, stub, HEAD_64)
        assert code == 0
        assert f"结论: head {HEAD_64[:12]} 的" in out

    def test_both_kinds_in_the_same_body(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.comments(
            [text_only(f"REVIEW-HEAD: {HEAD}\nREVIEW-KIND: defect\n"), mark(HEAD, "regression")]
        )
        code, _, _ = once(capsys, stub)
        assert code == 0

    def test_paginated_output_is_concatenated(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([mark(HEAD, "defect"), mark(OTHER, "defect")], pages=2)
        stub.comments([mark(HEAD, "regression")], pages=2)
        code, out, _ = once(capsys, stub)
        assert code == 0
        assert "扫描正文 3 条；带标记行 3 条" in out
        assert f"  STALE {OTHER[:12]} defect" in out


class TestRc1:
    def test_missing_regression(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([mark(HEAD, "defect")])
        code, out, err = once(capsys, stub)
        assert code == 1
        assert err == ""
        assert out.splitlines()[-1] == (
            f"结论: head {HEAD32} 缺 regression → head 变了要重跑双问"
        )

    def test_missing_defect(self, stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
        stub.comments([mark(HEAD, "regression")])
        code, out, _ = once(capsys, stub)
        assert code == 1
        assert "缺 defect" in out.splitlines()[-1]

    def test_only_other_sha_is_stale(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([mark(OTHER, "defect")])
        stub.comments([mark(OTHER, "regression")])
        code, out, _ = once(capsys, stub)
        assert code == 1
        assert f"  STALE {OTHER[:12]} defect" in out
        assert f"  STALE {OTHER[:12]} regression" in out
        assert out.splitlines()[-1] == (
            f"结论: head {HEAD32} 缺 defect,regression → head 变了要重跑双问"
        )

    def test_no_marks_at_all(self, stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
        stub.reviews([text_only("LGTM")])
        stub.comments([text_only("已自查")])
        code, out, err = once(capsys, stub)
        assert code == 1
        assert err == ""
        assert out.splitlines()[-1] == (
            "结论: 无结构化标记 → 该 PR 的双问不可机器绑定（按缺证据处理）"
        )

    def test_indented_marker_is_not_counted(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.comments(
            [
                text_only(f"  REVIEW-HEAD: {HEAD}\n  REVIEW-KIND: defect\n"),
                text_only(f"  REVIEW-HEAD: {HEAD}\n  REVIEW-KIND: regression\n"),
            ]
        )
        code, out, _ = once(capsys, stub)
        assert code == 1
        assert "带标记行 0 条" in out

    def test_one_stale_one_ok(self, stub: Stub, capsys: pytest.CaptureFixture[str]) -> None:
        stub.reviews([mark(OTHER, "defect")])
        stub.comments([mark(HEAD, "defect")])
        code, out, _ = once(capsys, stub)
        assert code == 1
        assert "缺 regression" in out.splitlines()[-1]


class TestRc2Params:
    @pytest.mark.parametrize(
        "head",
        [
            "",
            " ",
            "d2be80d",
            "a" * 39,
            "a" * 41,
            HEAD.upper(),
            "z" * 40,
        ],
    )
    def test_bad_head(
        self, stub: Stub, capsys: pytest.CaptureFixture[str], head: str
    ) -> None:
        code, out, err = run(capsys, "--pr", "34", "--head", head)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: --head 非法：")
        assert "Traceback" not in err
        # 参数错误在取数之前就拦住：gh 一次都不许发
        assert stub.calls() == []

    @pytest.mark.parametrize(
        "pr",
        [
            "0",
            "-1",
            "+1",
            "1_0",
            "abc",
            "",
            "1793160000000",
            "99999999999999999999",
        ],
    )
    def test_bad_pr(self, stub: Stub, capsys: pytest.CaptureFixture[str], pr: str) -> None:
        code, out, err = run(capsys, f"--pr={pr}", "--head", HEAD)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err.startswith("错误: PR 号")
        assert "Traceback" not in err
        assert stub.calls() == []

    def test_missing_required_args_is_argparse_rc2(self, stub: Stub) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["pr-review-head-audit", "--pr", "34"])
        assert excinfo.value.code == 2


class TestRc2Environment:
    def test_reviews_call_fails(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.failing("reviews", rc=1, err="HTTP 404: Not Found")
        code, out, err = once(capsys, stub)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert err == (
            "错误: gh api pulls/34/reviews 失败 rc=1: HTTP 404: Not Found\n"
        )
        # reviews 失败就停，不再发 comments
        assert len(stub.calls()) == 1

    def test_comments_call_fails(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([mark(HEAD, "defect")])
        stub.failing("comments", rc=4, err="HTTP 502: Bad Gateway")
        code, out, err = once(capsys, stub)
        assert code == 2
        assert out == ""
        assert "gh api issues/34/comments 失败 rc=4: HTTP 502" in err
        assert len(stub.calls()) == 2

    def test_gh_missing_from_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        code, out, err = run(capsys, "--pr", "34", "--head", HEAD)
        assert code == 2
        assert out == ""
        assert err.count("\n") == 1
        assert "找不到 gh 可执行文件" in err

    def test_invalid_json(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.setenv("FAKE_REVIEWS", "not json at all")
        code, out, err = once(capsys, stub)
        assert code == 2
        assert out == ""
        assert "不是合法 JSON" in err

    def test_non_array_json(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.setenv("FAKE_REVIEWS", '{"body": "x"}')
        code, out, err = once(capsys, stub)
        assert code == 2
        assert out == ""
        assert "不是对象数组" in err


class TestReadOnly:
    def test_exactly_two_api_get_calls(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        stub.reviews([mark(HEAD, "defect")])
        stub.comments([mark(HEAD, "regression")])
        once(capsys, stub)
        assert stub.calls() == [
            f"gh api {REVIEWS_PATH} --paginate",
            f"gh api {COMMENTS_PATH} --paginate",
        ]

    def test_no_write_subcommand_even_on_rc1(
        self, stub: Stub, capsys: pytest.CaptureFixture[str]
    ) -> None:
        once(capsys, stub)
        assert [call.split()[1] for call in stub.calls()] == ["api", "api"]
        assert all("--method" not in call and " -X " not in call for call in stub.calls())
