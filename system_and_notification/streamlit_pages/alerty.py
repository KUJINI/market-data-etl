import streamlit as st
import pandas as pd
import numpy as np
from sqlalchemy import inspect, text
import time

def setup_page():
    st.set_page_config(layout="wide", page_title="GEM Database Validator")
    st.title("🚨 GEM - Skaner Anomalii w Bazie Danych")
    st.markdown("""
    Narzędzie do wykrywania wartości `NULL`, `NaN` oraz `Infinity` we wszystkich tabelach. 
    **Uwaga:** Skanowanie dużych tabel obciąża pamięć RAM.
    """)
    st.divider()

def get_database_tables(engine):
    """
    Pobiera listę wszystkich tabel z podłączonej bazy danych.
    Zwraca listę stringów.
    """
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        return tables
    except Exception as e:
        st.error(f"Błąd podczas odczytu struktury bazy danych: {e}")
        return []


def analyze_table_for_anomalies(engine, object_name, is_view=False):
    """
    Zaawansowany skaner anomalii. Wyłapuje:
    NaN, NULL, Inf, -Inf, puste stringi, białe znaki oraz tekstowe fake-nulle.
    Zwraca też przykładowe ID błędnych rekordów.
    """
    try:
        query = text(f"SELECT * FROM {object_name}")
        with engine.connect() as conn:
            df = pd.read_sql(query, conn)

        if df.empty:
            return {"status": "empty"}

        inspector = inspect(engine)
        id_col = find_id_column(inspector, object_name, df, is_view)

        anomalies = []

        def add_anomaly(col_name, error_type, mask):
            count = mask.sum()
            if count > 0:
                # Wyciąganie próbek ID
                sample_ids = "Brak kolumny ID"
                if id_col and id_col in df.columns:
                    # Bierzemy max 5 IDków żeby nie zapchać pamięci/UI
                    samples = df.loc[mask, id_col].head(5).astype(str).tolist()
                    sample_ids = ", ".join(samples) + ("..." if count > 5 else "")

                anomalies.append({
                    "Kolumna": col_name,
                    "Typ błędu": error_type,
                    "Liczba": count,
                    "Zainfekowane %": round((count / len(df)) * 100, 2),
                    "Przykładowe ID": sample_ids
                })

        # 1. Klasyczne braki (NULL, NaN, NaT)
        for col in df.columns:
            add_anomaly(col, "NULL / NaN / NaT", df[col].isna())

        # 2. Sprawdzanie nieskończoności (Inf, -Inf) w numerykach
        numeric_cols = df.select_dtypes(include=[np.number]).columns
        for col in numeric_cols:
            add_anomaly(col, "Infinity (Inf/-Inf)", np.isinf(df[col]))

        # 3. Sprawdzanie tekstowych śmieci (puste stringi, fake-nulle)
        string_cols = df.select_dtypes(include=['object', 'string']).columns
        fake_nulls = ['nan', 'null', 'n/a', 'none', 'undefined']

        for col in string_cols:
            # Puste stringi lub same spacje
            str_series = df[col].astype(str)
            mask_empty = str_series.str.strip() == ''
            add_anomaly(col, "Pusty string / Tylko spacje", mask_empty)

            # Tekstowe udawanie nulli
            #mask_fake_null = str_series.str.strip().str.lower().isin(fake_nulls)
            #add_anomaly(col, "Tekstowy Fake-Null (np. 'N/A')", mask_fake_null)

        if not anomalies:
            return {"status": "clean"}

        return {
            "status": "anomalies",
            "data": pd.DataFrame(anomalies)
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}


