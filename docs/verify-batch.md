# verify-batch：一次跑一组同内容的验证（压缩率、单失败、退化、审计）

`verify-batch` 把「N 张卡各自跑一遍全套检查」压成「M 组各跑一遍」，但**判定仍逐 unit 出**：
组内一个 unit 的失败不牵连别人，组级检查失败才让整组 FAIL。本文的命令全部在
`/home/liu/project/tb-wt/t_a19fe62c`（分支 `wt/t_a19fe62c`）实跑过，每条带真实退出码。

契约唯一来源：`/home/liu/project/tb-contracts/t_f4e71bd3.md`（§4 执行、§6 审计、§7 回放配方）。
实现：`src/team_board/verifybatch.py`；CLI 接线：`src/team_board/cli.py`
（`verify-batch` / `verify-batch-audit`）。

## 0. 前置：用哪个 python、怎么让它找到 src

本仓是 `src` 布局、没有 pytest 的 `pythonpath` 配置，所以**必须**给 `PYTHONPATH=src`；
`python3` 又没装依赖，所以要用仓库 venv 的解释器：

```
V=PYTHONPATH=src /home/liu/project/team-board/.venv/bin/python
```

下文命令都省略这段前缀，写成 `$V -m team_board.cli …`。**`PYTHONPATH=src` 是相对路径**，
`verify-batch` 在每组自己的临时 worktree 里跑检查时会把它一起继承下去，于是每条检查用的都是
**那个 worktree 的** `src/`（不是主 checkout 的）—— 这是「组级检查跑在组首 unit 的 head 上」能成立的前提。

## 1. plan 文件：唯一的入口

```json
{
  "base": "b870c6f",
  "group_checks": [
    ["/home/liu/project/team-board/.venv/bin/python", "-m", "pytest", "-q", "tests/unit/test_gatelog.py"]
  ],
  "units": [
    {"card": "t_b1a3b00f", "pr": 53, "head": "af45471",
     "checks": [["/home/liu/project/team-board/.venv/bin/python", "-m", "pytest", "-q", "tests/unit/test_gatelog.py"]]},
    {"card": "t_b1a3b00f", "pr": 53, "head": "034ae4c",
     "checks": [["/home/liu/project/team-board/.venv/bin/python", "-m", "pytest", "-q", "tests/unit/test_gatelog.py"]]},
    {"card": "t_97b0ff97", "pr": 50, "head": "a977733",
     "checks": [["/home/liu/project/team-board/.venv/bin/python", "-m", "pytest", "-q", "tests/unit/test_gatelog.py"]]},
    {"card": "t_97b0ff97", "pr": 50, "head": "684fe9a",
     "checks": [["/home/liu/project/team-board/.venv/bin/python", "-m", "pytest", "-q", "tests/unit/test_gatelog.py"]]}
  ]
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `base` | 否 | 缺省 `origin/main`；只用于算 `patch-id`（展示用）与校验可解析。**分支已合进 main 时不能写 `origin/main`**，否则 `merge-base` 退化成 head 自己、diff 为空，见 §2 |
| `group_checks` | 否 | 组级检查，argv 数组的数组；一条 rc≠0 → 该组每个 unit 都 FAIL |
| `units[].card` | 是 | `t_` + 8 位小写十六进制 |
| `units[].pr` | 是 | 1..999999999 的**整数**；字符串 `"53"` 是非法（rc2），写错形态要跑一次越界用例验证 |
| `units[].head` | 是 | 任意能 `git rev-parse <rev>^{commit}` 的 rev（分支名或 sha） |
| `units[].checks` | 否 | 该 unit 的自检，可缺省 = 空；argv 列表、**不经 shell** |

检查命令里写**绝对路径的解释器**（上面的 venv python）：`run_groups` 在别的 worktree 里用
`cwd=<worktree>` 起子进程，写 `python` 只能碰 PATH，写 `sys.executable` 则需要你恰好从 venv 启动。

## 2. 回放要造「同内容、不同提交」的双头

分组键是 **`<head>^{tree}`**（组键 `tree:<tree sha>`），不是 `patch-id` —— `patch-id` 会剥空白，
`X = 2` 与 ` X = 2` 会算出同一个值，那两个 head 就不能算「同内容」。同 tree ⇔ 改动逐字相同。

实测（`$ git rev-parse <head>^{tree}`，rc0）：

```
af45471 tree=c399f28a277c056242ac60350d420c67a67a75ae parent=3edcc87
034ae4c tree=c399f28a277c056242ac60350d420c67a67a75ae parent=effc60e
a977733 tree=48d1157edffdefd295e314b4a8ffd0ed00bb5d9d parent=a063f1d
684fe9a tree=48d1157edffdefd295e314b4a8ffd0ed00bb5d9d parent=797596e
```

今天 5 张真 PR 的 patch-id 两两不同（契约 §7 实测），所以**拿真 PR 直接回放压缩率恒为 0**；
压缩率必须在含同 tree 组的回放上算。造双胞胎的办法就是「原改动 cherry-pick 到合并提交的第一父」：

```
$ git worktree add --detach <tmp>/replay53 b870c6f          # PR #53 的合并第一父
$ cd <tmp>/replay53 && git cherry-pick 3edcc87 af45471      # 同改动、新 sha
$ git rev-parse HEAD && git rev-parse HEAD^{tree}
c9ab126fd2e53668b8e39c9338e2e47bff513593
c399f28a277c056242ac60350d420c67a67a75ae                    # = af45471 的 tree，双胞胎成立
exit 0

