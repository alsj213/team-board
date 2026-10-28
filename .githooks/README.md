# .githooks

`core.hooksPath` 不进版本库，每个 clone 首次都要手动指一次：

    git config core.hooksPath .githooks

不配则该 clone 的 pre-push 护栏不生效（钩子文件在，但 git 不会调用）。
