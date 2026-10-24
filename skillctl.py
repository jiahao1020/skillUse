"""
skillctl - 本地 Agent 技能统一扫描与管理工具

一键扫描你本地所有 Agent 的技能，统一查看、对比、同步，
并通过 Windows 目录联接（Junction）实现「单一中心池 + 各 Agent 统一路由」。

用法:
    python skillctl.py scan                   # 扫描所有 Agent 的技能
    python skillctl.py status                 # 查看技能链接状态（哪些已集中化）
    python skillctl.py list                   # 列出所有技能（按名称排序）
    python skillctl.py list --agent gemini    # 只看某个 Agent 的技能
    python skillctl.py search <关键词>         # 搜索技能
    python skillctl.py shared                 # 查看多个 Agent 共有的技能
    python skillctl.py info <技能名>           # 查看技能详情
    python skillctl.py diff <技能名>           # 对比不同 Agent 中同名技能的差异
    python skillctl.py link <技能名>           # 将指定技能集中到共享池并建立所有 Agent 联接
    python skillctl.py unlink <技能名>         # 解除联接，恢复为独立本地副本
    python skillctl.py centralize             # 【一键集中化】所有技能移至共享池，各 Agent 自动建立联接
    python skillctl.py sync <技能名> --from <agent> --to <agent>  # 复制同步技能
    python skillctl.py export <技能名> --from <agent>             # 导出技能到共享池
    python skillctl.py install <技能名> --to <agent>              # 从共享池安装技能
"""

import os
import re
import sys
import json
import shutil
import hashlib
import argparse
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


# ============================================================
# 配置：各 Agent 的技能目录位置和扫描规则
# ============================================================

HOME = Path.home()

# 共享技能池目录
SHARED_POOL = HOME / ".agent-skills-pool"

AGENT_CONFIGS = {
    "gemini": {
        "name": "Gemini / Antigravity",
        "skill_dirs": [
            HOME / ".gemini" / "config" / "plugins",
            HOME / ".gemini" / "antigravity" / "builtin" / "skills",
        ],
        "scan_mode": "recursive_skill_md",  # 递归查找所有 SKILL.md
        "install_dir": HOME / ".gemini" / "antigravity" / "builtin" / "skills",
    },
    "claude": {
        "name": "Claude Code",
        "skill_dirs": [
            HOME / ".claude" / "skills",
        ],
        "scan_mode": "direct_subdir",  # 直接子目录就是技能
        "install_dir": HOME / ".claude" / "skills",
    },
    "qoder": {
        "name": "Qoder",
        "skill_dirs": [
            HOME / ".qoder" / "skills",
            HOME / ".qoder" / "workbuddy-skills",
        ],
        "scan_mode": "direct_subdir",
        "install_dir": HOME / ".qoder" / "skills",
    },
    "workbuddy": {
        "name": "WorkBuddy",
        "skill_dirs": [
            HOME / ".workbuddy" / "skills",
        ],
        "scan_mode": "direct_subdir",
        "install_dir": HOME / ".workbuddy" / "skills",
    },
    "zcode": {
        "name": "ZCode",
        "skill_dirs": [
            HOME / ".agents" / "skills",
            HOME / ".zcode" / "skills",
        ],
        "scan_mode": "direct_subdir",
        "install_dir": HOME / ".agents" / "skills",
    },
    "openclaw": {
        "name": "OpenClaw / AutoClaw",
        "skill_dirs": [
            HOME / ".openclaw-autoclaw" / "skills",
        ],
        "scan_mode": "direct_subdir",
        "install_dir": HOME / ".openclaw-autoclaw" / "skills",
    },
    "codebuddy": {
        "name": "CodeBuddy",
        "skill_dirs": [
            HOME / ".codebuddy" / "skills-marketplace" / "skills",
        ],
        "scan_mode": "direct_subdir",
        "install_dir": HOME / ".codebuddy" / "skills-marketplace" / "skills",
    },
    "hermes": {
        "name": "Hermes",
        "skill_dirs": [
            HOME / "AppData" / "Local" / "hermes" / "skills",
            HOME / ".hermes" / "plugins",
        ],
        "scan_mode": "recursive_skill_md",
        "install_dir": HOME / "AppData" / "Local" / "hermes" / "skills" / "custom",
    },
    "cc-switch": {
        "name": "CC-Switch",
        "skill_dirs": [
            HOME / ".cc-switch" / "skills",
        ],
        "scan_mode": "direct_subdir",
        "install_dir": HOME / ".cc-switch" / "skills",
    },
    "cline": {
        "name": "Cline",
        "skill_dirs": [
            HOME / ".cline" / "skills",
        ],
        "scan_mode": "direct_subdir",
        "install_dir": HOME / ".cline" / "skills",
    },
}


# ============================================================
# 数据模型
# ============================================================

