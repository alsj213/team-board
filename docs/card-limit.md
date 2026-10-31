# 卡正文 2048 字节硬上限

> 2026-10-29 定（老板原话「不是可选项」）。条文原文在
> `~/.hermes/profiles/{pm,engineer,ops}/SOUL.md` 的 `R16 · 卡正文 2048B 硬上限` 节；
> `ops/workspace/playbook.md` 的 R16 待 ops 同步 —— **本文件不是条文第二源**，两份冲突时
> 以章程条文为准，本文件只负责「口径 + 实测 + 边界」。
>
> 两条腿的实现卡：建卡拦截 `t_1564c165`、全板扫描 `t_d3625ec7`（`body-audit` 子命令）；
> 渲染本文件的卡是 `t_2f0b87d2`。模板见 [`docs/card-template.md`](card-template.md)。
> 模板 v3（五段骨架 + 固定段落锚点 + 目标 ≤800B）：[`docs/card-template.md`](card-template.md)；装到内核模板目录与漂移检查 `bash scripts/install-card-template.sh --check`（rc1 = 漂移或缺目标）。
> 固定段落红 / 1200B warn / 存量切点由 `t_7c17f775`（`cardtemplate.py`）与 `t_339b3d36`（`body-audit` 扩容）落地。

## 一、口径

`tasks.body` 的 **UTF-8 字节数 ≤ 2048** 才算合规，超过即不合规。

- **按字节不按字符**：700 个汉字是 700 个字符、**2100 字节**，超限。实测：

  ```
  $ wc -c body-700han.txt        # 文件内容 = 700 个「汉」字
  2100 body-700han.txt
  $ wc -m body-700han.txt
  700 body-700han.txt
  ```

  同一段正文的字符数（`wc -m`）和字节数（`wc -c`）不是一回事，闸门只看后者。
- 别和上下文截断的 `_CTX_MAX_BODY_BYTES = 8 * 1024` 混为一谈：那是拼 worker 上下文时的
  截断阈值（`hermes_cli/kanban_db.py:331`，用点 `:4034`），**不是**建卡闸门；建卡闸门是
  `_MAX_TASK_BODY_BYTES = 2048`（`hermes_cli/kanban_db.py:335`），它是本仓
  `src/team_board/bodylimit.py:23` 里 `BODY_LIMIT_BYTES` 的镜像，改动必须两边同步。

## 二、两条腿

### ① 建卡拦截（写的时候拒）

两条入口都走同一个校验：`kanban_db.create_task` 里的 `_validate_task_body(body)`
（唯一调用点 `hermes_cli/kanban_db.py:1239`，覆盖 CLI `hermes kanban create` 与
`kanban_create` 工具）。超限一律 rc=2、stderr **恰一行**、零 traceback、**卡不落库**。

告警原文（逐字，本卡实跑，`3000` 处是实际字节数）：

```
kanban: body 超过硬上限：3000 字节（UTF-8），上限 2048 字节；长背景/证据请放附件或卡评论，卡正文只写差异部分。
```

工具侧（`kanban_create`）返回结构化的 `{"error": "…"}`，同一句文案；那一侧由 `t_1564c165`
实测（本卡只实跑 CLI 一条，不转述未跑过的输出）。

边界实测（隔离 `HERMES_KANBAN_HOME`，不碰真板；`bash limit_probe.sh`，日志是本卡附件）：

| 正文字节 | 构造 | rc | 结果 |
| --- | --- | --- | --- |
| 2048 | `"B" * 2048` | 0 | 建成 |
| 2049 | `"C" * 2049` | 2 | 拒，stderr 1 行 `…：2049 字节（UTF-8）…` |
| 2100 | `"汉" * 700` | 2 | 拒（按字节判，不按字符） |
| 3000 | `"A" * 3000` | 2 | 拒，stderr 1 行 |

没有 `--allow-long-body`、没有环境变量开关、不截断正文 —— 只能改正文或改放评论/附件。

### ② 全板扫描（存量查）

```
$ PYTHONPATH=src python3 -m team_board.cli body-audit        # 未结卡（status ∉ {done, archived}）
$ PYTHONPATH=src python3 -m team_board.cli body-audit --all  # 含已结卡（仍不含 archived）
$ PYTHONPATH=src python3 -m team_board.cli body-audit --cutoff 0  # 全部视作新卡（造红例）
```

退出码：

