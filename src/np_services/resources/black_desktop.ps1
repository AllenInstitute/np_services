
#Modify Path to the picture accordingly to reflect your infrastructure
$imgPath="\\allen\programs\mindscope\workgroups\dynamicrouting\ben\black_wallpaper.bmp"
$code = @' 
using System.Runtime.InteropServices; 
namespace Win32{ 
    
     public class Wallpaper{ 
        [DllImport("user32.dll", CharSet=CharSet.Auto)] 
         static extern int SystemParametersInfo (int uAction , int uParam , string lpvParam , int fuWinIni) ; 
         
         public static void SetWallpaper(string thePath){ 
            SystemParametersInfo(20,0,thePath,3); 
         }
    }
 } 
'@


add-type $code 

Function Enable-AutoHideTaskBar {
    #This will configure the Windows taskbar to auto-hide
    [cmdletbinding(SupportsShouldProcess)]
    [Alias("Hide-TaskBar")]
    [OutputType("None")]
    Param()

    Begin {
        Write-Verbose "[$((Get-Date).TimeofDay) BEGIN  ] Starting $($myinvocation.mycommand)"
        $RegPath = 'HKCU:SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StuckRects3'
    } #begin
    Process {
        if (Test-Path $regpath) {
            Write-Verbose "[$((Get-Date).TimeofDay) PROCESS] Auto Hiding Windows 10 TaskBar"
            $RegValues = (Get-ItemProperty -Path $RegPath).Settings
            $RegValues[8] = 3

            Set-ItemProperty -Path $RegPath -Name Settings -Value $RegValues

            if ($PSCmdlet.ShouldProcess("Explorer", "Restart")) {
                #Kill the Explorer process to force the change
                Stop-Process -Name explorer -Force
            }
        }
        else {
            Write-Warning "Can't find registry location $regpath."
        }
    } #process
    End {
        Write-Verbose "[$((Get-Date).TimeofDay) END    ] Ending $($myinvocation.mycommand)"
    } #end

}
#Apply the Change on the system 
[Win32.Wallpaper]::SetWallpaper($imgPath)

(New-Object -ComObject shell.application).toggleDesktop()

#Hide icons on the Desktop
$Path="HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
Set-ItemProperty -Path $Path -Name "HideIcons" -Value 1
# Get-Process "explorer"| Stop-Process
Enable-AutoHideTaskBar
# Get-Process "explorer"| Stop-Process

