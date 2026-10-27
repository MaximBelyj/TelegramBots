#!/usr/bin/env bash
# A local access check only. Never starts Telegram polling or asks for credentials.
set -euo pipefail

case "${1:-diagnose}" in
  -h|--help)
    echo 'Usage: bash scripts/macos-check.sh [diagnose|setup|browser|browser-radiokot]'
    echo 'diagnose: prepare Python dependencies if needed and check public search access.'
    echo 'setup: install dependencies only. No command starts the Telegram bot.'
    echo 'browser: open three public searches in a separate visible Chromium profile.'
    exit 0
    ;;
  diagnose|setup|browser|browser-radiokot) mode="${1:-diagnose}" ;;
  *) echo 'Unknown command. Use --help.' >&2; exit 2 ;;
esac

if [[ "$(uname -s)" != Darwin ]]; then
  echo 'Этот помощник предназначен для macOS.' >&2
  exit 1
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
venv_python="$repo_root/.venv/bin/python"

if [[ ! -f .venv/pyvenv.cfg ]]; then
  if [[ -e .venv ]]; then
    echo 'В папке .venv уже есть файлы. Используйте новую копию репозитория; они не будут перезаписаны.' >&2
    exit 1
  fi
  task_python=''
  for candidate in python3.12 python3.13 python3.14 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
      task_python="$candidate"
      break
    fi
  done
  if [[ -z "$task_python" ]]; then
    echo 'Нужен Python 3.11 или новее. Рекомендуется Python 3.12 или 3.13.' >&2
    echo 'Установите его с https://www.python.org/downloads/macos/ и повторите команду.' >&2
    echo 'Если вы используете Homebrew: brew install python@3.12' >&2
    exit 1
  fi
  "$task_python" -m venv .venv
fi

if ! "$venv_python" -c 'import sys; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
  echo 'Существующая .venv не работает или использует старый Python. Используйте новую копию репозитория.' >&2
  exit 1
fi

if [[ "$mode" == setup ]] || ! "$venv_python" -c 'import dmr_bot.search_engine' >/dev/null 2>&1; then
  echo 'Устанавливаю зависимости в локальную .venv…'
  PIP_DISABLE_PIP_VERSION_CHECK=1 "$venv_python" -m pip install --quiet -r requirements.lock
  PIP_DISABLE_PIP_VERSION_CHECK=1 "$venv_python" -m pip install --quiet --no-deps -e .
fi

if [[ "$mode" == setup ]]; then
  echo 'Зависимости установлены. Для проверки: bash scripts/macos-check.sh'
  exit 0
fi

mkdir -p data
if [[ "$mode" == browser || "$mode" == browser-radiokot ]]; then
  export PLAYWRIGHT_BROWSERS_PATH="$repo_root/data/playwright-browsers"
  echo 'Устанавливаю компоненты обычной браузерной проверки…'
  PIP_DISABLE_PIP_VERSION_CHECK=1 "$venv_python" -m pip install --quiet -r requirements-browser.lock
  "$venv_python" -m playwright install chromium
  if [[ "$mode" == browser-radiokot ]]; then
    echo 'Проверяется только RadioKot. Ozon и Wildberries повторно не запрашиваются.'
    "$venv_python" -u -m dmr_bot.browser_check --sources radiokot --output data/macos-radiokot-report.json
  else
    echo 'Откроются RadioKot, Ozon и Wildberries по очереди. Не входите в аккаунты и не решайте CAPTCHA.'
    "$venv_python" -u -m dmr_bot.browser_check
  fi
  exit 0
fi
# This is a distinct machine/network. Reuse its own saved backoff across checks.
export DMR_HEALTH_PATH="$repo_root/data/source_health.macos.json"
echo 'Проверяю доступ из сети Mac. Токен Telegram не нужен; бот не запускается.'
echo 'Проверка может занять несколько минут. Повторные проверки соблюдают паузы сайтов.'
"$venv_python" -u -m dmr_bot.search_engine | tee "$repo_root/data/macos-access-report.txt"
echo 'Отчёт: data/macos-access-report.txt'
echo 'Можно отправить этот текстовый отчёт в чат. Настройки, базы и файлы с токенами присылать не нужно.'
