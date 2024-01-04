import pathlib
from typing import ClassVar

import fabric
import np_config

from np_services.proxies import ScriptCamstim

class DesktopThemeChanger(ScriptCamstim):
    """Base class for setting the background wallpaper on a remote machine,
    then hiding all desktop icons, hiding the taskbar, minimizing all windows"""
    local_file: ClassVar[str | pathlib.Path] 
    remote_file: ClassVar[str | pathlib.Path] 
    
    extra_args: ClassVar[list[str]] = []
    ssh: ClassVar[fabric.Connection]
    user: ClassVar[str] = 'svc_neuropix'
    password: ClassVar[str] = np_config.fetch('logins')['svc_neuropix']['password']
    
    # @classmethod
    # def initialize(cls):
    #     super().initialize()
    #     cls.get_ssh().put(cls.local_file, cls.remote_file)
    
    @classmethod
    def start(cls):
        cls.get_ssh().run(f'powershell.exe -ExecutionPolicy bypass {cls.remote_file}')
        
class DarkDesktopChanger(DesktopThemeChanger):
    local_file = pathlib.Path(__file__).parent / 'resources' / 'black_wallpaper.ps1'
    # remote_file: ClassVar[str | pathlib.Path] = 'c:/users/svc_neuropix/desktop/black_wallpaper.ps1'
    remote_file: ClassVar[str | pathlib.Path] = R'\\allen\programs\mindscope\workgroups\dynamicrouting\ben\black_wallpaper.ps1'
    
class GreyDesktopChanger(DesktopThemeChanger):
    local_file = pathlib.Path(__file__).parent / 'resources' / 'grey_wallpaper.ps1'
    # remote_file: ClassVar[str | pathlib.Path] = 'c:/users/svc_neuropix/desktop/grey_wallpaper.ps1'
    remote_file: ClassVar[str | pathlib.Path] = R'\\allen\programs\mindscope\workgroups\dynamicrouting\ben\grey_wallpaper.ps1'
    
class DesktopResetter(DesktopThemeChanger):
    local_file = pathlib.Path(__file__).parent / 'resources' / 'reset_wallpaper.ps1'
    # remote_file: ClassVar[str | pathlib.Path] = 'c:/users/svc_neuropix/desktop/reset_wallpaper.ps1'
    remote_file: ClassVar[str | pathlib.Path] = R'\\allen\programs\mindscope\workgroups\dynamicrouting\ben\reset_wallpaper.ps1'