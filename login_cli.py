#!/usr/bin/env python3
"""Шим обратной совместимости: логин-CLI живёт в пакете (tbank_mcp/login_cli.py,
console script `tbank-mcp-login`). Команды из истории шелла —
`.venv/bin/python login_cli.py +7…` — продолжают работать через этот файл."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from tbank_mcp.login_cli import main
except ModuleNotFoundError as _e:
    # Скрипт запускают руками, и первым делом — системным python3, потому что
    # так набирается быстрее. Зависимости живут в .venv репозитория, и голый
    # ModuleNotFoundError: No module named 'mcp' не подсказывает вообще ничего:
    # человек идёт ставить mcp глобально вместо того, чтобы взять готовое
    # окружение.
    _VENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    print(f"Не хватает зависимости: {_e.name}")
    if os.path.exists(_VENV):
        print("Похоже, запущено системным python. Повтори ту же команду, но "
              "интерпретатором из окружения репозитория:")
        print(f"  {_VENV} login_cli.py …")
    else:
        print("Окружения нет. Создай его:")
        print("  python3 -m venv .venv && .venv/bin/pip install -e .")
    sys.exit(1)

if __name__ == "__main__":
    sys.exit(main())
