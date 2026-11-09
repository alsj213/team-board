# devteam Playbook

> 团队唯一规则文件。维护人：ops。所有规则变更必须同时写一张 `assignee=pm` 的卡（卡是唯一事实源）。
> 硬约束：一条规则必须能指向至少一个真实失败案例；没有案例的先放「观察区」，不许进规则区。

## 规则区

### R1 · 验证结论必须在下游落卡闭合
QA / reviewer 给出 `ISSUES` 或 `changes_requested` 的卡，**24 小时内必须被下游卡承接**：要么开返工卡派给 engineer，要么在同一张卡上写明「不修 + 理由」。两样都没有 = 未处置，ops 直接建卡追问 pm。
- 案例：`t_eeefcd01`（QA，done 2026-10-28 02:00:36，verdict=ISSUES，P1 `summary --now` 裸 ValueError、P2 NULL status TypeError）→ 06:59 全板 0 张承接卡、0 条 comment，5 小时无人接手；ops 建 `t_90c4f66d`。
- 为什么是规则而不是个案：结论只写在 done 卡的 summary 里，就不会再有人来读；卡 done = 信号消失。

### R2 · 需求卡只在子卡全部闭合后关闭
从 chief 进来的需求卡（及其拆卡父卡），pm 只有在**全部子卡 `status=done` 且验收结论 PASS** 之后才允许关闭。拆完卡不等于需求完成；子卡未闭合期间父卡保持 `in_progress`，等外部输入时保持 `blocked`。抢先关闭的，ops 记录父卡与最后一张子卡的时间差并在卡上追问 pm。
- 案例 1：`t_c4f1a484`（需求卡，done 00:56:20）子卡 `t_631b3f59`（E2）01:04:53 才 done，父卡早关 **513 秒**。
- 案例 2：`t_5a465efb`（需求卡，done 01:40:59）5 张 E 卡最后一张 `t_c07fc14f` 01:56:26 done、QA 卡 `t_eeefcd01` 02:00:36 才以 ISSUES 收尾，父卡早关 **1177 秒**；关闭时 QA 还没开跑（`t_eeefcd01` started 01:57:21）。
- 为什么是规则：`done` 是板上唯一的闭合信号。父卡先 done = 需求在板上「已完成」而实际未验收，异常巡检与老板视图同时失真；两次都是同一动作，不是偶发。

### R3 · 验收映射表里每条 PASS 都要有本机实跑命令
pm 写 `acceptance_map` / 验收结论时，每一条 PASS 必须紧跟一条**本机亲跑的命令 + 实际输出 + 退出码**。没跑过的条目只能写 `NOT TESTED`，不许标 PASS。卡里声明的每个退出码、每个新参数的取值范围，至少各配一条越界/非法输入（0、负数、13 位毫秒、天文数字、空串）。未实跑却标 PASS = 证据不成立，ops 直接退回。
- 案例：`t_927ebb83`（run 27）与 `t_3ff92267`（run 29）两轮验收都把「`--now` 为 int」标 PASS，实际只跑了 argparse 级坏参数（坏 flag / 未知子命令 / 非法 `--date`），从未跑 `--now` 的越界值；QA `t_eeefcd01` 一换路径（`summary --now 1793160000000`）即抓到 P1（裸 ValueError traceback、rc1 应为 rc2），返工 `t_0630a104` + 复验 `t_59be4c53`。
- 为什么是规则：验收的价值在复跑，不在复述。标 PASS 而没跑等于把 QA 当第一道验收，pm 的验收权失去意义。

### R4 · 分组执行类卡必须附清单覆盖性对账命令
卡正文要求「按清单分组执行」（逐文件提交、逐文件迁移、逐条改名等）时，pm 必须在派卡前实跑一条**清单覆盖性核对**，并在卡里贴命令 + 输出 + 退出码（例：`git status --porcelain --untracked-files=all | wc -l` 与卡内显式路径清单 `wc -l` 相等，两侧 `diff` 为空）。卡里出现的「共 N 个 / 恰好 N 行」必须由该命令产生，**不许手算**。缺对账的卡 = 不合格，ops 退回 pm 重写。
- 案例：`t_bbdb794a`（D2 六组清单 8+2+1+1+3+3 = 18，基线 19）漏 `tests/integration/__init__.py`，engineer 按 D1/D2 照抄后 `git status` 仍脏、`git diff --name-status d8f92ac HEAD` 只有 18 行 → 停手交回；pm 复核 `t_3700f2a2` → 返工 `t_cf6bea8e`（第 7 条提交）→ 复核 `t_35eb0568`。一个手算数字多花 3 张卡。
- 为什么是规则：清单是执行契约，手算是整条链上唯一不可复跑的环节；engineer 只能照抄，等他发现短缺时已经到提交中途。

### R5 · 父卡由 ops 巡检报、普通 shell 会话代关（2026-10-31 修：原文「由 ops 在普通 shell 执行」不成立 —— ops 每次接到卡都是 dispatcher 拉起的 worker，它自己没有普通 shell，见 R22）
当一张卡的所有子卡都已 `done`、而父卡仍停在 `scheduled` 或 `ready` 时，ops 巡检把父卡号 + 下面两行命令写进巡检卡（或 comment）**转 paimeng**，由 paimeng 的 gateway 交互会话 / 人工 shell（非 dispatcher 拉起的 worker 进程）执行这两行关掉父卡，并在父卡上留一行「由 ops 代关，原因：worker 作用域限制」：

```
hermes kanban --board devteam unblock <父卡> --reason "R5: 子卡全部 done，ops 代关"
hermes kanban --board devteam complete <父卡> --summary "<子卡汇总>"
```

`--board` 是 `hermes kanban` 的**全局参数，必须放在子命令之前**（2026-10-28 实跑 `hermes kanban --help` / `hermes kanban complete --help` / `hermes kanban unblock --help` 核对，三条均 exit 0）；`complete` 报 live-claim 守卫时追加 `--force`。只跑 `unblock` 不跑 `complete` 会把父卡退回 `ready` 再派给 pm，下一轮仍关不掉 —— 两行必须连着跑。

**边界（防止这条规则变成越权口子）**：ops 只做状态流转。不改 summary 的验收语义、不改任何 verdict、不评价子卡质量、不替 pm 写验收结论 —— `--summary` 只能是子卡结果的原样汇总（子卡 id + verdict + 关键产出）。

**判据（本板实测，别想当然）**：**不能用 `task_links` 判父子关系** —— `t_e32041c1` 与其四张子卡的 `task_links` 行数为 **0**（子卡只挂在彼此身上，需求卡没被 pm 当 parent），全库 36 条边里没有一条指向它。所以子卡清单只能从父卡的 body/comment（标「子卡」的那段）里读出 id，再逐张查 `tasks.status`；「全部 done」以 `tasks.status='done'` 为唯一凭据，不看 worker 自述。

- 案例：`t_e32041c1` 的子卡 `t_18713037`/`t_7623a70a`/`t_322e3086`/`t_90ee3bb7` 全部 done 且 PASS 后，父卡卡在 `scheduled` 关不掉；worker 报 `Kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI` 与 `worker is scoped to task t_90ee3bb7; refusing to mutate t_e32041c1` 两条拦阻；最后由 paimeng 在普通 shell 手动 unblock + complete。时间线（`task_events`）：run 43 于 1793163415 置 `scheduled`（R2-WAIT-CHILDREN）→ 1793164238 unblocked → 1793164242 completed，**滞留 823 秒**。
- 为什么是规则：worker 是 delegated child，按设计禁止 mutate 自己作用域外的卡（护栏在 `hermes_cli/kanban.py:153`、`hermes_cli/kanban_db.py:149`）。只要链路是「最后一张子卡由 worker 收尾」+「父卡要靠人关」，这个死结每单必现 —— 不是 pm 偷懒，是权限边界，所以补的是「谁来代关」这一环，不是再骂 pm 一遍。

### R6 · 合并 PR 的三条前置 + 评审只认 Review 记录，不要求 approve
合并一张 PR 前必须**同时**满足三条，缺一条不许 merge，三条的实测输出都要抄进卡：

① `gh pr checks <n> -R alsj213/team-board` **rc=0（全 pass）** —— 命令原文 + rc + 输出末行；
② 卡上有 **R3 证据**：每条 PASS 对应一条本机实跑命令 + 真实退出码（同 R3，不许复述代替）；
③ head 分支名 **`wt/<卡号>`** —— 用 `gh pr view <n> --json headRefName` 核对，不看样子。

**评审以 PR 页面 Review 记录为准**：`gh pr review <n> --comment -b "<结论 + 依据>"` 即可，**不要求 approve 标记**。本仓评审人与作者是同一个账号（`alsj213`），平台禁止自审批 —— 按「必须 approve 才能合」的口径，PR #2/#3/#4 **一张都合不了**（三张的 Review 状态实测全是 `COMMENTED`）。

**判据口径（实测，别想当然）**

- `gh pr checks` 在**从没跑过 workflow 的分支**上输出 `no checks reported on the '<branch>' branch` 且 **rc=1**（PR #2/#3 实测）。这既不是 pass 也不是失败 —— 卡上必须原样贴这一行并标 `NO_CHECKS`，**不许当 pass 用**；`NO_CHECKS` 时按 R7 处理（R7 条件① 不成立 → 不许自动合并）。
- 本仓 CI 从 PR #4 合入 `main` 才存在（`.github/workflows/test.yml`，`pytest (3.12)`，merge `f2ee664`，2026-10-28T11:39:43Z）。在此之前合并的 PR #2/#3 只能标 `NO_CHECKS`。
- 分支名反例：PR #4 的 head 是 `wt/ci-setup` 而非 `wt/<卡号>` —— 因为它是 setup PR 不是卡。卡出的 PR 必须带卡号；setup/杂活 PR 在卡上写明例外理由。

- 案例（均已 `gh` 实跑核对，2026-10-28）：PR #2（5 文件，merge `69f8d9e`，mergedAt 05:04:50Z，2 条 Review **全 COMMENTED**，`gh pr checks` rc=1 `no checks reported`）；PR #3（3 文件，merge `0b50ab6`，11:39:57Z，1 条 COMMENTED，rc=1 `no checks reported`）；PR #4（1 文件，merge `f2ee664`，11:39:43Z，1 条 COMMENTED，rc=0 `pytest (3.12) pass 11s`）。三张的 `mergedBy` 都是 `alsj213`（全队共用单账号，板上分不出是 pm 还是老板点的手）。
- 为什么是规则：「评审通过」在 GitHub 上有可写状态三种，单账号只能写 `COMMENTED`；把门槛定在平台不允许的动作上，规则等于**永久阻塞**，最后每条都要绕到人身上，规则失效。所以门槛落在「Review 记录存在且写了结论 + 三条前置」，不落在 approve 按钮。

### R7 · 风险分级（2026-10-28 修订：改为三档，src/ 不再一律上报老板）

**A 档 · 自动合并（pm 自己合，不报老板）**，同时满足四条：
① CI 全绿：`gh pr checks <n>` rc=0（`no checks reported` 不算绿）；
② 改动只涉及 `tests/`、`docs/`、`README`；
③ 改动文件数 ≤ 3；
④ 无新依赖（不碰 `pyproject.toml`、`requirements*.txt`、任何 lock 文件）。

**B 档 · 复核后自合（pm 自己合，不报老板）**：不满足 A 档，但同时满足五条：
① CI 全绿（同上）；
② 本地全量测试 rc=0，输出抄进卡；
③ 第二视角双问（缺陷视角 + 回归视角）已跑，结论无 P1/P2 或已同轮闭合；
④ 改动文件数 ≤ 10；
⑤ 不碰 `.github/`、依赖文件、数据迁移或删库脚本、凭据与权限相关文件。
**B 档包含修改 `src/` 的常规改动** —— 这是本次修订的核心：`src/` 不再自动升级到老板。

**C 档 · 必须报老板点头**，满足任一即进入：
· 碰 `.github/` CI 配置；
· 碰依赖文件（`pyproject.toml`、`requirements*.txt`、任何 lock 文件）；
· 数据迁移、删库、批量删除脚本；
· 单卡删除行数 > 100 行；
· 改动文件数 > 10；
· 涉及凭据、密钥、权限、账号配置。

- 为什么改（2026-10-28，老板原话「改」）：旧版要求「一碰 `src/` 就升级、等人点头」，而本仓几乎所有 PR 都碰 `src/`，等于把分级做成 100% 上报，违背老板定的「不要多头汇报、判断权下放」。参照 Cursor 官方 PR Routing & Approval（https://cursor.com/docs/approval-agents）：用风险分与策略文件自动批准低风险 PR，人只看超过阈值的。
- 老板只需要定阈值（即 C 档六条），不需要逐 PR 审批。
- 升级时卡上必须写清「哪一条不满足 + 实跑输出」，不许写「风险较高」代替判据。

### R8 · 判定「谁写的代码」只认 run 身份与角色标签，不认 `tasks.assignee`

条文（可用 `python3 /home/liu/.hermes/profiles/ops/workspace/who_did_it.py <卡号>` 复跑，退出码 0 = 归因明确）：

1. 断言「某 profile 写了某张卡的代码」必须给出下面至少两条凭证：
   - ① 该卡 `task_events.kind='created'` payload 里的 `assignee`（创建时派给谁）；
   - ② 该卡 `task_runs.profile` 的 run 序列（谁真跑过；`review_requested` 是实现 run，`changes_requested` / `completed` 是验收 run）；
   - ③ `task_events.kind='review_requested'` payload 里的 `implementer` / `reviewer` —— 由内核按 current run 的 `task_runs.profile` 写入（`hermes_cli/kanban_db.py:3353–3361`），不是 worker 自己填的；未认领的卡该字段可能回落到 `assignee`，这种情形以 `task_runs.profile` 序列为准。
   - 一条能跑的检查：`python3 /home/liu/.hermes/profiles/ops/workspace/who_did_it.py <卡号>`。它的两个回落值**不是硬结论**：无 `review_requested` 流 → 取首条 run 的 profile；`implementer` 回落到 `tasks.assignee`（未被认领的卡）→ 一律以 `task_runs.profile` 序列为准。
2. **`tasks.assignee` 不作凭据**：`kanban_request_review(reviewer=…)` 会把卡改派给 reviewer，于是每张走完 review 的实现卡都停在 `assignee=pm`。凭 assignee 判执行者 = 把 reviewer 判成作者。
3. **提交 author 与提交时间窗也不作凭据**：engineer 在宿主跑时 author 是 `alsj213 <1204662321@qq.com>`，在 docker 沙箱跑时是 `engineer@local`；多个 worker 并行时 pm 的 run 窗口内会混入他卡 engineer 的提交（实测 `t_f43fa954` 的 pm run 95 窗口内 1 条 `94be67a`，作者 engineer、属别的卡）。
4. 由此，「pm 写生产代码」唯一成立条件是：`task_runs.profile='pm'` 的 run 自己产生了 **tracked** 文件改动（`src/`、`tests/`、`.github/`、`pyproject.toml` 等）。pm 为验收写的探针脚本与证据日志（未跟踪，通常在 `evidence/` 下）不算生产代码 —— 但不许落真仓库工作区（见观察区候选）。本条的机器检查：`python3 /home/liu/.hermes/profiles/ops/workspace/pm_touched_code.py [<卡号>…]`（rc 0 无告警 / 1 有告警并列出 卡 id·run id·文件 / 2 用法错误或仓库不可读；不带卡号 = 扫全板）。时间窗在这里只是**必要条件**、不构成凭据（见第 3 条），所以**它故意比字面判据窄一档**：窗口内命中路径但提交信息不带本卡卡号的提交只列 `INFO` 不算告警（否则并行 run 必误报，实测 `t_f43fa954` 的 pm run 95 窗口内 `94be67a` 属他卡 engineer 产物）；`--loose` 关掉这层归属守卫，只用于对照实验。
5. 拿不出第 1 条凭证就在卡上指责某人写了代码 = 证据不成立，按 R3 同一标准退回；写这种卡的 ops 自己撤卡，不许让被指控方举证。

