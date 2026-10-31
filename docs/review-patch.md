# 双问证据按「内容指纹」绑定（REVIEW-PATCH 规范）

> 定于 2026-10-31，卡 `t_494499cc`（E6）。判据本体在 `src/team_board/reviewhead.py`，
> 命令行入口是 `python3 -m team_board.cli pr-review-head-audit`，合并侧同一判据由
> `merge_guard` 的「双问绑定」闸门调用。本文只写**怎么贴、值从哪来、为什么这么算**；
> 闸门顺序与 rc 定义见 README「合并前的两道证据闸门」与 `docs/merge-rebase-gate.md`。
>
> 一句话：**双问结论绑的不是 head sha，是「这段改动的内容」**。纯 rebase 换了 sha 但内容
> 没变 → 结论继续有效；内容动了（**含只改缩进、行尾空格**）→ 立即失效，必须重跑双问。

## 一、两行标记：`REVIEW-PATCH` + `REVIEW-KIND`

一条标记 = 两行，**都顶格、各一行、值在冒号后一个空格**，写在同一份评审正文（PR 页面
Review 或 issue 评论）里：

```
REVIEW-PATCH: <40 位小写十六进制>      内容指纹（新写法）
REVIEW-KIND: defect|regression         这一条是哪一类双问
```

- `REVIEW-PATCH` 只收 **40 位小写十六进制**（`[0-9a-f]{40}`，大小写敏感、不许缩进、不许
  行尾多空格、不许写成 `` `REVIEW-PATCH:` `` 或 `**REVIEW-PATCH:**` 这种 Markdown 装饰）。
- `REVIEW-KIND` 只收 `defect` / `regression` 两个词（缺陷视角 / 回归视角各一条）。
- 一条正文只取每条正则的**第一个**匹配；只有 `REVIEW-KIND` 不算一条标记。
- 同一条正文里 `REVIEW-PATCH` 与 `REVIEW-HEAD` 同时出现时**优先 `REVIEW-PATCH`**。
- 判定口径：当前内容指纹上 `defect` 与 `regression` **各 ≥1 条** 才算绑定成立。

### 谁贴、什么时候贴、值从哪来

- **谁**：跑双问的人（pm / qa）。两条双问各贴一条标记，`REVIEW-KIND` 分别是
  `defect` / `regression`，两行的 `REVIEW-PATCH` 值相同。
- **什么时候**：双问在这一份内容上跑完之后、请人合并之前，把结论与两行标记一起贴到 PR
  页面（Review 正文或评论都行）。**纯 rebase 之后不用重贴**（指纹不变），只有内容改了才要
  重跑双问、贴新值。
- **值从哪来**：不要手搓。报告首行 `current=` 是**同一份内容**的指纹，但它只打印**前 12 位**
  （一行放得下）；`REVIEW-PATCH` 要的是完整 40 位，取法有两种，**结果逐字节相同**（实测见下）：

  ```
  # 1) 审计报告：看到的是哪一份内容
  $ PYTHONPATH=src python3 -m team_board.cli pr-review-head-audit \
      --pr <PR 号> --head <40 位 head sha> --repo <checkout 路径>
  algo=diff-sha256-U0-v1 current=<前 12 位> base=<base 的 40 位> mb=<merge-base 前 12 位>

  # 2) 完整 40 位（与指纹层同一算法：只丢 @@ 头与 index 行，其余逐字节进 sha256）
  $ mb=$(git -C <checkout> merge-base <base> <head>)
  $ git -C <checkout> diff -U0 --no-color "$mb" <head> \
      | grep -v -e '^@@' -e '^index ' | sha256sum | cut -c1-40
  64c73bcd6d673735bcab6116d9db34e7e9131515
  ```

  两处同值实测：报告 `current=64c73bcd6d67`、上面那行给出
  `64c73bcd6d673735bcab6116d9db34e7e9131515` —— 前 12 位一致，就是同一份指纹。
  （「报告只打印前 12 位」这个不便已单独立卡跟进，见本文末。）
  `--repo` 默认 `.`、`--base` 默认 `origin/main`（解析不到回退 `main`）、`--remote` 默认
  `origin`；`--head` 是**预检读到的 head 提交**，不是分支名。
- 报告其余部分：第二行 `扫描正文 N 条；带标记行 M 条`，随后每条标记一行
  `<状态> <标记指向的内容前 12 位> <kind>`（状态见第五节），最后一行 `结论: …`。
- 退出码：`0` 绑定成立 / `1` 该内容上双问不齐（缺类、缺证、标记指向别的内容、取不到对象）/
  `2` 参数或环境错误（`--head` 形态、`--pr` 形态、`--base` 解析不到、`--repo` 不是工作树、
  `gh` / `git` 调用失败）。

## 二、指纹算法：`sha256(git diff -U0 的内容行)[:40]`

算法标识 `FINGERPRINT_ALGO = diff-sha256-U0-v1`（报告首行 `algo=` 打印它，换算法就换这个串）：

1. `mb = git merge-base <base> <head>`（取分叉点；两条历史没有共同祖先 → 内容取不到，
   按「不可验证」处理，不是环境错误）；
