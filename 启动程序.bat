@echo off
chcp 65001 >nul
echo 正在启动控制面板...
start "" pythonw "%~dp0launcher.py"
