# 合并前 rebase 闸门（merge_guard）

2026-10-29 由卡 `t_c836202a`（字符串组 2/3，老板直接指令的「强制约束②」）落地，父需求卡
`t_0b2fd9a3`。实现文件只有一个：`src/team_board/merge_guard.py`（函数 `rebase_gate` 与它的
四个纯函数）。**没有**动 `branches.py`、没有加 CI 侧规则、没有新 CLI 子命令。

## 条文

### 1. 判据（三条命令，顺序固定）

闸门位置：`_run_merge()` 里，`gh pr view` 预检的三次校验（`state == OPEN` /
`headRefName == --branch` / `--branch != baseRefName`）之后、`lines = [...]` 之前。
因此拒绝时 stdout **0 字节**（证据行还没开始攒）、stderr **恰一行** `错误: …`、
**零 `gh pr merge`**（gh 只被调过那一次 `gh pr view`）。

```
# ① 取远端真身：一次 fetch 两个 refspec（base 取 gh pr view 的 baseRefName，不写死 main）
git -C <repo> fetch --no-tags --no-recurse-submodules --quiet <remote> \
    '+refs/heads/<base>:refs/merge-guard/<pr>/base' \
    '+refs/heads/<branch>:refs/merge-guard/<pr>/head'

# ② 交叉核对：远端 head 必须逐字等于预检读到的 headRefOid
git -C <repo> rev-parse refs/merge-guard/<pr>/head

# ③ 判定：base 是不是 head 的祖先
git -C <repo> merge-base --is-ancestor refs/merge-guard/<pr>/base refs/merge-guard/<pr>/head
```

- ② 是必需的，不许省：预检到合并之间分支被推送时，判的必须是预检看到的那条 sha ——
  与 `gh pr merge --match-head-commit` 同一个理由。
- 不许 `--depth` 浅抓：浅历史下 `merge-base --is-ancestor` 会假红
  （`src/team_board/branches.py` 已记同一个坑）。
- PR 来自 fork 的情况不用管：`ensure_branch_present()` 已经在前面把「分支不在本仓 origin」
  拦成 rc2。
- 私有 ref `refs/merge-guard/<pr>/{base,head}` 每次 `+` 强制覆盖，**不做清理**（刻意决定）：
  清理要多跑一条 `git update-ref -d`，于是多出两条失败路径，而「清理失败不许翻状态」最容易
  出 P1。它不在 `refs/heads` / `refs/remotes` 下 —— `branch-audit` 只枚举这两类，看不见；
  `git push` 也不会把它带出去（实测见下）。代价只是 pin 住两个提交对象。

### 2. 退出码归属

拒绝 = **rc2**，与「源分支在远端不存在」「PR 不是 OPEN」同档：合并前状态不满足，一个 gh
写操作都没发。**不许用 rc1**（rc1 = 尝试合并但未达成，专用码）。四条拒绝文案（前缀钉死，便于
grep / 文档 / 测试逐字断言）：

```
错误: 取远端提交失败（git fetch rc=<rc>）：<key_line>；无法判定 head 是否已 rebase 到 <base>，拒绝合并
错误: 远端 head 提交 <rev-parse 输出> 与 gh pr view 的 headRefOid <head_oid> 不一致：预检之后分支被推送，拒绝合并
错误: 分支 <branch> 落后 <base>（<base> 不是 head 的祖先，git merge-base --is-ancestor rc=1）：合并前必须先 rebase 到 <base>（必须在作者的 bot 身份 worktree 里做：bash scripts/setup-bot-identity.sh <profile> <worktree>，否则 committer≠author 会被 commit-identity 判红；卡分支上不许出现没有认账字段的同步合并提交，否则 merge-accountability 必红），拒绝合并
错误: 无法判定 <base> 是否为 head 的祖先（git merge-base rc=<rc>）：<key_line>；拒绝合并
```

