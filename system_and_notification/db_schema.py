from sqlalchemy import text
import inspect
import time
import traceback
from pathlib import Path
from system_and_notification import db_connection_configuration as dbcc

TABLES = {
    "prices_raw": """
        CREATE TABLE IF NOT EXISTS prices_raw (
            id SERIAL PRIMARY KEY,
            ticker TEXT NOT NULL,
            date TIMESTAMP NOT NULL,
            open NUMERIC,
            high NUMERIC,
            low NUMERIC,
            close NUMERIC,
            volume BIGINT,
            dividend NUMERIC DEFAULT 0,
            split NUMERIC DEFAULT 1,
            source TEXT DEFAULT 'UNKNOWN',
            timeframe TEXT DEFAULT '1d',
            CONSTRAINT prices_raw_ticker_date_tf_key UNIQUE(ticker, date, timeframe)
        )
    """,
    "prices_adj_factors": """
        CREATE TABLE IF NOT EXISTS prices_adj_factors (
            id SERIAL PRIMARY KEY,
            ticker TEXT NOT NULL,
            start_date TIMESTAMP NOT NULL,
            end_date TIMESTAMP NOT NULL,
            multiplier NUMERIC NOT NULL,
            UNIQUE(ticker, start_date)
        )
    """,
    "api_rate_limits": """
        CREATE TABLE IF NOT EXISTS api_rate_limits (
            service_name TEXT PRIMARY KEY,
            request_count INTEGER DEFAULT 0,
            window_start TIMESTAMP
        )
    """,
    "prices_tiingo_dictionary": """
        CREATE TABLE IF NOT EXISTS prices_tiingo_dictionary (
            id SERIAL PRIMARY KEY,
            ticker TEXT UNIQUE NOT NULL
        )
    """,
    "price_before_rebuild": """
        CREATE TABLE IF NOT EXISTS price_before_rebuild (
            id SERIAL PRIMARY KEY,
            nazwaTickera VARCHAR(50) NOT NULL,
            data_sprawdzenia TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "ticker_download_logs": """
        CREATE TABLE IF NOT EXISTS ticker_download_logs (
            id SERIAL PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL,
            execution_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ticker VARCHAR(50) NOT NULL,
            source VARCHAR(30),
            timeframe VARCHAR(20),
            db_before TIMESTAMP NULL,
            api_min TIMESTAMP NULL,
            api_max TIMESTAMP NULL,
            db_after TIMESTAMP NULL,
            rows_saved INTEGER DEFAULT 0,
            status VARCHAR(30) NOT NULL,
            details TEXT
        )
    """,
    "logs_functions": """
        CREATE TABLE IF NOT EXISTS logs_functions (
            id SERIAL PRIMARY KEY,
            execution_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            file TEXT NOT NULL,
            function TEXT NOT NULL,
            mode TEXT,
            additional_info TEXT NOT NULL,
            status TEXT NOT NULL,
            duration_ms INTEGER,
            error_traceback TEXT
        )
    """,
    "gem_zestawienie_dni_rebalancu": """
        CREATE TABLE IF NOT EXISTS gem_zestawienie_dni_rebalancu (
            id SERIAL PRIMARY KEY,
            dzien_miesiaca INTEGER NOT NULL,
            zysk_strata NUMERIC,
            roe NUMERIC,
            cagr NUMERIC,
            maximum_dropdown NUMERIC,
            dropdown_duration INTEGER,
            calmar NUMERIC,
            sortino NUMERIC,
            sharpe NUMERIC,
            liczba_tranzakcji INTEGER,
            sredni_czas_dlugosci_pozycji NUMERIC,
            win_ratio NUMERIC,
            market_exposure NUMERIC
        )
    """,
    "gem_change_logs": """
        CREATE TABLE IF NOT EXISTS gem_change_logs (
            id SERIAL PRIMARY KEY,
            dzien_rebalansu INTEGER,
            ticker TEXT,
            data_zakupu DATE,
            data_sprzedazy DATE,
            cena_zakupu NUMERIC,
            cena_sprzedazy NUMERIC,
            ilosc_akcji NUMERIC,
            zysk_strata_procent NUMERIC,
            wartosc_portfela_po_operacji NUMERIC
        )
    """,
    "gem_daily_portfolio_value": """
        CREATE TABLE IF NOT EXISTS gem_daily_portfolio_value (
            id SERIAL PRIMARY KEY,
            data DATE NOT NULL,
            dzien_rebalansu INTEGER NOT NULL,
            wartosc_portfela NUMERIC,
            gotowka NUMERIC,
            wartosc_aktywow NUMERIC,
            ticker TEXT,
            UNIQUE(data, dzien_rebalansu)
        )
    """,
    "gem_backtest_history": """
        CREATE TABLE IF NOT EXISTS gem_backtest_history (
            id SERIAL PRIMARY KEY,
            data_zapisu TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            poczatek_testu TEXT,
            koniec_testu TEXT,
            kwota_doplat NUMERIC,
            tickers TEXT,
            period_to_analyse TEXT,
            co_ile_rebalance TEXT,
            skip_period TEXT,
            czy_uzywac_sma TEXT,
            wartosc_sma NUMERIC,
            zysk_strata NUMERIC,
            roe NUMERIC,
            calmar NUMERIC,
            cagr NUMERIC,
            maximum_dropdown NUMERIC,
            dropdown_duration NUMERIC,
            sortino NUMERIC,
            sharpe NUMERIC,
            liczba_tranzakcji NUMERIC,
            sredni_czas_dlugosci_pozycji NUMERIC,
            win_ratio NUMERIC,
            market_exposure NUMERIC
        )
    """
}

VIEWS = {
    "v_prices_adjusted": """
        CREATE VIEW v_prices_adjusted AS
        SELECT 
            r.id,
            r.ticker,
            r.date,
            r.timeframe,
            r.source,
            CAST(r.open * COALESCE(f.multiplier, 1.0) AS NUMERIC(10,4)) AS adj_open,
            CAST(r.high * COALESCE(f.multiplier, 1.0) AS NUMERIC(10,4)) AS adj_high,
            CAST(r.low * COALESCE(f.multiplier, 1.0) AS NUMERIC(10,4)) AS adj_low,
            CAST(r.close * COALESCE(f.multiplier, 1.0) AS NUMERIC(10,4)) AS adj_close,
            CAST(r.volume / COALESCE(f.multiplier, 1.0) AS BIGINT) AS adj_volume,
            r.open,
            r.high,
            r.low,
            r.close,
            r.volume,
            r.dividend,
            r.split
        FROM prices_raw r
        LEFT JOIN prices_adj_factors f 
          ON r.ticker = f.ticker 
         AND r.date >= f.start_date 
         AND r.date <= f.end_date
    """,
}


def init_db_schema(engine):
    """Sprawdza całość bazy: tworzy brakujące tabele i odświeża wszystkie widoki."""
    with engine.begin() as conn:
        for ddl in TABLES.values():
            conn.execute(text(ddl))

        for view_name, ddl in VIEWS.items():
            conn.execute(text(f"DROP VIEW IF EXISTS {view_name} CASCADE;"))
            conn.execute(text(ddl))


def clean_table(engine, table_name: str):
    """Czyści wszystkie wiersze z tabeli i resetuje ID od 1. Nie rusza struktury ani widoków."""
    if table_name not in TABLES:
        raise ValueError(f"Nieznana tabela: '{table_name}'")

    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_name} RESTART IDENTITY;"))


def rebuild_table(engine, table_name: str):
    """Awaryjna przebudowa struktury: zrzuca tabelę (i zależne widoki), po czym odtwarza całość przez init_db_schema."""
    if table_name not in TABLES:
        raise ValueError(f"Nieznana tabela: '{table_name}'")

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {table_name} CASCADE;"))

    init_db_schema(engine)


def log_universal(
    engine,
    additional_info: str,
    status: str = "Success",
    mode: str = "DEFAULT",
    start_time: float | None = None,
    error_traceback: str | None = None,
):
    """
    Uniwersalny loger zapisujący zdarzenia do tabeli `logs_functions`.
    Automatycznie wykrywa nazwę pliku i funkcji, z której został wywołany.
    """
    try:
        caller_frame = inspect.stack()[1]
        caller_file = Path(caller_frame.filename).name
        caller_func = caller_frame.function
    except Exception:
        caller_file = "UNKNOWN"
        caller_func = "UNKNOWN"
    finally:
        if "caller_frame" in locals():
            del caller_frame

    duration_ms = None
    if start_time is not None:
        duration_ms = int(round((time.perf_counter() - start_time) * 1000))

    if error_traceback is None and status.lower() in ("failed", "error"):
        current_tb = traceback.format_exc()
        if current_tb and not current_tb.startswith("NoneType: None"):
            error_traceback = current_tb

    own_engine = False
    if engine is None:
        engine = dbcc.get_engine()
        own_engine = True

    try:
        with engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO logs_functions 
                        (file, function, mode, additional_info, status, duration_ms, error_traceback)
                    VALUES 
                        (:file, :func, :mode, :info, :status, :duration_ms, :error_traceback)
                """),
                {
                    "file": caller_file,
                    "func": caller_func,
                    "mode": mode,
                    "info": additional_info,
                    "status": status,
                    "duration_ms": duration_ms,
                    "error_traceback": error_traceback,
                },
            )
    except Exception as e:
        print(f"[LOGGER ERROR] Nie udało się zapisać logu do logs_functions: {e}")
    finally:
        if own_engine:
            engine.dispose()


# import db_schema as dbs
# engine = dbcc.get_engine()
# dsb.init_db_schema(engine)

# start_time = time.perf_counter()

# logs_functions