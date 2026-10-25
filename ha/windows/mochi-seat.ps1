# mochi-seat: tells Mochi that you are at the PC while it runs Windows.
#
# Mochi's brain lives on Linux; when this PC is in Windows (games), sff or the cloud thinks for it and, seeing no
# desk, takes you for away. Once a minute this posts one heartbeat to Mochi's relay: how long since you last touched
# the keyboard or mouse, the name of the app in front (its product name, never a window title) and whether it is
# fullscreen. Mochi then knows you're here and playing: asks still come to your phone, but nothing buzzes.
#
# The token below may only post this one heartbeat (PUT /v1/beacon/windows) and read nothing.
# Installed by ha/windows/install.sh into %LOCALAPPDATA%\Mochi, started at login by mochi-seat.vbs in Startup.
# Tray icon: right-click for Pause (an hour) and Quit.

$ErrorActionPreference = 'SilentlyContinue'
$Relay = '__RELAY__'
$Token = '__TOKEN__'
$Every = 60

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class MochiSeat {
    [StructLayout(LayoutKind.Sequential)] struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LASTINPUTINFO p);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    public static uint IdleSeconds() {
        LASTINPUTINFO i = new LASTINPUTINFO();
        i.cbSize = (uint)Marshal.SizeOf(i);
        GetLastInputInfo(ref i);
        return ((uint)Environment.TickCount - i.dwTime) / 1000;
    }
}
"@

$script:pausedUntil = [DateTime]::MinValue

function Get-Front {
    $h = [MochiSeat]::GetForegroundWindow()
    $procId = 0
    [void][MochiSeat]::GetWindowThreadProcessId($h, [ref]$procId)
    $p = Get-Process -Id $procId
    $name = if ($p.Product) { $p.Product } elseif ($p.Description) { $p.Description } else { $p.ProcessName }
    $r = New-Object MochiSeat+RECT
    [void][MochiSeat]::GetWindowRect($h, [ref]$r)
    $s = [System.Windows.Forms.Screen]::FromHandle($h).Bounds
    $full = ($r.Left -le $s.Left) -and ($r.Top -le $s.Top) -and ($r.Right -ge $s.Right) -and ($r.Bottom -ge $s.Bottom) -and ($p.ProcessName -ne 'explorer')
    return @{ app = [string]$name; full = [bool]$full }
}

function Send-Beat {
    $paused = (Get-Date) -lt $script:pausedUntil
    $f = Get-Front
    $beat = @{ node = 'windows'; seat_only = $true; healthy = $false; prio = 99; v = 1
               t = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); paused = $paused
               idle = [int][MochiSeat]::IdleSeconds(); app = $(if ($paused) { '' } else { $f.app })
               fullscreen = $(if ($paused) { $false } else { $f.full }) }
    try {
        Invoke-RestMethod -Method Put -Uri "$Relay/v1/beacon/windows" -TimeoutSec 15 -ContentType 'application/json' `
            -Headers @{ Authorization = "Bearer $Token" } -Body ($beat | ConvertTo-Json -Compress) | Out-Null
        $script:tray.Text = $(if ($paused) { 'Mochi: paused' } else { 'Mochi knows you are here' })
    } catch {
        $script:tray.Text = 'Mochi: relay unreachable'
    }
}

# the tray icon: a small pink dot
$bmp = New-Object System.Drawing.Bitmap 16, 16
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.SmoothingMode = 'AntiAlias'
$g.FillEllipse((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 240, 150, 170))), 1, 1, 14, 14)
$g.FillEllipse((New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255, 200, 100, 125))), 5, 7, 6, 4)
$g.Dispose()
$script:tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
$tray.Text = 'Mochi'
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$pause = $menu.Items.Add('Pause for an hour')
$pause.add_Click({ $script:pausedUntil = (Get-Date).AddHours(1); Send-Beat })
$resume = $menu.Items.Add('Resume')
$resume.add_Click({ $script:pausedUntil = [DateTime]::MinValue; Send-Beat })
$quit = $menu.Items.Add('Quit')
$quit.add_Click({ $script:tray.Visible = $false; [System.Windows.Forms.Application]::Exit() })
$tray.ContextMenuStrip = $menu
$tray.Visible = $true

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $Every * 1000
$timer.add_Tick({ Send-Beat })
$timer.Start()
Send-Beat
[System.Windows.Forms.Application]::Run()