`<base>` 是 `gh pr view` 的 `baseRefName`（通常 `main`）；`--remote` 非 `origin` 时文案写作
`<remote>/<base>`，不写死 "origin/main"。另有第四类入参问题：`baseRefName` 形态非法（空串 /
含空白字符 / 带 `refs/` 前缀）→ rc2，文案点名 `baseRefName`（**不是** `--branch`），
且**闸门自身不发任何 git 调用**（`fetch` 之前就拦；此前已跑过一次 `ls-remote` 存在性探针，
那是合并前的源分支存在闸门，不是本闸门发的）。

### 2b. 谁来做 rebase（拒绝文案里带短版，这里写全）

闸门只判「base 是不是 head 的祖先」，**不做也不替你选 rebase 的执行者**。补齐这一步动作的是
人，而且只有一条路成立：

- **在原作者的 bot 身份 worktree 里 rebase**：
  `bash scripts/setup-bot-identity.sh <profile> <worktree>` 先给该 worktree 装上这批提交作者的
  身份，再 `git rebase origin/main`。否则 committer 变成执行者（最典型：pm 在合并卡里 rebase
  `wt/<卡号>`），`commit-audit` 判「committer 与 author 不一致」→ rc1，CI job
  `commit-identity` 红 —— 闸门让 rebase、稽核却不认这次 rebase，同一张 PR 上同时为真就卡死。
- **「把 base 同步合并进卡分支」不是替代方案，是另一道红灯**：`git merge origin/main` 会在卡分支
  第一父链上多出一个同步合并提交，而 `merge-audit` 的 PR 形态（`--rev <head sha>`，见
  `.github/workflows/test.yml` 的 `merge-accountability` job）把这个链上**每个**合并提交都当
  「PR 合并提交」要求 `merged-by` / `merge-card` / `merge-pr` 三字段；同步合并提交没有，
  `merge-accountability` 在 PR head 上必红（rc1：三条「缺字段」）。所以卡分支上不许出现没有
  认账字段的同步合并提交。

判据本身不看「你用什么方式补上 main」，但**别的闸门会**：rebase 的方式与人选由 `commit-identity`
管，合并提交的形状由 `merge-accountability` 管。本闸门只回答祖先关系，不替那两道闸门背书。

通过时的证据行（下游 grep 这一行）：

```
rebase | PASS | git -C <repo> merge-base --is-ancestor refs/merge-guard/<pr>/base refs/merge-guard/<pr>/head | rc=0 | <base sha 前 7 位> 是 head <head sha 前 7 位> 的祖先（已 rebase 到 <base>）
```

步骤行顺序固定：`分支存在` → `预检` → `head sha` → `认领唯一性` → `双问绑定` → `rebase` → `合并` → `分支删除`。
（本文各节的样例块是各卡当时的真跑原文；现行 8 行样例见本节末尾「现行 8 行样例」，由
`tests/integration/test_merge_guard_cli.py` 的 `test_docs_declare_same_step_order` /
`test_docs_sample_block_matches_step_order` 对着实跑钉住。）
`--verify-only` 路径**不加**这一步（它一次 gh 都不调，也不宣称合并前状态）。

现行 8 行样例（2026-10-30 卡 `t_735e9250` 实跑：真 git + 假 gh，`origin` 的配置 URL 是
GitHub 形态、传输由 `url.<裸库>.insteadOf` 改写回本地裸库，一次网络调用都没有；`#20` /
`t_deadbeef` 是合法假号。脚本与完整日志是该卡附件 `t_735e9250-e2e-sample.log`）：

