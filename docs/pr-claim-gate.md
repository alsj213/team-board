# PR-CLAIM 建卡闸门与缺口稽核（唯一判据）

合并类卡（合并卡 / 收口卡 / 合并 PR #N / 收口：…）**建卡时**正文首行必须是
`PR-CLAIM: #<n>`；已经建好的存量卡用只读命令 `pr-claim-audit --gaps` 稽核。
两处用的是**同一份谓词**，代码只有一个源：`src/team_board/prclaim.py`。

## 1. 谓词原文（逐字，不许各写一份）

```python
#: 合并语义触发（只看标题前缀；正文关键词版实测误中 60 张返工/文档卡）
TRIGGER_RE = re.compile(r"^(?:合并卡|收口卡|合并\s*PR\s*#\s*\d+|收口[：:（(])")

#: 正文首行认领行（与 CLAIM_RE 同形态，但只判首行）
FIRST_CLAIM_RE = re.compile(r"^PR-CLAIM: #([1-9][0-9]*)$")
```

四种命中形态：`合并卡…`、`收口卡…`、`合并 PR #47…`、`收口：`/`收口(`/`收口（`。
`needs_claim(title)` = `TRIGGER_RE.match(title)` 非空；`title` 为 `None`/空串 → `False`。

## 2. 首行判据

- 「首行」= 正文的**第一个非空行**（空行跳过，`\n` 切分，不吞 `\r`）；
- 该行必须**整行**等于 `PR-CLAIM: #<n>`，`n` 是 `1..999999999` 的 ASCII 十进制正整数
  （`#0` / `#034` / 13 位毫秒一律不认）；
- 顶格、行尾无尾随字符：`  PR-CLAIM: #47`、`PR-CLAIM: #47 我领了`、`PR-CLAIM: #47\r`
  都不算；`PR-CLAIM:#47`（冒号后缺空格）也不算；
- 比 `CLAIM_RE`（正文任意顶格行）**更严**，不会更宽 —— 首行过了，后面的唯一性判定
  一定也认它。

## 3. 为什么只看标题前缀（实测依据）

本板存量标定（pm 标定日志：
`/home/liu/.hermes/profiles/pm/cache/scratch/t_e449edd1/ev/prefix-rule-calib.log`）：

| 谓词 | 命中 | 结果 |
| --- | --- | --- |
| 正文关键词版（标题含「合并」即算） | 60 张 | 大量返工/文档卡误中；把认领行强加给它们会破坏认领唯一性，`merge_guard` 反而 rc2 |
| 标题前缀版（本文件第 1 节） | 25 张 | 25/25 全是真合并/收口卡（5 张 `CLAIM-OK` + 20 张 `NO-CLAIM`，后者已 done，不回溯） |

现板（未结卡）实跑（本卡证据 `t_cd645098-cli-gaps.log` 的 A 段，快照时未结 11 张）：
`未结卡 11 张` / `结论: 无缺口（合并类卡都带 PR-CLAIM）`，rc=0 —— 命中 **0 张**，
**存量缺口为空，无需回填**（未结卡数随板上变动，判据是「命中 0 张 + rc0」）。

## 4. 命令与退出码

```bash
PYTHONPATH=src python3 -m team_board.cli pr-claim-audit --gaps --db <看板.db>
PYTHONPATH=src python3 -m team_board.cli pr-claim-audit --pr 47 --db <看板.db>
```

- `--gaps` 与 `--pr` **互斥且必填其一**（同给 / 都不给 → argparse rc2）；
- `--gaps`：rc `0` 无缺口 / `1` 有缺口 / `2` 参数或读库错；
- `--pr`：rc `0` 恰好一张卡认领 / `1` 无人认领或重复认领 / `2` 参数或环境错；
- rc2 时 stderr **恰一行** `错误: …`，stdout **零字节**（报告整块在库读成功后才拼）；
- 两种模式都只读（`mode=ro`），绝不写库。

输出格式（固定，测试按它断言）：

```
未结卡 N 张
GAP <id> [<status>] <assignee> 触发=<hit> 首行=<first>
结论: 无缺口（合并类卡都带 PR-CLAIM）
# 有缺口时末行：结论: 缺口 G 张（逐张把 PR-CLAIM: #<n> 放到正文首行）
```

`<hit>` 是命中的标题片段（`合并卡` / `收口卡` / `合并 PR #47` / `收口：`），
`<first>` 是正文第一个非空行、截 40 字符。

## 5. 与内核建卡闸门（卡 `t_c6ab9672`）的关系

- 内核那份在 `hermes_cli/kanban_db.py`：`_PR_CLAIM_TRIGGER_RE` / `_PR_CLAIM_LINE_RE` /
  `_validate_pr_claim(title, body)`，挂在 `create_task` 的 `_validate_task_body` 之后；
- 两边**正则文本与首行判据必须逐字相同**：改一处必须同轮改另一处，否则「建卡时放过的卡
  稽核时报缺口」或「稽核通过的卡 merge_guard 不认」；
- 本仓这份是 `pr-claim-audit --gaps` 的唯一实现，不新增第二份判定（不加 allowlist、
  不改 CI job 顺序、不动 `merge_guard` 的认领闸门语义）。
