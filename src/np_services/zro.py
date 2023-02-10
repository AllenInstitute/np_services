# -*- coding: utf-8 -*-
"""
proxy.py

@author: derricw

Proxy device and manager for ZRO devices.

`Proxy` is a remote object proxy designed to interact with objects
extending `BasePubRepDevice` (from device.py).

"""
import np_logging
import zmq

logger = np_logging.getLogger(__name__)


def get_address(ip="", port=None):
    """
    Trys to get a properly formatted address given a port and ip.

    ZMQ likes address to be in this format:

        {protocol}://{ip}:{port}

    Args:
        ip (Optional[str]): ip address with some semblance of correct formatting
        port (Optional[int]): port to use in the event that the port is not
            included in the IP

    Returns:
        str: a properly formatted ip str.

    """
    if not ip and not port:
        raise ValueError("Need a port or IP.")
    elif not ip and port:
        return "tcp://*:{}".format(port)
    else:
        if ip[:6] != "tcp://":
            ip = "tcp://" + ip
        if len(ip.split(":")) == 2:
            return "{}:{}".format(ip, port)
        else:
            return ip


class DeviceProxy(object):
    """
    Proxy object for a BasePubRepDevice.

    args:
        ip (Optional[str]): IP/DNS address of actual device. Defaults to
            "localhost".
        port (Optional[int]): REP port for actual device, if it isn't include
            in IP.
        timeout (Optional[float]): Timeout in seconds for all commands.
            Defaults to 10.
        serialization (Optional[str]): Serialization method.  "pickle" (default)
            or "json".

    Example:
        >>> dev = DeviceProxy("localhost:5556")
        >>> dev.call_method_on_device(some_argument)
        >>> dev.attr_on_device = 5

    """

    _context = zmq.Context()
    _context.setsockopt(zmq.LINGER, 1)

    def __init__(
        self, ip="localhost", port=None, timeout=10.0, serialization="pickle",
    ):
        super().__init__()
        self.__dict__["ip"] = ip
        self.__dict__["rep_port"] = port
        self.__dict__["timeout"] = timeout
        self.__dict__["serialization"] = serialization.lower()

        self._setup_socket()
        # self._setup_getset()

    def __setattr__(self, name, value):
        """
        Overwrite __setattr__ so that attributes are set on target object
            instead of this object.
        """
        packet = {"command": "set", "args": (name, value)}
        self._send_packet(packet)
        response = self.__dict__["recv"]()
        if response == "0":
            return None
        else:
            raise ZroError(message=str(response))

    def __getattr__(self, name):
        """
        Overwrite __getattr__ so that attributes are grabbed from target object
            instead of this object.
        """
        packet = {"command": "get", "args": (name,)}
        self._send_packet(packet)
        response = self.__dict__["recv"]()
        if isinstance(response, ZroError):
            raise ZroError(message=str(response))
        elif response in ("callable", "__callable__"):
            self.__dict__["to_call"] = name  # HOLD ON TO YOUR BUTTS
            return self._call
        else:
            return response

    def __dir__(self):
        """
        Overwrite __dir__ so that attributes and methods come from target
            object.
        """
        self.__dict__["to_call"] = "get_attribute_list"
        attrs = self._call()
        self.__dict__["to_call"] = "get_command_list"
        methods = self._call()
        return attrs + methods

    def _send_packet(self, packet):
        """
        Sends a packet.  Attempts to reconnect once if there is a failure.

        #TODO: Make packet a class.
        """
        try:
            self.__dict__["send"](packet)
        except zmq.ZMQError:
            self.__dict__["req_socket"].close()
            self._setup_socket()
            self.__dict__["send"](packet)

    def _setup_socket(self):
        """
        Sets up the request socket.
        """
        ip = self.__dict__["ip"]
        rep_port = self.__dict__["rep_port"]
        addr_str = get_address(ip, rep_port)
        timeout = self.__dict__["timeout"]
        self.__dict__["req_socket"] = self._context.socket(zmq.REQ)
        self.__dict__["req_socket"].setsockopt(zmq.SNDTIMEO, int(timeout * 1000))
        self.__dict__["req_socket"].setsockopt(zmq.RCVTIMEO, int(timeout * 1000))
        self.__dict__["req_socket"].connect(addr_str)

        if self.__dict__["serialization"] in ["pickle", "pkl", "p"]:
            self.__dict__["send"] = self.__dict__["req_socket"].send_pyobj
            self.__dict__["recv"] = self.__dict__["req_socket"].recv_pyobj
        elif self.__dict__["serialization"] in ["json", "j"]:
            self.__dict__["send"] = self.__dict__["req_socket"].send_json
            self.__dict__["recv"] = self.__dict__["req_socket"].recv_json
        else:
            raise ValueError("Incorrect serialization type. Try 'pickle' or 'json'.")

    def _call(self, *args, **kwargs):
        """
        Used for calling arbitrary methods in the device.
        """
        packet = {
            "command": "run",
            "callable": self.to_call,
            "args": args,
            "kwargs": kwargs,
        }
        self.__dict__["send"](packet)
        response = self.__dict__["recv"]()
        if isinstance(response, dict) and response.get("ZroError", False):
            response = ZroError.from_dict(response)
        if isinstance(response, ZroError):
            raise ZroError(message=str(response))
        return response

    def __del__(self):
        """
        Close the socket on cleanup.
        """
        self.__dict__["req_socket"].close()


