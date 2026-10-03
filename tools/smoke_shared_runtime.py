"""Exercise two real file-camera pipelines without touching customer data."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from camera_service.camera_manager import CameraManager
from camera_service.camera.supervisor import CameraSupervisor


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',default=str(ROOT/'yolo26n.pt'))
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='camera-eye-runtime-') as directory:
        home=Path(directory)
        manager=CameraManager(str(home/'test.db'))
        for index in (1,2):
            video=home/f'camera-{index}.mp4';shutil.copyfile(ROOT/'test.mp4',video)
            manager.create_camera({'camera_id':f'cam{index}','name':f'Camera {index}',
                'source_type':'file','rtsp_url':str(video),'tracking_mode':'track','features':{}})
        calls={}
        def pipeline(camera,stop,publish):
            calls[camera.camera_id]=calls.get(camera.camera_id,0)+1
            return manager.iter_tracking_mjpeg(camera,args.model,stop_event=stop,publish_callback=publish)
        supervisor=CameraSupervisor(None,None,None,None,manager,pipeline=pipeline)
        supervisor.start()
        try:
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                statuses=[manager.get_camera_status(f'cam{i}') for i in (1,2)]
                if all(s and s.online and s.ai_fps>0 for s in statuses):
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('Two real camera pipelines did not produce inference in time')
            for index in (1,2):
                view=supervisor.stream(f'cam{index}',True)
                if b'Content-Type: image/jpeg' not in next(view):
                    raise RuntimeError('Shared live frame unavailable')
                view.close()
            time.sleep(2)
            report={'passed':True,'pipeline_starts':calls,'cameras':{f'cam{i}':manager.get_camera_status(f'cam{i}').model_dump() for i in (1,2)}}
            if calls != {'cam1':1,'cam2':1}:
                raise RuntimeError('Viewer created/restarted camera pipeline')
            print(json.dumps(report,indent=2))
        finally:
            supervisor.shutdown()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
