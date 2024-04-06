"""
Proxy classes for interacting with devices via zro/zmq.

Proxy class names must match the name of the proxy key in the config dict.
"""
import abc
import contextlib
import copy
import csv
import datetime
import functools
import itertools
import json  # loading config from Sync proxy will instantiate datetime objects
import logging
import pathlib
import re
import tempfile
import time
from typing import Any, ClassVar, Literal, Mapping, Optional, Sequence

import fabric
import np_config
import np_logging
import np_session
import npc_stim
import npc_sync
import npc_mvr
import np_tools
import yaml
import pandas as pd

import np_services.resources.mvr_connector as mvr_connector
import np_services.utils as utils
import np_services.resources.zro as zro
from np_services.protocols import *

logger = np_logging.getLogger(__name__)

CONFIG = utils.config_from_zk()

ProxyState = tuple[Literal["", "READY", "BUSY"], str]


class Proxy(abc.ABC):
    # req proxy config - hardcode or overload ensure_config()
    host: ClassVar[str]
    port: ClassVar[int]
    timeout: ClassVar[float]
    serialization: ClassVar[Literal["json", "pickle"]]

    # if a program needs to be launched (e.g. via RSC):
    rsc_app_id: str

    # if device records:
    gb_per_hr: ClassVar[int | float]
    min_rec_hr: ClassVar[int | float]
    pretest_duration_sec: ClassVar[int | float]

    # for resulting data, if device records:
    data_root: ClassVar[Optional[pathlib.Path]] = None
    data_files: ClassVar[Optional[Sequence[pathlib.Path]]] = None

    # info
    exc: ClassVar[Optional[Exception]] = None

    latest_start: ClassVar[float | int] = 0
    "`time.time()` when the service was last started via `start()`."

    @classmethod
    def ensure_config(cls) -> None:
        """Updates any missing parameters for class proxy.

        Is called in `get_proxy()` so any time we need the proxy, we have a
        correct config, without remembering to run `initialize()` or some such.
        """
        config = CONFIG.get(
            __class__.__name__, {}
        )  # class where this function is defined
        config.update(**CONFIG.get(cls.__name__, {}))  # the calling class, if different

        # for proxy (reqd):
        if not hasattr(cls, "host"):
            cls.host = config["host"]
        if not hasattr(cls, "port"):
            cls.port = int(config["port"])
        if not hasattr(cls, "timeout"):
            cls.timeout = float(config.get("timeout", 10.0))
        if not hasattr(cls, "serialization"):
            cls.serialization = config.get("serialization", "json")

        # for pretest (reqd, not used if device doesn't record)
        if not hasattr(cls, "pretest_duration_sec"):
            cls.pretest_duration_sec = config.get("pretest_duration_sec", 5)
        if not hasattr(cls, "gb_per_hr"):
            cls.gb_per_hr = config.get("gb_per_hr", 2.0)
        if not hasattr(cls, "min_rec_hr"):
            cls.min_rec_hr = config.get("min_rec_hr", 3.0)

        # for resulting data (optional):
        if not cls.data_root or cls.host not in cls.data_root.parts:
            relative_path = config.get("data", None)
            if relative_path:
                root = pathlib.Path(f"//{cls.host}/{relative_path}")
                try:
                    _ = root.exists()
                except OSError as exc:
                    cls.exc = exc
                    logger.exception(
                        "Error accessing %s data path: %s", cls.__name__, root
                    )
                    raise FileNotFoundError(
                        f"{cls.__name__} data path is not accessible: {root}"
                    ) from exc
                else:
                    cls.data_root = root
        if hasattr(cls, "data_root") and cls.data_root:
            cls.data_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def launch(cls) -> None:
        utils.start_rsc_app(cls.host, cls.rsc_app_id)
    
    @classmethod
    def kill(cls) -> None:
        utils.kill_rsc_app(cls.host, cls.rsc_app_id)

    @classmethod
    def initialize(cls) -> None:
        cls.launch()
        with contextlib.suppress(AttributeError):
            del cls.proxy
        cls.proxy = cls.get_proxy()
        if isinstance(cls, Startable) and not cls.is_ready_to_start():
            if isinstance(cls, Finalizable):
                cls.finalize()
            if not cls.is_ready_to_start():
                logger.warning(
                    "%s not ready to start: %s", cls.__name__, cls.get_state()
                )
                return
        if cls.data_root:
            cls.data_files = []
        cls.sync_path = None
        cls.initialization = time.time()
        logger.info("%s(%s) initialized: ready for use", __class__.__name__, cls.__name__)

    @classmethod
    def test(cls) -> None:
        "Quickly verify service is working and ready for use, or raise `TestError`."
        logger.debug("Testing %s proxy", cls.__name__)
        if not cls.is_connected():
            raise TestError(
                f"{cls.__name__} not connected to {cls.host}:{cls.port}"
            ) from cls.exc
        logger.debug(
            "%s proxy connection to %s:%s confirmed", cls.__name__, cls.host, cls.port
        )
        gb = cls.get_required_disk_gb()
        if not cls.is_disk_space_ok():
            raise TestError(
                f"{cls.__name__} free disk space on {cls.data_root.drive} doesn't meet minimum of {gb} GB"
            ) from cls.exc
        logger.debug("%s(%s) tested successfully", __class__.__name__, cls.__name__)

    @classmethod
    def get_proxy(cls) -> zro.DeviceProxy:
        "Return a proxy to the service without re-creating unnecessarily."
        with contextlib.suppress(AttributeError):
            return cls.proxy
        cls.ensure_config()
        logger.debug("Creating %s proxy to %s:%s", cls.__name__, cls.host, cls.port)
        cls.proxy = zro.DeviceProxy(cls.host, cls.port, cls.timeout, cls.serialization)
        return cls.get_proxy()

    @classmethod
    def get_state(cls) -> ProxyState | dict:
        "Dict may be deprecated: is no longer returned by Sync or Camstim proxies."
        state = cls.get_proxy().get_state()
        logger.debug("%s state: %s", cls.__name__, state)
        return state

    @classmethod
    def get_latest_data(
        cls: Recorder, glob: Optional[str] = None, subfolders: str = ""
    ) -> list[pathlib.Path] | None:
        cls.ensure_config()
        if not cls.data_root:
            return None
        if subfolders == "/":  # can alter path to drive root
            subfolders = ""
        if not glob:
            glob = f"*{cls.raw_suffix}" if hasattr(cls, "raw_suffix") else "*"
        if not hasattr(cls, "latest_start"):
            data_paths = utils.get_files_created_between(
                cls.data_root / subfolders, glob
            )
            if not data_paths:
                return None
            return [
                max(data_paths, key=lambda x: x.stat().st_mtime)
            ]
        return utils.get_files_created_between(
            cls.data_root / subfolders, glob, cls.latest_start
        )

    @classmethod
    def get_required_disk_gb(cls) -> float:
        "Return the minimum disk space required prior to start (to .1 GB). Returns `0.0` if service generates no data."
        cls.ensure_config()
        if not isinstance(cls, Startable):
            return 0.0
        return round(cls.min_rec_hr * cls.gb_per_hr, 1)

    @classmethod
    def is_disk_space_ok(cls) -> bool:
        required = cls.get_required_disk_gb()
        if required == 0.0:
            return True
        try:
            free = utils.free_gb(cls.data_root)
        except FileNotFoundError as exc:
            cls.exc = exc
            logger.exception(
                f"{cls.__name__} data path not accessible: {cls.data_root}"
            )
            return False
        else:
            logger.debug(
                "%s free disk space on %s: %s GB",
                cls.__name__,
                cls.data_root.drive,
                free,
            )
            return free > required

    @classmethod
    def is_connected(cls) -> bool:
        if not utils.is_online(cls.host):
            cls.exc = ConnectionError(
                f"No response from {cls.host}: may be offline or unreachable"
            )
            return False
        try:
            _ = cls.get_proxy().uptime
        except zro.ZroError as exc:
            cls.exc = exc
            logger.exception(
                f"{cls.__name__} proxy connection to {cls.host}:{cls.port} failed"
            )
            return False
        try:
            _ = cls.get_state()
        except zro.ZroError as exc:
            cls.exc = exc
            logger.exception(
                f"{cls.__name__} proxy connection to {cls.host}:{cls.port} failed"
            )
            return False
        return True


