# 验收标准闸门（qa 预先签署）

**一句话**：验收标准由 **qa 先写、qa 先签**；pm 拆卡时只转抄 qa 的结论，不代签。两道检查——
**建卡时**验「有没有签」，**收口时**验「谁来关」。

模板侧的写法见 [`docs/card-template.md`](card-template.md) 第 7 块；本文只写口径、边界与自查
命令。返工归因口径见 R14。

## 0. 代码在哪（先读这段，别当成「已合入上游」）

两道闸门的代码**只在本机 `~/.hermes/hermes-agent` 的未提交工作区里**（`git status` 能看到
`hermes_cli/kanban_db.py` / `hermes_cli/kanban_db_graph.py` 及对应测试的改动），**没有合入上游**：
该 checkout 的 HEAD `3eb7ed08d1`（= `origin/main`）上
`git show HEAD:hermes_cli/kanban_db.py | grep -c _validate_qa_spec` → `0`
（`_qa_spec_first_line` / `_QA_SPEC_SIGN_RE` / `_gate_qa_completion` 同为 `0`，
`git show HEAD:hermes_cli/kanban_db_graph.py | grep -c _validate_qa_spec` 亦 `0`）。

所以把话说成两句，别混：

- **本机此刻会拦**：本文的命令、rc、文案都是在这份**未提交工作区**上的实跑（2026-10-31），
  不是预告；闸门入口：建卡 `hermes_cli/kanban_db.py:1436`（`create_task`）与
  `hermes_cli/kanban_db_graph.py:205`（decompose 子卡入口），完成闸门
  `hermes_cli/kanban_db.py:2909`。
- **「已合入上游 / `main` 上 live」不成立**：上述 grep 全 `0` 就是反证。而且按
  [`docs/card-limit.md`](card-limit.md) 第六节，hermes 侧补丁会被 `hermes update` 覆盖、
  **静默失效**，没有自动恢复 —— 换机、更新、或怀疑闸门不在时，先跑上面那条 grep：`0` = 闸门
  已经不在，本文的 rc 一律不适用。

行号（`1436` / `2909` / `205` / `1697` / `398-428` / `2898`）都是**本机工作区**的行号，上游
checkout 一变就对不上，引用前重核。

## 1. 为什么

返工归因（R14）把每次打回标成 `spec` / `impl` / `env` 三类。实测（2026-10-31 截面，
R14 ⑤ 唯一校验命令输出的第一段）：**spec 9 条 / impl 6 条 / `UNTAGGED` 12 条**。三个数
**同为事件数**（`changes_requested` 每打回一次算一条），比例也按同一单位算 —— 已归因的
15 条里 **9/15（60.0%）是卡面写错**（判据自相矛盾、卡内数字与 fixture 冲突、验收标准
漏写），不是实现走样。

**单位只认一种**：本页所有计数都是**事件数**。按卡去重是另一个单位（同日截面：
spec 8 卡 / impl 4 卡），别把它和事件数混在一句里 —— 上一版就犯过这个错（拿 spec 的
事件数去比 impl 的卡数）。

上述计数是**截面值，随板增长**（板上每多一次打回就变），引用前先自查 —— R14 ⑤ 的唯一校验
命令，输出第一段即分因占比；另有 `UNTAGGED` 单列，不折进 `spec` / `impl`：

```
$ /home/liu/miniconda3/bin/sqlite3 -header -column ~/.hermes/kanban/boards/devteam/kanban.db \
    < ~/.hermes/profiles/ops/workspace/rework_audit.sql
```

`sqlite3` **不在默认 PATH**（`command -v sqlite3` rc=1 并不等于本机没有）：miniconda 自带的
`/home/liu/miniconda3/bin/sqlite3` 3.51.1 实测跑通上面这条命令 rc=0，输出第一段就是上面那三个
数。等价读法：取 `task_events` 里 `kind='changes_requested'` 的行，`payload.reason` 首行的
`REWORK-CAUSE:` 值即归因；无此行的记 `UNTAGGED`。连 CLI 都没有的环境，改用 Python 标准库
`sqlite3` 以只读方式（`file:<db>?mode=ro`）打开同一个 `kanban.db`，按这句等价读法取数，
结果与上面命令同一份。

结论：问题不在验收轮次太少，而在**验收标准出场太晚**——派卡时只有一句「完成目标」，跑完
才第一次把判据说清，返工必然跑在实现者头上。修法是把验收标准前置：**派卡前写清判据，
并由 qa 签名**，卡面错了在开跑前就被拦住。

## 2. 建卡闸门（建卡时就拦）

- **适用范围**：`assignee=engineer` 的实现卡。其余 assignee（pm / qa / repo / ops，以及老板
  手写的需求卡）**不受约束** —— 需求卡不因为漏了这一段被拦。
- **判据两条，缺一条即拒**（内核 `_qa_spec_first_line` / `_validate_qa_spec`，
  `hermes_cli/kanban_db.py:398-428`，照它的实现抄，不抄「比这个更严」的直觉）：
  1. 正文里存在一行，`line.rstrip() == "## 验收标准"` —— 顶格、不带编号，**行尾多几个空格照样
     算**（内核先 rstrip 再比）；
  2. 该段的**第一条非空行**（段内空行跳过）整行 `fullmatch` `^QA-SPEC: qa$` —— 顶格、大小写
     敏感、冒号后是半角空格、行内不许别的内容、**行尾多一个空格就判红**。
  段的结束 = 下一个以 `## ` 开头的行或正文结尾；`splitlines()` 吞 CRLF，所以 CRLF 正文与 LF
  正文同一判定。
