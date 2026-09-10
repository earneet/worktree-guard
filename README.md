# worktree-guard (Kimi Code 插件) v1.1.3

> EN: A Kimi Code CLI plugin that enforces a strict git-worktree workflow for AI agents:
> all edits happen in an isolated `worktree-<task>` branch copy, the main checkout is
> write-protected via a `PreToolUse` hook, and merging back requires explicit user
> authorization. Ships a management script (`create/enter/exit/status`), an agent Skill,
> and an optional status-line script showing the active worktree.
>
> v1.1.0: (1) **Coexistence yield rule** — if the main checkout root contains
> `.kimi/worktree-state.json` (state file of a repo-specific guard), this generic guard
> silently allows everything for that repo; (2) `exit(action="remove")` is idempotent
> when the copy directory is already gone; (3) `exit` supports `delete_branch: true`
> (with `confirm_remove: true`) which deletes the worktree branch only after verifying
> it is merged into its base (`git merge-base --is-ancestor`), refusing otherwise;
> (4) optional per-repo config `.kimi/worktree-guard.json` with `post_create_commands`
> (array of shell strings) run best-effort inside the new copy after `create`.
> ⚠️ `authorize-main` and any guarded mutation command (e.g. `git merge`) must be issued
> as **separate tool/Bash calls** — the hook pre-checks the whole command string before
> execution, so `authorize && git merge` in one line is still blocked.
>
> v1.1.1: hook precision fixes — mutation regexes no longer swallow read-only subcommands
> (`git merge-base` / `git merge-file`) and only match `git` in command position (line
> start or after `&&`/`;`/`|`), so commit messages or echoed text containing "git merge"
> no longer trigger; git-mutation rules are now scoped to the guarded repo — a leading
> `cd <path> &&` chain is resolved to an effective cwd, and commands whose effective cwd
> is outside the guarded repo (main checkout or its linked worktrees) are allowed.
>
> v1.1.2: (1) **stale-state self-healing** — when `state.json` says a worktree is active
> but its registered path no longer exists (deleted externally without `exit`), the hook
> treats the repo as having no active worktree and removes the stale state, instead of
> blocking the whole repo on a ghost copy; (2) documentation: the coexistence yield rule
> now states its persistence contract — the repo-specific guard's `.kimi/worktree-state.json`
> anchor file must persist (with `active: false` meaning idle), or the yield rule lapses
> whenever the repo-specific guard exits and both guards would double-block.
>
> v1.1.3: (1) **residual-cleanup mode in `exit`** — when the state file is already
> `active: false` but still carries a `path`/`branch` registration (left by hook
> self-healing or a previous exit), `exit` no longer refuses; it runs the normal cleanup
> flow against the registered path/branch (`remove` rules, `delete_branch`, registration
> clearing) and says so explicitly. Only a state with no registration at all is still
> refused; (2) hook stale-state self-healing now **rewrites `active: false`** instead of
> deleting `state.json`, preserving the registration so a single `exit` can finish the
> cleanup (v1.1.2 deleted the file, which made the leftover branch unreachable by `exit`).

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

# 删除副本目录（需显式确认；分支保留）。目录已被外部删除时幂等成功，照常清理登记状态
echo '{"action": "remove", "confirm_remove": true}' | python scripts/wt.py exit

# 删除副本目录并一并删除分支（仅当分支已合并进其 base 才执行 git branch -d，未合并明确拒绝）
echo '{"action": "remove", "confirm_remove": true, "delete_branch": true}' | python scripts/wt.py exit

# 用户授权后合并（Agent 须先记录授权，合并完立即撤销）
# ⚠️ 授权与受守卫的变更命令必须分两次工具/Bash 调用：hook 在执行前对整个命令串预检，
#    `authorize-main && git merge` 连写在同一条命令里仍会被拦截。
echo '{"reason": "用户授权合并 fix-login"}' | python scripts/wt.py authorize-main
# git merge worktree-fix-login  …（单独一次调用）
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
| 主 checkout 根存在 `.kimi/worktree-state.json`（仓库专用守卫） | ✅ 全部放行（让位） |
| 仓库外路径、非 git 目录 | ✅ 放行 |

已知边界：hook 按官方定位是**轻量拦截**而非唯一安全屏障——fail-open（脚本异常放行）。
Bash 检查能识别行首 / `&&` / `;` / `|` 之后的 git 命令，并解析前导 `cd <path> &&` 段
推算有效工作目录（git 变更类拦截只在有效 cwd 属于被守卫仓库时生效）；但更深层的 shell
语义（变量、子shell、xargs 等）不做完整解析，仍可能绕过。

