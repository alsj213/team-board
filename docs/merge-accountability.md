# 合并认账与提交身份（判据①②③）

谁合的 PR、谁写的提交，都要能**从机器可判的元数据里查得到**，不靠账号、不靠自觉。
本文件写清三条判据、各自的判定命令、以及每个 bot 怎么持有自己的提交者身份。

三条判据（各有机器命令；②③ 全程进 CI，① 在 CI 只跑**字段判据**、卡级比较在验收时跑）：

1. **判据① `merged-by` == 该卡 assignee**：合并提交必须带认账三字段
   `merged-by: <profile>/run<NNN>` / `merge-card: t_xxxxxxxx` / `merge-pr: NN`
   （**小写键名**；旧的大写键 `Merge-By` 等已废除，不再接受），且 `merged-by`
   的 profile 部分严格等于合并时该卡的 `tasks.assignee`。
2. **判据② author 白名单**：卡分支 `wt/<卡号>` 上每条非合并提交的 author 必须是
   `{该卡**建卡时** assignee, engineer, qa}` 之一。
3. **判据③ committer 是本 bot**：每条提交的 committer 必须是
   `.team-board/bots.tsv` 里登记过的 bot 身份，且与该提交的 author 是同一个 profile。

判据① 在 CI 端跑的是**字段判据**（口径澄清，不是减检查项）：job `merge-accountability`
判 trailer 三字段**齐 + 格式合法**（缺字段 / 格式非法 / 重复字段 / 字段未落在 trailer 区
→ rc1）。而「`merged-by` 的 profile == 该卡 `tasks.assignee`」这一步 CI 端**做不到**：
一条合并提交属于哪张卡要查看板，CI 里没有「本次合并对应哪张卡」这个输入。于是卡级比较
由**验收时**跑 `team-board merge-audit --card <卡> --require` 完成（输出带判据① 行与 rc）。
CI 不是「豁免判据①」：它的字段部分已在 CI 常驻判，卡级部分随卡验收判，两段都不缺。

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
合并前用 `merge-record` 生成认账块，照抄它打印的 `merge_guard` 命令（body 就是同一份
`--body-out` 文件），别手写。

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

CI 侧：判定逻辑全部在 `scripts/ci-commit-identity.sh`（job `commit-identity` 只做
checkout / setup-python / fetch / 调脚本四步），契约
`bash scripts/ci-commit-identity.sh <repo-path> <branch> <rev>`，退出码 0 / 1 / 2 与
`commit-audit` 同义。脚本在两处与直接跑 `commit-audit` 不同：

1. **镜像只认 main**：临时看板库的 `card<TAB>建卡时 assignee` 表来自
   `git show origin/main:.team-board/cards.tsv`（取不到就当空表），**工作树里
   `.team-board/cards.tsv` 不参与判定** —— 否则 PR 可以在自己被审的树里改这张表，
   等于自己放宽判据②（自证通道）。
2. **未知卡回落默认白名单**：`<branch>` 是 `wt/<卡号>` 而该卡不在 main 的镜像表里时，
   脚本往副本追加一行 `$卡号<TAB>engineer` 并打印
   `卡 <卡号> 不在 main 的镜像表 → author 白名单取默认 {engineer, qa}`，白名单因此
   回落到 `{engineer, qa}`。**不 fail closed**：否则每张新卡都要先改同一个文件才能
   开 PR，并行 PR 必然冲突，且「补一行」发生在被审的 PR 自己身上。
   `{engineer, qa}` 是最严的安全默认（pm / hatch / chief 写代码本来就违规）。
`scripts/make-audit-board.py` 自己的 fail closed（表里行坏、卡既不在表里也没被追加）
保持原样，脚本只是透传它的 rc2。

`<repo-path>` 指向别的 worktree 时，`make-audit-board.py` / `team_board.cli` /
`bots.tsv` 仍取脚本所在的这棵树（与 `branch-audit` job「audit must run this build」
同口径）；`--repo` 才是被稽核的仓库。

CI 侧另两点不变：`--rev` 用 `github.event.pull_request.head.sha`（不是
`refs/pull/N/merge` 合成提交），分支名取 `github.head_ref`
（PR 的 checkout 是 detached HEAD，`git branch --show-current` 是空的）。

## 甲层 · 机器可判、可拦

合并提交 message 末尾的 trailer 三字段**齐 + 格式合法** → `merge-audit` rc0；
缺字段 / 格式非法 / 重复字段 / 字段未落在 trailer 区 → rc1。CI job
`merge-accountability`（另一张卡加）常驻跑**这一段字段判据**（判据① 的卡级比较随卡
验收跑，见上），红就是真红 —— 无认账字段的合并动作会被机器拦下或告警（rc≠0），
补齐后同一条检查 rc=0。