- 案例：`t_e0866bd4`（2026-10-28 复盘卡，`created_by=user`）以 `t_f43fa954` 的 `tasks.assignee=pm` + `created_by=pm` 为据，断言「pm 自己拆卡时把实现卡派给了自己」并要求写进规则。实测全不成立（`who_did_it.py t_f43fa954` rc=0）：`created` 事件 `assignee=engineer`；run 76/89/96 `profile=engineer`（实现，其中 run 76 卡龄 30 秒即开跑）、run 86/95/99 `profile=pm`（review，两轮 `changes_requested`）；三次 `review_requested` payload 全写 `implementer=engineer, reviewer=pm`；pm 三个 run 窗口内该卡提交 **0 条**。同一误读在观察区已记过一次（E1–E5 的 `assignee=pm` 复核），这是第二次 → 按「观察区再犯一次即升规则区」升级。
- 反面口径的代价（实测，别照抄卡面提议的告警条件）：「标题含实现类关键词 + `assignee=pm` 即告警」在全板 83 张卡上命中 8 张，其中 4 张真实现卡（`t_1b200562`/`t_f43fa954`/`t_06c7b458`/`t_68b0a37a`）全部是「创建时派给 engineer、review 后停在 pm」—— 误报 4/4 = 100%，装上就天天响。
- 为什么是规则：板上「谁在执行」有两个字段（`tasks.assignee` 当前持有者 / `task_runs.profile` 实际执行者），只写一个必然被读错；两次误读是同一个动作（拿 assignee 当作者），不是粗心，是缺口径。

### R9 · 夜间自治（23:00–08:00 静默，按策略合代码）

原则：**夜里没人拍板，不等于流程停摆。** 参照 Cursor 官方 PR Routing & Approval（https://cursor.com/docs/approval-agents）：低风险 PR 由策略自动批准，人只在超过阈值时被叫醒；第二天早上看清单。

① **不阻塞**：夜间遇到的 C 档 PR 不新建「等人点头」的阻塞卡，也不挂住整条链。卡上打 `[night-queue]` 标记，写清「哪一条 C 档判据不满足 + 实测输出」，其余卡继续跑。白天老板一话批。

② **夜间放宽一档**：满足下面全部五条时，pm 可在夜间自合并，卡上标 `B·night`，不等老板：
　· CI 全绿（`gh pr checks` rc=0，`no checks reported` 不算绿）；
　· 第二视角双问已跑，无 P1/P2（或已同轮闭合）；
　· 改动文件数 ≤ 10；
　· 不碰凭据 / 密钥 / token / 账号配置，不碰数据迁移与删库脚本，单卡删除 ≤ 100 行；
　· 卡上有 R3 证据（本机实跑命令 + 真实退出码）。

③ **夜间绝不做的事**（一律等白天，写了也压着）：
　· force push、直推 main、改分支保护或 CI 权限；
　· 动凭据、密钥、`.env`、任何 token；
　· 删除数据、跑迁移、`git reset --hard` 到别人的提交之上；
　· 跨仓库操作。

④ **必须报告**：夜间任何自合并，08:20 晨报里逐条列出（PR 号 + 档位 + 一句话理由 + merge commit）；`[night-queue]` 里积压的 C 档 PR 一并列出，等老板一句话。

⑤ 判据一律用命令产出，不许写「风险较高」「看情况」。夜间没有老板可问，含糊就等于不执行。

- 为什么是规则（2026-10-28，老板原话「这个合入规则还是要参考 cursor 的，他有过一晚上不打扰我的经历自己按照策略在合代码」）：把「人拍板」从每张 PR 变成一次阈值配置，是唯一能让交付链在夜间继续走的办法；但夜间没人兜底，所以放宽的同时把「绝不做」列成硬清单，代价可控。

### R10 · git 推送通道：SSH 优先，https 留备选，故障按固定顺序自处

（2026-10-29 定，老板原话「优先走 ssh」。失败案例见「教训记录 · 2026-10-29 · t_ad16e55a」：本机到 github.com:443 的 https git 传输整条断——直连 SNI 阻断 rc=28、代理 127.0.0.1:7892 上游节点死返 502 / TLS eof，`git ls-remote` rc=128；同期 SSH/443 一直可用 rc=0，`t_f27fbe62` / `t_d5149e95` 两张卡却卡在「代码写完了推不上去」。）

① 默认通道 = SSH over 443。主 checkout `/home/liu/project/team-board` 的 `remote.origin.url` 必须是 `git@github.com:alsj213/team-board.git`；`~/.ssh/config` 里 `Host github.com` → `Hostname ssh.github.com` / `Port 443`。所有 `git worktree add` 出来的卡 worktree 共享这份 `.git/config`，因此 `git push origin <branch>` 不需要任何额外参数。

② https 不许删，只降级为备选：另设 remote `origin-https = https://github.com/alsj213/team-board.git`。网络恢复后它仍是备份通道，删掉它等于把今天这个坑再挖一次。

③ 推送失败时按顺序自处，不许先停下来问人：
　a. `timeout 20 ssh -T git@github.com; echo rc=$?` → 输出出现 `Hi alsj213!` = 通道没坏（GitHub 不提供 shell access，本条正常时 **rc=1**，判据看输出不看 rc），去看 git 自己的报错正文（多半是分支名、非快进、权限）。
　b. 输出不含 `Hi alsj213!`（或 rc=255 之类连不上）→ `timeout 20 ssh -vvT git@github.com 2>&1 | tail -20`，确认 `~/.ssh/config` 仍是 `ssh.github.com` / `443`，把断点（DNS / TCP / 握手）写进卡。
　c. 想确认 https 是否恢复：`timeout 20 git -C <repo> ls-remote origin-https refs/heads/main; echo rc=$?` → rc=0 就用 `git push origin-https HEAD:refs/heads/wt/<卡号>`。
　d. a/b/c 全灭 → 不改配置、不手工造提交、不用 Git Data API 手搓提交；在卡上 `kanban_block(kind="transient")`，把 a/b/c 三条命令原文 + rc 贴卡并指派 ops。断点在代理节点时按 `t_ad16e55a` 的结论转老板（Windows 侧 FlyingBird 换节点/更新订阅），不重复排查。

④ 判据（卡上引用本规则时贴这两条实跑）：
　`git -C /home/liu/project/team-board remote get-url origin` → 以 `git@github.com:` 开头；
　`timeout 20 ssh -T git@github.com; echo rc=$?` → 输出含 `Hi alsj213!`（**rc=1 属正常**，GitHub 无 shell access；2026-10-29 本机实测输出 `Hi alsj213! …` + rc=1，同轮 `git push` 与 `ls-remote` 均 rc=0，证据见 `t_1b4d66ff` 附件）。

⑤ 口径废除：2026-10-29 之前 pm 在卡面（如 `t_f27fbe62` / `t_d5149e95`）写的「不许改 remote、不许换 SSH」自本规则生效日起作废。那句的初衷是防止各卡各自乱配，现在改为「默认 SSH + 只改主 checkout 一处配置 + 保留 https」。

### R11 · 证据分支独立命名空间 `evidence/*`（2026-10-29 定，老板原话「都给我添加强制约束，不能靠自觉」；本条为并行组第 4 条，破例建卡见 t_c9148b7c）

① **命名**：长期留证的证据分支一律叫 `evidence/<单段名>` —— `evidence/` 之后**只许一段**，字符集 `[A-Za-z0-9][A-Za-z0-9._-]*`。推荐带卡号：`evidence/t_<8位卡号>`；同一张卡多份证据加主题后缀，例：`evidence/t_589cc33a`、`evidence/t_589cc33a-ci`、`evidence/t_589cc33a-perf`。`evidence/a/b`（多段）、`evidence/-x`（非字母数字开头）不算证据分支。

② **不占卡分支命名空间**：卡分支仍然只认名字**精确等于** `wt/t_<8位小写十六进制>` 的分支。证据分支不参与规则 A（提交归属）、不参与规则 B（checkout 偏移），不进「检查的卡分支」，不影响稽核退出码。

③ **判定由程序给出，不靠人记**：`python -m team_board.cli branch-audit`（CI 的 `branch-audit` job 同款）—— 输出里单列一段 `证据分支（N 条，不占卡分支命名空间、不计入提交归属检查）:` 并逐条列名；`--branch evidence/t_xxx` 输出「… 是证据分支，不占卡分支命名空间，跳过提交归属检查」且 rc=0。

④ **卡分支口径一字不放宽**：`wt/t_<不存在的卡号>` 仍按「未声明卡号 / 跨卡提交」判红（rc=1）；不许把「证据分支不被扫」做成豁免 —— `.team-board/branch-audit-allow.txt` 保持 0 条，不许加条目、不许减 CI 检查项、不许临时关闸门。

⑤ **旧分支不动**：已经叫 `wt/*` 的旧证据分支（如 `wt/t_589cc33a`）原样保留：不删、不改名、不回改历史。本规则只约束此后的新证据分支。

⑥ **谁来验**：开卡人（pm）在派卡契约里写死 `evidence/<单段名>`，并在卡上贴出 ③ 的实跑命令 + rc；engineer 建证据分支必须用该命名空间；ops 巡检看到长期存在的 `evidence/*` 分支不算「在途卡分支」，不建卡追问。

### R12 · 合并后必删源分支（强制 --delete-branch）（2026-10-29 定，老板原话「都给我添加强制约束，不能靠自觉」；本条为并行组第 2 条，落地卡 t_40fa5375，机器闸门 t_bbeab29f / 真机证据 t_a867da93）

① 合并一张 PR，只用这一条命令（填好 PR 号 / 卡号 / **合并卡号** / checkout 路径）：

    cd <checkout> && PYTHONPATH=src python3 -m team_board.merge_guard \
      --pr <PR号> --branch wt/<卡号> --method merge --delete-branch \
      --card <合并卡号> --by <profile>/run<NNN> \
      --subject "<单行合并提交标题>" --body-file <merge-record --body-out 生成的 body 文件绝对路径> \
      --repo <checkout>

> `--card` 是**合并卡**（那张写「父卡可关」的收口卡）的卡号，不是 PR 分支对应的实现卡号；`--branch wt/<实现卡号>` 是**实现卡**的 head 分支（生成模式必填），两者常常不是一张卡。本板两张卡本来就不是一张（例：合并卡 `t_8886ae85` 合的是 `wt/t_bb6600d8`）。body 用 `merge-record --card <合并卡号> --by <profile>/run<NNN> --pr <N> --branch wt/<实现卡号> --subject "<同一句>" --body-out <绝对路径>` 生成。认账四参数（`--card` / `--by` / `--subject` / `--body-file`）自 PR #30 起在合并模式**必填**，缺任一项 → `错误: 合并命令缺少 --card`（rc2、不发起合并）；`--verify-only` 与这四个参数互斥（同给 → rc2）。照抄本条必须真能跑通，旧形态（不带四参数）实测 rc2。

缺 `--delete-branch` → rc2、**不发起合并**；合并 rc=0 但远端源分支仍在 → rc1、**不算完成**；只有「合并 rc0 且 `git ls-remote --exit-code --refs origin refs/heads/wt/<卡号>` 返回 rc2（无输出）」才算合并完成。`--method` 的取值由「合并方式统一」那条定，本条只锁 `--delete-branch`，不替它选。

② 合并卡（收口卡）正文必须把①这条命令**原样抄进去**并填好参数。写「合并后记得删源分支」这种话无效 —— 强制的判据是「命令里有没有 `--delete-branch`」，不是「有人记得」。

③ 只删本卡自己合并后留下的源分支。`wt/t_589cc33a` 与别人的分支一律不删、不改名、不批量清理历史分支。

④ 不许用「合并成功后手工 `git push origin --delete` 补一条」代替 `--delete-branch`；也不许把「删不掉」记成合并成功 —— rc1 就是 rc1，不许改写。

⑤ 机器判定口径（`git ls-remote --exit-code --refs origin refs/heads/<分支>`）：rc=0 = 仍在（不通过）、rc=2 = 已删（通过）、其它 rc = 判不了（按 rc2 处理，不许当通过）。

- 为什么是规则：历史上 PR #2–#14 全是「合完就走」，源分支堆在远端，板上看不出哪些卡的分支已经合过，稽核又把已合并的旧分支当成「卡还在途」——红灯越积越多，最后团队习惯性忽略红灯。把「删」写进合并命令本身（缺了就拒跑、删不掉就判失败），才不靠谁记得。
- 历史对照（实测）：`t_f51328fe`（PR #9 合并）与 `t_9abe2b3b`（PR #11 合并）当时都没有删除动作，`git ls-remote --exit-code --refs origin refs/heads/wt/t_f43fa954` 至今 **rc=0**（分支仍在）。

### R13 · 合并方式统一：只允许 merge commit（2026-10-29 定，老板原话「都给我添加强制约束，不能靠自觉」；本条为并行组第 3 条，破例建卡见 t_24e43d1a）

① **合并方式 = create a merge commit**。squash、rebase 一律不许。
② **命令层只有一个入口，且不是本条新建的**：`src/team_board/merge_guard.py`（并行组第 ⑤ 条 `t_40fa5375` 的实现），本条只钉 `--method` 这一维：`python -m team_board.merge_guard --pr <n> --branch wt/<卡号> --method merge --delete-branch --card <合并卡号> --by <profile>/run<NNN> --subject "<单行合并提交标题>" --body-file <body 文件绝对路径>`，`--method` 只允许 `merge`。（形态与 R12 ① 同一份、照抄即可：`--branch` 是 argparse 必填，认账四参数自 PR #30 起在合并模式必填；旧文那句不带 `--branch` / 不带四参数的写法实测 rc2，只作历史，不许再抄。）
   `merge_guard` 落地之前用过渡命令（`--delete-branch` 是 R12 的要求，不许为「只测方式」去掉）：
   `gh pr merge <n> -R alsj213/team-board --merge --delete-branch; echo rc=$?`
③ **平台层强制（已生效，不靠自觉）**：`allow_merge_commit=true` / `allow_squash_merge=false` / `allow_rebase_merge=false`。另一种方式在 API 层被拒（实测 `GraphQL: Squash merges are not allowed on this repository.` rc=1；rebase 同）。它是平台开关，不是第二个闸门。
④ **合并前先验配置没被改回去**：逐键断言 `.allow_merge_commit==true and .allow_squash_merge==false and .allow_rebase_merge==false` → 必须输出 `true`；`false` 或 rc≠0 → 停手报 ops，不许先合再补。（不要比对整串字面量：`gh --jq` 按键名字典序重排，实测输出是 `{"ms":true,"r":false,"s":false}`。）
⑤ **合并后判定「用的是哪一种」**：判本 PR 自己的合并提交（不判 `origin/main` 尖端，尖端恒真且会混入别人的 PR）：
   `sha=$(gh pr view <n> --json state,mergeCommit --jq 'select(.state=="MERGED")|.mergeCommit.oid')` → 空则禁止判定；
   `git cat-file -p "$sha" | grep -c '^parent '` → `2`（或 ≥3 octopus）= 合规；`1` = squash/rebase 违规（R1 同轮返工）；`0`/空/rc≠0 = 取值失败，不是结论。
⑥ **本条不管「合并是否完成」**：完成判据归 R12（合并 rc=0 + 源分支已删 + 三条输出贴卡）。本条只回答「用的是哪一种」。
⑦ **做不到机器校验的部分明写**：「禁止直推 main」**没有平台强制点** —— 本仓私有，`gh api repos/alsj213/team-board/branches/main/protection` → HTTP 403 `Upgrade to GitHub Pro or make this repository public…`（rulesets 同 403）。只能事后稽核：`git log --first-parent --format='%h %s' origin/main -20` 视检。
（样例与证据见卡 t_24e43d1a；仓库内唯一源 docs/merge-policy.md）

### R14 · 返工必须标因：spec / impl / env，考核口径一分为二（2026-10-29 老板原话「需要区分」）

