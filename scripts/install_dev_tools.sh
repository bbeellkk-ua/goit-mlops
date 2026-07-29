#!/usr/bin/env bash
# ============================================================================
# install_dev_tools.sh
# ----------------------------------------------------------------------------
# Ідемпотентний bash-скрипт для перевірки та підготовки середовища:
#   - Docker
#   - Docker Compose V2 (docker compose version)
#   - Python >= 3.13
#   - pip3
#   - Локальний virtual environment у .venv/ (створюється, якщо його немає)
#   - Python-бібліотеки у venv: torch, torchvision, pillow
#
# Скрипт можна запускати кілька разів підряд — він не буде перевстановлювати
# уже наявні компоненти. Всі результати логуються в install.log.
#
# Використання:
#     bash scripts/install_dev_tools.sh
#
# Після успішного запуску активуйте venv:
#     source .venv/bin/activate
# ============================================================================

set -u

# ---------- КОНФІГУРАЦІЯ ----------
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="${PROJECT_ROOT}/install.log"
VENV_DIR="${PROJECT_ROOT}/.venv"
REQUIREMENTS_FILE="${PROJECT_ROOT}/requirements.txt"

MIN_PY_MAJOR=3
MIN_PY_MINOR=13

# pip-name : python-import-name
REQUIRED_PACKAGES=(
    "torch:torch"
    "torchvision:torchvision"
    "pillow:PIL"
)

# ---------- ЛОГУВАННЯ ----------
log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    echo "[$ts] [$level] $msg" | tee -a "$LOG_FILE"
}
info()  { log "INFO"  "$*"; }
ok()    { log "OK"    "$*"; }
warn()  { log "WARN"  "$*"; }
err()   { log "ERROR" "$*"; }

# ---------- УТИЛІТИ ----------
check_command() { command -v "$1" >/dev/null 2>&1; }

