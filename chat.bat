@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title AICHAT

cd /d "%~dp0"

REM ------------------------------------------------------------------ .env --
if exist "%~dp0.env" (
    for /f "usebackq tokens=1* delims==" %%A in ("%~dp0.env") do (
        if not "%%A"=="" (
            set "%%A=%%B"
        )
    )
)

REM ------------------------------------------------------------------ python --
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo   [error] Python was not found on your PATH.
    echo   Install Python 3 from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

%PY% -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo   installing dependencies...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [error] dependency install failed. Run manually:
        echo       %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
)

REM ---------------------------------------------------- pass-through (prompt)
REM   chat.bat "summarise the CAP theorem"
REM   chat.bat --provider gemini "hello"
if not "%~1"=="" (
    %PY% aichat.py %*
    set "RESULT=!errorlevel!"
    echo.
    pause
    exit /b !RESULT!
)

:menu
cls
echo.
echo    aichat - provider-agnostic chat client
echo    ---------------------------------------
echo     [1] OpenAI        ^(OPENAI_API_KEY^)
echo     [2] Anthropic     ^(ANTHROPIC_API_KEY^)   alias: claude
echo     [3] Gemini        ^(GEMINI_API_KEY / GOOGLE_API_KEY^)   alias: google
echo     [4] Local model   ^(Ollama / LM Studio / vLLM^)
echo     [5] Interactive multi-turn ^(pick provider first^)
echo     [6] Remove API key / Reset saved keys
echo     [7] Exit
echo.
set "choice="
set /p "choice=   select [1-7]  (Enter quits): "
if not defined choice goto end

if "!choice!"=="1" ( set "PROV=openai"    & set "MODEL=gpt-4o-mini"                & goto ask )
if "!choice!"=="2" ( set "PROV=anthropic" & set "MODEL=claude-3-5-sonnet-latest" & goto ask )
if "!choice!"=="3" ( set "PROV=gemini"    & set "MODEL=gemini-flash-latest"       & goto ask )
if "!choice!"=="4" goto local
if "!choice!"=="5" goto interactive
if "!choice!"=="6" goto removekey
if "!choice!"=="7" goto end
echo.
echo   not a valid option.
goto menu

:ask
call :needkey %PROV% || goto menu
echo.
set "Q="
set /p "Q=   prompt [blank = interactive]: "
echo.
if not defined Q (
    %PY% aichat.py --provider !PROV! --model "!MODEL!"
) else (
    %PY% aichat.py --provider !PROV! --model "!MODEL!" "!Q!"
)
echo.
pause
goto menu

:interactive
set "PROV="
set /p "PROV=   provider [openai/anthropic/gemini, blank=openai]: "
if not defined PROV set "PROV=openai"
call :needkey !PROV! || goto menu
set "MODEL="
set /p "MODEL=   model [blank = provider default]: "
echo.
if not defined MODEL (
    %PY% aichat.py --provider !PROV!
) else (
    %PY% aichat.py --provider !PROV! --model "!MODEL!"
)
echo.
pause
goto menu

:local
set "BASE="
set /p "BASE=   base URL [enter = http://localhost:11434/v1]: "
if not defined BASE set "BASE=http://localhost:11434/v1"
set "MODEL="
set /p "MODEL=   model name [enter = llama3.1]: "
if not defined MODEL set "MODEL=llama3.1"
echo.
%PY% aichat.py --provider openai --base-url "!BASE!" --model "!MODEL!"
echo.
pause
goto menu

:removekey
cls
echo.
echo   Remove / Reset Saved API Keys
echo   ------------------------------------------------------------
echo     [1] Reset OpenAI Key
echo     [2] Reset Anthropic Key
echo     [3] Reset Gemini Key
echo     [4] Clear ALL saved keys from .env
echo     [5] Back to main menu
echo.
set "kchoice="
set /p "kchoice=   select [1-5]: "
if "!kchoice!"=="1" (
    %PY% -c "import aichat; aichat.remove_env_key('OPENAI_API_KEY')"
    set "OPENAI_API_KEY="
    echo   [cleared OPENAI_API_KEY]
    pause
    goto menu
)
if "!kchoice!"=="2" (
    %PY% -c "import aichat; aichat.remove_env_key('ANTHROPIC_API_KEY')"
    set "ANTHROPIC_API_KEY="
    echo   [cleared ANTHROPIC_API_KEY]
    pause
    goto menu
)
if "!kchoice!"=="3" (
    %PY% -c "import aichat; aichat.remove_env_key('GEMINI_API_KEY'); aichat.remove_env_key('GOOGLE_API_KEY')"
    set "GEMINI_API_KEY="
    set "GOOGLE_API_KEY="
    echo   [cleared GEMINI_API_KEY]
    pause
    goto menu
)
if "!kchoice!"=="4" (
    if exist "%~dp0.env" del "%~dp0.env"
    set "OPENAI_API_KEY="
    set "ANTHROPIC_API_KEY="
    set "GEMINI_API_KEY="
    set "GOOGLE_API_KEY="
    echo   [deleted .env and cleared all loaded keys]
    pause
    goto menu
)
goto menu

:end
endlocal
exit /b 0

REM ------------------------------------------------------------------- helper --
:needkey
REM %1 = provider -> maps to the right env var
set "ENVVAR="
if /i "%~1"=="openai"    set "ENVVAR=OPENAI_API_KEY"
if /i "%~1"=="anthropic" set "ENVVAR=ANTHROPIC_API_KEY"
if /i "%~1"=="claude"    set "ENVVAR=ANTHROPIC_API_KEY"
if /i "%~1"=="gemini"    set "ENVVAR=GEMINI_API_KEY"
if /i "%~1"=="google"    set "ENVVAR=GEMINI_API_KEY"
if not defined ENVVAR (
    echo   unknown provider "%~1".
    exit /b 1
)
if /i "%~1"=="gemini" if not defined GEMINI_API_KEY if defined GOOGLE_API_KEY set "GEMINI_API_KEY=!GOOGLE_API_KEY!"
if /i "%~1"=="google" if not defined GEMINI_API_KEY if defined GOOGLE_API_KEY set "GEMINI_API_KEY=!GOOGLE_API_KEY!"
if defined !ENVVAR! exit /b 0
echo.
echo   !ENVVAR! is not set.
set "KEYVAL="
set /p "KEYVAL=   paste your %~1 API key (blank cancels): "
if not defined KEYVAL (
    echo   cancelled.
    exit /b 1
)
set "!ENVVAR!=!KEYVAL!"
echo !ENVVAR!=!KEYVAL!>>"%~dp0.env"
echo   [saved !ENVVAR! to .env]
exit /b 0
