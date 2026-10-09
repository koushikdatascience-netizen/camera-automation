from __future__ import annotations

import json
import threading
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from camera_service.camera.onvif import probe_onvif, select_profile


def _safe_camera_test_result(result: dict[str, Any], *, source_was_secret: bool) -> dict[str, Any]:
    """Return camera diagnostics without exposing RTSP credentials to the cloud portal."""
    safe = dict(result)
    if source_was_secret:
        safe.pop("source", None)
    elif "source" in safe:
        safe["source"] = _redact_source(str(safe["source"]))
    return safe


def _redact_source(source: str) -> str:
    if "://" not in source:
        return source
    parsed = urlsplit(source)
    host = parsed.netloc.split('@')[-1]
    return urlunsplit((parsed.scheme, f"***@{host}" if '@' in parsed.netloc else host, parsed.path, '', ''))


def _redact_result(value):
    if isinstance(value, dict):
        return {key:_redact_result(item) for key,item in value.items()}
    if isinstance(value, list):
        return [_redact_result(item) for item in value]
    return _redact_source(value) if isinstance(value,str) else value


@dataclass
class SyncRunResult:
    enabled: bool
    blocked: bool = False
    synced: int = 0
    failed: int = 0
    message: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "blocked": self.blocked,
            "synced": self.synced,
            "failed": self.failed,
            "message": self.message,
        }


