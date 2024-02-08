import functools
import os
import pathlib
import tempfile
import time
import logging
from typing import Iterable


os.environ["USE_TEST_RIG"] = "0"
os.environ["AIBS_RIG_ID"] = "NP.3"

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

    np_services.OpenEphys.folder = '__test__'


@functools.cache
def get_temp_dir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())

def main(
    recorders: Iterable[np_services.Testable] = DEFAULT_RECORDERS,
    stim: np_services.Startable = DEFAULT_STIM,
    other: Iterable[np_services.Testable] = DEFAULT_SERVICES,
    ) -> None:
    print("Starting pretest")
    
    for service in (*recorders, stim, *other):
        if isinstance(service, np_services.Initializable):
            service.initialize()
            
    stoppables = tuple(_ for _ in recorders if isinstance(_, np_services.Stoppable))
    with np_services.stop_on_error(*stoppables):
        for service in stoppables:
            if isinstance(service, np_services.Startable):
                service.start()
        stim.start()
        time.sleep(30) # long enough to capture 1 set of barcodes on sync/openephys
        for service in stoppables:
            if isinstance(service, np_services.Stoppable):
                service.stop()
                
    np_services.VideoMVR.sync_path = np_services.OpenEphys.sync_path = np_services.Sync.get_latest_data()[0]
    
    for service in (*recorders, stim, *other):
        if isinstance(service, np_services.Validatable):
            service.validate()

if __name__ == '__main__':
    main()