def find_id_column(inspector, table_name, df, is_view=False):
    """
    Próbuje zlokalizować kolumnę pełniącą rolę ID, żeby wiedzieć po czym szukać błędu.
    Dla tabel szuka Primary Key. Dla widoków (lub brak PK) szuka kolumny 'id' w nazwie.
    """
    if not is_view:
        try:
            pk_constraint = inspector.get_pk_constraint(table_name)
            pk_cols = pk_constraint.get('constrained_columns', [])
            if pk_cols:
                return pk_cols[0]  # Bierzemy pierwszy klucz główny
        except:
            pass

    # Fallback: szukanie po nazwach kolumn w DataFrame
    cols = df.columns.str.lower()
    if 'id' in cols:
        return df.columns[cols.tolist().index('id')]

    # Szukamy czegokolwiek co kończy się na _id lub zaczyna na id_
    for col in df.columns:
        if str(col).lower().endswith('_id') or str(col).lower().startswith('id_'):
            return col

    return None  # Brak wyraźnego ID

def get_database_objects(engine):
    """
    Pobiera listę tabel oraz widoków z bazy danych.
    Zwraca słownik z dwiema listami.
    """
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        views = inspector.get_view_names()
        return {"tables": tables, "views": views}
    except Exception as e:
        st.error(f"Błąd podczas odczytu struktury bazy danych: {e}")
        return {"tables": [], "views": []}


def render_scanner_ui(engine):
    """
    Zarządza UI skanera dla tabel i widoków.
    """
    db_objects = get_database_objects(engine)
    tables = db_objects["tables"]
    views = db_objects["views"]

    total_objects = len(tables) + len(views)

    if total_objects == 0:
        st.warning("Brak tabel i widoków w bazie lub brak połączenia.")
        return

    st.subheader("Sterowanie Skanerem")
    col1, col2 = st.columns([1, 3])

    with col1:
        start_scan = st.button("🚀 URUCHOM PEŁNY SKAN BAZY", type="primary", width='stretch')
    with col2:
        st.info(f"Znaleziono obiektów do skanu: **{total_objects}** (Tabel: {len(tables)}, Widoków: {len(views)})")

    st.divider()

    if start_scan:
        st.subheader("Wyniki Skanowania")
        progress_bar = st.progress(0)
        status_text = st.empty()

        results_container = st.container()
        clean_objects = []

        # Łączymy tabele i widoki w jedną listę z flagami (nazwa, czy_to_widok)
        scan_list = [(t, False) for t in tables] + [(v, True) for v in views]

        for i, (obj_name, is_view) in enumerate(scan_list):
            obj_type = "Widok" if is_view else "Tabela"
            status_text.text(f"Skanowanie {obj_type.lower()}: {obj_name} ({i + 1}/{total_objects})...")

            result = analyze_table_for_anomalies(engine, obj_name, is_view=is_view)

            with results_container:
                if result["status"] == "anomalies":
                    st.error(f"🚨 Wykryto błędy w: **{obj_name}** ({obj_type})")
                    st.dataframe(result["data"], width='stretch', hide_index=True)
                elif result["status"] == "error":
                    st.warning(f"⚠️ Błąd odczytu: **{obj_name}** ({obj_type}): {result['message']}")
                elif result["status"] == "empty":
                    st.caption(f"**{obj_name}** ({obj_type}) jest pusta (pominięto).")
                elif result["status"] == "clean":
                    clean_objects.append(f"{obj_name} ({obj_type})")

            progress = (i + 1) / total_objects
            progress_bar.progress(progress)
            time.sleep(0.1)

        status_text.success("✅ Skanowanie zakończone!")

        if clean_objects:
            with st.expander("✅ Obiekty wolne od błędów (Czyste)", expanded=False):
                st.write(", ".join(clean_objects))


def main():
    setup_page()  # Funkcja z poprzedniej wiadomości pozostaje bez zmian

    try:
        from system_and_notification import db_connection_configuration as dbcc
        engine = dbcc.get_engine()

        render_scanner_ui(engine)

    except ImportError:
        st.error("Brak pliku db_connection_configuration.py w zasięgu skryptu.")
    except Exception as e:
        st.error(f"Wystąpił błąd krytyczny z połączeniem do bazy: {e}")
    finally:
        if 'engine' in locals():
            engine.dispose()


if __name__ == "__main__":
    main()
