<#
  U6Plus-Fixer.ps1 - Windows GUI for Fix-U6Plus.ps1 (UniFi U6+ eMMC recovery).
  Start it with U6Plus-Fixer.bat. The engine runs as a child process; its output is streamed into the
  log, and its ##-prefixed progress lines drive the stage list and the action banner.
#>
param(
    # Development aid: render the window, fed with the lines in -SnapshotFeed, to a PNG and exit.
    [string]$SnapshotPath,
    [string]$SnapshotFeed
)
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.Collections.Concurrent;
public class EngineRunner {
    public Process Proc;
    public ConcurrentQueue<string> Lines = new ConcurrentQueue<string>();
    public bool HasExited { get { return Proc != null && Proc.HasExited; } }
    public void Start(string file, string args, string workDir) {
        var psi = new ProcessStartInfo(file, args);
        psi.WorkingDirectory = workDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = System.Text.Encoding.UTF8;
        Proc = new Process();
        Proc.StartInfo = psi;
        Proc.OutputDataReceived += (s, e) => { if (e.Data != null) Lines.Enqueue(e.Data); };
        Proc.ErrorDataReceived += (s, e) => { if (e.Data != null && e.Data.Trim().Length > 0) Lines.Enqueue("[engine error] " + e.Data); };
        Proc.Start();
        Proc.BeginOutputReadLine();
        Proc.BeginErrorReadLine();
    }
    public void KillTree() {
        try {
            if (Proc != null && !Proc.HasExited) {
                var k = Process.Start(new ProcessStartInfo("taskkill", "/T /F /PID " + Proc.Id) { CreateNoWindow = true, UseShellExecute = false });
                k.WaitForExit(5000);
            }
        } catch { }
    }
}
'@
[System.Windows.Forms.Application]::EnableVisualStyles()

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Engine = Join-Path $Root 'Fix-U6Plus.ps1'
$script:Runner = $null
$script:Started = $null
$script:GotResult = $false
$script:Warnings = 0
$script:Unit = ''

# --------------------------------------------------------------- colours ----
$cNeutral = [Drawing.Color]::FromArgb(232, 236, 241)
$cWork = [Drawing.Color]::FromArgb(214, 230, 250)
$cPrompt = [Drawing.Color]::FromArgb(255, 214, 10)
$cGood = [Drawing.Color]::FromArgb(46, 160, 67)
$cBad = [Drawing.Color]::FromArgb(207, 34, 46)
$cWarn = [Drawing.Color]::FromArgb(191, 135, 0)
$cHead = [Drawing.Color]::FromArgb(9, 105, 218)

# ---------------------------------------------------------------- layout ----
$form = New-Object Windows.Forms.Form
$form.Text = 'U6+ eMMC Recovery'
$form.Size = New-Object Drawing.Size(1040, 780)
$form.MinimumSize = New-Object Drawing.Size(900, 640)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('Segoe UI', 9)

function New-Label($text, $x, $y, $w = 110) {
    $l = New-Object Windows.Forms.Label
    $l.Text = $text; $l.Location = New-Object Drawing.Point($x, $y); $l.Size = New-Object Drawing.Size($w, 22)
    $l.TextAlign = 'MiddleLeft'
    return $l
}

$gbConn = New-Object Windows.Forms.GroupBox
$gbConn.Text = 'Connection'; $gbConn.Location = New-Object Drawing.Point(12, 8); $gbConn.Size = New-Object Drawing.Size(470, 96)
$cbCom = New-Object Windows.Forms.ComboBox
$cbCom.DropDownStyle = 'DropDownList'; $cbCom.Location = New-Object Drawing.Point(130, 24); $cbCom.Size = New-Object Drawing.Size(240, 24)
$cbAdapter = New-Object Windows.Forms.ComboBox
$cbAdapter.DropDownStyle = 'DropDownList'; $cbAdapter.Location = New-Object Drawing.Point(130, 58); $cbAdapter.Size = New-Object Drawing.Size(240, 24)
$btnRefresh = New-Object Windows.Forms.Button
$btnRefresh.Text = 'Refresh'; $btnRefresh.Location = New-Object Drawing.Point(380, 23); $btnRefresh.Size = New-Object Drawing.Size(76, 26)
$gbConn.Controls.AddRange(@((New-Label 'Serial port' 12 25), $cbCom, $btnRefresh, (New-Label 'Network adapter' 12 59), $cbAdapter))

