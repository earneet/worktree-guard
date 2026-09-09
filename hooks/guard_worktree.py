#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PreToolUse hook：强制 git worktree 工作流的硬约束兜底。

策略：
1. 存在活动 worktree 时，只允许写入活动副本内；写主 checkout/其他副本拒绝。
2. 无活动 worktree 时，禁止写入主 checkout 根目录内的任何文件（无论当前在哪个分支）。
3. 用户明确授权后，可通过 override 文件放行主 checkout 写入/合并。
4. 在受保护分支（master / main / 活动副本记录的 base 分支）上，
   拦截 git merge/rebase/pull，防止未授权把 worktree 分支合入。
5. 在 worktree 副本内，拦截 git checkout/switch 到受保护分支、删除 worktree 分支、
   push 到 master/main 等违反工作流的操作。
6. 让位规则：主 checkout 根存在 .kimi/worktree-state.json（仓库专用守卫的状态文件）
   时，本 hook 对该仓库完全静默放行——通用守卫让位给专用守卫。

所有拦截都通过 stderr 把完整上下文（当前分支、位置、活动 worktree、目标路径/命令）
反馈给 LLM，确保模型在操作前一定意识到自己在哪、该往哪写。

插件配置（kimi.plugin.json 内，cwd = 插件根目录）：
    {
      "event": "PreToolUse",
      "matcher": "Write|Edit|Bash",
      "command": "python ./hooks/guard_worktree.py",
      "timeout": 10
    }

已知边界（hooks 按官方定位是提醒/轻量拦截，不是唯一安全屏障）：
- fail-open：脚本异常、超时、找不到 git 时都放行；
- Bash 正则只匹配以 git 直接开头的简单命令，`cd x && git merge` 这类组合命令可能绕过。

设计原则：fail-open —— 任何内部异常都放行（exit 0），绝不阻塞 Agent 正常工作。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATE_DIR_NAME = "worktree-guard"
BRANCH_PREFIX = "worktree-"
DEFAULT_PROTECTED = ("master", "main")
# 与本脚本同包的 wt.py（hooks/ 的兄弟目录 scripts/）
WT_TOOL = str(Path(__file__).resolve().parent.parent / "scripts" / "wt.py")

# git 全局选项（-C <path> / -c <k=v>）可出现于子命令之前，正则统一容忍
_GIT_PREFIX = r"\bgit\s+(?:(?:-C|-c)\s+\S+\s+)*"
GIT_MUTATE_RE = re.compile(_GIT_PREFIX + r"(merge|rebase|pull)\b", re.IGNORECASE)
GIT_MERGE_TARGET_RE = re.compile(
    _GIT_PREFIX + r"(merge|rebase)\s+(" + BRANCH_PREFIX + r"[^\s;|&\"'<>()]+)\b",
    re.IGNORECASE,
)
GIT_PUSH_PROTECTED_RE = re.compile(r"\bgit\s+push\b.*\b(master|main)\b", re.IGNORECASE)
GIT_PUSH_DEFAULT_RE = re.compile(r"^\s*git\s+push\s*$", re.IGNORECASE)
GIT_DEL_WORKTREE_RE = re.compile(
    r"\bgit\s+branch\s+(-[dD])\s+(" + BRANCH_PREFIX + r"[^\s;|&\"'<>()]+)\b",
    re.IGNORECASE,
)
GIT_CHECKOUT_RE = re.compile(_GIT_PREFIX + r"(checkout|switch)\s+([^\s;|&\"'<>()-][^\s;|&\"'<>()]*)", re.IGNORECASE)


def norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def run_git(args, cwd):
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        return p.returncode, (p.stdout or "").strip()
    except Exception:
        return -1, ""


def git_common_dir(cwd):
    rc, common = run_git(["rev-parse", "--git-common-dir"], cwd)
    if rc != 0 or not common:
        return None
    return os.path.normpath(common if os.path.isabs(common) else os.path.join(cwd, common))


def current_branch(cwd):
    rc, out = run_git(["branch", "--show-current"], cwd)
    return out if rc == 0 and out else "(detached HEAD)"


