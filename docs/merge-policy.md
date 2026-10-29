# 合并方式统一：只允许 merge commit（R11）

> 定于 2026-10-29，卡片 `t_24e43d1a`（老板指令：「都给我添加强制约束，不能靠自觉」）。
> 本文件是 R11 的仓库内落点；规则原文同时在 pm 章程 `规则区 · R11`。

## 一、条文（唯一合法方式）

① **唯一合法合并方式 = create a merge commit**：

```
gh pr merge <n> -R alsj213/team-board --merge
```

② 禁止 `--squash`、禁止 `--rebase`、禁止 `--admin`，禁止直推 `main`。

③ **强制点在平台配置，不在自觉**：本仓 merge 设置固定为

```
allow_merge_commit = true
allow_squash_merge = false
allow_rebase_merge = false
```

因此用另一种方式合并时，GitHub API 层直接拒绝（`rc=1`），与操作者是谁无关。

④ **合并后必须做一次机器判定**（判断「这次用的是哪一种」，不留猜测）：

```
git log -1 --format='%P' <merge commit> | wc -w     # 2 = merge commit（合规）；1 = squash/rebase（违规）
```

⑤ **每次合并前先验配置没被改回去**（合并卡第一步）：

```
gh api repos/alsj213/team-board --jq '{ms:.allow_merge_commit,s:.allow_squash_merge,r:.allow_rebase_merge}'
# 必须是 {"ms":true,"s":false,"r":false}，否则停手报 ops，不许先合再补
```

## 二、为什么定 merge commit（依据，均已本机实跑）

- **历史本来就是这一种**：main 上全部 9 个 PR 合并提交 parent 数都是 2
  （`f2ee6648` / `abb57ae5` / `80fbd999` / `de2a7d41` / `75f7230b` / `4a91d5e4` / `94e94a97` / `3eadfb83` / `1212183c`）。
  本条是把既有事实**写死成规则**，不是改方向。
- **R6 ② 证据链指向具体提交**：卡上贴的 R3 证据（命令 + 退出码）对应 engineer 的逐条提交；
  squash 会把这些提交压成一个，`git log` 与卡面记录的 head SHA 对不上，追溯只能猜 —— 正是本条要治的病。
- **R6 ③ 用 head 判据**：`gh pr view <n> --json headRefName` 核对 `wt/<卡号>`；rebase-merge 会改写 SHA，
  main 上再也找不到卡面记的那个 head，判定失效。
- **回滚**：merge commit 可 `git revert -m 1 <merge>` 单 PR 回滚；squash 后的 revert 在同一文件多 PR 叠加时要手工解冲突。

## 三、合并卡模板（收口卡照抄这一段）

```
## 合并前置（R6 三条 + R11 两条，逐条贴命令原文 / rc / 输出）

配置未被动过（R11 ⑤）：
$ gh api repos/alsj213/team-board --jq '{ms:.allow_merge_commit,s:.allow_squash_merge,r:.allow_rebase_merge}'
{"ms":true,"s":false,"r":false}   # 不匹配 → 停手报 ops

① CI 全绿（R6①）：
$ gh pr checks <n> -R alsj213/team-board; echo rc=$?
② R3 证据（R6②）：见 <卡号> 的验收段，每条 PASS 一条本机命令 + 真实退出码
③ head 分支（R6③）：
$ gh pr view <n> -R alsj213/team-board --json headRefName
④ 双问结论已贴 PR（缺陷视角 / 回归视角两条 review 记录）
⑤ 合并（R11①）：
$ gh pr merge <n> -R alsj213/team-board --merge; echo rc=$?
⑥ 合并用的是哪一种（R11④，机器判定）：
$ git fetch origin main && git log -1 --format='%H %P' origin/main
```

**只允许**第 ⑤ 步这一条命令；出现 `--squash` / `--rebase` / `--admin` 一律视为违规合并，按 R1 同轮返工。
