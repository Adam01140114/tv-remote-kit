# TV Remote Kit: set up on Windows.
#   1. checks Google Chrome and Python 3,
#   2. installs the Python packages (Selenium, psutil),
#   3. makes the helper start when you sign in (a Startup shortcut) and starts it,
#   4. prints your links.
#
#   powershell -ExecutionPolicy Bypass -File setup\install-windows.ps1
#   powershell -ExecutionPolicy Bypass -File setup\install-windows.ps1 -NoAutostart
param([switch]$NoAutostart)
$ErrorActionPreference = "Stop"

$Kit = Split-Path -Parent $PSScriptRoot
$App = Join-Path $Kit "app"
Write-Host "TV Remote Kit, from: $Kit"

$chromes = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")
if (-not ($chromes | Where-Object { Test-Path $_ })) {
  Write-Host "ERROR: Google Chrome isn't installed. Install it from https://www.google.com/chrome/ and run this again."
  exit 1
}

# Python: the "py" launcher first, then python.exe (not the Microsoft Store placeholder).
$Python = $null
foreach ($cand in @("py", "python")) {
  try {
    $exe = & $cand -3 -c "import sys; print(sys.executable)" 2>$null
    if (-not $exe) { $exe = & $cand -c "import sys; print(sys.executable)" 2>$null }
    if ($exe -and (Test-Path $exe) -and ($exe -notmatch "WindowsApps")) { $Python = $exe.Trim(); break }
  } catch {}
}
if (-not $Python) {
  Write-Host "ERROR: Python 3 isn't installed. Install it from https://www.python.org/downloads/windows/"
  Write-Host "       (tick 'Add python.exe to PATH' in the installer) and run this again."
  exit 1
}
Write-Host "Python: $Python"

& $Python -m pip install --quiet --upgrade -r (Join-Path $Kit "setup\requirements.txt")
if ($LASTEXITCODE -ne 0) { & $Python -m pip install --quiet --user --upgrade -r (Join-Path $Kit "setup\requirements.txt") }
& $Python -c "import selenium, psutil; print('Selenium', selenium.__version__, '| psutil', psutil.__version__)"
New-Item -ItemType Directory -Force -Path (Join-Path $App "nowplaying"), (Join-Path $App "picks") | Out-Null

if ($NoAutostart) {
  Write-Host "OK. Start it with:  `"$Python`" `"$App\builder-server.py`""
  exit 0
}

# pythonw.exe runs it without a console window.
$Pythonw = Join-Path (Split-Path $Python) "pythonw.exe"
if (-not (Test-Path $Pythonw)) { $Pythonw = $Python }

# Start at sign-in: a shortcut in the Startup folder.
$Startup = [Environment]::GetFolderPath("Startup")
$Link = Join-Path $Startup "TV Remote Kit.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($Link)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "`"$App\builder-server.py`" --no-open"
$Shortcut.WorkingDirectory = $App
$Shortcut.WindowStyle = 7
$Shortcut.Save()

# Start it now (unless it's already running).
$port = 8766
try { $port = (Get-Content (Join-Path $App "settings.json") -Raw | ConvertFrom-Json).builder_port } catch {}
if (-not $port) { $port = 8766 }
$running = $false
try { Invoke-WebRequest -UseBasicParsing "http://localhost:$port/command-builder.html" -TimeoutSec 3 | Out-Null; $running = $true } catch {}
if (-not $running) {
  Start-Process -FilePath $Pythonw -ArgumentList "`"$App\builder-server.py`" --no-open" -WorkingDirectory $App -WindowStyle Hidden
}
for ($i = 0; $i -lt 30; $i++) {
  try { Invoke-WebRequest -UseBasicParsing "http://localhost:$port/command-builder.html" -TimeoutSec 3 | Out-Null; $running = $true; break } catch { Start-Sleep 1 }
}
if (-not $running) {
  Write-Host "ERROR: the helper didn't start. Run it by hand to see why:"
  Write-Host "  `"$Python`" `"$App\builder-server.py`""
  exit 1
}

Write-Host ""
Write-Host "Running. It starts by itself whenever you sign in."
Write-Host "If Windows asks whether Python may use the network, allow it on Private networks (your phone and TV connect over Wi-Fi)."
& $Python (Join-Path $Kit "setup\links.py")
