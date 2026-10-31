# 合并方式统一：只允许 merge commit（R13）

> 定于 2026-10-29，卡片 `t_24e43d1a`（老板指令：「都给我添加强制约束，不能靠自觉」· 并行组第 3 条）。
> **编号依据**（本机实跑，不靠记）：`grep -n '^### R1[0-9]' ~/.hermes/profiles/{pm,engineer}/SOUL.md ~/.hermes/profiles/ops/workspace/playbook.md`
> → R11 =「证据分支独立命名空间 `evidence/*`」已落盘（t_55009247）；R12 =「合并后必删源分支」在途（t_2df659ad / t_e4656674，规则区尚无 `### R12`）→ **本条取 R13**，不占用、不顺延别人的号。
>
> **本文件是 R13 的唯一源**（在仓库里，可 review、可 diff）；三份章程只放条文原文指针，不复制命令，避免两份条文分叉。
> 以下命令在 WSL / bash 下执行（`grep -c` / `git` 语义与 PowerShell 不同）；仓库路径**不写死**，用 `git -C <你的 checkout>`。

## 一、条文（方式只此一种）

① **合并方式 = create a merge commit**。squash、rebase 一律不许。

② **命令层只有一个入口，且不是本条新建的**：合并前必跑
`src/team_board/merge_guard.py`（并行组第 ⑤ 条 `t_40fa5375` 的实现文件），本条只钉它 `--method` 这一维：

```
python -m team_board.merge_guard --pr <n> --branch wt/<卡号> --method merge --delete-branch --card <卡号> --by pm/run<NNN> --subject "Merge PR #<n>（<卡号>）" --body-file <body-file>
```

四种认账参数（`--card` / `--by` / `--subject` / `--body-file`）是**合并模式必填**：`gh pr merge`
不带 `--subject` / `--body-file` 时用默认文案生成合并提交 message，**没有任何 trailer**，
经 `merge_guard` 合出来的提交必然过不了 `merge-audit --require`（缺 `merged-by` / `merge-card` /
`merge-pr`）。body 文件用 `team-board merge-record --body-out` 生成；`--verify-only` 与这四个
参数互斥。`--body-file` 必须是**绝对路径**（相对路径会在进程 cwd 与 `--repo` 两处解析成
不同文件，gh 可能读不到、甚至静默合进另一份 body —— rc2 拦掉）。

`--method` 只允许取 `merge`；`squash` / `rebase` 一律拒绝（该 CLI 落地时按本条写死默认值与合法值域）。
**`merge_guard` 已强制**（合并入口只有 ② 与模板 ⑦ 那同一条命令，脚本是 `src/team_board/merge_guard.py`，PR #20 已落地）：
下面的平台命令原文只作历史对照，**不许再用它合并**：

```
gh pr merge <n> -R alsj213/team-board --merge --delete-branch; echo rc=$?
```

`--delete-branch` 不是本条加的：它是 R12（合并后必删源分支）的要求，本条只在命令里保持**同一条命令**，不许为了「只测方式」把它去掉 —— 去掉就变成两条规则各说一条命令。

③ **平台层强制（已生效，不靠自觉）**：本仓 merge 设置固定为
`allow_merge_commit=true` / `allow_squash_merge=false` / `allow_rebase_merge=false`，
另一种方式在 GitHub API 层直接被拒（`rc=1`），与操作者是谁无关：

```
GraphQL: Squash merges are not allowed on this repository. (mergePullRequest)      # rc=1
GraphQL: Rebase merges are not allowed on this repository. (mergePullRequest)      # rc=1
```

为什么这条是「配置」而不是「第二个闸门」：它是平台开关，不是本仓新增的可执行文件；`merge_guard` 落地后，`--method merge` 与平台开关是同一维的两层（CLI 拦参数、平台拦 API），不构成并行两条规则。

④ **合并前先验平台开关没被改回去**（合并卡第一步；**逐键断言，不要比对整串字面量** —— `gh --jq` 按键名字典序重排输出，实测是 `{"ms":true,"r":false,"s":false}`）：

```
out=$(gh api repos/alsj213/team-board --jq '{ms:.allow_merge_commit,s:.allow_squash_merge,r:.allow_rebase_merge}'); rc=$?
echo "$out"; echo "rc=$rc"          # rc 必须 0；三键为 null = 没读到/无权限，不是「配置被改」
gh api repos/alsj213/team-board --jq '.allow_merge_commit==true and .allow_squash_merge==false and .allow_rebase_merge==false'; echo rc=$?
# 输出 true 才继续；false 或 rc≠0 → 停手报 ops，不许先合再补
```

⑤ **合并后判定「这次用的是哪一种」**（只判**本 PR 自己的合并提交**；不判 `origin/main` 尖端 —— 尖端会混进别人刚合的 PR，且只要 squash/rebase 被关掉，尖端恒为 2 父，属恒真检查）：

```
sha=$(gh pr view <n> -R alsj213/team-board --json state,mergeCommit --jq 'select(.state=="MERGED")|.mergeCommit.oid')
echo "sha=$sha"        # 空 = 没合成功 / 还没合，禁止继续判定
git -C <你的 checkout> fetch origin main -q || { echo "fetch 失败，按 R10 ③ 处置"; exit 1; }
git -C <你的 checkout> cat-file -p "$sha" | grep -c '^parent '
```

取值表（**「用的是哪一种」这一问的唯一判据**；不问完成，完成判据见 R12）：

| 输出 | 判定 |
|---|---|
| `2` | merge commit = 合规 |
| `≥3` | octopus 合并，仍属 merge commit = 合规（卡上记一笔） |
| `1` | squash / rebase = **违规**，按 R1 同轮返工 |
| `0` / 空 / `rc≠0` | **取值失败，不是结论**；先修命令（fetch 失败、SHA 为空），禁止据此判合规或违规 |