class CamstimSyncShared(Proxy):
    started_state: ClassVar[Sequence[str]]
    
    @classmethod
    def is_ready_to_start(cls) -> bool:
        if cls.is_started():
            return False
        state = cls.get_state()
        if isinstance(state, Mapping) and state.get("message", "") == "READY":
            return True
        if isinstance(state, Sequence) and "READY" in state:
            return True
        return False

    @classmethod
    def is_started(cls) -> bool:
        return len(state := cls.get_state()) and all(
            msg in state for msg in cls.started_state
        )

    @classmethod
    def start(cls) -> None:
        logger.info("%s | Starting recording", cls.__name__)
        if cls.is_started():
            logger.warning(
                "%s already started - should be stopped manually", cls.__name__
            )
            return
            # otherwise, Sync - for example - would stop current recording and start another
        if not cls.is_ready_to_start():
            logger.error("%s not ready to start: %s", cls.__name__, cls.get_state())
            raise AssertionError(
                f"{cls.__name__} not ready to start: {cls.get_state()}"
            )
        cls.latest_start = time.time()
        cls.get_proxy().start()

    @classmethod
    def pretest(cls) -> None:
        "Test all critical functions"
        with np_logging.debug():
            logger.debug("Starting %s pretest", cls.__name__)
            cls.initialize()  # calls test()

            with utils.stop_on_error(cls):
                cls.start()
                time.sleep(1)
                cls.verify()
                time.sleep(cls.pretest_duration_sec)
                # stop() called by context manager at exit, regardless
            cls.finalize()
            cls.validate()
        logger.info("%s pretest complete", cls.__name__)

    @classmethod
    def verify(cls) -> None:
        "Assert latest data file is currently increasing in size, or raise AssertionError."
        if not cls.is_started():
            logger.warning(
                "Cannot verify %s if not started: %s", cls.__name__, cls.get_state()
            )
            raise AssertionError(f"{cls.__name__} not started: {cls.get_state()}")

    @classmethod
    def stop(cls) -> None:
        logger.debug("Stopping %s", cls.__name__)
        cls.get_proxy().stop()
        logger.info("%s | Stopped recording", cls.__name__)

    # --- End of possible Camstim/Sync shared methods ---

    # --- Sync-specific methods ---


