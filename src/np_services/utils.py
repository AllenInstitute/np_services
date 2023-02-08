from __future__ import annotations

import contextlib
import datetime
import logging
import math
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any, Generator, Literal, Mapping, Optional, Sequence, Type

import np_config
import np_logging

import np_services.zro as zro
from np_services.protocols import Stoppable

logger = np_logging.getLogger(__name__)


def config_from_zk(rig: Optional[Literal[0, 1, 2]] = None) -> Mapping[str, Any]:
    "Common `services` config plus rig-specific `services`"
    common_config = np_config.from_zk("/projects/np_workflows/defaults/configuration")[
        "services"
    ]

    if rig:
        rig_config = np_config.Rig(rig).config["services"]
    else:
        rig_config = np_config.Rig().config["services"]

    for k, v in rig_config.items():
        common_config[k] = common_config.get(k, {}) | v
    return common_config


def start_rsc_app(host, app_id) -> None:
    rsc_node = zro.Proxy(host, 6000)
    if rsc_node.p_status().get(app_id) == 0:
        logger.info("Launching %s on %s via RSC", app_id, host)
        rsc_node.p_start(app_id)
        time.sleep(0.1)
        if rsc_node.p_status().get(app_id) != 0:
            logger.warning("Failed to start %s", app_id)
            return
    logger.info("%s is running on %s", app_id, host)


def start_rsc_apps() -> None:
    rsc_app_ids_required = np_config.Rig().config["rsc_app_ids_required"]
    rig = np_config.Rig()
    for host in (rig.Sync, rig.Stim, rig.Mon, rig.Acq):
        rsc_node = zro.Proxy(host, 6000)
        status = rsc_node.p_status()
        if any(
            apps_required_in_node := [_ for _ in rsc_app_ids_required if _ in status]
        ):
            for app in apps_required_in_node:
                rsc_node.p_start(app)


@contextlib.contextmanager
def debug_logging() -> Generator[None, None, None]:
    root_logger = logging.getLogger()
    level_0 = root_logger.level
    root_logger.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        root_logger.setLevel(level_0)


@contextlib.contextmanager
def stop_on_error(obj: Stoppable, reraise=True):
    if not isinstance(obj, Stoppable):
        raise TypeError(f"{obj} does not support stop()")
    try:
        yield
    except Exception as exc:
        with contextlib.suppress(Exception):
            obj.exc = exc
            logger.info("%s interrupted by error:", obj.__name__, exc_info=exc)
            obj.stop()
        if reraise:
            raise exc


@contextlib.contextmanager
def suppress(*exceptions: Type[BaseException]):
    try:
        yield
    except exceptions or Exception as exc:
        with contextlib.suppress(Exception):
            logger.error(
                "Error suppressed: continuing despite raised exception", exc_info=exc
            )
    finally:
        return


def is_online(host: str) -> bool:
    "Use OS's `ping` cmd to check if `host` is online."
    command = ["ping", "-n" if "win" in sys.platform else "-c", "1", host]
    try:
        return subprocess.call(command, stdout=subprocess.PIPE, timeout=1.0) == 0
    except subprocess.TimeoutExpired:
        return False


def unc_to_local(path: pathlib.Path) -> pathlib.Path:
    "Convert UNC path to local path if on Windows."
    if "win" not in sys.platform:
        return path
    comp = os.environ["COMPUTERNAME"]
    if comp in path.drive:
        drive = path.drive.split("\\")[-1]
        drive = drive[:-1] if drive[-1] == "$" else drive
        drive = drive + ":" if drive[-1] != ":" else drive
        drive += "\\"
        path = pathlib.Path(drive, path.relative_to(path.drive))
    return path


def free_gb(path: str | bytes | os.PathLike) -> float:
    "Return free space at `path`, to .1 GB. Raises FileNotFoundError if `path` not accessible."
    path = pathlib.Path(path)
    path = unc_to_local(path)
    return round(shutil.disk_usage(path).free / 1e9, 1)


def get_files_created_between(
    path: str | bytes | os.PathLike,
    glob: str = "*",
    start: float | datetime.datetime = 0,
    end: Optional[float | datetime.datetime] = None,
) -> list[pathlib.Path]:
    path = pathlib.Path(path)
    if not path.is_dir():
        path = path.parent
    if not end:
        end = time.time()
    start = start.timestamp() if isinstance(start, datetime.datetime) else start
    end = end.timestamp() if isinstance(end, datetime.datetime) else end
    ctime = lambda x: x.stat().st_ctime
    files = (file for file in path.glob(glob) if int(start) <= ctime(file) <= end)
    return sorted(files, key=ctime)


def is_file_growing(path: str | bytes | os.PathLike) -> bool:
    "Compares size of most recent .sync data file at two time-points - will block for up to 20s depending on file-size."
    path = pathlib.Path(path)
    size_0 = path.stat().st_size
    # for sync: file is appended periodically in chunks that scale non-linearly with size
    if ".sync" == path.suffix:
        time.sleep(2 * math.log10(size_0))
    else:
        time.sleep(0.5 * math.log10(size_0))
    if path.stat().st_size == size_0:
        return False
    return True
