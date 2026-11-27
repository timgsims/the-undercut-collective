# Connects the F1 grid-penalty search (penalty_watch.py) to Claude with its own token, nothing to
# copy or paste. Copied from sippy/ops/connect-claude.ps1 (27 Nov 2026). Until then it used a copy of
# HomeBase's token; every app now gets its own (root CLAUDE.md, "Connecting an app to Tim's Claude account").
# Claude starts this in the Claude desktop app's Terminal panel; Tim only clicks Authorize:
#   powershell -ExecutionPolicy Bypass -File "C:\Users\timst\OneDrive\Documents\Home Network, Technology & PC\HomeLab\f1-fantasy\ops\connect-claude.ps1"
# 1. runs `claude setup-token` in this window (approve in the browser; it prints the token here)
# 2. reads the token off this window's screen (joining it back up if it wrapped),
# 3. saves it on Sentinel (~/.f1-claude/token, 0600; written to a temp file first, so a failed save
#    leaves the old token in place) and clears the screen.
$exe = Get-ChildItem "$env:LOCALAPPDATA\Claude-Data" -Recurse -Filter claude.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\claude-code\\' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $exe) { Write-Host "Couldn't find Claude Code (claude.exe)." -ForegroundColor Red; exit 1 }

Write-Host "Approve the sign-in in the browser. The token will show below; you don't need to copy it." -ForegroundColor Cyan
& $exe.FullName setup-token

# Read the whole screen buffer back. A row that runs to the right edge carries on in the next row.
$ui = $Host.UI.RawUI
$w = $ui.BufferSize.Width
$rect = New-Object System.Management.Automation.Host.Rectangle 0, 0, ($w - 1), ($ui.CursorPosition.Y)
$cells = $ui.GetBufferContents($rect)
$text = New-Object System.Text.StringBuilder
for ($y = 0; $y -le $cells.GetUpperBound(0); $y++) {
    $row = -join (0..($w - 1) | ForEach-Object { $cells[$y, $_].Character })
    if ($row[$w - 1] -eq ' ') { [void]$text.Append($row.TrimEnd()).Append("`n") } else { [void]$text.Append($row) }
}
$found = [regex]::Matches($text.ToString(), 'sk-ant-oat01-[A-Za-z0-9_-]{40,}')
$text = $null
if ($found.Count -eq 0) { Write-Host "No token on screen, so nothing was saved. Run this again." -ForegroundColor Red; exit 1 }

$found[$found.Count - 1].Value | ssh sentinel-admin@100.69.34.6 "umask 077 && cat > ~/.f1-claude/token.new && test -s ~/.f1-claude/token.new && mv ~/.f1-claude/token.new ~/.f1-claude/token && chmod 600 ~/.f1-claude/token"
$ok = $LASTEXITCODE -eq 0
$found = $null
Clear-Host
if ($ok) { Write-Host "Saved. The F1 penalty search has its own Claude token. Tell Claude it's done." -ForegroundColor Green }
else { Write-Host "Couldn't save it on Sentinel (ssh exit $LASTEXITCODE). The old token is still in place." -ForegroundColor Red }
