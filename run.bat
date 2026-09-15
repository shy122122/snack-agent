@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (set "PY=py -3") else (set "PY=python")

for /f "delims=" %%i in ('%PY% -c "import site; print(site.getusersitepackages())" 2^>nul') do set "PY_USER_SITE=%%i"
if defined PY_USER_SITE if exist "%PY_USER_SITE%" set "PYTHONPATH=%PY_USER_SITE%;%PYTHONPATH%"

%PY% -c "import flask" >nul 2>nul
if errorlevel 1 (
  echo [首次运行] 正在安装依赖（使用清华镜像）...
  %PY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 goto :err
)

echo.
%PY% app.py
goto :eof

:err
echo.
echo 依赖安装失败，可手动执行：
echo   pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
pause