@dataclass
class SkillInfo:
    """扫描到的技能信息"""
    name: str
    agent: str                           # 所属 Agent
    path: Path                           # 技能目录路径
    description: str = ""
    version: str = ""
    tags: list = field(default_factory=list)
    file_count: int = 0                  # 技能目录下的文件数
    total_size: int = 0                  # 总文件大小(bytes)
    content_hash: str = ""               # SKILL.md 内容哈希（用于对比）
    raw_frontmatter: dict = field(default_factory=dict)


# ============================================================
# 技能扫描器
# ============================================================

def parse_skill_md(filepath: Path) -> dict:
    """解析 SKILL.md 的 YAML frontmatter"""
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}

    # 提取 YAML frontmatter
    match = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not match:
        return {"_content": text[:200]}

    frontmatter = {}
    for line in match.group(1).split("\n"):
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if val:
                frontmatter[key] = val

    frontmatter["_content"] = text[:500]
    return frontmatter


def compute_hash(filepath: Path) -> str:
    """计算文件内容哈希"""
    try:
        content = filepath.read_bytes()
        return hashlib.md5(content).hexdigest()[:12]
    except Exception:
        return ""


def dir_stats(dirpath: Path) -> tuple:
    """统计目录下的文件数和总大小"""
    count = 0
    total = 0
    try:
        for f in dirpath.rglob("*"):
            if f.is_file():
                count += 1
                total += f.stat().st_size
    except Exception:
        pass
    return count, total


def scan_agent(agent_key: str, config: dict) -> list:
    """扫描单个 Agent 的所有技能"""
    skills = []

    for skill_dir in config["skill_dirs"]:
        if not skill_dir.exists():
            continue

        if config["scan_mode"] == "recursive_skill_md":
            # 递归查找所有 SKILL.md 文件
            for skill_md in skill_dir.rglob("SKILL.md"):
                # 忽略隐藏目录或备份目录
                rel_parts = skill_md.relative_to(skill_dir).parts
                if any(p.startswith(".") or p.startswith("__") for p in rel_parts):
                    continue
                skill_path = skill_md.parent
                meta = parse_skill_md(skill_md)
                fcount, fsize = dir_stats(skill_path)

                skills.append(SkillInfo(
                    name=meta.get("name", skill_path.name),
                    agent=agent_key,
                    path=skill_path,
                    description=meta.get("description", "")[:120],
                    version=meta.get("version", ""),
                    tags=meta.get("tags", "").split(",") if isinstance(meta.get("tags"), str) else [],
                    file_count=fcount,
                    total_size=fsize,
                    content_hash=compute_hash(skill_md),
                    raw_frontmatter=meta,
                ))

        elif config["scan_mode"] == "direct_subdir":
            # 直接子目录就是技能
            for subdir in sorted(skill_dir.iterdir()):
                if not subdir.is_dir() or subdir.name.startswith("."):
                    continue

                skill_md = subdir / "SKILL.md"
                meta = {}
                content_hash = ""

                if skill_md.exists():
                    meta = parse_skill_md(skill_md)
                    content_hash = compute_hash(skill_md)

                fcount, fsize = dir_stats(subdir)

                skills.append(SkillInfo(
                    name=meta.get("name", subdir.name),
                    agent=agent_key,
                    path=subdir,
                    description=meta.get("description", "")[:120],
                    version=meta.get("version", ""),
                    tags=meta.get("tags", "").split(",") if isinstance(meta.get("tags"), str) else [],
                    file_count=fcount,
                    total_size=fsize,
                    content_hash=content_hash,
                    raw_frontmatter=meta,
                ))

    return skills


def scan_all() -> list:
    """扫描所有 Agent 的技能"""
    all_skills = []
    for agent_key, config in AGENT_CONFIGS.items():
        skills = scan_agent(agent_key, config)
        all_skills.extend(skills)
    return all_skills


# ============================================================
# 输出格式化
# ============================================================

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def print_table(headers: list, rows: list, max_widths: dict = None):
    """打印格式化表格"""
    if not rows:
        print("  (无结果)")
        return

    # 计算列宽
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            cell_str = str(cell)
            # 简单处理中文宽度
            width = sum(2 if ord(c) > 127 else 1 for c in cell_str)
            col_widths[i] = max(col_widths[i], width)

    # 应用最大宽度限制
    if max_widths:
        for i, w in max_widths.items():
            col_widths[i] = min(col_widths[i], w)

    # 打印表头
    header_line = "  ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers))
    print(f"  {header_line}")
    print(f"  {'  '.join('-' * w for w in col_widths)}")

    # 打印数据行
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cell_str = str(cell)[:col_widths[i]]
            cells.append(cell_str.ljust(col_widths[i]))
        print(f"  {'  '.join(cells)}")


# ============================================================
# 命令实现
# ============================================================

