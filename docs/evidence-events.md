# evidence 事件（契约 v1）：写入、判定与合并闸门

判据的唯一来源是 `src/team_board/evidence.py` 的常量和 `judge()`；本文件只记录**形态**与
**本机实跑输出**，不另写一套口径。单个命令的参数细节见 README 的「evidence 事件」
「内容指纹」「证据状态机稽核」三节；本文讲一张卡从写证据到过合并闸门的全过程。

## 一、为什么要事件化

卡面是给人读的散文，机器读不出「四槽齐不齐、绑没绑当前 head」。同一批合并卡靠卡面文字
互相等证据时，六张卡会同时 `blocked` 在「等别人把证据搬过来」上 —— 事件化把证据从文字
变成 `task_events` 里带 `run_id` / `head_sha` 的结构化行，判定因此可以写成「一份取数 SQL
+ 一个纯函数」，同一份库在任何入口（`evidence audit`、`merge_guard`）给同一个结论。

## 二、kind 表与角色矩阵

七种事件，顺序即 `evidence.KINDS`（`src/team_board/evidence.py:41`）：

| # | kind | 谁写（`ROLE_RULES`） | payload 必填键（`REQUIRED_KEYS`） |
| --- | --- | --- | --- |
| 1 | `spec_sign` | `qa` | 无 |
| 2 | `impl_done` | `engineer` | `pr` / `head_sha` |
| 3 | `tier` | `pm` | `tier` / `reason` / `head_sha` |
| 4 | `qa_pass` | `qa` | `head_sha` / `run_id` |
| 5 | `review` | `pm` / `qa` | `kind` / `head_sha` / `verdict` |
| 6 | `boss_approve` | `paimeng` | `pr` / `approved_by` |
| 7 | `merged` | `repo` | ——（**不可 emit**，见下） |

角色矩阵逐字取自 `evidence.ROLE_RULES`（`src/team_board/evidence.py:52`）：

```python
ROLE_RULES: dict[str, frozenset[str]] = {
    "spec_sign": frozenset({"qa"}),
    "impl_done": frozenset({"engineer"}),
    "tier": frozenset({"pm"}),
    "qa_pass": frozenset({"qa"}),
    "review": frozenset({"pm", "qa"}),
    "boss_approve": frozenset({"paimeng"}),
    "merged": frozenset({"repo"}),
}
```

`merged` 是保留字：合并事实由 `merge_audit` 的提交 trailer（`merged-by` / `merge-card` /
`merge-pr`）承载，**不进程事件表**。所以可 emit 的只有六个
（`EMITTABLE_KINDS = KINDS - {"merged"}`，`evidence.py:63`）。

`review` 的 `kind` 只认 `defect` / `regression`（`REVIEW_KINDS`），`verdict` 只认
`pass` / `block`（`REVIEW_VERDICTS`）—— 两者共享同一个取值域常量，大小写敏感。

## 三、写入：`evidence emit`

命令形态（`PYTHONPATH=src python -m team_board.cli evidence emit --help`，实跑 rc=0）：

```
usage: team-board evidence emit [-h] --kind KIND [--payload JSON]
                                [--disc defect|regression] [--head SHA]
                                [--patch 40HEX] [--pr N] [--run-id RUN]
                                [--tier A|B|C] [--reason TEXT]
                                [--verdict pass|block] [--card t_xxxxxxxx]
                                [--db PATH]
```

`--payload '<json>'` 是权威入口；不给它时才用便捷参数拼（`--head` / `--patch` / `--disc` /
`--verdict` / `--tier` / `--reason` / `--pr` / `--run-id`）。落库前一律过
`evidence.validate_payload`，存的是 `evidence.normalize` 的结果。

**身份解析链只从库里取**（`evidence_store.resolve_identity`，`evidence_store.py:149`）：
`tasks.current_run_id` → `task_runs(id).profile`，且该 run 的 `status` 必须是 `running`。
`HERMES_PROFILE` 之类「自己报的」profile 不读 —— 环境变量只用于定位卡号。

**五种拒绝全 rc1，库里一行都不多**（`evidence_store.py:393-406` 的顺序：kind → payload →
身份 → 角色）：

