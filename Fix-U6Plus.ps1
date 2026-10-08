<#
  Fix-U6Plus.ps1 - recovery engine for UniFi U6+ (UAPL6, MT7981) units bricked by eMMC errors.
  Run it through U6Plus-Fixer.bat (GUI) or Run-Fix.bat (console).

  Modes
    Fix    Make a bricked unit boot UniFi 6.7.54 on its own: 1-bit eMMC boot loader in boot0, patched
           UniFi kernel (1-bit eMMC, firmware updates blocked) in SPI flash, U-Boot bootcmd_real.
    Reset  Factory-reset a unit that was already fixed (wipes the UniFi config partition).

  Recovery paths tried automatically
    - BootROM upload glitch            -> power-cycle and retry (3x)
    - Boot loader can't read eMMC      -> load U-Boot over the serial line instead, and repair the
                                          eMMC U-Boot partition if it is damaged
    - Missed the U-Boot window         -> power-cycle and retry
    - eMMC unreliable at 25 MHz        -> drop to 5 MHz in place, or reboot (from software) into 5 MHz
    - No DHCP address from OpenWrt     -> give OpenWrt a link-local address over the serial console
    - TFTP / SPI flash write problems  -> retried, every write read back and compared

  Wiring: USB-serial RX->AP TX, TX->AP RX, GND->GND (no VCC). AP LAN (via PoE injector) cabled straight
  to this PC. Start with the AP unpowered.
#>
param(
    [ValidateSet('Fix', 'Reset')][string]$Mode = 'Fix',
    [string]$ComPort,
    [string]$Adapter,
    [switch]$FactoryReset,
    [switch]$SkipBackup,
    # Linux eMMC speed: auto = 25 MHz if the read test passes, else 5 MHz
    [ValidateSet('auto', 'fast', 'slow')][string]$Speed = 'auto',
    # Machine-readable progress lines for the GUI
    [switch]$Gui,
    # Testing aid: pretend the 25 MHz test failed and the chip did not initialise, to exercise the
    # software-reboot path to the slow profile.
    [switch]$TestSoftReboot
)

$ErrorActionPreference = 'Stop'
if ($Gui) { [Console]::OutputEncoding = [Text.Encoding]::UTF8 }
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Img = Join-Path $Root 'images'
$Bin = Join-Path $Root 'bin'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$RunDir = Join-Path $Root "runs\$Stamp-$($Mode.ToLower())"
New-Item -ItemType Directory $RunDir -Force | Out-Null
$LogFile = Join-Path $RunDir 'fix.log'
$ConsoleLog = Join-Path $RunDir 'console.log'

# The boot loader always runs the eMMC at 400 kHz: at 25 MHz some chips miss BL2's 100 ms busy timeout
# after the partition switch (seen on a Samsung 4FTE4R) and the unit then fails to boot. Linux copes with
# slow chips, so its speed is picked by a read test.
$Profiles = @{
    fast = @{ Name = 'Linux eMMC 1-bit 25 MHz'; OpenWrt = 'openwrt-ram-1bit.itb'; Kernel = 'unifi-6.7.54-1bit-noupdate.itb'; WriteKernel0 = $true; TestMiB = 32 }
    # 5 MHz, not 400 kHz: Linux reads 128 KB per request with a 5 s timeout, which 400 kHz cannot meet.
    slow = @{ Name = 'Linux eMMC 1-bit 5 MHz'; OpenWrt = 'openwrt-ram-1bit-5mhz.itb'; Kernel = 'unifi-6.7.54-1bit-5mhz-noupdate.itb'; WriteKernel0 = $true; TestMiB = 8 }
}
$Bl2Emmc = 'bl2-emmc-x1-400k'
$StockFip = Join-Path $Img 'u6plus-stock-fip-6.7.54.bin'
$Kernel0Orig = Join-Path $Img 'unifi-6.7.54-kernel0-original.itb'
$NorOffset = '0x100000'
$NorLimit = 0xF00000
$script:OwrtIp = '192.168.1.1'
$script:Loader = 'emmc'          # 'emmc' = BL2 loads U-Boot from eMMC; 'ramfip' = U-Boot sent over serial
$script:PoweredOnce = $false
$script:StageNo = 0
$script:LastPrompt = ''

# ------------------------------------------------------------------ output ----
function Emit([string]$line) { if ($Gui) { [Console]::Out.WriteLine($line); [Console]::Out.Flush() } }
function Log([string]$m, [string]$color = 'Gray') {
    $line = '[{0}] {1}' -f (Get-Date -Format 'HH:mm:ss'), $m
    if ($Gui) { Emit $line } else { Write-Host $line -ForegroundColor $color }
    Add-Content -Path $LogFile -Value $line
}
function Warn([string]$m) { Emit "##WARN $m"; Log "WARNING: $m" 'Yellow' }
function Prompt([string]$m) {
    $script:LastPrompt = $m
    Emit "##PROMPT $m"
    if (-not $Gui) { Write-Host ''; Write-Host "  >>> $m <<<" -ForegroundColor Yellow; Write-Host '' }
    Add-Content -Path $LogFile -Value "[PROMPT] $m"
}
function PromptClear { if ($script:LastPrompt) { $script:LastPrompt = ''; Emit '##PROMPTCLEAR' } }
function Stage([int]$n, [string]$title) {
    if ($script:StageNo -gt 0 -and $script:StageNo -ne $n) { Emit "##STAGE $($script:StageNo) OK" }
    $script:StageNo = $n
    Emit "##STAGE $n RUN"
    Log ''
    Log "== Stage $n`: $title" 'Cyan'
}
function Stop-Loader { Get-Process mtk_uartboot -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue }
function Fail([string]$m) {
    PromptClear
    Emit "##STAGE $($script:StageNo) FAIL"
    Log "FAILED: $m" 'Red'
    Log "Logs: $RunDir" 'Red'
    Emit "##RESULT FAILED $m"
    Ser-Close
    Stop-Loader
    exit 1
}
function PowerPrompt {
    if (-not $script:PoweredOnce) { $script:PoweredOnce = $true; return "PLUG IN THE AP's POWER (PoE) NOW" }
    return 'UNPLUG THE PoE, WAIT 5 SECONDS, THEN PLUG IT BACK IN'
}

