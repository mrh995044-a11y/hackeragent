@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title HACKERAGENT

REM Always run from the folder this .bat lives in.
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
    echo   and tick "Add python.exe to PATH" during setup.
    echo.
    pause
    exit /b 1
)

REM --------------------------------------------------------------- pass-through
REM Any arguments are forwarded straight to agent.py and the menu is skipped.
REM   start.bat -p "list the files here" --yes
REM   start.bat --provider gemini -p "explain this repo"
if not "%~1"=="" (
    call :deps || exit /b 1
    %PY% agent.py %*
    set "RESULT=!errorlevel!"
    echo.
    pause
    exit /b !RESULT!
)

call :deps || exit /b 1

:menu
cls
echo.
echo    ============================================================
echo     H A C K E R A G E N T   -   terminal AI agent
echo    ============================================================
echo.
echo     [1] OpenAI        (OPENAI_API_KEY)
echo     [2] Anthropic     (ANTHROPIC_API_KEY)   alias: claude
echo     [3] Gemini        (GEMINI_API_KEY / GOOGLE_API_KEY)   alias: google
echo     [4] Local model   (Ollama / LM Studio / vLLM)
echo     [5] Remove API key / Reset saved keys
echo     [6] Help / usage
echo     [7] Exit
echo.
set "choice="
set /p "choice=   select [1-7]  (Enter quits): "
if not defined choice goto end

if "!choice!"=="1" goto openai
if "!choice!"=="2" goto anthropic
if "!choice!"=="3" goto gemini
if "!choice!"=="4" goto local
if "!choice!"=="5" goto removekey
if "!choice!"=="6" goto usage
if "!choice!"=="7" goto end
echo.
echo   not a valid option.
goto menu

REM ------------------------------------------------------------------ providers
:openai
call :needkey OPENAI_API_KEY "OpenAI" || goto menu
set "PROV=openai"
set "MODEL=gpt-4o-mini"
goto launch

:anthropic
call :needkey ANTHROPIC_API_KEY "Anthropic" || goto menu
set "PROV=anthropic"
set "MODEL=claude-3-5-sonnet-latest"
goto launch

:gemini
if not defined GEMINI_API_KEY if defined GOOGLE_API_KEY set "GEMINI_API_KEY=!GOOGLE_API_KEY!"
call :needkey GEMINI_API_KEY "Gemini" || goto menu
set "PROV=gemini"
set "MODEL=gemini-flash-latest"
goto launch

:local
set "BASE="
set /p "BASE=   base URL [enter = http://localhost:11434/v1]: "
if not defined BASE set "BASE=http://localhost:11434/v1"
set "MODEL="
set /p "MODEL=   model name [enter = llama3.1]: "
if not defined MODEL set "MODEL=llama3.1"
echo.
echo   starting HACKERAGENT (local: !MODEL! at !BASE!) ...
echo.
%PY% agent.py --provider openai --base-url "!BASE!" --model "!MODEL!"
echo.
pause
goto menu

:launch
echo.
echo   starting HACKERAGENT (!PROV! / !MODEL!) ...
echo.
%PY% agent.py --provider !PROV! --model "!MODEL!"
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
    %PY% -c "import agent; agent.remove_env_key('OPENAI_API_KEY')"
    set "OPENAI_API_KEY="
    echo   [cleared OPENAI_API_KEY]
    pause
    goto menu
)
if "!kchoice!"=="2" (
    %PY% -c "import agent; agent.remove_env_key('ANTHROPIC_API_KEY')"
    set "ANTHROPIC_API_KEY="
    echo   [cleared ANTHROPIC_API_KEY]
    pause
    goto menu
)
if "!kchoice!"=="3" (
    %PY% -c "import agent; agent.remove_env_key('GEMINI_API_KEY'); agent.remove_env_key('GOOGLE_API_KEY')"
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

:usage
cls
echo.
echo   HACKERAGENT launcher - usage
echo   ------------------------------------------------------------
echo   Interactive :  start.bat            (this menu)
echo   One-shot    :  start.bat -p "write a port scanner to scan.py"
echo   Auto-approve:  start.bat --yes -p "run 'uname -a' and explain it"
echo   Raw args    :  start.bat --provider gemini --model gemini-2.0-flash
echo   Chat client :  chat.bat "summarise the CAP theorem"
echo.
echo   Any arguments you pass are forwarded straight to agent.py.
echo.
pause
goto menu

:end
endlocal
exit /b 0

REM ------------------------------------------------------------------ helpers --
:deps
%PY% -c "import requests" >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Installing missing dependencies: requests
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
exit /b 0

:needkey
REM %1 = env var name, %2 = friendly label
if defined %~1 exit /b 0
echo.
echo   %~1 is not set.
set "KEYVAL="
set /p "KEYVAL=   paste your %~2 API key (blank cancels): "
if not defined KEYVAL (
    echo   cancelled.
    exit /b 1
)
set "%~1=!KEYVAL!"
echo %~1=!KEYVAL!>>"%~dp0.env"
echo   [saved %~1 to .env]
exit /b 0
