@echo off
chcp 65001 >nul
echo ============================================
echo   客服数据监听 - 环境安装脚本
echo ============================================
echo.

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 3.10+
    echo 下载地址: https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"
    pause
    exit /b 1
)

echo [1/3] 检测到 Python:
python --version
echo.

:: 安装依赖
echo [2/3] 正在安装依赖包...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败，请检查网络连接
    pause
    exit /b 1
)
echo.

:: 检查配置文件
echo [3/3] 检查配置文件...
if not exist "config.yaml" (
    echo [警告] config.yaml 不存在！
    echo 请复制 config.yaml.example 并填写你的配置
    pause
    exit /b 1
)
echo config.yaml 已存在
echo.

echo ============================================
echo   安装完成！
echo   运行方式：双击 launcher.py 打开控制面板
echo   或命令行：python launcher.py
echo ============================================
pause