# ------------------------------------------------------------------ serial ----
$script:Port = $null
$script:Console = New-Object Text.StringBuilder
function Ser-Open {
    if ($script:Port -and $script:Port.IsOpen) { return }
    $script:Port = New-Object System.IO.Ports.SerialPort $ComPort, 115200, 'None', 8, 'One'
    $script:Port.ReadBufferSize = 1MB
    $script:Port.Open()
}
function Ser-Close { if ($script:Port -and $script:Port.IsOpen) { $script:Port.Close() } }
function Ser-Read {
    $s = $script:Port.ReadExisting()
    if ($s) { [void]$script:Console.Append($s); [IO.File]::AppendAllText($ConsoleLog, $s) }
    return $s
}
function Ser-Wait([string]$Pattern, [int]$TimeoutSec) {
    $buf = ''
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ($t.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $s = Ser-Read
        if ($s) { $buf += $s; if ($buf -match $Pattern) { return $buf } }
        Start-Sleep -Milliseconds 30
    }
    return $null
}
function UB([string]$Cmd, [int]$TimeoutSec = 10) {
    for ($try = 1; $try -le 2; $try++) {
        [void](Ser-Read)
        $script:Port.Write("$Cmd`r")
        $out = Ser-Wait 'MT7981# $' $TimeoutSec
        if ($null -ne $out) { return $out }
        $script:Port.Write([string][char]3); Start-Sleep -Milliseconds 600; [void](Ser-Read)
        if ($TimeoutSec -gt 60) { break }   # long commands are not blindly repeated
    }
    throw "U-Boot command timed out: $Cmd"
}
# Send <Esc> continuously until U-Boot's autoboot is stopped and its prompt answers.
function Catch-UBoot([int]$TimeoutSec = 90) {
    $esc = [string][char]27
    $mark = $script:Console.Length
    $t = [Diagnostics.Stopwatch]::StartNew()
    $probe = [Diagnostics.Stopwatch]::StartNew()
    $hit = $null
    while ($t.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $script:Port.Write($esc)
        Start-Sleep -Milliseconds 20
        [void](Ser-Read)
        $recent = $script:Console.ToString($mark, $script:Console.Length - $mark)
        if ($recent -match 'hw code|BL2: v|NOTICE') { PromptClear }
        if (-not $hit -and $recent -match 'Autobooting|MT7981#') { $hit = [Diagnostics.Stopwatch]::StartNew() }
        if ($hit -and $hit.Elapsed.TotalSeconds -gt 3) { break }
        if ($recent -match 'Starting kernel|ubnt boot') { throw 'missed the U-Boot autoboot window' }
        if (-not $hit -and $probe.Elapsed.TotalSeconds -gt 2) { $script:Port.Write([string][char]3); $probe.Restart() }
    }
    if (-not $hit) { throw 'U-Boot never appeared on the serial console' }
    for ($i = 0; $i -lt 3; $i++) { $script:Port.Write([string][char]3); Start-Sleep -Milliseconds 400 }
    [void](Ser-Read)
    $v = UB 'version' 5
    if ($v -notmatch 'U-Boot 20') { throw 'U-Boot prompt not responding' }
}

# ----------------------------------------------------------- UART loading ----
# Run mtk_uartboot and watch its log. Returns 'ok', 'emmc', 'rejected' or 'noresponse'.
# With -Trigger the AP is rebooted from software (the BootROM listens on the serial line at every
# reset, not only at power-on); the power prompt is shown only if no handshake follows within 45 s.
function Run-Loader([string[]]$LoaderArgs, [string]$Tag, [string]$OkPattern, [string]$OkOnExitPattern, [scriptblock]$Trigger) {
    Stop-Loader
    $ubLog = Join-Path $RunDir "mtk_uartboot-$Tag.log"
    Remove-Item $ubLog, "$ubLog.err" -ErrorAction SilentlyContinue
    $proc = Start-Process -FilePath (Join-Path $Bin 'mtk_uartboot.exe') -WorkingDirectory $Img -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $ubLog -RedirectStandardError "$ubLog.err" -ArgumentList $LoaderArgs
    $prompted = $false
    if ($Trigger) {
        Log '  rebooting the AP from software (no need to touch the power)'
        Start-Sleep 1
        try { & $Trigger } catch { }
    } else {
        Prompt (PowerPrompt); $prompted = $true
    }
    $t = [Diagnostics.Stopwatch]::StartNew()
    $result = 'noresponse'
    $txt = ''
    while ($t.Elapsed.TotalSeconds -lt 300) {
        $txt = ((Get-Content $ubLog -Raw -ErrorAction SilentlyContinue) + (Get-Content "$ubLog.err" -Raw -ErrorAction SilentlyContinue))
        if ($txt -match 'hw code') { PromptClear }
        elseif (-not $prompted -and $t.Elapsed.TotalSeconds -gt 45) {
            Warn 'The AP did not restart into the loader by itself'
            Prompt (PowerPrompt); $prompted = $true
        }
        if ($txt -match $OkPattern) { $result = 'ok'; break }
        if ($txt -match 'panicked') { $result = 'rejected'; break }
        if ($txt -match 'Failed to load image|FIP boot source initialization failed|Failed to switch to UDA|PANIC at PC') { $result = 'emmc'; break }
        if ($proc.HasExited) {
            if ($OkOnExitPattern -and $txt -match $OkOnExitPattern) { $result = 'ok' }
            break
        }
        Start-Sleep -Milliseconds 20
    }
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    $proc.WaitForExit(2000) | Out-Null
    $errs = ([regex]::Matches($txt, 'MSDC: (Command has (timed out|CRC error)|CRC error)')).Count
    if ($errs) { Log "  boot loader saw $errs eMMC errors" }
    if ($result -eq 'noresponse' -and $txt -match 'Handshake' -and $txt -notmatch 'hw code') { Log '  no reply to the serial handshake' }
    return $result
}
function Load-Emmc([scriptblock]$Trigger) {
    Log "  loading $Bl2Emmc over UART (U-Boot comes from the eMMC)"
    # mtk_uartboot gives up before a 400 kHz BL2 has loaded U-Boot (~15 s); once BL2 has found the
    # u-boot partition we keep watching the console ourselves.
    return Run-Loader @('-s', $ComPort, '-p', "$Bl2Emmc.bin", '--aarch64', '--brom-load-baudrate', '115200', '-f', 'u6plus-stock-fip-6.7.54.bin') `
        'emmc' 'Hello BL31|U-Boot 20' "Located partition 'u-boot'" $Trigger
}
function Load-RamFip([scriptblock]$Trigger) {
    Log '  loading a RAM boot loader and U-Boot over UART (no eMMC needed)'
    return Run-Loader @('-s', $ComPort, '-p', 'bl2-ram-uartdl.bin', '--aarch64', '--brom-load-baudrate', '115200', '--bl2-load-baudrate', '115200', '-f', 'u6plus-stock-fip-6.7.54.bin') `
        'ramfip' 'Received FIP' '' $Trigger
}
# Get to a U-Boot prompt, trying every path. -SoftReboot reboots a running OpenWrt instead of asking
# for a power cycle on the first attempt; retries ask for a power cycle.
function Reach-UBoot([scriptblock]$SoftReboot) {
    $rejects = 0; $silent = 0; $misses = 0
    $trigger = $SoftReboot
    while ($true) {
        Ser-Close
        if ($script:Loader -eq 'emmc') { $r = Load-Emmc $trigger } else { $r = Load-RamFip $trigger }
        $trigger = $null
        if ($r -eq 'ok') {
            Ser-Open
            try { Catch-UBoot 90; PromptClear; Log '  U-Boot prompt reached'; return }
            catch {
                $misses++
                if ($misses -ge 3) { Fail "Could not stop U-Boot ($($_.Exception.Message)) after 3 tries." }
                Warn "Missed U-Boot ($($_.Exception.Message)); retrying"
                continue
            }
        } elseif ($r -eq 'rejected') {
            $rejects++
            if ($rejects -ge 3) { Fail 'The BootROM rejected the upload 3 times. Check the serial adapter and its cable.' }
            Warn 'The BootROM rejected the upload (a known one-off glitch); retrying'
        } elseif ($r -eq 'noresponse') {
            $silent++
            if ($silent -ge 2) { Fail 'No response from the AP. Check the serial wiring (try swapping RX and TX) and the PoE power.' }
            Warn 'No response from the AP. Check the serial wiring (swap RX and TX if unsure); retrying'
        } elseif ($r -eq 'emmc') {
            if ($script:Loader -eq 'emmc') {
                Warn 'The boot loader could not read U-Boot from the eMMC; switching to loading U-Boot over the serial line'
                $script:Loader = 'ramfip'
            } else {
                Fail 'Could not start U-Boot over the serial line either.'
            }
        }
    }
}

