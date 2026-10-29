# 合并认账与提交身份（判据①②③）

谁合的 PR、谁写的提交，都要能**从机器可判的元数据里查得到**，不靠账号、不靠自觉。
本文件写清三条判据、各自的判定命令、以及每个 bot 怎么持有自己的提交者身份。

三条判据（全部有命令、全部进 CI）：

1. **判据① `merged-by` == 该卡 assignee**：合并提交必须带认账三字段
   `merged-by: <profile>/run<NNN>` / `merge-card: t_xxxxxxxx` / `merge-pr: NN`
   （**小写键名**；旧的大写键 `Merge-By` 等已废除，不再接受），且 `merged-by`
   的 profile 部分严格等于合并时该卡的 `tasks.assignee`。
2. **判据② author 白名单**：卡分支 `wt/<卡号>` 上每条非合并提交的 author 必须是
   `{该卡**建卡时** assignee, engineer, qa}` 之一。
3. **判据③ committer 是本 bot**：每条提交的 committer 必须是
   `.team-board/bots.tsv` 里登记过的 bot 身份，且与该提交的 author 是同一个 profile。

本仓的解法是**在合并提交 message 末尾写三行 trailer**，并把「机器能判什么 /
判不了什么」分成三层写死。三层不许互相冒充：甲层是机器判定，乙层是卡面留痕
（内容自述、机器只判格式与交叉一致性），丙层是机器**做不到**的部分，只能靠
约定与人工复核。

```
merged-by: <profile>/run<NNN>       # 例 pm/run137
merge-card: t_[0-9a-f]{8}           # 例 t_ad5e7ff6
merge-pr: #<正整数>                  # 例 #1
```

## 判据① · `merged-by` == 卡 assignee

```
PYTHONPATH=src python -m team_board.cli merge-audit --repo . --card t_xxxxxxxx --require; echo rc=$?
```

`0` = 该卡有零问题的认账合并；`1` = 缺字段 / 键名大写 / 重复字段 / 字段没落在
trailer 区 / `merged-by` 与 assignee 不符；`2` = 参数或环境错。
合并前用 `merge-record` 生成认账块，照抄它打印的 `gh pr merge …` 命令，别手写。

## 判据② · author 白名单（取「建卡时 assignee」）

```
PYTHONPATH=src python -m team_board.cli commit-audit --repo . --card t_xxxxxxxx --rev HEAD; echo rc=$?
```

- 白名单 = `{该卡建卡时 assignee, engineer, qa}`。**建卡时 assignee** 取自看板库
  `task_events` 里该卡第一条 `kind='created'` 事件的 `payload.assignee`。
- **为什么不用 `tasks.assignee`**：`kanban_request_review` 会把卡改派给
  reviewer（通常是 `pm`），若拿 `tasks.assignee` 当白名单，同一批提交在验收前
  判红、验收后被静默放宽成 `{pm, engineer, qa}` 变成绿 —— 判据② 就成了时间相关
  的假闸门。取建卡事件后，验收前、验收后结论一致。
- 取不到建卡事件（库不可读、没有 `task_events` 表、没有 `created` 事件、
  payload 不是合法 JSON 或没有非空 `assignee`）→ **一律 rc2**（fail closed），
  不回退 `tasks.assignee`、不静默放过。
- author 邮箱的 localpart（`@` 之前那段）不在白名单 → 问题 `author 不在白名单`，
  rc1。区间为空 → 输出一行 `区间内无提交`，rc0。

## 判据③ · committer 是本 bot 且与 author 一致

同一条 `commit-audit` 命令一并判：

- committer 的 localpart 必须登记在 `.team-board/bots.tsv` 的 profile 集合里，
  否则问题 `committer 不是本 bot`；
- committer 与 author 的 localpart 必须相同，否则 `committer 与 author 不一致`
  （两条可同时出现）；
- author 的 localpart 不在身份表里，另报 `身份表里没有该身份`（免得只看到
  「committer 不是本 bot」而以为作者没问题）。

