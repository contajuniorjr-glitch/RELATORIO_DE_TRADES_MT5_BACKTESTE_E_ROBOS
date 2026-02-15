@echo off
REM Ativa ambiente e inicia o Streamlit do relatório
cd /d "%~dp0"
python -m streamlit run post_trade_app_real.py
pause