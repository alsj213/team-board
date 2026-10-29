# .githooks

`core.hooksPath` 不进版本库，每个 clone 首次都要手动指一次：

    git config core.hooksPath .githooks

不配则该 clone 的 pre-push 护栏不生效（钩子文件在，但 git 不会调用）。

## pre-push：薄壳，判定在 `push-guard`

`.githooks/pre-push` 只做一件事：拿工作树根 → 把 git 喂进来的 ref 行（stdin）
原样透传给 `team-board push-guard` → 照它的退出码放行 / 拦截。主 checkout 是否
停在 `main`（P-1）、主 checkout 是否在推卡分支（P-2）、worktree 与卡分支是否
一致（P-3）、`main` / `master` 保护（P-4）四条判定**全在
`src/team_board/pushguard.py`**，shell 里没有第二份判断。完整判定表与文案模板见
`docs/push-guard.md`，用户视角见 README「独立 worktree 与 push 前的拦截」一节。

装完自查（`core.hooksPath` 指对了 + 钩子文件在 + 可执行）：

    team-board push-guard --repo . --check-install

**它不看钩子内容**：钩子还是纯 shell 拦 main/master 时这条也 rc0（已知假绿）。
要证明「钩子确实在调判定本体」，另配一条内容断言：

    grep -q 'team_board.cli push-guard' .githooks/pre-push

## fail-closed：跑不起来就拒绝，不静默放行

`python3` 找不到、`team_board` 导入失败（rc 126/127，或 stderr 出现
`No module named`）、或拿不到工作树根 —— 钩子一律 `exit 1`，并在 stderr 打印
`[pre-push] 拒绝：…（fail-closed…）`。**宁可推不上去，也不静默放行**；push-guard
自己报参数/环境错误（rc2）同样拦。

唯一的例外是**空 stdin**：`git push` 在「已是最新」时仍会调用钩子，但一行 ref 都
不喂（push-guard 判 rc2 `没有收到任何要推送的 ref`）—— 没有 ref 要传就没有可拦的
东西，钩子放行且不打印。判据是那句固定文案，不在 shell 里另算一遍「有没有 ref」
（文案改了 `tests/integration/test_pre_push_hook.py` 的用例 h 会当场红）。

修法：确认本仓 `src/` 在（钩子把 `$root/src` 塞进 `PYTHONPATH`）、`python3` 可用
（本仓纯标准库，任意 3.11+ 即可）、`git config core.hooksPath` 指向本目录。

## 绕过方式（已知剩余风险，不是后门）

`git push --no-verify` 会让 git 直接跳过**所有**客户端钩子，机器层面无从拦截；
新 clone / 容器里没配 `core.hooksPath` 时钩子根本不跑。所以本钩子只是「推之前
当场拦住」，权威判定仍是 CI job `branch-audit (卡分支/checkout)`。
