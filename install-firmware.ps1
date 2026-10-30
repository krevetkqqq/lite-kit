# install-firmware.ps1 - скачать прошивку с зеркала в firmware\<версия>.zip
#
#   .\install-firmware.ps1                 # меню: платформа -> версия
#   .\install-firmware.ps1 -List           # только список платформ
#   .\install-firmware.ps1 -Platform yandexmicro -Version 0.369.1.46.4778262442.20261013.297
#
# На зеркале файл называется update.zip - сохраняем под именем версии.
param(
    [string]$Platform,
    [string]$Version,
    [switch]$List,
    [string]$Mirror = "http://144.76.194.29:62891"
)
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Test-Zip([string]$Path) {
    try {
        $fs = [IO.File]::OpenRead($Path)
        $b = New-Object byte[] 2
        [void]$fs.Read($b, 0, 2)
        $fs.Close()
        return ($b[0] -eq 0x50 -and $b[1] -eq 0x4B)
    } catch { return $false }
}

$Base = Split-Path -Parent $PSCommandPath
$FwRoot = Join-Path $Base "firmware"

function Get-Links([string]$Url) {
    $html = (Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 30).Content
    [regex]::Matches($html, 'href="([^"]+)/"') | ForEach-Object { $_.Groups[1].Value } | Sort-Object
}

function Get-VersionKey([string]$v) {
    # каждое число -> 10 цифр, чтобы строковая сортировка была числовой
    ($v -split '\.' | ForEach-Object {
        $n = 0
        if ([int]::TryParse($_, [ref]$n)) { $n.ToString("D10") } else { "0000000000" }
    }) -join ""
}

$plats = @(Get-Links "$Mirror/")
if ($List) {
    Write-Host "Зеркало: $Mirror"
    Write-Host "Платформы:"
    $plats | ForEach-Object { Write-Host "  - $_" }
    exit 0
}

# -Platform 3 — номер из меню
if ($Platform -match '^[0-9]+$') {
    $pi = [int]$Platform
    if ($pi -lt 1 -or $pi -gt $plats.Count) { Write-Host "[!] нет платформы с таким номером"; exit 1 }
    $Platform = $plats[$pi - 1]
}

if (-not $Platform) {
    if ($plats.Count -eq 0) { Write-Host "[!] не получил список платформ с $Mirror"; exit 1 }
    Write-Host "Зеркало: $Mirror"
    Write-Host "Выбери платформу:"
    for ($i = 0; $i -lt $plats.Count; $i++) { Write-Host ("  {0,2}) {1}" -f ($i + 1), $plats[$i]) }
    $ans = Read-Host "Номер (Enter = yandexmicro)"
    if ([string]::IsNullOrWhiteSpace($ans)) { $Platform = "yandexmicro" }
    else {
        $idx = 0
        if (-not [int]::TryParse($ans, [ref]$idx) -or $idx -lt 1 -or $idx -gt $plats.Count) {
            Write-Host "[!] нет такой платформы"; exit 1
        }
        $Platform = $plats[$idx - 1]
    }
}
Write-Host "[*] платформа: $Platform"
$FwDir = Join-Path $FwRoot $Platform
New-Item -ItemType Directory -Force -Path $FwDir | Out-Null

$versions = @(Get-Links "$Mirror/$Platform/")
if ($versions.Count -eq 0) { Write-Host "[!] нет версий в $Mirror/$Platform/"; exit 1 }
$versions = @($versions | Sort-Object -Property @{Expression={ (Get-VersionKey $_) }} -Descending)

$known = @{
    "yandexmicro" = "0.369.1.46.4778262442.20261013.297"
    "yandexmini"  = "1.305.4.16.3237824040.20250721"
    "yandexmini_2" = "0.369.1.46.4778262943.20261013.299"
}
$knownVersion = $known[$Platform]

# -Version 5 — номер из списка
if ($Version -match '^[0-9]+$') {
    $vi = [int]$Version
    if ($vi -lt 1 -or $vi -gt $versions.Count) { Write-Host "[!] нет версии с таким номером"; exit 1 }
    $Version = $versions[$vi - 1]
}

if (-not $Version) {
    Write-Host "Выбери версию (сверху новые):"
    for ($i = 0; $i -lt $versions.Count; $i++) {
        $mark = ""
        if ($knownVersion -and $versions[$i] -eq $knownVersion) { $mark = "   <-- проверено (ADB работает)" }
        Write-Host ("  {0,2}) {1}{2}" -f ($i + 1), $versions[$i], $mark)
    }
    $ans = Read-Host "Номер (Enter = самая новая)"
    if ([string]::IsNullOrWhiteSpace($ans)) { $Version = $versions[0] }
    else {
        $idx = 0
        if (-not [int]::TryParse($ans, [ref]$idx) -or $idx -lt 1 -or $idx -gt $versions.Count) {
            Write-Host "[!] нет такой версии"; exit 1
        }
        $Version = $versions[$idx - 1]
    }
}

$url = "$Mirror/$Platform/$Version"
Write-Host "[*] версия:    $Version"
Write-Host "[*] качаю:     $url/update.zip"