def cmd_scan(args):
    """扫描所有 Agent 的技能"""
    print("\n🔍 正在扫描本地所有 Agent 的技能...\n")

    total = 0
    for agent_key, config in AGENT_CONFIGS.items():
        agent_name = config["name"]
        skills = scan_agent(agent_key, config)
        count = len(skills)
        total += count

        if count > 0:
            print(f"  ✅ {agent_name:<25} {count:>4} 个技能")
        else:
            dirs_exist = any(d.exists() for d in config["skill_dirs"])
            if dirs_exist:
                print(f"  ⚪ {agent_name:<25}    0 个技能")
            else:
                print(f"  ❌ {agent_name:<25}    未安装 / 目录不存在")

    print(f"\n  {'─' * 40}")
    print(f"  📊 共扫描到 {total} 个技能\n")


def cmd_list(args):
    """列出技能"""
    all_skills = scan_all()

    if args.agent:
        all_skills = [s for s in all_skills if s.agent == args.agent]
        if not all_skills:
            print(f"\n  ❌ Agent '{args.agent}' 没有找到技能。可用 Agent: {list(AGENT_CONFIGS.keys())}\n")
            return

    all_skills.sort(key=lambda s: (s.agent, s.name))

    print(f"\n📋 技能列表 ({len(all_skills)} 个)\n")

    headers = ["Agent", "技能名", "版本", "文件数", "大小", "描述"]
    rows = []
    for s in all_skills:
        rows.append([
            s.agent,
            s.name,
            s.version or "-",
            str(s.file_count),
            format_size(s.total_size),
            s.description[:50] if s.description else "-",
        ])

    print_table(headers, rows, max_widths={5: 50})
    print()


def cmd_search(args):
    """搜索技能"""
    keyword = args.keyword.lower()
    all_skills = scan_all()

    matched = [
        s for s in all_skills
        if keyword in s.name.lower()
        or keyword in s.description.lower()
        or keyword in str(s.tags).lower()
    ]

    matched.sort(key=lambda s: s.name)

    print(f"\n🔎 搜索 \"{args.keyword}\" — 找到 {len(matched)} 个技能\n")

    headers = ["Agent", "技能名", "描述"]
    rows = [[s.agent, s.name, s.description[:60] or "-"] for s in matched]
    print_table(headers, rows, max_widths={2: 60})
    print()


def cmd_shared(args):
    """查看多个 Agent 共有的同名技能"""
    all_skills = scan_all()

    # 按技能名分组
    name_map: dict[str, list[SkillInfo]] = {}
    for s in all_skills:
        name_map.setdefault(s.name, []).append(s)

    # 找出存在于 2 个以上 Agent 的技能
    shared = {
        name: skills for name, skills in name_map.items()
        if len(set(s.agent for s in skills)) >= 2
    }

    if not shared:
        print("\n  ℹ️  没有发现多个 Agent 共有的同名技能。\n")
        return

    print(f"\n🔗 跨 Agent 共享的技能 ({len(shared)} 个)\n")

    headers = ["技能名", "所在 Agent", "内容是否一致"]
    rows = []
    for name in sorted(shared.keys()):
        skills = shared[name]
        agents = ", ".join(s.agent for s in skills)
        hashes = set(s.content_hash for s in skills if s.content_hash)
        same = "✅ 一致" if len(hashes) <= 1 else "⚠️ 不一致"
        rows.append([name, agents, same])

    print_table(headers, rows)
    print()


def cmd_info(args):
    """查看技能详情"""
    all_skills = scan_all()
    matched = [s for s in all_skills if s.name == args.skill_name]

    if not matched:
        # 模糊匹配
        fuzzy = [s for s in all_skills if args.skill_name.lower() in s.name.lower()]
        if fuzzy:
            print(f"\n  ❌ 未找到精确匹配 \"{args.skill_name}\"，你是不是想找:\n")
            for s in fuzzy[:10]:
                print(f"     - {s.name} ({s.agent})")
        else:
            print(f"\n  ❌ 未找到技能 \"{args.skill_name}\"\n")
        return

    for s in matched:
        print(f"\n{'═' * 60}")
        print(f"  📦 {s.name}")
        print(f"{'─' * 60}")
        print(f"  Agent:       {s.agent} ({AGENT_CONFIGS[s.agent]['name']})")
        print(f"  路径:        {s.path}")
        print(f"  版本:        {s.version or '未标注'}")
        print(f"  描述:        {s.description or '无'}")
        print(f"  文件数:      {s.file_count}")
        print(f"  总大小:      {format_size(s.total_size)}")
        print(f"  内容哈希:    {s.content_hash or '无 SKILL.md'}")

        # 列出目录下的文件
        print(f"  文件列表:")
        try:
            for f in sorted(s.path.rglob("*")):
                if f.is_file():
                    rel = f.relative_to(s.path)
                    print(f"     📄 {rel} ({format_size(f.stat().st_size)})")
        except Exception:
            pass

    print(f"\n{'═' * 60}\n")


