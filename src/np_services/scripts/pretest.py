import argparse
import contextlib
import functools
import os
import pathlib
import tempfile
import time
import logging
from typing import Iterable, Literal

import upath


import np_session
import np_services
import np_config
import npc_sync
import npc_ephys
import npc_mvr
import npc_stim

logger = logging.getLogger()

DEFAULT_SERVICES = (np_services.MouseDirector, )
DEFAULT_STIM = np_services.ScriptCamstim
DEFAULT_RECORDERS = (np_services.Sync, np_services.OpenEphys, np_services.VideoMVR, )

class DynamicRoutingPretest:
    """Modified version of class in np_workflows."""
    use_github: bool = True
    
    @property
    def rig(self) -> np_config.Rig:
        return np_config.Rig()

    @property
    def config(self) -> dict:
        return self.rig.config
    
    @property
    def commit_hash(self) -> str:
        if hasattr(self, '_commit_hash'):
            return self._commit_hash
        self._commit_hash = self.config['dynamicrouting_task_script']['commit_hash']
        return self.commit_hash
    
    @commit_hash.setter
    def commit_hash(self, value: str):
        self._commit_hash = value
        
    @property
    def github_url(self) -> str:
        if hasattr(self, '_github_url'):
            return self._github_url
        self._github_url = self.config['dynamicrouting_task_script']['url']
        return self.github_url
    
    @github_url.setter
    def github_url(self, value: str):
        self._github_url = value
    
    @property
    def base_url(self) -> upath.UPath:
        return upath.UPath(self.github_url) / self.commit_hash
    
    @property
    def base_path(self) -> pathlib.Path:
        return pathlib.Path('//allen/programs/mindscope/workgroups/dynamicrouting/DynamicRoutingTask/')

    @property
    def task_name(self) -> str:
        """For sending to runTask.py and controlling implementation details of the task."""
        if hasattr(self, '_task_name'): 
            return self._task_name 
        return ""

    @task_name.setter
    def task_name(self, task_name: str) -> None:
        self._task_name = task_name
        if task_name not in self.preset_task_names:
            print(f"{task_name = !r} doesn't correspond to a preset value, but the attribute is updated anyway!")
        else:
            print(f"Updated {self.__class__.__name__}.{task_name = !r}")

    @property
    def mouse(self) -> np_session.Mouse:
        return np_session.Mouse(366122)
    
    @property
    def hdf5_dir(self) -> pathlib.Path:
        return self.base_path / 'Data' /  str(self.mouse)
    
    @property
    def task_script_base(self) -> upath.UPath:
        return self.base_url if self.use_github else upath.UPath(self.base_path)
    
    @property
    def task_params(self) -> dict[str, str | bool]:
        """For sending to runTask.py"""
        return dict(
                rigName = str(self.rig).replace('.',''),
                subjectName = str(self.mouse),
                taskScript = 'DynamicRouting1.py',
                taskVersion = self.task_name,
                saveSoundArray = True,
        )
        
    @property
    def spontaneous_params(self) -> dict[str, str]:
        """For sending to runTask.py"""
        return dict(
                rigName = str(self.rig).replace('.',''),
                subjectName = str(self.mouse),
                taskScript = 'TaskControl.py',
                taskVersion = 'spontaneous',
        )
        
    @property
    def spontaneous_rewards_params(self) -> dict[str, str]:
        """For sending to runTask.py"""
        return dict(
                rigName = str(self.rig).replace('.',''),
                subjectName = str(self.mouse),
                taskScript = 'TaskControl.py',
                taskVersion = 'spontaneous rewards',
                rewardSound = "device",
        )
    
    def get_latest_optogui_txt(self, opto_or_optotagging: Literal['opto', 'optotagging']) -> pathlib.Path:
        dirname = dict(opto='optoParams', optotagging='optotagging')[opto_or_optotagging]
        file_prefix = dirname
        
        rig = str(self.rig).replace('.', '')
        locs_root = self.base_path / 'OptoGui' / f'{dirname}'
        available_locs = sorted(tuple(locs_root.glob(f"{file_prefix}_{self.mouse.id}_{rig}_*")), reverse=True)
        if not available_locs:
            raise FileNotFoundError(f"No optotagging locs found for {self.mouse}/{rig} - have you run OptoGui?")
        return available_locs[0]
        
        
    @property
    def optotagging_params(self) -> dict[str, str]:
        """For sending to runTask.py"""
        return dict(
                rigName = str(self.rig).replace('.',''),
                subjectName = str(self.mouse),
                taskScript = 'OptoTagging.py',
                optoTaggingLocs = self.get_latest_optogui_txt('optotagging').as_posix(),
        )

    @property
    def opto_params(self) -> dict[str, str | bool]:
        """Opto params are handled by runTask.py and don't need to be passed from
        here. Just check they exist on disk here.
        """
        _ = self.get_latest_optogui_txt('opto') # raises FileNotFoundError if not found
        return dict(
                rigName = str(self.rig).replace('.',''),
                subjectName = str(self.mouse),
                taskScript = 'DynamicRouting1.py',
                saveSoundArray = True,
            )

    @property
    def mapping_params(self) -> dict[str, str | bool]:
        """For sending to runTask.py"""
        return dict(
                rigName = str(self.rig).replace('.',''),
                subjectName = str(self.mouse),
                taskScript = 'RFMapping.py',
                saveSoundArray = True,
            )

    @property
    def sound_test_params(self) -> dict[str, str]:
        """For sending to runTask.py"""
        return dict(
                rigName = str(self.rig).replace('.',''),
                subjectName = 'sound',
                taskScript = 'TaskControl.py',
                taskVersion = 'sound test',
        )
        
    def get_github_file_content(self, address: str) -> str:
        import requests
        response = requests.get(address)
        if response.status_code not in (200, ):
            response.raise_for_status()
        return response.content.decode("utf-8")
    
    @property
    def camstim_script(self) -> upath.UPath:
        return self.task_script_base / 'runTask.py'
    
    def run_script(self, stim: Literal['sound_test', 'mapping', 'task', 'opto', 'optotagging', 'spontaneous', 'spontaneous_rewards']) -> None:
        
        params = getattr(self, f'{stim.replace(" ", "_")}_params')
        
        # add mouse and user info for MPE
        params['mouse_id'] = str(self.mouse.id)
        params['user_id'] = 'ben.hardcastle'
        
        script: str = params['taskScript']
        params['taskScript'] = (self.task_script_base / script).as_posix()
        
        params['maxTrials'] = 30
        
        if self.use_github:
        
            params['GHTaskScriptParams'] =  {
                'taskScript': params['taskScript'],
                'taskControl': (self.task_script_base / 'TaskControl.py').as_posix(),
                'taskUtils': (self.task_script_base / 'TaskUtils.py').as_posix(),
                }
            params['task_script_commit_hash'] = self.commit_hash

            np_services.ScriptCamstim.script = self.camstim_script.read_text()
        else:
            np_services.ScriptCamstim.script = self.camstim_script.as_posix()
        
        np_services.ScriptCamstim.params = params
        

        np_services.ScriptCamstim.start()
        with contextlib.suppress(np_services.resources.zro.ZroError):
            while not np_services.ScriptCamstim.is_ready_to_start():
                time.sleep(1)


        with contextlib.suppress(np_services.resources.zro.ZroError):
            np_services.ScriptCamstim.finalize()
     
