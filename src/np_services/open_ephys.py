import enum
import json
import os
import pathlib
import shutil
import socket
import sys
import time
from typing import Any, Optional, Sequence

import np_config
import np_logging
import requests

import np_services.utils as utils
from np_services.protocols import TestError

# global vars -------------------------------------------------------------------------- #
logger = np_logging.getLogger(__name__)  # logs will show full module path

__name = "OpenEphys"  # Service protocol operations will see just the 'class' name

exc: Optional[BaseException] = None
initialized: float = 0
"`time.time()` when the service was initialized."

try:
    host: str = np_config.Rig().Acq
except ValueError:
    logger.warning("Not connected to a rig: `OpenEphys.host` needs to be set manually.")
port: str | int = 37497  # 1-800-EPHYS
latest_start: float = 0
"`time.time()` when the service was last started via `start()`."

# for launching:
rsc_app_id = "open-ephys"

# device records:
gb_per_hr: int | float = 250  # per drive
min_rec_hr: int | float = 2
pretest_duration_sec: int | float = 0.5

# for resulting data:
folder: str  #! required
"The string that will be sent to Open Ephys to name the recording: typically `0123456789_366122_20220618`"
data_files: list[pathlib.Path] = []
"Storage for paths collected over the experiment."
data_root: Optional[pathlib.Path] = None

# for validation
sync_path: Optional[pathlib.Path] = None

# -------------------------------------------------------------------------------------- #


class State(enum.Enum):
    idle = "IDLE"
    acquire = "ACQUIRE"
    record = "RECORD"


class Endpoint(enum.Enum):
    status = "status"
    recording = "recording"
    processors = "processors"
    message = "message"


def launch() -> None:
    utils.start_rsc_app(host, rsc_app_id)


def pretest() -> None:
    logger.info("OpenEphys | Starting pretest")
    global folder
    folder = "_pretest_"
    initialize()
    test()
    try:
        start()
        time.sleep(pretest_duration_sec)
        verify()
    finally:
        stop()
    finalize()
    validate()
    logger.info("OpenEphys | Pretest passed")

def url(endpoint: Endpoint):
    return f"http://{host}:{port}/api/{endpoint.value}"


def get_state() -> requests.Response:
    mode = requests.get(u := url(Endpoint.status)).json().get("mode")
    logger.debug("%s -> get mode: %s", u, mode)
    return mode


def set_state(state: State) -> requests.Response:
    msg = {"mode": state.value}
    mode = requests.put(u := url(Endpoint.status), json.dumps(msg))
    logger.debug("%s <- set mode: %s", u, state.value)
    return mode


def is_connected() -> bool:
    global exc

    if not utils.is_online(host):
        exc = TestError(
            f"OpenEphys | No response from {host}: may be offline or unreachable"
        )
        return False

    try:
        state = get_state()
    except requests.RequestException:
        exc = TestError(
            f"OpenEphys | No response from Open Ephys http server: is the software started?"
        )
        return False
    else:
        if not any(_.value == state for _ in State):
            exc = TestError(f"OpenEphys | Unexpected state: {state}")
            return False

    return True


def initialize() -> None:
    logger.info("OpenEphys | Initializing")
    global data_files
    data_files = []
    global initialized
    initialized = time.time()
    global folder
    set_folder(folder)


def get_required_disk_gb() -> int | float:
    """The minimum amount of free disk space required to start recording."""
    return gb_per_hr * min_rec_hr


def is_disk_space_ok() -> bool:
    global exc
    exc = None
    required = get_required_disk_gb()
    for data_root in get_data_roots():
        try:
            free = utils.free_gb(data_root)
        except FileNotFoundError as e:
            exc = e
            logger.exception(f"{__name} data path not accessible: {data_root}")
        else:
            logger.info(
                "%s free disk space on %s: %s GB", __name, data_root.drive, free
            )
            if free < required:
                exc = ValueError(
                    f"{__name} free disk space on {data_root.drive} doesn't meet minimum of {required} GB"
                )
    if exc:
        return False
    return True


def test() -> None:
    logger.info("OpenEphys | Testing")
    if not is_connected():
        if exc:
            raise TestError(f"Acq computer {host} isn't responding, or OpenEphys isn't open") from exc
    gb = get_required_disk_gb()
    if not is_disk_space_ok():
        raise TestError(
            f"{__name} free disk space on one or more recording drives doesn't meet minimum of {gb} GB"
        ) from exc
    unlock_previous_recording()


def is_started() -> bool:
    if get_state() == State.record.value:
        return True
    return False


def is_ready_to_start() -> bool:
    if get_state() == State.acquire.value:
        return True
    return False


def start() -> None:
    logger.info("OpenEphys | Starting recording")
    if is_started():
        logger.warning("OpenEphys is already started")
        return
    if not is_ready_to_start():
        set_state(State.acquire)
        time.sleep(0.5)
    global latest_start
    latest_start = time.time()
    set_state(State.record)


def stop() -> None:
    logger.info("OpenEphys | Stopping recording")
    set_state(State.acquire)


def finalize() -> None:
    logger.info("OpenEphys | Finalizing")
    data_files.extend(get_latest_data_dirs())
    unlock_previous_recording()


