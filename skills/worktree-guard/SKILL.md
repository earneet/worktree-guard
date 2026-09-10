---
name: worktree-guard
description: 强制 git worktree 工作流：创建/进入/退出 worktree 副本，配合 PreToolUse hook 硬约束只在活动副本内写文件，合并回主分支必须用户明确授权
type: prompt
whenToUse: 当用户在任何 git 仓库请求代码修改、功能开发、重构、bug 修复时，先按本流程进入 worktree 副本；也用于查询 worktree 状态或退出副本
---

# 强制 git worktree 工作流

## 核心纪律

- **所有开发任务必须在隔离的 git worktree 分支副本中进行**（分支名 `worktree-<task>`）；
- **一般修改禁止在主 checkout（任何分支）进行**；
- 合并回主分支（master/main）必须等待用户明确授权；
- 在 worktree 副本内禁止 `git checkout/switch` 到 master/main。

## 工具脚本

所有 worktree 操作通过插件内 Python 脚本执行（stdin 收 JSON，stdout 出 `{"content": ...}`）：

```bash
# 查看所有副本和活动状态（每个任务开始前先跑这个）
echo '{}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" status

# 创建 worktree（默认基于主 checkout 当前分支；拒绝在副本内嵌套创建）
echo '{"task_name": "add-drop-module"}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" create

# 可选参数：base_branch 指定基线分支；worktree_parent 指定副本父目录（默认 .worktrees）
echo '{"task_name": "fix-login", "base_branch": "main", "worktree_parent": ".worktrees"}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" create

# 进入已存在的 worktree（登记活动状态后，写文件硬约束生效；相对路径基于主 checkout 根解析）
echo '{"path": ".worktrees/worktree-add-drop-module"}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" enter

# 退出当前活动 worktree（保留副本，汇报领先提交与未提交改动）
# 残留清理模式（v1.1.3）：状态已是 active:false 但仍带 path/branch 登记（hook 自愈所留）时，
# exit 不拒绝，按登记照常走清理流程并明确标注"残留清理"；连登记都没有才拒绝
echo '{"action": "keep"}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" exit

# 删除副本（需显式确认且工作区干净；只删目录，分支保留；目录已被外部删除时幂等成功）
echo '{"action": "remove", "confirm_remove": true}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" exit

# 删除副本并一并删除分支（须与 confirm_remove 同传；仅当分支已合并进 base 才删，
# 用 git merge-base --is-ancestor 校验，未合并明确拒绝并保留分支）
echo '{"action": "remove", "confirm_remove": true, "delete_branch": true}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" exit

# 授权在主 checkout 上修改/合并（需用户明确授权后才可调用）
# ⚠️ authorize-main 与后续受守卫的变更命令（git merge 等）必须分两次工具/Bash 调用：
#    hook 在执行前对整个命令串预检，`authorize-main && git merge` 连写仍会被拦截。
echo '{"reason": "用户授权合并 worktree-add-drop-module"}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" authorize-main

# 撤销授权（授权操作完成后立即执行）
echo '{}' | python "${KIMI_SKILL_DIR}/../../scripts/wt.py" revoke-main
```

## 开发任务标准流程

1. **任务开始时**：先跑 `status` 查看是否有活动 worktree。
   - 已有活动副本 → 继续在该副本工作。
   - 没有 → `create` 创建新副本。

2. **创建副本后**：必须 `enter` 登记为活动副本，写文件硬约束才生效。

3. **在副本内工作**：
   - 所有 `Write` / `Edit` 必须使用副本内的绝对路径；
   - 写主 checkout 或其他副本会被 `PreToolUse` hook 拦截（exit 2）。

4. **任务结束时**：`exit(action="keep")` 汇报状态，**等待用户授权合并**。
   - 不要自行 `git merge` / `git rebase` 到主分支。
   - 报告口径：`worktree <name> 已就绪，待您确认是否合并`。

