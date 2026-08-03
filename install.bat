@echo off
REM Installs the tandem CLI and the compute node on Windows, the same way
REM install.sh does on Linux and macOS. Run it from a checkout of the repo:
REM
REM   install.bat
REM
REM It sets up its own private Python environment (so it can't clash with other
REM Python packages you have), puts a `tandem` command on your PATH, and installs
REM the node. Safe to re-run any time. Set TANDEM_SKIP_NODE=1 to install just the
REM CLI and skip the node.

setlocal

REM The repo root is wherever this script lives. %~dp0 ends with a backslash, so
REM we trim it off to keep the paths tidy.
set "REPO_ROOT=%~dp0"
if "%REPO_ROOT:~-1%"=="\" set "REPO_ROOT=%REPO_ROOT:~0,-1%"

set "TANDEM_HOME=%USERPROFILE%\.tandem"
set "VENV_DIR=%TANDEM_HOME%\venv"
set "BIN_DIR=%TANDEM_HOME%\bin"
set "NODE_DEST=%BIN_DIR%\tandem-node.exe"

REM 1. Find a Python 3.10+ interpreter. Prefer the `py` launcher, then `python`.
set "PYTHON_CMD="
where py >nul 2>&1 && set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
  where python >nul 2>&1 && set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  echo Could not find Python. Install Python 3.10 or newer from
  echo   https://www.python.org/downloads/
  echo Tick "Add Python to PATH" during setup, then re-run install.bat.
  exit /b 1
)

echo Using Python:
%PYTHON_CMD% --version

REM 2. Create (or reuse) a private virtual environment just for the CLI.
if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo Creating a private environment at %VENV_DIR% ...
  %PYTHON_CMD% -m venv "%VENV_DIR%"
  if errorlevel 1 (
    echo Failed to create the virtual environment.
    exit /b 1
  )
) else (
  echo Reusing existing environment at %VENV_DIR%
  REM Older venvs (and some pre-release Python installs, like the 3.14 build
  REM that ships without ensurepip by default) end up without pip. Probe
  REM for it, try `ensurepip --upgrade` to repair in place, and only nuke
  REM + recreate the venv as a last resort -- otherwise users have to
  REM hand-delete %TANDEM_HOME%\venv just to re-run the installer.
  "%VENV_DIR%\Scripts\python.exe" -m pip --version >nul 2>&1
  if errorlevel 1 (
    echo The existing environment has no pip -- repairing it with ensurepip ...
    "%VENV_DIR%\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1
    if errorlevel 1 (
      echo ensurepip couldn't repair it -- recreating the environment from scratch.
      rmdir /s /q "%VENV_DIR%"
      %PYTHON_CMD% -m venv "%VENV_DIR%"
      if errorlevel 1 (
        echo Failed to create the virtual environment.
        exit /b 1
      )
    ) else (
      echo Repaired.
    )
  )
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

REM 3. Install the CLI into that environment. --force-reinstall means re-running
REM this always picks up local changes.
"%VENV_PY%" -m pip install --upgrade pip --quiet
echo Installing the tandem CLI ...
"%VENV_PY%" -m pip install --force-reinstall "%REPO_ROOT%\cli"
if errorlevel 1 (
  echo Failed to install the tandem CLI.
  exit /b 1
)

REM 4. Put a `tandem` command on PATH. We drop a tiny wrapper in the bin dir that
REM calls the venv's launcher, rather than copying the launcher, so it always
REM finds its own Python.
if not exist "%BIN_DIR%" mkdir "%BIN_DIR%"
(
  echo @echo off
  echo "%VENV_DIR%\Scripts\tandem.exe" %%*
) > "%BIN_DIR%\tandem.bat"

echo.
echo Tandem CLI installed.

REM 5. Logging in is all a node needs. As a convenience for the all-on-one-machine
REM dev setup, if this repo's server already has a token we save it as a fallback.
REM We check the environment, this repo's .env, then the server's generated token.
set "REGISTRATION_TOKEN=%TANDEM_NODE_REGISTRATION_TOKEN%"
set "TOKEN_SOURCE=the environment"
if not defined REGISTRATION_TOKEN if exist "%REPO_ROOT%\.env" (
  for /f "tokens=1,* delims==" %%A in ('findstr /b "TANDEM_NODE_REGISTRATION_TOKEN=" "%REPO_ROOT%\.env"') do set "REGISTRATION_TOKEN=%%B"
  set "TOKEN_SOURCE=%REPO_ROOT%\.env"
)
if not defined REGISTRATION_TOKEN if exist "%REPO_ROOT%\server\keys\node_registration_token.txt" (
  set /p REGISTRATION_TOKEN=<"%REPO_ROOT%\server\keys\node_registration_token.txt"
  set "TOKEN_SOURCE=the server's auto-generated token at %REPO_ROOT%\server\keys\node_registration_token.txt"
)

if defined REGISTRATION_TOKEN (
  call "%BIN_DIR%\tandem.bat" settings set-registration-token "%REGISTRATION_TOKEN%" >nul
  echo Saved this repo's server token ^(%TOKEN_SOURCE%^) as a fallback.
  echo.
)

REM 6. Install the compute node.
if "%TANDEM_SKIP_NODE%"=="1" (
  echo Skipping the node install because TANDEM_SKIP_NODE=1.
  goto path_check
)

REM An explicit prebuilt binary wins. This is the download-a-release flow:
REM   set "TANDEM_NODE_BIN=C:\path\to\tandem-node.exe" ^&^& install.bat
if defined TANDEM_NODE_BIN (
  if exist "%TANDEM_NODE_BIN%" (
    echo Using the prebuilt node binary at %TANDEM_NODE_BIN%
    copy /y "%TANDEM_NODE_BIN%" "%NODE_DEST%" >nul
    goto node_ok
  )
  echo warning: TANDEM_NODE_BIN is set but "%TANDEM_NODE_BIN%" does not exist; ignoring it.
)

REM Otherwise build it from the source in this repo, if Rust is available.
where cargo >nul 2>&1
if not errorlevel 1 (
  echo Building the Tandem node from source ^(this can take a few minutes the first time^)...
  cargo build --release --manifest-path "%REPO_ROOT%\node\Cargo.toml"
  if not errorlevel 1 (
    copy /y "%REPO_ROOT%\node\target\release\tandem-node.exe" "%NODE_DEST%" >nul
    goto node_ok
  )
  echo warning: building the node failed -- see the cargo output above.
)

REM No Cargo, but maybe there's already a build lying around from before.
if exist "%REPO_ROOT%\node\target\release\tandem-node.exe" (
  echo Cargo isn't installed, but found an existing node build -- using it.
  copy /y "%REPO_ROOT%\node\target\release\tandem-node.exe" "%NODE_DEST%" >nul
  goto node_ok
)

REM Nothing we can do on our own. Tell the user exactly what to do.
echo.
echo Could not install the Tandem node automatically ^(no Rust, no prebuilt binary^).
echo The CLI itself is installed and works -- you just can't start a node yet.
echo Pick whichever is easier:
echo   A^) Install Rust from https://rustup.rs then re-run install.bat
echo   B^) Download tandem-node.exe from a release, then run:
echo        set "TANDEM_NODE_BIN=C:\path\to\tandem-node.exe" ^&^& install.bat
goto compile_install

