import streamlit as st
from pathlib import Path
import io
import contextlib
import importlib
import pandas as pd
import plotly.express as px
from sqlalchemy import text

from system_and_notification import db_connection_configuration as dbcc
from strategies.GEM import executeBacktestGem as backtest_script
from strategies.GEM import createTableGem as signal_script
from strategies.GEM import functionsBacktestGem as fbg_helper

# Ścieżki bazujące na fizycznej lokalizacji modułu
SCRIPT_FOLDER = Path(backtest_script.__file__).resolve().parent
CONFIG_CREATE_PATH = SCRIPT_FOLDER / "cfg" / "config_backtest_createTableGem.py"
CONFIG_EXECUTE_PATH = SCRIPT_FOLDER / "cfg" / "config_executeBacktestGem.py"

st.title("GEM - Silnik Backtestowy")

st.divider()

# --- SEKCJA 1: EDYCJA KONFIGURACJI ---
st.subheader("Konfiguracja Parametrów")
col_cfg1, col_cfg2 = st.columns(2)

with col_cfg1:
    st.markdown("**1. Sygnały (config_backtest_createTableGem.py)**")
    if CONFIG_CREATE_PATH.exists():
        with open(CONFIG_CREATE_PATH, "r", encoding="utf-8") as f:
            cfg_create_content = f.read()

        updated_cfg_create = st.text_area(
            "Edycja parametrów sygnałów:", value=cfg_create_content, height=250,
            key="cfg_create", label_visibility="collapsed"
        )

        if st.button("💾 Zapisz Config Sygnałów", width='stretch'):
            with open(CONFIG_CREATE_PATH, "w", encoding="utf-8") as f:
                f.write(updated_cfg_create)
            st.success("Zapisano parametry sygnałów!")
    else:
        st.error(f"Nie znaleziono pliku: {CONFIG_CREATE_PATH}")

with col_cfg2:
    st.markdown("**2. Backtest (config_executeBacktestGem.py)**")
    if CONFIG_EXECUTE_PATH.exists():
        with open(CONFIG_EXECUTE_PATH, "r", encoding="utf-8") as f:
            cfg_exec_content = f.read()

        updated_cfg_exec = st.text_area(
            "Edycja parametrów portfela:", value=cfg_exec_content, height=250,
            key="cfg_exec", label_visibility="collapsed"
        )

        if st.button("💾 Zapisz Config Backtestu", width='stretch'):
            with open(CONFIG_EXECUTE_PATH, "w", encoding="utf-8") as f:
                f.write(updated_cfg_exec)
            st.success("Zapisano parametry portfela!")
    else:
        st.error(f"Nie znaleziono pliku: {CONFIG_EXECUTE_PATH}")

st.divider()

# --- SEKCJA 2: URUCHAMIANIE ---
st.subheader("Sterowanie Silnikiem")
col_btn1, col_btn2 = st.columns([2, 2])

with col_btn1:
    if st.button("🔄 1. PRZELICZ SYGNAŁY (BACKTEST)", width='stretch'):
        with open(CONFIG_CREATE_PATH, "w", encoding="utf-8") as f:
            f.write(st.session_state.cfg_create)
        st.session_state.run_signals = True

with col_btn2:
    if st.button("🚀 2. URUCHOM BACKTEST PORTFELA", type="primary", width='stretch'):
        with open(CONFIG_EXECUTE_PATH, "w", encoding="utf-8") as f:
            f.write(st.session_state.cfg_exec)
        st.session_state.run_backtest = True

status_area = st.empty()

# --- SEKCJA 3: TERMINAL NA ŻYWO ---
with st.expander("Podgląd Terminala", expanded=True):
    terminal_placeholder = st.empty()


class TerminalBuf(io.StringIO):
    def write(self, s):
        super().write(s)
        terminal_placeholder.code(self.getvalue())


if st.session_state.get("run_signals"):
    st.session_state.run_signals = False
    output_buffer = TerminalBuf()
    with contextlib.redirect_stdout(output_buffer):
        try:
            importlib.reload(signal_script)
            signal_script.main("config_backtest_createTableGem.py")
            status_area.success("✅ Sygnały backtestowe zostały przeliczone!")
        except Exception as e:
            print(f"\n❌ BŁĄD KRYTYCZNY:\n{e}")
            status_area.error(f"Wystąpił błąd: {e}")

