param([string]$Out, [string]$Cab)
# A small Windows Installer database, made by Windows Installer itself: summary information, one
# table and a CAB stream (where an installer keeps its files), for ConvertAll's MSI tests.
$ErrorActionPreference = "Stop"
$installer = New-Object -ComObject WindowsInstaller.Installer

function Call($obj, [string]$name, [object[]]$params) {
    return $obj.GetType().InvokeMember($name, [Reflection.BindingFlags]::InvokeMethod, $null, $obj, $params)
}
function Set-Prop($obj, [string]$name, [object[]]$params) {
    $obj.GetType().InvokeMember($name, [Reflection.BindingFlags]::SetProperty, $null, $obj, $params) | Out-Null
}

$db = Call $installer "OpenDatabase" @($Out, 3)  # create
$info = $db.GetType().InvokeMember("SummaryInformation", [Reflection.BindingFlags]::GetProperty, $null, $db, @(4))
Set-Prop $info "Property" @(2, "ConvertAll sample")  # the title
Call $info "Persist" @() | Out-Null

$sql = 'CREATE TABLE `File` (`File` CHAR(72) NOT NULL, `FileName` CHAR(255) NOT NULL PRIMARY KEY `File`)'
Call (Call $db "OpenView" @($sql)) "Execute" @() | Out-Null
$view = Call $db "OpenView" @('INSERT INTO `File` (`File`, `FileName`) VALUES (''notes'', ''notes.txt'')')
Call $view "Execute" @() | Out-Null

$view = Call $db "OpenView" @('SELECT `Name`, `Data` FROM `_Streams`')
Call $view "Execute" @() | Out-Null
$record = Call $installer "CreateRecord" @(2)
Set-Prop $record "StringData" @(1, "sample.cab")
Call $record "SetStream" @(2, $Cab) | Out-Null
Call $view "Modify" @(1, $record) | Out-Null  # insert
Call $view "Close" @() | Out-Null
Call $db "Commit" @() | Out-Null
