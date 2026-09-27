#!/usr/bin/env bash
# Анонимный профиль Chrome плагина с CDP на 127.0.0.1. Профиль лежит в каталоге данных
# плагина. `stop` закрывает этот Chrome. Сервер плагина поднимает профиль и сам, скрипт
# нужен, чтобы открыть окно заранее или пройти капчу руками.
set -euo pipefail
exec uv run --quiet --directory "$(cd "$(dirname "$0")/.." && pwd)" marketplace-mcp browser "$@"
