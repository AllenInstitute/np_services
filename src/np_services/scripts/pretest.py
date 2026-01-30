from __future__ import annotations

import abc
import argparse
import copy
import contextlib
import dataclasses
import functools
import json
import os
import pathlib
import sys
import tempfile
import time
import logging
from typing import Iterable, Literal, Type

import upath


import np_session
import np_services
import np_config
import npc_sync
import npc_ephys
import npc_mvr
import npc_stim

logger = logging.getLogger()

DEFAULT_SERVICES: tuple[np_services.Testable, ...] = (np_services.MouseDirector, )
DEFAULT_RECORDERS: tuple[np_services.Startable, ...] = (np_services.Sync, np_services.VideoMVR, )

@dataclasses.dataclass
class PretestConfig:
    check_sync_barcodes: bool = False
    check_ephys_barcodes: bool = False # this is error prone with short recordings, so is separated from checking sync alone
    check_licks: bool = False
    check_opto: bool = False
    check_audio: bool = False
    check_running: bool = False
    
    @property
    def check_barcodes(self) -> bool:
        return self.check_sync_barcodes or self.check_ephys_barcodes


class PretestSession(abc.ABC):

    def __init__(self, pretest_config: PretestConfig) -> None:
        self.pretest_config = pretest_config
    
    @property
    def services(self) -> tuple[np_services.Testable | np_services.Startable, ...]: 
        return DEFAULT_SERVICES + self.recorders + (self.stim, )
    
    @property
    def recorders(self) -> tuple[np_services.Startable, ...]:
        if self.pretest_config.check_ephys_barcodes or self.pretest_config.check_sync_barcodes:
            return DEFAULT_RECORDERS + (np_services.OpenEphys, )
        return DEFAULT_RECORDERS
    
    @property
    @abc.abstractmethod     
    def stim(self) -> Type[np_services.Camstim]: ...

    @abc.abstractmethod     
    def configure_services(self) -> None: ...
    
    @abc.abstractmethod
    def run_pretest_stim(self) -> None: ...
    
class DynamicRoutingPretest(PretestSession):
    """Modified version of class in np_workflows."""
    
    use_github: bool = True
    task_name: str = "" # unused in pretest
    
    @property
    def stim(self) -> Type[np_services.ScriptCamstim]:
        return np_services.ScriptCamstim
        
    @property
    def rig(self) -> np_config.Rig:
        return np_config.Rig()
    
    @property
    def commit_hash(self) -> str:
        if hasattr(self, '_commit_hash'):
            return self._commit_hash
        self._commit_hash = self.rig.config['dynamicrouting_task_script']['commit_hash']
        return self.commit_hash
    
    @commit_hash.setter
    def commit_hash(self, value: str):
        self._commit_hash = value
        
    @property
    def github_url(self) -> str:
        if hasattr(self, '_github_url'):
            return self._github_url
        self._github_url = self.rig.config['dynamicrouting_task_script']['url']
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
        # use any available locs file - as long as the light switches on the
        # values don't matter
        available_locs = sorted(tuple(locs_root.glob(f"{file_prefix}*")), reverse=True)
        if not available_locs:
            raise FileNotFoundError(f"No optotagging locs found - have you run OptoGui?")
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
        
        params = copy.deepcopy(getattr(self, f'{stim.replace(" ", "_")}_params'))
        
        # add mouse and user info for MPE
        params['mouse_id'] = str(self.mouse.id)
        params['user_id'] = 'ben.hardcastle'
        
        if self.task_script_base.as_posix() not in params['taskScript']:
            params['taskScript'] = (self.task_script_base / params['taskScript']).as_posix()
        
        params['maxTrials'] = 30    
        
        if self.use_github:
        
            params['GHTaskScriptParams'] =  {
                'taskScript': params['taskScript'],
                'taskControl': (self.task_script_base / 'TaskControl.py').as_posix(),
                'taskUtils': (self.task_script_base / 'TaskUtils.py').as_posix(),
                }
            params['task_script_commit_hash'] = self.commit_hash

            self.stim.script = self.camstim_script.read_text()
        else:
            self.stim.script = self.camstim_script.as_posix()
        
        self.stim.params = params
        

        self.stim.start()
        with contextlib.suppress(np_services.resources.zro.ZroError):
            while not self.stim.is_ready_to_start():
                time.sleep(1)


        with contextlib.suppress(np_services.resources.zro.ZroError):
            self.stim.finalize()
            
    def run_pretest_stim(self) -> None:
        if self.pretest_config.check_audio:
            print("Starting audio test - check for sound...", end="", flush=True)
            self.run_script('sound_test')
            print(" done")
        print("Starting stim - check for opto, spin wheel and tap lick spout...", end="", flush=True)
        self.run_script('optotagging') # vis stim with opto (for checking vsyncs, running,  )
        print(" done")
        
    def configure_services(self) -> None:
        self.stim.script = '//allen/programs/mindscope/workgroups/dynamicrouting/DynamicRoutingTask/runTask.py'
        self.stim.data_root = pathlib.Path('//allen/programs/mindscope/workgroups/dynamicrouting/DynamicRoutingTask/Data/366122')

                
