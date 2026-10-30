# PR-CLAIM 建卡闸门与缺口稽核（唯一判据）

合并类卡（合并卡 / 收口卡 / 合并 PR #N / 收口：…）**建卡时**正文首行必须是
`PR-CLAIM: #<n>`；已经建好的存量卡用只读命令 `pr-claim-audit --gaps` 稽核。
两处用的是**同一份触发谓词**，代码只有一个源：`src/team_board/prclaim.py`；
**首行判据刻意不同**（稽核更严），差异集在第 2 节，逐字形近项在第 5 节。

## 1. 谓词原文（逐字，不许各写一份）

`re.compile(...)` 这两行与内核建卡闸门（`hermes_cli/kanban_db.py` 的
`_PR_CLAIM_TRIGGER_RE` / `_PR_CLAIM_LINE_RE`）**正则文本逐字相同**，只允许注释不同：

```python
TRIGGER_RE = re.compile(r"^(?:合并卡|收口卡|合并\s*PR\s*#\s*\d+|收口[：:（(])")
FIRST_CLAIM_RE = re.compile(r"^PR-CLAIM: #([1-9][0-9]*)$")
```

四种命中形态：`合并卡…`、`收口卡…`、`合并 PR #47…`、`收口：`/`收口(`/`收口（`。
`needs_claim(title)` = `TRIGGER_RE.match(title)` 非空；`title` 为 `None`/空串 → `False`。

## 2. 首行判据（比内核建卡闸门更严，与 `CLAIM_RE` 对齐）

- 「首行」= 正文的**第一个非空行**（空行跳过，按 `\n` 切分，**不吞 `\r`**）；
- 该行必须**整行**等于 `PR-CLAIM: #<n>`，`n` 是 `1..999999999` 的 ASCII 十进制正整数
  （`#0` / `#034` / 13 位毫秒一律不认）；
- 顶格、行尾无尾随字符：`  PR-CLAIM: #47`、`PR-CLAIM: #47 我领了`、`PR-CLAIM: #47\r`
  都不算；`PR-CLAIM:#47`（冒号后缺空格）也不算；
- 比 `CLAIM_RE`（正文任意顶格行）**同形态、更严**，不会更宽 —— 首行过了，后面的
  唯一性判定一定也认它（`merge_guard` 认认领行只看 `CLAIM_RE`）。

### 与内核建卡闸门的差异集（恰好 4 种形态）

内核 `_validate_pr_claim` 取首行用 `ln.strip()` + `str.splitlines()`（缩进 / tab /
CRLF / 行尾空格都放行）；本仓 `first_line()` **不 strip、按 `\n` 切**。于是：

| 首行形态 | 内核建卡 | `--gaps` 稽核 | 为什么稽核判死 |
| --- | --- | --- | --- |
| `··PR-CLAIM: #47`（前导空格） | ACCEPT | GAP | `CLAIM_RE` 只认顶格行，`merge_guard` 不认它 |
| `\tPR-CLAIM: #47` | ACCEPT | GAP | 同上 |
| `PR-CLAIM: #47\r`（CRLF） | ACCEPT | GAP | `\r` 是尾随字符 |
| `PR-CLAIM: #47·`（行尾空格） | ACCEPT | GAP | 尾随字符 |

方向是刻意选的：放宽成「mirror 内核」会造出**反向缺口** —— 稽核通过、`merge_guard`
却拒绝合并；而建卡时被放过的这 4 种形态，补一行顶格认领行即可闭合。差异集钉在
`tests/unit/test_prclaim_gaps.py::TestKernelDifferencePin`（内联期望表：多一条少一条
都会红），内核那份若改成同样严格也要先被这张表提醒。

## 3. 为什么只看标题前缀（实测依据）

快照 2026-10-31，看板 `devteam`。数字随板变动，**判据是比例与抽样，不是这几个数**。
可复跑命令（只读，口径唯一）：

```bash
PYTHONPATH=src python3 - <<'PY'
import sqlite3
from team_board import prclaim
c = sqlite3.connect("file:/home/liu/.hermes/kanban/boards/devteam/kanban.db?mode=ro", uri=True)
rows = c.execute("SELECT title, body FROM tasks WHERE status != 'archived'").fetchall()
hit = [(t, b) for t, b in rows if prclaim.needs_claim(t)]
print("未归档卡:", len(rows))
print("前缀版(needs_claim):", len(hit))
print("  其中 CLAIM-OK:", sum(1 for _t, b in hit if prclaim.claimed_prs(b)))
print("  其中 NO-CLAIM:", sum(1 for _t, b in hit if not prclaim.claimed_prs(b)))
print("关键词版(标题含合并):", sum("合并" in (t or "") for t, _b in rows))
print("关键词版(正文含合并):", sum("合并" in (b or "") for _t, b in rows))
PY
```