| 场景 | 触发点 | stderr（前三条本机实跑；后两条本卡无法自证，标「实现文案」） |
| --- | --- | --- |
| `kind=merged`（保留字） | `check_kind` | `拒绝写入：kind=merged 由 merge_audit trailer 承载，不许 emit 成 evidence 事件` |
| payload 违约（缺键 / 多键 / 类型 / 取值域） | `check_payload` | `payload 违约：payload 缺键：reason/head_sha（kind=tier；实际键：['tier']）` |
| 角色不符 | `check_role` | `拒绝写入：kind=tier 只允许 pm，调用者 run 858 的 profile 是 engineer` |
| 无进行中的 run / run 不是 `running` | `resolve_identity` | 实现文案：`该卡没有进行中的 run：<卡>`（本卡有 running run，且跨卡 `--card` 先被拦，无法自证实跑） |
| 卡正文缺 `## 验收标准` 段（只对 `spec_sign`） | `check_spec_section` | 实现文案：缺段时拒绝（`spec_sign` 只许 qa 写，本卡为 engineer run，无法实跑） |

本机实跑（rc 逐条附后，`evidence/t_c5dd88f3-emit-reject.log`）：

```
$ PYTHONPATH=src python -m team_board.cli evidence emit --kind merged --payload '{}' --card t_c5dd88f3
拒绝写入：kind=merged 由 merge_audit trailer 承载，不许 emit 成 evidence 事件
rc=1
$ PYTHONPATH=src python -m team_board.cli evidence emit --kind tier --payload '{"tier":"A"}' --card t_c5dd88f3
payload 违约：payload 缺键：reason/head_sha（kind=tier；实际键：['tier']）
rc=1
$ PYTHONPATH=src python -m team_board.cli evidence emit --kind tier \
    --payload '{"tier":"A","reason":"x","head_sha":"16584483167bfd848482229e45bdf3b298d67a1b"}' --card t_c5dd88f3
拒绝写入：kind=tier 只允许 pm，调用者 run 858 的 profile 是 engineer
rc=1
$ PYTHONPATH=src python -m team_board.cli evidence emit --kind qa_pass --card t_1a042fbb \
    --head c641eff61e6941abbb71de18f159baee07a549ac --run-id 856
--card 与调用者卡号不一致：--card=t_1a042fbb，调用者=t_c5dd88f3
rc=1
```

参数或环境错误一律 **rc2**（`--payload` 不是合法 JSON 或不是合法 UTF-8、`--card` 形态非法、
无法确定调用者卡号、库里没有这张卡、`--db` 不存在 / 不是合法 SQLite / 缺表），stderr 恰一行
`错误: …`、stdout 为空；`rc2 与落库不并存` —— 摘要在事务里先写再 commit，stdout 写不出去
就回滚。

## 四、判定：`evidence audit`

取数只有一份 SQL（`evidence.EVENT_SQL`，`evidence.py:594`；`evidence audit` 与
`merge_guard` 的证据闸门都调 `collect()`）：

```sql
SELECT e.id, e.kind, e.payload, e.run_id, e.created_at, r.profile
  FROM task_events e
  LEFT JOIN task_runs r ON r.id = e.run_id
 WHERE e.task_id = ?
 ORDER BY e.id
```

`LEFT JOIN task_runs` 取 `profile`：**判角色只信 run 身份，不看 `tasks.assignee`**（R8）。
payload 解析失败的事件被丢弃并计数（`Collection.parse_failures`），不因一行脏数据冒异常。

**四槽**（`REQ_SLOTS`，`evidence.py:66`）：`tier` / `qa_pass` / `review:defect` /
`review:regression`。每槽取该 kind 里 **id 最大**的一条（`review` 按 `payload.kind` 分
`defect` / `regression` 两槽），逐槽按 `角色 → 缺证据 → 内容（patch/head） → 双问` 的顺序定
状态。

**三个额外判据**（槽位之外）：

1. `tier == "C"` 必须有 `boss_approve` 事件；给了 `--pr` 时还要求该事件的 `pr` 相等
   （不等 = 「缺老板点头」）；
2. `impl_done` 存在但 `payload.pr != --pr` = 「证据串卡」（跨卡证据）；
3. 角色反查：`run_id` → `task_runs.profile` 必须落在该 kind 的 `ROLE_RULES` 集合里；
   `run_id` 为空或 profile 缺失同样记「角色不符」—— 不拿 `tasks.assignee` 兜底。

**七个 VERDICT 字面量**（`evidence.py:115-121`，全仓只此七种）：

