import pathlib
import time
import typing
from typing import Any, Optional, Protocol, Union


class TestError(AssertionError):
    ...


# verb-based protocols define functions called at specific points in an experiment workflow
# so that their order is fixed: initialize() | start() | verify() | stop() | finalize()


@typing.runtime_checkable
class Initializable(Protocol):
    "Supports `initialize()`: runs setup or configuration to effectively reset the service for fresh use."

    def initialize(self) -> None:
        ...


@typing.runtime_checkable
class Configurable(Initializable, Protocol):
    "Supports `config()`: ensures all required parameters are set before use. Called in `initialize()`."

    def configure(self) -> None:
        ...

    # def ensure_config(self) -> None: ...


@typing.runtime_checkable
class Testable(Protocol):
    "Supports `test()`: without creating new data, quickly confirms readiness for use, or raises `TestError`. Always called before first use. See `PreTestable` for comprehensive test."

    def test(self) -> None:
        ...


@typing.runtime_checkable
class Pretestable(Protocol):
    "Supports `pretest()`: comprehensively tests service functionality and code by calling every class method critical for use. Should be expected to fail."

    def pretest(self) -> None:
        ...


@typing.runtime_checkable
class Startable(Protocol):
    "Supports `start()`, `latest_start`"

    def start(self) -> None:
        "Starts stimulus/recording and records current time in `latest_start`."
        self.latest_start = time.time()
        
    # def is_ready_to_start(self) -> bool: 
    #     "The body of start() will not execute unless `is_ready_to_start()` returns `True`"
    
    # def is_started(self) -> bool:
    #     "Prevents service from being re-started once started"
    
    latest_start: float
    "Store `time.time()` in each `start()` so we can find files created afterward."


@typing.runtime_checkable
class Primeable(Startable, Protocol):  # ? PreStartable
    "Supports `prime()`: makes ready for imminent `start()` by re-arming, running checks, etc. Called before `start()`."

    def prime(self) -> None:
        ...

    # ? auto-run at beginning of `start()`?


@typing.runtime_checkable
class Verifiable(Startable, Protocol):  # ? PostStartable
    "Supports `verify()`: asserts service has started, e.g. stimulus is running, data file is increasing in size etc., or raises `AssertionError`. Called after `start()` and checking `self.is_started()`."

    def verify(self) -> None:
        ...

    # ? auto-run at end of `start()`?


@typing.runtime_checkable
class Stoppable(Protocol):
    "Supports `stop()`: stops or pauses stimulus/recording. Called after `start()`."

    def stop(self) -> None:
        ...


@typing.runtime_checkable
class Finalizable(Protocol):
    "Supports `finalize()`: handle results of most-recent `start()` or `stop()`. Cleanup, file management etc."

    def finalize(self) -> None:
        ...

    # ? if multiple start-stop loops: finalize altogether or individually?
    # ? auto-run `finalize()` at end of `stop()`?


@typing.runtime_checkable
class Validatable(Protocol):
    "Supports `validate()`: asserts most-recent data are valid, or raises `AssertionError`.  Called after checking `self.is_started() is not True`."

    def validate(self, data: Optional[pathlib.Path] = None) -> None:
        ...


@typing.runtime_checkable
class Shutdownable(Protocol):
    "Supports `shutdown()`: gracefully closes service. Called after `finalize()`."

    def shutdown(self) -> None:
        ...


PreExperimentProtocols = Union[
    Initializable, Testable, Pretestable, Primeable, Startable
]
PostExperimentProtocols = Union[Stoppable, Finalizable, Validatable, Shutdownable]
Service = Union[PreExperimentProtocols, PostExperimentProtocols]

# special methods - should be the only :


@typing.runtime_checkable
class Gettable(Protocol):
    def get(self, property: str) -> Any:
        return getattr(self, property)


@typing.runtime_checkable
class Settable(Protocol):
    def set(self, property: str, value: Any) -> Any:
        if not hasattr(self, property):
            raise AttributeError(f"Service {self} has no property {property}")
        setattr(self, property, value)


# noun-based protocols are for more-specific functions/properties than verb-based protocols
# and may be checked at any time.. for example 'Recorder' captures data and therefore
# has a data path in the filesystem


@typing.runtime_checkable
class Recorder(Startable, Protocol):
    data_root: pathlib.Path
    raw_suffix: str  # include leading dot


if __name__ == "__main__":

    class Test_1:
        _is_started: bool

        def initialize(self):
            self._is_started = False
            print(__class__, "initialized")

        def is_started(self):
            return self._is_started

        def is_ready_to_start(self):
            return not self.is_started()

        def start():
            last_started = time.time()
            started = True
            print(__class__, "started")

    class Test_2:
        def start():
            print(__class__, "started")

    services = [Test_1, Test_2]

    def initialize_all():
        for service in services:
            if isinstance(service, Initializable):
                service.initialize()