class Sync(CamstimSyncShared):
    host = np_config.Rig().Sync
    started_state = ("BUSY", "RECORDING")
    raw_suffix: str = ".sync"
    rsc_app_id: str = "sync_device"

    @classmethod
    def ensure_config(cls) -> None:
        """Updates any missing parameters for class proxy.

        Is called in `get_proxy()` so any time we need the proxy, we have a
        correct config, without remembering to run `initialize()` or some such.
        """
        config = CONFIG.get(
            __class__.__name__, {}
        )  # class where this function is defined
        config.update(**CONFIG.get(cls.__name__, {}))  # the calling class, if different

        # for proxy (reqd):
        if not hasattr(cls, "host"):
            cls.host = config["host"]
        if not hasattr(cls, "port"):
            cls.port = int(config["port"])
        if not hasattr(cls, "timeout"):
            cls.timeout = float(config.get("timeout", 10.0))
        if not hasattr(cls, "serialization"):
            cls.serialization = config.get("serialization", "json")

        # for pretest (reqd, not used if device doesn't record)
        if not hasattr(cls, "pretest_duration_sec"):
            cls.pretest_duration_sec = config.get("pretest_duration_sec", 5)
        if not hasattr(cls, "gb_per_hr"):
            cls.gb_per_hr = config.get("gb_per_hr", 2.0)
        if not hasattr(cls, "min_rec_hr"):
            cls.min_rec_hr = config.get("min_rec_hr", 3.0)

        # for resulting data (optional):
        if not cls.data_root or cls.host not in cls.data_root.parts:
            relative_path = config.get("data", None)
            if relative_path:
                root = pathlib.Path(f"//{cls.host}/{relative_path}")
                try:
                    _ = root.exists()
                except OSError as exc:
                    cls.exc = exc
                    logger.exception(
                        "Error accessing %s data path: %s", cls.__name__, root
                    )
                    raise FileNotFoundError(
                        f"{cls.__name__} data path is not accessible: {root}"
                    ) from exc
                else:
                    cls.data_root = root
        if hasattr(cls, "data_root"):
            cls.data_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def finalize(cls) -> None:
        logger.debug("Finalizing %s", cls.__name__)
        if cls.is_started():
            cls.stop()
        while not cls.is_ready_to_start():
            logger.debug("Waiting for %s to finish processing", cls.__name__)
            time.sleep(1)  # TODO add backoff module
        if not cls.data_files:
            cls.data_files = []
        cls.data_files.extend(new := cls.get_latest_data("*.h5"))
        logger.debug("%s processing finished: %s", cls.__name__, [_.name for _ in new])

    @classmethod
    def shutdown(cls) -> None:
        logger.debug("Shutting down %s", cls.__name__)
        cls.stop()
        try:
            del cls.proxy
        except Exception as exc:
            logger.debug("Failed to delete %s proxy: %s", cls.__name__, exc)
            cls.exc = exc

    @classmethod
    def get_config(cls) -> dict[str, Any | datetime.datetime]:
        "Sync config, including `line_labels` and `frequency`"
        if cls.serialization in ("json", "j"):
            return eval(cls.get_proxy().config)
        if cls.serialization in ("pickle", "pkl", "p"):
            return cls.get_proxy().config

    @classmethod
    def validate(cls, data: Optional[pathlib.Path] = None) -> None:
        "Check that data file is valid, or raise AssertionError."
        logger.debug("Validating %s data", cls.__name__)
        if not data and bool(files := cls.get_latest_data("*.h5")):
            data = files[-1]
            logger.debug(
                "No data file provided: validating most-recent data in %s: %s",
                cls.data_root,
                data.name,
            )
            if cls.is_started():
                logger.warning(
                    f"Attempted to validate current data file while recording"
                )
                return
            elif not cls.is_ready_to_start():
                cls.finalize()
        try:
            import h5py
        except ImportError:
            logger.warning("h5py not installed: cannot open Sync data")
            cls.min_validation(data)
        else:
            cls.full_validation(data)

    @classmethod
    def verify(cls) -> None:
        "Assert latest data file is currently increasing in size, or raise AssertionError."
        super().verify()
        if cls.data_root and not utils.is_file_growing(cls.get_latest_data()[-1]):
            raise AssertionError(
                f"{cls.__name__} latest data file is not increasing in size: {cls.get_latest_data()[-1]}"
            )
        logger.info("%s | Verified: file on disk is increasing in size", cls.__name__)
        
    @classmethod
    def full_validation(cls, data: pathlib.Path) -> None:
        npc_sync.get_sync_data(data).validate()

    @classmethod
    def min_validation(cls, data: pathlib.Path) -> None:
        if data.stat().st_size == 0:
            raise AssertionError(f"Empty file: {data}")
        if data.suffix != ".h5":
            raise FileNotFoundError(
                f"Expected .sync to be converted to .h5 immediately after recording stopped: {data}"
            )
        logger.debug("%s minimal validation passed for %s", cls.__name__, data.name)


class Phidget(CamstimSyncShared):
    host = np_config.Rig().Stim
    rsc_app_id = "phidget_server"


class Camstim(CamstimSyncShared):
    host = np_config.Rig().Stim
    started_state = ("BUSY", "Script in progress.")
    rsc_app_id = "camstim_agent"
    sync_path: Optional[pathlib.Path] = None

    @classmethod
    def launch(cls) -> None:
        super().launch()
        Phidget.launch()

    @classmethod
    def get_config(cls) -> dict[str, Any]:
        return cls.get_proxy().config

    @classmethod
    def ensure_config(cls) -> None:
        """Updates any missing parameters for class proxy.

        Is called in `get_proxy()` so any time we need the proxy, we have a
        correct config, without remembering to run `initialize()` or some such.
        """
        config = CONFIG.get(
            __class__.__name__, {}
        )  # class where this function is defined
        config.update(**CONFIG.get(cls.__name__, {}))  # the calling class, if different

        # for proxy (reqd):
        if not hasattr(cls, "host"):
            cls.host = config["host"]
        if not hasattr(cls, "port"):
            cls.port = int(config["port"])
        if not hasattr(cls, "timeout"):
            cls.timeout = float(config.get("timeout", 10.0))
        if not hasattr(cls, "serialization"):
            cls.serialization = config.get("serialization", "json")

        # for pretest (reqd, not used if device doesn't record)
        if not hasattr(cls, "pretest_duration_sec"):
            cls.pretest_duration_sec = config.get("pretest_duration_sec", 5)
        if not hasattr(cls, "gb_per_hr"):
            cls.gb_per_hr = config.get("gb_per_hr", 2.0)
        if not hasattr(cls, "min_rec_hr"):
            cls.min_rec_hr = config.get("min_rec_hr", 3.0)

        # for resulting data (optional):
        if not cls.data_root:
            relative_path = config.get("data", None)
            if relative_path:
                root = pathlib.Path(f"//{cls.host}/{relative_path}")
                try:
                    _ = root.exists()
                except OSError as exc:
                    cls.exc = exc
                    logger.exception(
                        "Error accessing %s data path: %s", cls.__name__, root
                    )
                    raise FileNotFoundError(
                        f"{cls.__name__} data path is not accessible: {root}"
                    ) from exc
                else:
                    cls.data_root = root
        if hasattr(cls, "data_root") and cls.data_root is not None:
            cls.data_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def finalize(cls) -> None:
        logger.info("Finalizing %s", cls.__name__)
        if cls.is_started():
            cls.stop()
        count = 0
        while not cls.is_ready_to_start():
            if count % 120 == 0:
                logger.debug("Waiting for %s to finish processing", cls.__name__)
            time.sleep(1)  # TODO add backoff module
        if not cls.data_files:
            cls.data_files = []
        cls.data_files.extend(new := itertools.chain(cls.get_latest_data("*pkl"), cls.get_latest_data("*hdf5")))
        logger.info("%s added new data: %s", cls.__name__, [_.name for _ in new])

    @classmethod
    def validate(cls) -> None:
        if not cls.sync_path:
            logger.warning("Cannot validate stim without sync file: assign `stim.sync_path`")
            return
        logger.info("Validating %s", cls.__name__)  
        for file in cls.data_files:
            npc_stim.validate_stim(file, sync=cls.sync_path)
        logger.info(f"Validated {len(cls.data_files)} stim files with sync")