# --------------------------------------------------------------- network ----
function Get-PcIp {
    $a = Get-NetIPAddress -InterfaceAlias $Adapter -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.AddressState -eq 'Preferred' } | Select-Object -First 1
    if ($a) { return $a.IPAddress }
    return $null
}
function Wait-PcIp([int]$TimeoutSec = 90) {
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ($t.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $up = (Get-NetAdapter -Name $Adapter -ErrorAction SilentlyContinue).Status -eq 'Up'
        $ip = Get-PcIp
        if ($up -and $ip) { return $ip }
        Start-Sleep 2
    }
    return $null
}
function Get-PeerIp([string]$pc) {
    $o = $pc.Split('.')
    if ($o[0] -eq '169' -and $o[1] -eq '254') {
        if ($o[3] -eq '200') { $last = '201' } else { $last = '200' }
        return @("$($o[0]).$($o[1]).$($o[2]).$last", '255.255.0.0')
    }
    if ($o[3] -eq '250') { $last = '251' } else { $last = '250' }
    return @("$($o[0]).$($o[1]).$($o[2]).$last", '255.255.255.0')
}
# TFTP upload to U-Boot's 'tftpsrv'. U-Boot answers from a random port 1024-4095, which Windows
# Firewall only lets in if we've sent to it first, so every port in that range is primed.
function Tftp-Put([string]$LocalIp, [string]$Server, [string]$File) {
    $data = [IO.File]::ReadAllBytes($File)
    $u = New-Object System.Net.Sockets.UdpClient (New-Object System.Net.IPEndPoint ([Net.IPAddress]::Parse($LocalIp)), 0)
    try {
        $u.Client.ReceiveTimeout = 1000
        $srv = [Net.IPAddress]::Parse($Server)
        $one = [byte[]]@(0)
        for ($port = 1024; $port -le 4095; $port++) { [void]$u.Send($one, 1, (New-Object Net.IPEndPoint $srv, $port)) }
        $name = [IO.Path]::GetFileName($File)
        $req = [byte[]](@(0, 2) + [Text.Encoding]::ASCII.GetBytes($name) + @(0) + [Text.Encoding]::ASCII.GetBytes('octet') + @(0))
        $peer = $null
        for ($i = 0; $i -lt 30 -and -not $peer; $i++) {
            [void]$u.Send($req, $req.Length, (New-Object Net.IPEndPoint $srv, 69))
            try {
                $remote = New-Object Net.IPEndPoint ([Net.IPAddress]::Any), 0
                $r = $u.Receive([ref]$remote)
                if ($r[1] -eq 5) { throw 'TFTP server returned an error' }
                if ($r[1] -eq 4 -and $r[2] -eq 0 -and $r[3] -eq 0) { $peer = $remote }
            } catch [System.Net.Sockets.SocketException] { }
        }
        if (-not $peer) { throw 'no answer from U-Boot tftpsrv' }
        $blk = 512
        $n = [math]::Floor($data.Length / $blk) + 1
        $pkt = New-Object byte[] ($blk + 4)
        for ($i = 0; $i -lt $n; $i++) {
            $bno = ($i + 1) -band 0xffff
            $len = [math]::Min($blk, $data.Length - $i * $blk)
            $pkt[0] = 0; $pkt[1] = 3; $pkt[2] = [byte](($bno -shr 8) -band 0xff); $pkt[3] = [byte]($bno -band 0xff)
            if ($len -gt 0) { [Array]::Copy($data, $i * $blk, $pkt, 4, $len) }
            $acked = $false
            for ($rt = 0; $rt -lt 30 -and -not $acked; $rt++) {
                [void]$u.Send($pkt, $len + 4, $peer)
                try {
                    while ($true) {
                        $remote = New-Object Net.IPEndPoint ([Net.IPAddress]::Any), 0
                        $a = $u.Receive([ref]$remote)
                        if ($a[1] -eq 4 -and ((([int]$a[2]) -shl 8) -bor $a[3]) -eq $bno) { $acked = $true; break }
                    }
                } catch [System.Net.Sockets.SocketException] { }
            }
            if (-not $acked) { throw "TFTP timed out at block $($i + 1)" }
        }
    } finally { $u.Close() }
}
# Load a file into U-Boot RAM at 0x46000000 over TFTP and check the byte count (3 tries).
function Push-ToUBoot([string]$File) {
    $size = (Get-Item $File).Length
    for ($try = 1; $try -le 3; $try++) {
        try {
            $pc = Wait-PcIp 90
            if (-not $pc) { throw "no network link or address on '$Adapter' (is the PoE injector's LAN port cabled to this PC?)" }
            $ap = Get-PeerIp $pc
            [void](UB "setenv ipaddr $($ap[0])")
            [void](UB "setenv netmask $($ap[1])")
            [void](Ser-Read)
            $script:Port.Write("tftpsrv 0x46000000`r")
            if ($null -eq (Ser-Wait 'Loading:' 10)) { throw 'tftpsrv did not start' }
            Tftp-Put $pc $ap[0] $File
            $out = Ser-Wait 'Bytes transferred = \d+[\s\S]*MT7981# $' 60
            if ($null -eq $out -or $out -notmatch "Bytes transferred = $size ") { throw 'transfer incomplete' }
            Log ("  sent {0} ({1:N0} bytes) to {2}" -f (Split-Path $File -Leaf), $size, $ap[0])
            return
        } catch {
            if ($try -ge 3) { throw "Could not send $(Split-Path $File -Leaf) to the AP: $($_.Exception.Message)" }
            Warn "Transfer of $(Split-Path $File -Leaf) failed ($($_.Exception.Message)); retrying"
            $script:Port.Write([string][char]3); Start-Sleep 1; $script:Port.Write([string][char]3); Start-Sleep 1; [void](Ser-Read)
        }
    }
}
function Drop-Signature {
    [void](UB 'fdt addr ${fdtcontroladdr}')
    [void](UB ('fdt ' + 'rm /signature'))
}

