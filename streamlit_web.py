import streamlit as st

st.set_page_config(page_title="Investing Hub", layout="wide")

# Definicja stron
home = st.Page("system_and_notification/streamlit_pages/home.py", title="Strona startowa", default=True)
pobieranie_cen = st.Page("system_and_notification/streamlit_pages/pobieranie_cen.py", title="Pobieranie Cen")
gem_live = st.Page("system_and_notification/streamlit_pages/gem.py", title="GEM")
gem_backtest = st.Page("system_and_notification/streamlit_pages/gem_backtest.py", title="GEM Backtest")
mail_systemowy = st.Page("system_and_notification/streamlit_pages/mail_systemowy.py", title="Mail systemowy")
alerty = st.Page("system_and_notification/streamlit_pages/alerty.py", title="Alerty")

# Grupowanie w sekcje menu bocznego
pg = st.navigation({
    "": [home],
    "Pozyskiwanie danych": [pobieranie_cen],
    "Strategie": [gem_live, gem_backtest],
    "System i powiadomienia": [mail_systemowy, alerty],
})

pg.run()

# streamlit run streamlit_web.py
