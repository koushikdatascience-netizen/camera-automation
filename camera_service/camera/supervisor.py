import threading
from camera_service.camera.worker import CameraWorker
class CameraSupervisor:
    def __init__(self,config,store,face,attendance): self.config=config; self.store=store; self.face=face; self.attendance=attendance; self.workers={}; self.threads={}
    def start(self):
        for cam in self.config.cameras:
            if not cam.enabled: continue
            w=CameraWorker(self.config,cam,self.store,self.face,self.attendance); t=threading.Thread(target=w.run,name=f'camera-{cam.camera_id}',daemon=True); self.workers[cam.camera_id]=w; self.threads[cam.camera_id]=t; t.start()
    def shutdown(self):
        for w in self.workers.values(): w.stop()
        for t in self.threads.values(): t.join(timeout=3)
    def is_running(self): return any(t.is_alive() for t in self.threads.values()) if self.threads else True