$gbAct = New-Object Windows.Forms.GroupBox
$gbAct.Text = 'Action'; $gbAct.Location = New-Object Drawing.Point(494, 8); $gbAct.Size = New-Object Drawing.Size(518, 96)
$gbAct.Anchor = 'Top, Left, Right'
$rbFix = New-Object Windows.Forms.RadioButton
$rbFix.Text = 'Fix a bricked unit'; $rbFix.Location = New-Object Drawing.Point(12, 22); $rbFix.Size = New-Object Drawing.Size(200, 22); $rbFix.Checked = $true
$rbReset = New-Object Windows.Forms.RadioButton
$rbReset.Text = 'Factory-reset a fixed unit'; $rbReset.Location = New-Object Drawing.Point(12, 46); $rbReset.Size = New-Object Drawing.Size(200, 22)
$chkFactory = New-Object Windows.Forms.CheckBox
$chkFactory.Text = 'Also wipe UniFi settings'; $chkFactory.Location = New-Object Drawing.Point(12, 70); $chkFactory.Size = New-Object Drawing.Size(200, 22)
$cbSpeed = New-Object Windows.Forms.ComboBox
$cbSpeed.DropDownStyle = 'DropDownList'; $cbSpeed.Location = New-Object Drawing.Point(330, 22); $cbSpeed.Size = New-Object Drawing.Size(170, 24)
[void]$cbSpeed.Items.AddRange(@('Auto (recommended)', 'Fast: 25 MHz', 'Slow: 5 MHz')); $cbSpeed.SelectedIndex = 0
$chkSkip = New-Object Windows.Forms.CheckBox
$chkSkip.Text = 'Skip backup (not recommended)'; $chkSkip.Location = New-Object Drawing.Point(230, 58); $chkSkip.Size = New-Object Drawing.Size(260, 22)
$gbAct.Controls.AddRange(@($rbFix, $rbReset, $chkFactory, (New-Label 'eMMC speed' 230 23 95), $cbSpeed, $chkSkip))

function New-Button($text, $x, $w = 120) {
    $b = New-Object Windows.Forms.Button
    $b.Text = $text; $b.Location = New-Object Drawing.Point($x, 112); $b.Size = New-Object Drawing.Size($w, 32)
    return $b
}
$btnStart = New-Button 'Start' 12 140
$btnStart.Font = New-Object Drawing.Font('Segoe UI', 10, [Drawing.FontStyle]::Bold)
$btnCancel = New-Button 'Cancel' 160; $btnCancel.Enabled = $false
$btnLogs = New-Button 'Open logs' 288
$btnBackups = New-Button 'Open backups' 416
$btnHelp = New-Button 'Instructions' 544

$lblBanner = New-Object Windows.Forms.Label
$lblBanner.Location = New-Object Drawing.Point(12, 152); $lblBanner.Size = New-Object Drawing.Size(1000, 64)
$lblBanner.Anchor = 'Top, Left, Right'
$lblBanner.TextAlign = 'MiddleCenter'
$lblBanner.Font = New-Object Drawing.Font('Segoe UI', 14, [Drawing.FontStyle]::Bold)
$lblBanner.BackColor = $cNeutral

$lvStages = New-Object Windows.Forms.ListView
$lvStages.View = 'Details'; $lvStages.FullRowSelect = $true; $lvStages.HeaderStyle = 'Nonclickable'
$lvStages.Location = New-Object Drawing.Point(12, 226); $lvStages.Size = New-Object Drawing.Size(340, 480)
$lvStages.Anchor = 'Top, Bottom, Left'
[void]$lvStages.Columns.Add('Stage', 250); [void]$lvStages.Columns.Add('Status', 80)

$rtb = New-Object Windows.Forms.RichTextBox
$rtb.Location = New-Object Drawing.Point(362, 226); $rtb.Size = New-Object Drawing.Size(650, 480)
$rtb.Anchor = 'Top, Bottom, Left, Right'
$rtb.ReadOnly = $true; $rtb.BackColor = [Drawing.Color]::White
$rtb.Font = New-Object Drawing.Font('Consolas', 9)
$rtb.WordWrap = $false