$ git worktree add --detach <tmp>/replay50 01f6312          # PR #50 的合并第一父
$ cd <tmp>/replay50 && git cherry-pick ff1546e a063f1d a977733
$ git rev-parse HEAD && git rev-parse HEAD^{tree}
684fe9a950a21b61988fef4cdd683887b7fc6bb6
48d1157edffdefd295e314b4a8ffd0ed00bb5d9d                    # = a977733 的 tree
exit 0
```

注意重造出来的 sha 与表里的双胞胎 **可能不同**（cherry-pick 的 committer 时间不同 → 新 sha），
**判据只认 tree**：tree 相同就是「同内容、不同提交」，sha 相同与否无关紧要。本文用的四个 head 是
`af45471/034ae4c`（PR #53，`034ae4c` 由契约 §7 的同一配方产出）与 `a977733/684fe9a`（PR #50，本卡实跑产出）。
两个双胞胎都是**本地对象**（没有推送、也不是任何分支），换一台机器要按上面四行重造。

回放 plan 的 `base` 写 PR #53 的合并第一父 `b870c6f`。**`base` 不参与分组**（判据是 tree），它只喂
`patch_id`（展示/日志用）。四种 head 的实测（`bash evidence/patch-ids.sh`，rc0）：

```
af45471 merge-base=b870c6f patch-id=224c0ef4b97d1e8390eeeb0eeac5a2266e063be6
034ae4c merge-base=b870c6f patch-id=224c0ef4b97d1e8390eeeb0eeac5a2266e063be6
a977733 merge-base=a977733 patch-id=<empty: diff 为空>
684fe9a merge-base=01f6312 patch-id=8b92d0739899ce3077f2b4a0da081574616b26a7
```

两点必须知道：

- **已合进 main 的 head 算不出 patch-id**：`a977733` 是 `b870c6f` 的祖先，`merge-base(base, head)` 退化成 head
  自己 → diff 为空 → `patch_id=None`。这正是契约 §7 让人把 `base` 写成「该 PR 合并提交的第一父」的原因。
- **同组两个 unit 的 patch_id 可以一个 None 一个有值**（`a977733` vs `684fe9a`），照样并组成功 —— 判据是 tree，
  不是 patch-id；patch-id 只作展示，且会剥空白（`X = 2` 与 ` X = 2` 同值），拿它判「同内容」会让语法坏的那个白拿 PASS。

## 3. `--dry-run`：只算分组，不建 worktree、不跑检查、不写账本

```
$ $V -m team_board.cli verify-batch --plan evidence/replay-plan-4u2g.json --repo . --dry-run
若批量，今天 4 次验证可压成 2 次（下降 50.0%）
plan: b-36928a91 kind=patch units=2 heads=a977733,684fe9a
plan: b-7fc830f3 kind=patch units=2 heads=af45471,034ae4c
exit 0
```

压缩率 = 1 − 组数/unit 数，这里 1 − 2/4 = 50.0%。`--dry-run` 也会 `resolve_head`（`plan:` 行要打 short sha），
所以 head 写错在 dry-run 阶段就 rc2，不会等到跑完才知道。

## 4. 真跑：同一条 plan，4 条 unit 各自 verdict

```
$ $V -m team_board.cli verify-batch --plan evidence/replay-plan-4u2g.json --repo .
batch: b-36928a91  kind=patch  head=a977733  units=2
  group: exit:0  失败行（0/0，≤20）:
  unit: t_97b0ff97 pr=50 verdict=PASS  failed_check=-
  unit: t_97b0ff97 pr=50 verdict=PASS  failed_check=-