def cmd_diff(args):
    """对比不同 Agent 中同名技能的差异"""
    all_skills = scan_all()
    matched = [s for s in all_skills if s.name == args.skill_name]

    if len(matched) < 2:
        print(f"\n  ℹ️  技能 \"{args.skill_name}\" 只存在于 {len(matched)} 个 Agent 中，无法对比。\n")
        if matched:
            print(f"     所在 Agent: {matched[0].agent}\n")
        return

    print(f"\n📊 对比技能 \"{args.skill_name}\" 在不同 Agent 中的差异\n")

    headers = ["属性", *[s.agent for s in matched]]
    rows = [
        ["版本", *[s.version or "-" for s in matched]],
        ["文件数", *[str(s.file_count) for s in matched]],
        ["大小", *[format_size(s.total_size) for s in matched]],
        ["内容哈希", *[s.content_hash or "-" for s in matched]],
        ["路径", *[str(s.path)[:40] for s in matched]],
    ]

    print_table(headers, rows)

    # 内容是否一致
    hashes = set(s.content_hash for s in matched if s.content_hash)
    if len(hashes) <= 1:
        print(f"\n  ✅ SKILL.md 内容完全一致\n")
    else:
        print(f"\n  ⚠️  SKILL.md 内容不一致！可用 sync 命令同步。\n")


def cmd_sync(args):
    """同步技能从一个 Agent 到另一个"""
    src_agent = args.src
    dst_agent = args.dst
    skill_name = args.skill_name

    if src_agent not in AGENT_CONFIGS:
        print(f"\n  ❌ 未知 Agent: {src_agent}。可用: {list(AGENT_CONFIGS.keys())}\n")
        return
    if dst_agent not in AGENT_CONFIGS:
        print(f"\n  ❌ 未知 Agent: {dst_agent}。可用: {list(AGENT_CONFIGS.keys())}\n")
        return

    # 找到源技能
    src_skills = scan_agent(src_agent, AGENT_CONFIGS[src_agent])
    src = next((s for s in src_skills if s.name == skill_name), None)

    if not src:
        print(f"\n  ❌ 在 {src_agent} 中未找到技能 \"{skill_name}\"\n")
        return

    # 目标目录
    dst_dir = AGENT_CONFIGS[dst_agent]["install_dir"] / skill_name

    if dst_dir.exists():
        confirm = input(f"\n  ⚠️  目标 {dst_dir} 已存在。覆盖? [y/N] ")
        if confirm.lower() != "y":
            print("  取消。")
            return
        shutil.rmtree(dst_dir)

    # 复制
    shutil.copytree(src.path, dst_dir)
    print(f"\n  ✅ 已同步技能 \"{skill_name}\": {src_agent} → {dst_agent}")
    print(f"     源: {src.path}")
    print(f"     目标: {dst_dir}\n")


def cmd_export(args):
    """导出技能到共享池"""
    skill_name = args.skill_name
    src_agent = args.src

    if src_agent and src_agent not in AGENT_CONFIGS:
        print(f"\n  ❌ 未知 Agent: {src_agent}\n")
        return

    all_skills = scan_all()
    matched = [s for s in all_skills if s.name == skill_name]

    if src_agent:
        matched = [s for s in matched if s.agent == src_agent]

    if not matched:
        print(f"\n  ❌ 未找到技能 \"{skill_name}\"\n")
        return

    src = matched[0]

    # 导出到共享池
    SHARED_POOL.mkdir(parents=True, exist_ok=True)
    dst = SHARED_POOL / skill_name

    if dst.exists():
        shutil.rmtree(dst)

    shutil.copytree(src.path, dst)
    print(f"\n  ✅ 已导出技能 \"{skill_name}\" 到共享池")
    print(f"     来源: {src.agent} ({src.path})")
    print(f"     共享池: {dst}")
    print(f"\n  💡 其他 Agent 可用: python skillctl.py install {skill_name} --to <agent>\n")


def cmd_install(args):
    """从共享池安装技能到指定 Agent"""
    skill_name = args.skill_name
    dst_agent = args.dst

    if dst_agent not in AGENT_CONFIGS:
        print(f"\n  ❌ 未知 Agent: {dst_agent}。可用: {list(AGENT_CONFIGS.keys())}\n")
        return

    src = SHARED_POOL / skill_name
    if not src.exists():
        print(f"\n  ❌ 共享池中没有技能 \"{skill_name}\"")
        print(f"     先用 export 导出: python skillctl.py export {skill_name} --from <agent>\n")

        # 看看哪个 Agent 有这个技能
        all_skills = scan_all()
        found = [s for s in all_skills if s.name == skill_name]
        if found:
            print(f"     该技能存在于: {', '.join(s.agent for s in found)}\n")
        return

    dst_dir = AGENT_CONFIGS[dst_agent]["install_dir"] / skill_name

    if dst_dir.exists():
        confirm = input(f"\n  ⚠️  目标 {dst_dir} 已存在。覆盖? [y/N] ")
        if confirm.lower() != "y":
            print("  取消。")
            return
        shutil.rmtree(dst_dir)

    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst_dir)
    print(f"\n  ✅ 已安装技能 \"{skill_name}\" 到 {dst_agent}")
    print(f"     路径: {dst_dir}\n")


