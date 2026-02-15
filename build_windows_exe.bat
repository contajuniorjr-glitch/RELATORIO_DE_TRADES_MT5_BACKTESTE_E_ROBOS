@echo off
setlocal enableextensions

REM Build standalone Windows executable for the Streamlit app.
REM Usage: double-click or run from terminal inside this folder.

cd /d "%~dp0"

if not exist "%ProgramFiles%\\Python311\\python.exe" (
    python -m pip --version >nul 2>&1
    if errorlevel 1 (
        echo Python nao foi encontrado no PATH. Instale Python 3.10+ e tente novamente.
        exit /b 1
    )
)

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

pyinstaller ^
    run_post_trade_app.py ^
    --noconfirm ^
    --clean ^
    --onedir ^
    --collect-data streamlit ^
    --collect-data plotly ^
    --add-data "POST_TRADE_APP_REAL.py;." ^
    --add-data "asset_type_map.json;." ^
    --add-data "candle_colors.json;." ^
    --add-data "stored_mt5_magics.json;."

if errorlevel 1 (
    echo.
    echo Falha ao criar o executavel. Verifique as mensagens acima.
    exit /b 1
)

echo.
echo Build concluido. O executavel esta em dist\\run_post_trade_app\\run_post_trade_app.exe
