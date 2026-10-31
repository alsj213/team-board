# 卡正文模板 v3

**版本 `v3`**（2026-10-31 定）。本文件是模板的**唯一源**：六块固定段落只在文末锚点块里存
一份，卡正文**读它、不抄它**；卡正文只写与本卡不同的部分。

> **依赖与合并顺序（未合入，先读这段）**：下文引用的两项实现都不在 `main` 上 ——
> ① **PR #63**（`wt/t_7c17f775`）：`src/team_board/cardtemplate.py`（`TARGET_BYTES` / `WARN_BYTES` /
>    `FIXED_HEADING_RE` / `scan` / 切点 `FIXED_SECTION_EPOCH`）；
> ② **PR #65**（`wt/t_eb47566b`）：`scripts/install-card-template.sh`。
> **合并顺序固定：`#63 → #65 → #62`。** 三者合一前 `main` 上没有这两个可执行物，下文凡引用
> 它们都按「合入后生效」读；今天照做是 `No such file`（rc=127）。

## 一、引用行（新卡正文第一行）

- 新卡正文**首行**（顶格、整行）：`TEMPLATE: card-template.md#v3`
- **合并类卡**：首行仍是 `PR-CLAIM: #<n>`（整行首行，见 [`docs/pr-claim-gate.md`](pr-claim-gate.md)），
  `TEMPLATE: card-template.md#v3` 放**第二行**。
- 缺这一行 → `cardtemplate.scan`（PR #63，未合）记一条 warn；内核注入侧按它贴固定段落（`t_c0f2c33e`），
  钉版本是为了「模板改了、老卡不被静默换口径」。

## 二、五段骨架（标题逐字，顺序不许换）

骨架就是文末**样例块**里的五行 `## ` 标题（全文只在那里出现一次，照抄即可）：

- `## 目标` —— 一句话说清交付什么；不写背景、不写过程、不写「为什么」。
- `## 现状证据` —— 一行给出现场依据（文件名 + 行号 / 一条已跑过的命令与输出），不写猜测。
- `## 差异要求` —— 只写与模板默认口径**不同**的部分：参数取值域、退出码、输出格式的每处偏离；默认全同就写「见模板」。
- `## 验收标准` —— 标题**逐字**就是这五个字：不许加「(qa 签)」后缀、不许改写、不许换序
  （内核按 `_QA_SPEC_HEADING` 逐字匹配，写错**建卡即拒**）；该段**第一行**必须是顶格
  `QA-SPEC: qa`，其后每条判据一行（格式见文末「验收三段式」）。
- `## 边界例外` —— 明确**不做**什么：不许碰的文件 / 模块 / 动作各列一句，防止顺手扩大范围。

## 三、固定段落（唯一一份，只读不抄）

下面锚点块里的六段是**默认口径**，新卡正文**不许抄**：这六个标题已被建卡闸门拦
（`FIXED_HEADING_RE`，`src/team_board/cardtemplate.py`（PR #63，未合）+ 内核镜像），卡正文里出现
`## 完成契约` 这类标题 → **建卡 rc2、卡不落库**。要表达时写「见模板 · 完成契约」。

锚点是两行整行 HTML 注释（顶格、不缩进）：`FIXED:v3:` 前缀 + `BEGIN` / `END`。内核按版本取
这一段贴进 `worker_context`（`t_c0f2c33e`），`bash scripts/install-card-template.sh --check`（PR #65，未合）
按它判漂移（`t_eb47566b`）。

含 PR 号的卡，关闭前还必须跑 `merge-owner-audit <卡号>`：`rc=0` 才许关；`UNOWNED` 先建收口卡（首行 `PR-CLAIM: #<n>`）或把 PR 合掉；`rc=2` 按错误修、不算通过。口径与降级日志见 [`docs/merge-owner.md`](merge-owner.md)。

<!-- FIXED:v3:BEGIN -->
## 完成契约

- 收口 verdict 三选一开头：`PASS` / `ISSUES` / `BLOCKED`；**PASS 必须给「命令原文 + 真实退出码」**，自述（「已测试」「应该可以」）不算证据。
- 带 `QA-SPEC: qa` 签署行的实现卡：engineer **不自己关单**，交 `kanban_request_review(reviewer="qa")`；PASS 归 qa。
- 判 `ISSUES`：同一会话内交 qa 或建卡给 pm，不许静默 `kanban_complete`；判 `BLOCKED`：`kanban_block(reason=…)`。

## 验收三段式

- 每条判据一行，格式写死：`<条目> | PASS | $ <命令> | exit <码> | <输出末 1-3 行>`。
- 没跑过的写 `NOT TESTED`，不许编 PASS；退出码非 0 不许写 PASS。
- 命令必须可复跑（不依赖临时文件 / 一次性 shell 状态 / 随机数据）。
- 证据落文件 `<卡号>-<用途>.log` 并 `kanban_attach` 上卡；卡面只留结论、绝对路径、一行 `$ 命令 | exit N`。

## 边界默认值

- 只改卡里列出的文件；不动 `src/` 与 `tests/`（除非卡里写死允许）。
- 不改 2048 上限与 `OVER_LIMIT_TMPL`；不碰凭据 / 密钥 / `.env` / token。
- 不碰 `merge_guard` 判据、不碰 R7 风险阈值、不改验收标准。
- 不推 main、不合并 PR、不跨卡改别人的模块；发现额外工作 → `kanban_create` 交 pm，别自己扩范围。