class ScriptCamstim(Camstim):
    script: ClassVar[str]
    "path to script on Stim computer"
    params: ClassVar[dict[str, Any]] = {}

    @classmethod
    def pretest(cls) -> None:
        pretest_mouse = "599657"

        cls.script = "C:/ProgramData/StimulusFiles/dev/bi_script_pretest_v2.py"

        # get params from MTrain, as if we were running `Agent.start_session`
        cls.params = np_session.mtrain.MTrain(pretest_mouse).stage["parameters"]
        cls.params.update(dict(user_name="ben.hardcastle", mouse_id=pretest_mouse))

        logger.info(
            "%s | Pretest: running %s with MTrain stage params for mouse %s",
            cls.__name__,
            cls.script,
            pretest_mouse,
        )
        cls.initialize()
        cls.test()
        cls.start()
        while not cls.is_ready_to_start():
            logger.debug("Waiting for %s to finish processing", cls.__name__)
            time.sleep(10)
        cls.finalize()
        # cls.validate()
        cls.initialize()

    @classmethod
    def start(cls):
        cls.latest_start = time.time()
        cls.get_proxy().start_script(cls.script, cls.params)


class SessionCamstim(Camstim):
    lims_user_id: ClassVar[str]
    labtracks_mouse_id: ClassVar[int]
    override_params: ClassVar[dict[str, Any] | None] = None
    
    @classmethod
    def start(cls):
        cls.latest_start = time.time()
        cls.get_proxy().start_session(
            cls.labtracks_mouse_id, cls.lims_user_id, override_params=cls.override_params
        )

    @classmethod
    def pretest(cls) -> None:
        cls.labtracks_mouse_id = 598796
        cls.lims_user_id = "ben.hardcastle"
        logger.info(
            "%s | Pretest with mouse %s, user %s",
            cls.__name__,
            cls.labtracks_mouse_id,
            cls.lims_user_id,
        )
        super().pretest()


class NoCamstim(Camstim):
    "Run remote files (e.g. .bat) without sending directly to Camstim Agent"

    remote_file: ClassVar[str | pathlib.Path]
    extra_args: ClassVar[list[str]] = []
    ssh: ClassVar[fabric.Connection]
    user: ClassVar[str] = "svc_neuropix"
    password: ClassVar[str]

    @classmethod
    def pretest(cls) -> None:
        logger.warning("%s | Pretest not implemented", cls.__name__)

    @classmethod
    def get_ssh(cls) -> fabric.Connection:
        with contextlib.suppress(AttributeError):
            return cls.ssh
        cls.initialize()
        return cls.ssh

    @classmethod
    def initialize(cls) -> None:
        if not hasattr(cls, "password"):
            cls.password = input(f"{cls.__name__} | Enter password for {cls.host}: ")
        cls.remote_file = utils.unc_to_local(pathlib.Path(cls.remote_file))
        cls.ssh = fabric.Connection(
            cls.host, cls.user, connect_kwargs=dict(password=cls.password)
        )
        super().initialize()
        cls.test()

    @classmethod
    def test(cls) -> None:
        super().test()
        logger.debug(f"{cls.__name__} | Testing")
        try:
            result = cls.get_ssh().run("hostname", hide=True)
        except Exception as exc:
            raise TestError(
                f"{cls.__name__} Error connecting to {cls.host} via ssh: {exc!r}. Is this password correct? {cls.password}"
            )
        else:
            if result.exited != 0:
                raise TestError(
                    f"{cls.__name__} Error connecting to {cls.host} via ssh: {result}"
                )
            logger.debug(f"{cls.__name__} | Connected to {cls.host} via ssh")

        try:
            result = cls.get_ssh().run(f"type {cls.remote_file}", hide=True)
        except Exception as exc:
            extra = (
                f" | '{exc.result.command}': {exc.result.stderr.strip()!r}"
                if hasattr(exc, "result")
                else ""
            )
            raise TestError(
                f"{cls.__name__} | Error calling ssh-executed command{extra}"
            )
        else:
            if result.exited != 0:
                raise TestError(
                    f"{cls.__name__} Error accessing {cls.remote_file} on {cls.host} - is filepath correct? {result}"
                )
            logger.debug(
                f"{cls.__name__} | {cls.remote_file} is accessible via ssh on {cls.host}"
            )

    @classmethod
    def start(cls):
        if cls.is_started():
            logger.warning(f"{cls.__name__} already started")
            return
        logger.debug(f"{cls.__name__} | Starting {cls.remote_file} on {cls.host}")
        cls.latest_start = time.time()
        cls.get_ssh().run(f"call {cls.remote_file} {cls.extra_args}")

    @classmethod
    def verify(cls):
        logger.warning(f"{cls.__name__} | No verification implemented")


class MouseDirector(Proxy):
    """Communicate with the ZMQ remote object specified here:
    http://aibspi.corp.alleninstitute.org/braintv/visual_behavior/mouse_director/-/blob/master/src/mousedirector.py

    ::
        MouseDirector.get_proxy().set_mouse_id(str(366122))
        MouseDirector.get_proxy().set_user_id("ben.hardcastle")
    """
    
    user: ClassVar[str | np_session.User]
    mouse: ClassVar[str | int | np_session.Mouse]
    
    rsc_app_id = CONFIG['MouseDirector']['rsc_app_id']
    host = np_config.Rig().Mon
    gb_per_hr = 0
    serialization = "json"
    started_state: ClassVar[ProxyState] = ("READY", "")
    not_connected_state: ClassVar[ProxyState] = ("", "NOT_CONNECTED")
    
    @classmethod
    def pretest(cls):
        with np_logging.debug():
            logger.debug(f"{cls.__name__} | Pretest")
            cls.user = "ben.hardcastle"
            cls.mouse = 366122
            cls.initialize()
            cls.test()
            cls.get_proxy().retract_lick_spout()
            time.sleep(3)
            cls.get_proxy().extend_lick_spout()
            time.sleep(3)
            cls.get_proxy().retract_lick_spout()
            time.sleep(3)
        logger.info(f"{cls.__name__} | Pretest passed")
        
    @classmethod
    def initialize(cls):
        logger.debug(f"{cls.__name__} | Initializing")
        super().initialize()
        cls.get_proxy().set_mouse_id(str(cls.mouse))
        time.sleep(1)
        cls.get_proxy().set_user_id(str(cls.user))
        time.sleep(1)
        logger.debug(f"{cls.__name__} | Initialized with mouse {cls.mouse}, user {cls.user}")
        
    @classmethod
    def get_state(cls) -> ProxyState:
        result: str = cls.get_proxy().rig_dict
        if str(np_config.Rig()) in result:
            return cls.started_state
        return cls.not_connected_state
    
