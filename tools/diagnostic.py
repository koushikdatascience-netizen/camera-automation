import argparse,cv2,time
p=argparse.ArgumentParser(); p.add_argument('source'); a=p.parse_args(); cap=cv2.VideoCapture(a.source); n=0; t=time.time()
while n<100:
 ok,f=cap.read()
 if not ok: break
 n+=1
print({'opened':cap.isOpened(),'frames':n,'fps_observed':n/max(time.time()-t,1e-6),'width':int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),'height':int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))})
cap.release()
