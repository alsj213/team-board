"""``reviewhead`` 纯逻辑层单测：标记解析、四态判定、按内容复用、渲染、参数形态、分页。

这一层不碰 gh：取数在 ``tests/integration/test_cli_review_head_audit.py`` 里用假 gh
跑。这里锁的是「什么算一条标记」「四态怎么判」「旧标记怎么按内容复用 / 失效」
「二进制与空 diff 为什么一律不可绑定」「rc2 的参数边界」。

判定层（:func:`reviewhead.judge`）用注入的 ``resolve`` 替身，**不碰 git**；只有
「旧标记按内容复用」与「二进制 / 空 diff」两组用真 ``git`` 在 ``tmp_path`` 造仓 ——
那两件正是「真内容等价 / 真判不了」，用假 sha 造不出来。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from team_board import cli, reviewhead

#: 真实内容指纹（#57 实测值，见卡面评论）
PID = "1f03c0cee0258384265b9eeda1bf20abd5611ced"
#: 另一个内容指纹（形态合法，值不同）
OTHER_PID = "b" * 40
#: 历史 head sha（PR #34 的 head）
OLD_HEAD = "d2be80da5e30f8560579f6010217f05349127498"
HEAD_64 = "0123456789abcdef" * 4  # 64 位
#: merge-base（40 hex；报告首行只打前 12 位）
MB = "df48d1832485" + "0" * 28


def body(*lines: str) -> str:
    return "\n".join(lines)


def patched(pid: str, kind: str, *, prefix: str = "") -> str:
    """新写法：``REVIEW-PATCH`` + ``REVIEW-KIND``。"""

    return body(f"{prefix}REVIEW-PATCH: {pid}", f"{prefix}REVIEW-KIND: {kind}")


def headed(sha: str, kind: str, *, prefix: str = "") -> str:
    """旧写法：``REVIEW-HEAD`` + ``REVIEW-KIND``（只读兼容）。"""

    return body(f"{prefix}REVIEW-HEAD: {sha}", f"{prefix}REVIEW-KIND: {kind}")


def fp(value: str | None = PID, *, kind: str = "patch") -> reviewhead.Fingerprint:
    return reviewhead.Fingerprint(
        kind=kind, value=value, merge_base=MB, paths=(),
        algo=reviewhead.FINGERPRINT_ALGO,
    )


class _Resolver:
    """记调用次数的 ``resolve`` 替身（judge 的 memo 就靠它验）。"""

    def __init__(self, mapping: dict[str, str | None]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, sha: str) -> reviewhead.Fingerprint | None:
        self.calls.append(sha)
        pid = self.mapping.get(sha)
        return None if pid is None else fp(pid)


# --------------------------------------------------------------- 正则即契约


class TestRegexIsTheSpec:
    """三条正则逐字就是卡面给的那三条（改一个字符都要先改卡）。"""

    def test_patch_regex_literal(self) -> None:
        assert reviewhead.PATCH_RE.pattern == r"^REVIEW-PATCH: ([0-9a-f]{40})$"

    def test_head_regex_literal(self) -> None:
        assert reviewhead.HEAD_RE.pattern == r"^REVIEW-HEAD: ([0-9a-f]{40}|[0-9a-f]{64})$"

    def test_kind_regex_literal(self) -> None:
        assert reviewhead.KIND_RE.pattern == r"^REVIEW-KIND: (defect|regression)$"

    def test_all_regex_are_multiline(self) -> None:
        assert reviewhead.PATCH_RE.flags & reviewhead.re.M
        assert reviewhead.HEAD_RE.flags & reviewhead.re.M
        assert reviewhead.KIND_RE.flags & reviewhead.re.M

    def test_states_are_the_four_values(self) -> None:
        assert [
            reviewhead.STATE_OK,
            reviewhead.STATE_LEGACY_OK,
            reviewhead.STATE_STALE,
            reviewhead.STATE_UNVERIFIABLE,
        ] == ["OK", "LEGACY-OK", "STALE", "UNVERIFIABLE"]
        assert {"OK", "LEGACY-OK"} == reviewhead.STATES_COUNTED


# --------------------------------------------------------------- 标记解析


class TestParseMarks:
    def test_patch_line_is_one_mark(self) -> None:
        marks = reviewhead.parse_marks([patched(PID, "defect")])
        assert marks == (reviewhead.Mark(kind="defect", patch=PID, head=None),)

    def test_head_line_is_one_mark(self) -> None:
        marks = reviewhead.parse_marks([headed(OLD_HEAD, "regression")])
        assert marks == (reviewhead.Mark(kind="regression", patch=None, head=OLD_HEAD),)

    def test_surrounding_text_is_fine(self) -> None:
        text = body(
            "## 缺陷视角",
            "阻塞：P2-1 --branch 未 quote",
            "",
            f"REVIEW-PATCH: {PID}",
            "REVIEW-KIND: defect",
            "",
            "依据：gh pr checks rc=0",
        )
        assert reviewhead.parse_marks([text]) == (
            reviewhead.Mark(kind="defect", patch=PID, head=None),
        )

    def test_patch_wins_when_both_lines_present(self) -> None:
        text = body(
            f"REVIEW-HEAD: {OLD_HEAD}",
            f"REVIEW-PATCH: {PID}",
            "REVIEW-KIND: defect",
        )
        (mark,) = reviewhead.parse_marks([text])
        assert mark.patch == PID
        assert mark.head == OLD_HEAD  # 留痕：两种值都记在 Mark 上
        assert mark.value == PID  # 判定按 PATCH

    def test_64_hex_head_accepted(self) -> None:
        assert reviewhead.parse_marks([headed(HEAD_64, "regression")]) == (
            reviewhead.Mark(kind="regression", patch=None, head=HEAD_64),
        )

    def test_only_head_line_is_not_a_mark(self) -> None:
        assert reviewhead.parse_marks([body(f"REVIEW-HEAD: {OLD_HEAD}")]) == ()

    def test_only_patch_line_is_not_a_mark(self) -> None:
        assert reviewhead.parse_marks([body(f"REVIEW-PATCH: {PID}")]) == ()

    def test_only_kind_line_is_not_a_mark(self) -> None:
        assert reviewhead.parse_marks([body("REVIEW-KIND: defect")]) == ()

    @pytest.mark.parametrize("indent", [" ", "  ", "\t"])
    def test_indented_lines_are_not_marks(self, indent: str) -> None:
        assert reviewhead.parse_marks([patched(PID, "defect", prefix=indent)]) == ()
        assert reviewhead.parse_marks([headed(OLD_HEAD, "defect", prefix=indent)]) == ()

    def test_trailing_space_is_not_a_mark(self) -> None:
        text = body(f"REVIEW-PATCH: {PID} ", "REVIEW-KIND: defect")
        assert reviewhead.parse_marks([text]) == ()

    def test_crlf_is_not_a_mark(self) -> None:
        # 卡面给的 ``$`` 是「\n 之前」，行尾多一个 \r 就不匹配；这是照抄语义，不是漏判
        text = f"REVIEW-PATCH: {PID}\r\nREVIEW-KIND: defect\r\n"
        assert reviewhead.parse_marks([text]) == ()

    def test_uppercase_patch_is_not_a_mark(self) -> None:
        assert reviewhead.parse_marks([patched(PID.upper(), "defect")]) == ()

    @pytest.mark.parametrize("n", [39, 41, 63, 65])
    def test_wrong_patch_length_is_not_a_mark(self, n: int) -> None:
        assert reviewhead.parse_marks([patched("a" * n, "defect")]) == ()

    def test_unknown_kind_is_not_a_mark(self) -> None:
        text = body(f"REVIEW-PATCH: {PID}", "REVIEW-KIND: other")
        assert reviewhead.parse_marks([text]) == ()

    def test_64_hex_patch_is_not_a_mark(self) -> None:
        """``REVIEW-PATCH`` 只认 40 位（内容指纹恒 40 hex，64 位属填错字段）。"""

        assert reviewhead.parse_marks([patched(HEAD_64, "defect")]) == ()

    def test_first_line_wins_within_one_body(self) -> None:
        text = body(
            f"REVIEW-PATCH: {PID}",
            "REVIEW-KIND: defect",
            f"REVIEW-PATCH: {OTHER_PID}",
            "REVIEW-KIND: regression",
        )
        assert reviewhead.parse_marks([text]) == (
            reviewhead.Mark(kind="defect", patch=PID, head=None),
        )

    def test_one_mark_per_body(self) -> None:
        assert len(reviewhead.parse_marks([patched(PID, "defect")])) == 1

    def test_no_bodies(self) -> None:
        assert reviewhead.parse_marks([]) == ()
        assert reviewhead.parse_marks(["", None]) == ()  # type: ignore[list-item]

    def test_order_preserved(self) -> None:
        bodies = [patched(PID, "defect"), headed(OLD_HEAD, "regression")]
        assert reviewhead.parse_marks(bodies) == (
            reviewhead.Mark(kind="defect", patch=PID, head=None),
            reviewhead.Mark(kind="regression", patch=None, head=OLD_HEAD),
        )

    def test_parse_leaves_state_to_judge(self) -> None:
        (mark,) = reviewhead.parse_marks([patched(PID, "defect")])
        assert mark.state == ""
        assert mark.resolved is None

    def test_short_is_first_12_of_value(self) -> None:
        assert reviewhead.Mark(kind="defect", patch=PID, head=None).short == PID[:12]
        assert (
            reviewhead.Mark(kind="defect", patch=None, head=OLD_HEAD).short
            == OLD_HEAD[:12]
        )


# --------------------------------------------------------------- 四态判定


class TestJudge:
    def test_patch_equal_is_ok_and_never_resolves(self) -> None:
        resolver = _Resolver({})
        (mark,) = reviewhead.judge(
            reviewhead.parse_marks([patched(PID, "defect")]), fp(), resolve=resolver
        )
        assert mark.state == reviewhead.STATE_OK
        assert mark.resolved is None
        assert resolver.calls == []  # PATCH 命中不调用 resolve

    def test_patch_different_is_stale(self) -> None:
        (mark,) = reviewhead.judge(
            reviewhead.parse_marks([patched(OTHER_PID, "defect")]),
            fp(),
            resolve=_Resolver({}),
        )
        assert mark.state == reviewhead.STATE_STALE

    def test_head_unresolvable_is_unverifiable(self) -> None:
        resolver = _Resolver({OLD_HEAD: None})
        (mark,) = reviewhead.judge(
            reviewhead.parse_marks([headed(OLD_HEAD, "defect")]), fp(), resolve=resolver
        )
        assert mark.state == reviewhead.STATE_UNVERIFIABLE
        assert mark.resolved is None
        assert resolver.calls == [OLD_HEAD]

    def test_head_same_content_is_reused(self) -> None:
        (mark,) = reviewhead.judge(
            reviewhead.parse_marks([headed(OLD_HEAD, "regression")]),
            fp(),
            resolve=_Resolver({OLD_HEAD: PID}),
        )
        assert mark.state == reviewhead.STATE_LEGACY_OK
        assert mark.resolved == PID

    def test_head_other_content_is_stale(self) -> None:
        (mark,) = reviewhead.judge(
            reviewhead.parse_marks([headed(OLD_HEAD, "regression")]),
            fp(),
            resolve=_Resolver({OLD_HEAD: OTHER_PID}),
        )
        assert mark.state == reviewhead.STATE_STALE
        assert mark.resolved == OTHER_PID

    def test_both_lines_judged_by_patch_only(self) -> None:
        text = body(
            f"REVIEW-HEAD: {OLD_HEAD}", f"REVIEW-PATCH: {PID}", "REVIEW-KIND: defect"
        )
        resolver = _Resolver({OLD_HEAD: OTHER_PID})
        (mark,) = reviewhead.judge(reviewhead.parse_marks([text]), fp(), resolve=resolver)
        assert mark.state == reviewhead.STATE_OK  # PATCH 优先：HEAD 内容是别的也不算
        assert resolver.calls == []

    def test_no_resolver_makes_head_unverifiable(self) -> None:
        (mark,) = reviewhead.judge(
            reviewhead.parse_marks([headed(OLD_HEAD, "defect")]), fp(), resolve=None
        )
        assert mark.state == reviewhead.STATE_UNVERIFIABLE

    def test_resolve_is_memoised_per_sha(self) -> None:
        resolver = _Resolver({OLD_HEAD: PID})
        marks = reviewhead.parse_marks(
            [headed(OLD_HEAD, "defect"), headed(OLD_HEAD, "regression")]
        )
        judged = reviewhead.judge(marks, fp(), resolve=resolver)
        assert [m.state for m in judged] == [
            reviewhead.STATE_LEGACY_OK,
            reviewhead.STATE_LEGACY_OK,
        ]
        assert resolver.calls == [OLD_HEAD]  # 一轮判定只算一次

    def test_old_head_without_computable_fingerprint_is_unverifiable(self) -> None:
        """旧 sha 的 diff 判不出内容指纹（二进制 / 空）→ 不可验证，不折成「内容变了」。"""

        def resolve_without_value(sha: str) -> reviewhead.Fingerprint | None:
            # 二进制 / 空 diff 的 Fingerprint.value 都是 None（patch-id 盲），旧 sha 算不出值
            return reviewhead.Fingerprint(
                kind="binary", value=None, merge_base=MB, paths=(),
                algo=reviewhead.FINGERPRINT_ALGO,
            )

        (mark,) = reviewhead.judge(
            reviewhead.parse_marks([headed(OLD_HEAD, "defect")]),
            fp(),
            resolve=resolve_without_value,
        )
        assert mark.state == reviewhead.STATE_UNVERIFIABLE
        assert mark.resolved is None


# --------------------------------------------------------------- analyze / render


class TestAnalyze:
    def test_patch_marks_fill_both_kinds(self) -> None:
        report = reviewhead.analyze(
            fp(), [patched(PID, "defect"), patched(PID, "regression")]
        )
        assert report.ok is True
        assert report.missing == ()
        assert report.kinds_on_patch == {"defect", "regression"}
        assert report.bodies_scanned == 2
        assert report.current == fp()

    def test_stale_patch_does_not_count(self) -> None:
        report = reviewhead.analyze(
            fp(), [patched(PID, "defect"), patched(OTHER_PID, "regression")]
        )
        assert report.missing == ("regression",)
        assert [m.state for m in report.marks] == [
            reviewhead.STATE_OK,
            reviewhead.STATE_STALE,
        ]

    def test_no_marks_at_all(self) -> None:
        report = reviewhead.analyze(fp(), ["随便一条评论", ""])
        assert report.ok is False
        assert report.marks == ()
        assert report.missing == ("defect", "regression")

    def test_duplicate_kind_does_not_fill_the_other(self) -> None:
        report = reviewhead.analyze(fp(), [patched(PID, "defect"), patched(PID, "defect")])
        assert report.missing == ("regression",)

    def test_head_marks_without_repo_context_are_unverifiable(self) -> None:
        """没有 repo / base 就现算不了旧标记的指纹 —— 不许按 sha 猜成 OK。"""

        report = reviewhead.analyze(fp(), [headed(OLD_HEAD, "defect")])
        assert report.marks[0].state == reviewhead.STATE_UNVERIFIABLE
        assert report.missing == ("defect", "regression")

    def test_kinds_on_head_is_the_old_alias(self) -> None:
        report = reviewhead.analyze(fp(), [patched(PID, "defect")])
        assert report.kinds_on_head == report.kinds_on_patch == {"defect"}


class TestRender:
    def test_full_report_lines(self) -> None:
        report = reviewhead.analyze(
            fp(), [patched(PID, "defect"), patched(PID, "regression")], base="origin/main"
        )
        assert reviewhead.render(report).splitlines() == [
            f"algo={reviewhead.FINGERPRINT_ALGO} current={PID[:12]}"
            f" base=origin/main mb={MB[:12]}",
            "扫描正文 2 条；带标记行 2 条",
            f"  OK {PID[:12]} defect",
            f"  OK {PID[:12]} regression",
            f"结论: 内容指纹 {PID[:12]} 的 defect + regression 各 >=1 条 → 绑定成立",
        ]

    def test_first_line_base_is_dash_without_repo_context(self) -> None:
        """没有 base 上下文时首行仍然定型：``base=-``，不省略字段。"""

        report = reviewhead.analyze(fp(), [patched(PID, "defect")])
        assert reviewhead.render(report).splitlines()[0] == (
            f"algo={reviewhead.FINGERPRINT_ALGO} current={PID[:12]} base=- mb={MB[:12]}"
        )

    def test_four_states_in_one_report(self) -> None:
        """四态混排：OK / LEGACY-OK / STALE / UNVERIFIABLE 各一条，只有前两个计入。"""

        missing_sha = "c" * 40
        bodies = [
            patched(PID, "defect"),  # OK
            headed(OLD_HEAD, "regression"),  # LEGACY-OK（内容等价）
            patched(OTHER_PID, "defect"),  # STALE
            headed(missing_sha, "regression"),  # UNVERIFIABLE
        ]
        marks = reviewhead.judge(
            reviewhead.parse_marks(bodies),
            fp(),
            resolve=_Resolver({OLD_HEAD: PID, missing_sha: None}),
        )
        kinds = frozenset(m.kind for m in marks if m.state in reviewhead.STATES_COUNTED)
        report = reviewhead.Report(
            current=fp(),
            bodies_scanned=len(bodies),
            marks=marks,
            kinds_on_patch=kinds,
            missing=tuple(k for k in reviewhead.KINDS if k not in kinds),
            base="main",
        )
        lines = reviewhead.render(report).splitlines()
        assert lines[0] == (
            f"algo={reviewhead.FINGERPRINT_ALGO} current={PID[:12]}"
            f" base=main mb={MB[:12]}"
        )
        assert lines[1] == "扫描正文 4 条；带标记行 4 条"
        assert f"  OK {PID[:12]} defect" in lines
        assert f"  LEGACY-OK {OLD_HEAD[:12]} regression" in lines
        assert f"  STALE {OTHER_PID[:12]} defect" in lines
        assert f"  UNVERIFIABLE {missing_sha[:12]} regression" in lines
        assert report.ok is True  # defect + regression 各有一条内容等价
        assert lines[-1].endswith("绑定成立")

    def test_missing_kind_text_mentions_content(self) -> None:
        report = reviewhead.analyze(fp(), [patched(PID, "defect")])
        lines = reviewhead.render(report).splitlines()
        assert lines[-1] == f"结论: 内容指纹 {PID[:12]} 缺 regression → 内容变了要重跑双问"

    def test_no_mark_text(self) -> None:
        report = reviewhead.analyze(fp(), ["no marker here"])
        lines = reviewhead.render(report).splitlines()
        assert lines[-1] == reviewhead.CONCLUSION_NO_MARK
        assert lines[1] == "扫描正文 1 条；带标记行 0 条"

    def test_no_bodies_render_zero(self) -> None:
        report = reviewhead.analyze(fp(), [])
        assert reviewhead.render(report).splitlines()[1] == "扫描正文 0 条；带标记行 0 条"


# --------------------------------------------------------------- 真 git：内容复用


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "reviewhead-test",
    "GIT_AUTHOR_EMAIL": "reviewhead@example.com",
    "GIT_COMMITTER_NAME": "reviewhead-test",
    "GIT_COMMITTER_EMAIL": "reviewhead@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def write(repo: Path, name: str, data: str | bytes) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "f1.txt", "l1\nl2\nl3\n")
    commit(repo, "base")
    return repo


def test_legacy_mark_reused_when_content_identical(tmp_path: Path) -> None:
    """纯 rebase：旧 ``REVIEW-HEAD`` 的 sha 变了、内容没变 → 复用为 ``LEGACY-OK``。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "feat.txt", "feature\n")
    old_sha = commit(repo, "feat")

    git(repo, "checkout", "-q", "main")
    write(repo, "other.txt", "main moved\n")
    commit(repo, "main moves")
    git(repo, "checkout", "-q", "feat")
    git(repo, "rebase", "-q", "main")
    new_sha = git(repo, "rev-parse", "HEAD")

    assert old_sha != new_sha  # 真 rebase：head sha 换了
    current = reviewhead.fingerprint(repo, "main", new_sha)
    assert current.kind == "patch"
    assert current.value == reviewhead.fingerprint(repo, "main", old_sha).value  # 内容等价

    report = reviewhead.analyze(
        current, [headed(old_sha, "defect")], repo=repo, base="main"
    )
    assert len(report.marks) == 1
    assert report.marks[0].state == reviewhead.STATE_LEGACY_OK
    assert report.marks[0].resolved == current.value
    assert report.kinds_on_patch == {"defect"}
    assert report.missing == ("regression",)


