#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""worktree-guard 插件工具脚本：create / enter / exit / status / authorize-main / revoke-main。

强制 git worktree 工作流：所有开发在隔离的 worktree 副本（worktree-<task> 分支）进行，
主 checkout 默认禁止写入；合并回主分支必须用户明确授权。

通信协议：stdin 收 JSON 参数，stdout 打 {"content": ...}。
所有异常兜底为正常退出（exit 0），错误信息放在 content 里，避免阻塞 Agent。

状态存放：主 checkout 的 git common dir 下 `.git/worktree-guard/`（state.json /
override.json / bases.json）。该位置天然不被 `git add -A` 追踪，且所有 worktree
共享同一份（worktree 的 common dir 指向主 checkout 的 .git）。
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TASK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
BRANCH_PREFIX = "worktree-"
DEFAULT_PARENT = ".worktrees"
STATE_DIR_NAME = "worktree-guard"


# ---------------------------------------------------------------- 基础工具

def run_git(args, cwd, check=False):
    """跑 git 命令，返回 (returncode, stdout_stripped)。"""
    try:
        p = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} 失败: {err or out}")
        return p.returncode, out if p.returncode == 0 else (err or out)
    except FileNotFoundError:
        raise RuntimeError("找不到 git 命令（不在 PATH）")