2026-10-29 定，老板原话「需要区分」—— 返工必须分因，不许把所有返工都算到执行者头上。失败案例见「教训记录 · 2026-10-29 · t_df640caa」：该卡 C-2 被 pm 打回，pm 在 `changes_requested`（event 2036，`run151 pm`）里亲笔写「卡面 C-2 算法是我 pm 写错的，不是实现走样」，而 `who_did_it.py t_df640caa` rc=0 → `VERDICT: engineer`；现役返工口径（`load_rework_counts` 按 `tasks.assignee` 求和、`render_member` 交付数同源，`src/team_board/db.py:77–80` / `commands.py:232–237`）把这一次记在 engineer 名下，错在写卡人。

① 标记格式（唯一，写在 `request-changes --reason` 正文最前，行首顶格、全大写、冒号后半角空格）：

```
REWORK-CAUSE: spec
REWORK-OWNER: pm
REWORK-EVIDENCE: 卡面 C-2 算法是我 pm 写错的，不是实现走样
```

- `REWORK-CAUSE` ∈ `spec` / `impl` / `env`，一条事件只许一个因（写两个 = 无效标签）。
- `REWORK-OWNER`：`impl` → `engineer`；`spec` → 写卡人 `pm` / `chief`（`tasks.created_by='user'` 的卡由真人写卡，填 `user`）；`env` → `-`。
- 三行与 reason 同一次落盘，位置是 `task_events(kind='changes_requested').payload.reason`；事件行入库即不可改 —— **这就是「不许事后补标」的机制保证**，不靠自觉。事后另发 comment 补的标签一律不认。

② 三类判据（只认评审正文里的事实，不认印象）：
- `spec`：卡面 / 验收标准 / 契约本身写错、自相矛盾、卡内数字与 fixture 冲突 → 责任在写卡人。engineer 逐条照卡面执行、仍被验成不过，就是 spec。
- `impl`：卡面契约清楚无歧义，实现与契约不符（rc 码、输出格式、边界行为、清单覆盖） → 责任在 engineer。
- `env`：环境 / CI / 网络 / 依赖 / 推送通道故障（R10 那一类） → 不计任何个人。
- 三类都不成立（评审意见分歧、需求变更）→ 不许硬塞：按 R1 另开卡处置，**不产生** `changes_requested`。

③ `REWORK-EVIDENCE` 必须逐字取自同一段 reason 正文（可中文、单行、不改写、不写「详见评论」）；只标 `spec` / `impl` 不给原话 = 标签无效，按 `UNTAGGED` 计，不得用于考核。

④ 两条考核口径（分母不同、不许合并；`env` 单列，不进任何个人分母）：

| 口径 | 分子 | 分母 |
| --- | --- | --- |
| **impl 返工率**（考核执行者） | 标 `impl` 的 `changes_requested` 事件数，按 `review_requested.payload.implementer` 归属（**不按** `tasks.assignee`，R8） | 该执行者实现且 `status='done'` 的卡数（`archived` 不算交付，沿用 `render_member` 口径） |
| **spec 返工率**（考核拆卡质量） | 标 `spec` 的事件数，按 `REWORK-OWNER` 归属 | 该写卡人 `tasks.created_by=<profile>` 且 `status='done'` 的卡数；`created_by='user'` 单列一行，不进任何 bot 分母 |
| `env`（不考核） | 标 `env` 的事件数 | 无 |

- 对外的**总返工率** = (spec + impl) ÷ 交付数；`env` 单独报数，不许混进总返工率。
- member 视图里的「被驳回」只是原始计数、不是考核数：它按 `tasks.assignee` 求和，而 `request-review --reviewer pm` 会把 assignee 覆盖成 pm（R8 案例 `t_e0866bd4`）。

⑤ 机器校验（唯一命令，只读，不新增脚本、不改板）：

```
sqlite3 -header -column /home/liu/.hermes/kanban/boards/devteam/kanban.db < /home/liu/.hermes/profiles/ops/workspace/rework_audit.sql
```

输出三段：① 按 `REWORK-CAUSE` 的分因占比（`UNTAGGED` 单列）② 生效后每条 `changes_requested` 的三行完整性（`OK` / `MISSING-*`）③ impl / spec 两条返工率 + `env` 单列数。2026-10-29 实跑：真板 rc=0，① `UNTAGGED 3 / 100.0%`、② 无 `MISSING-*`、③ `impl engineer 14 0 0.0`；临时副本注入 spec / impl / env 各 1 条后变成 `UNTAGGED 3 50.0 / env 1 16.7 / impl 1 16.7 / spec 1 16.7`、spec 行 `pm 54 1 1.9`、校验三行全 `OK` —— 四类都能分出来。该 SQL 只实现本条条文，不许改宽（尤其：不许把 `UNTAGGED` 折成 `impl`、不许把 `env` 并进总返工率）。

⑥ 历史口径（不回溯）：本规则生效时刻 epoch 1793251339（2026-10-29 13:22 CST）。此前板上的 3 条 `changes_requested`（`t_f43fa954` event 1015 / 1143、`t_df640caa` event 2036）全部无标 → 一律 `UNTAGGED`，**不补标、不进任何考核分子**；观察区已留一句。

⑦ 谁写、谁不写：标签由**打回方**（pm 验收打回；hatch / ops 打回同理）在调用 `request-changes` 时写；ops 巡检不改标签、不补标签，只在 ⑤ 报出 `MISSING-*` 时把卡号交回打回方，由打回方在卡上留 comment 说明（该 comment 不补票，只留痕）。复盘卡引用归因时同样只引 `REWORK-CAUSE` + 原话，不许自己给结论（对齐本文件「2·5」）。

### R15 · 合并认账：谁合的 PR 可追溯（2026-10-29 定，老板原话「都给我添加强制约束，不能靠自觉」；工程侧机器闸门已由 `t_ad5e7ff6` 组落地，本条只写操作侧）

（编号说明：**R11 已被「证据分支独立命名空间」占用，R12 / R13 已由并行组落地（`t_2df659ad` / `t_8f1ef2f4`，两条均已 `done`），R14 已用于「返工必须标因」**，本条顺延取 **R15**，不占别人的号、也不催别人让号。）

> **2026-10-29 19:05 升级（上级：`t_38895c77`，依据 chief 替换后的需求卡 `t_ad5e7ff6`）**：本节正文里的「三字段（`Merge-By` / `Merge-Card` / `Merge-PR`）」段**已被下方 `R15-A` 三判据版取代**，作为历史留痕原样保留、不再单独作为闸门；**现行条文以 `R15-A` 为准**（字段名统一小写 `merged-by`，并增加 author 白名单、committer 必须是本 bot 两条机器判据）。两段冲突时 `R15-A` 优先。

合并一张 PR 的**行为本身**必须留下三件，缺一件不许合并：
- `Merge-By: <profile>/run<NNN>` —— 哪个 bot 的哪一次 run 按的合并（R8 口径：只认 run 身份，不认 GitHub 账号）
- `Merge-Card: t_xxxxxxxx` —— 依据哪张卡
- `Merge-PR: #<N>` —— PR 号
合并提交 sha 由 git 自己给（就是那条合并提交），不另写字段。

**强制在哪（机器，不靠自觉）**：三字段写进合并提交 message 末尾 trailer 块，CI job `merge-accountability` 常驻判定 —— 没有/写错的合并在 main 上直接红。合并前后各一条命令：
- 合并前：`PYTHONPATH=src python -m team_board.cli merge-record --card t_xxx --by pm/runNNN --pr NN --branch wt/<实现卡号> --body-out /tmp/t_xxx-merge-body.txt`
  再把它打印的 `merge_guard` 命令（含认账四参数，body 就是上面那份文件）照抄执行 —— R12 ① 是合并的唯一入口，不再照抄裸 `gh pr merge`。
- 合并后：`PYTHONPATH=src python -m team_board.cli merge-audit --repo . --card t_xxx --require; echo rc=$?` → 必须 rc=0，把回读到的三字段 + 合并提交 sha 贴到卡面。
- 预检不合格（rc≠0）就不许合并；不许加豁免、不许减检查项、不许临时关闸门。

**三层口径（写清，别混）**
- 甲层（机器可判、可拦）：trailer 三字段齐 + 格式合法 → `merge-audit` rc0；缺/错 → rc1；CI 红就是真红。
- 乙层（卡面留痕，内容自述）：卡面认账块由 `merge-record` 生成、`merge-audit --card` 回读；`Merge-By` 是 `profile/run` 标签，**不是** GitHub 层面的身份证明。不许把乙层冒充甲层。
- 丙层（做不到）：**本卡无法机器校验「账号级身份区分」：GitHub 侧单账号只记 alsj213，Merge-By 是执行者自述的 run 标签，不是 GitHub 层面的身份证明；要做账号级区分需每 bot 一账号或换 Gitea，不在本卡范围。**

**历史口径**：基线（`.team-board/merge-audit-baseline`）之前的合并只能回读 PR 号 / 卡号 / sha，`Merge-By` 写「无法判定」；不做历史批量回填。

#### R15-A · commit 层可追溯（谁合的 PR 查得到，不靠账号）

三条机器判据（全部进 CI，红就是真红）：
1. 合并提交必须含 `merged-by: <profile>/run<NNN>`，且 profile 部分**严格等于该卡的 tasks.assignee**；
2. 该卡分支 `wt/<卡号>` 上每条提交的 author 必须 ∈ {该卡 assignee, engineer, qa}；
3. 每条提交的 committer 必须是本 bot 身份（`.team-board/bots.tsv` 里的一行，且与 author 同一 profile）；
   这条同时挡住 `--no-verify`、提交消息里已有 Co-authored-by、hooks 被重装三种失效场景。

强制在哪（机器，不靠自觉）：
- 合并前：`PYTHONPATH=src python -m team_board.cli merge-record --card t_xxx --by <profile>/runNNN --pr NN --branch wt/<实现卡号> --body-out /tmp/t_xxx-merge-body.txt`
  → 照抄它打印的 `python3 -m team_board.merge_guard … --delete-branch …` 命令（含认账四参数，body 就是上面那份文件）；R12 ① 下不许再照抄裸 `gh pr merge`。
- 合并后（判据①）：`PYTHONPATH=src python -m team_board.cli merge-audit --repo . --card t_xxx --require; echo rc=$?` → **必须 rc=0**，把回读到的 `merged-by / merge-card / merge-pr / 合并提交 sha` 贴到卡面。
- 分支身份（判据②③）：`PYTHONPATH=src python -m team_board.cli commit-audit --repo . --card t_xxx --rev HEAD; echo rc=$?` → **必须 rc=0**。
- 每个 bot 在自己的 worktree 里先配身份：`bash scripts/setup-bot-identity.sh <profile> /path/to/worktree`
  （`extensions.worktreeConfig=true` + `git config --worktree user.name/user.email`；pre-commit 只做早警，权威判定在 commit-audit 与 CI）。
- 预检不合格（rc≠0）不许合并；不许加豁免、不许减检查项、不许临时关闸门。

三层口径（写清，别混）：
- 甲层（机器可判、可拦）：三判据在提交元数据里可判 → `merge-audit` / `commit-audit` rc0/rc1；CI job `merge-accountability` 与 `commit-identity` 常驻跑。
- 乙层（卡面留痕，内容自述）：卡面认账块由 `merge-record` 生成、`merge-audit --card` 回读；`merged-by` 是 run 标签 + 提交身份，**不是 GitHub 层面的身份证明**。不许把乙层冒充甲层。
- 丙层（做不到）：**本卡无法机器校验「账号级 / 页面级身份区分」：GitHub 侧单账号只记 `alsj213`，提交元数据里的 bot 身份不改变页面显示；要做账号级区分需每 bot 一账号或换 Gitea，不在本卡范围。**（GitHub 页面恒显示 alsj213，本卡不承诺解决。）

历史口径：基线（`.team-board/merge-audit-baseline`）之前的合并只能回读 PR 号 / 卡号 / sha，`merged-by` 写「无法判定」；不做历史批量回填。

落地记录（2026-10-29 19:05，`t_38895c77`）：判据②③ 与身份表的机器实现见上级卡 `t_51f5fad0`（`commit-audit` / `.team-board/bots.tsv` / `scripts/setup-bot-identity.sh`）。本节引用的四条命令均为该实现 worktree 实测存在：`merge-audit --help` rc=0（含 `--require`）、`commit-audit --help` rc=0（含 `--card` / `--bots` / `--db`）、`scripts/` 内 `setup-bot-identity.sh` 与 `make-audit-board.py` 均在。

**落地（ops 那一半）**：巡检发现 main 上合并提交缺 `merged-by` / `merge-card` / `merge-pr` 三字段（CI `merge-accountability` 红）→ 把卡号与 PR 号报 pm，由 pm 按 R15 补录 body 或开返工卡；ops 不自行改历史、不补写字段、不合并。来源：hatch `charter-log.md` 2026-10-29 R15 条「待办（留卡）」原句。

### R16 · 卡正文 2048B 硬上限（2026-10-29 定，老板原话「不是可选项」；机器闸门在 hermes 内核 + body-audit）

① **硬线**：`tasks.body` 的 **UTF-8 字节数 ≤ 2048** = 合规，超过即不合规。按**字节**不按字符（700 个汉字 = 2100 字节，已超）。数字只此一处，别处只引用「2048 字节硬上限」。

② **建卡即拦**：`hermes kanban create` 与 `kanban_create` 工具在超限时**不落库**，告警原文逐字：
`body 超过硬上限：<n> 字节（UTF-8），上限 2048 字节；长背景/证据请放附件或卡评论，卡正文只写差异部分。`
CLI 退出码 **rc=2**（stderr 恰一行、零 traceback）。没有 `--allow-long-body`、没有环境变量开关、不截断正文。

③ **存量卡能查**：`PYTHONPATH=src python3 -m team_board.cli body-audit` 列全板未结卡逐卡字节数 + 分桶 + 超限清单，`--all` 含已结卡。rc：`0` 无超限 / `1` 有超限 / `2` 参数或环境错误。

④ **长内容只走两条路**：卡**评论**（`kanban_comment`）或**附件**（`kanban_attach`）；卡正文只写**差异部分**，固定段落（完成契约 / 验收三段式 / 边界 / 不要做的事）在 `docs/card-template.md` 里写一次，卡正文引用、不重抄。

⑤ **边界（硬规）**：不追溯改写已 `done` 卡的正文；不为压缩让验收标准、边界、禁止事项变得不可追溯（只允许移到附件、模板或评论）；**只告警不拦截不算达标**；在途卡不被强行中断（建卡拦截不回溯、不改写正在跑的卡）。

⑥ **证据**：交卡时贴 `body-audit` 真跑命令 + rc + 输出原文，新建卡逐张贴字节数；转述不算证据（R3 同口径）。

### R17 · 一个 PR 只许一张未结卡认领（认领字段 = 卡正文顶格 `PR-CLAIM: #<N>`）

（2026-10-29 定，案例 PR #34 / 卡 `t_cb6ac485`（收口）与 `t_04eb3882`（合并）；裁定卡 `t_6b766b2d`。本条只管「谁有权对一个 PR 发 gh 写操作」，合并方式归 R13、合并命令归 R12、认账归 R15。）

① **认领字段（唯一，机器可判）**：卡正文里**顶格**一行

```
PR-CLAIM: #<N>
```

形态 `^PR-CLAIM: #[1-9][0-9]*$` —— 顶格、`#` 后正整数、行尾无尾随字符。同一张卡可写多行，一行一个 PR。

② **引用不算认领**：其它卡（实现卡、返工卡、修复卡、收口卡、验收卡）写到该 PR 时写顶格 `PR-REF: #<N>`，或写在散文里；`PR-REF` 与散文**不参与**唯一性判定。

③ **唯一性**：同一 `#N` 在**未结卡**（`status ∉ {done, archived}`）里必须**恰好 1 条** `PR-CLAIM`。该卡即该 PR 的合并权持有者，也是 `merge_guard --card` 的唯一合法取值（即 R12 ① 里的「合并卡」）。

④ **判定命令（唯一入口，只读，不写板）**：

```
PYTHONPATH=src python3 -m team_board.cli pr-claim-audit --pr <N>
```

