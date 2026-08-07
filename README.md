# worktree-guard (Kimi Code 插件)

> EN: A Kimi Code CLI plugin that enforces a strict git-worktree workflow for AI agents:
> all edits happen in an isolated `worktree-<task>` branch copy, the main checkout is
> write-protected via a `PreToolUse` hook, and merging back requires explicit user
> authorization. Ships a management script (`create/enter/exit/status`), an agent Skill,
> and an optional status-line script showing the active worktree.

强制 git worktree 工作流的 Kimi Code CLI 插件：

- 🔴 **主 checkout 写保护**：无活动副本时，Agent 的 `Write` / `Edit` 落到主 checkout 一律被 `PreToolUse` hook 拦截（exit 2 + 上下文反馈）；
- 📦 **副本管理脚本**：`create / enter / exit / status / authorize-main / revoke-main`，创建时自动做防 `git add -A` 卷入的本地排除；
- 🔐 **合并授权闸**：在 master/main 上的 `git merge / rebase / pull`、`git push`、删 worktree 分支等均需用户明确授权（`authorize-main`）后才放行；
- 📊 **可选状态栏**：TUI 底部实时显示 Agent 正在工作的活动副本路径与分支（而不是会话启动目录）。

## 要求

- Kimi Code CLI ≥ 0.30（`[[hooks]]` / 插件 hooks / `[status_line]` 均可用）
- `python`（Python 3）与 `git` 在 PATH 上
- Windows / macOS / Linux 均可（插件脚本无平台特定调用）

## 安装

```text
/plugins install https://github.com/<owner>/worktree-guard
```

或指定分支 / tag / commit：

```text
/plugins install https://github.com/<owner>/worktree-guard/tree/<ref>
```

安装后运行 `/reload` 或开新会话生效。hook 随插件启停（`/plugins disable worktree-guard` 即暂停全部硬约束）。

## 可选：状态栏显示活动副本

插件无法代为修改 `tui.toml`，如需状态栏显示活动 worktree，手工追加一行到 `~/.kimi-code/tui.toml`：

```toml
[status_line]
command = "python <插件目录>/hooks/statusline.py"
```

插件目录通常为 `~/.kimi-code/plugins/managed/worktree-guard/`（Windows 例：
`command = "python C:/Users/<你>/.kimi-code/plugins/managed/worktree-guard/hooks/statusline.py"`）。
效果：有活动副本时显示 `⛏ <副本路径> (<副本名>) [<分支>]`，无副本时显示会话目录与当前分支。
注意自定义 status_line 会替换 TUI 底部第一行的内置布局。

## 工作流速览

```bash
# 创建并进入副本（skill 加载后 Agent 会自动按此流程操作）
echo '{"task_name": "fix-login"}' | python scripts/wt.py create
echo '{"path": ".worktrees/worktree-fix-login"}' | python scripts/wt.py enter

# ... 在副本内开发、提交 ...

# 退出并汇报，等待用户授权合并
echo '{"action": "keep"}' | python scripts/wt.py exit

# 用户授权后合并（Agent 须先记录授权，合并完立即撤销）
echo '{"reason": "用户授权合并 fix-login"}' | python scripts/wt.py authorize-main
# git merge worktree-fix-login  …
echo '{}' | python scripts/wt.py revoke-main
```

## 拦截规则一览

| 场景 | 结果 |
|---|---|
| 有活动副本，写副本内文件 | ✅ 放行 |
| 有活动副本，写主 checkout / 其他副本 | 🔴 拦截 |
| 无活动副本，写主 checkout 内任何文件 | 🔴 拦截 |
| master/main 上 `git merge / rebase / pull` | 🔴 拦截（需授权） |
| 副本内 `git checkout master/main`、删 `worktree-*` 分支 | 🔴 拦截 |
| 任何位置 `git push` 到 master/main | 🔴 拦截（需授权） |
| 副本内 `git merge master`（同步基线） | ✅ 放行 |
| `authorize-main` 授权期间 | ✅ 全部放行 |
| 仓库外路径、非 git 目录 | ✅ 放行 |

已知边界：hook 按官方定位是**轻量拦截**而非唯一安全屏障——fail-open（脚本异常放行），
且 Bash 正则只匹配以 `git` 直接开头的简单命令（`cd x && git merge` 类组合命令可能绕过）。

## 状态文件位置

全部状态存于 git common dir（主 checkout 的 `.git/`）下：

```
.git/worktree-guard/state.json      # 活动副本登记
.git/worktree-guard/override.json   # 主 checkout 写入授权
.git/worktree-guard/bases.json      # 各副本的 base 分支
```

不进版本库、不影响 `.gitignore`、所有 worktree 共享同一份。

## 卸载

```text
/plugins remove worktree-guard
```

只会删除安装记录与 hook 生效；各仓库 `.git/worktree-guard/` 内的状态文件可人工删除。