| rc | 含义 | 实测命令 |
| --- | --- | --- |
| 0 | 无超限、无固定段落红卡（**只有 warn 仍是 0**） | `body-audit --db <干净库>` → `合计：2 张；超限：0 张；NULL：0 张` |
| 1 | **发现**超限卡，或有固定段落红卡 | `body-audit --db <含红卡库> --cutoff 0` → `固定段落红清单（2 张）：` |
| 2 | 数据源或参数错误 | `body-audit --db /nonexistent/x.db` → stderr `错误: 数据源不存在: /nonexistent/x.db`；`--cutoff -1` → stderr `错误: --cutoff 不许为负：-1（0 = 全部视作新卡）` |

注意 rc=2 这一条与基础子命令**不同**：`list` / `summary` 的数据源错误是 rc=1，
`body-audit` 把「数据源打不开」也算成 2（「判不了」绝不和「发现问题」同码）。

输出含逐卡明细（卡号 / 状态 / 负责人 / 字节 / 判定）、分桶、合计、`NULL` 计数与
**超限清单**（卡号列表直接给出来，不用自己数）。

卡 t_339b3d36 起末尾追加三段（前六段的行序一字未动，新段一律排在超限清单之后）：

- **固定段落红清单**：不早于 `--cutoff` 的卡若正文顶格出现 `## 完成契约` / `## 验收三段式`
  等固定段落标题，就进这个清单（逐卡附改写指引，指引里含 `TEMPLATE: card-template.md#v3`
  的准确写法）；**有红即 rc1**。
- **warn 清单**：正文 UTF-8 字节数 > `WARN_BYTES`（1200，目标 800）或缺 `TEMPLATE:` 引用行，
  逐卡打印「卡号 / 字节 / 标记（`over-1200` / `no-template-ref`）」；**warn 不改 rc**。
- **存量跳过**：`created_at` 早于切点的卡数量 —— 存量卡不判红（R16 ⑤ 不追溯），只计数。

判据本体全部在 `src/team_board/cardtemplate.py`（`scan` / `rewrite_hint` /
`FIXED_SECTION_EPOCH` / `WARN_BYTES`），`bodylimit.summarize` 只负责把它们汇总进
`counts['red' | 'warn' | 'legacy']`，本文件不复制任何一条规则。

## 三、为什么

主对话每次工具调用都要重发整段上下文（含卡正文），长正文按 token 计费 —— 卡正文越长，
后续每一次工具往返都越贵，而且这份成本与卡本身的价值无关。

2026-10-29 老板口径：**「今日 $9.46 里 93% 是主对话重发」**。来源是本卡的需求卡
（`t_c4a840eb` 的评论，转引老板账单口径），**不是**本文件作者自测；本仓没有 token 账单
数据，也不复现这个比例。

## 四、边界

- **不追溯改写已 `done` 卡**：存量超限卡只由 `body-audit` 记录并列出，正文保持原样。
- **不为压缩让验收标准 / 边界 / 禁止事项不可追溯**：只允许把它们**移到**附件、模板或
  卡评论，不许删掉。
- **只告警不拦截不算达标**：判据是写入口真的 rc=2 且卡不落库，不是「打印一句提醒」。
- **在途卡不强行中断**：建卡拦截不回溯、不改写正在跑的卡。
- `body-audit` 是只读的：`mode=ro` 打开看板库，不建表、不写库。

## 五、与既有规则的关系

- **R3**（验收映射每条 PASS 必须本机实跑命令 + 真实退出码）：照旧，本规则不替代证据。
- **R14**（返工标因 `spec` / `impl` / `env`）：不受影响；返工计数与卡正文长度无关。
- **R2**（需求卡只在子卡全部闭合且验收 PASS 后才关）：不变；本条不改变父子卡状态机。
- **R4**（分组执行类卡必须附清单覆盖性对账）：不变；对账命令照跑。

## 六、没法机器校验的部分

- **hermes 侧补丁会被 `hermes update` 覆盖**：建卡拦截改在
  `~/.hermes/hermes-agent/hermes_cli/kanban_db.py`（上游仓库的本地 checkout），
  上游一更新这份补丁就没了，建卡拦截会**静默失效**；代码里只留了一行注释指针
  （`镜像 src/team_board/bodylimit.py:BODY_LIMIT_BYTES，勿单方面改`），没有自动恢复。
- **跨仓一致性**：本仓 `bodylimit.py` 的 `OVER_LIMIT_TMPL` 与 `hermes_cli/kanban_db.py`
  的两句告警是两份字面量，一致性只在 **V1 收口卡实跑比对**，**不进 CI**
  （CI 只 checkout 本仓，看不到 hermes 侧仓库）。
