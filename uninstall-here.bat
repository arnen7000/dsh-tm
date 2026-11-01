@echo off
title dsh 管理器 · 一键卸载
setlocal

cd /d "%~dp0"

echo ============================================================
echo           dsh 管理器   一键卸载
echo ============================================================
echo.
echo  · 这会帮你安全地卸载已安装的 dsh 版本槽
echo  · 卸载前，会先把该槽的用户数据备份到  安装目录\dsh备份\
echo  · 可以只卸其中几个槽，也可以全部卸
echo  · 不会删除你的聊天数据，也不影响其它软件
echo.
echo  · 接下来会列出 config.json 里实际配置的每个槽，
echo    按提示输入编号（可多选，用逗号分隔）或 a 全部卸载
echo.
echo  · 想先看看会发什么、不改任何东西？
echo    可以先运行：scripts\uninstall.ps1 -DryRun -Target all
echo.
echo  正在启动...
pause

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1"

echo.
echo ============================================================
echo   卸载完成。详细信息请看上面的输出
echo   你的数据已备份在  安装目录\dsh备份\ 中，可随时取回。
echo   脚本不会删除你的聊天数据，也不会影响其它用 node 的软件。
echo ============================================================
echo.
pause
