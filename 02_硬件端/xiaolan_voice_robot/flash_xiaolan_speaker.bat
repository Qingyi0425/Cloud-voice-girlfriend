@echo off
setlocal EnableExtensions
title Xiao Lan ESP32 Speaker Firmware
cd /d "%~dp0"
set "SKETCH=%~dp0."

set "CLI=D:\Arduino\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
set "BUILD=D:\Arduino\build\xiaolan_voice_robot_speaker"
set "FQBN=esp32:esp32:esp32s3:FlashSize=16M,PartitionScheme=huge_app,PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc"
set "PORT=COM7"

echo.
echo Xiao Lan ESP32 speaker firmware uploader
echo.

if not exist "%CLI%" goto :no_cli

echo Detected serial ports:
"%CLI%" board list
if errorlevel 1 goto :cli_error

echo.
set /p "PORT=Enter ESP32 port [COM7]: "
if not defined PORT set "PORT=COM7"
for /f "tokens=1" %%A in ("%PORT%") do set "PORT=%%A"

echo.
echo Compiling firmware to D drive...
"%CLI%" compile --fqbn "%FQBN%" --build-path "%BUILD%" "%SKETCH%"
if errorlevel 1 goto :compile_error

echo.
echo Uploading to %PORT%...
"%CLI%" upload -p "%PORT%" --fqbn "%FQBN%" --input-dir "%BUILD%"
if errorlevel 1 goto :upload_error

echo.
echo Upload completed successfully.
echo You can now disconnect USB, reconnect the modules, and power on the board.
goto :done

:no_cli
echo Arduino CLI was not found:
echo %CLI%
goto :failed

:cli_error
echo Arduino CLI could not list serial ports.
goto :failed

:compile_error
echo Firmware compilation failed.
goto :failed

:upload_error
echo Upload failed. If the board is not detected, hold BOOT while starting upload.
goto :failed

:failed
echo.
echo The window will stay open so you can read the error.

:done
echo.
pause
endlocal