def configure_services(services: Iterable[np_services.Testable]) -> None:
    """For each service, apply every key in self.config['service'] as an attribute."""

    def apply_config(service) -> None:
        if config := np_config.Rig().config["services"].get(service.__name__):
            for key, value in config.items():
                setattr(service, key, value)
                logger.debug(
                    f"{service.__name__} | Configuring {service.__name__}.{key} = {getattr(service, key)}"
                )

    for service in services:
        for base in service.__class__.__bases__:
            apply_config(base)
        apply_config(service)
        
    np_services.ScriptCamstim.script = '//allen/programs/mindscope/workgroups/dynamicrouting/DynamicRoutingTask/runTask.py'
    np_services.ScriptCamstim.data_root = pathlib.Path('//allen/programs/mindscope/workgroups/dynamicrouting/DynamicRoutingTask/Data/366122')

    np_services.MouseDirector.user = 'ben.hardcastle'
    np_services.MouseDirector.mouse = 366122

    np_services.OpenEphys.folder = '_test_'


@functools.cache
def get_temp_dir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())

def main(
    recorders: Iterable[np_services.Testable] = DEFAULT_RECORDERS,
    stim: np_services.Startable = DEFAULT_STIM,
    other: Iterable[np_services.Testable] = DEFAULT_SERVICES,
    check_licks: bool = False,
    check_opto: bool = False,
    check_audio: bool = False,
    ) -> None:
    print("Starting pretest")
    configure_services((*recorders, stim, *other))
    for service in (*recorders, stim, *other):
        if isinstance(service, np_services.Initializable):
            service.initialize()
            
    stoppables = tuple(_ for _ in recorders if isinstance(_, np_services.Stoppable))
    with np_services.stop_on_error(*stoppables):
        for service in stoppables:
            if isinstance(service, np_services.Startable):
                service.start()
        t0 = time.time()
        DynamicRoutingPretest().run_script('optotagging')
        t1 = time.time()
        time.sleep(max(0, 70 - (t1 - t0))) # long enough to capture 2 sets of barcodes on sync/openephys (cannot scale time with 1 set)
        for service in reversed(stoppables):
            if isinstance(service, np_services.Stoppable):
                service.stop()

    for service in (*recorders, stim, *other):
        if isinstance(service, np_services.Finalizable):
            service.finalize()

    np_services.VideoMVR.sync_path = np_services.OpenEphys.sync_path = stim.sync_path = np_services.Sync.data_files[0]
    
    for service in (*recorders, stim, *other):
        if isinstance(service, np_services.Validatable):
            service.validate()
    if any((check_licks, check_opto, check_audio)):
        npc_sync.SyncDataset(np_services.Sync.data_files[0]).validate(
            licks=check_licks, opto=check_opto, audio=check_audio,
        )

def parse_args() -> dict:
    parser = argparse.ArgumentParser(description="Run pretest")
    parser.add_argument("--check_licks", action="store_true", help="Check lick sensor line on sync", default=False)
    parser.add_argument("--check_opto", action="store_true", help="Check opto-running line on sync", default=False)
    parser.add_argument("--check_audio", action="store_true", help="Check audio-running line on sync", default=False)
    return vars(parser.parse_args())

if __name__ == '__main__':
    main(
        **parse_args()
    )