def in_linked_worktree(cwd):
    rc, git_dir = run_git(["rev-parse", "--git-dir"], cwd)
    if rc != 0:
        return False
    rc, common = run_git(["rev-parse", "--git-common-dir"], cwd)
    if rc != 0:
        return False
    abs_dir = norm(os.path.join(str(cwd), git_dir) if not os.path.isabs(git_dir) else git_dir)
    abs_common = norm(os.path.join(str(cwd), common) if not os.path.isabs(common) else common)
    if abs_dir == abs_common:
        return False
    rc, super_tree = run_git(["rev-parse", "--show-superproject-working-tree"], cwd)
    if rc == 0 and super_tree:
        return False
    return True


def _read_json(f):
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except Exception:
        return None


def load_state(common):
    s = _read_json(Path(common) / STATE_DIR_NAME / "state.json")
    return s if s and s.get("active") else None


def load_override(common):
    return _read_json(Path(common) / STATE_DIR_NAME / "override.json")


def block(reason, ctx):
    target_or_cmd = ctx.get("target") or ctx.get("command") or "(无)"
    sys.stderr.write(
        "\n"
        "🔴🔴🔴 worktree-guard 硬约束拦截 🔴🔴🔴\n"
        "\n"
        "当前上下文（操作前请务必确认）：\n"
        f"  当前分支: {ctx['branch']}\n"
        f"  当前位置: {ctx['cwd']}\n"
        f"  是否在 linked worktree: {'是' if ctx['in_worktree'] else '否'}\n"
        f"  主 checkout 根目录: {ctx['root']}\n"
        f"  活动 worktree: {ctx['active_wt'] or '无'}\n"
        f"  目标/命令: {target_or_cmd}\n"
        "\n"
        f"拦截原因: {reason}\n"
        "\n"
        "修正方式（按推荐顺序）：\n"
        "1. 若这是普通开发任务 → 先 create 创建副本，再 enter 进入，然后写入副本内的对应路径；\n"
        "2. 若已存在目标副本 → 先 exit 清除旧状态，再 enter 进入目标副本；\n"
        "3. 若用户明确授权在主 checkout 上修改或合并 →\n"
        f"   运行: echo '{{\"reason\": \"用户授权 XXX\"}}' | python {WT_TOOL} authorize-main\n"
        f"   完成后运行: echo '{{}}' | python {WT_TOOL} revoke-main\n"
        f"（工具用法详见 worktree-guard 技能；脚本: python {WT_TOOL} <create|enter|exit|status>，stdin 传 JSON）\n"
    )
    sys.exit(2)


def check_write_tool(tool_input, context):
    """检查 Write / Edit 的目标路径。"""
    target = tool_input.get("path") or ""
    if not target:
        return

    cwd = context["cwd"]
    root = context["root"]
    target_abs = target if os.path.isabs(target) else os.path.join(cwd, target)
    n_target = norm(target_abs)
    n_root = norm(root)
    inside_root = n_target == n_root or n_target.startswith(n_root + os.sep)

    # 仓库外路径一律放行
    if not inside_root:
        return

    context["target"] = target_abs
    active_wt = context.get("_active_wt")

    # 用户已明确授权主 checkout 操作 → 放行（与 Bash 检查口径一致；
    # 授权合并 worktree 时必须能在主 checkout 内解决冲突写文件）
    if context.get("_allow_main"):
        return

    # 情况1：存在活动 worktree → 只能写活动副本内
    if active_wt:
        n_wt = norm(active_wt["path"])
        if n_target == n_wt or n_target.startswith(n_wt + os.sep):
            return  # 写在活动副本内 → 放行
        block(
            f"当前有活动 worktree（{active_wt['path']}，分支 {active_wt.get('branch', '?')}），"
            "禁止写入主 checkout 或其他非活动副本内的文件。",
            context,
        )

    # 情况2：无活动 worktree 且未授权 → 禁止写入主 checkout 根目录内的任何文件
    block(
        "当前无活动 worktree，且未授权。按工作流约定，一般修改禁止在主 checkout（任何分支）进行。",
        context,
    )


