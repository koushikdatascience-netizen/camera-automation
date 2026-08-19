import cv2
class VideoSource:
    def __init__(self, source, source_type='file'):
        self.source=int(source) if source_type=='webcam' and str(source).isdigit() else source; self.cap=None
    def open(self): self.cap=cv2.VideoCapture(self.source); return bool(self.cap.isOpened())
    def read(self): return self.cap.read() if self.cap else (False,None)
    def close(self):
        if self.cap: self.cap.release(); self.cap=None