这条同时挡住三种绕过：`--no-verify`（钩子被跳过，但提交元数据还在）、提交消息里
已有 `Co-authored-by`（不影响 committer 字段）、hooks 被重装（`core.hooksPath`
清掉也不影响事后稽核）。所以**判定权威在 `commit-audit` + CI job
`commit-identity`**，钩子只做早警。

CI 侧：job `commit-identity` 在 PR 上用 `github.event.pull_request.head.sha`
（不是 `refs/pull/N/merge` 合成提交）当 `--rev`，分支名取 `github.head_ref`
（PR 的 checkout 是 detached HEAD，`git branch --show-current` 是空的），看板库由
`scripts/make-audit-board.py` 用 `.team-board/cards.tsv`（`card<TAB>建卡时 assignee`
镜像）在 runner 上现造。卡不在 `cards.tsv` 里 → 该步骤 rc2，**不许跳过**。

## 甲层 · 机器可判、可拦

合并提交 message 末尾的 trailer 三字段**齐 + 格式合法** → `merge-audit` rc0；
缺字段 / 格式非法 / 重复字段 / 字段未落在 trailer 区 → rc1。CI job
`merge-accountability`（另一张卡加）常驻跑同一判定，红就是真红 —— 无认账字段的
合并动作会被机器拦下或告警（rc≠0），补齐后同一条检查 rc=0。

```bash
team-board merge-audit --repo . --card t_xxxxxxx --require; echo "rc=$?"
```

甲层只认「提交 message 里有什么」，不认「谁写的」。它判不了账号身份（见丙层）。

## 乙层 · 卡面留痕（内容自述，机器只判格式与交叉一致性）

卡面认账块由 `merge-record` 生成、合并后由
`merge-audit --card <卡号> --require` 回读合并提交 sha 补一行：

1. 合并前：`team-board merge-record --card t_xxx --by pm/runNNN --pr NN --body-out /tmp/t_xxx-merge-body.txt`
   打印 body 段（三行 trailer）+ 卡面认账块说明 + `gh pr merge` 命令模板（**只打印，
   不执行**）；`--check-file PATH` 是另一模式：合并前预检一份 body 文件是否三字段齐
   （不合格 rc1，参数/文件级错误 rc2）。
2. 合并后：`team-board merge-audit --repo . --card t_xxx --require; echo "rc=$?"`
   rc0 时把回读到的三字段 + `gh pr view NN --json mergeCommit` 的 sha 贴卡。

`merged-by` 是 `profile/run<NNN>` 形式的 run 标签（R8 口径：执行者角色 + run 号），
**不是** GitHub 层面的身份证明。乙层的内容是自述，机器只能判它的格式（正则）与
与参数的交叉一致性（`--card/--by/--pr` 与文件内容不符 → rc1 `字段与参数不一致`）。
两层的结论要分开写：不许把乙层的「卡面写了谁合的」写成「GitHub 层面能证明是
谁合的」。

## 丙层 · 做不到（一字一句写死）

本卡无法机器校验「账号级身份区分」：GitHub 侧单账号只记 alsj213，`merged-by` 是执行者自述的 run 标签，不是 GitHub 层面的身份证明；要做账号级区分需每 bot 一账号或换 Gitea，不在本卡范围。

反过来说：本仓**不提供**任何「账号级身份区分」能力，也不声称已经实现。谁想拿
`merged-by` 去追责到某个 GitHub 账号，答案只会是 `alsj213`。

## 合并操作五步（命令原文，照抄可跑）