## 与仓库专用守卫共存（让位规则）

若目标仓库主 checkout 根存在 `.kimi/worktree-state.json`（另一套更具体的仓库专用守卫
系统的状态文件），本插件对该仓库**完全静默放行**：hook 在任何拦截检查之前做文件存在性
检测（廉价、只读、fail-open），存在即 exit 0。共存契约：**通用守卫让位给仓库专用守卫**，
避免两套守卫重复拦截、反馈互相打架。状态栏脚本同样让位——检测到该文件时不再展示本插件
的活动副本状态，避免显示空/旧状态误导。

**锚点持久化契约**：让位规则依赖的是该状态文件的**存在性**，因此专用守卫一侧必须保证
锚点文件**持久存在**——空闲态应写为 `active: false`（保留最后一会话信息无妨），而不是
退出时删除文件。若专用守卫在 exit 后删除该文件，空闲态让位即失效，两套守卫会重复拦截
（2026-09-10 实测踩到：xkx 仓库双拦截即源于此）。

## 与仓库专用守卫（A / xkx-worktree）的关系 / 同步纪律

**B（本插件）是上游通用版。** xkx 仓库的专用守卫 A（`xkx-worktree` 技能 +
`~/.kimi-code/hooks/guard_worktree.py` + `~/.kimi-code/worktree/wt.py`）自 2026-09-10 起
重建为 **A = B（v1.1.2+）为基座 + xkx delta 重灌**（历史上 A 在先、B 是 A 的通用化抽取，
见 v1.1.1 及之前版本的说明；自 v1.1.2 起代码方向反转，以 B 为底）。

A 侧保留的 xkx delta（不属于通用层，永不回流）：

1. svn 拦截（`svn update|commit` 只允许主副本 master 且须 `authorize-master` 授权）；
2. 状态文件固定在主副本 `.kimi/worktree-state.json`（即本插件让位规则的锚点，
   **持久存在**，`active:false` 表空闲），不用 git common dir 方案；
3. 子命令命名 `authorize-master` / `revoke-master`（master-only 口径）；
4. 拦截消息与文案里的 xkx 仓库纪律说明（svn sync 只在主副本 master、AGENTS.md 等）；
5. xkx 专属 create 自动设置（编译环境：gradle-wrapper.jar / build-logic 扩展源 /
   `_shared/bin` junction 等）。

同步纪律约定：

- **通用改进一律先进 B（开发目录 `F:/workspace_2/worktree-guard`），再同步到 A**；
- **A 的 delta 改动若被证明具有通用价值，则泛化后回流 B**；
- 两侧同一逻辑（拦截正则、作用域判定、让位规则、自愈逻辑）保持语义一致，改动时在
  commit message / 复盘文档里互相标注。

## 仓库级配置：post_create_commands

主 checkout 根可选放置 `.kimi/worktree-guard.json`（JSON，兼容老 Python）：

```json
{
  "post_create_commands": [
    "cp ../gradle/wrapper/gradle-wrapper.jar gradle/wrapper/",
    "echo ok > setup_marker.txt"
  ]
}
```

`create` 完成后在新副本内逐个执行（cwd = 新副本，shell 执行，单条 300s 超时）。
**best-effort**：任何一条失败只在 create 输出的 content 里警告，不影响创建成功。
典型用途：仓库特定的编译环境准备（复制未跟踪的 gradle-wrapper.jar、config.bytes 等）。

## exit 的删除语义

- `exit(action="remove", confirm_remove=true)`：删除副本目录，分支保留；
  目录已被外部删除时**幂等成功**（照常清理登记状态并 `git worktree prune` 残留登记）。
- 追加 `delete_branch=true`（必须与 `confirm_remove=true` 同传）：删目录后校验该
  worktree 分支是否已合并进其 base 分支（`git merge-base --is-ancestor <branch> <base>`，
  base 来自登记状态 `state.json` / `bases.json`），已合并才执行 `git branch -d`；
  未合并则明确拒绝并保留分支。所有结果都在输出的 content 里报告。
- **残留清理模式**（v1.1.3）：状态文件已是 `active:false` 但仍带 path/branch 登记
  （hook 陈旧自愈所留——自愈现在改写 `active:false` 而非删文件，正为此模式保留登记）时，
  `exit` 不再拒绝，而是按登记的 path/branch 照常执行上述清理流程，输出中明确标注
  "残留清理（非正常退出）"；清理完成即清除登记。残留 + `action="keep"` 则只报告、
  登记保留（供后续 remove 一步清理）。状态文件里连 path/branch 都没有时，`exit`
  才保持拒绝（"无任何登记信息"）。

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
