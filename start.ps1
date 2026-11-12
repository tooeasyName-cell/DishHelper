param([int]$Port = 8501, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    $venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) {
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        $pyCommand = Get-Command py -ErrorAction SilentlyContinue
        $candidates = @()
        if (Test-Path -LiteralPath $bundledPython) {
            $candidates += @{ Exe = $bundledPython; Prefix = @() }
        }
        if ($pythonCommand -and $pythonCommand.Source -notlike '*\Microsoft\WindowsApps\*') {
            $candidates += @{ Exe = $pythonCommand.Source; Prefix = @() }
        }
        if ($pyCommand) { $candidates += @{ Exe = $pyCommand.Source; Prefix = @('-3') } }
        $selectedPython = $null
        foreach ($candidate in $candidates) {
            $pythonExe = $candidate.Exe
            $pythonPrefix = $candidate.Prefix
            try {
                & $pythonExe @pythonPrefix -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
                if ($LASTEXITCODE -eq 0) { $selectedPython = $candidate; break }
            }
            catch { continue }
        }
        if (-not $selectedPython) { throw 'Python 3.12+ is required. Install Python, then run start.cmd again.' }
        $pythonExe = $selectedPython.Exe
        $pythonPrefix = $selectedPython.Prefix
        & $pythonExe @pythonPrefix -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
    }
    & $venvPython -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('streamlit') else 1)"
    if ($LASTEXITCODE -ne 0) {
        & $venvPython -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network access, then retry.' }
    }
    $launcherArguments = @('launcher.py', '--port', "$Port")
    if ($NoBrowser) { $launcherArguments += '--no-browser' }
    & $venvPython @launcherArguments
    if ($LASTEXITCODE -ne 0) { throw 'The server stopped with an error. Review the message above.' }
}
finally { Pop-Location }