def cmd_pool(args):
    """查看共享池中的技能"""
    if not SHARED_POOL.exists():
        print(f"\n  ℹ️  共享池为空。用 export 命令导出技能到共享池。")
        print(f"     路径: {SHARED_POOL}\n")
        return

    skills = sorted(d.name for d in SHARED_POOL.iterdir() if d.is_dir())

    if not skills:
        print(f"\n  ℹ️  共享池为空。\n")
        return

    print(f"\n📦 共享池中的技能 ({len(skills)} 个)  路径: {SHARED_POOL}\n")
    for s in skills:
        print(f"  - {s}")
    print()


# ============================================================
# 目录联接（Junction）工具函数
# ============================================================

def is_junction(path: Path) -> bool:
    """检查路径是否是 Windows 目录联接 / 符号链接"""
    if not path.exists() and not path.is_symlink():
        return False
    try:
        return path.is_symlink() or path.is_junction()
    except AttributeError:
        # Python < 3.12 没有 is_junction()
        try:
            import ctypes
            FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
        except Exception:
            return False


def get_junction_target(path: Path) -> Optional[str]:
    """获取联接/符号链接的目标路径"""
    try:
        return str(path.resolve())
    except Exception:
        return None


def create_junction(link_path: Path, target_path: Path) -> bool:
    """创建 Windows 目录联接 (mklink /J)，无需管理员权限"""
    try:
        # 确保 link_path 不存在
        if link_path.exists() or is_junction(link_path):
            return False
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        return result.returncode == 0
    except Exception as e:
        print(f"  ⚠️  创建联接失败: {e}")
        return False


def remove_junction(path: Path) -> bool:
    """移除目录联接（不删除目标内容）"""
    try:
        if is_junction(path):
            # Windows 上用 rmdir 移除联接点本身，不删除目标内容
            subprocess.run(
                ["cmd", "/c", "rmdir", str(path)],
                capture_output=True, text=True
            )
            return True
        return False
    except Exception:
        return False


# ============================================================
# centralize / link / unlink / status 命令
# ============================================================

def cmd_centralize(args):
    """
    一键集中化：把各 Agent 的技能搬到共享池，原位替换为联接。

    流程:
    1. 扫描所有 Agent 的技能
    2. 按技能名去重，选择最完整的版本移到共享池
    3. 原目录删除，替换为指向共享池的目录联接
    """
    print("\n🔄 一键集中化：把所有技能搬到共享池，各 Agent 指向同一份\n")

    all_skills = scan_all()
    if not all_skills:
        print("  ❌ 没有扫描到任何技能。\n")
        return

    # 选择要集中化的 Agent
    target_agents = args.agents.split(",") if args.agents else list(AGENT_CONFIGS.keys())
    target_agents = [a.strip() for a in target_agents if a.strip() in AGENT_CONFIGS]

    # 过滤出目标 Agent 的技能
    skills_to_process = [s for s in all_skills if s.agent in target_agents]

    # 跳过已经是联接的
    real_skills = []
    already_linked = 0
    for s in skills_to_process:
        if is_junction(s.path):
            already_linked += 1
        else:
            real_skills.append(s)

    if already_linked:
        print(f"  ℹ️  跳过 {already_linked} 个已链接的技能")

    if not real_skills:
        print("  ✅ 所有技能已经集中化！\n")
        return

    # 按技能名分组
    name_map: dict[str, list[SkillInfo]] = {}
    for s in real_skills:
        name_map.setdefault(s.name, []).append(s)

    print(f"  📊 将处理 {len(real_skills)} 个技能 (来自 {len(target_agents)} 个 Agent)")
    print(f"  📦 共享池位置: {SHARED_POOL}")
    print()

    if not args.yes:
        confirm = input("  确认执行? [y/N] ")
        if confirm.lower() != "y":
            print("  取消。")
            return

    SHARED_POOL.mkdir(parents=True, exist_ok=True)

    success = 0
    failed = 0
    skipped = 0

    for skill_name, skill_list in sorted(name_map.items()):
        pool_dir = SHARED_POOL / skill_name

        # 如果共享池里还没有这个技能，选最完整的版本搬过去
        if not pool_dir.exists():
            # 选文件数最多的版本作为"最完整"的
            best = max(skill_list, key=lambda s: s.file_count)
            try:
                shutil.copytree(best.path, pool_dir)
            except Exception as e:
                print(f"  ❌ {skill_name}: 复制到共享池失败 - {e}")
                failed += 1
                continue

        # 将所有 Agent 中的该技能替换为联接
        for skill in skill_list:
            if is_junction(skill.path):
                skipped += 1
                continue

            try:
                # 备份原目录名
                backup = skill.path.with_name(skill.path.name + ".__backup__")
                skill.path.rename(backup)

                # 创建联接
                if create_junction(skill.path, pool_dir):
                    # 成功后删除备份
                    shutil.rmtree(backup, ignore_errors=True)
                    success += 1
                else:
                    # 失败则恢复
                    backup.rename(skill.path)
                    print(f"  ❌ {skill_name} ({skill.agent}): 创建联接失败")
                    failed += 1
            except Exception as e:
                print(f"  ❌ {skill_name} ({skill.agent}): {e}")
                failed += 1

    print(f"\n  {'─' * 40}")
    print(f"  ✅ 成功链接: {success}")
    if skipped:
        print(f"  ⏩ 已跳过:   {skipped}")
    if failed:
        print(f"  ❌ 失败:     {failed}")
    print(f"  📦 共享池:   {SHARED_POOL}")
    print()