batch: b-7fc830f3  kind=patch  head=af45471  units=2
  group: exit:0  失败行（0/0，≤20）:
  unit: t_b1a3b00f pr=53 verdict=PASS  failed_check=-
  unit: t_b1a3b00f pr=53 verdict=PASS  failed_check=-
counts: groups=2 units=4 pass=4 fail=0
log: /home/liu/project/tb-wt/t_a19fe62c/evidence/batch-b-36928a91.log
batch-json: /home/liu/project/tb-wt/t_a19fe62c/evidence/batch-b-36928a91.json
log: /home/liu/project/tb-wt/t_a19fe62c/evidence/batch-b-7fc830f3.log
batch-json: /home/liu/project/tb-wt/t_a19fe62c/evidence/batch-b-7fc830f3.json
emit: PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass --payload '{"head_sha":"a977733bf08e88d20b0c521421f5b981085fa575","run_id":<你的 run_id>}' --card t_97b0ff97
emit: …（共 4 条，逐 unit 一条，见下节）
exit 0
```

- 4 次验证 → 2 次「全套」：`counts:` 的 `groups=2` 是代价，`units=4` 里的每条仍有**自己的** verdict。
- 全量输出不落 stdout：每组一份 `evidence/batch-<batch_id>.log`（含组级与每条 unit 检查的原文），
  每份账本 `evidence/batch-<batch_id>.json` 记 `kind` / `key` / `base` / `head_sha` / `created_at` / `group_rc` + 逐 unit verdict。
- 临时 worktree 在 `finally` 里 `git worktree remove --force` 掉；`--keep-worktrees` 才保留并把路径打进摘要。

账本样例（`evidence/batch-b-7fc830f3.json`，`created_at` 是唯一每次不同的字段 —— §0 D6 的确定性靠这条）：

```json
{
  "base": "b870c6f",
  "batch_id": "b-7fc830f3",
  "created_at": 1793528244,
  "group_rc": 0,
  "head_sha": "af45471ebfa4ef2e7e4a81928be263941dd944fe",
  "key": "tree:c399f28a277c056242ac60350d420c67a67a75ae",
  "kind": "patch",
  "units": [
    {"card": "t_b1a3b00f", "failed_check": null, "head_sha": "af45471ebfa4ef2e7e4a81928be263941dd944fe",
     "log": "…/evidence/batch-b-7fc830f3.log", "pr": 53, "rc": 0, "verdict": "PASS"},
    {"card": "t_b1a3b00f", "failed_check": null, "head_sha": "034ae4cf6608e12d481919ee5f65b9a605e366c4",
     "log": "…/evidence/batch-b-7fc830f3.log", "pr": 53, "rc": 0, "verdict": "PASS"}
  ]
}
```

## 5. 组内一个失败：只该 unit FAIL，其余照旧 PASS

把 `034ae4c` 那个 unit 的 `checks` 换成必失败命令 `["/bin/false"]`（plan：`evidence/replay-plan-4u2g-onefail.json`）：

```
$ $V -m team_board.cli verify-batch --plan evidence/replay-plan-4u2g-onefail.json --repo . --log-dir evidence/fail-scenario
batch: b-36928a91  kind=patch  head=a977733  units=2
  group: exit:0  失败行（0/0，≤20）:
  unit: t_97b0ff97 pr=50 verdict=PASS  failed_check=-
  unit: t_97b0ff97 pr=50 verdict=PASS  failed_check=-
batch: b-7fc830f3  kind=patch  head=af45471  units=2
  group: exit:0  失败行（0/0，≤20）:
  unit: t_b1a3b00f pr=53 verdict=PASS  failed_check=-
  unit: t_b1a3b00f pr=53 verdict=FAIL  failed_check=unit:/bin/false
