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

## 边界（不许写成「已解决」）

- **本卡无法机器校验『账号级/页面级身份区分』**：GitHub 侧单账号只记 `alsj213`，
  提交元数据里的 bot 身份不改变页面显示。**GitHub 页面恒显示 alsj213，本卡不承诺解决**。
- 真身份区分要每 bot 一账号，或换 Gitea —— 明写不在本卡范围。
- 历史口径：基线之前的合并只能回读 PR 号 / 卡号 / sha，`merged-by` 写「无法判定」，
  不做历史批量回填。
- 不加豁免、不加 `--skip` / `--force`、不减 CI 检查项、不临时关闸门。