$tmp = Join-Path $env:TEMP ("fw-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$metaPath = Join-Path $tmp "metadata.json"
$zipPath = Join-Path $tmp "update.zip"

$meta = $null
try {
    Invoke-WebRequest -Uri "$url/metadata.json" -UseBasicParsing -OutFile $metaPath -TimeoutSec 30
    $meta = Get-Content $metaPath -Raw | ConvertFrom-Json
} catch {
    Write-Host "[!] metadata.json не найден, продолжу без него"
}

try {
    Invoke-WebRequest -Uri "$url/update.zip" -UseBasicParsing -OutFile $zipPath -TimeoutSec 900
} catch {
    Write-Host "[!] зеркало не отдало update.zip"
}

$size = 0
if (Test-Path $zipPath) { $size = (Get-Item $zipPath).Length }
if (-not $meta) {
    # metadata.json не скачался — пробуем ещё раз, от неё зависит запасной URL
    try {
        Invoke-WebRequest -Uri "$url/metadata.json" -UseBasicParsing -OutFile $metaPath -TimeoutSec 30
        $meta = Get-Content $metaPath -Raw | ConvertFrom-Json
    } catch { Write-Host "[!] metadata.json недоступен — запасной URL неизвестен" }
}
if ($size -lt 1000000 -or -not (Test-Zip $zipPath)) {
    # зеркало бывает обрезанным (~33 КБ вместо 60 МБ) — берём официальный URL из metadata.json
    $alts = @()
    if ($meta) {
        if ($meta.downloadUrl) { $alts += $meta.downloadUrl }
        if ($meta.fallbackUrl) { $alts += $meta.fallbackUrl }
    }
    foreach ($alt in $alts) {
        Write-Host "[*] зеркало отдало $size б — качаю с официального URL: $alt"
        try {
            Invoke-WebRequest -Uri $alt -UseBasicParsing -OutFile $zipPath -TimeoutSec 900
            $size = (Get-Item $zipPath).Length
            if ($size -gt 1000000 -and (Test-Zip $zipPath)) { break }
        } catch { }
    }
}
if ($size -lt 1000000 -or -not (Test-Zip $zipPath)) {
    Write-Host "[!] не удалось получить прошивку (размер $size б, zip: $(Test-Zip $zipPath))"
    if ($meta -and $meta.crc32) { Write-Host "    ожидаемый crc32 из metadata: $($meta.crc32)" }
    exit 1
}

$sha = (Get-FileHash -Algorithm SHA256 -Path $zipPath).Hash.ToLower()
# crc32 без Python (на Windows его может не быть) — считаем через C#
if (-not ("Crc32" -as [type])) {
    Add-Type -TypeDefinition @"
public static class Crc32 {
    static readonly uint[] T = new uint[256];
    static Crc32() {
        for (uint i = 0; i < 256; i++) {
            uint c = i;
            for (int k = 0; k < 8; k++) c = ((c & 1) != 0) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
            T[i] = c;
        }
    }
    public static uint Compute(string path) {
        uint crc = 0xFFFFFFFFu;
        using (var fs = System.IO.File.OpenRead(path)) {
            var buf = new byte[1 << 20];
            int n;
            while ((n = fs.Read(buf, 0, buf.Length)) > 0)
                for (int i = 0; i < n; i++) crc = T[(crc ^ buf[i]) & 0xFF] ^ (crc >> 8);
        }
        return crc ^ 0xFFFFFFFFu;
    }
}
"@
}
$crc = [Crc32]::Compute($zipPath)
Write-Host "[*] размер:    $size б, crc32 $crc"

if ($meta) {
    if ($meta.version -and $meta.version -ne $Version) {
        Write-Host "[!] внимание: в metadata.json версия $($meta.version), а папка $Version"
    }
    if ($meta.crc32 -and $crc -ne "0" -and "$($meta.crc32)" -ne "$crc") {
        Write-Host "[!] crc32 не совпал с metadata.json ($($meta.crc32)) - файл битый"; exit 1
    }
}

$dest = Join-Path $FwDir "$Version.zip"
Move-Item -Force -Path $zipPath -Destination $dest

$manifest = [ordered]@{
    platform = $Platform
    version  = $Version
    filename = "$Version.zip"
    size     = $size
    crc32    = [int64]$crc
    sha256   = $sha
    note     = "скачано install-firmware.ps1 с $Mirror"
}
# ВАЖНО: без BOM, иначе Python-сервер не прочитает JSON (Unexpected UTF-8 BOM)
[IO.File]::WriteAllText((Join-Path $FwDir "manifest.json"),
    ($manifest | ConvertTo-Json),
    (New-Object Text.UTF8Encoding($false)))

Get-ChildItem -Path $FwDir -Filter *.zip | Where-Object { $_.Name -ne "$Version.zip" } | ForEach-Object {
    Write-Host "[*] удалён старый: $($_.Name)"
    Remove-Item $_.FullName -Force
}
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[OK] прошивка: $dest"
Write-Host "     manifest: $($FwDir)\manifest.json"
Write-Host 'Дальше: New-Item run\downgrade -ItemType File -Force; .\stop.ps1; .\start.ps1'