Proxy = DeviceProxy


class ZroError(Exception):
    """Base class for zro errors."""

    error_codes = {
        1: "{} -> HAS_NO_ATTRIBUTE -> {}",
        2: "{} -> HAS_NO_CALLABLE -> {}",
        3: "{} -> ATTRIBUTE_NOT_CALLABLE -> {}",
        4: "{} -> CALLABLE_FAILED -> {}",
        5: "{} -> ARGUMENTS_INVALID -> {}",
        6: "{} -> UNHANDLED_ERROR -> {}",
        7: "{} -> ASYNC_RESULT_INVALID_HANDLE -> {}",
        8: "{} -> ASYNC_RESULT_UNFINISHED -> {}",
        9: "{} -> ASYNC_CALLBACK_FAILED -> {}",
    }

    HAS_NO_ATTRIBUTE = 1
    HAS_NO_CALLABLE = 2
    ATTRIBUTE_NOT_CALLABLE = 3
    CALLABLE_FAILED = 4
    ARGUMENTS_INVALID = 5
    UNHANDLED_ERROR = 6
    ASYNC_RESULT_INVALID_HANDLE = 7
    ASYNC_RESULT_UNFINISHED = 8
    ASYNC_CALLBACK_FAILED = 9

    def __init__(self, obj=None, target=None, error_code=6, message=""):
        if not message:
            message = self.error_codes[error_code].format(obj, target)
        self.message = message
        self.error_code = error_code
        super(ZroError, self).__init__(message)

    def to_JSON(self):
        return {
            "ZroError": str(
                type(self.get_specific_error())
            ),  # this key lets zro convert this on the receive side
            "error_code": self.error_code,
            "message": str(self.message),
        }

    @staticmethod
    def from_dict(d):
        return ZroError(
            error_code=d["error_code"], message=d["message"]
        ).get_specific_error()

    def get_specific_error(self, to_raise=False):
        """Get the appropriate ZroError for the error type."""
        err = _SPECIFIC_ERRORS[self.error_code](message=self.message)
        if to_raise:
            raise err
        return err


class ZroNoAttributeError(ZroError):
    """Error for HAS_NO_ATTRIBUTE."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroNoAttributeError, self).__init__(
            obj, target, ZroError.HAS_NO_ATTRIBUTE, message
        )


class ZroNoCallableError(ZroError):
    """Error for HAS_NO_CALLABLE."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroNoCallableError, self).__init__(
            obj, target, ZroError.HAS_NO_CALLABLE, message
        )


class ZroAttrNotCallableError(ZroError):
    """Error for ATTRIBUTE_NOT_CALLABLE."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroAttrNotCallableError, self).__init__(
            obj, target, ZroError.ATTRIBUTE_NOT_CALLABLE, message
        )


class ZroCallableFailedError(ZroError):
    """Error for CALLABLE_FAILED."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroCallableFailedError, self).__init__(
            obj, target, ZroError.CALLABLE_FAILED, message
        )


class ZroArgumentsInvalidError(ZroError):
    """Error for ARGUMENTS_INVALID."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroArgumentsInvalidError, self).__init__(
            obj, target, ZroError.ARGUMENTS_INVALID, message
        )


class ZroAsyncHandleInvalidError(ZroError):
    """Error for ASYNC_RESULT_INVALID_HANDLE."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroAsyncHandleInvalidError, self).__init__(
            obj, target, ZroError.ASYNC_RESULT_INVALID_HANDLE, message
        )


class ZroResultUnfinishedError(ZroError):
    """Error for ASYNC_RESULT_UNFINISHED."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroResultUnfinishedError, self).__init__(
            obj, target, ZroError.ASYNC_RESULT_UNFINISHED, message
        )


class ZroCallbackFailedError(ZroError):
    """Error for ASYNC_CALLBACK_FAILED."""

    def __init__(self, obj=None, target=None, message=""):
        super(ZroCallbackFailedError, self).__init__(
            obj, target, ZroError.ASYNC_CALLBACK_FAILED, message
        )


_SPECIFIC_ERRORS = {
    1: ZroNoAttributeError,
    2: ZroNoCallableError,
    3: ZroAttrNotCallableError,
    4: ZroCallableFailedError,
    5: ZroArgumentsInvalidError,
    6: ZroError,
    7: ZroAsyncHandleInvalidError,
    8: ZroResultUnfinishedError,
    9: ZroCallbackFailedError,
}
