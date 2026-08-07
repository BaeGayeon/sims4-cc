@echo off
REM Sims 4 CC Manager 실행기 (Windows) - 이 파일을 더블클릭하면 실행됨
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel%==0 (
    python sims_cc_manager.py
) else (
    where py >nul 2>&1
    if %errorlevel%==0 (
        py sims_cc_manager.py
    ) else (
        echo Python이 설치돼 있지 않은 것 같습니다. https://python.org 에서 설치 후 다시 실행해 주세요.
        pause
    )
)