5. **合并 worktree 到主分支**（用户明确说"合并"后）：
   - `authorize-main` 记录授权原因 → 在主 checkout 执行合并 → 立即 `revoke-main`；
   - ⚠️ 授权与合并命令必须分两次工具/Bash 调用（hook 对整条命令串预检，连写仍被拦）；
   - 合并经用户确认后，才可 `exit(action="remove")` 清理副本目录（默认分支保留；
     分支已合并且用户确认删除时，可加 `delete_branch=true` 一并删分支）。

6. **极少数直改主 checkout 的情况**（如改仓库级文档）：
   - 先获得用户明确授权 → `authorize-main` → 修改 → 立即 `revoke-main`。

## 路径约定

- 副本默认位置：`<主 checkout>/.worktrees/worktree-<task-name>/`（自动加入 `.git/info/exclude`）
- 状态文件：git common dir 下 `worktree-guard/state.json`（天然不被 git 追踪，所有副本共享）
- 分支名：`worktree-<task-name>`，base 记录在 `worktree-guard/bases.json`
- 仓库级配置（可选）：主 checkout 根 `.kimi/worktree-guard.json`，支持
  `post_create_commands`（字符串数组）——create 完成后在新副本内逐个 best-effort 执行
  （cwd=新副本，失败只警告），用于仓库特定的编译环境准备

## 与仓库专用守卫共存（让位规则）

若目标仓库主 checkout 根存在 `.kimi/worktree-state.json`（另一套仓库专用守卫系统的
状态文件），本插件的 hook 对该仓库**完全静默放行**（通用守卫让位给专用守卫），状态栏
也不再展示本插件的活动副本状态。检测只做文件存在性判断，廉价、只读、fail-open。

**锚点持久化契约**：专用守卫一侧必须保证该锚点文件**持久存在**——空闲态写
`active: false` 而不是删除文件；否则空闲态让位失效，两套守卫会重复拦截。

本插件（B）是上游通用版；xkx 仓库的专用守卫 A = B 为基座 + xkx delta（svn 拦截、
`.kimi/` 持久锚点状态文件、`authorize-master` 命名、xkx 文案、xkx create 自动设置）。
**通用改进先进 B 再同步到 A；A 的 delta 改动若具通用价值则泛化后回流 B。**

## 纪律兜底（PreToolUse hook）

插件 hook 在每次 `Write` / `Edit` / `Bash` 调用前执行，拦截时通过 stderr 反馈完整上下文（当前分支、位置、是否在 worktree、活动 worktree、目标路径/命令）。

拦截规则：
1. 存在活动 worktree 时，禁止写入主 checkout 或其他非活动副本；
2. 无活动 worktree 时，禁止写入主 checkout 根目录内的任何文件（无论当前在哪个分支）；
3. 在受保护分支（master/main/副本 base）时，禁止未授权的 `git merge` / `git rebase` / `git pull`；
4. 在 worktree 副本内，禁止 `git checkout/switch` 到受保护分支、删除 worktree 分支；任何位置 `git push` 到 master/main 都需授权；
5. `authorize-main` 授权期间，以上全部放行。

已知边界（hooks 是轻量拦截，不是唯一安全屏障，仍需自律）：
- fail-open：hook 脚本异常/超时时放行；
- Bash 检查能识别行首/`&&`/`;`/`|` 之后的 git 命令，并解析前导 `cd <path> &&` 段推算
  有效工作目录（git 变更类拦截只在有效 cwd 属于被守卫仓库时生效）；更深层的 shell 语义
  （变量、子shell、xargs 等）不做完整解析，仍可能绕过。

触发拦截时，按 stderr 指引操作：创建/进入正确副本，或先获得用户授权。

## 与纪律的对照表

| 操作 | 允许位置 | 是否需要用户明确授权 |
|---|---|---|
| 写代码/改文件 | 活动 worktree 副本 | 否（但必须先进副本） |
| 改仓库级文档/配置 | 主 checkout | 是 |
| `git merge worktree-xxx` → 主分支 | 主 checkout | 是 |
| `git merge master` → 副本 | worktree 副本 | 否（同步基线） |
| `git push` → master/main | 主 checkout | 是 |
| `git checkout master/main` | worktree 副本内禁止 | 是 |
| 删除 worktree 分支 | 任何位置 | 是 |
