@echo off
chcp 65001 >nul
title Conciliador Bancario - Dominio - Inicializando...
color 0A

echo ========================================
echo    CONCILIADOR BANCARIO - DOMINIO
echo    OFX e PDF: Sicoob, Sicredi, Itau,
echo    Inter, Caixa, BB, Santander
echo ========================================
echo.

:: 1. Verifica se o Python esta instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    color 0C
    echo [ERRO] Python nao encontrado!
    echo Baixe e instale em: https://www.python.org/downloads/
    echo IMPORTANTE: Marque a opcao "Add Python to PATH" na instalacao.
    echo.
    pause
    exit /b 1
)

:: 2. Verifica/Instala as dependencias automaticamente
pip show flask >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Instalando dependencia Flask...
    pip install flask --quiet
)
pip show pdfplumber >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Instalando dependencia pdfplumber - leitura de PDF...
    pip install pdfplumber --quiet
    if %errorlevel% neq 0 (
        color 0E
        echo [AVISO] Falha ao instalar pdfplumber. Extratos PDF nao funcionarao.
        echo         Extratos OFX continuam funcionando normalmente.
    )
)
echo [OK] Dependencias verificadas.

:: 3. Inicia o servidor Flask em segundo plano
echo [INFO] Iniciando banco de dados SQLite e servidor local...
start /min cmd /c "cd /d %~dp0conciliador && python app.py"

:: 4. Aguarda o servidor ficar pronto (maximo 15 segundos)
set /a count=0
:wait_loop
timeout /t 1 /nobreak >nul
curl -s http://127.0.0.1:5001 >nul 2>&1
if %errorlevel% equ 0 goto :server_ready
set /a count+=1
if %count% geq 15 (
    color 0E
    echo [AVISO] Servidor demorando a responder. Abrindo navegador mesmo assim...
    goto :open_browser
)
goto :wait_loop

:server_ready
echo [OK] Servidor rodando em http://127.0.0.1:5001

:open_browser
:: 5. Abre o navegador padrao automaticamente
start "" http://127.0.0.1:5001

echo.
echo ========================================
echo   CONCILIADOR PRONTO! Navegador aberto.
echo   Banco de dados: conciliador\conciliador.db
echo   NAO FECHE ESTA JANELA!
echo ========================================
echo.
echo Pressione qualquer tecla para PARAR o sistema...
pause >nul

:: 6. Encerramento limpo
echo [INFO] Parando servidor...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5001 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
color 0A
echo [OK] Conciliador encerrado com seguranca.
echo.
pause
