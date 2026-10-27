# team-board

命令行看板，只读本地数据，用于查看团队当前任务状态。

## 子命令（占位）

- `team-board list` —— 列出看板上的任务
- `team-board show <id>` —— 查看单条任务详情
- `team-board stats` —— 输出任务状态统计

## 数据源

本地文件：`~/.hermes/profiles/hatch/workspace/board.json`（JSON，只读，不写回）。
文件不存在或格式非法时命令以非零码退出并打印错误，不自动创建文件。

## 开发

需要 Python >= 3.11，运行时零第三方依赖。

```
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
PYTHONPATH=src python3 -m team_board.cli
```