rc0 = 恰好一张未结卡认领（stdout 打该卡号）；rc1 = 0 张「无未结卡认领该 PR」或 ≥2 张「重复认领」；rc2 = 参数或库错误。

⑤ **何时跑**：合并卡开卡即写 `PR-CLAIM`；**任何 gh 写操作（合并、`gh pr review/comment`、关 PR）之前**跑一次 ④，rc≠0 停手。`merge_guard` 合并模式先跑同一判定，rc≠0 或认领卡号 ≠ `--card` → rc2、**零 gh 写操作**。

⑥ **事后交叉**：`merge-audit` 回读的 `merge-card` 必须等于该 PR 的唯一 `PR-CLAIM` 持有卡；不等 = 「认领的人不是动手的人」，按 R1 同轮开卡处置。

⑦ **不采用「同一 PR 只许一张未结卡」**：后续修复卡、返工卡、验收卡天然指向同一 PR —— 2026-10-29 实测 #34 有 **7 张卡**正文提到它（`t_cb6ac485` `t_04eb3882` `t_5c07db8f` `t_d2498e69` `t_d5f6c108` `t_880a8ab2` `t_6b766b2d`），其中只有 1 张是合并动作、6 张是修复/后续/引用。按「只许一张未结卡」判，这 6 张天天假红。所以判据落在 `PR-CLAIM` 这个显式字段上，不落在「有没有提到」。

⑧ **不采用 `merge-card` trailer 做事前判据**：trailer 只在**合并后**存在，防不了「两张卡都准备合」。实测 `6ea8cca` 的 trailer 是 `merge-card: t_cb6ac485`，而自述合并的卡是 `t_04eb3882` —— 事后能查出不一致，事前零拦截。

**案例（2026-10-29，PR #34）**：`t_cb6ac485`（run 249）与 `t_04eb3882`（run 254）都把自己的卡面写成「#34 是我的活」（前者「顺序 3」、后者「目标」），散文层面谁也没越界；run 249 于 14:58:09Z 先合（merge commit `6ea8cca`），run 254 的 `merge_guard` 因「源分支已不在 origin」被拦成 rc2、零写操作 —— 拦住它的是 R12 的分支存在性检查，**不是认领规则，属侥幸**。本规则生效后：合并卡写 `PR-CLAIM: #34`、收口卡写 `PR-REF: #34`，`pr-claim-audit --pr 34` 在 14:5x 之前就会把「两张卡都认领」判成 rc2 拦截。

**历史不追溯**：已并入的合并提交不回改 trailer；本规则生效前的卡面不补标记。

### R18 · 双问结论必须绑定被合并的 head sha（head 变了要重跑）

（2026-10-29 定，案例 PR #34；裁定卡 `t_6b766b2d`。本条补的是现行「合并前必须双问」硬规的**绑定缺口**，不改那两条问题本身。）

① **标记行（唯一形态，机器可判）**：两条双问结论贴到 PR 页面（`gh pr review --comment` 或 `gh pr comment`）时，正文里必须有**顶格两行**：

```
REVIEW-HEAD: <40 或 64 位小写十六进制 sha>
REVIEW-KIND: defect|regression
```

同一条正文里两行都有才算一条标记；写在行中、被别的字包住、或分在两条评论里都不算。

② **绑定判据**：合并时的 head sha 上，`defect` 与 `regression` 必须**各 ≥1 条**。只有别的 sha 的标记 = head 变了没重跑，判不通过。

③ **判定命令（唯一入口，只读，不写 PR）**：

```
PYTHONPATH=src python3 -m team_board.cli pr-review-head-audit --pr <N> --head <sha>
```

rc0 = 该 sha 两类齐；rc1 = 缺一类 / 两类都缺 / 只有别的 sha 的标记；rc2 = `--head` 非 40-64 位小写十六进制、`--pr` 非正整数、gh 调用失败。

④ **拦截点**：`merge_guard` 合并模式在 `--match-head-commit` 之前跑 ③，`--head` = 预检读到的 `headRefOid`；rc≠0 → 整个 merge_guard rc2、一行 stderr、**一个 gh 写操作都不发**。**没有跳过开关**（不加 `--skip-review-head`，同 R12 ① 口径）。`--verify-only` 不跑（它不合并）。

⑤ **三层口径（别混）**：
- **甲层机器可判**：标记行存在 + sha 逐字等于合并时的 `headRefOid`（`--match-head-commit` 另保证「预检到合并之间被推送就失败」，两条合起来 = 合进去的就是被双问覆盖的那条 sha）。
- **乙层内容自述**：结论「阻塞/非阻塞」与依据文字由评审人自己写，机器不判对错；标记只是标签，不许把乙层冒充甲层。
- **丙层做不到**：**谁真正起了独立进程、结论是否成立、评论发出后有没有被编辑或删除，都没有机器凭据** —— GitHub 不提供评论编辑历史，标记可被复制伪造。本条只消除「head 变了忘重跑」这一类，**不证明评审可信**。

⑥ **历史不追溯**：PR #34 于 2026-10-29T14:58:09Z 合入 head `d2be80d`，对 `d2be80d` 的两条双问 15:05:17Z/15:05:19Z 才落（对 `0ae9593` 的两条 14:43:11Z/14:43:14Z 是旧 head），按条文生效前案例留痕：不回滚、不重做、不补标。

**案例（2026-10-29，PR #34）**：合入时 head 是 `d2be80d`，PR 上存量的双问对象是 `0ae9593`（其 P2 已由返工卡 `t_d2498e69` 修掉）—— 也就是说合进 main 的代码**没有**在双问覆盖之下；对 `d2be80d` 的两条 15:05 才补，结论均非阻塞、内容不回滚，但顺序不合规。带标记行之后：若 15:05 那两条已写 `REVIEW-HEAD: d2be80da…`，14:58 的合并会被 rc1 拦（该 sha 上当时零标记），只能等双问落地后再合。

### R19 · 定时任务必须落在 DeepSeek 半价窗口（2026-10-29 定，老板原话「要加上」；R19 编号空闲，接 R18）

**适用范围（2026-10-29 老板裁定，硬规）**：本规则只约束团队 profile（paimeng / chief / pm / engineer / ops / qa / hatch）；`wsl-gp-agent`、`wsl-uumit-0706` 属其他项目，不在本规则范围，也不许去改它们的 cron。

① 半价窗口（取自 DeepSeek 官方定价页 https://api-docs.deepseek.com/zh-cn/quick_start/pricing/ ，2026-10-29 核对）：
　· 工作日 **18:00–次日 09:00**、**12:00–14:00**；
　· **周末与法定节假日全天**。
② 高峰（全价）：工作日 **09:00–12:00**、**14:00–18:00**。
③ 规则：任何新增或修改的定时任务（cron）都必须落在半价窗口。建卡时必须在卡上写出所选时点，并注明它属于上面哪个窗口。
④ 机器可校验：要能一条命令列出所有 profile 的 cron 时点，逐个判定是否落在"工作日 09–12 / 14–18"，命中即报红。判据用 cron 表达式解析，不许人工目测。
⑤ 例外：no-agent 脚本模式（零 token，如看板自愈每 2 分钟）不受此限，但必须在卡上标明它是 no-agent。
⑥ 案例（2026-10-29 实跑核对）：现有 6 条 cron —— ops 巡检 01:10/07:10/12:10/18:10、ops 每日 07:40、paimeng 晨报 08:20、hatch 周体检 周一 07:50、R8-4 告警 01:20/07:20/12:20/18:20、看板自愈 every 2m（no-agent）—— 全部合规，无一条落在高峰。

**ops 复核注（2026-10-29 23:43 CST 写入前实测；只补事实，不改 ①–⑥ 一个字）**

取数命令（逐 profile 读 `cron/jobs.json` 的 `schedule.expr` / `no_agent` / `enabled`）：

```
grep -n '"name"\|"expr"\|"display"\|"no_agent"\|"enabled"' /home/liu/.hermes/profiles/*/cron/jobs.json
```

- **① ② 的官网依据实测（2026-10-29 23:44 CST）**：`timeout 25 curl -s -o /home/liu/.hermes/profiles/ops/cache/scratch/deepseek_pricing.html -w 'http=%{http_code} bytes=%{size_download}\n' https://api-docs.deepseek.com/zh-cn/quick_start/pricing/` → `http=200 bytes=26182`，rc=0；页面原文逐字摘（`grep -o '空闲时段价格.\{0,140\}' <该文件>`，rc=0）：「(2) 空闲时段价格为高峰时段价格的一半。北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段；其余时段，包括周末及中国法定节假日全天均为空闲时段。」→ 与 ① ② 口径一致（页里写作 `9:00`，语义等同 `09:00`，不改条文）。
- **团队三个 profile 实得 8 条**（7 条 enabled + 1 条 disabled）：
  - ops `10 1,7,12,18 * * *`（巡检心跳-四时段，agent）→ 01:10 / 07:10 / 12:10 / 18:10，全部落窗（18:00–09:00 与 12:00–14:00）
  - ops `40 7 * * *`（每日巡检验复，agent）→ 07:40 落窗
  - ops `48 12 * * *`（自检-定时器，`enabled=false`、`repeat` 1/1 completed）→ 12:48 落 12:00–14:00
  - paimeng `30 0,2,4,6 * * *`（kanban 通知订阅守夜，no_agent）→ 00:30–06:30 落窗
  - paimeng `20 8 * * *`（晨间团队简报，agent）→ 08:20 落窗
  - paimeng `every 2m`（看板自愈-自动关父卡+滞留标记，no_agent）→ 按 ⑤ 豁免
  - paimeng `20 1,7,12,18 * * *`（R8-4 执行者证据告警，no_agent）→ 01:20 / 07:20 / 12:20 / 18:20，落窗
  - hatch `50 7 * * 1`（团队周体检，agent）→ 周一 07:50 落窗
- **逐条判定结论**：无一条落在工作日 09:00–12:00 / 14:00–18:00 → 与 ⑥「全部合规」一致。
- **与 ⑥ 的两处差额（事实，不影响结论）**：⑥ 列 6 条，`jobs.json` 实为 8 条 —— ⑥ 未列「kanban 通知订阅守夜 `30 0,2,4,6 * * *`（no_agent）」与「自检-定时器 `48 12 * * *`（disabled、已 completed）」。两条都不落高峰，故「全部合规」成立；**条数以本注为准**。
- **④ 实装状态（缺口，明写）**：workspace 内没有 cron 时点稽核脚本（`grep -rln 'cron' --include=*.py --include=*.sh .` 空输出、rc=1），④ 要的「一条命令」**尚未实装**。实装前只能逐 profile 读 `schedule.expr` 自己比对，**不满足 ④「不许人工目测」** —— 需另开卡实装；本条卡只改 playbook，不写代码、不动别处。
- **④ 的「所有 profile」范围未定（明写）**：本机另有 `wsl-gp-agent` / `wsl-uumit-0706` 两个 profile（UUMit 任务市场，非本团队 bot），其中有 **agent 模式**作业 `0 * * * *` / `30 * * * *`，会在工作日 09–12 / 14–18 **命中红线**（同两个 profile 的其余作业是 no_agent，按 ⑤ 豁免）。是否把非团队 profile 纳入「所有 profile」，本注不替老板裁、不改 ④ 原文 —— 交 pm / 老板定。 → **2026-10-29 老板已裁：只约束团队 profile（paimeng / chief / pm / engineer / ops / qa / hatch），`wsl-gp-agent` / `wsl-uumit-0706` 不纳入、也不许改其 cron，见本条「适用范围」段；④ 原文不动。**

### R20 · force push 被无人值守守卫拦住 = `env` 事件，走人工代推，不许绕（2026-10-30 定；卡 `t_bbff1465`，证据 `t_f8978b9b` 附件 + run 253 / 256 / 266 / 278）

（编号说明：本条原建议取 R19，**R19 已被「定时任务必须落在 DeepSeek 半价窗口」占用**（上一节），故顺延取 **R20**，不占别人的号、也不催别人让号。）

① 事实：护栏的真实形状（以下每条都实测，没有推断）

- 无人值守会话（cron / `-q` / webhook）里，工具层按**命令文本正则**判危险，规则在 `tools/approval_detection.py:447–448`：`\bgit\s+push\b.*--forc[a-z]*\b` 与 `\bgit\s+push\b.*-f\b`。命中即拦，原文：
  `BLOCKED: Command flagged as dangerous (git force push (rewrites remote history)) but single-query mode (-q) runs without a user present to approve it.`
  同一阻拦已在板上 5 张卡上复现，其中 2 张逐字引了这段原文：`t_f8978b9b` run 253（engineer；原文 + 附件 `t_f8978b9b-push-blocked.log`）、`t_8a220e95` run 274（engineer，引用同句）；`t_cb6ac485` run 249 记「pm 与 engineer 两次独立尝试都被终端工具的危险命令护栏拦下」，`t_477c04f4` run 280 与 `t_98ce4cf7` run 288 以「force push 被硬拦」转述（转述不算证据，仅作覆盖面）。
- **反面事实（必须一起写）**：`git push origin +HEAD:refs/heads/wt/<卡号>` 这种 refspec 加号形态**不命中**上面两条正则，本板已 3 次推成功 —— run 278（`t_b368476d`，`git push origin +HEAD:refs/heads/wt/t_4c08419a`，推前 `git ls-remote` 核远端仍是旧 head 5497a84，`push_rc=0`，PR #39 CI 7/7 绿）、run 256（`t_d5f6c108`）、run 266（`t_eaaca884`）。→ 这堵墙是**命令文本匹配差异**，不是权限不足；谁把它写成「做不到」，下一个人就会再绕一次圈子（`t_477c04f4` / `t_70ef360e` 已是第三、四张同题卡）。
- 配置现状（本卡实测）：`grep -n '^approvals' /home/liu/.hermes/config.yaml` rc=1（无 `approvals` 段）；`grep -rn 'command_allowlist' config.yaml profiles/*/config.yaml` 零命中（全机无白名单）。
- **新证据（2026-11-01 01:13，ops 巡检 cron 会话）**：守卫按**命令文本正则**匹配，所以**转述**也会中 —— 往 `hermes kanban comment` 里抄「`git push --force-with-lease …`」这一串，整条命令直接被拦（`BLOCKED: … git force push (rewrites remote history) …`，两次都拦）。→ 写卡/写评论转代推时**不许把命令串抄进去**，改成「命令形态按 R20 ③，卡的原 blocked reason 正文里有」，否则连留痕都留不下。

② 归因（接 R14）：该阻拦**不是 engineer 的 `impl` 返工**，一律记 `env` —— 打回方写 `REWORK-CAUSE: env` / `REWORK-OWNER: -`，`REWORK-EVIDENCE` 逐字取拦截原文那一行。`env` 不进任何人的分子分母、不用于考核；因它 `block` 的卡不算交付失败，解封重开也不计返工。

③ 正确动作（engineer 侧，唯一一条路径）

1. 在 worktree 内 `git rebase origin/main`（**只 rebase，不推**）；
2. 本地全绿，两条实跑并贴输出原文：`PYTHONPATH=src … -m pytest -q` 与 `commit-audit`，均 rc=0；
3. `kanban_block(kind="needs_input", reason=…)` 停在等代推 —— 不 `done`、不转派、不改 assignment；
4. 卡上 comment 写**四要素**，缺一条代推方有权直接拒：worktree **绝对路径** / 分支 `wt/<卡号>` / rebase 后**新 head sha**（40 位）/ `pytest` 实跑结论行（形如 `1037 passed, 6 skipped in 82.46s`，rc=0）。run 278 的坑：卡面给的 worktree 路径不存在，实际在 `/home/liu/tb-wt/<卡号>` —— 路径必须抄 `git worktree list` 里那条。

④ 代推（现为 paimeng，2026-10-30 07:14 定）：在**交互 shell** 里 `git push --force-with-lease origin HEAD:refs/heads/wt/<卡号>`；推前 `git ls-remote origin refs/heads/wt/<卡号>` 确认远端仍是评论里的旧 head（这才是 lease 语义，别裸用 `+` 顶替）；推后把 `push_rc` 与推后 head 回帖到卡上，再 unblock。

