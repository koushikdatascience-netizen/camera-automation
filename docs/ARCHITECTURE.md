# Architecture
RTSP/file -> Ultralytics YOLO+ByteTrack -> person track -> face ROI -> InsightFace -> identity consensus -> line crossing -> AttendanceEngine. Unknown tracks create deduplicated incidents and evidence snapshots. Lost tracks never close attendance.
