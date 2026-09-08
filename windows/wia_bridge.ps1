#Requires -Version 5.1

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("selftest", "list", "probe", "scan")]
    [string]$Action,

    [string]$DeviceId = "",
    [string]$OutputDirectory = "",

    [ValidateSet("Flatbed", "ADF", "ADF Duplex")]
    [string]$Source = "ADF",

    [ValidateSet("Color", "Gray", "Lineart")]
    [string]$Mode = "Color",

    [ValidateSet(75, 100, 150, 200, 300, 400, 600, 1200)]
    [int]$Dpi = 300,

    [ValidateSet("A4", "Letter")]
    [string]$Paper = "A4"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$WiaScannerDeviceType = 1
$WiaPropertyDeviceDescription = 4
$WiaPropertyPortName = 6
$WiaPropertyDeviceName = 7
$WiaPropertyItemCategory = 1029
$WiaPropertyDocumentHandlingCapabilities = 3086
$WiaPropertyDocumentHandlingSelect = 3088
$WiaPropertyPages = 3096
$WiaPropertyIntent = 6146
$WiaPropertyXResolution = 6147
$WiaPropertyYResolution = 6148
$WiaPropertyXPosition = 6149
$WiaPropertyYPosition = 6150
$WiaPropertyXExtent = 6151
$WiaPropertyYExtent = 6152

$WiaSelectFeeder = 1
$WiaSelectFlatbed = 2
$WiaSelectDuplex = 4
$WiaCapabilityFeeder = 1
$WiaCapabilityFlatbed = 2
$WiaCapabilityDuplex = 4

$WiaIntentColor = 1
$WiaIntentGrayscale = 2
$WiaIntentText = 4
$WiaFormatBmp = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"
$WiaCategoryFlatbed = "{FB607B1F-43F3-488B-855B-FB703EC342A6}"
$WiaCategoryFeeder = "{FE131934-F84C-42AD-8DA4-6129CDDD7288}"
$WiaErrorPaperEmpty = "0x80210003"
$WiaStatusEndOfMedia = "0x00210001"

function ConvertTo-CompactJson {
    param([object]$Value)
    ConvertTo-Json -InputObject $Value -Compress -Depth 8
}

function Get-HResultHex {
    param([System.Exception]$Exception)
    $signed = [System.Runtime.InteropServices.Marshal]::GetHRForException($Exception)
    $bytes = [System.BitConverter]::GetBytes([int32]$signed)
    $unsigned = [System.BitConverter]::ToUInt32($bytes, 0)
    return ("0x{0:X8}" -f $unsigned)
}

function Get-WiaProperty {
    param([object]$Properties, [int]$Id)
    if ($null -eq $Properties) {
        return $null
    }
    foreach ($property in @($Properties)) {
        if ([int]$property.PropertyID -eq $Id) {
            return $property
        }
    }
    return $null
}

function Get-WiaPropertyValue {
    param([object]$Properties, [int]$Id, [object]$Default = $null)
    $property = Get-WiaProperty -Properties $Properties -Id $Id
    if ($null -eq $property) {
        return $Default
    }
    try {
        return $property.Value
    }
    catch {
        return $Default
    }
}

function Get-NearestPropertyValue {
    param([object]$Property, [int]$Requested)
    try {
        $values = @($Property.SubTypeValues) | ForEach-Object { [int]$_ }
        if ($values.Count -gt 0) {
            $lower = @($values | Where-Object { $_ -le $Requested } | Sort-Object -Descending)
            if ($lower.Count -gt 0) {
                return [int]$lower[0]
            }
            return [int](@($values | Sort-Object)[0])
        }
    }
    catch {
        # Some drivers throw while reading subtype metadata. Use the requested
        # value and let the property setter decide whether it is accepted.
    }
    try {
        $minimum = [int]$Property.SubTypeMin
        $maximum = [int]$Property.SubTypeMax
        $step = [Math]::Max(1, [int]$Property.SubTypeStep)
        $bounded = [Math]::Min($maximum, [Math]::Max($minimum, $Requested))
        return $minimum + ([Math]::Floor(($bounded - $minimum) / $step) * $step)
    }
    catch {
        return $Requested
    }
}

function Set-WiaProperty {
    param(
        [object]$Properties,
        [int]$Id,
        [int]$Value,
        [switch]$ChooseSupported
    )
    $property = Get-WiaProperty -Properties $Properties -Id $Id
    if ($null -eq $property) {
        return $null
    }
    $effective = if ($ChooseSupported) {
        Get-NearestPropertyValue -Property $property -Requested $Value
    }
    else {
        $Value
    }
    try {
        $property.Value = [int]$effective
        return [int]$effective
    }
    catch {
        return $null
    }
}