```
分支存在 | PASS | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_deadbeef | rc=0 | 5690e76ad629876e1beed7f32e321efc26b89db6	refs/heads/wt/t_deadbeef
预检 | PASS | gh pr view 20 --json number,state,headRefName,baseRefName,headRefOid | rc=0 | state=OPEN, headRefName=wt/t_deadbeef, baseRefName=main
head sha | PASS | 5690e76ad629876e1beed7f32e321efc26b89db6
认领唯一性 | PASS | prclaim.audit(看板库 <board>（未结卡，mode=ro）, pr=#20) | rc=0 | 认领唯一 → t_deadbeef（与 --card 逐字一致）
双问绑定 | PASS | gh api repos/alsj213/team-board/pulls/20/reviews --paginate + gh api repos/alsj213/team-board/issues/20/comments --paginate | rc=0 | head 5690e76ad629 上 defect + regression 各 >=1 条（扫描正文 2 条、带标记 2 条）
rebase | PASS | git -C <repo> merge-base --is-ancestor refs/merge-guard/20/base refs/merge-guard/20/head | rc=0 | ff57582 是 head 5690e76 的祖先（已 rebase 到 main）
合并 | PASS | gh pr merge 20 --merge --delete-branch --match-head-commit 5690e76ad629876e1beed7f32e321efc26b89db6 --subject Merge PR #20（t_deadbeef） --body-file <body> | rc=0 | fake gh: merged #20
分支删除 | PASS | git -C <repo> ls-remote --exit-code --refs origin refs/heads/wt/t_deadbeef | rc=2 | 分支已不在远端
结论：合并完成，源分支 wt/t_deadbeef 已从 origin 删除
```

双问绑定闸门取数前还要核对**仓库**：`reviewhead` 的 `gh api` 路径前缀写死
`REPO = alsj213/team-board`、不看 `--repo`，所以闸门先读
`git config --get remote.<name>.url`（配置里的原始 URL，不是被 `insteadOf` 改写后的
`git remote get-url` 结果），解析出 `owner/name` 与 `REPO` 比对；不一致 / 读不到 /
不是 GitHub 形态一律 rc2 拒绝 —— 跨仓时不许拿别的仓库同号 PR 的评审当证据。


### 3. 为什么不放在 branch-audit（`branches.py` 规则 C-2）

现有 `branch-audit` 的 C-2 判的是「**fork 点**落在 main（或 origin/main）上」，不判「head 是否
包含 **main 尖端**」—— 分支从旧 main 分叉出来照样过 C-2。所以复用不了，必须在合并入口新加
一道。另外 `branches.py` 是后续卡（独立 worktree 卡、证据分支卡）的战场，同文件互踩；本卡把
改动收在 `merge_guard.py` 一个文件里。

## 本卡实跑（2026-10-29，本机）

环境：临时 `git init --bare origin.git` + clone，`main` 两个提交，`wt/t_deadbeef` 从 `main~1`
拉出再补一个提交（于是落后 main）；`gh` 是假脚本（只回答 `pr view` / `pr merge`，`pr merge`
时顺手删远端分支模拟 `--delete-branch`），调用记录写 `$FAKE_LOG`。脚本：
`rebase_gate_e2e.sh`（`t_c836202a` 附件）。

> 下面两段原文来自**返工卡 `t_0a16dc4d`** 用同一脚本的重跑（拒绝文案改了两轮，`t_c836202a` 那轮
> 的输出已过时）：`bash rebase_gate_e2e.sh` → rc=0，临时目录 `/tmp/rg-e2e-cOr0Dy`，
> 完整日志见该卡附件 `t_0a16dc4d-rebase-e2e-retry.log`。

### 红样：分支落后 main

```
rc=2
stdout 字节数=0
stderr 行数=1
stderr 原文: 错误: 分支 wt/t_deadbeef 落后 main（main 不是 head 的祖先，git merge-base --is-ancestor rc=1）：合并前必须先 rebase 到 main（必须在作者的 bot 身份 worktree 里做：bash scripts/setup-bot-identity.sh <profile> <worktree>，否则 committer≠author 会被 commit-identity 判红；卡分支上不许出现没有认账字段的同步合并提交，否则 merge-accountability 必红），拒绝合并
gh pr merge 次数=0
gh 调用记录:
gh pr view 9 --json number,state,headRefName,baseRefName,headRefOid
私有 ref（fetch 抓下来的）:
  refs/merge-guard/9/base
  refs/merge-guard/9/head
```