class Cam3d(CamstimSyncShared):
    
    label: str
    
    host = np_config.Rig().Mon
    serialization = "json"
    started_state = ["READY", "CAMERAS_OPEN,CAMERAS_ACQUIRING"]
    rsc_app_id = CONFIG['Cam3d']['rsc_app_id']
    data_files: ClassVar[list[pathlib.Path]] = []
    
    @classmethod
    def is_started(cls) -> bool:
        return cls.get_state() == cls.started_state
    
    @classmethod
    def is_ready_to_start(cls) -> bool:
       if cls.is_started():
           return False
       time.sleep(1)
       if (
           cls.get_state() == cls.started_state
           or 'READY' not in cls.get_state()
        ):
            return False
       return True
    
    @classmethod
    def initialize(cls) -> None:
        logger.debug(f"{cls.__name__} | Initializing")
        super().initialize()
        if not cls.is_ready_to_start():
            cls.reenable_cameras()
            
        time.sleep(1)
        
    @classmethod
    def reenable_cameras(cls) -> None:
        cls.get_proxy().release_cameras()
        time.sleep(.2)
        cls.get_proxy().enable_cameras()
        time.sleep(.2)
        cls.get_proxy().stop_capture()
        time.sleep(.2)
        cls.get_proxy().start_capture()
        time.sleep(.2)
        
    @classmethod
    def generate_image_paths(cls) -> tuple[pathlib.Path, pathlib.Path]:
        if not hasattr(cls, 'label') or not cls.label:
            logger.warning(f"{cls.__name__} | `cls.label` not specified")
        def path(side: str) -> pathlib.Path:
            return cls.data_root / f"{datetime.datetime.now():%Y%m%d_%H%M%S}_{getattr(cls, 'label', 'image')}_{side}.png"
        return path('left'), path('right')
    
    @classmethod
    def start(cls) -> None:
        logger.debug(f"{cls.__name__} | Starting")
        cls.latest_start = time.time()
        left, right = cls.generate_image_paths()
        cls.get_proxy().save_left_image(str(left))
        cls.get_proxy().save_right_image(str(right))
        time.sleep(.5)
        for path, side in zip((left, right), ('Left', 'Right')):
            if path.exists():
                logger.debug(f"{cls.__name__} | {side} image saved to {path}")
            else:
                logger.debug(f"{cls.__name__} | {side} image capture request sent, but image not saved")
    
    @classmethod
    def finalize(cls) -> None:
        logger.debug(f"{cls.__name__} | Finalizing")
        counter = 0
        while (
            not (latest := cls.get_latest_data('*'))
            or cls.is_started()
        ):
            time.sleep(1)
            counter += 1
            if counter == 3:
                cls.reenable_cameras()
            break
        cls.data_files.extend(latest)
        logger.debug(f"{cls.__name__} | Images captured: {latest}")
        
    @classmethod
    def validate(cls):
        if not (latest := cls.get_latest_data('*')) or len(latest) != 2:
            raise AssertionError(f"{cls.__name__} | Expected 2 images, got {len(latest)}: {latest}")

    @classmethod
    def stop(cls):
        logger.debug("%s | `stop()` not implemented", cls.__name__)
        
    @classmethod
    def pretest(cls):
        with np_logging.debug():
            logger.debug(f"{cls.__name__} | Pretest")
            cls.label = 'pretest'
            cls.initialize()
            cls.test()
            cls.start()
            cls.finalize()
            cls.validate()
            logger.info(f"{cls.__name__} | Pretest passed")
            
class MVR(CamstimSyncShared):
    
    # req proxy config - hardcode or overload ensure_config()
    host: ClassVar[str] = np_config.Rig().Mon
    port: ClassVar[int] = CONFIG['MVR']['port']

    re_aux: re.Pattern = re.compile("aux|USB!|none", re.IGNORECASE)

    @classmethod
    def is_connected(cls) -> bool:
        if not utils.is_online(cls.host):
            cls.exc = ConnectionError(
                f"No response from {cls.host}: may be offline or unreachable"
            )
            return False
        if not cls.get_proxy()._mvr_connected:
            cls.exc = ConnectionError(f"MVR likely not running on {cls.host}")
            return False
        try:
            _ = cls.get_camera_status()
        except ConnectionError as exc:
            cls.exc = exc
            return False
        return True

    @classmethod
    def initialize(cls) -> None:
        with contextlib.suppress(AttributeError):
            del cls.proxy
        cls.proxy = cls.get_proxy()
        cls.test()
        cls.configure_cameras()
        _ = cls.get_proxy().read()  # empty buffer
        if isinstance(cls, Startable) and not cls.is_ready_to_start():
            if cls.is_started() and isinstance(cls, Stoppable):
                cls.stop()
            if isinstance(cls, Finalizable):
                cls.finalize()
            if not cls.is_ready_to_start():
                logger.warning(
                    "%s not ready to start: %s", cls.__name__, cls.get_state()
                )
                return
        if cls.data_root:
            cls.data_files = []
        cls.initialization = time.time()
        logger.info("%s initialized: ready for use", cls.__name__)

    @classmethod
    def shutdown(cls) -> None:
        cls.get_proxy()._mvr_sock.close()
        del cls.proxy

    @classmethod
    def get_proxy(cls) -> mvr_connector.MVRConnector:
        with contextlib.suppress(AttributeError):
            return cls.proxy
        cls.ensure_config()
        logger.debug("Creating %s proxy to %s:%s", cls.__name__, cls.host, cls.port)
        cls.proxy = mvr_connector.MVRConnector({"host": cls.host, "port": cls.port})
        cls.proxy._mvr_sock.settimeout(cls.timeout)
        return cls.get_proxy()

    @classmethod
    def get_cameras(cls) -> list[dict[str, str]]:
        if not hasattr(cls, "all_cameras"):
            cls.get_proxy().read()
            cls.all_cameras = cls.get_proxy().request_camera_ids()[0]["value"]
        return cls.all_cameras

    @classmethod
    def get_camera_status(cls) -> list[dict[str, str]]:
        _ = cls.get_proxy().read()  # empty buffer
        _ = cls.get_proxy()._send({"mvr_request": "get_camera_status"})
        for msg in reversed(cls.get_proxy().read()):
            if msg.get("mvr_response", "") == "get_camera_status" and (
                cams := msg.get("value", [])
            ):
                break
        else:
            logger.error("Could not get camera status from %s", cls.host)
            raise ConnectionError(f"Could not get camera status from {cls.host}")
        return [
            _
            for _ in cams
            if any(_["camera_id"] == __["id"] for __ in cls.get_cameras())
        ]

    @classmethod
    def get_state(cls) -> ProxyState:
        if not cls.is_connected():
            return "", "MVR_CLOSED"
        status = cls.get_camera_status()
        # cam status could change between calls, so only get once
        if any(not _["is_open"] for _ in status):
            return "", "CAMERA_CLOSED"
        if any(not _["is_streaming"] for _ in status):
            return "", "CAMERA_NOT_STREAMING"
        if cls.get_cameras_recording(status):
            return "BUSY", "RECORDING"
        return "READY", ""

    @classmethod
    def get_cameras_recording(cls, status=None) -> list[dict[str, str]]:
        return [_ for _ in status or cls.get_camera_status() if _["is_recording"]]

    @classmethod
    def is_ready_to_start(cls) -> bool:
        if cls.is_started():
            return False
        return all(
            _["is_open"] and _["is_streaming"] and not _["is_recording"]
            for _ in cls.get_camera_status()
        )

    @classmethod
    def configure_cameras(cls) -> None:
        "Set MVR to record video from subset of all cameras, via `get_cameras` (implemented by subclass)"
        cam_ids = [_["id"] for _ in cls.get_cameras()]
        cls.get_proxy().define_hosts(cam_ids)
        cls.get_proxy().start_display()