def set_folder(
    name: str, prepend_text: str = "", append_text: str = ""
) -> None:
    """Recording folder string"""
    recording = requests.get(url(Endpoint.recording)).json()

    if name == "":
        name = "_"
        logger.warning(
            "OpenEphys | Recording directory cannot be empty, replaced with underscore: %s",
            name,
        )
    if "." in name:
        name.replace(".", "_")
        logger.warning(
            "OpenEphys | Recording directory cannot contain periods, replaced with underscores: %s",
            name,
        )

    recording["base_text"] = name
    recording["prepend_text"] = prepend_text
    recording["append_text"] = append_text
    logger.debug(
        "OpenEphys | Setting recording directory to: %s",
        prepend_text + name + append_text,
    )
    response = requests.put(url(Endpoint.recording), json.dumps(recording))
    time.sleep(0.1)
    if (actual := response.json().get("base_text")) != name:
        raise TestError(
            f"OpenEphys | Set folder to {name}, but software shows: {actual}"
        )


def get_folder() -> str | None:
    return requests.get(url(Endpoint.recording)).json().get("base_text")


def clear_open_ephys_name() -> None:
    set_folder("_temp_")


def set_idle():
    "Should be called before sending any configuration to Open Ephys"
    if is_started():
        stop()
    time.sleep(0.5)
    set_state(State.idle)


def unlock_previous_recording():
    "stop rec/acquiring | set name to _temp_ | record briefly | acquire | set name to folder"
    logger.debug("OpenEphys | Unlocking previous recording")
    set_idle()
    time.sleep(0.5)
    clear_open_ephys_name()
    time.sleep(0.5)
    start()
    time.sleep(0.5)
    stop()
    time.sleep(0.5)
    global folder
    set_folder(folder)


def get_record_nodes() -> list[dict[str, Any]]:
    """Returns a list of record node info dicts, incl keys `node_id`, `parent_directory`"""
    return requests.get(url(Endpoint.recording)).json().get("record_nodes", None) or []


def get_data_roots() -> list[pathlib.Path]:
    return [
        pathlib.Path(f"//{host}/{_['parent_directory'].replace(':','')}")
        for _ in get_record_nodes()
    ]


def get_latest_data_dirs() -> list[pathlib.Path]:
    """Returns the path to the latest data folder, based on the latest modified time"""
    dirs = []
    for root in get_data_roots():
        if subfolders := [
            sub
            for sub in root.iterdir()
            if sub.is_dir()
            and not any(
                _ in str(sub) for _ in ["System Volume Information", "$RECYCLE.BIN"]
            )
        ]:
            subfolders.sort(key=lambda _: _.stat().st_ctime)
            dirs.append(subfolders[-1])
    return dirs

def check_files_increasing_in_size() -> None:
    for data_dir in get_latest_data_dirs():
        for file in reversed(
            utils.get_files_created_between(
                data_dir, "*/*/*/continuous/*/sample_numbers.npy", latest_start
            )
        ):
            if utils.is_file_growing(file):
                break
        else:
            raise TestError(
                f"OpenEphys | Data file(s) not increasing in size in {data_dir}"
            )

def verify() -> None:
    logger.debug("OpenEphys | Verifying")
    check_files_increasing_in_size()    
    logger.info(
        "OpenEphys | Verified files are increasing in size for all Record Nodes"
    )


def validate() -> None:
    logger.info(f"OpenEphys | Validating")
    import npc_ephys
    npc_ephys.validate_ephys(
        root_paths=data_files,
        sync_path_or_dataset=sync_path,
        ignore_small_folders=True,
    )
    logger.info(f"OpenEphys | Validated data {'with' if sync_path else 'without'} sync")

def set_ref(ext_tip="TIP"):
    # for port in [0, 1, 2]:
    #     for slot in [0, 1, 2]:

    slot = 2  #! Test
    port = 1  #! Test
    dock = 1  # TODO may be 1 or 2 with firmware upgrade
    tip_ref_msg = {"text": f"NP REFERENCE {slot} {port} {dock} {ext_tip}"}
    # logger.info(f"sending ...
    # return
    requests.put(
        "http://localhost:37497/api/processors/100/config", json.dumps(tip_ref_msg)
    )
    time.sleep(3)


# TODO set up everything possible from here to avoid accidentally changed settings ?
# probe channels
# sampling rate
# tip ref
# signal chain?
# acq drive letters

"""
if __name__ == "__main__":
    r = requests.get(Endpoint.recording)
    print((r.json()['current_directory_name'], r.json()['prepend_text'], r.json()['append_text']))
        
    r = EphysHTTP.set_folder(path = "mouseID_", prepend_text="sessionID", append_text="_date")
    print((r.json()['current_directory_name'], r.json()['prepend_text'], r.json()['append_text']))
    
    r = EphysHTTP.set_folder(path = "mouse", prepend_text="session", append_text="date")
    print((r.json()['current_directory_name'], r.json()['prepend_text'], r.json()['append_text']))
    
    print((r.json()['base_text'])) # fails as of 06/23 https://github.com/open-ephys/plugin-GUI/pull/514
"""