⑤ 禁用绕过三条（**制度约束，机器拦不住**；违反一次按 R14 记 `impl` 到绕过者名下）

- 禁 `+refs` / `+HEAD:refs/…` 加号 refspec 形态顶替 —— 本板已有 3 次先例，本条是来堵这个口子的，不是来承认它的；
- 禁 `gh api` 手搓提交 / 直接改远端 ref（`POST /git/refs` 一类）绕过推送；
- 禁「删远端分支再重推」（`git push origin --delete` + 重推 = 同样重写历史，且丢掉 PR 关联）。

⑥ 只写一次：卡面、三份 `SOUL.md` 只许写「见 playbook R20」+ ③④⑤ 的一句话指针，**不许把本节条文复制进卡正文**；本节要改只改这一处。

⑦ 三选一（**属老板职权，hatch 只上交不代裁**；卡 `t_bbff1465`，2026-10-30）。未裁定前一律按 ③④ 人工代推执行：
- ① `config.yaml` 配白名单，只放 `--force-with-lease` 到 `wt/*`（按 R7 是权限配置 = C 档）。**机制修正（本卡实测）**：白名单的键不是 `approvals.*`，而是**顶层 `command_allowlist`**（`tools/approval_floors.py:188–204`，glob 匹配整条命令文本，命中即放行，检查点在危险判定之前，`tools/approval.py:1085`）；`approvals.single_query_mode: approve` 是**全局一刀切**（连 `rm -rf` 一起放），不推荐。代价：只认**单条、不含 `&&`/`;`/`$()` 的命令文本**（`_has_allowlist_shell_operator`，同文件 144–185），`cd X && git push …` 这种写法照样被拦；且白名单**按 profile** 生效（写在 engineer 那份 config 里 = 该 profile 这一类命令永久放行）。
- ② 正式放行 refspec 加号形态（写进规则即合法，⑤ 第一条随之作废，须同时改本节）。代价：`+` **没有 lease 保护**，别人并行改同一分支时会直接覆盖对方提交；本板 3 次成功全靠推前手工 `ls-remote` 对旧 head，靠自觉。
- ③ 维持现状：人工代推，每轮一张卡的分支等一次人。

### R21 · 复用别人工作区里的 worktree：用 `--workspace dir:<绝对路径>`，不要用 `worktree` 型（2026-10-30 定；案例 `t_96a7767b` run289/292，同型 `t_1838d395`）

① 事实（本机实测，不是推断）：`t_96a7767b`（「解冲突：wt/t_dd98c031 rebase 到 origin/main」）建卡时 `workspace_kind=worktree`、`workspace_path=/home/liu/project/tb-wt/t_dd98c031`、`branch_name` 空（即 `wt/t_96a7767b`）→ dispatcher 两次都在 spawn 阶段失败（`run289 spawn_failed` / `run292 gave_up`，`consecutive_failures=2`），error 逐字：
`workspace: git worktree add failed for /home/liu/.worktrees/t_96a7767b on branch wt/t_96a7767b: fatal: Not a valid object name: 'HEAD'.`

② 机制（读 `hermes_cli/kanban_db_workspace.py:714-783`）：worktree 型先算 `branch_name = task.branch_name or "wt/<本卡号>"`；若 `workspace_path` 已是一个**在别的分支上**的 linked worktree（本例 `wt/t_dd98c031` ≠ `wt/t_96a7767b`），内核**拒绝复用**（防跨卡提交串味，行为正确），改在 `<repo>/.worktrees/<卡号>` 新建；该回退锚点解析到 `$HOME`，而 `/home/liu` 是一个**没有任何提交**的 git 仓库（`git -C /home/liu log` → `fatal: your current branch 'main' does not have any commits yet`），于是 `git worktree add -b wt/<卡号> … HEAD` 必然报 `Not a valid object name: 'HEAD'`。

③ 规则（写卡人执行）：要复用**已有 worktree**（别人的分支，或本卡本来就去改那条分支）时用 `--workspace dir:<该 worktree 绝对路径>`，或 `scratch` + 卡面写绝对路径；`--workspace worktree:<path>` 只在「本卡自己的卡分支、且 path 就是本卡 worktree」时用，并且必须同时给 `--branch wt/<本卡号>`。派卡前判据一条：`workspace_kind` 与 `branch_name` 自洽，不自洽不许派。

④ 佐证：同类卡 `t_b368476d`（同样 rebase 到 origin/main）用 `scratch` + 卡面绝对路径跑通（run 278，rc=0）；同型未派发的 `t_1838d395` 已被 `t_6ee962f6` 点名要求改。

⑤ 边界：env 侧（`$HOME` 是空仓库）本规则不改、不删 `/home/liu/.git`（删数据属停手项）；要根治由 hatch 上交老板三选一（给 board 设 `default_workdir=/home/liu/project/team-board` / 清理 `$HOME/.git` / 维持现状 + 本条规则）。

### R22 · 跨卡状态流转只能由「非 worker 会话」执行：不许写成 `assignee=ops` / `assignee=pm` 的卡（2026-10-31 定；案例 `t_d5f23eb3`，同型先例 R5 案例 `t_e32041c1`）

① **事实（本机实测，不是推断）**：dispatcher 拉起的所有 worker（不管 assignee 是 ops / pm / repo）对本卡以外的卡**做不了任何 mutate**，两条路都 fail closed：
- CLI 路：`hermes_cli/kanban.py:153` + `hermes_cli/kanban_db.py:149`，fence 来自 `agent/delegation_context.py:141-162` —— worker 的 terminal 子进程继承 `HERMES_DELEGATED_CHILD_CONTEXT=<kanban 根>`（`HERMES_KANBAN_TASK` 反而被 scrub 掉），`kanban_path_is_fenced()` 恒真，`complete` / `unblock` / `edit` / `assign` / `schedule` / `block` … 一律 `kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI`（rc=1）。
- 工具路：`tools/kanban_tools.py:251-264` `_enforce_worker_task_ownership` → `worker is scoped to task <本卡>; refusing to mutate <他卡>`。
- worker 对别的卡**只有两个合法动作**：`kanban_comment`（留话）与 `kanban_create`（建后续卡）。**没有第三个**。

② **能执行跨卡流转的只有非 worker 会话**：paimeng 的 gateway 交互会话、人工 shell、系统 crontab 里的看护脚本（`guard_run.sh` 那一类，进程不在 worker 血缘里）。**「ops 的普通 shell」不存在** —— ops 每接到一张卡就是 worker，卡面写「必须在普通 shell 执行」对它等于「必须由别人执行」。

③ **写卡约束**：凡需要另一张卡 `unblock` / `complete` / `edit` / `assign` / `schedule` 的活（R5 父卡代关、R17 失效认领摘除或作废、任何「把某卡收掉」的要求），**不许把动作写成 `assignee=ops` 或 `assignee=pm` 的卡**。要么写卡人自己在非 worker 会话里跑完再开卡留痕，要么把命令原文写进卡面/评论转 paimeng，由人执行。派给 worker 只会得到一张 blocked 卡 + 一轮往返。

④ **不许绕（命中即按 R1 开卡）**：`env -u HERMES_DELEGATED_CHILD_CONTEXT`、直接 `sqlite3` 写 `kanban.db`、排一个 cron 转一手让别的进程代写 —— 三种都是绕过护栏且绕过 kernel 事件，谁做谁错。ops 巡检遇到「卡面要求跨卡 mutate」的卡：**先报 paimeng，不许在卡上让执行者重试、也不许自己绕**。

⑤ **验收侧判据（贴卡用）**：`hermes kanban --board devteam edit --body "…"` 是**整段替换**，只传一行会把整张卡正文清空 —— 用 `edit --body` 改首行时必须传完整正文；不确定就改用 `complete`。

⑥ **案例**：`t_d5f23eb3`（2026-10-31 12:31，pm 建，body 明写「必须在普通 shell 执行」）要求把 `t_e31bf1ac` 作废或把首行 `PR-CLAIM: #53` 改成 `PR-REF: #53`；ops 实跑 `complete` / `unblock` / `edit` 三条全部 rc=1（同一句 CLI 拒绝），工具侧 `kanban_complete(task_id="t_e31bf1ac")` 回 `worker is scoped to task t_d5f23eb3`，最终只能 blocked 转人。同型先例：R5 案例 `t_e32041c1`（父卡滞留 823 秒，最后由 paimeng 普通 shell 代关）—— 同一原因第二次出现，故从 R5 的「为什么是规则」里提出来单独立条，并把 R5 的执行者改成 paimeng。

### R23 · 看护件自检（A1）三条硬口径：不自检自身、结论必进收件箱、响应窗口按「下一轮巡检」（2026-11-01 定；案例 guard_health 2026-10-31 19:10 → 2026-11-01 01:10 自锁连红 73 轮）

① **guard_health 不检查自己**。`guard/<件>.ok` 里记的是该件上一次的 rc，`guard_health` 若把自己列进 `ALLOW`，一次红就写进自己的 `.ok`，下一轮读到自己 → 永远红：2026-10-31 19:10 起 73 轮（约 12 小时）全是这条假红，期间其它件一直全绿。判据：`python3 workspace/check_board_anomalies.py` 之类的巡检看不到它，只有 `guard_health.py` 自己会喊。**修法**：`ALLOW` 里去掉 `guard_health`；本件自身存活由 crontab 排程 + `.ok` 的 mtime 兜底（它死了，`.ok` 就不更新）。改过 `guard_health.py` 后必须跑 `--strict`（应 rc=0）+ `--selftest`（应 rc=0）留证。

② **自检结论必须进收件箱**。A1 的响应链是「巡检第一件事读 `workspace/patrol-hits.md`」，而 `patrol_quick` 原先只写 P1–P6，自检红根本进不了收件箱 —— 上面那 12 小时无人应，一半原因是**看得见的通路没接**。**判据**：`python3 scripts/patrol_quick.py` 的输出里必须能出现 `[自检红 A1] <件名>: <原因>` 行（由 `patrol_quick.guard_reds()` 调 `guard_health.check()` 搬结论，不重写判据、走同一套 6 小时去重）。

③ **响应窗口 = 下一轮 agent 巡检，不是 15 分钟**。原文「红了你 15 分钟内做什么」在巡检降到每天两次（01:10 / 12:10，07:40 复核）之后**结构上不可执行**：19:10 红、下一次 agent 巡检是次日 01:10，中间 12 小时注定无人应。**改成**：红了的当下由 `patrol_quick` 每小时的 `[自检红 A1]` 行进收件箱，**下一轮 agent 巡检必须处置**（看 `guard/<件名>.log` 末尾 → 在板记「哪个件 / 红了多久 / 初步原因 / 下一步」→ 能机器修当场修、命令抄进同一条记录）；连续两轮巡检仍红 → 升级 paimeng。**不许停派工**（不 pause dispatcher、不改 cron 频率、不 block 在跑的卡）。

④ **事实澄清（R22 的边界，2026-11-01 01:12 实测）**：ops 的**巡检 cron 会话不是卡 worker** —— 环境里只有 `HERMES_SUPERVISED_CHILD=1`，**没有** `HERMES_KANBAN_TASK`，因此不被 delegated-child fence 拦：对父卡 `t_4a1028f5` 实跑 `hermes kanban unblock` rc=0、`complete --result` rc=0（卡上已 comment 留痕）。有卡号绑定的 worker（dispatcher 为某张卡拉起的那个）仍按 R22，一律 rc=1。判据一行：`env | grep -c HERMES_KANBAN_TASK` → 0 = 不受 fence。


### R24 · 看护脚本判「归属」只认合并语义卡，且必须 import 仓库唯一判据（2026-11-03 定；案例 `t_ec78eebc` 误归档，同型前科 4 张）

（2026-11-03 定，卡 `t_d356f6fe`。本条是 `kanban_unstick.py` R3 连续第 5 次误归档的收口：2026-11-01 18:30 起 #88 `t_947530a6` / `t_7a21e747`、#91 `t_c625dd52`、#93 `t_e9e995d6`，直到 #94 `t_ec78eebc` 才被拆开看。）

① **归属只认合并语义卡**：判「同一 PR 有多张未结卡认领、谁是归属」时，只有 `team_board.prclaim.needs_claim(title)` 为真的卡（标题以 `合并卡` / `收口卡` / `合并 PR #n` / `收口：` 开头）才算认领者。实现卡、验证卡正文首行的 `PR-CLAIM: #n` 是「这个 PR 是我开的」自认行，按 `src/team_board/mergeowner.py` 模块说明第 5 条「**本卡自己认领不算**」不构成归属。判据四类标题与 `pr-claim-audit --gaps` 同源，见 R17 ③。

② **歧义不许机器动手**：同一 PR 有 **≥2 张**合并语义卡同时未结认领 → 只进 digest「需人判」，**不许 archive、不许 unblock**。机器能判的只有「归属卡数为 0 或 1」，之外的判断权在人。

③ **判定代码只许 import，不许本地抄正则**：`sys.path.insert(0, "/home/liu/project/team-board/src")` + `from team_board.prclaim import needs_claim`；取不到 → 该段整段跳过并 `rc=2`（`guard_health` 会红、`patrol_quick` 会进收件箱），**不许降级成本地抄一份正则** —— 抄一份就是第二份判据，两边必然漂。

④ **案例**：2026-11-03 02:30 `kanban_unstick` R3 对 PR #94 archive 了合法收口卡 `t_ec78eebc`（`assignee=repo`、首行 `PR-CLAIM: #94`），理由「认领归 `t_26084082`（running）」—— 而 `t_26084082` 是实现卡（`脚本化流程预检：card-audit + handoff-check`），只是首行自认。代价：`merge-owner-audit t_26084082` 由 CLAIMED 变 UNOWNED（卡面现场记录：qa run 888 实测 rc=1；本轮未复跑，现场已被后续重建覆盖），闸门反过来要求重建收口卡（`t_bf672fa2` 人工重建、`t_f6465408` 重认领）。修后判定见 `t_d356f6fe`：重建现场副本 dry-run 输出 `nothing to do`（旧判据同副本上会 archive 1 张）、两张合并语义卡时只出 `需人判`。

---

### 条文里引用的机器实装（谁落地）
- `pr-claim-audit` ← `t_dd98c031`；`pr-review-head-audit` ← `t_b274b178`；`merge_guard` 双闸门 ← `t_98ce4cf7`（前两张 done 后开工）。playbook 里请按这三个卡号写，别写「待定」。

### 角色分工指针（2026-10-31 · t_9553f476 · 理由：合并权归 repo、验收归 qa 两条决策只写进 bot 章程，playbook 不复制条文，只留指向）

本节**只给指针**，条文以被指文件为准；本文件其余条文的判据一字不动。

- **合并权归 repo bot** → `~/.hermes/profiles/pm/SOUL.md` 的「#### R15-B · 合并权归 repo bot」节（合并类卡 `assignee=repo`、`merged-by` 必须是 `repo/run<NNN>`、pm 不再合并任何 PR）；执行侧 → `~/.hermes/profiles/repo/SOUL.md`。
- **验收归 qa** → `~/.hermes/profiles/qa/SOUL.md` 的「## 验收职责 · 唯一 PASS 权」节 + `~/.hermes/profiles/engineer/SOUL.md` 交卡段「现实补丁（2026-10-31）」+ `~/.hermes/profiles/pm/SOUL.md` 流程第 6 条的现实补丁行。
- **两条内核闸门的位置**（`/home/liu/.hermes/hermes-agent/hermes_cli/kanban_db.py`）：
  - 合并类卡 assignee：`MergeAssigneeError` / `_validate_merge_assignee()`，CLI 映射在 `hermes_cli/kanban.py:383`（**随卡 `t_bf0545d1` 落地**）；
  - 验收标准段签署：`_QA_SPEC_HEADING` / `_QA_SPEC_SIGN_RE` / `_validate_qa_spec()`（**随卡 `t_9cfd9d8d` 落地**）；
  - 完成闸门：`complete_task()` 的 `QaCompletionError` + `qa_completion_override` 审计事件（**随卡 `t_90468e1d` 落地**）。
  - 2026-10-31 实测：三条里只有第一条的符号已出现在 live 内核源码，三张卡均未通过验收 —— 巡检按「未合入」处理，别照未合入的 rc 开罚单。
