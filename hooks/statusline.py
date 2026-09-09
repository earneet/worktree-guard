#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kimi Code TUI status_line 自定义脚本（worktree-guard 版）。

显示口径：优先展示"Agent 正在工作的活动 worktree"（读 git common dir 下
worktree-guard/state.json），而不是会话启动时的静态 cwd —— 这样即使会话开在
主 checkout，状态栏也能反映真实写入位置。

性能约束：CLI 对 status_line 命令有 300ms 上限，因此本脚本**不启动任何子进程**，
全部通过读文件完成（.git 文件 / HEAD / state.json），超时或异常时 CLI
自动回退到内置布局（fail-open）。

安装方式（插件无法代为配置，需手工加一行到 ~/.kimi-code/tui.toml）：
    [status_line]
    command = "python <插件目录>/hooks/statusline.py"
"""
import json
import os
import sys
from pathlib import Path

STATE_DIR_NAME = "worktree-guard"


def read_head_branch(git_path):
    """从 .git/HEAD 解析分支名；git_path 是 .git 目录（或 worktree 的 gitdir）。"""
    try:
        head = (git_path / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            return head[4:].strip().replace("refs/heads/", "")
        return head[:8] if head else ""  # detached
    except Exception:
        return ""


def find_repo(cwd):
    """向上找 .git。返回 (git_common_dir, worktree_gitdir_or_None)。

    worktree 的 .git 是文本文件（gitdir: <main>/.git/worktrees/<name>），
    直接解析即可定位 common dir，无需调用 git 子进程。
    """
    try:
        p = Path(cwd).resolve()
    except Exception:
        return None, None
    for d in [p, *p.parents]:
        g = d / ".git"
        if g.is_dir():
            return g, None
        if g.is_file():
            try:
                content = g.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                return None, None
            if content.startswith("gitdir:"):
                gitdir = Path(content[len("gitdir:"):].strip())
                if not gitdir.is_absolute():
                    gitdir = (d / gitdir).resolve()
                # gitdir = <main>/.git/worktrees/<name> → common dir = 上两级
                return gitdir.parent.parent, gitdir
            return None, None
    return None, None


def shorten_path(path, max_len=60):
    path = str(path).replace("\\", "/")
    if len(path) <= max_len:
        return path
    parts = path.split("/")
    if len(parts) <= 4:
        return path
    return f"{parts[0]}/{parts[1]}/.../{'/'.join(parts[-3:])}"


def model_name(ctx):
    m = ctx.get("model")
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        return m.get("display_name") or m.get("name") or m.get("model") or m.get("id") or ""
    return ""


def snapshot_branch(ctx):
    b = ctx.get("git_branch") or ctx.get("branch")
    if isinstance(b, str) and b:
        return b
    g = ctx.get("git")
    if isinstance(g, dict):
        return g.get("branch") or ""
    if isinstance(g, str):
        return g
    return ""


def main():
    raw = sys.stdin.read().lstrip("\ufeff")
    ctx = json.loads(raw) if raw.strip() else {}
    cwd = ctx.get("cwd") or os.getcwd()
    prefix = (model_name(ctx) + " ") if model_name(ctx) else ""

    common, wt_gitdir = find_repo(cwd)

    # 让位规则（与 guard_worktree.py 同契约）：主 checkout 根存在仓库专用守卫的
    # 状态文件（.kimi/worktree-state.json）时，本插件的活动状态不再展示，
    # 避免两套守卫并存时状态栏显示本插件的空/旧状态造成误导。
    yield_to_repo_guard = False
    if common:
        try:
            yield_to_repo_guard = (common.parent / ".kimi" / "worktree-state.json").exists()
        except Exception:
            pass

    # 1) 有活动 worktree 登记 → 状态栏显示活动副本（Agent 真实工作位置）
    if common and not yield_to_repo_guard:
        state = None
        try:
            state = json.loads((common / STATE_DIR_NAME / "state.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        if state and state.get("active") and state.get("path"):
            wt_path = str(state["path"]).replace("\\", "/")
            branch = state.get("branch") or snapshot_branch(ctx) or "?"
            wt_name = wt_path.rstrip("/").split("/")[-1]
            print(f"{prefix}⛏ {shorten_path(wt_path)} ({wt_name}) [{branch}]")
            return

    # 2) 无活动副本 → 显示会话 cwd + 当前分支
    branch = snapshot_branch(ctx)
    if not branch:
        if wt_gitdir:
            branch = read_head_branch(wt_gitdir)
        elif common:
            branch = read_head_branch(common)
    branch = branch or "?"
    print(f"{prefix}{shorten_path(cwd)} [{branch}]")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("")