counts: groups=2 units=4 pass=3 fail=1
…
exit 1
```

三点要点：

1. **只有那一个 unit 红**：同组另一个 unit（同 tree、同 head 家族）仍是 PASS —— 判定隔离用的是
   `failed_check=unit:<argv>`，不是「整批绿」。
2. 有 FAIL 就 **rc1**，不管其它 unit 多绿。
3. **FAIL 的 unit 不生成 `emit` 行**（上面输出只剩 3 条 emit）：「缺证据即不许合并」，没有 PASS 就没有
   `qa_pass` 可写。组级检查失败时相反：整组每个 unit 都 FAIL、`failed_check=group:<argv>`（同 patch 同代码，
   这是诚实归因）。

## 6. 单 unit：退化成逐卡流程（`degraded:`）

1 个 unit 的 plan（`evidence/replay-plan-solo.json`）：

```
$ $V -m team_board.cli verify-batch --plan evidence/replay-plan-solo.json --repo . --log-dir evidence/solo-scenario
batch: b-9f4f9d39  kind=solo  head=a977733  units=1
  group: exit:0  失败行（0/0，≤20）:
  unit: t_97b0ff97 pr=50 verdict=PASS  failed_check=-
degraded: b-9f4f9d39 单 unit 走逐卡流程
counts: groups=1 units=1 pass=1 fail=0
log: /home/liu/project/tb-wt/t_a19fe62c/evidence/solo-scenario/batch-b-9f4f9d39.log
batch-json: /home/liu/project/tb-wt/t_a19fe62c/evidence/solo-scenario/batch-b-9f4f9d39.json
emit: PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass --payload '{"head_sha":"a977733bf08e88d20b0c521421f5b981085fa575","run_id":<你的 run_id>}' --card t_97b0ff97
exit 0
```

`kind=solo` + 一行 `degraded:`，行为与逐卡验证一致（组级检查 + 该 unit 自检都照跑，只是没有可压的东西）。
`kind` 只看组内 unit 数：<2 恒 `solo`；≥2 且 head 全同 → `head`；≥2 且 head 不同但同 tree → `patch`。
`patch_id` 算不出来（merge-base 失败 / diff 为空）的 unit 单独一桶、组键 `head:<sha>`，**绝不靠猜并组**。

## 7. 审计：qa 的 run 必须带 `batch_id`

`batch_id` 是「这次 qa 验的是哪一批」的唯一凭据；它会写进批次账本，qa 回各卡写 `qa_pass` 时把它放进
run 的 metadata 或 summary（见 §8）。审计只读扫库：

```
$ $V -m team_board.cli verify-batch-audit --db /home/liu/.hermes/kanban/boards/devteam/kanban.db \
    --since 1793376000 --batch-dir evidence