- **拒绝行为**（对本机那份未提交补丁的实跑口径，见 §0）：`hermes kanban create` 返回 **rc=2**、stderr 恰一行、
  **卡不落库**；`kanban_create` 工具返回 `{"error": …}`，同样不落库。「缺段」与「缺签署」是
  两条分开的文案，不复用一句。
- **不提供的口子**：没有关闭开关、不截断正文、不自动补段、不事后转正。要放宽范围只能改内核
  里那个「必须签署的 assignee」元组（`_QA_SPEC_REQUIRED_ASSIGNEES`，唯一一处）。

### 自查命令（与内核同一判据）

卡正文先落到文件 `body.md`（或从 `kanban_show` 里取 `body`），**直接调内核那个函数**，别自己
写判据 —— 判据只有一份，抄一遍就会漂：

```
$ /home/liu/.hermes/hermes-agent/venv/bin/python -c "import sys; sys.path.insert(0,'/home/liu/.hermes/hermes-agent'); \
from hermes_cli import kanban_db as k; \
k._validate_qa_spec(open('body.md', encoding='utf-8').read(), 'engineer'); print('ACCEPT')"
```

- `exit 0` + 打印 `ACCEPT` → 合规；
- 抛 `QaSpecMissingError`（rc≠0）→ 不合规，文案里会写清是**缺段**还是**缺签署**（后者附上它
  看到的「段内第一行」前 40 字）。
- **解释器不能用本机 PATH 上的 `python3`**：那一个是 3.14.7，`import ruamel.yaml` 直接
  `ModuleNotFoundError`（实测 rc=1，压根到不了闸门）；带依赖的是 hermes-agent 自带的
  `~/.hermes/hermes-agent/venv/bin/python`（实测 rc=0，A–F 六形态逐格与内核一致）。换机器就
  换成你自己那份能 `import hermes_cli` 的解释器，路径别照抄。

**`grep` 只能做粗筛，不能当判据**（A–F 六形态实测，证据见卡 `t_5ace7c80` 附件
`t_5ace7c80-forms.log`）：

| 形态 | 内核 | `grep -n '^## 验收标准$'` | `grep -A2 …` 第二行 |
| --- | --- | --- | --- |
| A 规范 | ACCEPT | rc=0 | `QA-SPEC: qa` |
| B 标题带尾空格 | ACCEPT | **rc=1（误判缺段）** | 无 |
| C 标题后空一行 | ACCEPT | rc=0 | **空行（误判缺签署）** |
| D 签署行带尾空格 | REJECT | rc=0 | `QA-SPEC: qa `（尾空格） |
| E CRLF 正文 | ACCEPT | **rc=1（误判缺段）** | 无 |
| F 签署行在段外 | REJECT | rc=0 | 判据一条。（**误判合规**） |

也就是说：`grep` 在 B / E 上误报「不合规」，在 F 上误报「合规」，C 上误报「缺签署」。所以
「任一条 rc=1 就是缺段或缺签署」这句不成立 —— 判合规 / 不合规一律以上面那段 python 为准。

## 3. 完成闸门（卡只能由 qa 收口）

闸门在本机工作区 **live**（同 §0：上游 HEAD `3eb7ed08d1` 里没有这段代码）：入口
`_gate_qa_completion`（`hermes_cli/kanban_db.py:2898`）在完成事务**之前**跑，判据一行 ——
`_QA_SPEC_SIGN_RE.search(body)`（同文件 `:2909`）：**全文任意位置**命中整行
`QA-SPEC: qa` 就算签，**不看段、不看 assignee**。

- **判据**：正文带顶格整行 `QA-SPEC: qa` 的卡，**只有 qa 的 run 能 `complete`**。
- **pm 的验收 run 被拒**：状态零变更、不发完成事件。出路三条，按场景选：
  - `kanban_request_changes(reason=…)` —— 实现没达标，打回重做；
  - `kanban_request_review(reviewer="qa")` —— 实现达标，转给 qa 收口；
  - `kanban_comment(body=…)` —— 只留话，不改状态。
- **无 active run 的 CLI 手工 `complete` 同样被拒**（rc=2、stderr 恰一行、零 traceback）。
  `--force` 是运维逃逸口，不是常规路径：放行会写一条 `qa_completion_override` 审计事件
  （记录 profile / run_id / at），事后可查，不许静默放行。
- **不受影响**：`kanban_request_changes` / `kanban_comment` / `kanban_request_review` 三条
  路径的行为不变 —— pm 照样能打回、能留言、能转签。

## 4. 存量不追溯

- 只约束**新建**卡。旧卡不改正文、不补签、不因缺这一段被回头判不通过。
- 已 `done` 的卡不重判；在途卡不中断。
- **改派不追溯**：闸门只挂在建卡入口（`create_task`）和 decompose 子卡入口
  （`hermes_cli/kanban_db_graph.py:205`）；`assign_task`（`hermes_cli/kanban_db.py:1697`）
  **不跑** `_validate_qa_spec` —— 把一张已有卡改派给 `engineer` 不会因缺段被拦，也不会补签。
  这是已知的覆盖缺口，不是「本该拦却没拦」的 bug；要判据就赶在建卡时说清。
- 想给旧卡换口径：**新开一张卡**，不改旧卡 —— 历史保持可追溯。