- **冲突处理**：本文件 R5 / R6 / R7 / R9 / R12 / R15 里凡以「pm 自己合并」「pm 夜间自合并」为前提的执行句，自 2026-10-31 起执行者改为 repo（判据不变）；原文保留留痕，改条文本身须另建卡、归 ops。

## 巡检口径（2026-10-28 调整：频率 2 小时 → 6 小时，阈值同比放大）

- **频率**：每日四个时段（cron `巡检心跳-四时段`，job `3ddd9ce09e3b`，`10 1,7,12,18 * * *`，即 01:10 / 07:10 / 12:10 / 18:10），外加每日一次的「每日巡检验复」（cron `每日巡检验复`，job `c26d2d063a9c`，`40 7 * * *`）。原文「巡检心跳-6h，`every 360m`」与「每日 09:00，`0 9 * * *`」已过期，2026-10-28 七轮按 `hermes -p ops cron list` + `/home/liu/.hermes/profiles/ops/cron/jobs.json` 双回读对齐。两次巡检之间发生的异常只在下一次巡检被看到，所以下面的阈值一律按 6 小时口径取，**不许再用 2 小时时的数**。
- **阈值表**（判据字段全部来自 `tasks` / `task_runs` / `task_events`）：

| 异常 | 判据 | 阈值 |
| --- | --- | --- |
| 卡死 | `status='in_progress'` 且 `last_heartbeat_at` 距今 | > 20 分钟（不变） |
| 跑超时 | `status='in_progress'` 且 `started_at` 距今 | > **3 ×** `max_runtime_seconds`（engineer 默认 5400 → 16200 秒 / 4.5 小时） |
| 跑挂 | `consecutive_failures >= 2`，或最近一条 `task_runs.outcome` ∈ crashed / timed_out / spawn_failed / failed | 不变 |
| 卡住 | `status='blocked'` 且最近一条 `task_events.kind='blocked'` 距今 | > **6 小时**（原 2 小时） |
| 父卡可关（R5） | `status in ('scheduled','ready')` 且卡内自述子卡全部 `done` | 发现即代关，不设等待期 |
| 派不出去（守卫挡派） | `status in ('ready','review','scheduled')` 且同一卡最近 30 分钟内 `task_events.kind='respawn_guarded'` | **≥3 条**（dispatcher 每 60s 一拍）即处置，不设等待期 |

- **`respawn_guarded` 挡派的处置**（2026-10-29 教训 `t_f27fbe62`）：卡停在 `ready`/`scheduled` **不等于**「没人派」—— dispatcher 可能每一拍都在尝试、又被自身守卫放弃，`unblock` / `promote` / `schedule` 都**不**解除守卫。先读 `payload.reason`，按 reason 分头处置：
  - `active_pr` —— 24 小时窗口内评论里有 PR URL，且其后没有 handoff 事件（只认 `assigned` 改到**不同** profile / `changes_requested` / `review_reopened`；源码 `hermes_cli/kanban_db_dispatch.py` 第 1525–1646 行，`_RESPAWN_GUARD_PR_WINDOW = 86400`）。实现已完成待验收 → `request-review --reviewer <reviewer>`（review 车道跳过该守卫）；需要返工 → `request-changes`。**不许**等 24h 窗口自然过期，也**不许**反复 `unblock` 装作在处置。
  - `rate_limit_cooldown` / `infrastructure_cooldown` —— 等冷却，不在卡上追问执行者「为什么不动」。
  - `blocker_auth` —— 凭据/配额，写卡挂 `blocked` 交人。

- **一条能跑的检查**：`python3 /home/liu/.hermes/profiles/ops/workspace/check_board_anomalies.py`（退出码 0 = 全清，1 = 有异常并逐条列出卡 id）。阈值改动后已实测：`scheduled` 父卡回放到事故现场能报 `PARENT-CLOSABLE`；`in_progress` 2 小时不报、5 小时报；`blocked` 3 小时不报、7 小时报。
- **执行者类证据（R8 第 4 条）由机器定时判，不由巡检 agent 判**：cron `18124c3e3044`，`20 1,7,12,18 * * *`（四个巡检时段各晚 10 分钟），`no_agent`，脚本 `/home/liu/.hermes/profiles/paimeng/scripts/pm_touched_watch.py` → 调 `pm_touched_code.py` 全板扫描。CLEAN 无输出；首次/内容变化才推 feishu；同一告警满 24h 复报一次；rc=2（查不了）立即推 —— **「查不了」不等于「没问题」**。巡检 agent 不必再跑这条命令，只在要写「pm 写代码」类结论时引用它的输出。
- **`evidence/*` 分支是证据分支，不是在途卡分支**：巡检看到长期存在的 `evidence/<单段名>` 分支（含跨很多天）**不算卡住、不建卡追问 pm**；判据是 `python -m team_board.cli branch-audit` 输出里的「证据分支（N 条…）」段（程序判定，不靠人认名字）。若证据分支还叫 `wt/*`（旧形态），按 R11 ⑤ 原样保留、只在有 CI 红时按 `t_59c0eae1` 线的口径处理，不另开卡。
- **合并后源分支抽检（R12，2026-10-29 起）**：每次巡检对**本周期内合并的 PR** 各跑一次 `git -C /home/liu/project/team-board ls-remote --exit-code --refs origin refs/heads/<head 分支>; echo rc=$?`：rc=0 = 分支仍在（对应用户卡上留一条 comment 起头 `r12: <分支> 合并后仍在远端`，写明 PR 号与卡号）；rc=2 = 已删（正常）；其它 rc 记为「无法核验」并写明原因。**只报不删** —— 清理归卡主，ops 不批量删历史分支（`wt/t_589cc33a` 与别人的分支永远不删）。
- 无异常就什么都不留，不出「一切正常」的日报。

### 巡检口径补充 · 「配置声明 vs 运行态」对账 + 「不可见作业面」（2026-10-29 十一轮，案例 `t_b43ab8a5` → 本卡 `t_0c70c181`）

本小节只**加判据**：不改上面任何阈值、不改规则区任何原文与 R 区编号（编号对齐归 `t_dfdbf806`）。

**为什么有这一节**：2026-10-29 13:59–18:18 派工停摆 4h13m。`~/.hermes/config.yaml` 在 18:03 已被改回 `dispatch_interval_seconds: 60`，但网关进程仍按 13:59 启动时读到的 `interval=86400.0s` 跑 —— **配置改了、进程没吃**，板上四小时没有一张卡被派，直到 18:10 巡检靠人撞上。

**一处必须留痕的事实纠正**：那次 18:03 的恢复作业**不是不存在**，它**存在、跑了、静默失败** —— 在 **OS 系统 crontab**（脚本 `/home/liu/.hermes/profiles/paimeng/scripts/resume_dispatch_1803.sh`，日志 `/tmp/resume_dispatch_1803.log`，跑完自摘出 crontab）。`hermes cron list` 查不到它，是**查询面选错**，不是「作业不存在」；把这条写成「写在注释里的恢复计划不算数」会把方向带偏到「去板上写卡留痕」，真坑（作业失败 + rc 被吞 + 4h 无告警）原样留着。**任何「某作业不存在」的结论，先看 `/tmp/*dispatch*.log` 的 mtime 与 `rc=` 行。**

#### A · 配置声明 vs 运行态对账（每次巡检必跑；`MISMATCH` 立即报，不等 6 小时阈值）

```bash
CFG=$(grep -E '^\s*dispatch_interval_seconds:' /home/liu/.hermes/config.yaml | awk '{print $2}')
RUN=$(grep "embedded in gateway" /home/liu/.hermes/logs/gateway.log | tail -1 | sed -E 's/.*interval=([0-9.]+)s.*/\1/')
echo "config=$CFG running=$RUN"; [ "$CFG" = "${RUN%.0}" ] && echo MATCH || echo MISMATCH
```

**这条的 rc 不可信**：`[ … ] && echo MATCH || echo MISMATCH` 的退出码来自 `echo`，**两种结果都是 rc=0** —— 与本卡要治的是同一类病（rc 被吞）。要拿到 rc 的判定，用下面这条（`MISMATCH` 时 rc=1，可直接当门禁用）：

```bash
CFG=$(grep -E '^\s*dispatch_interval_seconds:' /home/liu/.hermes/config.yaml | awk '{print $2}')
RUN=$(grep "embedded in gateway" /home/liu/.hermes/logs/gateway.log | tail -1 | sed -E 's/.*interval=([0-9.]+)s.*/\1/')
echo "config=$CFG running=$RUN"; if [ "$CFG" = "${RUN%.0}" ]; then echo MATCH; else echo MISMATCH; exit 1; fi
```

判据：`MISMATCH` = **立即报异常**（写卡给 pm；重启网关 / 改 config 都交人，ops 不亲自做），含义是「配置改了但进程没吃」，派工 / 评审派发会在无人察觉的情况下停摆。判据看输出，不看上面那条的 rc。

反例（本机真日志，非构造）：`gateway.log` 里 `2026-10-29 13:59:43 … (interval=86400.0s)`，而 `config.yaml` mtime 已是 `2026-10-29 18:03:01`（值 60）；18:03 当场跑本条 → `config=60 running=86400.0` → `MISMATCH`，比 18:13 靠人撞见早发现。

#### B · 不可见作业面：`hermes cron list` 之外必须同时看三处

`hermes cron list` 只覆盖 Hermes 自己的 job；一次性「恢复 / 暂停派工」脚本走的是 **OS 系统 crontab**，且多数跑完自摘出 crontab —— **两条命令事后都查不到 ≠ 从未存在**。巡检必须同时看：

- `crontab -l`（系统 crontab，`crontab -l | tail -5; echo rc=$?`）；
- `/tmp/*dispatch*.log`、`/tmp/resume_dispatch_*.log`、`/tmp/pause_dispatch_*.log` 这类一次性脚本日志的**最后修改时间**与其中的 `rc=` 行（`ls -l --time-style=full-iso /tmp/*dispatch*.log`）；
- 判定：同一份日志里出现 `command not found` / `No such file or directory` / `MISMATCH`，而同日又有一行 `rc=0` **并存** = **rc 被吞**（典型成因 `echo "$(date …) rc=$?"`：`$(date)` 先把 `$?` 重置成 0），按异常报；
- 必读失败 marker `/tmp/dispatch-resume.FAILED`（paimeng 卡 `t_244c0f44` 会让恢复脚本失败时写它）：**marker 存在 = 恢复作业失败，直接报**；`ls` rc=2（不存在）= 本轮没失败，属正常。

#### C · 不新增定时任务

A / B 两条并进现有 6 小时巡检（`10 1,7,12,18 * * *`）：**不加新 cron、不改 `dispatch_interval_seconds`**（老板在意成本，2026-10-29 已定）。

## 观察区（候选规则，等真实案例升级）

**明日待办（2026-10-29 收敛夜留档，按老板收敛令不开新卡）**：① R19 ④ 机器校验未实装，需一条命令解析所有团队 profile 的 cron 时点并判是否落高峰（工作日 09:00–12:00 / 14:00–18:00 命中即报红）；扫描范围按 R19「适用范围」只含 paimeng / chief / pm / engineer / ops / qa / hatch，不含 `wsl-gp-agent` / `wsl-uumit-0706`。② R19 条文与本复核注**在板上无对应卡**（`sqlite3` 只读查：标题/正文提到 `R19` 的卡 0 张；playbook mtime 2026-10-29 23:44 时段无新卡），按本文件第 3 行「所有规则变更必须同时写一张 `assignee=pm` 的卡」属留痕缺口 —— 明早补一张补录卡（只留痕、不派生工作）。明早处理。

| 候选规则 | 来源 | 触发它的案例 |
| --- | --- | --- |
| 跨文件引用规则时写节名（`## 巡检口径`），不写行号 | 2026-10-28 三轮插入 R6/R7 后 playbook 行号整体右移 | 有轻案例、未升级：R6/R7 插入后 `pm/SOUL.md:96` 仍写「playbook.md 第 46–60 行」（实际已变 77–92），由 pm 在 `t_d558ed1e` 自查发现，未花卡。**再犯一次（有人按旧行号找错段落）就升为规则区** |
| 卡进入 doing 后三要素冻结，变更开新卡 | 设计阶段（agent-team-v1.1 §4.2） | 待补：第一次出现"卡被改、engineer 按旧版执行" |
| 同一错误重试 ≤3 次、同错 2 次即升级 | 设计阶段（§8） | 待补：第一次出现"重试掩盖真实故障" |
| PM 不写生产代码（候选：实现类卡——交付代码/PR——的 assignee 只能是 engineer，qa 只做复跑） | 官方指南（`my EM is not supposed to code`）；`t_e0866bd4` 复核 | **仍无案例**：2026-10-28 报的「pm 写代码」经 R8 口径复核不成立（`who_did_it.py t_f43fa954` → engineer；pm 三个 run 窗口内该卡提交 0 条，主 checkout tracked 改动 0 处）。ops 卡 `t_a5c2f894` 按 R8 第 4 条回放 4 张卡（`t_f43fa954`/`t_a8c95560`/`t_e32041c1`/`t_385db65f`）：`pm_touched_code.py` rc=0，alerts=0 / infos=1 / note=1 —— 候选案例 `t_e32041c1` 的 pm 窗口内 0 条提交；`t_385db65f` 的 pm run 51 窗口内只有 `README.md`（`8dfa2c4`，不在生产路径）→ 仍无案例。再有人报，先跑 `who_did_it.py` + `pm_touched_code.py`；拿不出 pm run 的 tracked 改动凭证就不许建卡。**升级触发器（2026-10-28 hatch 定）**：只有 `pm_touched_code.py` 出现 `alerts≥1`（pm run 窗口内、提交信息带本卡号的 commit 命中 `src/|tests/|.github/|pyproject.toml`）才升级；`infos` / `notes` 一律不算。另：卡 `t_d3e409ff` 卡面「pm 的 run 里 6 条 commit」按 committer 时间不成立（33–40 秒后落在 run37 之后），矫正记录见该卡 **comment #132**，引用时以 comment 为准 |
| 验收探针/证据不落真仓库工作区（落卡附件） | `t_e0866bd4` 复核实测 | 轻案例、未升级：主 checkout `/home/liu/project/team-board` 现有 **62 个未跟踪文件**，其中 `evidence/pm-t_68b0a37a-*.log` 5 个由 pm run 98（1793201152–1793201361）写出；tracked 改动 0 处、未造成失败。若再出现「因工作区脏导致 checkout/合并/稽核误报」即升级 |