| 字面量 | 常量 | 含义 |
| --- | --- | --- |
| `PASS` | `VERDICT_PASS` | 四槽齐、全绑当前内容 |
| `缺证据` | `VERDICT_MISSING` | 某槽没有事件，或事件缺键 / 取值域外 |
| `证据对新 head 无效` | `VERDICT_HEAD_MISMATCH` | 槽位最新事件的 head/patch 与本次要判的不是同一个 |
| `双问阻塞` | `VERDICT_BLOCKED` | 某 `review` 槽最新一条 `verdict=block` |
| `缺老板点头` | `VERDICT_NO_BOSS` | `tier=C` 但无 `boss_approve`（或 pr 不等） |
| `证据串卡` | `VERDICT_CROSS_CARD` | `impl_done` 的 pr 与被合并 PR 不等 |
| `角色不符` | `VERDICT_ROLE_MISMATCH` | 写入者 profile 不在该 kind 的允许集合 |

多个问题同时存在时只取一个当 verdict，优先级（`VERDICT_PRIORITY`，`evidence.py:124`）：
`角色不符 > 缺证据 > 证据对新 head 无效 > 双问阻塞 > 缺老板点头 > 证据串卡`，其余问题写进
报告首部与 `detail`。

**「证据对新 head 无效」的语义**：本仓库**没有**额外的「陈旧」状态词 —— 判定字面量只有上面
七个，head 一变，旧证据的结论就是「证据对新 head 无效」，既不加第八个判定词，也不把旧证据
改名成另一种状态。给了 `--patch` 时事件带 `patch` 的按**内容指纹**判（纯 rebase 改父提交不
改内容 → 证据继续有效），旧事件没有 `patch` 才退回逐字比 `head_sha`。

本机实跑三例（同一张卡 `t_1a042fbb` 的证据，换 head 就翻判）：

```
$ PYTHONPATH=src python -m team_board.cli evidence audit --card t_1a042fbb \
    --head c641eff61e6941abbb71de18f159baee07a549ac --pr 90
evidence audit card=t_1a042fbb head=c641eff61e6941abbb71de18f159baee07a549ac verdict=PASS
tier | PASS | 17577 | c641eff61e69 | pm
qa_pass | PASS | 17602 | c641eff61e69 | qa
review:defect | PASS | 17603 | c641eff61e69 | qa
review:regression | PASS | 17604 | c641eff61e69 | qa
missing: -
rc=0

$ PYTHONPATH=src python -m team_board.cli evidence audit --card t_1a042fbb \
    --head 16584483167bfd848482229e45bdf3b298d67a1b --pr 90
证据对新 head 无效：tier：head c641eff61e69 != 16584483167b
证据对新 head 无效：qa_pass：head c641eff61e69 != 16584483167b
证据对新 head 无效：review:defect：head c641eff61e69 != 16584483167b
证据对新 head 无效：review:regression：head c641eff61e69 != 16584483167b
evidence audit card=t_1a042fbb head=16584483167bfd848482229e45bdf3b298d67a1b verdict=证据对新 head 无效
tier | 证据对新 head 无效 | 17577 | c641eff61e69 | pm
qa_pass | 证据对新 head 无效 | 17602 | c641eff61e69 | qa
review:defect | 证据对新 head 无效 | 17603 | c641eff61e69 | qa
review:regression | 证据对新 head 无效 | 17604 | c641eff61e69 | qa
missing: -
rc=1

$ PYTHONPATH=src python -m team_board.cli evidence audit --card t_c5dd88f3 \
    --head 16584483167bfd848482229e45bdf3b298d67a1b
缺证据：tier,qa_pass,review:defect,review:regression
缺证据：tier：无 tier 事件
缺证据：qa_pass：无 qa_pass 事件
缺证据：review:defect：无 review 事件
缺证据：review:regression：无 review 事件
evidence audit card=t_c5dd88f3 head=16584483167bfd848482229e45bdf3b298d67a1b verdict=缺证据
tier | 缺证据 | - | - | -
qa_pass | 缺证据 | - | - | -
review:defect | 缺证据 | - | - | -
review:regression | 缺证据 | - | - | -
missing: tier,qa_pass,review:defect,review:regression
rc=1

$ PYTHONPATH=src python -m team_board.cli evidence audit --card t_1a042fbb \
    --head c641eff61e6941abbb71de18f159baee07a549a
错误: --head 非法：'c641eff61e6941abbb71de18f159baee07a549a'（应为 40 或 64 位小写十六进制的提交 sha）
rc=2（stdout 0 字节）
```

以上四例的原始日志：`evidence/t_c5dd88f3-audit.log`。

报告行格式固定：`<槽位> | <判定词> | <事件 id> | <head 前 12 位> | <写入 profile>`，末行
`missing: <逗号分隔或 ->`。退出码：`0` 四槽齐且全绑当前内容**且无解析失败事件**；`1` 判定
不通过**或有解析失败事件**；`2` 参数或环境错误（stderr 一行 `错误: …`、stdout 0 字节）。