# Знайти інтерпретатор >= 3.13
find_python() {
    for candidate in python3.13 python3.14 python3; do
        if check_command "$candidate"; then
            if "$candidate" - <<PY >/dev/null 2>&1
import sys
sys.exit(0 if sys.version_info >= (${MIN_PY_MAJOR}, ${MIN_PY_MINOR}) else 1)
PY
            then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

venv_python() { echo "${VENV_DIR}/bin/python"; }
venv_pip()    { echo "${VENV_DIR}/bin/pip"; }

check_venv_module() {
    # $1 — import-name
    "$(venv_python)" -c "import $1" >/dev/null 2>&1
}

install_python_package() {
    # $1 — pip-name, $2 — import-name
    local pip_name="$1"
    local import_name="$2"

    if check_venv_module "$import_name"; then
        local ver
        ver="$("$(venv_python)" -c "import ${import_name}; print(getattr(${import_name}, '__version__', 'unknown'))" 2>/dev/null || echo unknown)"
        ok "Пакет '${pip_name}' вже встановлено у venv (import ${import_name}, version=${ver}) — пропускаємо."
        return 0
    fi

    info "Встановлюємо пакет у venv: ${pip_name} ..."
    if "$(venv_pip)" install --no-cache-dir "${pip_name}" >>"$LOG_FILE" 2>&1; then
        ok "Пакет '${pip_name}' встановлено успішно."
    else
        err "Не вдалося встановити '${pip_name}'. Дивіться деталі у ${LOG_FILE}."
        return 1
    fi
}

install_requirements_file() {
    # Ідемпотентно: якщо requirements.txt існує — pip install -r; він сам
    # пропустить уже встановлені версії, тож повторний запуск безпечний.
    if [ -f "$REQUIREMENTS_FILE" ]; then
        info "Встановлюємо залежності з ${REQUIREMENTS_FILE} у venv (pip -r)..."
        if "$(venv_pip)" install --no-cache-dir -r "$REQUIREMENTS_FILE" >>"$LOG_FILE" 2>&1; then
            ok "Залежності з requirements.txt встановлено/актуалізовано."
        else
            warn "Помилка встановлення requirements.txt — падаємо на пакетний режим."
            for entry in "${REQUIRED_PACKAGES[@]}"; do
                install_python_package "${entry%%:*}" "${entry##*:}" || true
            done
        fi
    else
        warn "Файл ${REQUIREMENTS_FILE} не знайдено. Встановлюємо пакети окремо."
        for entry in "${REQUIRED_PACKAGES[@]}"; do
            install_python_package "${entry%%:*}" "${entry##*:}" || true
        done
    fi
}

# ---------- ОСНОВНА ЛОГІКА ----------
main() {
    {
        echo ""
        echo "============================================================"
        echo " install_dev_tools.sh запуск: $(date '+%Y-%m-%d %H:%M:%S')"
        echo " Project root: ${PROJECT_ROOT}"
        echo "============================================================"
    } >> "$LOG_FILE"

    info "Log file: $LOG_FILE"

    # --- Docker ---
    if check_command docker; then
        ok "Docker знайдено: $(docker --version 2>/dev/null)"
    else
        warn "Docker не знайдено."
        warn "На macOS: brew install --cask docker  (або скачайте Docker Desktop)"
    fi

    # --- Docker Compose V2 ---
    if docker compose version >/dev/null 2>&1; then
        ok "Docker Compose V2 знайдено: $(docker compose version 2>/dev/null | head -n1)"
    else
        warn "Docker Compose V2 не знайдено (docker compose version)."
        warn "Docker Desktop >= 4.x містить Compose V2 з коробки."
    fi

    # --- Пошук потрібного Python >= 3.13 ---
    local PY_BIN
    if PY_BIN="$(find_python)"; then
        ok "Знайдено Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}: ${PY_BIN} ($($PY_BIN --version 2>&1))"
    else
        err "Не знайдено Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}."
        err "На macOS встановіть: brew install python@3.13"
        exit 1
    fi

    # --- Створення venv (ідемпотентно) ---
    if [ -x "$(venv_python)" ]; then
        ok "venv вже існує: ${VENV_DIR} (python=$($(venv_python) --version 2>&1))"
    else
        info "Створюємо venv у ${VENV_DIR} через ${PY_BIN} ..."
        if "$PY_BIN" -m venv "$VENV_DIR" >>"$LOG_FILE" 2>&1; then
            ok "venv створено успішно."
        else
            err "Не вдалося створити venv."
            exit 1
        fi
    fi

    # --- pip у venv ---
    if [ -x "$(venv_pip)" ]; then
        ok "venv pip знайдено: $($(venv_pip) --version 2>/dev/null)"
    else
        err "pip у venv не знайдено — щось пішло не так під час створення venv."
        exit 1
    fi

    # Оновлюємо pip лише коли він явно застарілий (тримаємо ідемпотентність).
    # Не робимо безумовний --upgrade кожного разу.
    local pip_ver
    pip_ver="$($(venv_pip) --version 2>/dev/null | awk '{print $2}')"
    info "venv pip version: ${pip_ver}"

    # --- Python-пакети ---
    install_requirements_file

    # --- Підсумок ---
    info "----- Підсумок середовища -----"
    check_command docker  && info "docker:  $(docker --version 2>/dev/null)"
    docker compose version >/dev/null 2>&1 && info "compose: $(docker compose version 2>/dev/null | head -n1)"
    info "python:  $($(venv_python) --version 2>&1) (у venv: ${VENV_DIR})"
    info "pip:     $($(venv_pip) --version 2>/dev/null)"

    for entry in "${REQUIRED_PACKAGES[@]}"; do
        local pip_name="${entry%%:*}"
        local import_name="${entry##*:}"
        if check_venv_module "$import_name"; then
            local ver
            ver="$("$(venv_python)" -c "import ${import_name}; print(getattr(${import_name}, '__version__', 'unknown'))" 2>/dev/null || echo unknown)"
            info "${pip_name}: version=${ver}"
        else
            warn "${pip_name}: НЕ встановлено."
        fi
    done

    ok "install_dev_tools.sh завершив роботу."
    echo ""
    echo ">>> Щоб активувати venv, виконайте:"
    echo ">>>     source .venv/bin/activate"
    echo ""
}

main "$@"