升级方式：案例出现 → 在这一行补上案例链接（卡 id）→ 移入规则区 → 写卡广播给 pm。
2026-10-28 本轮：三条问题（父卡早关 / 验收只跑单一路径 / 手算数量断言）全部有真实案例，全部进规则区，观察区不新增。
2026-10-28 二轮：R5（父卡关不掉）有真实案例 `t_e32041c1`，进规则区；同时按老板拍板把巡检频率 2h→6h、阈值放大（blocked 6h / 超时 3×），写进「巡检口径」，观察区同样不新增。
2026-10-28 三轮：R6（合并前置 + 评审不要求 approve）与 R7（风险分级自动合并）进规则区，案例取自 `alsj213/team-board` 的 PR #2/#3/#4（`gh` 实跑核对：Review 全 `COMMENTED`、PR #2/#3 `no checks reported` rc=1、PR #4 `pytest (3.12)` pass rc=0）；R7 明确标注零实跑，观察区不新增。
2026-10-28 四轮：按老板指令「改」把 R7 由「四条件单一分级」整段替换为 A/B/C 三档 —— A 档四条件自合、B 档五条件复核后自合（`src/` 常规改动落 B 档、不再上报）、C 档六条任一即报批；只改规则区 R7 段落，观察区不新增。
2026-10-28 五轮：R8（执行者判据）进规则区，案例 `t_e0866bd4`（误读 `tasks.assignee`，同日第二次）；观察区「PM 不写代码」行改写为候选条文（仍无案例）+ 新增「验收证据不落真仓库工作区」候选行。同轮新增可复跑检查 `who_did_it.py`。
2026-10-28 六轮：按 hatch 卡 `t_a5c2f894` 收口 R8 —— ① 第 1 条 ③ 项措辞由「worker 自写」改为「内核按 current run 的 `task_runs.profile` 写入」（源码实读 `hermes_cli/kanban_db.py:3353–3361`），并补 `who_did_it.py` 两个回落值「不作硬结论」的口径；② 第 4 条补机器检查 `pm_touched_code.py`（回放 4 张卡 rc=0；合成仓库自测 6 例覆盖 rc 0/1/2 三档与现役窗口路径）。观察区「PM 不写代码」候选性质不变（仍无案例），只补回放证据。
2026-10-28 七轮：按 hatch 卡 `t_aed80ce3` 拍板落四处 —— ① **执行者证据做成独立机器定时判**：`no_agent` 脚本 cron `18124c3e3044`（`20 1,7,12,18 * * *`，注册在 paimeng 因其唯一配了飞书通道；检查内容仍归 ops），**不并入** `check_board_anomalies.py`（失败模式会耦合：一个只吃 SQLite、一个还要读 git；rc 0/1/2 语义不同；且巡检是 LLM job，确定性检查不该赌 LLM 那轮记得跑）。「巡检口径」按此补一条，巡检 agent 不再亲跑该命令。② 观察区「PM 不写生产代码」行补**升级触发器**：仅 `alerts≥1` 才升级，`infos`/`notes` 不算 —— 候选**仍不升级进规则区**（两轮候选卡 `t_e32041c1`/`t_385db65f` 均未命中；无案例就升级等于自己违反第 4 行硬约束，且会让下一个读 playbook 的人以为违规已发生过，正是 `t_e0866bd4` 的同一误判机制）。③ 修 `pm_touched_code.py` 缺陷：`--repo` 落在上层仓库（本机 `/home/liu` 自身是 git 仓库）时 git 向上找到 `.git`，脚本静默改扫上层仓库并报 `CLEAN` rc=0（最坏失效：检查死了、读起来是「没问题」）→ 补 toplevel 断言（`rev-parse --show-toplevel` 的 realpath 必须等于 `--repo` 的 realpath，否则 rc=2 并打出两边路径）。④ 本段「频率」按 cron list + jobs.json 双回读对齐为「巡检心跳-四时段 `10 1,7,12,18 * * *`」+「每日巡检验复 `40 7 * * *`」。
2026-10-29 九轮：按 `t_63fd3172` 事故（`t_f27fbe62` 停在 `ready` 31 分钟、`respawn_guarded` 连发 11 条）在「巡检口径」新增「派不出去（守卫挡派）」判据 + 按 `reason` 分头的处置顺序，教训记录补一条；`check_board_anomalies.py` 同步加 `GUARDED-STALL`（回放事故现场 1/1 命中，现网 0 误报）。**观察区不新增、规则区不动**（无新编号 → 不涉及 hatch 章程落盘）。
2026-10-29 十轮：按老板原话「需要区分」新增 **R14**（返工分因 spec / impl / env + 两条考核口径 + 一条机器校验命令；R12 / R13 已被 pm 并行组 `t_2df659ad` / `t_8f1ef2f4` 预留，故顺延取 R14），案例 `t_df640caa`；观察区不新增候选规则，只留一句口径提醒 —— **历史返工未标因，不可用于考核**：R14 生效时刻（epoch 1793251339 / 2026-10-29 13:22 CST）之前的 3 条 `changes_requested`（`t_f43fa954` ×2、`t_df640caa` ×1）全部无 `REWORK-CAUSE`，一律 `UNTAGGED`，不回溯补标、不进任何考核分子与分母。


2026-10-29 十一轮：按 `t_b43ab8a5`（派工停摆 4h13m：「config 已改回 60、进程没重启」+ 18:03 恢复作业跑了并静默失败）在「巡检口径」新增小节「配置声明 vs 运行态对账 + 不可见作业面」（判据 A `config` vs `interval=` MISMATCH 立即报、并附 rc 版判定式；判据 B 必看 `crontab -l` / `/tmp/*dispatch*.log` mtime 与 `rc=` 行 / `/tmp/dispatch-resume.FAILED` marker；判据 C 不新增定时任务）。**观察区不新增候选、规则区不动**（无新 R 编号 → 不涉及 hatch 章程落盘）。
2026-10-29 十二轮：老板三裁（收敛夜，不建卡）—— ① R19 加「适用范围」段：只约束团队 profile（paimeng / chief / pm / engineer / ops / qa / hatch），`wsl-gp-agent` / `wsl-uumit-0706` 不纳入、不许改其 cron；② R16 从三份 SOUL.md（三份逐字同一份，1754 字节 / sha1 592ce8da78df）整段搬回 playbook R15 与 R17 之间，属补漏不属派生新工作；③ R19 ④ 机器校验未实装 → 记入上方「明日待办」，今晚不开卡。R15→R16→R17→R18→R19 编号连续已 grep 验证；章程文件零改动（R19 不在任何 SOUL 中，R16 是 SOUL→playbook 追平方向，不涉及 hatch 落盘）。


## 教训记录（postmortem）

格式固定三行，一条一个二级标题：

```
### <日期> · <卡片 id 或事件>
- 触发场景：
- 根因：卡写错了 / worker 理解错了 / 规则缺了
- 规则动作：加|改|删 —— <规则原文>
```

### 2026-10-28 · t_eeefcd01（QA 判 ISSUES 未处置）
- 触发场景：QA 卡 02:00:36 以 verdict=ISSUES 收尾（自身 done），此后全板无新卡、无 comment；pm 在 01:56:26 已先验收通过 E5，双方都没人把 ISSUES 转成下游动作。
- 根因：规则缺了 —— 没有规定「验证结论不通过时，谁、在多长时间内、用什么东西闭合」，结论只存在 done 卡的 summary 字段里。
- 规则动作：加 —— R1 · 验证结论必须在下游落卡闭合：QA / reviewer 给出 ISSUES 或 changes_requested 的卡，24 小时内必须被下游卡承接（开返工卡派给 engineer，或同卡写明「不修 + 理由」）；两样都没有 = 未处置，ops 建卡追问 pm。

### 2026-10-28 · t_c4f1a484 / t_5a465efb（父卡早于子卡关闭）
- 触发场景：pm 拆完卡当轮就把需求卡置 done——`t_c4f1a484` 00:56:20 done，子卡 E2 `t_631b3f59` 01:04:53 才 done；`t_5a465efb` 01:40:59 done，而 `t_c07fc14f` 01:56:26 才 done、`t_eeefcd01` 02:00:36 才跑完，且关闭那一刻 QA 还没启动（01:57:21）。
- 根因：规则缺了 —— 章程只写了「pm 拆卡派活、按目标验收」，从没定义父卡的关闭条件，于是 pm 把「我的活干完了」等同于「需求完成了」。
- 规则动作：加 —— R2 · 需求卡只在子卡全部闭合后关闭：pm 只有在全部子卡 done 且验收结论 PASS 后才允许关闭需求卡；拆完卡不等于需求完成，子卡未闭合期间父卡保持 in_progress（等外部输入时保持 blocked）。

### 2026-10-28 · t_927ebb83 / t_3ff92267（P1 穿过 pm 两轮验收）
- 触发场景：E3、E4 两张卡的 pm 验收（run 27 / run 29）都把「`--now` 为 int」标 PASS，实际只跑了 argparse 级坏参数；QA `t_eeefcd01` 换成 `--now 1793160000000`（13 位毫秒）立刻抓到裸 ValueError traceback、rc1 应为 rc2。
- 根因：规则缺了 —— 没有规定验收映射表的每条 PASS 必须对应一条实跑命令与退出码，「没测」和「测过通过」在卡上是同一个 PASS。
- 规则动作：加 —— R3 · 验收映射表里每条 PASS 都要有本机实跑命令：每条 PASS 紧跟本机亲跑命令 + 实际输出 + 退出码，没跑过的只能写 NOT TESTED；每个退出码与每个新参数的取值范围至少各配一条越界/非法输入。

### 2026-10-28 · t_bbdb794a（D2 清单 18 vs 基线 19）
- 触发场景：pm 把 19 个待入库文件手分成 6 条提交（8+2+1+1+3+3=18），漏掉 `tests/integration/__init__.py`；engineer 按 D1/D2 照抄提交后 `git status` 仍显示 `?? tests/integration/__init__.py`、`git diff --name-status d8f92ac HEAD | wc -l` = 18 而契约要 19，遂停手交回 pm。
- 根因：卡写错了 —— 卡里「共 6 条提交」「18/19 个文件」全是手算数字，没有任何一条命令把分组清单与原清单对账；漏项是手算的必然产物，不是笔误。
- 规则动作：加 —— R4 · 分组执行类卡必须附清单覆盖性对账命令：派卡前实跑清单覆盖性核对并贴命令/输出/退出码（清单 `wc -l` == 基线 `wc -l`、两侧 `diff` 为空），卡里的「共 N 个 / 恰好 N 行」必须由该命令产生，不许手算。

### 2026-10-28 · t_e32041c1（子卡全 PASS 后父卡关不掉，滞留 823 秒）
- 触发场景：四张串行子卡 12:59–13:06（1793163544 / 1793163639 / 1793163820 / 1793163985）全部 done 且 PASS，pm 的 worker 在 13:05 连发三贴报「两条关闭路径都被环境拒绝」（CLI mutate 被 `delegate_task child contexts cannot mutate Kanban tasks via the CLI` 拦；`kanban_complete` 被 `worker is scoped to task t_90ee3bb7; refusing to mutate t_e32041c1` 拦），父卡自 12:56:55（1793163415）起停在 `scheduled`，直到 paimeng 13:10:38 unblock、13:10:42 complete 才闭合。
- 根因：规则缺了 —— R2 只约束「什么时候才能关父卡」，从没写「谁来关」。worker 进程按设计无权 mutate 作用域外的卡，于是「子卡由 worker 收尾 + 父卡需人关」这个组合必然卡死；pm 还想再建一张 pm 卡去关，但 dispatcher 拉起的 pm worker 同样是 delegated child，会撞同一条护栏（白跑一张卡）。
- 规则动作：加 —— R5 · 父卡由 ops 巡检代关：子卡全部 done 而父卡仍停在 scheduled/ready 时，由 ops 在普通 shell 里 `unblock` + `complete`（`--board` 放子命令之前），并在卡上留一行「由 ops 代关，原因：worker 作用域限制」；ops 只做状态流转，不改 verdict、不改验收语义。判据不许用 `task_links`（该父卡边数为 0），只认父卡 body/comment 自述子卡清单 + 逐张 `tasks.status`。

### 2026-10-28 · PR #2 / #3 / #4（「必须 approve」口径把三张 PR 全堵死）
- 触发场景：`alsj213/team-board` 的 PR #2（5 文件，merge `69f8d9e`，05:04:50Z）、PR #3（3 文件，merge `0b50ab6`，11:39:57Z）、PR #4（1 文件，merge `f2ee664`，11:39:43Z）—— `gh pr view --json reviews` 实测三张的 Review 状态**全部 `COMMENTED`**，评审人 login 与作者同为 `alsj213`；按「合并前必须拿到 approve」的口径，这三张一张都合不了，只能每张等人工放行、由人点 merge。
- 根因：规则缺了 —— 章程里从没写「评审通过」在本仓以什么为准、approve 由谁给；单账号下平台禁止自审批，等于把合并门槛设在一个**永远不可能成立**的状态上，规则落地成「每条 PR 都阻塞到有人手动放行」。
- 规则动作：加 —— R6 · 合并 PR 的三条前置 + 评审只认 Review 记录，不要求 approve：① `gh pr checks` rc=0 全 pass（`no checks reported` 标 NO_CHECKS，不许当 pass）② 卡上有 R3 证据（本机命令 + 退出码）③ head 分支 `wt/<卡号>`；评审用 `gh pr review --comment` 即可，不要求 approve。

### 2026-10-28 · PR #2 / #3 / #4 逐张等人工放行（吞吐卡在一个人身上）
- 触发场景：三张 PR 的合并全部发生在「老板点头之后」，板上没有任何一张走过「按风险自动放行」的路径；用 R7 的四条口径回放，三张都不合格（PR #2 五文件 >3 且 `no checks reported`、PR #3 改 `src/team_board/db.py` 且 `no checks reported`、PR #4 改 `.github/workflows/test.yml` 且彼时无 check）。合并由同一账号 `alsj213` 执行（全队共用），板上分不出人。
- 根因：规则缺了 —— 没有任何条款说明「哪类 PR 不必经过老板」，于是默认 100% 审批；老板离线时交付链整段停摆，且每个 PR 的等待时间都不留在板上（不是卡的异常，巡检看不见）。
- 规则动作：加 —— R7 · 风险分级：同时满足 ① CI 全绿 rc=0 ② 只改 tests/docs/README ③ 文件数 ≤3 ④ 无新依赖 → pm 可直接合并、不必惊动老板；任一不满足按影响面升级。**该规则的「可直接合并」分支至今零实跑**，首次实用时 pm 必须贴四条实测输出，ops 逐条复核。（该条已于 2026-10-28 按老板指令「改」修订为 A/B/C 三档，现行文本见 R7。）

### 2026-10-28 · R7 旧版（碰 `src/` 一律升级上报）
- 触发场景：R7 三轮写入时把「碰 `src/`」列进升级条件，而本仓几乎每张 PR 都碰 `src/`（PR #2/#3/#4 回放：两张碰 src 或 CI 配置），分级退化成 100% 上报，等同没有分级；老板 2026-10-28 直接以「改」下指令修订。
- 根因：卡写错了 —— 阈值按目录敏感度（改哪个目录）而不是改动风险（改了多大、跑没跑测试）设计，定规则时没有拿仓库真实 diff 分布回放一遍。
- 规则动作：改 —— R7 · 风险分级（2026-10-28 修订：改为三档，src/ 不再一律上报老板）：A 档四条同时满足（CI 全绿 / 只碰 tests、docs、README / 文件数 ≤3 / 无新依赖）→ pm 自合；B 档五条同时满足（CI 全绿 / 本地全量 rc=0 / 双问无 P1P2 / 文件数 ≤10 / 不碰 .github、依赖、迁移删库、凭据权限）→ 复核后自合，含 `src/` 常规改动；C 档六条任一 → 必须报老板点头。

### 2026-10-28 · t_e0866bd4（拿 `tasks.assignee` 当作者，差点把误判写成规则）
- 触发场景：`t_e0866bd4`（复盘卡，`created_by=user`）要求就 `t_f43fa954`「实现卡派给 pm」做 postmortem，并指定根因「pm 自己拆卡时把实现卡派给了自己」。核实后前提不成立：该卡 `created` 事件 payload `assignee=engineer`，run 76/89/96 是 engineer 的实现 run、86/95/99 是 pm 的验收 run，三次 `review_requested` 均写 `implementer=engineer, reviewer=pm`，pm 三个 run 窗口内该卡提交 0 条。附带的「卡龄 58 分钟正在写码」时段（1793199988）实际在 pm run 95 内部，是验收不是写码。
- 根因：规则缺了 —— playbook 的巡检口径只定义了状态类判据（`status` / `last_heartbeat_at` / `task_runs.outcome`），从没写「执行者」判据；`tasks.assignee` 会被 `kanban_request_review(reviewer=pm)` 覆盖成 reviewer，于是任何按 assignee 读板的人都会把验收者读成实现者（同日第二次：观察区 E1–E5 那次已记过）。
- 规则动作：加 —— R8 · 判定「谁写的代码」只认 run 身份与角色标签，不认 `tasks.assignee`：凭证必须来自 `created` 事件 payload.assignee / `task_runs.profile` run 序列 / `review_requested` payload 的 implementer·reviewer 三条中至少两条；`tasks.assignee` 与提交 author、提交时间窗一律不作凭据；「pm 写生产代码」只有 pm run 产生 tracked 改动才成立；拿不出凭证的指控按 R3 退回。落地检查：`who_did_it.py <卡号>`（rc0 = 归因明确）。同轮把观察区「PM 不写代码」改成仍无案例的候选条文，并新增「验收证据不落真仓库工作区」候选行。

