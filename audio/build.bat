@echo off
rem Builds proccap.exe (Ibasho's audio capture helper) with Visual Studio 2022 or its Build Tools (C++ workload,
rem the same install the viewer build needs).
rem   audio\build.bat [OUTPUT_DIR]
rem The default output is %IBASHO_HOME%\bin, or %USERPROFILE%\Ibasho\bin without IBASHO_HOME: where `ibasho hear`
rem looks for it. The binary is statically linked, so it runs on machines without the Visual C++ runtime.
setlocal

set "OUT=%~1"
if "%OUT%"=="" if not "%IBASHO_HOME%"=="" set "OUT=%IBASHO_HOME%\bin"
if "%OUT%"=="" set "OUT=%USERPROFILE%\Ibasho\bin"

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo build.bat: no Visual Studio installer found ^(vswhere.exe^). Install the Build Tools with the C++ workload: 1>&2
    echo   vs_buildtools.exe --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended 1>&2
    exit /b 1
)
set "VS="
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS=%%i"
if "%VS%"=="" (
    echo build.bat: Visual Studio is installed without the C++ tools; add the "Desktop development with C++" workload. 1>&2
    exit /b 1
)
set "PATH=%PATH%;%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 1

if not exist "%OUT%" mkdir "%OUT%" || exit /b 1
set "OBJ=%TEMP%\ibasho-proccap-build"
if not exist "%OBJ%" mkdir "%OBJ%"
cl /nologo /O2 /MT /EHsc /std:c++17 /DUNICODE /D_UNICODE "%~dp0proccap.cpp" /Fe"%OUT%\proccap.exe" /Fo"%OBJ%\proccap.obj" || exit /b 1
echo built %OUT%\proccap.exe