## R6 三条

- ① `gh pr checks <n>` **rc=0 且不是** `no checks reported` —— 后者是 `NO_CHECKS`（rc=1），不算绿。
- ② 卡上有 R3 证据：每条 PASS 对应一条本机实跑命令 + 真实退出码。
- ③ head 分支名 = `wt/<卡号>`：`gh pr view <n> --json headRefName` 核对，不看样子。
- 评审只认 PR 页面 Review 记录（`gh pr review <n> --comment`），**不要求 approve**；merge 不归 engineer。

## 交付方式

- worktree `/home/liu/project/tb-wt/<卡号>`、分支 `wt/<卡号>`、推送 `git push origin HEAD:refs/heads/wt/<卡号>`（SSH over 443，见 R10）。
- PR base=main，**不合并**；交卡 `kanban_request_review(reviewer="qa")`。
- 提交前 `bash scripts/setup-bot-identity.sh engineer <worktree>`（CI `commit-identity` 认 `bots.tsv`）。

## 长内容去哪

- 长背景 / 日志 / 证据 → 卡评论（`kanban_comment`）或附件（`kanban_attach`），正文只留一句「见评论 #N / 附件 <文件名>」。
- 检查器全量输出落 `evidence/<gate>-<卡号>.log`，卡面只贴摘要 + 绝对路径。
<!-- FIXED:v3:END -->

## 四、阈值（三档，2048 硬上限不变）

| 档 | 字节（UTF-8） | 判定 |
| --- | --- | --- |
| 目标 | ≤ 800 | 合规，鼓励按此写 |
| warn | > 1200 | `cardtemplate.scan`（PR #63，未合）记 warn，建议压回 800 |
| 硬上限 | 2048 | 建卡即拒：rc=2、stderr 恰一行、卡不落库（不截断、无开关） |

- 数字各有唯一源，本文件不复写第三份字面量：`TARGET_BYTES = 800` / `WARN_BYTES = 1200`
  （`src/team_board/cardtemplate.py`，PR #63 未合）、`BODY_LIMIT_BYTES = 2048`（`src/team_board/bodylimit.py`）。
- **存量不追溯**：切点 `1793462400`（PR #63 `FIXED_SECTION_EPOCH`，未合；`2026-11-01 00:00:00 CST`，实测 `date -d @1793462400`）。
  `created_at < 切点` 的卡按存量豁免 —— 不判红、不改写正文，只由 `body-audit` 记录与列出。
- 按**字节**不按字符：700 个汉字 = 700 字符 / 2100 字节，超限。细则见 [`docs/card-limit.md`](card-limit.md)。

## 五、样例（v3 五段，实测 650 字节）

下面 ```text 块里的内容就是「骨架 + 样例」同一份：照抄这五行 `## ` 标题，正文按本卡改。

```text
TEMPLATE: card-template.md#v3

## 目标
给 `list` 子命令加 `--limit N`，只打印前 N 行。

## 现状证据
`cli.py:412` 的 list handler 没有该参数，`--help` 里查不到。

## 差异要求
见模板；新增 `--limit`，取值 1–200 的十进制整数，越界 rc=2。

## 验收标准
QA-SPEC: qa
--limit 3 | PASS | $ PYTHONPATH=src python3 -m team_board.cli list --limit 3 | exit 0 | 打印 3 行
--limit 0 | PASS | $ PYTHONPATH=src python3 -m team_board.cli list --limit 0 | exit 2 | argparse 拒

## 边界例外
不做分页、不改输出列；不碰 `list` 之外的子命令。见模板 · 完成契约 / 交付方式。
```

字节数的产生命令（本卡实跑，逐字节等于上面块内内容）：

```
$ awk '/^```text$/{f=1;next}/^```$/{f=0}f' docs/card-template.md > /tmp/sample-v3.md
$ wc -c < /tmp/sample-v3.md
650
```

## 六、安装与指针

- 装到内核读的模板目录 + 漂移检查：`bash scripts/install-card-template.sh [--check]`（**PR #65，未合**）
  （`main` 上没有该文件，今天执行是 `No such file`，rc=127；`--check` rc1 = 漂移或缺目标，见 `t_eb47566b`）。
  落点以 `--dest` 为准，**默认必须等于内核 `kanban_home()/templates/card-template.md`** ——
  即 `${HERMES_KANBAN_HOME:-$HOME/.hermes}/templates/card-template.md`
  （`kanban_home()` = `HERMES_KANBAN_HOME` else `~/.hermes`，**不是** `~/.hermes/kanban`；
  内核 `_card_template_path()` 另有 `${HERMES_CARD_TEMPLATE}` 绝对路径覆盖）。
  #65 脚本默认 dest 目前写成 `$HOME/.hermes/kanban/templates/…`（差一层 `kanban`），偏差归该卡返工
  `t_7e847105`；在它修正前显式传 `--dest "${HERMES_KANBAN_HOME:-$HOME/.hermes}/templates/card-template.md"`。
- 合并顺序：**#63 → #65 → #62**（本文件引用的两张未合 PR 合入后，上面这条才可执行）。
- 2048 上限的条文、实测与边界：[`docs/card-limit.md`](card-limit.md)；建卡闸门与 `body-audit`：README「卡正文上限」节。
