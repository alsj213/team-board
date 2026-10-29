# 合并认账：谁按了合并、依据哪张卡、合并提交/PR 号

出事之后要回查「这个 PR 是谁合的、依据哪张卡」，在本仓曾经**查不出来**：GitHub
侧只有一个共用账号 `alsj213`，合并记录里没有执行者信息，卡面也没有认账字段。

本仓的解法是**在合并提交 message 末尾写三行 trailer**，并把「机器能判什么 /
判不了什么」分成三层写死。三层不许互相冒充：甲层是机器判定，乙层是卡面留痕
（内容自述、机器只判格式与交叉一致性），丙层是机器**做不到**的部分，只能靠
约定与人工复核。

```
Merge-By: <profile>/run<NNN>       # 例 pm/run137
Merge-Card: t_[0-9a-f]{8}          # 例 t_ad5e7ff6
Merge-PR: #<正整数>                 # 例 #1
```

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

`Merge-By` 是 `profile/run<NNN>` 形式的 run 标签（R8 口径：执行者角色 + run 号），
**不是** GitHub 层面的身份证明。乙层的内容是自述，机器只能判它的格式（正则）与
与参数的交叉一致性（`--card/--by/--pr` 与文件内容不符 → rc1 `字段与参数不一致`）。
两层的结论要分开写：不许把乙层的「卡面写了谁合的」写成「GitHub 层面能证明是
谁合的」。

## 丙层 · 做不到（一字一句写死）

本卡无法机器校验「账号级身份区分」：GitHub 侧单账号只记 alsj213，Merge-By 是执行者自述的 run 标签，不是 GitHub 层面的身份证明；要做账号级区分需每 bot 一账号或换 Gitea，不在本卡范围。

反过来说：本仓**不提供**任何「账号级身份区分」能力，也不声称已经实现。谁想拿
`Merge-By` 去追责到某个 GitHub 账号，答案只会是 `alsj213`。

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

第 5 步 rc0 后，把回读到的 `Merge-By` / `Merge-Card` / `Merge-PR` 与第 4 步的 sha
一起贴卡面。

## 基线口径与残余风险

- 判定区间由基线文件 `.team-board/merge-audit-baseline` 决定（另一张卡建），也可用
  `--baseline REV` 显式给；两者只能给一个，都不给时读默认文件，**文件缺失即 rc2**
  （不静默当成「没有基线」）。
- 把基线文件改成新 sha 会让判定区间变空 = 等于关闸。这条改动**会出现在 PR diff
  里**，属「明着关闸」、不是隐蔽豁免；审 PR 的人看得见。
- 工具**不提供** `--skip` / `--force` / `--ignore` 任何降级开关：要么三字段齐，要么红。

## 历史口径

基线之前的合并只能回读 PR 号 / 卡号 / sha，`Merge-By` 一律写「无法判定」——
那些提交里本来就没有该字段。**不做历史批量回填**：回填等于用今天的自述覆盖
当时的事实，比留白更糟。
