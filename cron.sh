#!/bin/bash

PROJECT_DIR="/home/kktf/dev/investing/market-data-etl"
VENV_PYTHON="/home/kktf/dev/investing/market-data-etl/venv/bin/python3"
LOG_DIR="$PROJECT_DIR/cron_logs"

export PYTHONPATH=$PROJECT_DIR
export PATH=/usr/bin:/bin:$PATH

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

CURRENT_LOG="$LOG_DIR/cron_$(date +%Y-%m-%d_%H-%M-%S).log"

{
    echo "========================================"
    echo "Start automatycznej aktualizacji: $(date)"
    echo "Plik logu: $CURRENT_LOG"
    echo "========================================"

    # 1 i 2. Pobieranie nowych cen
    if [ "$(date +%d)" = "01" ]; then
        echo "-> KROK 1: Pierwszy dzień miesiąca. Aktualizacja słownika Tiingo..."
        $VENV_PYTHON data_collection/insertPricesHybrid/updateTiingoDictionary.py 2>&1
        echo "-> KROK 2: Pierwszy dzień miesiąca. Rebuild bazy cen tickerow..."
        $VENV_PYTHON data_collection/insertPricesHybrid/engine.py 2 2>&1
    else
        echo "-> KROK 1: Pomijam aktualizację słownika (to nie jest pierwszy dzień miesiąca)."
        echo "-> KROK 2: Aktualizacja cen (System Hybrydowy)..."
        $VENV_PYTHON data_collection/insertPricesHybrid/engine.py 1 2>&1
    fi

    # 3. Przeliczanie strategii GEM
    echo "-> KROK 3: Przeliczanie GEM..."
    $VENV_PYTHON strategies/GEM/createTableGem.py 2>&1

    # 4. Wysyłka podsumowania e-mail z przekazaniem ścieżki do bieżącego logu
    echo "-> KROK 4: Wysyłka maila podsumowującego..."
    $VENV_PYTHON system_and_notification/mails/send_summary_email.py "$CURRENT_LOG" 2>&1

    echo "Koniec aktualizacji: $(date)"
    echo "========================================"
} >> "$CURRENT_LOG" 2>&1

# Retencja: zostaw 30 najnowszych plików, resztę usuń
ls -1t "$LOG_DIR"/cron_*.log 2>/dev/null | tail -n +31 | xargs -r rm -f