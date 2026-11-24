import streamlit as st
from pathlib import Path
import io
import contextlib
import importlib

from data_collection.insertPricesHybrid import engine as abc
from system_and_notification import db_connection_configuration as dbcc
from sqlalchemy import text

# 1. Ustawienia strony - TYLKO RAZ NA SAMEJ GÓRZE!
st.set_page_config(layout="wide", page_title="YFinance Admin Panel")

# --- KONFIGURACJA ŚCIEŻEK ---
SCRIPT_FOLDER = Path(abc.__file__).resolve().parent
CONFIG_PATH = SCRIPT_FOLDER / "cfg" / "config_editable.py"
CONFIG_ALL_PATH = SCRIPT_FOLDER / "cfg" / "config_automatic.py"

# --- INTERFEJS ---
st.title("Zarządzanie Pobieraniem")


def get_last_update_info():
    engine = dbcc.get_engine()
    try:
        # Pobieramy ostatnią datę dla trybu Automatic
        query = text("""
            SELECT execution_date, status 
            FROM logs_functions
            WHERE file = 'engine.py' AND mode = 'AUTOMATIC' 
            ORDER BY execution_date DESC LIMIT 1
        """)
        with engine.connect() as conn:
            result = conn.execute(query).fetchone()
            return result if result else None
    except:
        return None
    finally:
        engine.dispose()


# Wyciągamy dane o ostatniej aktualizacji
last_info = get_last_update_info()

if last_info:
    last_date, last_status = last_info
    st.info(f"🕒 Ostatnia pełna aktualizacja (Auto): **{last_date.strftime('%Y-%m-%d %H:%M:%S')}**")
else:
    st.warning("⚠️ Nie znaleziono śladów poprzednich pełnych aktualizacji w bazie.")

st.divider()

# GÓRNA SEKCJA: Dwie kolumny na configi
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("QUAL (config_editable.py)")
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            content_editable = f.read()

        updated_config = st.text_area("Edytuj ustawienia ręczne:", value=content_editable, height=350, key="cfg_edit")

        if st.button("Zapisz i Uruchom Wybiórczo"):
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(updated_config)
            st.session_state.run_type = "QUAL"
    else:
        st.error("Brak pliku config_editable")

with col_right:
    st.subheader("PROD (config_automatic.py)")
    if CONFIG_ALL_PATH.exists():
        with open(CONFIG_ALL_PATH, "r", encoding="utf-8") as f:
            content_auto = f.read()

        updated_auto_config = st.text_area("Edytuj ustawienia pełne:", value=content_auto, height=350,
                                           key="cfg_auto_edit")

        btn_col_save, btn_col_run = st.columns([1, 1])

        with btn_col_save:
            if st.button("💾 Zapisz zmiany w Auto", width='stretch'):
                with open(CONFIG_ALL_PATH, "w", encoding="utf-8") as f:
                    f.write(updated_auto_config)
                st.session_state.show_save_msg = True
                st.rerun()

        with btn_col_run:
            if st.button("🚀 Uruchom Pełną (All)", width='stretch'):
                with open(CONFIG_ALL_PATH, "w", encoding="utf-8") as f:
                    f.write(updated_auto_config)
                st.session_state.run_type = "PROD"

        status_area = st.empty()

st.write("")
with st.expander("REBUILD BAZY", expanded=False):
    st.error("Ta akcja wykasuje tabele cen i przebuduje je od zera")
    if st.button("WYKONAJ PEŁNY REBUILD (Tryb 2)", type="primary", width='stretch'):
        st.session_state.run_type = "REBUILD"

st.write("")

# DOLNA SEKCJA: Terminal na całą szerokość
st.subheader("Podgląd Terminala")
terminal_placeholder = st.empty()

# --- LOGIKA URUCHAMIANIA ---
if "show_save_msg" in st.session_state and st.session_state.show_save_msg:
    status_area.success("💾 Zmiany w Config Automatic zostały zapisane pomyślnie!")
    del st.session_state.show_save_msg  # Usuwamy, żeby nie wisiało przy następnych akcjach

if "run_type" in st.session_state and st.session_state.run_type:
    run_mode = st.session_state.run_type
    # Czyścimy flagę, żeby nie zapętlić skryptu przy odświeżeniu strony
    st.session_state.run_type = None


    # Terminal i przechwytywanie wyjścia w czasie rzeczywistym
    class TerminalBuf(io.StringIO):
        def write(self, s):
            super().write(s)
            # Wyświetlamy to, co skrypt wypluwa na bieżąco
            terminal_placeholder.code(self.getvalue())


    output_buffer = TerminalBuf()

    with contextlib.redirect_stdout(output_buffer):
        try:
            # 1. Przeładowujemy moduł skryptu głównego
            importlib.reload(abc)

            # 2. Mapujemy tryb na cyfrę z engine.py
            if run_mode == "PROD":
                target_selection = 1
            elif run_mode == "REBUILD":
                target_selection = 2
            else:
                target_selection = 0  # QUAL

            # 3. WYWOŁANIE:
            abc.main(mode_selection=target_selection)

            status_area.success(f"✅ Proces {run_mode.upper()} zakończony pomyślnie!")

        except Exception as e:
            # Wyświetlamy błąd w terminalu na stronie
            print(f"\n❌ BŁĄD KRYTYCZNY W STREAMLIT:\n{e}")
            status_area.error(f"Wystąpił błąd podczas uruchamiania: {e}")
