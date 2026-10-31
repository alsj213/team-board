# 卡正文模板（2048 字节硬上限）

**口径（写死）**：`tasks.body` 的 **UTF-8 字节数 ≤ 2048** 才算合规。超限的卡**建不出来**
（`hermes kanban create` / `kanban_create` 工具都拒，rc=2，不截断、没有开关）；存量卡用
`PYTHONPATH=src python3 -m team_board.cli body-audit` 查超限清单。细则见
[`docs/card-limit.md`](card-limit.md)。

**用法一句话**：模板是基线，**卡正文只写与本卡不同的部分**。长背景、日志、证据一律写进
卡评论（`kanban_comment`）或附件（`kanban_attach`），正文只留一句指针。下面六块一段不缺、
也不多加；`assignee=engineer` 的实现卡另有第 7 块「验收标准（qa 签署）」—— 那一段必填，
口径见 [`docs/qa-spec-gate.md`](qa-spec-gate.md)。

## 1. 目标

- 写什么：一句话说清这张卡交付什么。不写背景、不写过程、不写「为什么」。
- 示例：`给 list 子命令加 --limit N，只打印前 N 行。`

## 2. 差异契约

- 写什么：只写与模板默认口径**不同**的部分；一样的写「见模板」。参数取值域、退出码、
  输出格式的每一处偏离都写在这里。
- 示例：`见模板；本条新增 --limit，取值 1–200 的十进制整数，越界 rc=2。`
- 合并类卡（标题以「合并卡 / 收口卡 / 合并 PR #N / 收口：」开头）：**正文首行必须
  `PR-CLAIM: #<n>`**（顶格、整行、无尾随字符），否则建卡即被拒；存量卡用
  `pr-claim-audit --gaps` 查。细则见 [`docs/pr-claim-gate.md`](pr-claim-gate.md)。

## 3. 边界与不做

- 写什么：明确**不做**什么，防止顺手扩大范围（不许碰的文件 / 模块 / 动作各列一句）。
- 示例：`不做分页、不改输出列；不碰 list 之外的子命令。`

## 4. 交付方式

- 写什么：worktree 路径 / 分支名 / 推送命令 / PR 目标分支 / **不合并**（merge 归 pm）/
  交卡口径（`kanban_request_review(reviewer="pm")`）。
- 示例：`worktree /home/liu/project/tb-wt/<卡号>、分支 wt/<卡号>、git push origin HEAD:refs/heads/wt/<卡号>、
  PR base=main 不合并、交卡 request_review(reviewer="pm")。`（默认全同模板时直接写「见模板」）

## 5. 验收三段式

- 写什么：每条判据一行，格式写死 `<条目> | PASS | $ <命令> | exit <码> | <输出末 1-3 行>`；
  没跑过的写 `NOT TESTED`，不许写「已测试」「应该可以」。
- 示例：`--limit 3 | PASS | $ PYTHONPATH=src python3 -m team_board.cli list --limit 3 | exit 0 | 打印 3 行卡`

## 6. 长内容去哪

- 写什么：指针，不贴内容。长背景、日志、证据写进**卡评论**（`kanban_comment`）或
  **附件**（`kanban_attach`），正文只留一句「见评论 #N / 附件 <文件名>」。
- 示例：`完整需求背景见评论 #1；实测日志见附件 t_xxxxxxxx-list.log。`

## 7. 验收标准（qa 签署）

- 段标题固定：卡正文里这一段的标题**单独一行、逐字 `## 验收标准`**（顶格、不带编号 ——
  本文的「7.」只是本页的小节编号，不写进卡里）。
- 段内**第一行非空内容**必须是签署行：顶格、整行恰为 `QA-SPEC: qa`（大小写敏感、冒号后是
  半角空格、行内不许有别的内容）。签署行写在段外、或段内有别的行排在它前面，都不算签署。
- 必填范围：**`assignee=engineer` 的实现卡必填** —— 缺段或缺签署行，内核建卡闸门将直接拒
  （rc=2，卡不落库）；其余 assignee（pm / qa / repo / ops，以及老板手写的需求卡）可选。
  反过来，正文带签署行的卡**将只能由 qa 的 run 收口**：pm 的验收 run 只许
  `kanban_request_changes` / `kanban_comment`，不许 `complete`。
- 谁写：qa 写、qa 签；pm 拆卡时转抄 qa 的结论，不代签。存量卡不追溯 —— 不补签、不改写、
  不因缺这一段回头把旧卡判不通过。
- 两条闸门的完整口径（为什么、rc、文案、出路、边界）见
  [`docs/qa-spec-gate.md`](qa-spec-gate.md)。

## 样例（可直接抄，实测 834 字节）

把下面 ```text 与 ``` 之间的内容（含行尾换行）存成文件，`wc -c` 得 **834**，
按字符数 `wc -m` 得 **558** —— 同一段正文，字符数和字节数差 276，这就是「按字节不按字符」：

```text
## 目标
给 `list` 子命令加 `--limit N`，只打印前 N 行。

## 差异契约
见模板；本条新增 `--limit`，取值 1–200 的十进制整数，越界 rc=2。

## 边界与不做
不做分页、不改输出列；不碰 `list` 之外的子命令。

## 交付方式
见模板（worktree / `wt/<卡号>` / `git push origin` / PR base=main / 不合并 / request_review）。

## 验收三段式
--limit 3 | PASS | $ PYTHONPATH=src python3 -m team_board.cli list --limit 3 | exit 0 | 打印 3 行卡
--limit 0 | PASS | $ PYTHONPATH=src python3 -m team_board.cli list --limit 0 | exit 2 | argparse 报越界

## 长内容去哪
完整需求背景见评论 #1；实测日志见附件 `t_xxxxxxxx-list.log`。

## 验收标准
QA-SPEC: qa
`--limit` 取 1 / 200 / 201 三边界与 0 各一条实测，缺一条即不通过。
```

字节数的产生命令（本卡实测，日志见卡 `t_7a643b19` 的附件）：

```
$ wc -c sample-body.md        # 与上面 ```text 块逐字节相同
834 sample-body.md
$ wc -m sample-body.md
558 sample-body.md
```