def cmd_link(args):
    """将指定技能集中到共享池，并在各 Agent 中创建联接"""
    skill_name = args.skill_name
    all_skills = scan_all()

    matched = [s for s in all_skills if s.name == skill_name]
    if not matched:
        print(f"\n  ❌ 未找到技能 \"{skill_name}\"\n")
        return

    master_agent = getattr(args, "master", None)
    if master_agent:
        master_matches = [s for s in matched if s.agent == master_agent]
        if not master_matches:
            available = list(set(s.agent for s in matched))
            print(f"\n  ❌ 在 Agent \"{master_agent}\" 中未找到技能 \"{skill_name}\"")
            print(f"     拥有此技能的 Agent 有: {available}\n")
            return
        master_skill = master_matches[0]
    else:
        master_skill = None

    target_agents = args.agents.split(",") if getattr(args, "agents", None) else [s.agent for s in matched]
    matched = [s for s in matched if s.agent in target_agents]

    pool_dir = SHARED_POOL / skill_name
    SHARED_POOL.mkdir(parents=True, exist_ok=True)
    backups_root = SHARED_POOL / ".backups"

    # 1. 确定并搬迁主技能到共享池
    if master_skill:
        print(f"\n👑 指定【{master_skill.agent}】下的 \"{skill_name}\" 为主技能")
        if is_junction(master_skill.path):
            real_path = master_skill.path.resolve()
            if real_path != pool_dir.resolve():
                if pool_dir.exists():
                    shutil.rmtree(pool_dir)
                shutil.copytree(real_path, pool_dir)
        else:
            if pool_dir.exists():
                shutil.rmtree(pool_dir)
            shutil.copytree(master_skill.path, pool_dir)
        print(f"  📦 已将【{master_skill.agent}】的版本作为权威主版本放入共享池: {pool_dir}")
    elif not pool_dir.exists():
        # 未指定 master 且共享池中尚无此技能
        hashes = set(s.content_hash for s in matched if s.content_hash)
        if len(hashes) > 1:
            print(f"\n  ℹ️  发现多个 Agent 拥有不同版本的 \"{skill_name}\":")
            for s in matched:
                print(f"     - {s.agent:<12}: {s.file_count:>2} 个文件, {format_size(s.total_size):>8}, hash={s.content_hash}")
        best = max(matched, key=lambda s: s.file_count)
        if is_junction(best.path):
            real_path = best.path.resolve()
            shutil.copytree(real_path, pool_dir)
        else:
            shutil.copytree(best.path, pool_dir)
        print(f"\n  📦 自动选择最完整的版本（{best.agent}）放入共享池: {pool_dir}")
        if len(hashes) > 1:
            print(f"  💡 提示: 若要指定特定 Agent 为主版本，可使用: python skillctl.py set-master {skill_name} <agent>")
    else:
        print(f"\n  📦 共享池中已存在版本: {pool_dir}")

    # 2. 遍历所有相关 Agent，将它们统一变为指向共享池的联接
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for skill in matched:
        if is_junction(skill.path):
            target = get_junction_target(skill.path)
            try:
                if Path(target).resolve() == pool_dir.resolve():
                    print(f"  ⏩ {skill.agent}: 已是联接 → {pool_dir}")
                    continue
            except Exception:
                pass
            remove_junction(skill.path)
            if create_junction(skill.path, pool_dir):
                print(f"  🔗 {skill.agent}: 已重新定位联接 → {pool_dir}")
            else:
                print(f"  ❌ {skill.agent}: 重建联接失败")
            continue

        # 对独立的非主副本或原目录进行安全备份与联接替换
        backup = skill.path.with_name(skill.path.name + ".__backup__")
        try:
            skill.path.rename(backup)
        except PermissionError:
            print(f"  ⚠️  {skill.agent}: 目录被当前正在运行的 Agent 进程占用锁定。")
            print(f"     （该版本已成功复制至共享池作为主版本，其他 Agent 已全部成功路由）")
            print(f"     （如需将 {skill.agent} 本地目录也转为联接，只需关闭其客户端后重新执行本命令）")
            continue
        except Exception as e:
            print(f"  ❌ {skill.agent}: 重命名备份失败 - {e}")
            continue

        try:
            # 安全归档到 .backups 目录以防需要恢复
            agent_backup_dir = backups_root / skill.agent / f"{skill_name}_{timestamp}"
            agent_backup_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(backup, agent_backup_dir)

            if create_junction(skill.path, pool_dir):
                shutil.rmtree(backup, ignore_errors=True)
                print(f"  🔗 {skill.agent}: 已转为联接 → {pool_dir} (原副本安全归档至 .backups)")
            else:
                backup.rename(skill.path)
                print(f"  ❌ {skill.agent}: 创建联接失败，已恢复原目录")
        except Exception as e:
            backup.rename(skill.path)
            print(f"  ❌ {skill.agent}: 处理失败 - {e}")

    # 3. 如果还有 Agent 没有这个技能但用户想安装联接
    if getattr(args, "also_to", None):
        for agent_key in args.also_to.split(","):
            agent_key = agent_key.strip()
            if agent_key not in AGENT_CONFIGS:
                print(f"  ⚠️  未知 Agent: {agent_key}")
                continue
            dst_dir = AGENT_CONFIGS[agent_key]["install_dir"] / skill_name
            if dst_dir.exists() or is_junction(dst_dir):
                print(f"  ⏩ {agent_key}: 已存在，跳过安装")
                continue
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            if create_junction(dst_dir, pool_dir):
                print(f"  ✅ {agent_key}: 已新建联接路由 → {pool_dir}")
            else:
                print(f"  ❌ {agent_key}: 创建联接失败")

    print()