**判什么：只判 first-parent 链上的合并** —— 即「把卡分支合进 main」的那个合并提交，
也就是真正的「合并一张 PR」事件。工具按**被审 `rev` / `baseline` 自己的 first-parent
链**取数（判定段沿 `rev`，历史段沿 `baseline`），所以：

- **起点在 main 线上时**（`merge-audit --repo .` 在 main checkout 的默认 `HEAD`；
  CI `merge-accountability` job 的 `push` 事件 `--rev = ${{ github.sha }}` —— 判的是
  **本次 push 的那个提交**，不是 `origin/main`：并发合并时 fetch 到的 `origin/main`
  可能已前进到别人的提交上，红就会记错提交）：分支侧
  `git merge origin/main` 产生的同步合并（把 main 并回卡分支）落在 second-parent 链上，
  既没合任何 PR，也本来就不该有 `merge-card` / `merge-pr`，因此**既不判、也不计入**
  records / problems / 历史段。
- **起点不在 main 线上时结论随起点变**，这不是豁免：`--rev` 给的是**卡分支尖端**时
  （CI `pull_request` 事件就是 `--rev = github.event.pull_request.head.sha`），该分支
  自己的同步合并仍在它自己的 first-parent 链上，**照旧被判**（缺三字段即 rc1）；
  `--baseline` 给成卡分支侧提交时，历史段只沿那条卡分支的链回显，main 侧已合并的 PR
  合并会从历史段消失（视角变化，判定段与 rc 不受影响）。
  判据跟着**起点自己的链**走，不跟着「这条提交是不是同步合并」走。

工具里没有反向开关（没有 `--all-merges` 之类）：判定对象是口径，不是可选项。

```bash
team-board merge-audit --repo . --card t_xxxxxxx --require; echo "rc=$?"
```

甲层只认「提交 message 里有什么」，不认「谁写的」。它判不了账号身份（见丙层）。

## 乙层 · 卡面留痕（内容自述，机器只判格式与交叉一致性）

卡面认账块由 `merge-record` 生成、合并后由
`merge-audit --card <卡号> --require` 回读合并提交 sha 补一行：

1. 合并前：`team-board merge-record --card t_xxx --by pm/runNNN --pr NN --branch wt/t_xxx --body-out /tmp/t_xxx-merge-body.txt`
   打印 body 段（三行 trailer）+ 卡面认账块说明 + 一行 `merge_guard` 合并命令（R12 ① 的
   唯一入口，带齐 `--delete-branch` 与认账四参数；**只打印，不执行**）；`--check-file PATH`
   是另一模式：合并前预检一份 body 文件是否三字段齐
   （不合格 rc1，参数/文件级错误 rc2）。
   打印行里 `--branch` / `--repo`（含 `cd` 与 `PYTHONPATH` 两处拼接）与 `--subject` /
   `--body-file` 一样过 `shlex.quote`：仓库路径含空格、分支名含 `;` / `$` 时照抄仍是
   **一条**命令；`--subject` 含换行 → rc2（否则打印行被切成半条）。
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
# 1. 生成认账 body + 一行可直接照抄的 merge_guard 合并命令（只打印，不执行 git/gh）
PYTHONPATH=src python -m team_board.cli merge-record \
  --card t_xxx --by pm/runNNN --pr NN --branch wt/t_xxx --body-out /tmp/t_xxx-merge-body.txt

# 2. R6 三条前置（不改门槛）
PYTHONPATH=src python -m team_board.cli merge-record --check-file /tmp/t_xxx-merge-body.txt
gh pr checks NN -R alsj213/team-board; echo "rc=$?"          # rc=0；no checks reported 不算绿
gh pr view NN -R alsj213/team-board --json headRefName        # 必须是 wt/t_xxx
# 卡上 R3 证据（本机实跑命令 + 真实退出码）齐

# 3. 合并（唯一入口 merge_guard：--body-file 带进合并提交 message，认账三字段随命令走）
python -m team_board.merge_guard --pr NN --branch wt/t_xxx --method merge --delete-branch \
  --card t_xxx --by pm/runNNN --subject "<subject>" \
  --body-file /tmp/t_xxx-merge-body.txt; echo "rc=$?"
# --subject / --body-file 缺任一项即 rc2、零 gh 调用：不带它们时 gh 用默认文案生成合并
# 提交，message 里没有任何 trailer，第 5 步的 merge-audit --require 必红。
# --body-file 必须是绝对路径（相对路径会被进程 cwd 与 --repo 两套 cwd 解析成不同文件）：
# 给相对路径 → rc2、零 gh 调用。

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
- **CI 只认 base 侧那份文件（机器强制，不靠人看 diff）**：`merge-accountability` job
  先 `git show <BASE_REF>:.team-board/merge-audit-baseline` 写出到
  `$RUNNER_TEMP/baseline`，再用 `--baseline-file` 喂给工具；工具只认这个参数的内容，
  **不读检出树里的那份**。`BASE_REF` 的定义：`pull_request` → `origin/<base_ref>`
  （main 侧提供基线）；`push` → `github.event.before`（上一个 main 尖端）。
  于是被审 PR 改自己那份基线**不生效** —— 旧口径只写「这条改动会出现在 PR diff 里、
  审的人看得见」（纯 prose、无校验），现在由机器挡住。
  读不到该文件（新分支 push 的 `before` 全 0、或 base 侧根本没有这条文件）→ `rc=2`
  红，属**预期 fail-closed**，不是配置失误。