# ------------------------------------------------------------ ssh / shell ----
$SshOpts = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=10'
function Ssh-Run([string]$RemoteCmd) {
    # Send the command as a script on stdin: avoids cmd.exe mangling quotes and pipes.
    $f = Join-Path $RunDir 'remote-cmd.sh'
    [IO.File]::WriteAllText($f, $RemoteCmd + "`n")
    $out = cmd /c "ssh $SshOpts root@$($script:OwrtIp) sh -s < ""$f"" 2>&1"
    return ($out | Out-String).Trim()
}
function Ssh-RunFile([string]$ScriptText, [string]$ArgText) {
    $f = Join-Path $RunDir 'remote-script.sh'
    [IO.File]::WriteAllText($f, $ScriptText + "`n")
    $out = cmd /c "ssh $SshOpts root@$($script:OwrtIp) ""sh -s $ArgText"" < ""$f"" 2>&1"
    return @($out)
}
function Ssh-Upload([string]$Local, [string]$Remote) {
    for ($try = 1; $try -le 2; $try++) {
        cmd /c "ssh $SshOpts root@$($script:OwrtIp) ""cat > $Remote"" < ""$Local"""
        $l = (Get-FileHash $Local -Algorithm SHA256).Hash.ToLower()
        $r = (Ssh-Run "sha256sum $Remote").Split(' ')[0]
        if ($l -eq $r) { return }
        Warn "Upload of $(Split-Path $Local -Leaf) was corrupted; retrying"
    }
    throw "Upload of $(Split-Path $Local -Leaf) failed twice"
}
function Ssh-Backup([string]$Name, [string]$RemoteCmd, [string]$Dir) {
    # Read the source once into the AP's RAM, hash it there, then download and compare.
    $f = Join-Path $Dir $Name
    $r = (Ssh-Run "$RemoteCmd > /tmp/bk.bin && sha256sum /tmp/bk.bin").Split(' ')[0]
    for ($try = 1; $try -le 2; $try++) {
        cmd /c "ssh $SshOpts root@$($script:OwrtIp) ""cat /tmp/bk.bin"" > ""$f"""
        $l = (Get-FileHash $f -Algorithm SHA256).Hash.ToLower()
        if ($l -eq $r) { break }
        if ($try -ge 2) { throw "Backup $Name does not match the AP" }
    }
    [void](Ssh-Run 'rm -f /tmp/bk.bin')
    Add-Content -Path (Join-Path $Dir 'SHA256SUMS.txt') -Value "$l  $Name"
    Log ("  {0,-26} {1,12:N0} bytes  verified" -f $Name, (Get-Item $f).Length)
}
function Test-Ssh([string]$ip) {
    return (Test-NetConnection $ip -Port 22 -WarningAction SilentlyContinue -InformationLevel Quiet)
}

