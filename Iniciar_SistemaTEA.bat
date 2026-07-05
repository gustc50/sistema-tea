@echo off
chcp 65001 >nul
title Sistema Gestão TEA - Inicializando...
color 0A

echo ========================================
echo    SISTEMA GESTAO TEA - SQLITE LOCAL
echo ========================================
echo.

:: 1. Verifica se o Python está instalado
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

:: 2. Verifica/Instala o Flask automaticamente
pip show flask >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Instalando dependencia Flask...
    pip install flask --quiet
    if %errorlevel% neq 0 (
        color 0C
        echo [ERRO] Falha ao instalar Flask. Verifique sua conexao com a internet.
        pause
        exit /b 1
    )
    echo [OK] Flask instalado com sucesso!
) else (
    echo [OK] Flask ja esta instalado.
)

:: 3. Inicia o servidor Flask em segundo plano
echo [INFO] Iniciando banco de dados SQLite e servidor local...
start /min cmd /c "python app.py"

:: 4. Aguarda o servidor ficar pronto (maximo 10 segundos)
set /a count=0
:wait_loop
timeout /t 1 /nobreak >nul
curl -s http://127.0.0.1:5000 >nul 2>&1
if %errorlevel% equ 0 goto :server_ready
set /a count+=1
if %count% geq 10 (
    color 0E
    echo [AVISO] Servidor demorando a responder. Abrindo navegador mesmo assim...
    goto :open_browser
)
goto :wait_loop

:server_ready
echo [OK] Servidor rodando em http://127.0.0.1:5000

:open_browser
:: 5. Abre o navegador padrão automaticamente
start "" http://127.0.0.1:5000

echo.
echo ========================================
echo   SISTEMA PRONTO! Navegador aberto.
echo   Banco de dados: prontuario_tea.db
echo   NAO FECHE ESTA JANELA!
echo ========================================
echo.
echo Pressione qualquer tecla para PARAR o sistema...
pause >nul

:: 6. Encerramento limpo
echo [INFO] Parando servidor...
taskkill /f /im python.exe /fi "WINDOWTITLE eq *app.py*" >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5000 ^| findstr LISTENING') do (
    taskkill /f /pid %%a >nul 2>&1
)
color 0A
echo [OK] Sistema encerrado com seguranca.
echo.
pause