class EdgeSyncWorker:
    """Best-effort local queue drain for cloud portal/mobile visibility."""

    def __init__(self, store, cloud_client, edge_config, sync_config, license_manager, camera_manager=None, camera_supervisor=None):
        self.store = store
        self.cloud_client = cloud_client
        self.edge_config = edge_config
        self.sync_config = sync_config
        self.license_manager = license_manager
        self.camera_manager = camera_manager
        self.camera_supervisor = camera_supervisor
        self._live_view_publisher = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_personnel_sync_monotonic: float | None = None
        self.last_run_at: str | None = None
        self.last_result: dict[str, Any] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="edge-sync-worker", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                self._remember(SyncRunResult(enabled=True, failed=1, message="Sync attempt failed; retrying automatically."))
            delay = min(5.0, max(2.0, float(getattr(self.sync_config, "interval_seconds", 15.0))))
            self._stop.wait(delay)

    def run_once(self) -> SyncRunResult:
        with self._lock:
            if not self.cloud_client.enabled():
                result = SyncRunResult(enabled=False, message="Cloud sync is disabled.")
                self._remember(result)
                return result

            license_status = self.license_manager.status()
            if not license_status.active:
                result = SyncRunResult(enabled=True, blocked=True, message=license_status.reason)
                self._remember(result, {"license": license_status.model_dump()})
                return result
            if hasattr(license_status,'allows_feature') and not license_status.allows_feature('cloud_sync'):
                result = SyncRunResult(enabled=True,blocked=True,message='Cloud sync is not enabled for this license.')
                self._remember(result)
                return result

            synced = 0
            failed = 0
            heartbeat_sync = {"sent": False}
            try:
                heartbeat = self.cloud_client.heartbeat(self.edge_config, self._edge_status_payload(license_status))
                heartbeat_sync = {"sent": True, "received_at": heartbeat.get("received_at")}
            except Exception as exc:
                heartbeat_sync = {"sent": False, "error": str(exc)}
            command_sync = {"fetched": 0, "completed": 0, "failed": 0}
            try:
                commands = self.cloud_client.edge_commands()
                command_sync["fetched"] = len(commands)
                for command in commands:
                    try:
                        result = self._execute_command(command)
                        camera_test_failed = (
                            str(command.get("command_type") or "") == "CAMERA_TEST"
                            and result.get("success") is False
                        )
                        if camera_test_failed:
                            self.cloud_client.complete_edge_command(command["id"], {
                                "ok": False,
                                **result,
                                "error": result.get("message") or "Camera connection failed",
                            })
                            command_sync["failed"] += 1
                        else:
                            self.cloud_client.complete_edge_command(command["id"], {"ok": True, **result})
                            command_sync["completed"] += 1
                    except Exception as exc:
                        self.cloud_client.complete_edge_command(command["id"], {"ok": False, "error": str(exc)})
                        command_sync["failed"] += 1
            except Exception as exc:
                command_sync["error"] = str(exc)
            camera_sync = {"fetched": 0, "applied": 0, "failed": 0}
            if self.camera_manager is not None:
                try:
                    assignment = self.cloud_client.camera_config()
                    items = assignment.get("items") or []
                    camera_sync["fetched"] = len(items)
                    for camera in items:
                        try:
                            self.camera_manager.apply_cloud_camera(camera)
                            detection=camera.get("detection_config") or {"version":0}
                            camera_id=str(camera.get("camera_id") or "")
                            local=self.camera_manager.get_detection_config(camera_id,
                                bool((camera.get("features") or {}).get("unknown_detection") or (camera.get("features") or {}).get("unknown_person_detection")))
                            local_override=bool(local.get("local_override"))
                            incoming_version=int(detection.get("version") or 0)
                            ack_status="LOCAL_OVERRIDE" if local_override else (
                                "APPLIED" if int(local.get("cloud_version") or 0)>=incoming_version else "FAILED")
                            if ack_status=="FAILED": camera_sync["failed"]+=1
                            acknowledge=getattr(self.cloud_client,"acknowledge_detection_config",None)
                            if acknowledge:
                                try: acknowledge(camera_id,incoming_version,ack_status,local_override)
                                except Exception as ack_exc:
                                    camera_sync["failed"]+=1
                                    camera_sync.setdefault("ack_errors",[]).append({"camera_id":camera_id,"error":str(ack_exc)[:240]})
                            camera_sync["applied"] += 1
                        except Exception as exc:
                            camera_sync["failed"] += 1
                            camera_sync.setdefault("errors", []).append({"camera_id": str(camera.get("camera_id") or ""), "error": str(exc)[:300]})
                    if self.camera_supervisor is not None:
                        self.camera_supervisor.reconcile()
                except Exception as exc:
                    camera_sync["error"] = str(exc)
            # Camera/heartbeat/event delivery run frequently; CRM personnel snapshots
            # do not need a SQLite rewrite every few seconds. A failed refresh is
            # immediately retryable on the next sync cycle.
            personnel_sync={"fetched":0,"applied":0,"deactivated":0,"failed":0}
            personnel_interval=max(15.0,float(os.getenv("SNAPKEY_EDGE_PERSONNEL_SYNC_SECONDS","60")))
            personnel_now=time.monotonic()
            personnel_due=(self._last_personnel_sync_monotonic is None or
                           personnel_now-self._last_personnel_sync_monotonic>=personnel_interval)
            if personnel_due:
                try:
                    roster=self.cloud_client.personnel_config()
                    people=roster.get("items") or []
                    personnel_sync["fetched"]=len(people)
                    applied=self.store.apply_cloud_personnel(people)
                    personnel_sync["applied"]=int(applied.get("applied") or 0)
                    personnel_sync["deactivated"]=int(applied.get("deactivated") or 0)
                    personnel_sync["authoritative"]=bool(applied.get("authoritative",False))
                    self._last_personnel_sync_monotonic=time.monotonic()
                except Exception as exc:
                    personnel_sync["failed"]+=1
                    personnel_sync["error"]=str(exc)
            else:
                personnel_sync["skipped"]="interval_not_elapsed"
            for row in self.store.queued_events(getattr(self.sync_config, "batch_size", 50)):
                try:
                    event = json.loads(row["payload_json"])
                    metadata = event.get("metadata") or {}
                    # Alert events deliberately wait for their post-event clip to finish
                    # so image + video evidence reach the cloud in one durable event.
                    if metadata.get("evidence_pending"):
                        continue
                    # Face login must use the current recognition face crop when one
                    # exists. Other event types keep their normal snapshot priority.
                    if str(event.get("event_type") or "") == "PERSON_RECOGNIZED":
                        snapshot_path = metadata.get("face_path") or metadata.get("snapshot_path") or metadata.get("person_path")
                    else:
                        snapshot_path = metadata.get("snapshot_path") or metadata.get("person_path") or metadata.get("face_path")
                    clip_path = metadata.get("clip_path")
                    cloud_metadata = dict(metadata)
                    event_id=str(event.get("event_id") or row["id"])
                    is_attendance_evidence=(str(event.get("event_type") or "")=="PERSON_RECOGNIZED"
                                            or bool(metadata.get("attendance_action_evidence")))
                    if snapshot_path and Path(snapshot_path).is_file():
                        cloud_metadata["cloud_evidence"] = self.cloud_client.upload_event_evidence(event_id, snapshot_path)
                    elif snapshot_path:
                        cloud_metadata["evidence_unavailable"] = True
                        cloud_metadata.setdefault("evidence_missing",{})["primary_snapshot"]="local_file_missing"
                    if clip_path and Path(clip_path).is_file() and not is_attendance_evidence:
                        cloud_metadata["cloud_clip"] = self.cloud_client.upload_event_evidence(event_id, clip_path)
                    elif clip_path:
                        cloud_metadata["clip_unavailable"] = True
                        cloud_metadata.setdefault("evidence_missing",{})["clip"]="local_file_missing"

                    if is_attendance_evidence:
                        uploaded=[];missing=dict(cloud_metadata.get("evidence_missing") or {})
                        paths=metadata.get("snapshot_paths") or ([snapshot_path] if snapshot_path else [])
                        for index,path_value in enumerate(paths[:3]):
                            if path_value and Path(path_value).is_file():
                                ref=self.cloud_client.upload_event_evidence(
                                    f"{event_id}:attendance-snapshot-{index+1}",str(path_value))
                                uploaded.append({"index":index,"evidence":ref})
                            else:
                                missing[f"snapshot_{index+1}"]="not_captured" if not path_value else "local_file_missing"
                        if len(uploaded)<3:
                            missing["snapshots"]=f"expected_3_received_{len(uploaded)}"
                        if clip_path and Path(clip_path).is_file():
                            cloud_metadata["cloud_clip"]=self.cloud_client.upload_event_evidence(
                                f"{event_id}:attendance-video",str(clip_path))
                        elif not cloud_metadata.get("cloud_clip"):
                            missing["clip"]=(metadata.get("evidence_missing") or {}).get("clip") or "not_captured"
                        cloud_metadata["cloud_evidence_snapshots"]=uploaded
                        cloud_metadata["evidence_missing"]=missing
                        cloud_metadata["evidence_status"]=("COMPLETE" if len(uploaded)==3 and cloud_metadata.get("cloud_clip")
                                                           else "PARTIAL" if uploaded or cloud_metadata.get("cloud_clip")
                                                           else "UNAVAILABLE")
                    event["metadata"] = {key:value for key,value in cloud_metadata.items()
                        if key not in {'snapshot_path','person_path','face_path','clip_path','snapshot_paths','evidence_pending','evidence_path','candidate_token'}}
                    receipt=self.cloud_client.post_event(self.edge_config, event)
                    if metadata.get('attendance_sync_bridge') is True and event.get('event_type') in {
                            'ATTENDANCE_ENTRY','ATTENDANCE_EXIT','BREAK_START','BREAK_END'}:
                        status=(receipt.get('attendance_sync') or {}).get('status') if isinstance(receipt,dict) else None
                        if status!='SUCCEEDED':
                            raise RuntimeError('Attendance cloud delivery pending: '+str(status or 'NO_RECEIPT'))
                    self.store.mark_event_synced(row["id"])
                    synced += 1
                except Exception as exc:
                    attempts = int(row.get("attempts", 0) or 0) + 1
                    retry_after = min(300.0, max(2.0, 2.0 ** min(attempts, 8)))
                    self.store.mark_event_failed(row["id"], str(exc), retry_after_seconds=retry_after)
                    failed += 1

            result = SyncRunResult(enabled=True, synced=synced, failed=failed)
            self._remember(result, {"heartbeat": heartbeat_sync, "camera_sync": camera_sync, "personnel_sync": personnel_sync, "command_sync": command_sync})
            return result

    @staticmethod
    def _personnel_model_status(faces):
        from camera_service.face_provenance import active_model_key,template_diagnostics
        return template_diagnostics(faces,active_model_key())

    def _edge_status_payload(self, license_status) -> dict[str, Any]:
        """Build a credential-free inventory/status heartbeat for the cloud portal."""
        cameras = []
        if self.camera_manager is not None:
            for camera in self.camera_manager.list_cameras():
                runtime = self.camera_manager.get_camera_status(camera.camera_id)
                cameras.append({
                    "camera_id": camera.camera_id,
                    "name": camera.name,
                    "source_type": camera.source_type,
                    "camera_role": camera.camera_role.value if hasattr(camera.camera_role, "value") else str(camera.camera_role),
                    "camera_zone": camera.camera_zone.value if hasattr(camera.camera_zone, "value") else str(camera.camera_zone),
                    "enabled": bool(camera.enabled),
                    "features": camera.features.model_dump(),
                    "online": bool(runtime.online) if runtime else False,
                    "state": runtime.state.value if runtime and hasattr(runtime.state, "value") else (str(runtime.state) if runtime else "UNKNOWN"),
                    "last_frame_at": runtime.last_frame_at if runtime else None,
                    "capture_fps": runtime.capture_fps if runtime else 0.0,
                    "ai_fps": runtime.ai_fps if runtime else 0.0,
                    "frame_width":runtime.frame_width if runtime else None,
                    "frame_height":runtime.frame_height if runtime else None,
                    "last_error": runtime.last_error if runtime else None,
                })
        personnel = []
        try:
            for person in self.store.list_people():
                faces = self.store.list_faces(person["id"])
                personnel.append({
                    "person_id": person["id"],
                    "employee_code": person["employee_code"],
                    "full_name": person["full_name"],
                    "role": person["role"],
                    "active": bool(person["active"]),
                    "face_count": len(faces),
                    "face_ids": [f['id'] for f in faces],
                    **self._personnel_model_status(faces),
                })
        except Exception:
            # Heartbeat must remain available even if an older local database is
            # temporarily unable to provide the optional personnel inventory.
            personnel = []
        from camera_service.updater import current_build
        return {
            "service": "SnapKeyVisionAI",
            "build": current_build(),
            "license": {
                "active": bool(license_status.active),
                "plan": license_status.plan,
                "mode": license_status.mode,
            },
            "camera_count": len(cameras),
            "online_camera_count": sum(1 for camera in cameras if camera["online"]),
            "cameras": cameras,
            "personnel_count": len(personnel),
            "personnel": personnel,
        }

    def _execute_command(self, command: dict[str, Any]) -> dict[str, Any]:
        command_type=str(command.get("command_type") or "")
        request=command.get("request") or {}
        if command_type == "CAMERA_DELETE":
            camera_id = str(request.get("camera_id") or "")
            if self.camera_manager is None:
                raise RuntimeError("Camera manager is unavailable")
            self.camera_manager.delete_camera(camera_id)
            self.camera_manager.delete_camera_status(camera_id)
            if self.camera_supervisor is not None:
                self.camera_supervisor.reconcile()
            return {"deleted": True, "camera_id": camera_id}
        if command_type=="LIVE_VIEW_START":
            import json
            import logging
            logging.getLogger(__name__).info("[LIVE_VIEW] edge command received %s", json.dumps({
                "command_id":command.get("id"),"session_id":request.get("session_id"),
                "camera_id":request.get("camera_id"),"room":request.get("room")}))
            if self.camera_manager is None:
                raise RuntimeError("Camera manager is unavailable")
            from camera_service.livekit_publisher import LiveKitCameraPublisher
            if self._live_view_publisher is None:
                self._live_view_publisher = LiveKitCameraPublisher(self.camera_manager)
            return self._live_view_publisher.start(
                session_id=str(request.get("session_id") or ""),
                camera_id=str(request.get("camera_id") or ""),
                url=str(request.get("url") or ""),
                token=str(request.get("publisher_token") or ""),
                ttl_seconds=int(request.get("ttl_seconds") or 600),
            )
        if command_type=="LIVE_VIEW_STOP":
            if self._live_view_publisher is None:
                return {"stopped": True, "session_id": str(request.get("session_id") or ""), "already_stopped": True}
            return self._live_view_publisher.stop(str(request.get("session_id") or ""))
        if command_type=="ONVIF_PROBE":
            result=probe_onvif(str(request.get("host") or ""),int(request.get("port") or 80),
                               str(request.get("username") or ""),str(request.get("password") or ""))
            result["recommended_profile"]=select_profile(result.get("profiles") or [],str(request.get("purpose") or "ai"))
            return _redact_result(result)
        if command_type=="CAMERA_TEST":
            if self.camera_manager is None: raise RuntimeError("camera manager is unavailable")
            camera_id=str(request.get("camera_id") or "").strip()
            source_was_secret=False
            source_type=str(request.get("source_type") or "").strip()
            if camera_id:
                camera = self.camera_manager.get_camera(camera_id)
                if not camera:
                    raise RuntimeError(f"Camera {camera_id} was not found on this edge")
                source = camera.rtsp_url
                source_type = camera.source_type
                source_was_secret = True
            else:
                source=str(request.get("source") or "").strip()
            if not source:
                raise RuntimeError("Camera source is required")
            runtime = self.camera_manager.get_camera_status(camera_id) if camera_id and hasattr(self.camera_manager, "get_camera_status") else None
            if runtime and runtime.online:
                result = {"success": True, "message": "Camera runtime is receiving frames", "frames_received": runtime.frames_received, "fps": runtime.capture_fps}
            else:
                result=self.camera_manager.test_rtsp_connection(source)
            if not isinstance(result,dict):
                result={"connected":bool(result)}
            result=_safe_camera_test_result(result, source_was_secret=source_was_secret)
            if camera_id:
                result["camera_id"]=camera_id
            if source_type:
                result["source_type"]=source_type
            return result
        raise RuntimeError(f"Unsupported edge command: {command_type}")

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "enabled": self.cloud_client.enabled(),
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
        }

    def _remember(self, result: SyncRunResult, extra: dict[str, Any] | None = None) -> None:
        data = result.model_dump()
        if extra:
            data.update(extra)
        self.last_result = data
        self.last_run_at = self.store.now()