# --------------------------------------------------------------- OpenWrt ----
# Boot OpenWrt in RAM with this profile's eMMC speed and get an SSH connection to it.
function Boot-OpenWrt($prf) {
    Push-ToUBoot (Join-Path $Img $prf.OpenWrt)
    Drop-Signature
    $o = UB 'iminfo 0x46000000' 30
    if ($o -notmatch 'Hash\(es\) for Image 0 \(kernel-1\): crc32\+ sha1\+') { throw 'OpenWrt image failed its hash check in U-Boot' }
    [void](Ser-Read)
    $script:Port.Write("bootm 0x46000000`r")
    if ($null -eq (Ser-Wait 'Please press Enter to activate this console' 180)) { throw 'OpenWrt did not finish booting' }
    Log '  OpenWrt is up (running from RAM)'
    # Path 1: OpenWrt's DHCP server gives the PC a 192.168.1.x address
    $t = [Diagnostics.Stopwatch]::StartNew()
    while (((Get-PcIp) -notlike '192.168.1.*') -and $t.Elapsed.TotalSeconds -lt 60) { Start-Sleep 2 }
    $script:OwrtIp = '192.168.1.1'
    for ($i = 0; $i -lt 10; $i++) { if (Test-Ssh $script:OwrtIp) { Log "  SSH to OpenWrt at $($script:OwrtIp)"; return }; Start-Sleep 3 }
    # Path 2: give OpenWrt an address in the PC's own subnet over the serial console
    $pc = Get-PcIp
    if (-not $pc) { throw 'The PC has no address on the AP link' }
    $peer = Get-PeerIp $pc
    $bits = 24; if ($peer[1] -eq '255.255.0.0') { $bits = 16 }
    Warn "No DHCP address from OpenWrt; adding $($peer[0]) to OpenWrt over the serial console"
    $script:Port.Write("`n"); Start-Sleep 1
    $script:Port.Write("ip addr add $($peer[0])/$bits dev br-lan`n"); Start-Sleep 2
    [void](Ser-Read)
    $script:OwrtIp = $peer[0]
    for ($i = 0; $i -lt 10; $i++) { if (Test-Ssh $script:OwrtIp) { Log "  SSH to OpenWrt at $($script:OwrtIp)"; return }; Start-Sleep 3 }
    throw 'Cannot reach OpenWrt over SSH'
}
function Get-EmmcErrors {
    $n = Ssh-Run 'dmesg | grep -iE "msdc|mmc0" | grep -viE "cmd=(52|8|5|55) " | grep -ciE "error|timeout|crc"'
    if ($n -match '^\d+$') { return [int]$n }
    return 999
}
function Test-EmmcPresent {
    $emmc = Ssh-Run 'grep -E "^(clock|bus width)" /sys/kernel/debug/mmc0/ios; cat /sys/block/mmcblk0/device/name 2>/dev/null; test -b /dev/mmcblk0 && echo BLOCK-OK'
    Log "  eMMC: $($emmc -replace '\s+', ' ')"
    return ($emmc -match 'bus width:\s+0 \(1 bits\)' -and $emmc -match 'BLOCK-OK')
}
# Read the same eMMC area twice, compare, and check the kernel log for new errors.
function Test-Emmc([int]$MiB, [int]$BaseErrors = 0) {
    if (-not (Test-EmmcPresent)) { Log '  eMMC is not present or not in 1-bit mode'; return $false }
    $lines = @((Ssh-Run "for i in 1 2; do echo 3 > /proc/sys/vm/drop_caches; dd if=/dev/mmcblk0 bs=1M count=$MiB 2>/dev/null | md5sum | cut -d' ' -f1; done") -split "`r?`n" | Where-Object { $_ })
    $errs = Get-EmmcErrors
    $ok = ($lines.Count -ge 2) -and ($lines[0] -match '^[0-9a-f]{32}$') -and ($lines[0] -eq $lines[1]) -and ($errs -le $BaseErrors)
    Log "  read test: $MiB MiB twice -> $($lines -join ' ') (new eMMC errors: $($errs - $BaseErrors))"
    return $ok
}
# Drop the running OpenWrt's eMMC clock to the slow profile's 5 MHz without rebooting.
function Set-EmmcSlowClock {
    $o = Ssh-Run 'echo 5000000 > /sys/kernel/debug/mmc0/clock; grep -E "^clock" /sys/kernel/debug/mmc0/ios'
    return ($o -match 'clock:\s+[1-5]\d{6} Hz')
}
# Partition table and data sanity before anything is written.
function Check-Layout {
    $layoutCmd = @(
        'for d in /sys/block/mmcblk0/mmcblk0p*; do echo "$(basename $d)=$(grep PARTNAME $d/uevent | cut -d= -f2)"; done'
        'echo fip=$(dd if=/dev/mmcblk0p4 bs=8 count=1 2>/dev/null | hexdump -v -e "8/1 \"%02x\"")'
        'echo eeprom=$(dd if=/dev/mtd0 bs=64 count=1 2>/dev/null | hexdump -v -e "64/1 \"%02x\"")'
        'grep -E "mtd[0-9]" /proc/mtd | tr "\n" " "'
    ) -join "`n"
    $info = Ssh-Run $layoutCmd
    $map = @{}
    foreach ($l in ($info -split "`r?`n")) { if ($l -match '^(\w+)=(.*)$') { $map[$matches[1]] = $matches[2].Trim() } }
    $want = @{ mmcblk0p1 = 'bl2'; mmcblk0p4 = 'u-boot'; mmcblk0p5 = 'EEPROM'; mmcblk0p6 = 'kernel0'; mmcblk0p9 = 'cfg' }
    foreach ($k in $want.Keys) {
        if ($map[$k] -ne $want[$k]) { throw "Unexpected eMMC partition layout ($k is '$($map[$k])', expected '$($want[$k])'). Not touching this unit." }
    }
    Log '  eMMC partition layout matches the U6+'
    $script:FipOk = ($map['fip'] -eq '010064aa78563412')
    if ($script:FipOk) { Log '  U-Boot partition has a valid FIP header' } else { Warn "U-Boot partition header is not a valid FIP ($($map['fip'])); it will be repaired" }
    $ee = $map['eeprom']
    if (-not $ee -or $ee -match '^(ff)+$' -or $ee -match '^(00)+$') { Warn 'The Wi-Fi calibration area (SPI EEPROM) looks blank. Wi-Fi may not work on this unit regardless of the fix.' }
    if ($info -notmatch 'mtd2: 00f70000') { throw 'SPI flash layout is not the expected 16 MB chip' }
}

# ================================================================== main ====
Log "U6+ eMMC recovery - mode $Mode - run $Stamp" 'White'
if ($Mode -eq 'Fix') {
    Emit '##STAGES Pre-flight checks|Reach U-Boot|Boot OpenWrt in RAM and test the eMMC|Back up this unit|Write boot loader to the eMMC|Write UniFi kernel to SPI flash|Unattended test boot'
} else {
    Emit '##STAGES Pre-flight checks|Reach U-Boot|Boot OpenWrt in RAM|Back up the UniFi config|Wipe the UniFi config|Test boot with factory defaults'
}