## 五、`merge_guard` 的闸门顺序

顺序写死在 `merge_guard._run_merge()`（`merge_guard.py:2540-2550`），**全部在 `gh pr merge`
之前**：

```
源分支存在 + 预检（gh pr view）
  → ① 认领唯一性（R17⑤：恰好一张未结卡顶格 PR-CLAIM: #<pr>，且卡号 == --card）
  → ② 认账 run 归属（merged-by 的 run 必须属于 --card 这张卡）
  → ③ 证据状态机（四槽 + 内容绑定 + tier=C 的 boss_approve，全调 evidence.collect/judge）
  → ④ CI 全绿（R6①；no checks reported 不算绿）
  → ⑤ 双问绑定内容指纹（R18④；缺陷 / 回归各 ≥1 条）
  → ⑥ rebase（base 必须是 head 的祖先）
  → gh pr merge --merge --delete-branch --match-head-commit <sha>
  → 出口：git ls-remote --exit-code --refs origin refs/heads/<branch>，rc=2 才算删掉
```

①–⑥ 无跳过开关（没有 `--skip-claim` / `--skip-evidence` / `--skip-ci` /
`--allow-stale` / `--force`）。**拒绝口径**：`rc=2`、**stdout 0 字节**、stderr 恰一行
`错误: …`、**零 `gh` 写调用**；`rc=1` 只留给「未达成」（合了但源分支还在、或
`--verify-only` 时分支仍存在），任何异常都不许落到 rc1。

本机实跑（`evidence/t_c5dd88f3-mergeguard-reject.log`）：

```
$ PYTHONPATH=src python -m team_board.merge_guard --pr 91 --branch wt/t_c5dd88f3
stdout 字节数=0
错误: 合并命令缺少 --delete-branch（合并卡必须带；本工具不提供不带删除的合并路径）
rc=2

$ PYTHONPATH=src python -m team_board.merge_guard --pr 91 --branch wt/t_c5dd88f3 \
    --method squash --delete-branch
stdout 字节数=0
错误: argument --method: invalid choice: 'squash' (choose from 'merge')
rc=2
```

## 六、证据缺失时自动建卡

证据闸门拒绝**不会**因此变成「等人搬证据」：`merge_guard` 在抛拒绝之前先按缺失槽位自动建
对应角色卡（`_auto_create_cards`，`merge_guard.py:1658`）。槽位 → 角色写死一份
`evidence_cards.SLOT_OWNER`（`evidence_cards.py:63`）：

| 槽位 | 建给谁 | 标题前缀 |
| --- | --- | --- |
| `qa_pass` | `qa` | 补 qa_pass 证据 |
| `review:defect` | `qa` | 补缺陷视角双问 |
| `review:regression` | `qa` | 补回归视角双问 |
| `tier` | `pm` | 补档位判定 |
| `boss_approve` | `pm` | C 档待老板点头 |
| `impl_done` | `engineer` | 补 impl_done 事件 |

- **幂等 key 形态**：`evidence-<实现卡号>-<槽位>-<head 前 12 位>`
  （`evidence_cards.py:254`；槽位名逐字用证据槽位名，`review:defect` 的冒号不转义、不二次
  改名）。同一槽位同一 head 只许一个 key，重跑不会建第二张。
- **落哪个板**：`devteam` 写死（`evidence_cards.BOARD`），走
  `hermes kanban --board devteam create` 子进程，**不直写 sqlite**；也不建合并卡 / 收口卡
  （那两张归 repo 侧）。
- **`--no-create-card`**：干跑，只把「按缺失槽位该建哪些卡」的计划行打到 stderr，不建任何卡。
- **守卫拦下**：子进程被作用域守卫拒绝时不重试、不换 env 绕过，只回一行
  `建卡被作用域守卫拦下，计划已落 evidence/<卡号>-<head12>.cards.txt，请转 paimeng`；计划
  落不了盘时同一文案改写「计划落盘失败」。**善后失败不改退出码** —— 拒绝原因一个字不改
  （`_auto_create_cards` 外面再兜一层 `except Exception`）。

## 过渡期：旧文字判据还没删

- 现状：evidence 闸门与 review_head 闸门并存，两条都 PASS 才合并；
- 删除条件：evidence 闸门已在 main 且至少有一次真实 PR 走过 evidence 闸门合并成功（merge-audit rc0 有记录）；
- 删除动作由 pm 另开卡，不在本卡、不在本链其他卡。