$status = New-Object Windows.Forms.StatusStrip
$stTime = New-Object Windows.Forms.ToolStripStatusLabel
$stUnit = New-Object Windows.Forms.ToolStripStatusLabel
$stWarn = New-Object Windows.Forms.ToolStripStatusLabel
$stTime.Text = 'Idle'; $stUnit.Text = ''; $stWarn.Text = ''
[void]$status.Items.AddRange(@($stTime, $stUnit, $stWarn))

$form.Controls.AddRange(@($gbConn, $gbAct, $btnStart, $btnCancel, $btnLogs, $btnBackups, $btnHelp, $lblBanner, $lvStages, $rtb, $status))

# --------------------------------------------------------------- helpers ----
function Set-Banner([string]$text, $color, $fore = [Drawing.Color]::Black) {
    $lblBanner.Text = $text; $lblBanner.BackColor = $color; $lblBanner.ForeColor = $fore
}
function Add-LogLine([string]$line, $color = [Drawing.Color]::Black, [switch]$Bold) {
    $rtb.SelectionStart = $rtb.TextLength
    $rtb.SelectionLength = 0
    $rtb.SelectionColor = $color
    if ($Bold) { $rtb.SelectionFont = New-Object Drawing.Font('Consolas', 9, [Drawing.FontStyle]::Bold) } else { $rtb.SelectionFont = $rtb.Font }
    $rtb.AppendText($line + "`n")
    $rtb.SelectionStart = $rtb.TextLength
    $rtb.ScrollToCaret()
}
function Load-Ports {
    $cbCom.Items.Clear()
    $sel = -1
    $ports = Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '\(COM\d+\)' } | Sort-Object Name
    foreach ($pt in $ports) {
        $com = [regex]::Match($pt.Name, 'COM\d+').Value
        $i = $cbCom.Items.Add("$com - $($pt.Name -replace '\s*\(COM\d+\)', '')")
        if ($sel -lt 0 -and $pt.Name -match 'USB|FT232|CH34|CP210|PL2303') { $sel = $i }
    }
    if ($cbCom.Items.Count -gt 0) { if ($sel -lt 0) { $sel = 0 }; $cbCom.SelectedIndex = $sel }
    $cbAdapter.Items.Clear()
    $sel = -1
    $ads = Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Where-Object { $_.MediaType -eq '802.3' } | Sort-Object Name
    foreach ($ad in $ads) {
        $i = $cbAdapter.Items.Add($ad.Name)
        if ($sel -lt 0) { $sel = $i }
    }
    if ($cbAdapter.Items.Count -gt 0) { $cbAdapter.SelectedIndex = $sel }
}
function Set-Running([bool]$running) {
    foreach ($c in @($gbConn, $gbAct, $btnStart)) { $c.Enabled = -not $running }
    $btnCancel.Enabled = $running
}
function Update-Options {
    $fix = $rbFix.Checked
    $chkFactory.Enabled = $fix
}
function Set-StageState([int]$n, [string]$state) {
    if ($n -lt 1 -or $n -gt $lvStages.Items.Count) { return }
    $it = $lvStages.Items[$n - 1]
    switch ($state) {
        'RUN' { $it.SubItems[1].Text = 'running'; $it.ForeColor = $cHead; $it.Font = New-Object Drawing.Font('Segoe UI', 9, [Drawing.FontStyle]::Bold) }
        'OK' { $it.SubItems[1].Text = 'done'; $it.ForeColor = $cGood; $it.Font = New-Object Drawing.Font('Segoe UI', 9) }
        'FAIL' { $it.SubItems[1].Text = 'FAILED'; $it.ForeColor = $cBad; $it.Font = New-Object Drawing.Font('Segoe UI', 9, [Drawing.FontStyle]::Bold) }
        'RETRY' { $it.SubItems[1].Text = 'retrying'; $it.ForeColor = $cWarn; $it.Font = New-Object Drawing.Font('Segoe UI', 9) }
    }
    if ($state -eq 'RUN') { Set-Banner "Working: $($it.Text)" $cWork }
}
function Handle-Line([string]$line) {
    if ($line.StartsWith('##STAGES ')) {
        $lvStages.Items.Clear()
        foreach ($s in $line.Substring(9).Split('|')) {
            $it = New-Object Windows.Forms.ListViewItem($s)
            [void]$it.SubItems.Add('waiting')
            $it.ForeColor = [Drawing.Color]::Gray
            [void]$lvStages.Items.Add($it)
        }
        return
    }
    if ($line -match '^##STAGE (\d+) (\w+)$') { Set-StageState ([int]$matches[1]) $matches[2]; return }
    if ($line.StartsWith('##PROMPT ')) {
        Set-Banner $line.Substring(9) $cPrompt
        [System.Media.SystemSounds]::Exclamation.Play()
        $form.Activate()
        return
    }
    if ($line -eq '##PROMPTCLEAR') { Set-Banner 'Working... leave the AP and cables alone' $cWork; return }
    if ($line.StartsWith('##WARN ')) { $script:Warnings++; $stWarn.Text = "Warnings: $($script:Warnings)"; return }
    if ($line.StartsWith('##UNIT ')) { $script:Unit = $line.Substring(7); $stUnit.Text = "Unit MAC: $($script:Unit)"; return }
    if ($line.StartsWith('##RESULT ')) {
        $script:GotResult = $true
        if ($line -eq '##RESULT SUCCESS') {
            if ($rbReset.Checked) { $msg = 'DONE: unit reset to factory defaults. Disconnect the serial adapter and adopt it.' }
            else { $msg = 'DONE: unit fixed. Disconnect the serial adapter, adopt it, and turn off auto-updates for it.' }
            Set-Banner $msg $cGood ([Drawing.Color]::White)
        } else {
            Set-Banner ('FAILED: ' + $line.Substring(16)) $cBad ([Drawing.Color]::White)
        }
        return
    }
    # Plain log line
    if ($line -match 'FAILED') { Add-LogLine $line $cBad -Bold }
    elseif ($line -match 'WARNING') { Add-LogLine $line $cWarn }
    elseif ($line -match 'SUCCESS') { Add-LogLine $line $cGood -Bold }
    elseif ($line -match '\] == ') { Add-LogLine $line $cHead -Bold }
    elseif ($line -match '\[engine error\]') { Add-LogLine $line $cBad }
    else { Add-LogLine $line }
}

