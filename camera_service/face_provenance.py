"""Compatibility reporting without images, embeddings or permissive fallbacks."""
STATUSES=('UNVERIFIED','MODEL_UNAVAILABLE','INCOMPATIBLE','COMPATIBLE')


def template_diagnostics(faces, active_key):
    items=[]
    for face in faces:
        key=face.get('model_key')
        status=('UNVERIFIED' if not key else 'MODEL_UNAVAILABLE' if not active_key
                else 'COMPATIBLE' if key==active_key else 'INCOMPATIBLE')
        items.append({'face_id':face['id'],'model_compatibility':status,'enrollment_model_key':key})
    counts={status:sum(i['model_compatibility']==status for i in items) for status in STATUSES}
    aggregate=next((status for status in STATUSES if counts[status]),'UNVERIFIED')
    return {'model_compatibility':aggregate,'template_status_counts':counts,'templates':items}


def active_model_key():
    from camera_service.face_service import enrollment_model_key
    try: return enrollment_model_key()
    except (ValueError,OSError): return None