⑥ **本条不管「合并是否完成」**：完成判据归 R12（合并 rc=0 + 源分支已删 + 三条输出贴卡）。本节只回答「用的是哪一种」，不与之争「唯一判据」。（R12 若最终换了编号，本节 4 处交叉引用要同步核一次。）

## 二、为什么定 merge commit（依据，均已本机实跑）

- **历史本来就是这一种**：`git log --merges --format='%h' origin/main` 给出 **11 个 merge commit**
  （`75f7230` `94e94a9` `1212183` `3eadfb8` `4a91d5e` `de2a7d4` `80fbd99` `abb57ae` `0b50ab6` `f2ee664` `69f8d9e`，
  含最早两张 PR #2/#3 的 `69f8d9e`/`0b50ab6`），逐条 `git cat-file -p <sha> | grep -c '^parent '` → **11 条全是 2**。
  本条把既有事实**写死成规则**，不改方向。
- **R6 ② 证据链指向具体提交**：卡上的 R3 证据（命令 + 退出码）对应 engineer 的逐条提交；squash 把它们压成一个，`git log` 与卡面 head 对不上，追溯只能猜 —— 正是本条要治的病。
- **R6 ③ 用 head 判据**：`gh pr view <n> --json headRefName` 核对 `wt/<卡号>`；rebase-merge 改写 SHA 后 main 上找不到卡面记的 head，判定失效。
- **回滚**：merge commit 可 `git revert -m 1 <merge>` 单 PR 回滚；squash 后在同文件多 PR 叠加时要手工解冲突。

## 三、合并卡模板（收口卡照抄这一段）

```
## 合并前置（R6 三条 + R7 判档 + R9 夜间五条 + R12 + R13，逐条贴命令原文 / rc / 输出）

① 平台开关未被改回去（R13 ④）：
$ gh api repos/alsj213/team-board --jq '.allow_merge_commit==true and .allow_squash_merge==false and .allow_rebase_merge==false'; echo rc=$?
true / rc=0                 # false 或 rc≠0 → 停手报 ops

② CI 全绿（R6 ①）：
$ gh pr checks <n> -R alsj213/team-board; echo rc=$?
③ R3 证据（R6 ②）：见 <卡号> 验收段，每条 PASS 一条本机命令 + 真实退出码
④ head 分支（R6 ③）：
$ gh pr view <n> -R alsj213/team-board --json headRefName
⑤ 双问结论已贴 PR（缺陷视角 / 回归视角两条 review 记录，写明评论 id 或时间）；标记两行写法与判据见 docs/review-patch.md
⑥ R7 判档：A / B / C 三档选一名 + 四条素材（CI rc、本地全量测试 rc、`--json files` 路径清单与 nfiles、是否碰 .github/依赖/迁移/凭据）
   B 档另附 R9 夜间五条：CI rc=0 且非 no checks reported / 双问无 P1P2 / nfiles≤10 / 不碰凭据迁移删库且删除≤100 行 / R3 证据齐；夜间自合标 B·night
   C 档（或夜间撞「绝不做」四条）：不合并，标 [night-queue]，写「第 N 条不满足 + 实测输出」
⑦ 合并（R13 ②，唯一命令）：
   merge_guard 已强制（见 PR #20）：python -m team_board.merge_guard --pr <n> --branch wt/<卡号> --method merge --delete-branch --card <卡号> --by pm/run<NNN> --subject "Merge PR #<n>（<卡号>）" --body-file <body-file>; echo rc=$?
   历史原文（已完成，勿用）：gh pr merge <n> -R alsj213/team-board --merge --delete-branch; echo rc=$?
⑧ 用的是哪一种（R13 ⑤，判本 PR 的合并提交）：
$ sha=$(gh pr view <n> -R alsj213/team-board --json state,mergeCommit --jq 'select(.state=="MERGED")|.mergeCommit.oid'); echo "sha=$sha"
$ git -C <checkout> cat-file -e "$sha" 2>/dev/null || git -C <checkout> fetch origin -q   # 本地没这个对象才 fetch；fetch 失败按 R10 ③
$ git -C <checkout> cat-file -p "$sha" | grep -c '^parent '
2 = merge commit（合规）/ 1 = squash 或 rebase（违规，返工）/ 0 或 rc≠0 = 取值失败，不是结论
⑨ 完成判据（R12）：源分支已删 `git ls-remote --exit-code --refs origin refs/heads/wt/<卡号>` → rc=2
```

**只允许**第 ⑦ 步这一条合并命令；出现 `--squash` / `--rebase` / `--admin` 一律视为违规合并，按 R1 同轮返工。

## 四、本条做不成机器校验的部分（明写，不许含糊）

- **「禁止直推 `main`」没有平台强制点**：本仓私有，GitHub 免费版不开分支保护/规则集 ——
  `gh api repos/alsj213/team-board/branches/main/protection` → HTTP 403 `Upgrade to GitHub Pro or make this repository public to enable this feature.`（rulesets 同样 403）。
  所以这一条**只能事后稽核，无法在推送时拦住**：

```
git -C <checkout> fetch origin main -q && git -C <checkout> log --first-parent --format='%h %s' origin/main -20
# 视检：main 上除 "Merge pull request #N …" 之外不应出现直推提交
```

- **合并方式本身可以做机器校验**（R13 ③④⑤ 三层），所以本条的强制不依赖自觉；
  但「直推 main」这半条做不到，按老板要求在此明写原因，不写成「规则已定，执行靠自觉」。