# ------------------------------------------------------------- pre-flight ----
Stage 1 'Pre-flight checks'
if (-not (Test-Path (Join-Path $Root 'SHA256SUMS.txt'))) { Fail 'The firmware images have not been prepared yet. Run Prepare.bat once (needs Python 3 and internet).' }
$sums = Get-Content (Join-Path $Root 'SHA256SUMS.txt')
foreach ($line in $sums) {
    $h, $n = $line -split '\s+', 2
    $fp = Join-Path $Root $n
    if (-not (Test-Path $fp)) { Fail "Missing package file $n" }
    if ((Get-FileHash $fp -Algorithm SHA256).Hash.ToLower() -ne $h) { Fail "Package file $n is corrupted (hash mismatch). Re-copy the tool folder." }
}
Log "  all $($sums.Count) package files present and intact"
if (-not $ComPort) {
    $ports = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\(COM\d+\)' -and $_.Name -match 'USB|FT232|CH34|CP210|PL2303|Serial' } |
        ForEach-Object { [regex]::Match($_.Name, 'COM\d+').Value } | Select-Object -Unique)
    if ($ports.Count -ne 1) { Fail "Could not pick the serial port automatically (found: $($ports -join ', ')). Choose it explicitly." }
    $ComPort = $ports[0]
}
if ([System.IO.Ports.SerialPort]::GetPortNames() -notcontains $ComPort) { Fail "Serial port $ComPort does not exist. Plug in the USB-serial adapter and pick its port." }
try { Ser-Open; Ser-Close } catch { Fail "Serial port $ComPort is in use by another program (close PuTTY or any serial terminal)." }
Log "  serial port: $ComPort (free)"
if (-not $Adapter) {
    $ads = @(Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' })
    if ($ads.Count -gt 1) { $ads = @($ads | Where-Object { $_.Status -eq 'Up' }) }
    if ($ads.Count -ne 1) { Fail "Could not pick the Ethernet adapter automatically (found $($ads.Count) wired adapters). Choose it explicitly." }
    $Adapter = $ads[0].Name
}
$na = Get-NetAdapter -Name $Adapter -ErrorAction SilentlyContinue
if (-not $na) { Fail "Network adapter '$Adapter' not found" }
Log "  network adapter: $Adapter ($($na.Status))"
$gw = Get-NetRoute -InterfaceAlias $Adapter -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue
$ip = Get-PcIp
if ($gw -and $ip -and $ip -notlike '169.254.*' -and $ip -notlike '192.168.1.*') {
    Fail "'$Adapter' is connected to a network ($ip, with a gateway). Cable the PoE injector straight to this PC: the recovery system runs a DHCP server that must not reach a real LAN."
}
if ($na.Status -eq 'Up') { Warn "'$Adapter' already has a link. Make sure the AP is UNPOWERED before you continue (the tool will ask you to plug it in)." }
if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) { Fail 'Windows OpenSSH client (ssh.exe) not found. Enable it under Settings > Apps > Optional features.' }
$free = (Get-PSDrive -Name ($Root.Substring(0, 1))).Free
if ($free -lt 200MB) { Fail "Less than 200 MB free on drive $($Root.Substring(0, 1)): for backups and logs" }
Stop-Loader

