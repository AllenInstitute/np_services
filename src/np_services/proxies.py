"""
Proxy classes for interacting with devices via zro/zmq.

Proxy class names must match the name of the proxy key in the config dict.
"""
import abc
import contextlib
import csv
import datetime
import functools
import json  # loading config from Sync proxy will instantiate datetime objects
import logging
import pathlib
import re
import time
from typing import Any, ClassVar, Literal, Mapping, Optional, Sequence

import fabric
import np_config
import np_logging
import np_session
import pydantic
import yaml

import np_services.mvr as mvr
import np_services.utils as utils
import np_services.zro as zro
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

    latest_start: ClassVar[float | int]
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
        cls.initialization = time.time()
        logger.info("%s initialized: ready for use", cls.__name__)

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
        logger.debug("%s tested successfully", cls.__name__)

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
            return [
                max(
                    utils.get_files_created_between(
                        cls.data_root / subfolders, glob
                    ),
                    key=lambda x: x.stat().st_mtime,
                )
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
        logger.debug("Starting %s", cls.__name__)
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
        with utils.debug_logging():
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

    # --- End of possible Camstim/Sync shared methods ---

    # --- Sync-specific methods ---


class Sync(CamstimSyncShared):
    host = np_config.Rig().Sync
    started_state = ("BUSY", "RECORDING")
    raw_suffix: ClassVar[int | float] = ".sync"
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
        logger.debug("%s latest data file is increasing in size", cls.__name__)

    @classmethod
    def full_validation(cls, data: pathlib.Path) -> None:
        line_labels: dict = cls.get_config()["line_labels"]
        # TODO
        pass

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
        if hasattr(cls, "data_root") and cls.data_root is not None:
            cls.data_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def finalize(cls) -> None:
        logger.info("Finalizing %s", cls.__name__)
        if cls.is_started():
            cls.stop()
        while not cls.is_ready_to_start():
            logger.debug("Waiting for %s to finish processing", cls.__name__)
            time.sleep(1)  # TODO add backoff module
        if not cls.data_files:
            cls.data_files = []
        cls.data_files.extend(new := cls.get_latest_data("*.pkl"))
        logger.info("%s added new data: %s", cls.__name__, [_.name for _ in new])


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

    @classmethod
    def start(cls):
        cls.latest_start = time.time()
        cls.get_proxy().start_session(
            cls.labtracks_mouse_id, cls.lims_user_id
        )  # , cls.params)

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

    # @classmethod
    # def pretest(cls) -> None:
    #     cls.remote_file =
    #     super().pretest()

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
        logger.debug(f"{cls.__name__} | Pretest")
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
    data_files: ClassVar[list[pathlib.Path]]
    
    @classmethod
    def is_started(cls) -> bool:
        return cls.get_state() == cls.started_state
    
    @classmethod
    def is_ready_to_start(cls) -> bool:
       if cls.is_started():
           return False
       time.sleep(1)
       if 'CAMERAS_CLOSED' in cls.get_state():
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
        time.sleep(1)
        cls.get_proxy().enable_cameras()
        time.sleep(1)
        cls.get_proxy().stop_capture()
        time.sleep(1)
        cls.get_proxy().start_capture()
        time.sleep(1)
        
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
        cls.reenable_cameras()
        time.sleep(1)
        # _ = cls.get_proxy().get_left_image()
        cls.get_proxy().save_left_image(str(left))
        time.sleep(1)
        # logger.info(_)
        cls.reenable_cameras()
        time.sleep(1)
        # _ = cls.get_proxy().get_right_image()
        # logger.info(_)
        cls.get_proxy().save_right_image(str(right))
        time.sleep(1)
        for path, side in zip((left, right), ('Left', 'Right')):
            if path.exists():
                logger.debug(f"{cls.__name__} | {side} image saved to {path}")
            else:
                logger.debug(f"{cls.__name__} | {side} image capture request sent, but image not saved")
    
    @classmethod
    def finalize(cls) -> None:
        logger.debug(f"{cls.__name__} | Finalizing")
        while (
            cls.is_started()
            or not (latest := cls.get_latest_data('*'))
        ):
            time.sleep(1)
            cls.reenable_cameras()
        cls.data_files.extend(latest)
        logger.debug(f"{cls.__name__} | Images captured: {latest}")
        
    @classmethod
    def validate(cls):
        if not (latest := cls.get_latest_data('*')) or len(latest) != 2:
            raise AssertionError(f"{cls.__name__} | Expected 2 images, got {len(latest)}: {latest}")

    @classmethod
    def pretest(cls):
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
    port: ClassVar[int]

    re_aux: re.Pattern = re.compile("aux|USB!", re.IGNORECASE)

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
    def get_proxy(cls) -> mvr.MVRConnector:
        with contextlib.suppress(AttributeError):
            return cls.proxy
        cls.ensure_config()
        logger.debug("Creating %s proxy to %s:%s", cls.__name__, cls.host, cls.port)
        cls.proxy = mvr.MVRConnector({"host": cls.host, "port": cls.port})
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
    gb_per_hr: ClassVar[int | float]
    min_rec_hr: ClassVar[int | float]
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
    pretest_duration_sec: ClassVar[int | float]
    gb_per_hr: ClassVar[int | float]
    min_rec_hr: ClassVar[int | float]

    raw_suffix: ClassVar[str] = ".mp4"

    started_state = ("BUSY", "RECORDING")

    @classmethod
    def get_cameras(cls) -> list[dict[str, str]]:
        "All available cams except Aux"
        cams = super().get_cameras()
        return [_ for _ in cams if cls.re_aux.search(_["label"]) is None]

    @classmethod
    def start(cls) -> None:
        cls.latest_start = time.time()
        cls.get_proxy().start_record(record_time=365 * 60,)  # sec

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
        logger.debug(
            "%s verified: %s cameras recording to disk", cls.__name__, len(files)
        )

    @classmethod
    def stop(cls) -> None:
        cls.get_proxy().stop_record()

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
        logger.warning("%s.validate() not implemented", cls.__name__)


class JsonRecorder:
    "Just needs a `start` method that calls `write()`."

    log_name: ClassVar[str]
    log_root: ClassVar[pathlib.Path]

    @classmethod
    def pretest(cls) -> None:
        cls.initialize()
        cls.start()
        cls.validate()

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
        log.touch()
        cls.all_files = [log]
        cls.test()

    @classmethod
    def test(cls) -> None:
        logger.debug("%s testing", __class__.__name__)
        try:
            _ = cls.get_current_log().open("w")
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
        except json.JSONDecodeError:
            if cls.get_current_log().stat().st_size:
                raise
            data = {}  # file was empty
        else:
            logger.debug("%s read from %s", cls.__name__, cls.get_current_log())
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
        data.update(value)
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
        with utils.suppress(
            AttributeError, OSError
        ):  # if this fails we still have the json file
            yaml.dump(log, cls.get_current_log().with_suffix(".yaml").read_bytes())


class PlatformJsonWriter(pydantic.BaseModel):
    """Writes D1 platform json for lims upload. Just requires a path (dir or dir+filename)."""

    # ------------------------------------------------------------------------------------- #
    # required kwargs on init: 
    
    path: pathlib.Path
    "Typically the storage directory for the session. Will be modified on assignment."
    
    # ------------------------------------------------------------------------------------- #
    
    class Config:
        validate_assignment = True # coerce types on assignment
        extra = 'forbid' # properties must be defined in the model
        fields = {'path': {'exclude': True}}
        
    suffix: ClassVar[str] = "_platformD1.json"
    
    def load_from_existing(self) -> None:
        "Reads existing file and loads all non-empty fields to self."
        if existing := json.loads(self.path.read_text() or "{}"):
            for k, v in existing.items():
                if v and v != getattr(self, k):
                    setattr(self, k, v)
        
    def write(self, update_existing=True): 
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        if update_existing:
            self.load_from_existing()
        self.path.write_text(self.json(indent=4))
        logger.debug("%s wrote to %s", self.__class__.__name__, self.path.as_posix())
    
    @pydantic.validator("path", pre=True)
    def normalize_path(cls, v: str | pathlib.Path) -> pathlib.Path:
        return np_config.normalize_path(v)
    
    @pydantic.validator("path")
    def add_filename_to_path(cls, v: pathlib.Path) -> pathlib.Path:
        name = cls.append_suffix_to_filename(v.name)
        return v / name if v.is_dir() else v.with_name(name)
    
    @classmethod
    def append_suffix_to_filename(cls, v: str) -> str:
        if not v.endswith(cls.suffix):
            v += cls.suffix
        v.replace(".json.json", ".json")
        return v

    _normalize_time = pydantic.validator(
        *(
            'workflow_start_time',
            'HeadFrameEntryTime',
            'CartridgeLowerTime',
            'ProbeInsertionStartTime',
            'ProbeInsertionCompleteTime',
            'ExperimentStartTime',
            'ExperimentCompleteTime',
            'HeadFrameExitTime',
            'manifest_creation_time',
            'workflow_complete_time',
            'platform_json_save_time',
        ),
        allow_reuse=True, pre=True)(utils.normalize_time)
    
    _foraging_id_re: ClassVar[str] = (
        r"([0-9,a-f]{8}-[0-9,a-f]{4}-[0-9,a-f]{4}-[0-9,a-f]{4}-[0-9,a-f]{12})"
    )
    
    # auto-generated / ignored ------------------------------------------------------------- #
    rig_id: str = np_config.Rig().id
    wfl_version: float = 1.0

    # pre-experiment ---------------------------------------------------------------------- #
    workflow_start_time: str = pydantic.Field(
        default_factory=lambda: utils.normalize_time(time.time())
    )
    operatorID: Optional[str] = ""
    sessionID: Optional[str | int] = ""
    mouseID: Optional[str | int] = ""
    DiINotes: dict[str, int | str] = dict(
        EndTime="", StartTime="", dii_description="", times_dipped=""
    )
    probe_A_DiI_depth: str = ""
    probe_B_DiI_depth: str = ""
    probe_C_DiI_depth: str = ""
    probe_D_DiI_depth: str = ""
    probe_E_DiI_depth: str = ""
    probe_F_DiI_depth: str = ""
    water_calibration_heights: list[float] = [0.0]
    water_calibration_volumes: list[float] = [0.0]
    mouse_weight_pre: str = ""
    mouse_weight_pre_float: float = 0.0
    HeadFrameEntryTime: str = ""
    wheel_height: str = ""
    CartridgeLowerTime: str = ""
    ProbeInsertionStartTime: str = ""
    ProbeInsertionCompleteTime: str = ""
    InsertionNotes: dict = pydantic.Field(default_factory=dict)
    ExperimentStartTime: str = ""
    stimulus_name: str = ""
    script_name: str = ""

    # post-experiment ---------------------------------------------------------------------- #
    ExperimentCompleteTime: str = ""
    ExperimentNotes: dict[str, dict[str, Any]] = dict(
        BleedingOnInsertion={}, BleedingOnRemoval={}
    )
    foraging_id: str = pydantic.Field(default="", regex=_foraging_id_re)
    foraging_id_list: list[str] = pydantic.Field(
        default_factory=lambda: [""], regex=_foraging_id_re
    )
    HeadFrameExitTime: str = ""
    mouse_weight_post: str = ""
    water_supplement: float = 0.0
    files: dict[str, dict[str, str]] = pydantic.Field(default_factory=dict)
    manifest_creation_time: str = ""
    workflow_complete_time: str = ""
    platform_json_save_time: str = ""


class NewScaleCoordinateRecorder(JsonRecorder):
    "Gets current manipulator coordinates and stores them in a file with a timestamp."

    host: ClassVar[str] = np_config.Rig().Mon
    data_root: ClassVar[pathlib.Path]
    data_name: ClassVar[str] = "log.csv"
    data_fieldnames: ClassVar[Sequence[str]] = (
        "last_moved",
        "manipulator",
        "x",
        "y",
        "z",
        "x_virtual",
        "y_virtual",
        "z_virtual",
    )

    num_probes: ClassVar[int] = 6
    max_travel: ClassVar[float]
    log_name: ClassVar[str] = "newscale_coords_{}.json"
    log_root: ClassVar[pathlib.Path] = pathlib.Path(
        "."
    ).resolve()  #! move to config after testing
    label: ClassVar[str] = ""
    "A label to tag each entry with"
    latest_start: ClassVar[int] = 0
    "`time.time()` when the service was last started via `start()`."

    @classmethod
    def get_current_data(cls) -> pathlib.Path:
        cls.ensure_config()
        return cls.data_root / cls.data_name

    @classmethod
    def get_coordinates(cls) -> dict[str, float]:
        with cls.get_current_data().open("r") as _:
            reader = csv.DictReader(_, fieldnames=cls.data_fieldnames)
            rows = list(reader)
        coords = {}
        for row in reversed(rows):  # search for the most recent coordinates
            if len(coords.keys()) == cls.num_probes:
                break  # we have an entry for each probe
            if (m := row.pop(cls.data_fieldnames[1]).strip()) not in coords:
                coords[m] = {}
                for k, v in row.items():
                    if "virtual" in k:
                        continue
                    v = v.strip()
                    with contextlib.suppress(ValueError):
                        v = float(v)
                    coords[m].update({k: v})
        coords["label"] = cls.label
        logger.debug("%s retrieved coordinates: %s", cls.__name__, coords)
        return coords

    @classmethod
    def start(cls):
        cls.latest_start = time.time()
        cls.write({str(datetime.datetime.now()): cls.get_coordinates()})

    @classmethod
    def test(cls) -> None:
        super().test()
        logger.debug("%s testing", __class__.__name__)
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
            logger.info("%s test passed", cls.__name__)

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
