#!/usr/bin/env python3
"""T-Bank MCP — установка скилов Claude Code из установленного пакета.

Скилы едут внутри wheel (tbank_mcp/skills/), но Claude Code читает их из
~/.claude/skills/. Этот скрипт копирует их туда и — в отличие от голого
`cp -r` — сначала вычищает устаревшие копии: скил, переименованный в новой
версии, иначе остаётся под старым именем, и агент грузит протухший.

    tbank-mcp-skills                # в ~/.claude/skills
    tbank-mcp-skills --target DIR   # куда угодно (например ./.claude/skills)

Повторный запуск после каждого обновления пакета — это замена, не слияние.
"""
import argparse
import os
import shutil
import sys

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")


def _is_ours(name):
    # Все скилы этого пакета живут в неймспейсе tbank/tbank-*: по нему же
    # вычищаются осиротевшие копии под именами, которых в пакете больше нет.
    return name == "tbank" or name.startswith("tbank-")


def main():
    ap = argparse.ArgumentParser(
        prog="tbank-mcp-skills",
        description="Установить скилы T-Bank MCP для Claude Code (копия + вычистка старых имён).")
    ap.add_argument("--target", default=os.path.expanduser("~/.claude/skills"),
                    help="куда ставить (по умолчанию ~/.claude/skills)")
    args = ap.parse_args()

    shipped = sorted(d for d in (os.listdir(SKILLS_DIR) if os.path.isdir(SKILLS_DIR) else [])
                     if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md")))
    if not shipped:
        print(f"✗ В пакете нет скилов ({SKILLS_DIR} пуст) — установка повреждена.")
        return 1

    os.makedirs(args.target, exist_ok=True)
    stale = sorted(d for d in os.listdir(args.target)
                   if _is_ours(d) and d not in shipped
                   and os.path.isdir(os.path.join(args.target, d)))
    for name in stale + shipped:
        old = os.path.join(args.target, name)
        if os.path.isdir(old):
            shutil.rmtree(old)
    for name in shipped:
        shutil.copytree(os.path.join(SKILLS_DIR, name), os.path.join(args.target, name))

    print(f"✓ Скилы установлены в {args.target}: {', '.join(shipped)}")
    if stale:
        print(f"  Вычищены устаревшие: {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