class ImageMVR(MVR):
    
    gb_per_hr: ClassVar[int | float] = CONFIG['ImageMVR']["gb_per_hr"]
    min_rec_hr: ClassVar[int | float] = CONFIG['ImageMVR']["min_rec_hr"]
    
    label: ClassVar[str]
    "Rename file after capture to include label"

    # TODO ready state is if Aux cam is_open
    @classmethod
    def get_cameras(cls) -> list[dict[str, str]]:
        "Aux cam only"
        cams = super().get_cameras()
        return [_ for _ in cams if cls.re_aux.search(_["label"])]

    @classmethod
    def start(cls):
        if not cls.is_ready_to_start():
            # TODO display state, wait on user input to continue
            logger.error("%s not ready to start: %s", cls.__name__, cls.get_state())
            raise AssertionError(
                f"{cls.__name__} not ready to start: {cls.get_state()}"
            )
        cls.latest_start = time.time()
        cls.get_proxy().take_snapshot()

    @classmethod
    def stop(cls):
        "Overload parent method to do nothing"
        pass

    @classmethod
    def is_started(cls) -> bool:
        for msg in cls.get_proxy().read():
            if msg.get("mvr_broadcast", "") == "snapshot_converted":
                return True
            if msg.get("mvr_broadcast", "") == "snapshot_failed":
                return False
        return False

    @classmethod
    def verify(cls):
        "Overload parent method to do nothing"
        pass

    # TODO
    @classmethod
    def validate(cls) -> None:
        logger.warning("%s.validate() not implemented", cls.__name__)

    @classmethod
    def finalize(cls) -> None:
        logger.debug("Finalizing %s", cls.__name__)
        t0 = time.time()
        timedout = lambda: time.time() > t0 + 10
        while (
            cls.is_started()
            or not cls.is_ready_to_start()
            or not cls.get_latest_data("*")
            or cls.get_latest_data(".bmp")
        ) and not timedout():
            logger.debug("Waiting for %s to finish processing", cls.__name__)
            time.sleep(1)  # TODO add backoff module
        if timedout():
            logger.warning(
                "Timed out waiting for %s to finish processing", cls.__name__
            )
            return
        if not hasattr(cls, "data_files") or not cls.data_files:
            cls.data_files = []
        new = cls.get_latest_data("*")
        if hasattr(cls, "label") and cls.label:
            new = [_.rename(_.with_stem(f"{_.stem}_{cls.label}")) for _ in new]
        cls.data_files.extend(new)
        logger.debug("%s processing finished: %s", cls.__name__, [_.name for _ in new])


class VideoMVR(MVR):
    
    pretest_duration_sec: ClassVar[int | float] = CONFIG['VideoMVR']["pretest_duration_sec"]
    gb_per_hr: ClassVar[int | float] = CONFIG['VideoMVR']["gb_per_hr"]
    min_rec_hr: ClassVar[int | float] = CONFIG['VideoMVR']["min_rec_hr"]

    raw_suffix: ClassVar[str] = ".mp4"

    started_state = ("BUSY", "RECORDING")
    sync_path: Optional[pathlib.Path] = None
    
    @classmethod
    def get_cameras(cls) -> list[dict[str, str]]:
        "All available cams except Aux"
        cams = super().get_cameras()
        # check for camera labels with known Aux cam names
        return [_ for _ in cams if cls.re_aux.search(_["label"]) is None]

    @classmethod
    def start(cls) -> None:
        logger.info("%s | Starting recording", cls.__name__)
        cls.latest_start = time.time()
        cls.get_proxy().start_record(record_time=24 * 60 * 60,)  # sec

    @classmethod
    def verify(cls) -> None:
        "Assert data exists since latest start, or raise AssertionError."
        # files grow infrequently while MVR's recording - checking their size
        # is unreliable
        if not cls.is_started():
            logger.warning(
                "Cannot verify %s if not started: %s", cls.__name__, cls.get_state()
            )
            raise AssertionError(f"{cls.__name__} not started: {cls.get_state()}")
        if datetime.datetime.fromtimestamp(
            cls.latest_start
        ) < datetime.datetime.now() - datetime.timedelta(
            seconds=cls.pretest_duration_sec
        ):
            time.sleep(cls.pretest_duration_sec)
        if not (files := cls.get_latest_data()) or len(files) < len(
            cls.get_cameras_recording()
        ):
            raise AssertionError(
                f"{cls.__name__} files do not match the number of cameras: {files}"
            )
        logger.info(
            "%s | Verified: %s cameras recording to disk", cls.__name__, len(files)
        )
    @classmethod
    def stop(cls) -> None:
        cls.get_proxy().stop_record()
        logger.info("%s | Stopped recording", cls.__name__)
        
    @classmethod
    def is_started(cls) -> bool:
        if len(state := cls.get_state()) and all(
            msg in state for msg in cls.started_state
        ):
            return True
        return False

    @classmethod
    def finalize(cls) -> None:
        logger.debug("Finalizing %s", cls.__name__)
        if cls.is_started():
            cls.stop()
        t0 = time.time()
        timedout = lambda: time.time() > t0 + 30
        while not cls.is_ready_to_start() and not timedout():
            logger.debug("Waiting for %s to finish processing", cls.__name__)
            time.sleep(1)  # TODO add backoff module
        if timedout():
            logger.warning(
                "Timed out waiting for %s to finish processing", cls.__name__
            )
            return
        if not hasattr(cls, "data_files"):
            cls.data_files = []
        cls.data_files.extend(
            new := (cls.get_latest_data("*.mp4") + cls.get_latest_data("*.json"))
        )
        logger.debug("%s processing finished: %s", cls.__name__, [_.name for _ in new])

    @classmethod
    def validate(cls) -> None:
        tempdir = pathlib.Path(tempfile.gettempdir())
        tempfiles: list[pathlib.Path] = []
        # currently can't pass individual files to mvrdataset - just a dir
        for file in itertools.chain(cls.get_latest_data("*.mp4"), cls.get_latest_data("*.json")):
            np_tools.copy(file, t := tempdir / file.name)
            tempfiles.append(t)
        npc_mvr.MVRDataset(
            tempdir, 
            getattr(cls, "sync_path", None),
        )
        logger.info(f"Validated {len(tempfiles)} video/info files {'with' if getattr(cls, 'sync_path', None) else 'without'} sync")
        for file in tempfiles:
            file.unlink(missing_ok=True)