def check_bash_tool(tool_input, context):
    """检查 Bash 命令，防止违反 worktree 工作流的操作。"""
    command = tool_input.get("command") or ""
    if not command:
        return

    context["command"] = command
    branch = context["branch"]
    branch_l = branch.lower()
    protected = context["_protected"]
    in_worktree = context["in_worktree"]

    # 已授权主 checkout 操作 → 放行
    if context.get("_allow_main"):
        return

    # ---- 1. git push 到 master/main 需要授权 ----
    if GIT_PUSH_PROTECTED_RE.search(command) or (GIT_PUSH_DEFAULT_RE.search(command) and branch_l in protected):
        block("git push 到受保护分支（master/main），必须用户明确授权。", context)

    # ---- 2. worktree 副本内禁止切换到受保护分支 ----
    if in_worktree:
        m = GIT_CHECKOUT_RE.search(command)
        if m and m.group(2).lower() in protected:
            block(
                f"在 worktree 副本内禁止执行 git checkout/switch {m.group(2)}。"
                f"副本应始终工作在 {BRANCH_PREFIX}<task> 分支；如需切换任务，先 exit 再 enter。",
                context,
            )

    # ---- 3. 删除 worktree 分支需要授权 ----
    if GIT_DEL_WORKTREE_RE.search(command):
        block(
            "删除 worktree 分支必须用户明确授权。通常通过 exit(action='remove') 删除副本目录，"
            "分支保留；如需删除分支，请先确认。",
            context,
        )

    # ---- 4. git merge / rebase / pull 检查 ----
    if GIT_MUTATE_RE.search(command):
        if branch_l in protected:
            block(
                f"当前在受保护分支 {branch}，git merge / rebase / pull 会改变其历史或内容。"
                "把 worktree 分支合并进来必须获得用户明确授权。",
                context,
            )
        m = GIT_MERGE_TARGET_RE.search(command)
        if m:
            block(
                f"检测到尝试把 {m.group(2)} 合并到当前分支 {branch}。"
                "worktree 分支只能合并到主分支，且必须用户明确授权。",
                context,
            )

    # git merge master / git rebase master 在当前分支是同步基线，放行


def main():
    raw = sys.stdin.read().lstrip("\ufeff")
    ctx = json.loads(raw) if raw.strip() else {}
    cwd = ctx.get("cwd") or os.getcwd()

    common = git_common_dir(cwd)
    if common is None:
        return  # 非 git 仓库 → 放行

    # ---- 让位规则：与仓库级专用守卫共存 ----
    # 若主 checkout 根存在 .kimi/worktree-state.json，说明该仓库部署了另一套
    # 更具体的仓库专用守卫系统（那是它的状态文件）。共存契约：通用守卫让位给
    # 专用守卫，本 hook 对该仓库完全静默放行，避免两套守卫重复拦截、反馈互相打架。
    # 检测只做文件存在性判断：廉价、只读，且整体 fail-open（外层异常兜底照常放行）。
    if (Path(os.path.dirname(common)) / ".kimi" / "worktree-state.json").exists():
        return

    tool_name = ctx.get("tool_name") or ""
    tool_input = ctx.get("tool_input") or {}

    branch = current_branch(cwd)
    in_wt = in_linked_worktree(cwd)
    active_wt = load_state(common)
    override = load_override(common)
    allow_main = bool(override.get("allow_main_writes")) if override else False

    protected = set(DEFAULT_PROTECTED)
    if active_wt and active_wt.get("base"):
        protected.add(active_wt["base"].lower())

    context = {
        "cwd": cwd,
        "root": str(Path(os.path.dirname(common))),
        "branch": branch,
        "in_worktree": in_wt,
        "active_wt": active_wt["path"] if active_wt else None,
        "_active_wt": active_wt,
        "_allow_main": allow_main,
        "_protected": protected,
    }

    if tool_name in ("Write", "Edit"):
        check_write_tool(tool_input, context)
    elif tool_name == "Bash":
        check_bash_tool(tool_input, context)
    # 其他工具放行


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open
    sys.exit(0)