def cmd_set_master(args):
    """将指定 Agent 的技能设为主技能并建立路由"""
    args.master = args.master_agent
    args.agents = None
    cmd_link(args)


def cmd_unlink(args):
    """取消联接，将共享池中的技能复制回各 Agent"""
    skill_name = args.skill_name
    all_skills = scan_all()

    matched = [s for s in all_skills if s.name == skill_name]
    if not matched:
        print(f"\n  ❌ 未找到技能 \"{skill_name}\"\n")
        return

    if getattr(args, "agent", None):
        matched = [s for s in matched if s.agent == args.agent]

    pool_dir = SHARED_POOL / skill_name

    for skill in matched:
        if not is_junction(skill.path):
            print(f"  ⏩ {skill.agent}: 不是联接，跳过")
            continue

        try:
            remove_junction(skill.path)
            if pool_dir.exists():
                shutil.copytree(pool_dir, skill.path)
                print(f"  ✅ {skill.agent}: 已取消联接，恢复为独立副本")
            else:
                print(f"  ⚠️  {skill.agent}: 联接已移除，但共享池中无此技能")
        except Exception as e:
            print(f"  ❌ {skill.agent}: {e}")

    print()


def cmd_status(args):
    """查看所有技能的链接状态"""
    all_skills = scan_all()

    if args.agent:
        all_skills = [s for s in all_skills if s.agent == args.agent]

    linked = []
    independent = []

    for s in all_skills:
        if is_junction(s.path):
            target = get_junction_target(s.path)
            linked.append((s, target))
        else:
            independent.append(s)

    print(f"\n📊 技能链接状态\n")
    print(f"  🔗 已链接到共享池: {len(linked)} 个")
    print(f"  📁 独立副本:       {len(independent)} 个")

    if linked:
        print(f"\n  已链接的技能:")
        for s, target in sorted(linked, key=lambda x: x[0].name):
            print(f"    🔗 {s.name} ({s.agent}) → {target}")

    print()


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        prog="skillctl",
        description="🛠️  本地 Agent 技能统一管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python skillctl.py scan                           # 一键扫描所有 Agent
  python skillctl.py status                         # 查看技能链接状态（集中化状态）
  python skillctl.py list                           # 列出全部技能
  python skillctl.py list --agent openclaw          # 只看 OpenClaw 的技能
  python skillctl.py search stock                   # 搜索包含 stock 的技能
  python skillctl.py shared                         # 查看跨 Agent 共享的技能
  python skillctl.py info deep-research             # 查看技能详情
  python skillctl.py diff agent-browser-core        # 对比同名技能差异
  python skillctl.py link deep-research             # 集中此技能到共享池并建立各 Agent 联接
  python skillctl.py link deep-research --also-to qoder,claude # 同时安装联接到其他 Agent
  python skillctl.py unlink deep-research           # 解除联接恢复为独立副本
  python skillctl.py centralize                     # 【一键集中化】迁移所有技能到共享池并建立联接
  python skillctl.py centralize --agents claude,qoder -y # 针对指定 Agent 无确认执行
  python skillctl.py sync deep-research --from openclaw --to claude   # 复制同步技能
  python skillctl.py export deep-research --from openclaw             # 导出到共享池
  python skillctl.py install deep-research --to qoder                 # 从共享池安装
  python skillctl.py pool                           # 查看共享池