:node_ok
echo Tandem node installed at %NODE_DEST%

:compile_install
REM 7. Install the Tandem compile engine (tandem-compile). This is the little
REM Rust program `tandem build` / `tandem start` shell out to -- it turns
REM marked Python into a WASM component. componentize-py itself already came
REM along as one of the CLI's Python dependencies (step 3), so all we need
REM here is this binary.
set "COMPILE_DEST=%BIN_DIR%\tandem-compile.exe"

REM An explicit prebuilt binary wins, same as the node.
if defined TANDEM_COMPILE_BIN (
  if exist "%TANDEM_COMPILE_BIN%" (
    echo Using the prebuilt compile binary at %TANDEM_COMPILE_BIN%
    copy /y "%TANDEM_COMPILE_BIN%" "%COMPILE_DEST%" >nul
    goto compile_ok
  )
  echo warning: TANDEM_COMPILE_BIN is set but "%TANDEM_COMPILE_BIN%" does not exist; ignoring it.
)

REM Otherwise build it from the source in this repo, if Rust is available.
where cargo >nul 2>&1
if not errorlevel 1 (
  echo Building the Tandem compile engine from source ...
  cargo build --release --manifest-path "%REPO_ROOT%\sdk\core\Cargo.toml" --bin tandem-compile
  if not errorlevel 1 (
    copy /y "%REPO_ROOT%\sdk\target\release\tandem-compile.exe" "%COMPILE_DEST%" >nul
    goto compile_ok
  )
  echo warning: building tandem-compile failed -- see the cargo output above.
)

REM No Cargo, but maybe there's already a build lying around from before.
if exist "%REPO_ROOT%\sdk\target\release\tandem-compile.exe" (
  echo Cargo isn't installed, but found an existing compile build -- using it.
  copy /y "%REPO_ROOT%\sdk\target\release\tandem-compile.exe" "%COMPILE_DEST%" >nul
  goto compile_ok
)

REM Nothing we can do on our own. Tell the user exactly what to do.
echo.
echo Could not install the Tandem compile engine automatically ^(no Rust, no prebuilt binary^).
echo The CLI itself is installed and works -- `tandem build` / `tandem start` won't until this is built.
echo Pick whichever is easier:
echo   A^) Install Rust from https://rustup.rs then re-run install.bat
echo   B^) Download tandem-compile.exe from a release, then run:
echo        set "TANDEM_COMPILE_BIN=C:\path\to\tandem-compile.exe" ^&^& install.bat
goto path_check

:compile_ok
echo Tandem compile engine installed at %COMPILE_DEST%

:path_check
REM 8. Make sure the bin dir is on PATH for future terminals.
echo.
echo ";%PATH%;" | find /i ";%BIN_DIR%;" >nul
if errorlevel 1 (
  echo %BIN_DIR% isn't on your PATH yet. Add it once so `tandem` works everywhere:
  echo   - open Settings ^> System ^> About ^> Advanced system settings
  echo   - click Environment Variables, edit "Path" under User variables
  echo   - add a new entry:  %BIN_DIR%
  echo   - then open a NEW terminal
) else (
  echo Run: tandem --help
)

echo.
echo Next steps:
echo   1. Log in:            tandem auth login
echo   2. Start your node:   tandem node start
echo   3. Check on it:       tandem status
echo.
echo Your node needs to be running before you can deploy or start a job.

endlocal
exit /b 0