```bash
# 1. 生成认账 body（合并前；只打印模板，不执行 git/gh）
PYTHONPATH=src python -m team_board.cli merge-record \
  --card t_xxx --by pm/runNNN --pr NN --body-out /tmp/t_xxx-merge-body.txt

# 2. R6 三条前置（不改门槛）
PYTHONPATH=src python -m team_board.cli merge-record --check-file /tmp/t_xxx-merge-body.txt
gh pr checks NN -R alsj213/team-board; echo "rc=$?"          # rc=0；no checks reported 不算绿
gh pr view NN -R alsj213/team-board --json headRefName        # 必须是 wt/t_xxx
# 卡上 R3 证据（本机实跑命令 + 真实退出码）齐

# 3. 合并（body-file 带进合并提交 message）
gh pr merge NN -R alsj213/team-board --merge \
  --subject "<subject>" --body-file /tmp/t_xxx-merge-body.txt

# 4. 记 sha
gh pr view NN -R alsj213/team-board --json mergeCommit

# 5. 回读认账（rc0 才可贴卡收尾）
PYTHONPATH=src python -m team_board.cli merge-audit --repo . --card t_xxx --require; echo "rc=$?"
```

第 5 步 rc0 后，把回读到的 `merged-by` / `merge-card` / `merge-pr` 与第 4 步的 sha
一起贴卡面。

## 每 bot 提交身份怎么配

身份表 `.team-board/bots.tsv` 是唯一身份源（`profile<TAB>name<TAB>email`，
`#` 开头的注释行与空行忽略，目前六行：pm / engineer / qa / ops / hatch / chief）。

每个 bot 在自己的 worktree 里跑一次（幂等，重复跑不报错）：

```
bash scripts/setup-bot-identity.sh engineer /home/liu/project/tb-wt/t_xxxxxxxx
```

脚本做四件事：

1. 从 `<worktree>/.team-board/bots.tsv` 查 profile（查不到 → stderr 提示 + `exit 2`）；
2. 主仓开 `git config extensions.worktreeConfig true`；
3. 给**这个 worktree** 写 `git config --worktree user.name` / `user.email`
   （只影响本 worktree，不动主仓、不动别的卡）；
4. 打印写入后的 `user.email`。

`.githooks/pre-commit` 只做**早警**：`user.email` 的 localpart 不在身份表里、或
`git var GIT_COMMITTER_IDENT` 里的邮箱与它不一致（`git -c user.email=… commit`
就是这么暴露的）→ 打印 `[pre-commit] committer 不是本 bot：<实际>；…` 并 `exit 1`。
钩子可以被 `--no-verify` 或重装 `core.hooksPath` 绕过，**判定权威在 `commit-audit`
与 CI job `commit-identity`**，钩子只是让人早点发现配错了。

## 基线口径与残余风险

- 判定区间由基线文件 `.team-board/merge-audit-baseline` 决定（另一张卡建），也可用
  `--baseline REV` 显式给；两者只能给一个，都不给时读默认文件，**文件缺失即 rc2**
  （不静默当成「没有基线」）。
- 把基线文件改成新 sha 会让判定区间变空 = 等于关闸。这条改动**会出现在 PR diff
  里**，属「明着关闸」、不是隐蔽豁免；审 PR 的人看得见。
- 工具**不提供** `--skip` / `--force` / `--ignore` 任何降级开关：要么三字段齐，要么红。

## 历史口径

基线之前的合并只能回读 PR 号 / 卡号 / sha，`merged-by` 一律写「无法判定」——
那些提交里本来就没有该字段。**不做历史批量回填**：回填等于用今天的自述覆盖
当时的事实，比留白更糟。

## 边界（不许写成「已解决」）

- **本卡无法机器校验『账号级/页面级身份区分』**：GitHub 侧单账号只记 `alsj213`，
  提交元数据里的 bot 身份不改变页面显示。**GitHub 页面恒显示 alsj213，本卡不承诺解决**。
- 真身份区分要每 bot 一账号，或换 Gitea —— 明写不在本卡范围。
- 历史口径：基线之前的合并只能回读 PR 号 / 卡号 / sha，`merged-by` 写「无法判定」，
  不做历史批量回填。
- 不加豁免、不加 `--skip` / `--force`、不减 CI 检查项、不临时关闸门。