# ---------------------------------------------------------------- events ----
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 150
$timer.Add_Tick({
    if (-not $script:Runner) { return }
    $line = $null
    $n = 0
    while ($n -lt 200 -and $script:Runner.Lines.TryDequeue([ref]$line)) { Handle-Line $line; $n++ }
    if ($script:Started) {
        $el = (Get-Date) - $script:Started
        $stTime.Text = 'Elapsed {0:mm\:ss}' -f $el
    }
    if ($script:Runner.HasExited -and $script:Runner.Lines.IsEmpty) {
        $timer.Stop()
        if (-not $script:GotResult) { Set-Banner 'Stopped before finishing. See the log; you can run it again.' $cBad ([Drawing.Color]::White) }
        $script:Runner = $null
        Set-Running $false
    }
})

$btnRefresh.Add_Click({ Load-Ports })
$rbFix.Add_CheckedChanged({ Update-Options })
$rbReset.Add_CheckedChanged({ Update-Options })
$btnLogs.Add_Click({ $d = Join-Path $Root 'runs'; New-Item -ItemType Directory $d -Force | Out-Null; Start-Process explorer.exe $d })
$btnBackups.Add_Click({ $d = Join-Path $Root 'backups'; New-Item -ItemType Directory $d -Force | Out-Null; Start-Process explorer.exe $d })
$btnHelp.Add_Click({ Start-Process notepad.exe (Join-Path $Root 'README.md') })