### 2026-10-29 · R9 夜间自治条文入区（老板直接指令、零失败案例）
- 触发场景：老板睡前指令经 pm 转达，要求把 Cursor 官方 PR Routing & Approval 的夜间自治机制写成 R9 并逐字写进规则区末尾。该条文在板与仓库里**没有任何真实失败案例**——依据只有老板原话 + 外部产品参照，而 playbook 第 4 行硬约束是「一条规则必须能指向至少一个真实失败案例，没有案例的先放观察区」。
- 根因：规则缺了 —— 第 4 行硬约束没给「老板直接下指令」留通道：既没写这种条文能否破例入规则区（破例时怎么标注），也没写「零实跑」的风险由谁在首次实用时兜住。R7 旧版当时是零实跑入区，但那是现场临时判断，没写成口径，于是每次都要重新拍一遍。
- 规则动作：加 —— R9（原文逐字入规则区，24 行逐字比对通过，R1–R9 连续）；零实跑沿用 R7 旧版先例：首次实用时 pm 必须贴五条判据各自的实测输出 + 退出码，ops 逐条复核，卡上标 `B·night` 与 merge commit 一并留痕。是否把「老板指令条文的入区口径（零实跑可入区 + 首用必复核）」补写进第 4 行，交 hatch 裁 —— 本轮不改第 4 行、不动任何章程文件，只留这条复盘备查。

### 2026-10-29 · t_ad16e55a（https 通道断，两张 engineer 卡卡在 push）
- 触发场景：`t_f27fbe62`（CI push 触发改动）、`t_d5149e95`（PR 侧必红证据）两张 engineer 卡代码写完却推不上去，卡面还写着「不许换 SSH」；同期直连 github.com:443 SNI 阻断（rc=28）、代理 7892 上游节点死（502 / TLS eof）、`git ls-remote` rc=128，而 SSH/443 一直可用。
- 根因：规则缺了 —— 默认通道是 https 且断在代理节点；没有写明 SSH 是一号通道、没有写故障时的固定顺序。附带一处卡面事实错：判据写「`ssh -T git@github.com` rc=0」，实测正常态 rc=1（GitHub 无 shell access）。
- 规则动作：加 —— R10 · git 推送通道：SSH 优先，https 留备选，故障按固定顺序自处（判据改为看输出 `Hi alsj213!`，不看 rc）。

### 2026-10-29 · t_f27fbe62（停在 ready 31 分钟「无人派」，实为 `active_pr` 守卫静默挡派）
- 触发场景：`t_f27fbe62` 在 12:39:53 被 chief 改 `completion_contract` 为 `local-only` 并 `unblock`，状态回到 `ready`、`assignee=engineer`、`claim_lock=NULL`；从 12:40:10 起每一拍都写一条 `respawn_guarded {"reason":"active_pr"}`（到 12:52 共 11 条），卡没有任何 run 被拉起，父卡 `t_59c0eae1` 因等它而一直停在 `scheduled`。12:52:18 `request-review --reviewer pm` 后 46 秒内被 review 车道认领（`claimed source_status=review`，run 139），链条恢复。
- 根因：规则缺了 —— 「巡检口径」只定义状态类阈值（`in_progress` 心跳 / 超时 / `blocked` 时长 / 父卡可关），没有「派不出去」这一类；于是 `status` 变回 `ready` 就被当成「已恢复」，没人验证它是否真的被重派。机制侧的事实：`check_respawn_guard()` 的 `active_pr` 只认 `assigned`(改到不同 profile) / `changes_requested` / `review_reopened` 三种 handoff 事件，`unblock` 不在其中，窗口 24 小时 —— 靠 unblock 处置的卡会静默空转一整天。
- 规则动作：改 —— 「巡检口径」新增「派不出去（守卫挡派）」判据（`respawn_guarded` 30 分钟内 ≥3 条即处置）与按 `reason` 分头的处置顺序（`active_pr` → `request-review` / `request-changes`，不许等 24h 窗口、不许反复 unblock）；`check_board_anomalies.py` 同步加 `GUARDED-STALL` 检测。不动规则区、不新增编号，故不涉及 hatch 章程落盘。

### 2026-10-29 · t_df640caa（返工记到执行者头上，实际错在写卡人）
- 触发场景：`t_df640caa`（规则 C 内核，一卡一分支）的 C-2 被 pm 打回（`changes_requested` event 2036，`run151 pm`）。pm 在评审正文里亲笔写「卡面 C-2 算法是我 pm 写错的，不是实现走样」；`who_did_it.py t_df640caa` rc=0 → `VERDICT: engineer`（`review_requested.implementer` 一致 = engineer）。按当时的返工率口径（被打回次数 ÷ 交付次数，`load_rework_counts` 与 `render_member` 均按 `tasks.assignee` 归属），这一次返工整笔记在 engineer 头上。
- 根因：规则缺了 —— 返工只有一个「被驳回次数」口径，playbook 从没定义返工要分因、也没定义 spec / impl / env 各自的考核分母，于是「谁的卡被打回」被默认读成「谁的错」；写卡错与实现错在板上是同一个数，考核必然冤枉执行者。
- 规则动作：加 —— R14 · 返工必须标因：spec / impl / env，考核口径一分为二（`REWORK-CAUSE` / `REWORK-OWNER` / `REWORK-EVIDENCE` 三行随 reason 落 `changes_requested` 事件、事件不可改即无法事后补标；一条命令 `rework_audit.sql` 出分因占比 + impl / spec 两条返工率；env 单列不考核；生效前 3 条历史返工一律 UNTAGGED、不回溯）。

### 2026-10-29 · t_b43ab8a5（结论「恢复作业不存在」实为作业跑了并静默失败；配置改了进程没吃）
- 触发场景：ops 18:10 巡检报「派工停摆 4h13m」，证据第 4 项把 config 注释里的「cron 已定 18:03」判为**作业不存在**（10 个 profile 逐个 `hermes cron list` 都没有），并据此提制度结论「写在注释里的恢复计划不算数」。pm 复核：作业**存在** —— 在 OS 系统 crontab（`resume_dispatch_1803.sh`），`/tmp/resume_dispatch_1803.log` 有实跑痕迹（18:03:01 start → 18:03:11 done），且是**跑了并静默失败**：裸 `hermes` 在 cron PATH 下 `rc=127`，`echo "$(date …) rc=$?"` 把 `$?` 吞成 `rc=0`，脚本跑完自摘出 crontab 所以事后 `crontab -l` 也查不到。同轮真相：config 18:03 已回到 60，网关进程仍按 13:59 读到的 `interval=86400.0s` 跑。
- 根因：规则缺了 —— 「巡检口径」的判据全在板上字段（`tasks` / `task_runs` / `task_events`）和 Hermes 自身 cron 里，从没写「配置声明 vs 运行态」对账，也没写「`hermes cron list` 之外还有一层 OS crontab + `/tmp` 一次性脚本日志」。缺这一层，三件事同时成盲区：配置改了进程没吃、系统 cron 作业失败、rc 被命令替换吞成 0；并且会把「两条命令都查不到」写成「从未存在」，把处置方向带偏成「别在注释里写计划」，真坑（作业失败 + rc 被吞 + 4h13m 无告警）原样留着。
- 规则动作：加 —— 「巡检口径」新增小节「配置声明 vs 运行态对账 + 不可见作业面」：A 判据 `config` vs `gateway.log` 最新 `interval=`，`MISMATCH` 立即报不等 6h，并**注明卡片原命令 rc 恒 0（rc 来自 echo）**，另给 rc=1 的判定式；B 判据必看 `crontab -l`、`/tmp/*dispatch*.log` 的 mtime 与 `rc=` 行（错误行与 `rc=0` 并存 = rc 被吞）、`/tmp/dispatch-resume.FAILED` marker（存在即报）；C 不新增定时任务。不动规则区、无新 R 编号，故不涉及 hatch 章程落盘。

### 2026-10-29 · t_b9e29152（规则条文里的命令没跟着工具改版走，照抄即 rc2）
- 触发场景：PR #30（合并提交 `d3e5268`）把 `merge_guard` 的合并模式改成认账四参数（`--card` / `--by` / `--subject` / `--body-file`）必填，而 R12 ① / R13 ② 那条「唯一允许命令」的原文仍是旧形态。实测旧文照抄：R12 ① → `错误: 合并命令缺少 --card …` rc=2；R13 ② → `错误: the following arguments are required: --branch` rc=2（连 `--branch` 都没有）。同一形态分布在 4 个文件 8 处（`pm` / `engineer` / `ops` 三份 `SOUL.md` 各 2 处 + `playbook.md` 2 处），R12 ② 又要求「合并卡正文必须把①这条命令原样抄进去」—— 条文自己把自己的唯一的合并通道堵死。
- 根因：规则缺了 —— 规则区只钉判据语义（`--delete-branch` / `--method merge`），从没要求「条文里的命令必须在本机实跑过一次再去写」，也没有「工具参数变了必须在同一批里把引用了它的条文一起改」这条。于是 R12/R13 落地时引用的还是工具升级前那一版命令，谁都没跑过它。
- 规则动作：改 —— 只补命令形态、不动任何判据：R12 ① 的命令块补 `--card <合并卡号>` / `--by <profile>/run<NNN>` / `--subject` / `--body-file`（并说明 `--card` 是**合并卡**号、body 由 `merge-record --body-out` 生成、四参数必填且与 `--verify-only` 互斥、照抄必须真能跑通），R13 ② 单行同步补全并补 `--branch`（argparse 必填）。不动 R12 其余 ①–⑤、R13 其余 ①③④⑤⑥⑦、R15 全段；不新增 R 编号。改动范围外的两处另开卡：三份 `SOUL.md` 那 6 处按「章程文字不归 ops 改、也不归被写的人改」转 hatch 卡 `t_c0b674f7`；`merge-record` 仍打印裸 `gh pr merge`（`src/team_board/cli.py:692–693` + `docs/merge-accountability.md:36` + R15 段两处「照抄它」）与 R12 ① 冲突，交 pm 判断卡 `t_439d06bf`。

### 2026-10-30 · t_96a7767b（worktree 型 workspace 去复用别的卡分支的 worktree → spawn_failed ×2，engineer 一秒活没干）
- 触发场景：pm 07:21 建「解冲突（engineer）：wt/t_dd98c031 rebase 到 origin/main」，卡面 `workspace_kind=worktree` + `workspace_path=/home/liu/project/tb-wt/t_dd98c031`（该路径是**另一张卡分支** `wt/t_dd98c031` 的 worktree）+ `branch_name` 空；`run289 spawn_failed`、`run292 gave_up`，`consecutive_failures=2`，error 逐字 `workspace: git worktree add failed for /home/liu/.worktrees/t_96a7767b on branch wt/t_96a7767b: fatal: Not a valid object name: 'HEAD'.` 该 worktree 本身健康（`wt/t_dd98c031` @ `115fb4e`，与 origin 同 sha）。
- 根因：卡写错了 —— 内核按 `branch_name`（缺省 `wt/<本卡号>`）判「这不是本卡分支的 worktree」而拒绝复用（防跨卡提交串味，行为正确），回退锚点落到 `$HOME`，而 `/home/liu` 是个**没有提交**的 git 仓库，`git worktree add … HEAD` 必然失败。写卡人选错了 workspace 型（应 `dir:<绝对路径>` 或 `scratch`），env 只是把错误放大成必然失败。
- 规则动作：加 —— R21 · 复用别人工作区里的 worktree：用 `--workspace dir:<绝对路径>`，不要用 `worktree` 型（判据：`workspace_kind` 与 `branch_name` 自洽才许派；同型未派发的 `t_1838d395` 已在巡检卡 `t_6ee962f6` 点名）。

### 2026-10-31 · t_d5f23eb3（写着「必须在普通 shell 执行」的活，派给了 ops 的 worker）
- 触发场景：PR #53 有两张未结卡顶格 `PR-CLAIM: #53`（`t_becc41cb [todo] repo` / `t_e31bf1ac [scheduled] pm`），`merge_guard` 认领闸门 rc2、收口链停摆；pm 建 `t_d5f23eb3` 派给 ops「作废 t_e31bf1ac 或把首行改 PR-REF」，并在卡面写明「必须在普通 shell 执行：跨卡 mutate 在 dispatcher 拉起的 worker 里会被 delegated-child 卫拦」。ops 实跑 `complete` / `unblock` / `edit` 三条 CLI 全部 rc=1（同一句 `kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI`），工具侧 `kanban_complete(task_id="t_e31bf1ac")` 回 `worker is scoped to task t_d5f23eb3; refusing to mutate t_e31bf1ac`，卡只能 blocked 转人。
- 根因：规则缺了 —— R5 只写「父卡由 ops 在**普通 shell** 里执行」，既没说清「ops 拿到卡就是 worker、它没有普通 shell」，也没把「跨卡 mutate 只能由非 worker 会话执行」立成通用条款；于是写卡人照 R5 的字面把跨卡动作派给了 worker，还把「做不到」当成了「ops 记得换个 shell」。同一原因在 R5 案例 `t_e32041c1` 就出现过一次（滞留 823 秒），属复发。
- 规则动作：加 + 改 —— 加 R22（跨卡状态流转只能由非 worker 会话执行；不许写成 `assignee=ops` / `assignee=pm` 的卡；`env -u` / 直写 sqlite / 排 cron 转手三种绕法明禁；`edit --body` 整段替换的坑一并写明）；改 R5（执行者从「ops 在普通 shell」改成「ops 巡检报、paimeng 在普通 shell / 交互会话执行」，标题同步改）。章程落盘转 hatch 卡（正文抄 R22 原文 + 案例 + 要落的 pm/ops 两份 SOUL）。

### 2026-11-01 · guard_health 自锁连红 73 轮（自检把自己的 rc 当判据 + 收件箱不接自检结论 + 15 分钟窗口在降频后不可执行）
- 触发场景：`web_watchdog.ok` 在 2026-10-31 19:10 出现 ts=0（文件损坏），`guard_health` 据此判「已 29890750 分钟没心跳」→ 自己 rc=1；从 19:20 起它每轮都读到**自己上一轮的 rc=1**，于是连红 73 轮到 2026-11-01 01:10，期间 `unstick` / `selfheal` / `patrol_quick` / `web_watchdog` 全部健康。这 12 小时里没有任何一张卡或任何一条 comment 提到它：`patrol-hits.md`（快检每小时覆盖写）只写 P1–P6，不含自检结论；agent 巡检上一次是 10-31 12:10，早于第一次红。
- 根因：规则缺了 —— ①A1 只写「红了你 15 分钟内做什么」，而巡检已降到每天两次，19:10 红到次日 01:10 之间结构上无人执行该窗口；②A1 没写「自检件不许把自己当被检对象」，`.ok` 存的是上一轮 rc，自引用即自锁；③A1 没把「自检结论必须进 `patrol-hits.md`」写成判据，巡检第一件事读的收件箱里根本看不到自检红。
- 规则动作：加 —— R23（①`guard_health` 不检查自身，存活由 crontab + `.ok` mtime 兜底；②`patrol_quick` 必须输出 `[自检红 A1]` 行进收件箱，判据只搬运 `guard_health.check()` 不重写；③响应窗口改为「下一轮 agent 巡检必须处置」，连续两轮仍红升级 paimeng，不许停派工；④澄清 ops 巡检 cron 会话不带 `HERMES_KANBAN_TASK`、不是卡 worker，跨卡 `unblock`/`complete` 实测 rc=0）。实修：`scripts/guard_health.py` 的 `ALLOW` 去掉 `guard_health`（`--strict` rc=0、`--selftest` rc=0）；`scripts/patrol_quick.py` 加 `guard_reds()`（假红注入实测出 2 条）。章程（A1 的 15 分钟段落）转 hatch 卡。

