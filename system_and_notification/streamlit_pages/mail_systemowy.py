import streamlit as st
import re
import io
import contextlib
import importlib
import runpy
from pathlib import Path
from datetime import date
import pandas as pd
from sqlalchemy import text

from system_and_notification import db_connection_configuration as dbcc
from system_and_notification.mails import send_summary_email as email_script

# Ścieżka bazująca na fizycznej lokalizacji modułu mailowego
MAILS_FOLDER = Path(email_script.__file__).resolve().parent
CONFIG_PY_PATH = MAILS_FOLDER / "config_email.py"

st.title("✉️ Konfiguracja Powiadomień E-mail")


def load_config():
    if CONFIG_PY_PATH.exists():
        try:
            cfg = runpy.run_path(str(CONFIG_PY_PATH))
            return {
                "subject_prefix": cfg.get("SUBJECT_PREFIX", "Raport aktualizacji inwestycji"),
                "receivers": cfg.get("RECEIVERS", [])
            }
        except Exception as e:
            st.error(f"Błąd podczas wczytywania config_email.py: {e}")

    return {"subject_prefix": "Raport aktualizacji inwestycji", "receivers": []}


def save_config(config_dict):
    py_content = (
        f"SUBJECT_PREFIX = {repr(config_dict['subject_prefix'])}\n"
        f"RECEIVERS = {repr(config_dict['receivers'])}\n"
    )
    with open(CONFIG_PY_PATH, "w", encoding="utf-8") as f:
        f.write(py_content)


def is_valid_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None


current_config = load_config()

st.divider()

# --- SEKCJA 1: KONFIGURACJA ---
st.subheader("Ustawienia Raportu")

col1, col2 = st.columns(2)

with col1:
    st.markdown("**1. Tytuł Maila (Prefix)**")
    st.info("Do tego tekstu skrypt automatycznie doklei ' za DD-MM-YYYY'")
    new_subject = st.text_input("Tytuł:", value=current_config.get("subject_prefix", ""), label_visibility="collapsed")

with col2:
    st.markdown("**2. Odbiorcy (jeden pod drugim)**")
    current_receivers_text = "\n".join(current_config.get("receivers", []))
    new_receivers_text = st.text_area("Maile:", value=current_receivers_text, height=100, label_visibility="collapsed")

if st.button("💾 Zapisz Konfigurację", type="primary", width='stretch'):
    raw_emails = new_receivers_text.split('\n')
    cleaned_emails = [e.strip() for e in raw_emails if e.strip()]

    invalid_emails = [e for e in cleaned_emails if not is_valid_email(e)]

    if invalid_emails:
        st.error(f"Błąd! Wykryto niepoprawne adresy e-mail: {', '.join(invalid_emails)}")
    elif not cleaned_emails:
        st.error("Musisz podać przynajmniej jednego odbiorcę!")
    else:
        new_config = {
            "subject_prefix": new_subject.strip(),
            "receivers": cleaned_emails
        }
        save_config(new_config)
        st.success("✅ Zapisano nową konfigurację jako plik .py!")
        st.session_state['config_saved'] = True

st.divider()

# --- SEKCJA 2: TESTOWANIE ---
st.subheader("Sterowanie")


class TerminalBuf(io.StringIO):
    def write(self, s):
        super().write(s)
        terminal_placeholder.code(self.getvalue())


col_btn, _ = st.columns([1, 3])
with col_btn:
    test_mail_btn = st.button("🚀 Wyślij raport testowy TERAZ", width='stretch')

with st.expander("Podgląd Terminala (Logi wysyłki)", expanded=bool(test_mail_btn)):
    terminal_placeholder = st.empty()

if test_mail_btn:
    output_buffer = TerminalBuf()
    with contextlib.redirect_stdout(output_buffer):
        try:
            importlib.reload(email_script)
            email_script.send_email()
            st.success("Proces wysyłania zakończony. Sprawdź terminal powyżej.")
        except Exception as e:
            print(f"\n❌ BŁĄD:\n{e}")
            st.error(f"Wystąpił błąd krytyczny skryptu: {e}")

st.divider()

# --- SEKCJA 3: PODGLĄD LOGÓW CRONA ---
st.subheader("Bieżące logi bazy (logs_functions - Dzisiaj)")
st.caption("Podgląd statusu crona na żywo, abyś widział, czy w ogóle jest sens wysyłać maila.")

engine = dbcc.get_engine()
try:
    with engine.connect() as conn:
        q_logs = text("""
            SELECT execution_date, file, function, mode, additional_info, status, duration_ms 
            FROM logs_functions
            WHERE DATE(execution_date) = :today
            ORDER BY execution_date DESC
        """)
        df_logs = pd.read_sql(q_logs, conn, params={"today": date.today()})

    if not df_logs.empty:
        def color_status(val):
            if isinstance(val, str):
                if 'error' in val.lower():
                    return 'color: red; font-weight: bold'
                elif 'success' in val.lower() or 'ok' in val.lower():
                    return 'color: green'
            return ''

        st.dataframe(df_logs.style.map(color_status, subset=['status']), width='stretch', hide_index=True)
    else:
        st.info("Brak dzisiejszych logów w bazie. Cron jeszcze się nie wykonał lub wystąpił krytyczny błąd w bashu.")
except Exception as e:
    st.error(f"Nie udało się pobrać logów z bazy: {e}")
finally:
    engine.dispose()