class LegacyNP0Pretest(PretestSession):
    def __init__(self, pretest_config: PretestConfig) -> None:
        self.pretest_config = pretest_config
        
    @property
    def stim(self) -> Type[np_services.SessionCamstim]:
        return np_services.SessionCamstim
    
    def run_pretest_stim(self) -> None:
        self.stim.start()
                                
    def configure_services(self) -> None:
        self.stim.lims_user_id = "ben.hardcastle"
        self.stim.labtracks_mouse_id = 598796
        self.stim.override_params = json.loads(pathlib.Path("//allen/programs/mindscope/workgroups/dynamicrouting/ben/np0_pretest/params.json").read_bytes())

def configure_services(session: PretestSession) -> None:
    """For each service, apply every key in self.config['service'] as an attribute."""

    def apply_config(service) -> None:
        if config := np_config.Rig().config["services"].get(service.__name__):
            for key, value in config.items():
                setattr(service, key, value)
                logger.debug(
                    f"{service.__name__} | Configuring {service.__name__}.{key} = {getattr(service, key)}"
                )

    for service in session.services:
        for base in service.__class__.__bases__:
            apply_config(base)
        apply_config(service)
    
    np_services.MouseDirector.user = 'ben.hardcastle'
    np_services.MouseDirector.mouse = 366122
    
    session.configure_services()

    if session.pretest_config.check_barcodes:
        np_services.OpenEphys.folder = '_test_'


@functools.cache
def get_temp_dir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())

def run_pretest(
    config: PretestConfig = PretestConfig(),
    ) -> None:
    print("Starting pretest")
    session: PretestSession
    if np_config.Rig().idx == 0:
        session = LegacyNP0Pretest(config)
    else:
        session = DynamicRoutingPretest(config)
    configure_services(session)
    
    for service in session.services:
        if isinstance(service, np_services.Initializable):
            service.initialize()
            
    stoppables = tuple(_ for _ in session.recorders if isinstance(_, np_services.Stoppable))
    with np_services.stop_on_error(*stoppables):
        for service in stoppables:
            if isinstance(service, np_services.Startable):
                service.start()
        t0 = time.time()
        session.run_pretest_stim()
        t1 = time.time()
        if not config.check_barcodes:
            min_wait_time = 0
        elif config.check_sync_barcodes and not config.check_ephys_barcodes:
            min_wait_time = 35 # long enough to capture 1 sets of barcodes on sync
        else:
            min_wait_time = 70 # long enough to capture 2 sets of barcodes on sync/openephys (cannot scale time with 1 set)
        time.sleep(max(0, min_wait_time - (t1 - t0))) 
        for service in reversed(stoppables):
            if isinstance(service, np_services.Stoppable):
                service.stop()

    for service in session.services:
        if isinstance(service, np_services.Finalizable):
            service.finalize()

    np_services.VideoMVR.sync_path = np_services.OpenEphys.sync_path = session.stim.sync_path = np_services.Sync.data_files[0]
    
    # validate
    for service in session.services:
        if isinstance(service, np_services.Validatable):
            if service is not np_services.OpenEphys:
                service.validate()
            elif config.check_ephys_barcodes:
                # try validating ephys without sync (currently error prone with short pretest-like recordings)
                npc_ephys.validate_ephys(
                    root_paths=service.data_files,
                    sync_path_or_dataset=False,
                    ignore_small_folders=False,
                )
            else:
                # barcodes on sync will be validated, open ephys will be assumed to work correctly
                continue
    assert np_services.Sync.data_files is not None, "No sync file found"
    assert session.stim.data_files is not None, "No stim file found"
    
    if any((config.check_licks, config.check_opto, config.check_audio)):
        npc_sync.SyncDataset(np_services.Sync.data_files[0]).validate(
            licks=config.check_licks, opto=config.check_opto, audio=config.check_audio,
        )
    if config.check_running:
        speed, timestamps  = npc_stim.get_running_speed_from_stim_files(*session.stim.data_files, sync=np_services.Sync.data_files[0])
        if not speed.size or not timestamps.size:
            raise AssertionError("No running data found")

def parse_args() -> PretestConfig:
    parser = argparse.ArgumentParser(description="Run pretest")
    parser.add_argument("--check_ephys_barcodes", action="store_true", help="Check barcodes from Arduino are being received on open ephys and time-alignment is possible (currently error prone with short pretest-like recordings)", default=False)
    parser.add_argument("--check_sync_barcodes", action="store_true", help="Check barcodes from Arduino are being received on sync", default=False)
    parser.add_argument("--check_licks", action="store_true", help="Check lick sensor line on sync", default=False)
    parser.add_argument("--check_opto", action="store_true", help="Check opto-running line on sync", default=False)
    parser.add_argument("--check_audio", action="store_true", help="Check audio-running line on sync", default=False)
    parser.add_argument("--check_running", action="store_true", help="Check running-wheel encoder data in stim files", default=False)
    return PretestConfig(**vars(parser.parse_args()))

def main() -> None:
    logging.basicConfig(
        level="INFO",
        format="%(name)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    run_pretest(parse_args())

if __name__ == '__main__':
    main()