2. `git diff --numstat -U0 --no-color <mb> <head>` —— 输出为空 = 空 diff；任一行以
   `-\t-\t` 开头 = 该范围含二进制文件；
3. 否则 `git diff -U0 --no-color <mb> <head>`，丢掉两类与内容无关的行 ——
   `@@` 开头的 hunk 头（含行号 / 行数）与 `index <blob>..<blob>` 行（blob sha）——
   剩下的行**逐字节**用 `\n` 连接后取 `sha256`，取前 40 位十六进制。

**为什么必须 `-U0`**：默认上下文（`-U3`）会把改动点上下各 3 行一起带进 diff。上游在同一
文件的**别处**插几行，这几行上下文就跟着变 → 指纹变 → 明明内容没动的结论被判陈旧（误杀）。
`-U0` 只留真实的 `+` / `-` 行；再丢掉 `@@`（行号）与 `index`（blob sha）之后，
纯 rebase 与行号位移都不再影响指纹。

**为什么不用 `git patch-id --stable`**：它会把行首 / 行尾与连续空白归一化，于是「只改缩进」
不换指纹 —— Python 里缩进能改语义，这是 **fail-open**（拿旧证据给新内容放行）。实测同一份
内容的两种缩进写法：

```
内容 A `    x = 1`   → 内容指纹 64c73bcd6d673735bcab6116d9db34e7e9131515 | patch-id 718d6f3c1b56bde9edbdcc09a7a1562219567bd8
内容 B `    x  =  1` → 内容指纹 0a6b822b1a99fef780e7072921d94bed6643e945 | patch-id 718d6f3c1b56bde9edbdcc09a7a1562219567bd8
```

指纹变了、patch-id 没变 —— 所以本仓不用 `patch-id`（本机 git 2.34.1 也没有
`patch-id --verbatim`），自己按内容行算。

## 三、纯 rebase 不变：实测表

真 git 造提交（`git init` 一个临时仓库，`feat` 分支 `git rebase` 到动过的 `main` 上），
`base` / `head` 显示前 12 位，指纹是完整 40 位：

| 场景 | base | head | 内容指纹 | kind | 对照 `patch-id --stable` |
| --- | --- | --- | --- | --- | --- |
| 起点：feat 相对 base 的提交 | `64cb714980b6` | `99af27724ade` | `64c73bcd6d673735bcab6116d9db34e7e9131515` | patch | `718d6f3c1b56bde9edbdcc09a7a1562219567bd8` |
| 纯 rebase 到「上游改了别的文件」 | `b097e682766b` | `6259a1834512` | `64c73bcd6d673735bcab6116d9db34e7e9131515` | patch | `718d6f3c1b56bde9edbdcc09a7a1562219567bd8` |
| 纯 rebase 到「上游同文件上方插行」 | `b4664460d4f6` | `676545d4378b` | `64c73bcd6d673735bcab6116d9db34e7e9131515` | patch | `718d6f3c1b56bde9edbdcc09a7a1562219567bd8` |
| 只改缩进（多两个空格） | `b4664460d4f6` | `1b7e887d856f` | `0a6b822b1a99fef780e7072921d94bed6643e945` | patch | `718d6f3c1b56bde9edbdcc09a7a1562219567bd8` |
| 改一个值 `1` → `2` | `b4664460d4f6` | `026965e23b42` | `6828f405dce7abc466c19d9917cee42739f85ace` | patch | `82ac20d07ecec16325c188a3aedee6ac8a329d65` |
| 行尾多一个空格 | `b4664460d4f6` | `9dbec156de49` | `7f303b1a8143e3e848deee92dbfba17289ba5444` | patch | `718d6f3c1b56bde9edbdcc09a7a1562219567bd8` |
| 新增二进制文件 | `b4664460d4f6` | `71b18bb9471e` | `-`（`kind=binary`） | binary | `326dfeaa0c1915e8d53b9fd4c26680f8775974f3` |
| head == merge base（空 diff） | `b4664460d4f6` | `b4664460d4f6` | `-`（`kind=empty`） | empty | `-` |

读表三条：

1. 前两行 rebase 前后 **sha 全变、指纹一字不变** → 旧的 `REVIEW-PATCH` / `REVIEW-HEAD`
   在纯 rebase 后继续可用，不必重跑双问；
2. 「只改缩进」「行尾多一个空格」**指纹变**（patch-id 对照却不变）→ 这就是不能绑
   `patch-id` 的实测依据；
3. 换文件 / 改行 → 指纹变，正是要被判出来的两件事。

端到端复核（`gh` 是假脚本、git 全真）：

```
$ PYTHONPATH=src python3 -m team_board.cli pr-review-head-audit --pr 4 --head <rebase 后的 head> --repo <临时仓库> --base main
exit 0
algo=diff-sha256-U0-v1 current=64c73bcd6d67 base=b4664460d4f6e550c01636654b8808bdc92f67f7 mb=b097e682766b
结论: 内容指纹 64c73bcd6d67 的 defect + regression 各 >=1 条 → 绑定成立
```