def test_binary_diff_is_unbindable_and_names_paths(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "blob.bin", b"\x00\x01\x02\xff" * 8)
    commit(repo, "add binary")
    sha = git(repo, "rev-parse", "HEAD")

    current = reviewhead.fingerprint(repo, "main", sha)
    assert current.kind == "binary"
    report = reviewhead.analyze(current, [patched(PID, "defect")], repo=repo, base="main")
    assert report.ok is False
    assert report.missing == ("defect", "regression")
    assert report.kinds_on_patch == frozenset()
    assert report.marks[0].state == reviewhead.STATE_UNVERIFIABLE
    conclusion = reviewhead.render(report).splitlines()[-1]
    assert "blob.bin" in conclusion
    assert "二进制" in conclusion


def test_empty_diff_is_unbindable_and_names_branch_content(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")  # 分支相对 main 无任何提交
    current = reviewhead.fingerprint(repo, "main", "feat")
    assert current.kind == "empty"

    report = reviewhead.analyze(
        current, [patched(PID, "defect")], repo=repo, base="origin/main"
    )
    assert report.ok is False
    assert report.missing == ("defect", "regression")
    conclusion = reviewhead.render(report).splitlines()[-1]
    assert "无差异" in conclusion and "origin/main" in conclusion


def test_head_sha_whose_object_is_missing_is_unverifiable(tmp_path: Path) -> None:
    """旧标记的 sha 本地取不到、远端也抓不到 → ``UNVERIFIABLE``（不猜成 STALE）。"""

    repo = make_repo(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat")
    write(repo, "feat.txt", "feature\n")
    sha = commit(repo, "feat")
    current = reviewhead.fingerprint(repo, "main", sha)

    report = reviewhead.analyze(
        current, [headed("d" * 40, "defect")], repo=repo, base="main", remote="nosuch"
    )
    assert report.marks[0].state == reviewhead.STATE_UNVERIFIABLE
    assert report.missing == ("defect", "regression")


# --------------------------------------------------------------- 兼容形态（接线前）


class TestShaModeCompatibility:
    """``cli.py`` / ``merge_guard.py`` 在 E3 / E4 接线前仍传 head sha：输出逐字不变。"""

    def test_sha_mode_report_keeps_old_rendering(self) -> None:
        report = reviewhead.analyze(
            OLD_HEAD, [headed(OLD_HEAD, "defect"), headed(OLD_HEAD, "regression")]
        )
        assert report.current is None
        assert reviewhead.render(report).splitlines() == [
            "扫描正文 2 条；带标记行 2 条",
            f"  OK {OLD_HEAD[:12]} defect",
            f"  OK {OLD_HEAD[:12]} regression",
            f"结论: head {OLD_HEAD[:12]} 的 defect + regression 各 >=1 条 → 绑定成立",
        ]

    def test_sha_mode_missing_kind_text(self) -> None:
        report = reviewhead.analyze(OLD_HEAD, [headed(OLD_HEAD, "defect")])
        assert reviewhead.render(report).splitlines()[-1] == (
            f"结论: head {OLD_HEAD[:12]} 缺 regression → head 变了要重跑双问"
        )

    def test_sha_mode_other_sha_is_stale(self) -> None:
        report = reviewhead.analyze(OLD_HEAD, [headed("e" * 40, "defect")])
        assert report.marks[0].state == reviewhead.STATE_STALE
        assert report.missing == ("defect", "regression")


# --------------------------------------------------------------- 参数形态


class TestCheckHead:
    def test_40_ok(self) -> None:
        assert reviewhead.check_head(OLD_HEAD) == OLD_HEAD

    def test_64_ok(self) -> None:
        assert reviewhead.check_head(HEAD_64) == HEAD_64

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " ",
            "d2be80d",  # 短 sha
            "a" * 39,
            "a" * 41,
            "a" * 63,
            "a" * 65,
            OLD_HEAD.upper(),
            "g" * 40,
            "0x" + "a" * 38,
            OLD_HEAD + "\n",
            None,
            1793160000000,
        ],
    )
    def test_bad_head_raises(self, value: object) -> None:
        with pytest.raises(reviewhead.ReviewHeadError):
            reviewhead.check_head(value)