function Get-DeviceInfo {
    param([object]$Manager, [string]$WantedId)
    foreach ($info in @($Manager.DeviceInfos)) {
        if ([int]$info.Type -ne $WiaScannerDeviceType) {
            continue
        }
        if ([string]$info.DeviceID -eq $WantedId) {
            return $info
        }
    }
    return $null
}

function Get-TransferItem {
    param([object]$Device, [string]$RequestedSource)
    $wantedCategory = if ($RequestedSource -eq "Flatbed") {
        $WiaCategoryFlatbed
    }
    else {
        $WiaCategoryFeeder
    }
    $fallback = $null
    foreach ($item in @($Device.Items)) {
        if ($null -eq $fallback) {
            $fallback = $item
        }
        $category = [string](Get-WiaPropertyValue -Properties $item.Properties -Id $WiaPropertyItemCategory -Default "")
        if ($category.Equals($wantedCategory, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $item
        }
    }
    return $fallback
}

function Get-ResolutionValues {
    param([object]$Item)
    $property = Get-WiaProperty -Properties $Item.Properties -Id $WiaPropertyXResolution
    if ($null -eq $property) {
        return @()
    }
    try {
        $values = @($property.SubTypeValues) | ForEach-Object { [int]$_ }
        return @($values | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    }
    catch {
        return @()
    }
}

function Get-DeviceCapabilities {
    param([object]$Info)
    try {
        $device = $Info.Connect()
        $value = Get-WiaPropertyValue -Properties $device.Properties -Id $WiaPropertyDocumentHandlingCapabilities -Default 0
        $item = Get-TransferItem -Device $device -RequestedSource "ADF"
        $resolutions = if ($null -ne $item) { @(Get-ResolutionValues -Item $item) } else { @() }
        return @{
            known = ([int]$value -ne 0)
            value = [int]$value
            resolutions = $resolutions
        }
    }
    catch {
        return @{ known = $false; value = 0; resolutions = @() }
    }
}

function Set-ScanProperties {
    param(
        [object]$Device,
        [object]$Item,
        [string]$RequestedSource,
        [string]$RequestedMode,
        [int]$RequestedDpi,
        [string]$RequestedPaper
    )
    $selection = if ($RequestedSource -eq "Flatbed") { $WiaSelectFlatbed } else { $WiaSelectFeeder }
    if ($RequestedSource -eq "ADF Duplex") {
        $selection = $selection -bor $WiaSelectDuplex
    }
    $deviceSelection = Set-WiaProperty -Properties $Device.Properties -Id $WiaPropertyDocumentHandlingSelect -Value $selection
    $itemSelection = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyDocumentHandlingSelect -Value $selection

    $category = [string](Get-WiaPropertyValue -Properties $Item.Properties -Id $WiaPropertyItemCategory -Default "")
    if ($RequestedSource -ne "Flatbed" -and $null -eq $deviceSelection -and $null -eq $itemSelection -and -not $category.Equals($WiaCategoryFeeder, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Драйвер WIA не позволяет выбрать автоподатчик."
    }

    $intent = switch ($RequestedMode) {
        "Gray" { $WiaIntentGrayscale }
        "Lineart" { $WiaIntentText }
        default { $WiaIntentColor }
    }
    $null = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyIntent -Value $intent
    $effectiveX = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyXResolution -Value $RequestedDpi -ChooseSupported
    $effectiveY = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyYResolution -Value $RequestedDpi -ChooseSupported
    $effectiveDpi = if ($null -ne $effectiveX) { [int]$effectiveX } elseif ($null -ne $effectiveY) { [int]$effectiveY } else { $RequestedDpi }

    $null = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyXPosition -Value 0
    $null = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyYPosition -Value 0
    $widthInches = if ($RequestedPaper -eq "Letter") { 8.5 } else { 210.0 / 25.4 }
    $heightInches = if ($RequestedPaper -eq "Letter") { 11.0 } else { 297.0 / 25.4 }
    $widthPixels = [int][Math]::Floor($widthInches * $effectiveDpi)
    $heightPixels = [int][Math]::Floor($heightInches * $effectiveDpi)
    $null = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyXExtent -Value $widthPixels -ChooseSupported
    $null = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyYExtent -Value $heightPixels -ChooseSupported
    if ($RequestedSource -ne "Flatbed") {
        $null = Set-WiaProperty -Properties $Device.Properties -Id $WiaPropertyPages -Value 1
        $null = Set-WiaProperty -Properties $Item.Properties -Id $WiaPropertyPages -Value 1
    }
    return $effectiveDpi
}

try {
    if ($Action -eq "selftest") {
        ConvertTo-CompactJson -Value @{ ok = $true; bridge = "wia" }
        exit 0
    }

    $manager = New-Object -ComObject "WIA.DeviceManager"

    if ($Action -eq "list") {
        $devices = @()
        foreach ($info in @($manager.DeviceInfos)) {
            if ([int]$info.Type -ne $WiaScannerDeviceType) {
                continue
            }
            # DeviceInfo is intentionally enumerated without Connect(). Some
            # sleeping USB MFPs block that COM call for tens of seconds. The
            # actual scan connects only the exact device selected by the user.
            $devices += [pscustomobject]@{
                device_id = [string]$info.DeviceID
                name = [string](Get-WiaPropertyValue -Properties $info.Properties -Id $WiaPropertyDeviceName -Default "Сканер WIA")
                description = [string](Get-WiaPropertyValue -Properties $info.Properties -Id $WiaPropertyDeviceDescription -Default "")
                port = [string](Get-WiaPropertyValue -Properties $info.Properties -Id $WiaPropertyPortName -Default "")
                capabilities_known = $false
                has_adf = $false
                has_flatbed = $false
                has_duplex = $false
                resolutions = @()
            }
        }
        ConvertTo-CompactJson -Value $devices
        exit 0
    }

    if ([string]::IsNullOrWhiteSpace($DeviceId)) {
        throw "Не указан идентификатор выбранного WIA-сканера."
    }
    $deviceInfo = Get-DeviceInfo -Manager $manager -WantedId $DeviceId
    if ($null -eq $deviceInfo) {
        throw "Выбранный WIA-сканер больше не зарегистрирован в Windows."
    }

    if ($Action -eq "probe") {
        ConvertTo-CompactJson -Value @{ present = $true; device_id = $DeviceId }
        exit 0
    }

    if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
        throw "Не указан каталог для полученных страниц."
    }
    $null = New-Item -ItemType Directory -Path $OutputDirectory -Force
    Get-ChildItem -LiteralPath $OutputDirectory -Filter "raw-*.bmp" -File -ErrorAction SilentlyContinue | Remove-Item -Force

    $device = $deviceInfo.Connect()
    $item = Get-TransferItem -Device $device -RequestedSource $Source
    if ($null -eq $item) {
        throw "Драйвер WIA не предоставил элемент для получения изображения."
    }
    $capabilityValue = [int](Get-WiaPropertyValue -Properties $device.Properties -Id $WiaPropertyDocumentHandlingCapabilities -Default 0)
    if ($Source -ne "Flatbed" -and $capabilityValue -ne 0 -and ($capabilityValue -band $WiaCapabilityFeeder) -eq 0) {
        throw "Выбранный сканер не сообщает о наличии автоподатчика."
    }
    if ($Source -eq "ADF Duplex" -and $capabilityValue -ne 0 -and ($capabilityValue -band $WiaCapabilityDuplex) -eq 0) {
        throw "Выбранный сканер не поддерживает аппаратный дуплекс WIA."
    }

    $effectiveDpi = Set-ScanProperties -Device $device -Item $item -RequestedSource $Source -RequestedMode $Mode -RequestedDpi $Dpi -RequestedPaper $Paper
    $files = @()
    $maximumImages = if ($Source -eq "Flatbed") { 1 } else { 500 }
    for ($index = 1; $index -le $maximumImages; $index++) {
        try {
            $image = $item.Transfer($WiaFormatBmp)
            if ($null -eq $image) {
                if ($files.Count -gt 0) {
                    break
                }
                throw "WIA не вернул изображение."
            }
            $path = Join-Path $OutputDirectory ("raw-{0:D4}.bmp" -f $index)
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force
            }
            $image.SaveFile($path)
            $files += $path
        }
        catch {
            $errorCode = Get-HResultHex -Exception $_.Exception
            if ($files.Count -gt 0 -and $Source -ne "Flatbed" -and $errorCode -in @($WiaErrorPaperEmpty, $WiaStatusEndOfMedia)) {
                break
            }
            throw
        }
        if ($Source -eq "Flatbed") {
            break
        }
    }

    if ($files.Count -eq 0) {
        throw "WIA не получил ни одной страницы."
    }
    ConvertTo-CompactJson -Value @{
        files = $files
        pages = $files.Count
        effective_dpi = $effectiveDpi
        source = $Source
    }
}
catch {
    $payload = @{
        error = [string]$_.Exception.Message
        hresult = Get-HResultHex -Exception $_.Exception
    }
    [Console]::Error.WriteLine((ConvertTo-CompactJson -Value $payload))
    exit 1
}