try {
    # --------------------------------------------- reach U-Boot + OpenWrt
    # One power-on in the normal case. If the eMMC fails at 25 MHz the tool first drops the clock to
    # 5 MHz in the running system; only if the chip didn't initialise at all does it reboot (from
    # software, with the loader waiting) into the 5 MHz system.
    if ($Speed -eq 'slow') { $first = 'slow' } else { $first = 'fast' }
    $prof = $null
    Stage 2 'Reach U-Boot'
    Reach-UBoot
    if ($Mode -eq 'Fix') { Stage 3 "Boot OpenWrt in RAM and test the eMMC ($($Profiles[$first].Name))" } else { Stage 3 'Boot OpenWrt in RAM' }
    Boot-OpenWrt $Profiles[$first]
    if ($first -eq 'slow') {
        if (Test-Emmc $Profiles.slow.TestMiB) { $prof = $Profiles.slow } else { Fail 'The eMMC is not reliable even at 5 MHz. This unit cannot be fixed this way.' }
    } elseif ((-not $TestSoftReboot) -and (Test-Emmc $Profiles.fast.TestMiB)) {
        $prof = $Profiles.fast
    } elseif ($Speed -eq 'fast') {
        Fail 'The eMMC is not reliable at 25 MHz (you chose Fast). Run again with Auto or Slow.'
    } else {
        Warn 'The eMMC is not reliable at 25 MHz; switching to 5 MHz'
        if ((-not $TestSoftReboot) -and (Test-EmmcPresent) -and (Set-EmmcSlowClock)) {
            $base = Get-EmmcErrors
            if (Test-Emmc $Profiles.slow.TestMiB $base) { $prof = $Profiles.slow; Log '  eMMC is reliable at 5 MHz (switched in place, no reboot needed)' }
        }
        if (-not $prof) {
            Warn 'Restarting into the 5 MHz recovery system'
            Emit "##STAGE 3 RETRY"; $script:StageNo = 0
            Stage 2 'Reach U-Boot'
            Reach-UBoot -SoftReboot { [void](Ssh-Run 'reboot') }
            Stage 3 "Boot OpenWrt in RAM and test the eMMC ($($Profiles.slow.Name))"
            Boot-OpenWrt $Profiles.slow
            if (Test-Emmc $Profiles.slow.TestMiB) { $prof = $Profiles.slow } else { Fail 'The eMMC is not reliable even at 5 MHz. This unit cannot be fixed this way.' }
        }
    }
    Log "  profile: $($prof.Name)" 'White'
    $mac = (Ssh-Run 'cat /sys/class/net/eth0/address').Replace(':', '').ToUpper()
    if ($mac -notmatch '^[0-9A-F]{12}$') { throw "Could not read the unit's MAC address ($mac)" }
    Log "  unit MAC: $mac"
    Emit "##UNIT $mac"
    Check-Layout

    if ($Mode -eq 'Fix') {
        # ------------------------------------------------------- backup
        Stage 4 'Back up this unit'
        if ($SkipBackup) {
            Warn 'Backup skipped at your request'
        } else {
            $bdir = Join-Path $Root "backups\$mac-$Stamp"
            New-Item -ItemType Directory $bdir -Force | Out-Null
            Ssh-Run 'cat /sys/kernel/debug/mmc0/mmc0:0001/ext_csd; echo; for f in name cid date life_time pre_eol_info; do echo "$f=$(cat /sys/block/mmcblk0/device/$f)"; done' | Out-File (Join-Path $bdir 'emmc-info.txt') -Encoding ascii
            Log "  eMMC wear: $(Ssh-Run 'echo life_time=$(cat /sys/block/mmcblk0/device/life_time) pre_eol=$(cat /sys/block/mmcblk0/device/pre_eol_info)')"
            Ssh-Backup 'boot0.bin' 'dd if=/dev/mmcblk0boot0 bs=64k 2>/dev/null' $bdir
            Ssh-Backup 'boot1.bin' 'dd if=/dev/mmcblk0boot1 bs=64k 2>/dev/null' $bdir
            Ssh-Backup 'emmc-gpt-p1-p5.bin' 'dd if=/dev/mmcblk0 bs=512 count=17536 2>/dev/null' $bdir
            Ssh-Backup 'emmc-p8-bs.bin' 'dd if=/dev/mmcblk0p8 bs=512 2>/dev/null' $bdir
            Ssh-Backup 'emmc-p9-cfg.bin' 'dd if=/dev/mmcblk0p9 bs=64k 2>/dev/null' $bdir
            Ssh-Backup 'emmc-gpt-backup.bin' 'dd if=/dev/mmcblk0 bs=512 skip=$(( $(cat /sys/block/mmcblk0/size) - 2048 )) count=2048 2>/dev/null' $bdir
            Ssh-Backup 'nor-mtd0-EEPROM.bin' 'cat /dev/mtd0' $bdir
            Ssh-Backup 'nor-mtd1-u-boot-env.bin' 'cat /dev/mtd1' $bdir
            Ssh-Backup 'nor-mtd2-rest-0x90000.bin' 'cat /dev/mtd2' $bdir
            Log "  backups in $bdir"
        }

        # ------------------------------------------------- eMMC writes
        Stage 5 'Write boot loader to the eMMC'
        $repairFip = (-not $script:FipOk) -or ($script:Loader -eq 'ramfip')
        Ssh-Upload (Join-Path $Img "$Bl2Emmc.img") '/tmp/bl2.img'
        if ($prof.WriteKernel0) { Ssh-Upload $Kernel0Orig '/tmp/kernel0.itb' } else { Log '  kernel0 copy skipped (not used for booting)' }
        if ($repairFip) { Log '  the eMMC U-Boot partition will be rewritten with the stock 6.7.54 FIP'; Ssh-Upload $StockFip '/tmp/fip.bin' }
        $sh = @(
            'set -e'
            'part() { for d in /sys/block/mmcblk0/mmcblk0p*; do grep -q "^PARTNAME=$1\$" $d/uevent && { echo /dev/$(basename $d); return 0; }; done; return 1; }'
            'K=$(part kernel0); C=$(part cfg); U=$(part u-boot)'
            '[ "$K" = /dev/mmcblk0p6 ] && [ "$C" = /dev/mmcblk0p9 ] && [ "$U" = /dev/mmcblk0p4 ] || { echo "LAYOUT-MISMATCH $K $C $U"; exit 1; }'
            'verify() { w=$(sha256sum "$1" | cut -d" " -f1); g=$(head -c "$(wc -c < "$1")" "$2" | sha256sum | cut -d" " -f1); [ "$w" = "$g" ] && echo "VERIFY-OK $2" || { echo "VERIFY-FAIL $2"; exit 1; }; }'
            'if [ -f /tmp/fip.bin ]; then dd if=/tmp/fip.bin of=$U bs=512 2>/dev/null; sync; echo 3 > /proc/sys/vm/drop_caches; verify /tmp/fip.bin $U; fi'
            'echo 0 > /sys/block/mmcblk0boot0/force_ro'
            'dd if=/tmp/bl2.img of=/dev/mmcblk0boot0 bs=512 2>/dev/null; sync'
            'echo 1 > /sys/block/mmcblk0boot0/force_ro'
            'echo 3 > /proc/sys/vm/drop_caches'
            'verify /tmp/bl2.img /dev/mmcblk0boot0'
            'if [ -f /tmp/kernel0.itb ]; then dd if=/tmp/kernel0.itb of=$K bs=512 2>/dev/null; sync; echo 3 > /proc/sys/vm/drop_caches; verify /tmp/kernel0.itb $K; fi'
            'if [ "$1" = wipecfg ]; then dd if=/dev/zero of=$C bs=512 count=$(cat /sys/block/mmcblk0/$(basename $C)/size) 2>/dev/null; sync; echo CFG-WIPED; fi'
            'n=$(dmesg | grep -iE "msdc|mmc0" | grep -viE "cmd=(52|8|5|55) " | grep -ciE "error|timeout|crc" || true); echo "EMMC-ERRORS $n"'
            'echo INSTALL-OK'
        ) -join "`n"
        $arg = ''; if ($FactoryReset) { $arg = 'wipecfg' }
        $res = Ssh-RunFile $sh $arg
        $res | ForEach-Object { Log "  $_" }
        $txt = $res | Out-String
        if ($txt -notmatch 'INSTALL-OK') { throw 'eMMC write step failed (see above)' }
        if ($txt -match 'EMMC-ERRORS ([1-9]\d*)') { Warn "Linux logged $($matches[1]) eMMC errors during the writes (all writes verified OK)" }

        # ------------------------------------------------- SPI flash
        Stage 6 'Write UniFi kernel to SPI flash'
        $KernelFile = Join-Path $Img $prof.Kernel
        $size = (Get-Item $KernelFile).Length
        if ($size -ge $NorLimit) { throw 'Kernel image too large for the SPI flash area' }
        $hex = '0x{0:x}' -f $size
        [void](Ser-Read)
        [void](Ssh-Run 'reboot')
        $caught = $false
        for ($try = 1; $try -le 2 -and -not $caught; $try++) {
            try { Catch-UBoot 120; $caught = $true; Log '  U-Boot reached again, this time from the new boot loader in the eMMC' }
            catch {
                Warn "Missed U-Boot after reboot ($($_.Exception.Message))"
                Prompt 'UNPLUG THE PoE, WAIT 5 SECONDS, THEN PLUG IT BACK IN'
            }
        }
        if (-not $caught) {
            Warn 'U-Boot did not come up from the eMMC boot loader; reaching it over the serial line instead (the unit may not boot on its own)'
            Reach-UBoot
        }
        Start-Sleep 5   # let the PC's address settle after the link bounce
        Push-ToUBoot $KernelFile
        Drop-Signature
        $o = UB 'iminfo 0x46000000' 30
        if ($o -notmatch 'Hash\(es\) for Image 0 \(kernel-1\): sha1\+') { throw 'UniFi kernel failed its hash check in U-Boot' }
        $o = UB 'sf probe' 10
        if ($o -notmatch '16 MiB') { throw 'SPI flash is not the expected 16 MB chip' }
        $written = $false
        for ($try = 1; $try -le 2 -and -not $written; $try++) {
            Log "  writing $size bytes to SPI flash at $NorOffset (about 90 s)"
            $o = UB "sf update 0x46000000 $NorOffset $hex" 400
            # 'sf update' skips sectors that already hold the same data (e.g. when re-running on a fixed unit)
            $m = [regex]::Match($o, '(\d+) bytes written, (\d+) bytes skipped')
            if (-not $m.Success -or ([long]$m.Groups[1].Value + [long]$m.Groups[2].Value) -ne $size) { Warn 'SPI flash write did not report completion'; continue }
            if ([long]$m.Groups[2].Value -eq $size) { Log '  SPI flash already held this exact kernel (nothing needed writing)' }
            [void](UB "mw.b 0x4A000000 0x00 $hex" 30)
            [void](UB "sf read 0x4A000000 $NorOffset $hex" 120)
            $o = UB "cmp.b 0x46000000 0x4A000000 $hex" 120
            if ($o -match "Total of $size byte\(s\) were the same") { $written = $true } else { Warn 'SPI flash read-back differs; writing again' }
        }
        if (-not $written) { throw 'SPI flash write could not be verified after 2 tries' }
        Log '  SPI flash read-back matches byte for byte'
        [void](UB 'setenv ipaddr 192.168.1.20')
        [void](UB 'setenv netmask 255.255.255.0')
        $bc = 'sf probe; sf read 0x46000000 ' + $NorOffset + ' ' + $hex + '; fdt addr ${fdtcontroladdr}; fdt ' + 'rm /signature; bootm 0x46000000'
        [void](UB "setenv bootcmd_real '$bc'")
        $o = UB 'printenv bootcmd_real'
        if ($o -notmatch [regex]::Escape("sf read 0x46000000 $NorOffset $hex")) { throw 'bootcmd_real was not set correctly' }
        $o = UB 'saveenv' 30
        if ($o -notmatch 'OK') { throw 'saveenv failed' }
        Log '  boot setting saved'

        # ------------------------------------------------- test boot
        Stage 7 'Unattended test boot'
        [void](Ser-Read)   # flush earlier output first, so only the new boot is checked
        $mark = $script:Console.Length
        $script:Port.Write("reset`r")
    } else {
        # ------------------------------------------------- reset mode
        Stage 4 'Back up the UniFi config'
        if ($SkipBackup) { Warn 'Backup skipped at your request' } else {
            $bdir = Join-Path $Root "backups\$mac-$Stamp-reset"
            New-Item -ItemType Directory $bdir -Force | Out-Null
            Ssh-Backup 'emmc-p9-cfg.bin' 'dd if=/dev/mmcblk0p9 bs=64k 2>/dev/null' $bdir
            Ssh-Backup 'nor-mtd1-u-boot-env.bin' 'cat /dev/mtd1' $bdir
            Log "  backup in $bdir"
        }
        Stage 5 'Wipe the UniFi config'
        $sh = @(
            'set -e'
            'C=/dev/mmcblk0p9'
            'grep -q "^PARTNAME=cfg$" /sys/block/mmcblk0/mmcblk0p9/uevent || { echo LAYOUT-MISMATCH; exit 1; }'
            'dd if=/dev/zero of=$C bs=512 count=$(cat /sys/block/mmcblk0/mmcblk0p9/size) 2>/dev/null; sync'
            'echo 3 > /proc/sys/vm/drop_caches'
            'n=$(hexdump -v -e "16/1 \"%02x\"" $C | grep -c "[1-9a-f]" || true)'
            '[ "$n" = 0 ] && echo CFG-ZERO-OK || { echo "CFG-NOT-ZERO $n"; exit 1; }'
            'echo WIPE-OK'
        ) -join "`n"
        $res = Ssh-RunFile $sh ''
        $res | ForEach-Object { Log "  $_" }
        if (($res | Out-String) -notmatch 'WIPE-OK') { throw 'Wiping the config partition failed (see above)' }
        Stage 6 'Test boot with factory defaults'
        [void](Ser-Read)   # flush earlier output (OpenWrt's own messages), so only the new boot is checked
        $mark = $script:Console.Length
        [void](Ssh-Run 'reboot')
    }

    # ------------------------------------------------- shared: check the boot
    # UniFi's console asks for Enter before it prints the login prompt.
    $o = Ser-Wait 'Please press Enter to activate this console|login:' 300
    if ($null -eq $o) {
        $boot = $script:Console.ToString($mark, $script:Console.Length - $mark)
        if ($boot -match 'Failed to load image|FIP boot source initialization failed') { throw "The unit's boot loader could not read the eMMC on its own. This unit's eMMC is too unreliable for this fix." }
        if ($boot -match 'ubnt boot') { throw 'U-Boot ran the stock boot instead of the SPI flash kernel. In Reset mode this means the unit was never fixed: run Fix first.' }
        throw 'UniFi did not finish booting'
    }
    Start-Sleep 5
    $script:Port.Write("`r")
    $o = Ser-Wait 'login:' 30
    $boot = $script:Console.ToString($mark, $script:Console.Length - $mark)
    if ($null -eq $o) { throw 'UniFi did not show its login prompt' }
    if ($boot -notmatch 'bytes @ 0x100000 Read: OK') { throw 'U-Boot did not load the kernel from SPI flash' }
    if ($boot -notmatch 'ubnthal: Ubiquiti U6\+') { throw 'UniFi booted but did not identify the board as U6+' }
    $crc = @([regex]::Matches($boot, '(?im)^.*(msdc|mmc0).*(error|crc|timeout).*$') | Where-Object { $_.Value -notmatch 'cmd=(52|8|5|55) ' }).Count
    if ($crc -gt 0) { throw "eMMC reported $crc errors during boot" }
    $hostname = [regex]::Match($boot, '(\S+) login:').Groups[1].Value
    if ($Mode -eq 'Reset' -or $FactoryReset) {
        if ($boot -match "running\.cfg") { Warn 'UniFi still loaded a saved config: the reset may not have taken effect' } else { Log '  UniFi started with factory defaults (no saved config loaded)' }
    }
    Log "  UniFi booted unattended (prompt '$hostname login:'), eMMC errors: 0"
} catch {
    Fail $_.Exception.Message
}

Emit "##STAGE $($script:StageNo) OK"
Ser-Close
Stop-Loader
Log ''
if ($Mode -eq 'Fix') {
    Log 'SUCCESS: unit fixed. Disconnect the serial adapter, connect the AP to the network and adopt it.' 'Green'
    Log 'Turn off automatic firmware updates for this unit in the UniFi controller.' 'Green'
} else {
    Log 'SUCCESS: unit reset to factory defaults. Disconnect the serial adapter; it will appear in the controller ready to adopt.' 'Green'
}
Log "Logs: $RunDir" 'Green'
Emit '##RESULT SUCCESS'
exit 0
