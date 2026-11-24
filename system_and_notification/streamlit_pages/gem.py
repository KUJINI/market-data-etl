import streamlit as st
from pathlib import Path
import io
import contextlib
import importlib
import pandas as pd
import plotly.express as px
from sqlalchemy import text

from system_and_notification import db_connection_configuration as dbcc
from strategies.GEM import createTableGem as gem_script

SCRIPT_FOLDER = Path(gem_script.__file__).resolve().parent
CONFIG_PATH = SCRIPT_FOLDER / "cfg" / "config_createTableGem.py"
EXPLANATION_PATH = SCRIPT_FOLDER / "cfg" / "explanation.txt"


@st.cache_data(ttl=5)
def fetch_gem_data():
    engine = dbcc.get_engine()
    try:
        with engine.connect() as conn:
            query = text('SELECT * FROM gem_price_changes ORDER BY "dateOfAnalyze" ASC')
            df = pd.read_sql(query, conn)
            if not df.empty:
                df['dateOfAnalyze'] = pd.to_datetime(df['dateOfAnalyze'])
            return df
    except Exception as e:
        st.error(f"Błąd odczytu bazy: {e}")
        return pd.DataFrame()
    finally:
        engine.dispose()


def get_last_update_info():
    """Pobiera status z centralnej tabeli logów tylko dla modułu GEM."""
    engine = dbcc.get_engine()
    try:
        query = text("""
            SELECT execution_date, status, duration_ms
            FROM logs_functions
            WHERE file = 'createTableGem.py' AND mode = 'GEM_MAIN'
            ORDER BY execution_date DESC LIMIT 1
        """)
        with engine.connect() as conn:
            result = conn.execute(query).fetchone()
            return result if result else None
    except Exception:
        return None
    finally:
        engine.dispose()


# --- INTERFEJS ---
st.title("GEM")

last_info = get_last_update_info()

if last_info:
    last_date, last_status, last_duration = last_info
    st.info(f"🕒 Ostatnia pełna aktualizacja (Auto): **{last_date.strftime('%Y-%m-%d %H:%M:%S')}**")
else:
    st.warning("⚠️ Nie znaleziono śladów poprzednich pełnych aktualizacji w bazie.")

df_gem = fetch_gem_data()

status_area = st.empty()

with st.expander("Podgląd Terminala", expanded=False):
    terminal_placeholder = st.empty()

if st.session_state.get("run_gem"):
    st.session_state.run_gem = False

    class TerminalBuf(io.StringIO):
        def write(self, s):
            super().write(s)
            terminal_placeholder.code(self.getvalue())

    output_buffer = TerminalBuf()

    with contextlib.redirect_stdout(output_buffer):
        try:
            importlib.reload(gem_script)
            gem_script.main()
            status_area.success("✅ Obliczenia GEM zakończone pomyślnie!")
            st.cache_data.clear()
        except Exception as e:
            print(f"\n❌ BŁĄD KRYTYCZNY W STREAMLIT:\n{e}")
            status_area.error(f"Wystąpił błąd podczas uruchamiania: {e}")

st.divider()

# --- WYKRES (PLOTLY) ---
st.subheader("Wizualizacja Momentum")

if not df_gem.empty:
    time_options = {"6 Miesięcy": 6, "1 Rok": 12, "3 Lata": 36, "MAX": None}

    selected_period = st.radio(
        "Zakres widoku:",
        options=list(time_options.keys()),
        index=1,
        horizontal=True,
        label_visibility="collapsed"
    )

    max_date = df_gem['dateOfAnalyze'].max()
    months_to_subtract = time_options[selected_period]

    if months_to_subtract is not None:
        cutoff_date = max_date - pd.DateOffset(months=months_to_subtract)
        df_plot = df_gem[df_gem['dateOfAnalyze'] >= cutoff_date].copy()
    else:
        df_plot = df_gem.copy()

    df_plot = df_plot.reset_index(drop=True)

    exclude_cols = [
        'id',
        'dateStart',
        'dateStop',
        'dateOfAnalyze',
        'best_performing_ticker_name',
        'best_performing_ticker_difference',
        'isBestPerformerOverSMA200'
    ]
    ticker_cols = [c for c in df_plot.columns if c not in exclude_cols]

    for col in ticker_cols:
        df_plot[col] = pd.to_numeric(df_plot[col], errors='coerce')

    vivid_palette = [
        '#00FF00', '#FF004D', '#00E5FF', '#FFEA00', '#D500F9',
        '#FF6D00', '#1DE9B6', '#F50057', '#3D5AFE', '#AEEA00'
    ]

    try:
        fig = px.line(
            df_plot,
            x='dateOfAnalyze',
            y=ticker_cols,
            height=450,
            color_discrete_sequence=vivid_palette
        )

        fig.update_layout(
            legend=dict(
                orientation="h",
                yanchor="top",
                y=-0.15,
                xanchor="center",
                x=0.5,
                title=None
            ),
            margin=dict(l=10, r=10, t=10, b=10),
            xaxis_title="",
            yaxis_title="Różnica [%]",
            hovermode="x unified"
        )

        st.plotly_chart(fig, width='stretch')
    except ValueError as ve:
        st.error(f"Nie udało się narysować wykresu. Zwrócony błąd Plotly: {ve}")
else:
    st.warning("Brak danych do wygenerowania wykresu. Uruchom obliczenia.")

st.divider()

# --- USTAWIENIA STRATEGII I DOKUMENTACJA ---
st.subheader("Ustawienia Strategii i Dokumentacja")

col_cfg, col_expl = st.columns(2)

with col_cfg:
    st.markdown("**Edycja config_createTableGem.py**")
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config_content = f.read()

        updated_config = st.text_area(
            "Parametry:", value=config_content, height=250, key="gem_cfg_edit",
            label_visibility="collapsed"
        )

        col_btn_save, spacer, col_btn_run = st.columns([2, 4, 3])

        with col_btn_save:
            if st.button("💾 Zapisz zmiany", width='stretch'):
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    f.write(updated_config)
                st.success("Zapisano zmiany!")

        with col_btn_run:
            if st.button("🚀 Uruchom Przeliczanie", width='stretch'):
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    f.write(updated_config)
                st.session_state.run_gem = True
    else:
        st.error(f"Nie znaleziono pliku konfiguracyjnego w ścieżce: {CONFIG_PATH}")

with col_expl:
    st.markdown("**Objaśnienia parametrów (explanation.txt)**")
    if EXPLANATION_PATH.exists():
        with open(EXPLANATION_PATH, "r", encoding="utf-8") as f:
            expl_content = f.read()
        st.text_area(
            "Dokumentacja:", value=expl_content, height=250, disabled=True, key="gem_expl_view",
            label_visibility="collapsed"
        )
    else:
        st.warning(f"Brak pliku explanation.txt. Utwórz go w {EXPLANATION_PATH}, aby dodać instrukcje dla użytkowników.")

st.divider()

# --- PREZENTACJA WYNIKÓW BAZY DANYCH ---
st.subheader("Wyniki w Bazie Danych (gem_price_changes)")
if not df_gem.empty:
    st.dataframe(
        df_gem.sort_values("dateOfAnalyze", ascending=False),
        width="stretch",
        hide_index=True,
    )
else:
    st.warning("Tabela jest pusta. Uruchom przeliczanie strategii.")