""",
    )

    sub = parser.add_subparsers(dest="command")

    # scan
    sub.add_parser("scan", help="一键扫描所有 Agent 的技能")

    # status
    p_status = sub.add_parser("status", help="查看所有技能的链接状态")
    p_status.add_argument("--agent", "-a", help="只看某个 Agent")

    # list
    p_list = sub.add_parser("list", help="列出技能")
    p_list.add_argument("--agent", "-a", help="只看某个 Agent")

    # search
    p_search = sub.add_parser("search", help="搜索技能")
    p_search.add_argument("keyword", help="搜索关键词")

    # shared
    sub.add_parser("shared", help="查看多个 Agent 共有的技能")

    # info
    p_info = sub.add_parser("info", help="查看技能详情")
    p_info.add_argument("skill_name", help="技能名称")

    # diff
    p_diff = sub.add_parser("diff", help="对比不同 Agent 中同名技能的差异")
    p_diff.add_argument("skill_name", help="技能名称")

    # link
    p_link = sub.add_parser("link", help="将指定技能集中到共享池并给各 Agent 创建联接")
    p_link.add_argument("skill_name", help="技能名称")
    p_link.add_argument("--master", "--from", dest="master", help="指定以哪个 Agent 的版本作为主技能放入共享池")
    p_link.add_argument("--agents", help="指定只联接哪些 Agent（逗号分隔）")
    p_link.add_argument("--also-to", dest="also_to", help="同时为没有该技能的 Agent 安装联接（逗号分隔）")

    # set-master
    p_set_master = sub.add_parser("set-master", help="指定某 Agent 的副本为主技能放入共享池，其他 Agent 变为联接")
    p_set_master.add_argument("skill_name", help="技能名称")
    p_set_master.add_argument("master_agent", help="作为主版本的 Agent (如 workbuddy, qoder, openclaw)")
    p_set_master.add_argument("--also-to", dest="also_to", help="同时为没有该技能的 Agent 安装联接（逗号分隔）")

    # unlink
    p_unlink = sub.add_parser("unlink", help="取消指定技能的联接，恢复为独立目录")
    p_unlink.add_argument("skill_name", help="技能名称")
    p_unlink.add_argument("--agent", "-a", help="只取消指定 Agent 的联接")

    # centralize
    p_centralize = sub.add_parser("centralize", help="一键集中化技能到共享池并建立联接")
    p_centralize.add_argument("--agents", help="指定 Agent（逗号分隔，如 openclaw,claude,qoder）")
    p_centralize.add_argument("-y", "--yes", action="store_true", help="跳过确认直接执行")

    # sync
    p_sync = sub.add_parser("sync", help="同步技能到另一个 Agent")
    p_sync.add_argument("skill_name", help="技能名称")
    p_sync.add_argument("--from", dest="src", required=True, help="源 Agent")
    p_sync.add_argument("--to", dest="dst", required=True, help="目标 Agent")

    # export
    p_export = sub.add_parser("export", help="导出技能到共享池")
    p_export.add_argument("skill_name", help="技能名称")
    p_export.add_argument("--from", dest="src", help="从哪个 Agent 导出")

    # install
    p_install = sub.add_parser("install", help="从共享池安装技能")
    p_install.add_argument("skill_name", help="技能名称")
    p_install.add_argument("--to", dest="dst", required=True, help="安装到哪个 Agent")

    # pool
    sub.add_parser("pool", help="查看共享池中的技能")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    commands = {
        "scan": cmd_scan,
        "status": cmd_status,
        "list": cmd_list,
        "search": cmd_search,
        "shared": cmd_shared,
        "info": cmd_info,
        "diff": cmd_diff,
        "link": cmd_link,
        "set-master": cmd_set_master,
        "unlink": cmd_unlink,
        "centralize": cmd_centralize,
        "sync": cmd_sync,
        "export": cmd_export,
        "install": cmd_install,
        "pool": cmd_pool,
    }

    commands[args.command](args)


if __name__ == "__main__":
    # 修复 Windows 终端编码问题
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    main()