if st.session_state.get("run_backtest"):
    st.session_state.run_backtest = False
    output_buffer = TerminalBuf()
    with contextlib.redirect_stdout(output_buffer):
        try:
            importlib.reload(backtest_script)
            backtest_script.main()
            status_area.success("✅ Backtest zakończony pomyślnie! Wyniki poniżej.")
        except Exception as e:
            print(f"\n❌ BŁĄD KRYTYCZNY:\n{e}")
            status_area.error(f"Wystąpił błąd: {e}")

st.divider()

# --- SEKCJA 4: ARCHIWUM I WYNIKI Z BAZY ---
engine = dbcc.get_engine()

try:
    # 1. ARCHIWUM WYNIKÓW
    st.subheader("Archiwum Wykonanych Backtestów (gem_backtest_history)")

    with engine.connect() as conn:
        check_stats = conn.execute(text("SELECT COUNT(*) FROM gem_zestawienie_dni_rebalancu")).fetchone()[0]

    if check_stats > 0:
        if st.button("💾 Zapisz wyniki bieżącego testu w archiwum", type="secondary", width='stretch'):
            if fbg_helper.save_backtest_run_to_history(engine):
                st.success("Zapisano!")
            else:
                st.error("Błąd zapisu.")

    with engine.connect() as conn:
        try:
            df_history = pd.read_sql(text("SELECT * FROM gem_backtest_history ORDER BY data_zapisu DESC"), conn)
            if not df_history.empty:
                fmt = {"format": "%.2f"}
                column_config = {
                    "kwota_doplat": st.column_config.NumberColumn(**fmt),
                    "zysk_strata": st.column_config.NumberColumn(**fmt),
                    "roe": st.column_config.NumberColumn(**fmt),
                    "cagr": st.column_config.NumberColumn(**fmt),
                    "maximum_dropdown": st.column_config.NumberColumn(**fmt),
                    "calmar": st.column_config.NumberColumn(**fmt),
                    "sortino": st.column_config.NumberColumn(**fmt),
                    "sharpe": st.column_config.NumberColumn(**fmt),
                    "liczba_tranzakcji": st.column_config.NumberColumn(format="%.0f"),
                    "sredni_czas_dlugosci_pozycji": st.column_config.NumberColumn(**fmt),
                    "win_ratio": st.column_config.NumberColumn(**fmt),
                    "market_exposure": st.column_config.NumberColumn(**fmt),
                }
                st.dataframe(
                    df_history,
                    width='stretch',
                    hide_index=True,
                    column_config=column_config
                )
            else:
                st.info("Baza archiwum jest pusta.")
        except Exception:
            st.info("Tabela archiwum jeszcze nie istnieje. Zapisz pierwszy test.")

    st.divider()

    # 2. PREZENTACJA WYNIKÓW OSTATNIEGO BACKTESTU
    st.subheader("Wyniki Ostatniego Backtestu")

    with engine.connect() as conn:
        df_stats = pd.read_sql(text('SELECT * FROM gem_zestawienie_dni_rebalancu ORDER BY dzien_miesiaca ASC'), conn)
        df_equity = pd.read_sql(text('SELECT data, wartosc_portfela FROM gem_daily_portfolio_value ORDER BY data ASC'), conn)
        df_logs = pd.read_sql(text('SELECT * FROM gem_change_logs ORDER BY data_zakupu DESC'), conn)

    if not df_stats.empty and not df_equity.empty:
        st.markdown("**Statystyki Strategii (gem_zestawienie_dni_rebalancu)**")
        st.dataframe(df_stats, width='stretch', hide_index=True)

        st.markdown("**Krzywa Kapitału (Wartość Portfela w czasie)**")
        fig = px.line(
            df_equity,
            x='data',
            y='wartosc_portfela',
            color_discrete_sequence=['#00E5FF']
        )
        fig.update_layout(
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title="Data",
            yaxis_title="Wartość Portfela [USD]",
            hovermode="x unified",
            yaxis_tickformat="$,.0f"
        )
        st.plotly_chart(fig, width='stretch')

        st.markdown("**Logi Transakcyjne (gem_change_logs)**")
        if not df_logs.empty:
            st.dataframe(df_logs, width='stretch', hide_index=True)
        else:
            st.info("Brak wykonanych transakcji w logach dla tego testu.")
    else:
        st.info("Brak danych wynikowych. Uruchom backtest, aby wygenerować wykresy.")

except Exception as e:
    st.error(f"Wystąpił błąd podczas ładowania danych z bazy: {e}")
finally:
    engine.dispose()