@echo off
setlocal enabledelayedexpansion

:: garante que os caminhos relativos funcionem mesmo se o BAT for chamado de outro local
pushd "%~dp0" >nul

set "VENV_PYTHON=.venv\Scripts\python.exe"
set "VENV_CFG=.venv\pyvenv.cfg"
set "STREAMLIT_APP=POST_TRADE_APP_REAL.py"
set "PY_CMD="

if exist "%VENV_PYTHON%" if exist "%VENV_CFG%" (
    set "PY_CMD=%VENV_PYTHON%"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo [ERRO] Python nao encontrado no PATH e venv invalido/ausente.
        echo Recrie o venv com "py -3.12 -m venv .venv" ou instale Python.
        goto :fim
    )
    set "PY_CMD=python"
    echo [AVISO] Usando Python global porque o venv esta ausente ou invalido.
)

echo Iniciando Streamlit com %STREAMLIT_APP% ...
echo (Pressione CTRL+C para encerrar)
"%PY_CMD%" -m streamlit run "%STREAMLIT_APP%"

:fim
popd >nul
endlocal
