import os
import pathlib
import tempfile

os.environ["USE_TEST_RIG"] = "0"
os.environ["AIBS_RIG_ID"] = "NP.0"

import np_logging

from np_services import *

logger = np_logging.getLogger()

with debug_logging():
    if True:
        NewScaleCoordinateRecorder.log_root = pathlib.Path(tempfile.mkdtemp())
        NewScaleCoordinateRecorder.pretest()

    if False:
        with stop_on_error(ImageMVR, reraise=False):
            ImageMVR.pretest()

    if False:
        with stop_on_error(VideoMVR, reraise=False):
            VideoMVR.pretest()

    if False:
        OpenEphys.folder = "test"
        with stop_on_error(OpenEphys, reraise=False):
            OpenEphys.pretest()

    if False:
        with stop_on_error(Sync, reraise=False):
            Sync.pretest()

    if False:
        # NoCamstim.password  # careful not to commit this to github!
        NoCamstim.data_root = pathlib.Path("C:/ProgramData/camstim/output")
        NoCamstim.remote_file = pathlib.Path(
            "C:/Users/svc_neuropix/Desktop/run_blue_opto.bat"
        )
        NoCamstim.initialize()  # will prompt for password if not entered
        NoCamstim.start()

    if False:
        ScriptCamstim.data_root = pathlib.Path("C:/ProgramData/camstim/output")
        ScriptCamstim.script = (
            "C:/Users/svc_neuropix/Desktop/optotagging/optotagging_sro.py"
        )
        ScriptCamstim.params = (
            "C:/Users/svc_neuropix/Desktop/optotagging/experiment_params_blue.json"
        )
        ScriptCamstim.initialize()
        ScriptCamstim.start()

    if False:
        SessionCamstim.initialize()
        SessionCamstim.lims_user_id = "ben.hardcastle"
        SessionCamstim.labtracks_mouse_id = 598796
        SessionCamstim.start()

    quit()

    ImageMVR.host = VideoMVR.host = NewScaleCoordinateRecorder.host = "w10dtsm18280"
    NewScaleCoordinateRecorder.initialize()
    NewScaleCoordinateRecorder.start()
    NewScaleCoordinateRecorder.start()
    # VideoMVR.initialize()
    # VideoMVR.test()
    # with stop_on_error(VideoMVR):
    #     VideoMVR.start()
    #     time.sleep(VideoMVR.pretest_duration_sec)
    #     VideoMVR.verify()
    # VideoMVR.finalize()

    Camstim.get_proxy().session_output_path
    Camstim.get_proxy().start_session(mouse_id, user_id)
    Camstim.get_proxy().status["running"]