- **方向断言（防静默假绿）**：基线必须是被审 `rev` 的**祖先**，否则 `rc=2` +
  `错误: 基线不是被审 rev 的祖先：<B> / <R>`。`git log <baseline>..<rev>` 在基线不在
  rev 上游时会退化成空区间，输出「判定（0 条）/ 结论：无问题」并 rc0，与「真的没有
  任何合并」不可区分 —— 那种假绿比红更坏，所以一律报错而不是照样跑。
- 把基线文件改成新 sha 仍然等于关闸（区间变空）；但因为基线必须来自 base 侧，PR 自己
  推不动它 —— 推基线要单独一张卡、改 main 上的那份文件，且在 `push` 事件里同样受方向
  断言约束（基线到不了 rev 之外）。
- 工具**不提供** `--skip` / `--force` / `--ignore` 任何降级开关：要么三字段齐，要么红。

### 基线 = 历史不追溯点（本 job 引入时的口径）

- 基线取**引入 `merge-accountability` 这个 job 那一刻的 main 尖端**：
  `8cd790c13899acedd75a57020f896f38d7cc3194`（PR #28 合并提交）。它同时是
  **历史不追溯点**：基线之前的合并一律不回审、不要求 `Merge-By` / `Merge-Card` /
  `Merge-PR`（那些提交里本来就没有这些字段）。
- CI 的比较区间因此被限定为 `(基线, 被审 rev]`，即只有「基线之后新产生的合并」必须
  三字段齐。**这是区间限定，不是豁免出口**：区间里任何一条缺字段依旧判红，退出码
  语义、检查项一个不减。
- 基线**不得**为了「让红变绿」而继续往前推：再往前推就是区间变空 = 关闸。真要
  推进基线（例如换了合并工具），必须单独开卡、在 PR diff 里明示，并按上面的
  「明着关闸」口径评审。本次一次性初始化只做一次，理由记在卡 `t_5dcafcc7`
  （老板 2026-10-29 批准的二选一中的 (b)：明确写出历史不追溯，并在 CI 里限定比较区间）。
- 依据（决策原话）：老板 2026-10-29 批准「(b) 明确写出历史不追溯，并在 CI 里限定
  比较区间」；不许用豁免文件、不许减检查项、不许把 job 改成 `continue-on-error`。

## 历史口径

基线之前的合并只能回读 PR 号 / 卡号 / sha，`merged-by` 一律写「无法判定」——
那些提交里本来就没有该字段。**不做历史批量回填**：回填等于用今天的自述覆盖
当时的事实，比留白更糟。

历史段与判定段用同一条 `--first-parent`，但**两段各自沿自己的起点取数**：判定段沿
被审 `rev` 的链，历史段沿 `baseline` 的链。所以在 main 视角下（`rev` 与 `baseline`
都在 main 线上）分支侧的同步合并（second-parent 侧）在历史段里同样不出现、不计数 ——
它不是认账事件。起点不在 main 线上时结论随起点变：`--rev` 给卡分支尖端，该分支自己的
同步合并就在判定段的链上、照旧被判；`--baseline` 给卡分支侧提交，历史段只沿那条卡分支
的链回显，main 侧已合并的 PR 合并会从历史段消失。**那是视角变化，不是豁免** ——
判定段的结论与 rc 语义都不受影响。

`--baseline` 应给 **main 线上的提交**（默认的 `--baseline REV` 与基线文件
`.team-board/merge-audit-baseline` 都按这个用）。历史段取数**包含基线自身**：
`git log --merges --first-parent <baseline>` 会列出基线这条提交（若它是合并提交），
与判定段的 `(baseline, rev]` 不重叠。

## 边界（不许写成「已解决」）

- **本卡无法机器校验『账号级/页面级身份区分』**：GitHub 侧单账号只记 `alsj213`，
  提交元数据里的 bot 身份不改变页面显示。**GitHub 页面恒显示 alsj213，本卡不承诺解决**。
- 真身份区分要每 bot 一账号，或换 Gitea —— 明写不在本卡范围。
- 历史口径：基线之前的合并只能回读 PR 号 / 卡号 / sha，`merged-by` 写「无法判定」，
  不做历史批量回填。
- 不加豁免、不加 `--skip` / `--force`、不减 CI 检查项、不临时关闸门。
