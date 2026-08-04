@echo off
rem Startet gogdl, ohne dass man den Pfad ins venv kennen muss.
rem Fehlt die Umgebung, wird sie beim ersten Aufruf angelegt.

setlocal
set "WURZEL=%~dp0"
set "VENV=%WURZEL%.venv"
set "BIN=%VENV%\Scripts\gogdl.exe"

if not exist "%BIN%" (
    echo Richte die Umgebung ein ^(einmalig^)... 1>&2
    where uv >nul 2>&1
    if errorlevel 1 (
        where python >nul 2>&1
        if errorlevel 1 (
            echo Fehler: weder uv noch python gefunden. Python 3.12 oder neuer wird gebraucht. 1>&2
            exit /b 1
        )
        python -m venv "%VENV%" 1>&2
        "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip 1>&2
        "%VENV%\Scripts\python.exe" -m pip install --quiet -e "%WURZEL%." 1>&2
    ) else (
        uv venv --directory "%WURZEL%." 1>&2
        uv pip install --directory "%WURZEL%." -e "%WURZEL%." 1>&2
    )
)

if not exist "%BIN%" (
    echo Fehler: %BIN% fehlt auch nach der Einrichtung. 1>&2
    echo Bitte "uv pip install -e ." in %WURZEL% ausfuehren. 1>&2
    exit /b 1
)

"%BIN%" %*
exit /b %errorlevel%