def norm(p):
    """Windows 大小写不敏感，统一 normcase + 绝对路径用于前缀比较。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(str(p))))


def git_common_dir(cwd):
    """git common dir 的绝对路径（主 checkout 的 .git 目录）。"""
    _, common = run_git(["rev-parse", "--git-common-dir"], cwd, check=True)
    return os.path.normpath(common if os.path.isabs(common) else os.path.join(str(cwd), common))


def main_root(cwd):
    """主 checkout 根目录 = git common dir 的父目录（在任意 worktree 内调用也解析到主 checkout）。"""
    return Path(os.path.dirname(git_common_dir(cwd)))


def state_dir(cwd):
    return Path(git_common_dir(cwd)) / STATE_DIR_NAME


def _read_json(f):
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except Exception:
        return None


def _write_json(f, data):
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(cwd):
    s = _read_json(state_dir(cwd) / "state.json")
    return s if s and s.get("active") else None


def save_state(cwd, state):
    _write_json(state_dir(cwd) / "state.json", state)


def clear_state(cwd):
    f = state_dir(cwd) / "state.json"
    if f.exists():
        f.unlink()


def load_override(cwd):
    return _read_json(state_dir(cwd) / "override.json")


def save_override(cwd, override):
    _write_json(state_dir(cwd) / "override.json", override)


def clear_override(cwd):
    f = state_dir(cwd) / "override.json"
    if f.exists():
        f.unlink()


def load_bases(cwd):
    return _read_json(state_dir(cwd) / "bases.json") or {}


def save_base(cwd, branch, base):
    bases = load_bases(cwd)
    bases[branch] = base
    _write_json(state_dir(cwd) / "bases.json", bases)


def ensure_local_exclude(main, rel_path):
    """把路径追加到 .git/info/exclude（本地排除，不进版本库，所有 worktree 共享）。

    为什么不用 .gitignore：不污染仓库文件，也不要求团队成员各自维护忽略规则。
    """
    exclude = Path(git_common_dir(main)) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
    if rel_path not in existing:
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# worktree-guard 本地排除\n{rel_path}\n")
        return True
    return False


def in_linked_worktree(cwd):
    """git-dir != git-common-dir 即在 linked worktree（排除 submodule）。"""
    _, git_dir = run_git(["rev-parse", "--git-dir"], cwd, check=True)
    _, common = run_git(["rev-parse", "--git-common-dir"], cwd, check=True)
    abs_dir = norm(os.path.join(str(cwd), git_dir) if not os.path.isabs(git_dir) else git_dir)
    abs_common = norm(os.path.join(str(cwd), common) if not os.path.isabs(common) else common)
    if abs_dir == abs_common:
        return False
    rc, super_tree = run_git(["rev-parse", "--show-superproject-working-tree"], cwd)
    if rc == 0 and super_tree:
        return False  # submodule，按普通仓库对待
    return True


def registered_worktrees(root):
    """解析 git worktree list --porcelain → [{path, branch}]。"""
    _, out = run_git(["worktree", "list", "--porcelain"], root, check=True)
    result, cur = [], {}
    for line in out.splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):], "branch": ""}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line == "" and cur:
            result.append(cur)
            cur = {}
    return result


def dirty_summary(path):
    _, out = run_git(["status", "--porcelain"], path, check=True)
    lines = [l for l in out.splitlines() if l.strip()]
    return len(lines), lines[:10]


def ahead_summary(path, base):
    rc, out = run_git(["log", "--oneline", f"{base}..HEAD"], path)
    if rc != 0:
        return 0, []
    lines = [l for l in out.splitlines() if l.strip()]
    return len(lines), lines[:10]


def ok(text):
    print(json.dumps({"content": text}, ensure_ascii=False))


def fail(text):
    ok(f"❌ {text}")


# ---------------------------------------------------------------- create

def cmd_create(params, cwd):
    task = (params.get("task_name") or "").strip()
    parent = (params.get("worktree_parent") or DEFAULT_PARENT).strip().strip("/")

    if not TASK_NAME_RE.match(task):
        return fail(f"task_name 非法: '{task}'（要求 ^[a-z0-9][a-z0-9-]{{0,49}}$）")

    root = main_root(cwd)

    # 已在某个 linked worktree 内 → 拒绝嵌套创建
    if in_linked_worktree(cwd):
        _, branch = run_git(["branch", "--show-current"], cwd)
        return fail(
            f"当前已在 worktree 副本内（分支 {branch or 'detached'}）。"
            "先完成/退出当前副本（exit），不要嵌套创建。"
        )

    # base 默认 = 主 checkout 当前分支
    base = (params.get("base_branch") or "").strip()
    if not base:
        _, base = run_git(["branch", "--show-current"], root)
        base = base or "HEAD"

    branch = f"{BRANCH_PREFIX}{task}"
    wt_path = root / parent / branch

    rc, _ = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root)
    if rc == 0:
        return fail(f"分支 {branch} 已存在。如要继续该任务，用 enter 进入现有副本。")
    if wt_path.exists():
        return fail(f"目录已存在: {wt_path}。如是残留副本，请人工核查后处理。")

    # 安全校验：worktree 父目录必须被 git 忽略（防 git add -A 把副本卷进版本库）
    rc, _ = run_git(["check-ignore", "-q", f"{parent}/{branch}"], root)
    ignored = rc == 0
    if not ignored:
        ensure_local_exclude(root, f"{parent}/")

    # 创建
    try:
        run_git(["worktree", "add", str(wt_path), "-b", branch, base], root, check=True)
    except RuntimeError as e:
        return fail(f"git worktree add 失败: {e}")

    save_base(cwd, branch, base)

    lines = [f"✅ worktree 已创建\n- 路径: {wt_path}\n- 分支: {branch}（基于 {base}）"]
    if not ignored:
        lines.append(f"- 已将 {parent}/ 追加到 .git/info/exclude（本地排除，防 add -A 卷入）")
    lines.append(
        "\n下一步:\n"
        "1. enter 进入该副本后再做任何文件修改\n"
        "2. 如项目需要编译环境设置（复制未跟踪资源等），在副本内自行执行"
    )
    ok("\n".join(lines))


# ---------------------------------------------------------------- enter

def cmd_enter(params, cwd):
    raw = (params.get("path") or "").strip()
    if not raw:
        return fail("缺少 path 参数")
    root = main_root(cwd)
    # 相对路径基于主 checkout 根解析，避免在 worktree 内调用时解析到副本路径
    path = raw if os.path.isabs(raw) else os.path.join(str(root), raw)

    target = None
    for wt in registered_worktrees(root):
        if norm(wt["path"]) == norm(path):
            target = wt
            break
    if target is None:
        return fail(f"{path} 不是本仓库已注册的 worktree（见 git worktree list）。")
    if not Path(target["path"]).is_dir():
        return fail(f"worktree 目录不存在: {target['path']}（可能被人工删除，先 git worktree prune）")

    branch = target["branch"]
    if not branch:
        _, branch = run_git(["branch", "--show-current"], target["path"])

    base = load_bases(cwd).get(branch) or "master"

    state = {
        "active": True,
        "path": str(Path(target["path"]).resolve()),
        "branch": branch,
        "base": base,
        "entered_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(cwd, state)
    ok(
        f"✅ 已进入 worktree（活动副本已登记，写文件硬约束生效）\n"
        f"- 路径: {state['path']}\n- 分支: {branch}\n\n"
        "纪律提醒：\n"
        "- 所有 Write/Edit 必须用该副本内的绝对路径；写主 checkout/其他副本会被 hook 拒绝\n"
        "- 完成后用 exit 退出；合并回主分支必须等用户明确授权"
    )


# ---------------------------------------------------------------- exit

def cmd_exit(params, cwd):
    action = params.get("action", "keep")
    confirm_remove = params.get("confirm_remove", False)
    state = load_state(cwd)
    if not state:
        return fail("当前没有活动 worktree（状态文件不存在或已退出）。")

    path, branch = state["path"], state.get("branch", "")
    base = state.get("base", "master")
    lines = [f"退出 worktree: {path}（分支 {branch}，基于 {base}）"]

    if Path(path).is_dir():
        n_dirty, dirty = dirty_summary(path)
        n_ahead, ahead = ahead_summary(path, base)
        lines.append(f"- 领先 {base} 的提交: {n_ahead} 个" + ("" if not ahead else "\n  " + "\n  ".join(ahead)))
        lines.append(f"- 未提交改动: {n_dirty} 个文件" + ("" if not dirty else "\n  " + "\n  ".join(dirty)))
        if n_dirty:
            lines.append("⚠️ 有未提交改动！建议先在副本内提交（git add <具体文件> 精确提交）。")
    else:
        n_dirty = 0
        lines.append("⚠️ 副本目录已不存在。")

    if action == "remove":
        if not confirm_remove:
            return fail("action=remove 需要 confirm_remove=true 显式确认。\n" + "\n".join(lines))
        if n_dirty:
            return fail("工作区有未提交改动，拒绝删除。请先提交或人工清理。\n" + "\n".join(lines))
        rc, out = run_git(["worktree", "remove", path], main_root(cwd))
        if rc != 0:
            return fail(f"git worktree remove 失败: {out}\n" + "\n".join(lines))
        lines.append(f"🗑️ 副本目录已删除（分支 {branch} 保留；删分支需用户明确授权后人工执行）")

    clear_state(cwd)
    lines.append("\n✅ 活动状态已清除。")
    if action == "keep":
        lines.append(
            f"📌 报告口径：worktree `{branch}` 已就绪，待您确认是否合并。"
            "未获用户明确授权前，禁止 merge / rebase 进主分支，也禁止删除分支。"
        )
    ok("\n".join(lines))


# ---------------------------------------------------------------- status

def cmd_status(params, cwd):
    root = main_root(cwd)
    lines = [f"主 checkout: {root}", "", "已注册 worktree:"]
    for wt in registered_worktrees(root):
        exists = "✅" if Path(wt["path"]).is_dir() else "❌ 目录缺失"
        lines.append(f"- {wt['path']}  [{wt['branch'] or 'detached'}]  {exists}")

    state = load_state(cwd)
    lines.append("")
    if state:
        base = state.get("base", "master")
        lines.append(f"活动 worktree: {state['path']}（分支 {state.get('branch')}，基于 {base}，进入于 {state.get('entered_at')}）")
        if Path(state["path"]).is_dir():
            n_dirty, _ = dirty_summary(state["path"])
            n_ahead, _ = ahead_summary(state["path"], base)
            lines.append(f"  领先 {base} {n_ahead} 个提交，未提交改动 {n_dirty} 个文件")
        lines.append("写文件硬约束：生效中（只允许写活动副本内文件）")
    else:
        lines.append("活动 worktree: 无（写文件硬约束未启用）")

    override = load_override(cwd)
    if override and override.get("allow_main_writes"):
        reason = override.get("reason") or "未说明"
        lines.append(f"⚠️ 主 checkout 写入授权: 已启用（原因: {reason}）")
    else:
        lines.append("主 checkout 写入授权: 未启用（默认禁止在主 checkout 写入项目文件）")
    ok("\n".join(lines))


# ---------------------------------------------------------------- authorize / revoke

def cmd_authorize(params, cwd):
    reason = (params.get("reason") or "用户授权").strip()
    override = {
        "allow_main_writes": True,
        "reason": reason,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_override(cwd, override)
    ok(
        f"✅ 已授权在主 checkout 写入项目文件\n"
        f"原因: {reason}\n"
        "注意：授权期间写文件硬约束对主 checkout 放行，完成修改后应立即 revoke-main。"
    )


def cmd_revoke(params, cwd):
    clear_override(cwd)
    ok("✅ 已撤销主 checkout 写入授权。后续在主 checkout 写入项目文件将再次被拦截。")


# ---------------------------------------------------------------- main

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        raw = sys.stdin.read().lstrip("\ufeff")
        params = json.loads(raw) if raw.strip() else {}
    except Exception:
        params = {}
    cwd = Path(os.getcwd())

    handlers = {
        "create": cmd_create,
        "enter": cmd_enter,
        "exit": cmd_exit,
        "status": cmd_status,
        "authorize-main": cmd_authorize,
        "revoke-main": cmd_revoke,
    }
    handler = handlers.get(action)
    if handler is None:
        return fail(f"未知子命令 '{action}'（可用: {', '.join(handlers)}）")
    try:
        handler(params, cwd)
    except Exception as e:  # 兜底：永不非零退出，避免阻塞 Agent
        fail(f"工具内部错误: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
