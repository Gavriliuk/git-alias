@echo off

for /f "usebackq tokens=*" %%i in (`
  "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" ^
    -latest ^
    -products * ^
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 ^
    -property installationPath
`) do set "VSROOT=%%i"

if not defined VSROOT (
    echo Visual Studio C++ tools not found
    exit /b 1
)

call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" || exit /b 1

if exist pre-commit.exe del /q pre-commit.exe

cl /nologo /W3 /O2 /EHsc /std:c++17 /MT pre-commit.cpp /link /MACHINE:X64 /OUT:pre-commit.exe
if exist pre-commit.obj del /q pre-commit.obj
