# 合并方式统一：只允许 merge commit（R11）

> 定于 2026-10-29，卡片 `t_24e43d1a`（老板指令：「都给我添加强制约束，不能靠自觉」）。
> **本文件是 R11 的唯一源**（在仓库里，可 review、可 diff）；pm 章程 `规则区 · R11` 只放条文摘要 + 指向本文件的指针，不复制命令，避免两份条文分叉。
> 以下命令均在 WSL / bash 下执行（`grep -c` / `git` 语义与 PowerShell 不同）。

## 一、条文（唯一合法方式）

① **唯一合法合并方式 = create a merge commit**：

```
gh pr merge <n> -R alsj213/team-board --merge
```

② **禁止** `--squash`、`--rebase`、`--admin`；**禁止直推 `main`**。
　「禁止直推 main」**没有平台强制点**（本仓私有、GitHub 免费版不支持分支保护/规则集：`gh api repos/alsj213/team-board/branches/main/protection` → HTTP 403 `Upgrade to GitHub Pro or make this repository public to enable this feature.`）。
　这一条只能事后稽核，命令：

```
git -C /home/liu/project/team-board fetch origin main -q && git log --first-parent --format='%h %s' origin/main -20
# 视检：main 上除 merge commit（"Merge pull request #N …"）外不应出现直推提交
```

③ **强制点在平台配置，不在自觉**：本仓 merge 设置固定为
`allow_merge_commit=true` / `allow_squash_merge=false` / `allow_rebase_merge=false`，
因此用另一种方式合并时 GitHub API 层直接拒绝（`rc=1`），与操作者是谁无关。

每次合并前先验配置（**逐键断言，不要比对整串字面量**：`gh --jq` 会按键名字典序重排输出，实测输出是 `{"ms":true,"r":false,"s":false}`）：

```
out=$(gh api repos/alsj213/team-board --jq '{ms:.allow_merge_commit,s:.allow_squash_merge,r:.allow_rebase_merge}'); rc=$?
echo "$out"; echo "rc=$rc"
# rc 必须 0；三键必须非 null（null = 根本没读到/无权限，不是「配置被改」）
# 布尔断言，输出 true 才算配置正确：
gh api repos/alsj213/team-board --jq '.allow_merge_commit==true and .allow_squash_merge==false and .allow_rebase_merge==false'; echo rc=$?
# 输出 false → 配置被改回去了：停手，报 ops，**不许先合再补**
```

④ **合并后判定「这次用的是哪一种」**（判**本 PR 自己的合并提交**，不判 `origin/main` 尖端 —— 尖端会混入别人刚合的 PR，且只要 squash/rebase 被关掉，尖端恒为 2 父，属于恒真检查）：

```
sha=$(gh pr view <n> -R alsj213/team-board --json state,mergeCommit --jq 'select(.state=="MERGED")|.mergeCommit.oid')
echo "sha=$sha"        # 空 = 没合成功或还没合，禁止继续判定
git fetch origin main -q
git cat-file -p "$sha" | grep -c '^parent '
```

取值表（**唯一判据**）：

| 输出 | 判定 |
|---|---|
| `2` | merge commit = 合规 |
| `≥3` | octopus 合并，仍属 merge commit = 合规（记录在卡上） |
| `1` | squash / rebase = **违规**，按 R1 同轮返工 |
| `0` / 无输出 / `rc≠0` | **取值失败**，不是结论；先修命令（多为 fetch 失败或 SHA 为空），禁止据此判合规或违规 |

⑤ **fetch 失败按 R10 ③ 处置**：`git fetch` 失败（rc≠0）时不得用上一轮 stale 的 `origin/main` 判 SHA。

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

① 配置未被改回去（R11 ③）：
$ gh api repos/alsj213/team-board --jq '.allow_merge_commit==true and .allow_squash_merge==false and .allow_rebase_merge==false'; echo rc=$?
true / rc=0        # false 或 rc≠0 → 停手报 ops

② CI 全绿（R6①）：
$ gh pr checks <n> -R alsj213/team-board; echo rc=$?
③ R3 证据（R6②）：见 <卡号> 验收段，每条 PASS 一条本机命令 + 真实退出码
④ head 分支（R6③）：
$ gh pr view <n> -R alsj213/team-board --json headRefName
⑤ 双问结论已贴 PR（缺陷视角 / 回归视角两条 review 记录，写明评论 id 或时间）
⑥ 合并（R11①，唯一允许的命令）：
$ gh pr merge <n> -R alsj213/team-board --merge; echo rc=$?
⑦ 用的是哪一种（R11④，判本 PR 的合并提交）：
$ sha=$(gh pr view <n> -R alsj213/team-board --json state,mergeCommit --jq 'select(.state=="MERGED")|.mergeCommit.oid'); echo "sha=$sha"
$ git fetch origin main -q; git cat-file -p "$sha" | grep -c '^parent '
2 = merge commit（合规）/ 1 = squash 或 rebase（违规，返工）
```

**只允许**第 ⑥ 步这一条合并命令；出现 `--squash` / `--rebase` / `--admin` 一律视为违规合并，按 R1 同轮返工。