### 祖先判定（判据 ③ 本身，两段都要贴）

```
$ git -C <clone> merge-base --is-ancestor origin/main refs/merge-guard/9/head
rebase 之前：rc=1
rebase 之后：rc=0
```

两个 rc 都真跑过 —— 证明这条判据既不是恒真也不是恒假。

### 绿样：`git rebase origin/main` 之后同一条命令

```
rc=0
stderr 字节数=0
分支存在 | PASS | git -C /tmp/rg-e2e-cOr0Dy/clone ls-remote --exit-code --refs origin refs/heads/wt/t_deadbeef | rc=0 | f483f2d8c4ae812086c19c7003406eb270774c56	refs/heads/wt/t_deadbeef
预检 | PASS | gh pr view 9 --json number,state,headRefName,baseRefName,headRefOid | rc=0 | state=OPEN, headRefName=wt/t_deadbeef, baseRefName=main
head sha | PASS | f483f2d8c4ae812086c19c7003406eb270774c56
rebase | PASS | git -C /tmp/rg-e2e-cOr0Dy/clone merge-base --is-ancestor refs/merge-guard/9/base refs/merge-guard/9/head | rc=0 | f7501e4 是 head f483f2d 的祖先（已 rebase 到 main）
合并 | PASS | gh pr merge 9 --merge --delete-branch --match-head-commit f483f2d8c4ae812086c19c7003406eb270774c56 --subject Merge PR #9（t_deadbeef） --body-file /tmp/rg-e2e-cOr0Dy/body.txt | rc=0 | (无输出)
分支删除 | PASS | git -C /tmp/rg-e2e-cOr0Dy/clone ls-remote --exit-code --refs origin refs/heads/wt/t_deadbeef | rc=2 | 分支已不在远端
结论：合并完成，源分支 wt/t_deadbeef 已从 origin 删除
```

### 私有 ref 不外泄 / 源分支真删了

```
远端 ref 里含 merge-guard 的条数=0
本地私有 ref 条数=2
git -C <clone> ls-remote --exit-code --refs origin refs/heads/wt/t_deadbeef → rc=2（已删）
```

## 残余风险（本卡不解决，明写）

1. **有谁能绕过 `merge_guard` 直接 `gh pr merge`，就仍能把落后分支合进去。** 本仓是私有仓库，
   分支保护 / rulesets API 返回 **HTTP 403**（R13 ⑦ 同一结论），无法用 required check 把
   「必须走 `merge_guard`」变成平台强制。本闸门只对「走 `merge_guard` 的合并」生效。
2. 闸门判的是 **祖先关系**，不是「PR 合进去之后 main 一定绿」：它不取代 R6 的「合并前 CI 全绿」，
   也不判代码内容。落后分支 rebase 之后仍可能让 main 的测试变红，那一半仍归 CI。
3. 私有 ref 不清理由第 1 节；长时间运行会 pin 住每张 PR 的两个提交对象（`git gc` 不回收），
   代价可忽略，但**不是**零。
4. 判定依赖 `git fetch` 成功。网络断 / remote 名写错 → 走「取远端提交失败」rc2（拒绝合并），
   不会误放行。

## 与 R6 / R12 / R13 / R15 的关系

- **R6**（合并前 CI 全绿、head 分支 `wt/<卡号>`、Review 记录）：**不取代**，只是再加一道。
  本闸门不管 CI 状态，也不管分支名。
- **R12 / R13**（合并唯一入口 = `merge_guard`，`--delete-branch`，`--method merge`）：**不改**
  它们的任何判据。合并 argv 一个字节都没动（`--merge --delete-branch --match-head-commit
  --subject --body-file` 原序保留），本闸门只是在 `lines = [...]` 之前多算一步。
- **R15-A**（提交身份）：无关。
- 没有新增跳过开关：`--allow-stale` / `--skip-rebase-check` / `--force` / `--no-verify` 一律
  不存在；也没有按环境变量关闭的口子。