MISSING run 383 card t_216a3cf7 started 1793383776
MISSING run 443 card t_9553f476 started 1793415745
…（共 98 行，全量见 evidence/t_a19fe62c-audit.log）
MISSING run 831 card t_2f0f1290 started 1793527230
结论: 98 条 qa run，违规 98 条
exit 1
```

**这 98 条 MISSING 不是缺陷**：本规则不回溯，`verify-batch-audit` 落地之前（以及落地当天、qa 还没开始
按批跑之前）的 qa run 本来就没有 `batch_id`。所以要看清楚的是三件事：

- rc1 = 有违规（MISSING / BAD / NOFILE 三类），rc0 = 无违规，rc2 = 参数或库不可用；
- `--since` **必填、无默认值**（不填直接 rc2）—— 否则历史 run 全红，规则永远不可能绿；
- 逐条的来源二选一：run 的 `metadata` 里的 `batch_id`，或 `summary` 里顶格一行 `batch_id: b-[0-9a-f]{8}`；
  形态不合法 → `BAD`；`--batch-dir` 下没有 `batch-<id>.json` → `NOFILE`（三类都算违规）。

库是 `mode=ro` 打开的，只读不写。

## 8. qa 回各卡执行 `emit`：原文照抄，别自己拼

`verify-batch` **不写任何事件**（角色边界：`evidence emit --card` 必须逐字等于调用者自己的卡，
一次 qa run 只能给自己那张卡写 `qa_pass`）。它只把命令原文打印出来，由各卡自己的 qa run 照抄执行。
上面 §4 的摘要里那 4 行就是原文（`run_id` 是占位符 `<你的 run_id>`，不猜、不填 0）：

```
PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass --payload '{"head_sha":"a977733bf08e88d20b0c521421f5b981085fa575","run_id":<你的 run_id>}' --card t_97b0ff97
PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass --payload '{"head_sha":"684fe9a950a21b61988fef4cdd683887b7fc6bb6","run_id":<你的 run_id>}' --card t_97b0ff97
PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass --payload '{"head_sha":"af45471ebfa4ef2e7e4a81928be263941dd944fe","run_id":<你的 run_id>}' --card t_b1a3b00f
PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass --payload '{"head_sha":"034ae4cf6608e12d481919ee5f65b9a605e366c4","run_id":<你的 run_id>}' --card t_b1a3b00f
```

- `head_sha` 是**解析后的 40 位 sha**（不是 plan 里的短 rev）：分支会被新提交顶掉，sha 不会；
- 同一张卡的两个 head（重新推送过）会给出两条 emit，各自对应一个 head 的验证结论；
- 别跨卡执行别人的 emit 行 —— 身份规则会拒，且那是伪造证据。

## 9. 退出码

| 命令 | 0 | 1 | 2 |
| --- | --- | --- | --- |
| `verify-batch` | 全部 unit PASS | 有任一 unit FAIL | 参数 / 环境（plan 不合法、base 或 head 解析不出、worktree 建不起来、日志写不进、`--dry-run` 校验失败） |
| `verify-batch-audit` | 无违规 | 有违规 | 参数或库不可用（`--since` 没填、库打不开、`task_runs` 表不存在） |

rc2 一律 stderr **恰一行** `错误: …`、stdout 0 字节（「读不到库」不许长得像「没有违规」）。

## 10. 复跑清单（本卡实测，顺序照抄）

```
cd /home/liu/project/tb-wt/t_a19fe62c
V=PYTHONPATH=src /home/liu/project/team-board/.venv/bin/python
$V -m team_board.cli verify-batch --plan evidence/replay-plan-4u2g.json --repo . --dry-run      # exit 0
$V -m team_board.cli verify-batch --plan evidence/replay-plan-4u2g.json --repo .                # exit 0
$V -m team_board.cli verify-batch --plan evidence/replay-plan-4u2g-onefail.json --repo . --log-dir evidence/fail-scenario  # exit 1
$V -m team_board.cli verify-batch --plan evidence/replay-plan-solo.json --repo . --log-dir evidence/solo-scenario          # exit 0
$V -m team_board.cli verify-batch-audit --db /home/liu/.hermes/kanban/boards/devteam/kanban.db --since 1793376000 --batch-dir evidence  # exit 1
$V -m team_board.cli output-audit --db /home/liu/.hermes/kanban/boards/devteam/kanban.db         # exit 0
```

`evidence/` 已 gitignore：plan 与日志留在工作区当证据、不进仓库；要在别的机器复跑，先按 §2 造双胞胎。

一条命令跑完全部断言（rc0 = 全绿，任一不符即 rc1 并打印 `FAIL …` 行）：

```
$ bash evidence/check-e5.sh
OK   dry-run-4u2g           rc=0
OK   run-4u2g               rc=0
OK   run-onefail            rc=1
OK   run-solo               rc=0
OK   audit-real-db          rc=1
OK   output-audit           rc=0
结论: 全部断言通过
exit 0
```

（`check-e5.sh` 断言的是「rc + 摘要行形态 + FAIL unit 不生成 emit」，不是把输出再抄一遍。）

## 11. 已知边界

- **不写事件**：`verify-batch` 只分组、跑检查、打印 emit 原文；`qa_pass` 事件永远由各卡自己的 qa run 写。
- **压缩的是「全套检查跑几次」，不是「qa 的 run 条数」**：每张卡仍要自己的 qa run。
- **组级检查跑在组首 unit 的 head 上**（排序后第一个），同 tree 才算同内容；跨 base 的两个 head 不会并组。
- **`patch_id` 只作展示**：分组判据是 tree，`patch-id` 剥空白会导致「语法坏的那个白拿 PASS」。
- **历史 qa run 没有 `batch_id` 不追责**：`--since` 无默认值，就是为了让规则从生效点开始算。
- **`--help` 文本与实现有一处漂移（本卡未改，已回报 pm）**：`src/team_board/cli.py` 的模块 docstring 与
  `--help` 里仍写「按 patch-id 分组」，实现（`verifybatch.build_groups`）分组判据是 `<head>^{tree}`。
  以实现与本文件为准；E5 只写文档、不改 `src/`。
