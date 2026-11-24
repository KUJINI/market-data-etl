#!/bin/bash

# Zmienne środowiskowe i ścieżki
PROJECT_DIR="/home/kktf/dev/investing/analizaPokrycia"
VENV_PYTHON="/home/kktf/dev/investing/analizaPokrycia/venv/bin/python3"
LOG_FILE="/home/kktf/dev/investing/analizaPokrycia/cron_debug.log"
DICT_LOG_FILE="/home/kktf/dev/investing/analizaPokrycia/cron_dict.log"

export PYTHONPATH=$PROJECT_DIR
export PATH=/usr/bin:/bin:$PATH

echo "========================================" >> $LOG_FILE
echo "Start automatycznej aktualizacji: $(date)" >> $LOG_FILE

cd $PROJECT_DIR || exit

# 2. Pobieranie nowych cen
if [ "$(date +%d)" = "01" ]; then
    echo "-> KROK 1: Pierwszy dzień miesiąca. Aktualizacja słownika Tiingo..." >> $LOG_FILE
    $VENV_PYTHON data_collection/insertPricesHybrid/updateTiingoDictionary.py >> $DICT_LOG_FILE 2>&1
    echo "-> KROK 2: Pierwszy dzień miesiąca. Rebuild bazy cen tickerow" >> $LOG_FILE
    $VENV_PYTHON data_collection/insertPricesHybrid/engine.py 2 >> $LOG_FILE 2>&1
else
    echo "-> KROK 1: Pomijam aktualizację słownika (to nie jest pierwszy dzień miesiąca)." >> $LOG_FILE
    echo "-> KROK 2: Aktualizacja cen (System Hybrydowy)..." >> $LOG_FILE
    $VENV_PYTHON data_collection/insertPricesHybrid/engine.py 1 >> $LOG_FILE 2>&1
fi

# 3. Przeliczanie strategii GEM
echo "-> KROK 3: Przeliczanie GEM..." >> $LOG_FILE
$VENV_PYTHON strategies/GEM/createTableGem.py >> $LOG_FILE 2>&1

# 4. Wysyłka podsumowania e-mail
echo "-> KROK 4: Wysyłka maila podsumowującego..." >> $LOG_FILE
$VENV_PYTHON system_and_notification/mails/send_summary_email.py >> $LOG_FILE 2>&1

echo "Koniec aktualizacji: $(date)" >> $LOG_FILE
echo "========================================" >> $LOG_FILE