class JsonRecorder:
    "Just needs a `start` method that calls `write()`."

    log_name: ClassVar[str]
    log_root: ClassVar[pathlib.Path]

    @abc.abstractclassmethod
    def start() -> None:
        pass
    
    @classmethod
    def pretest(cls) -> None:
        with np_logging.debug():
            cls.initialize()
            cls.start()
            cls.validate()
        logger.info("%s | Pretest passed", cls.__name__)
        
    @classmethod
    def ensure_config(cls) -> None:
        config = CONFIG.get(
            __class__.__name__, {}
        )  # class where this function is defined
        config.update(**CONFIG.get(cls.__name__, {}))  # the calling class, if different

        if not hasattr(cls, "log_name"):
            cls.log_name = config.get("log_name", "{}_.json")
        cls.log_name = cls.log_name.format(
            datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        )

        if not hasattr(cls, "log_root"):
            cls.log_root = config.get("log_root", ".")
        cls.log_root = pathlib.Path(cls.log_root).resolve()

    @classmethod
    def initialize(cls) -> None:
        logger.debug("%s initializing", __class__.__name__)
        cls.ensure_config()
        cls.initialization = time.time()
        log = (cls.log_root / cls.log_name).with_suffix(".json")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch(exist_ok=True)
        if log.read_text().strip() == "":
            log.write_text("{}")
        cls.all_files = [log]
        cls.test()

    @classmethod
    def test(cls) -> None:
        logger.debug("%s testing", __class__.__name__)
        try:
            _ = cls.get_current_log().read_bytes()
        except OSError as exc:
            raise TestError(
                f"{__class__.__name__} failed to open {cls.get_current_log()}"
            ) from exc

    @classmethod
    def get_current_log(cls) -> pathlib.Path:
        if not hasattr(cls, "initialization"):
            cls.initialize()
        return cls.all_files[-1]

    @classmethod
    def read(cls) -> dict[str, str | float]:
        try:
            data = json.loads(cls.get_current_log().read_bytes())
        except json.JSONDecodeError as exc:
            if cls.get_current_log().stat().st_size:
                raise
            logger.debug("%s | Error encountered reading file %s: %r", cls.__name__, cls.get_current_log(), exc)
            data = {}  # file was empty
        else:
            logger.debug("%s | Read from %s", cls.__name__, cls.get_current_log())
        return data

    @classmethod
    def write(cls, value: dict) -> None:
        try:
            data = cls.read()
        except json.JSONDecodeError:
            data = {}
            file = cls.get_current_log().with_suffix(".new.json")
            file.touch()
            cls.all_files.append(file)
        else:
            file = cls.get_current_log()
        np_config.merge(data, value)
        file.write_text(json.dumps(data, indent=4, sort_keys=False, default=str))
        logger.debug("%s wrote to %s", cls.__name__, file)

    @classmethod
    def validate(cls) -> None:
        if not (log := cls.read()):
            cls.exc = TestError(
                f"{cls.__name__} failed to validate because log is empty: {cls.get_current_log()}"
            )
            logger.error(
                "%s failed to validate: log is empty %s",
                cls.__name__,
                cls.get_current_log(),
                exc_info=cls.exc,
            )
        logger.debug("%s validated", __class__.__name__)


class YamlRecorder(JsonRecorder):
    @classmethod
    def test(cls) -> None:
        logger.debug("%s testing", __class__.__name__)
        super().test()
        try:
            import yaml
        except ImportError as exc:
            raise TestError(f"{__class__.__name__} failed to import yaml") from exc

    @classmethod
    def finalize(cls) -> None:
        logger.debug("Finalizing %s", __class__.__name__)
        log = json.load(cls.get_current_log().read_bytes())
        with contextlib.suppress(
            AttributeError, OSError
        ):  # if this fails we still have the json file
            yaml.dump(log, cls.get_current_log().with_suffix(".yaml").read_bytes())


