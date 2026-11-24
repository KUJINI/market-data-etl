# market-data-etl

Narzędzie analityczne do automatycznego pobierania notowań giełdowych, przeliczania strategii inwestycyjnych (m.in. GEM – Global Equities Momentum) oraz wizualizacji wyników w interfejsie webowym.

## Architektura i stack technologiczny

* **Backend / ETL:** Python 3, SQLAlchemy, Pandas
* **Źródła danych (silnik hybrydowy):**
  * **Tiingo API:** rynek USA oraz główne benchmarki Total Return (`VFINX`, `VEURX`, `VEIEX`).
  * **Yahoo Finance / Stooq:** rynki europejskie (GPW `.WA`, Xetra `.DE`), ETF-y, indeksy oraz surowce.
* **Baza danych:** PostgreSQL
* **Frontend:** Streamlit (`streamlit_web.py`)
* **Automatyzacja (Linux VPS):** `cron` (codzienna aktualizacja cen, przeliczenie strategii, raport e-mail) + `systemd` (utrzymanie aplikacji Streamlit)

## Struktura projektu

```text
market-data-etl/
├── data_collection/
│   └── insertPricesHybrid/             # Hybrydowy silnik pobierania notowań (Tiingo / Yahoo / Stooq)
├── strategies/
│   └── GEM/                            # Obliczenia i budowa tabel dla strategii GEM
├── system_and_notification/
│   ├── db_connection_configuration.py  # Konfiguracja silnika SQLAlchemy (dev/prod)
│   ├── mails/                          # Wysyłka dziennych podsumowań e-mail
│   └── streamlit_pages/                # Podstrony interfejsu webowego
├── cron.sh                             # Skrypt harmonogramu aktualizacji
├── streamlit_web.py                    # Punkt wejścia aplikacji webowej
├── .env.example                        # Szablon zmiennych środowiskowych
└── requirements.txt                    # Zależności Pythona
```

## Uruchomienie lokalne / serwerowe

1. **Klonowanie repozytorium i środowisko wirtualne:**
   ```bash
   git clone <URL_REPOZYTORIUM>
   cd market-data-etl
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Konfiguracja zmiennych środowiskowych:**
   Skopiuj szablon i uzupełnij własnymi danymi dostępowymi:
   ```bash
   cp .env.example .env
   ```

| Zmienna                                        | Opis                                                                    |
|:-----------------------------------------------|:------------------------------------------------------------------------|
| `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_PORT` | Dane uwierzytelniające oraz nazwa i port bazy PostgreSQL                |
| `DB_HOST_DEV`, `DB_HOST_PROD`                  | Adresy hosta bazy danych dla środowiska deweloperskiego i produkcyjnego |
| `PROD_HOSTNAME`                                | Nazwa hosta maszyny produkcyjnej (wybór połączenia dev/prod)            |
| `TIINGO_API_KEY`                               | Klucz API do serwisu Tiingo                                             |
| `EMAIL_USER`, `EMAIL_PASS`                     | Konto nadawcy oraz hasło aplikacji (App Password) do wysyłki SMTP       |
| `EMAIL_RECEIVERS`                              | Lista adresów e-mail odbiorców raportów (oddzielona przecinkami)        |

3. **Uruchomienie aplikacji webowej:**
   ```bash
   streamlit run streamlit_web.py
   ```

4. **Automatyzacja (Cron):**
   Skrypt `cron.sh` uruchamia codzienny pipeline:
   1. Aktualizacja słownika tickerów Tiingo (wykonywana 1. dnia miesiąca).
   2. Pobranie aktualnych notowań (silnik hybrydowy).
   3. Przeliczenie tabel strategii GEM.
   4. Wysyłka podsumowania e-mail.