把 `--head` 换成「只改缩进」的那个提交，同一份标记立刻变 `exit 1`：
`结论: 内容指纹 0a6b822b1a99 缺 defect,regression → 内容变了要重跑双问`。

## 四、算不出指纹就拒绝：二进制 / 空 diff（fail-closed）

两种范围算不出「可比内容」，一律**不许当 PASS**，也不许拿 `patch-id` 之类凑一个值出来：

- **含二进制文件**：diff 正文只有 `Binary files a/x and b/x differ` 一行，**两个不同的
  blob 长得一样** → 指纹不覆盖二进制，`kind=binary`、`value=None`，报告点名路径。
- **空 diff**（`head == merge base`）：没有任何内容可比 → `kind=empty`、`value=None`。

两端表现（实测）：

```
$ ... pr-review-head-audit --pr 8 --head <加了 blob.bin 的提交> --repo <临时仓库> --base main
exit 1
结论: 分支含二进制文件 blob.bin,src/mod.py → 内容指纹不覆盖二进制（diff 正文只有 differ 一行），按不可绑定处理

$ ... pr-review-head-audit --pr 9 --head <与 base 相同的提交> --repo <临时仓库> --base main
exit 1
结论: 分支相对 b4664460d4f6e550c01636654b8808bdc92f67f7 无差异 → 无可绑定内容（不许当 PASS）
```

合并侧（`merge_guard` 双问绑定闸门）对这两种范围的拒绝文案是**同一条**
`双问不可复用`（附原因），两种实测原文：

```
双问不可复用：分支含二进制文件 blob.bin,src/mod.py（patch-id 对二进制内容盲，换文件也换不了指纹）；拒绝合并
双问不可复用：分支相对 main 无差异（空 diff 没有可比内容）；拒绝合并
```

结论行里点名的路径是「该 diff 里出现过的全部路径」，不只有二进制那个文件 —— 按它去找
二进制文件时，路径清单是要人工看一眼的那一份，不是过滤结果。

## 五、旧 `REVIEW-HEAD` 按内容复用：只读、不追溯、不改历史评论

历史上贴的是 head sha：

```
REVIEW-HEAD: <40 或 64 位小写十六进制>   历史 head sha（只读兼容）
REVIEW-KIND: defect|regression
```

处理口径：**用它的 sha 现算一份内容指纹**，与当前内容比。等价即结论可用，不等价即失效。
历史评论**只读** —— 不改写、不补标记、不追溯重贴；`REVIEW-HEAD` 不需要迁移。

| 状态 | 条件 | 是否计入「该内容上的一类双问」 |
| --- | --- | --- |
| `OK` | `REVIEW-PATCH` 的值 == 当前内容指纹 | 计入 |
| `LEGACY-OK` | 只有 `REVIEW-HEAD`，其 sha 现算出的指纹 == 当前内容指纹 | 计入 |
| `STALE` | 标记指向的内容 ≠ 当前内容（含「只有别的 sha 的标记」） | 不计入 |
| `UNVERIFIABLE` | 只有 `REVIEW-HEAD`，对象 / 指纹取不到（孤儿 sha） | 不计入 |

实测（旧标记 + 纯 rebase 后的 head，`LEGACY-OK` 放行）：

```
$ PYTHONPATH=src python3 -m team_board.cli pr-review-head-audit --pr 5 --head <rebase 后的 head> --repo <临时仓库> --base main
exit 0
algo=diff-sha256-U0-v1 current=64c73bcd6d67 base=b4664460d4f6e550c01636654b8808bdc92f67f7 mb=b4664460d4f6
扫描正文 2 条；带标记行 2 条
  LEGACY-OK 99af27724ade defect
  LEGACY-OK 99af27724ade regression
结论: 内容指纹 64c73bcd6d67 的 defect + regression 各 >=1 条 → 绑定成立
```

另外两种 rc1（都**不许**当成「没绑定，重贴就行」，也都不许静默放行）：

- **head 对象取不到**（本地没有、按 sha `git fetch` 也失败）——
  `结论: 取不到 <sha>，无法按内容绑定；先 git fetch 或在新 head 上重贴 REVIEW-PATCH`；
- **标记的 sha 是孤儿**（对象不存在 / 与 base 无共同祖先）→ 该标记 `UNVERIFIABLE`，不计入；
  当前内容上没有别的有效标记时结论是「缺 defect,regression」。历史评论删不掉，一条孤儿
  旧标记不该把整条命令变成永久的环境错误（rc2）。

> 不要做的事：不要手搓 `REVIEW-PATCH` 的值，也不要贴「上一条的指纹 + 新跑的双问结论」；
> 值只能来自上面第一节的两条取法，否则闸门照样判陈旧。

---

附注（本条之外的已知不便，已单独立卡 `t_a0f1e55b`）：`pr-review-head-audit` 报告首行只打印
指纹前 12 位，贴 `REVIEW-PATCH` 需要完整 40 位时得按第一节第 2 条自己算 —— 工具侧改输出等
那张卡落地，本文写法随之简化。