$btnStart.Add_Click({
    if ($cbCom.SelectedIndex -lt 0) { [Windows.Forms.MessageBox]::Show('No serial port found. Plug in the USB-serial adapter and press Refresh.', 'U6+ Recovery') | Out-Null; return }
    if ($cbAdapter.SelectedIndex -lt 0) { [Windows.Forms.MessageBox]::Show('No wired network adapter found.', 'U6+ Recovery') | Out-Null; return }
    $com = ($cbCom.SelectedItem.ToString() -split ' ')[0]
    $adapter = $cbAdapter.SelectedItem.ToString()
    if ($rbFix.Checked) { $mode = 'Fix' } else { $mode = 'Reset' }
    $speed = @('auto', 'fast', 'slow')[$cbSpeed.SelectedIndex]
    $checklist = "Before starting:`n`n- Serial adapter wired: RX to AP TX, TX to AP RX, GND to GND (no VCC)`n- PoE injector LAN port cabled straight to this PC (no switch or router)`n- The AP is UNPOWERED`n`nStart $($mode.ToLower()) on $com / $adapter ?"
    if ([Windows.Forms.MessageBox]::Show($checklist, 'U6+ Recovery', 'OKCancel', 'Information') -ne 'OK') { return }
    $argList = "-NoProfile -ExecutionPolicy Bypass -File `"$Engine`" -Gui -Mode $mode -ComPort $com -Adapter `"$adapter`" -Speed $speed"
    if ($mode -eq 'Fix' -and $chkFactory.Checked) { $argList += ' -FactoryReset' }
    if ($chkSkip.Checked) { $argList += ' -SkipBackup' }
    $rtb.Clear(); $lvStages.Items.Clear()
    $script:GotResult = $false; $script:Warnings = 0; $stWarn.Text = ''; $stUnit.Text = ''
    Set-Banner 'Starting...' $cWork
    Add-LogLine "powershell.exe $argList" ([Drawing.Color]::Gray)
    $script:Runner = New-Object EngineRunner
    $script:Runner.Start('powershell.exe', $argList, $Root)
    $script:Started = Get-Date
    Set-Running $true
    $timer.Start()
})

$btnCancel.Add_Click({
    $q = 'Stop the run now? The AP may be left part-way through. Running Fix again is safe.'
    if ([Windows.Forms.MessageBox]::Show($q, 'U6+ Recovery', 'YesNo', 'Warning') -ne 'Yes') { return }
    if ($script:Runner) { $script:Runner.KillTree() }
    Get-Process mtk_uartboot -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Add-LogLine 'Cancelled by user.' $cBad -Bold
})

$form.Add_FormClosing({
    param($s, $e)
    if ($script:Runner -and -not $script:Runner.HasExited) {
        $q = 'A run is still in progress. Stop it and close?'
        if ([Windows.Forms.MessageBox]::Show($q, 'U6+ Recovery', 'YesNo', 'Warning') -ne 'Yes') { $e.Cancel = $true; return }
        $script:Runner.KillTree()
        Get-Process mtk_uartboot -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    }
})

Load-Ports
Update-Options
Set-Banner 'Ready. Wire the serial adapter, cable the PoE injector to this PC, leave the AP unpowered, then press Start.' $cNeutral
if ($SnapshotPath) {
    # Rendered entirely off-screen from the form's own controls (never from screen pixels). The
    # RichTextBox log does not draw into DrawToBitmap, so a plain TextBox stands in for it here.
    $form.StartPosition = 'Manual'; $form.Location = New-Object Drawing.Point(-3000, -3000); $form.ShowInTaskbar = $false
    $form.Show()
    if ($SnapshotFeed) { foreach ($l in (Get-Content $SnapshotFeed)) { Handle-Line $l } ; Set-Running $true }
    $tb = New-Object Windows.Forms.TextBox
    $tb.Multiline = $true; $tb.ReadOnly = $true; $tb.BackColor = [Drawing.Color]::White; $tb.ScrollBars = 'None'
    $tb.Font = $rtb.Font; $tb.Bounds = $rtb.Bounds; $tb.Anchor = $rtb.Anchor; $tb.WordWrap = $false
    $tb.Text = ($rtb.Lines -join "`r`n")
    $form.Controls.Add($tb); $tb.BringToFront(); $rtb.Visible = $false
    [Windows.Forms.Application]::DoEvents()
    $bmp = New-Object Drawing.Bitmap($form.Width, $form.Height)
    $form.DrawToBitmap($bmp, (New-Object Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
    $bmp.Save($SnapshotPath, [Drawing.Imaging.ImageFormat]::Png)
    Write-Output "log lines: $($rtb.Lines.Count); first: $($rtb.Lines[0]); last: $($rtb.Lines[$rtb.Lines.Count - 2])"
    $form.Close()
    return
}
[void]$form.ShowDialog()