class NewScaleCoordinateRecorder(JsonRecorder):
    "Gets current manipulator coordinates and stores them in a file with a timestamp."

    host: ClassVar[str] = np_config.Rig().Mon
    data_root: ClassVar[pathlib.Path] = CONFIG['NewScaleCoordinateRecorder']['data']
    data_name: ClassVar[str] = CONFIG['NewScaleCoordinateRecorder']['data_name']
    data_fieldnames: ClassVar[Sequence[str]] = CONFIG['NewScaleCoordinateRecorder']['data_fieldnames']
    data_files: ClassVar[list[pathlib.Path]] = [] 
    "Files to be copied after exp"
    
    max_z_travel: ClassVar[int] = CONFIG['NewScaleCoordinateRecorder']['max_z_travel']
    num_probes: ClassVar[int] = 6
    log_name: ClassVar[str] = "newscale_coords_{}.json"
    log_root: ClassVar[pathlib.Path] = pathlib.Path(tempfile.gettempdir()).resolve()
    label: ClassVar[str] = ""
    "A label to tag each entry with"
    latest_start: ClassVar[int] = 0
    "`time.time()` when the service was last started via `start()`."
    log_time_fmt: str = CONFIG['NewScaleCoordinateRecorder']['log_time_fmt']
    
    @classmethod
    def pretest(cls) -> None:
        cls.label = 'pretest'
        super().pretest()
            
    @classmethod
    def get_current_data(cls) -> pathlib.Path:
        cls.ensure_config()
        return cls.data_root / cls.data_name

    @classmethod
    def last_logged_coords_csv(cls) -> dict[str, float]:
        "Get the most recent coordinates from the log file using the csv parser in the stdlib."
        with cls.get_current_data().open("r") as _:
            reader = csv.DictReader(_, fieldnames=cls.data_fieldnames)
            rows = list(reader)
        last_moved_label = cls.data_fieldnames[0]
        coords = {}
        for row in reversed(rows):  # search for the most recent coordinates
            if len(coords.keys()) == cls.num_probes:
                break  # we have an entry for each probe
            if (m := row.pop(cls.data_fieldnames[1]).strip()) not in coords:
                coords[m] = {}
                for k, v in row.items():
                    if "virtual" in k:
                        continue
                    if k == last_moved_label:
                        v = datetime.datetime.strptime(v, cls.log_time_fmt)
                    else:
                        v = v.strip()
                        with contextlib.suppress(ValueError):
                            v = float(v)
                    coords[m].update({k: v})
        return coords

    @classmethod
    def last_logged_coords_pd(cls) -> dict[str, float]:
        "Get the most recent coordinates from the log file using pandas."
        coords = {}
        manipulator_label = cls.data_fieldnames[1]
        last_moved_label = cls.data_fieldnames[0]
        df = pd.read_csv(cls.get_current_data(), names=cls.data_fieldnames, parse_dates=[last_moved_label])
        # group by manipulator_label and get the maximum value in last_moved_label for each group 
        # (i.e. the most recent entry for each manipulator)
        last_moved = df.loc[
                df.groupby(manipulator_label)[last_moved_label].idxmax()
            ].set_index(manipulator_label).sort_values(last_moved_label, ascending=False)
        for serial_number, row in last_moved.iloc[:cls.num_probes].iterrows(): 
            new = {key: row[key] for key in cls.data_fieldnames if (key != manipulator_label and 'virtual' not in key)}
            new[last_moved_label] = row[last_moved_label].to_pydatetime()
            coords[str(serial_number).strip()] = new
        return coords

    @classmethod
    def convert_serial_numbers_to_probe_labels(cls, coords: dict[str, float]) -> None:
        for k, v in CONFIG[cls.__name__].get("probe_to_serial_number", {}).items():
            if v in coords:
                coords[k] = coords.pop(v)
                coords[k]['serial_number'] = v
                
    @classmethod
    def get_coordinates(cls) -> dict[str, float]:
        try:
            import pandas as pd
        except ImportError:
            coords = cls.last_logged_coords_csv()
        else:
            coords = cls.last_logged_coords_pd()
            
        def adjust_z_travel(coords):
            for v in coords.values():
                if 'z' in v:
                    v['z'] = cls.max_z_travel - v['z']
        adjust_z_travel(coords)
        cls.convert_serial_numbers_to_probe_labels(coords)
        coords["label"] = cls.label
        logger.debug("%s | Retrieved coordinates: %s", cls.__name__, coords)
        return coords

    @classmethod
    def write_to_platform_json(cls):
        coords = cls.get_coordinates()
        for k, v in coords.items():
            if isinstance(v, Mapping) and (last_moved := v.get('last_moved')):
                del coords[k]['last_moved']
                del coords[k]['serial_number']
                continue
                # if last_moved is kept, then normalize it depending on csv/pd method:
                match last_moved:
                    case str():
                        timestamp = datetime.datetime.strptime(last_moved, cls.log_time_fmt)
                    case datetime.datetime():
                        timestamp = last_moved
                coords[k]['last_moved'] = np_config.normalize_time(timestamp)
            
        # rearrange so `label`` is top-level key, or use capture-timestamp if no label
        platform_json = np_session.PlatformJson(cls.get_current_log())
        platform_json_entry = copy.deepcopy(platform_json.manipulator_coordinates)
        coords = {str(coords.pop('label', np_config.normalize_time(cls.latest_start))): coords}
        logger.debug("%s | Adding to platform json: %s", cls.__name__, coords)
        platform_json.manipulator_coordinates = np_config.merge(platform_json_entry, coords)
        if (csv := cls.get_current_data()) not in cls.data_files:
            cls.data_files.append(csv)
            
    @classmethod
    def start(cls):
        cls.latest_start = time.time()
        if 'platformD1' in cls.log_name:
            cls.write_to_platform_json()
        else: 
            cls.write({str(datetime.datetime.now()): cls.get_coordinates()})
                
    @classmethod
    def test(cls) -> None:
        super().test()
        logger.debug("%s | Testing", __class__.__name__)
        try:
            _ = cls.get_current_data().open("r")
        except OSError as exc:
            raise TestError(
                f"{cls.__name__} failed to open {cls.get_current_data()}"
            ) from exc
        try:
            _ = cls.get_coordinates()
        except Exception as exc:
            raise TestError(f"{cls.__name__} failed to get coordinates") from exc
        else:
            logger.info("%s | Test passed", cls.__name__)

    @classmethod
    def ensure_config(cls) -> None:
        super().ensure_config()

        if CONFIG.get("services", {}):
            config = CONFIG["services"].get(__class__.__name__, {})
            config.update(**CONFIG["services"].get(cls.__name__, {}))
        else:
            config = CONFIG.get(
                __class__.__name__, {}
            )  # class where this function is defined
            config.update(
                **CONFIG.get(cls.__name__, {})
            )  # the calling class, if different

        if not hasattr(cls, "host"):
            cls.host = config["host"]

        # for resulting data
        if (
            not hasattr(cls, "data_root")
            or cls.host not in pathlib.Path(cls.data_root).parts
        ):
            relative_path = config["data"]
            if relative_path:
                root = pathlib.Path(f"//{cls.host}/{relative_path}")
                try:
                    _ = root.exists()
                except OSError as exc:
                    cls.exc = exc
                    logger.exception(
                        "Error accessing %s data path: %s", cls.__name__, root
                    )
                    raise FileNotFoundError(
                        f"{cls.__name__} data path is not accessible: {root}"
                    ) from exc
                else:
                    cls.data_root = root

        if not hasattr(cls, "data_name"):
            cls.data_name = config["data_name"]
        if not hasattr(cls, "data_fieldnames"):
            cls.data_fieldnames = config["data_fieldnames"]