class TestCheckPr:
    def test_one_ok(self) -> None:
        assert reviewhead.check_pr("1") == 1

    def test_real_pr_ok(self) -> None:
        assert reviewhead.check_pr("34") == 34

    def test_upper_bound_ok(self) -> None:
        assert reviewhead.check_pr(str(reviewhead.PR_MAX)) == reviewhead.PR_MAX

    @pytest.mark.parametrize(
        "value",
        [
            "0",
            "00",
            "-1",
            "+1",
            " 1",
            "1 ",
            "1_0",
            "abc",
            "",
            "1.0",
            "0x1f",
            "١٢٣",  # 阿拉伯-印度数字（int() 会认，字符串判据不认）
            str(reviewhead.PR_MAX + 1),
            "1793160000000",  # 13 位毫秒
            "99999999999999999999",  # 天文数字
            None,
            34,
        ],
    )
    def test_bad_pr_raises(self, value: object) -> None:
        with pytest.raises(reviewhead.ReviewHeadError):
            reviewhead.check_pr(value)

    def test_upper_bound_matches_merge_record(self) -> None:
        """两个子命令的 PR 上限是同一个口径（防漂移）。"""

        assert reviewhead.PR_MAX == cli.MAX_MERGE_PR


class TestParsePages:
    def test_blank_stdout_is_zero_items(self) -> None:
        assert reviewhead.parse_pages("") == []

    def test_single_page(self) -> None:
        assert reviewhead.parse_pages('[{"body": "a"}, {"body": "b"}]') == [
            {"body": "a"},
            {"body": "b"},
        ]

    def test_two_pages_concatenated(self) -> None:
        out = '[{"body": "a"}]\n[{"body": "b"}, {"body": "c"}]\n'
        assert [item["body"] for item in reviewhead.parse_pages(out)] == ["a", "b", "c"]

    def test_surrounding_whitespace(self) -> None:
        assert reviewhead.parse_pages('  \n[]\n  ') == []

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="不是合法 JSON"):
            reviewhead.parse_pages('{"body": "a"')

    def test_non_array_raises(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="不是对象数组"):
            reviewhead.parse_pages('{"body": "a"}')

    def test_array_of_non_objects_raises(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="非对象元素"):
            reviewhead.parse_pages('["a"]')

    def test_endpoint_appears_in_error(self) -> None:
        with pytest.raises(reviewhead.ReviewHeadError, match="pulls/34/reviews"):
            reviewhead.parse_pages("not json", endpoint="pulls/34/reviews")