该快照的实跑输出（证据 `evidence/t_cd645098-doc-cmd.log`）：

```
未归档卡: 237
前缀版(needs_claim): 25
  其中 CLAIM-OK: 5
  其中 NO-CLAIM: 20
关键词版(标题含合并): 74
关键词版(正文含合并): 168
```

- **前缀版 25 张 = 5 `CLAIM-OK` + 20 `NO-CLAIM`**，20 张全是已 `done` 的历史合并卡
  （不回溯，见 §4）；
- **关键词版显著更宽**：标题含「合并」74 张、正文含「合并」168 张 —— 里面大量是
  返工卡 / 文档卡 / 引用合并的普通卡。把它们都算成「必须有认领行」会破坏认领唯一性
  （同一 PR 出现多张卡认领），`merge_guard` 反而 rc2。

现板（未结卡）实跑：

```bash
PYTHONPATH=src python3 -m team_board.cli pr-claim-audit --gaps --db <看板.db>
```

快照：`未结卡 9 张` / `结论: 无缺口（合并类卡都带 PR-CLAIM）`，rc=0 —— 命中 **0 张**，
**存量缺口为空，无需回填**（未结卡数随板上变动，判据是「命中 0 张 + rc0」）。

## 4. 命令与退出码

```bash
PYTHONPATH=src python3 -m team_board.cli pr-claim-audit --gaps --db <看板.db>
PYTHONPATH=src python3 -m team_board.cli pr-claim-audit --pr 47 --db <看板.db>
```

- `--gaps` 与 `--pr` **互斥且必填其一**（同给 / 都不给 → argparse rc2）；
- `--gaps`：rc `0` 无缺口 / `1` 有缺口 / `2` 参数或读库错；
- `--pr`：rc `0` 恰好一张卡认领 / `1` 无人认领或重复认领 / `2` 参数或环境错；
- rc2 的 stderr 分两类，**都** stdout 零字节（报告整块在库读成功后才拼）：
  - **运行期错误**（`--pr` 取值非法、读库失败：不存在 / 非 SQLite / 缺 `title` 列 /
    无 `tasks` 表）→ stderr **恰一行**以 `错误: ` 开头，不冒 traceback；
  - **argparse 级错误**（`--pr` 与 `--gaps` 同给、两个都不给、未知参数）→ argparse
    原样 **2 行**：`usage: …` + `…: error: …`（全仓所有子命令同一形态）；
- 两种模式都只读（`mode=ro`），绝不写库。

输出格式（固定，测试按它断言）：

```
未结卡 N 张
GAP <id> [<status>] <assignee> 触发=<hit> 首行=<first>
结论: 无缺口（合并类卡都带 PR-CLAIM）
# 有缺口时末行：结论: 缺口 G 张（逐张把 PR-CLAIM: #<n> 放到正文首行）
```

`<hit>` 是命中的标题片段（`合并卡` / `收口卡` / `合并 PR #47` / `收口：`），
`<first>` 是正文第一个非空行、截 40 字符后**控制字符转义显示**（`\r` / `\t` / `\xNN`）——
`\r` 原样输出会把光标拉回行首、污染报告；转义只动渲染，不动判定。

## 5. 与内核建卡闸门（卡 `t_c6ab9672`）的关系

- 内核那份在 `hermes_cli/kanban_db.py`：`_PR_CLAIM_TRIGGER_RE` / `_PR_CLAIM_LINE_RE` /
  `_validate_pr_claim(title, body)`，挂在 `create_task` 的 `_validate_task_body` 之后；
- **触发谓词逐字相同**（§1 那两行 `re.compile`）：改一处必须同轮改另一处，否则
  「建卡时放过的卡稽核时报缺口」或「稽核认的卡建卡时被拦」；
- **首行判据不同、方向固定**：`needs_claim` 两边一致；`first_claim_pr` 比内核严格
  （内核 `strip()`，本仓不 strip），差异集就是 §2 那 4 种形态，由
  `TestKernelDifferencePin` 钉住。改内核那一侧前先看那张表是否会红；
- 本仓这份是 `pr-claim-audit --gaps` 的唯一实现，不新增第二份判定（不加 allowlist、
  不改 CI job 顺序、不动 `merge_guard` 的认领闸门语义）。
