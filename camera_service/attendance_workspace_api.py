"""Versioned local and scoped cloud reporting endpoints over existing attendance storage."""
from datetime import datetime,timezone
import json
from pathlib import Path
from urllib.parse import quote
from fastapi import Depends,HTTPException,Request,Response
from pydantic import BaseModel,Field,ValidationError,field_validator
from sqlalchemy import text
from cloud_portal.policy_resolution import resolve_attendance_policy
from camera_service.attendance_workspace import WorkspaceQuery,build_workspace,export_csv,policy_snapshot,EVENTS
from camera_service.attendance_state import STATES,ACTIONS,session_state

LIMIT=100000


class WorkspacePolicy(BaseModel):
    attendance_mode:str=Field(default='AUTO',pattern='^(AUTO|MANUAL)$')
    grace_period_minutes:int=Field(default=15,ge=1,le=1440)
    allowed_break_minutes:int=Field(default=60,ge=0,le=1440)
    total_working_minutes:int=Field(default=540,ge=1,le=1440)
    max_logoff_time:str=Field(default='21:30',pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    absence_auto_logout_enabled:bool=True
    absence_monitoring_enabled:bool=True
    mark_absent_after_minutes:int=Field(default=60,ge=60,le=1440)
    presence_update_interval_minutes:int=Field(default=2,ge=1,le=60)
    out_of_camera_grace_minutes:int=Field(default=5,ge=1,le=1440)
    max_out_of_camera_occurrences_per_day:int=Field(default=5,ge=1,le=100)
    admin_notification_after_minutes:int=Field(default=15,ge=1,le=1440)
    day_end_auto_logout_enabled:bool=True
    timezone:str='Asia/Kolkata'
    attendance_day_start_time:str=Field(default='00:00',pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    email_recipients:list[str]=Field(default_factory=list,max_length=50)
    whatsapp_recipients:list[str]=Field(default_factory=list,max_length=50)
    shift_start_time:str|None=Field(default=None,pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    late_grace_minutes:int=Field(default=0,ge=0,le=240)
    scheduled_weekdays:list[int]|None=None
    overtime_enabled:bool=True

    @field_validator('scheduled_weekdays')
    @classmethod
    def weekdays(cls,value):
        if value is not None and (len(set(value))!=len(value) or any(d not in range(7) for d in value)):
            raise ValueError('Weekdays must be unique values 0 (Monday) through 6 (Sunday)')
        return value

    @field_validator('email_recipients','whatsapp_recipients')
    @classmethod
    def recipients(cls,value):
        if any('\r' in item or '\n' in item or len(item)>254 for item in value):raise ValueError('Invalid notification recipient')
        return value


def parse_query(request):
    try:return WorkspaceQuery.model_validate(dict(request.query_params))
    except (ValidationError,ValueError,KeyError):raise HTTPException(422,'Invalid attendance filters, date range or timezone') from None


def _json(value):
    return json.loads(value or '{}') if isinstance(value,str) else value or {}


def local_sources(store,shop):
    with store._conn() as conn:
        sessions=[dict(r) for r in conn.execute('SELECT * FROM attendance_sessions WHERE store_id=? ORDER BY arrival_time LIMIT ?', (shop,LIMIT+1))]
        queue=[dict(r) for r in conn.execute('SELECT id,event_type,payload_json,status AS sync_status,last_error,crm_message FROM edge_event_queue WHERE event_type IN (\'ATTENDANCE_ENTRY\',\'ATTENDANCE_EXIT\',\'BREAK_START\',\'BREAK_END\') LIMIT ?', (LIMIT+1,))]
        original=[dict(r) for r in conn.execute('SELECT * FROM person_events WHERE store_id=? ORDER BY event_time LIMIT ?', (shop,LIMIT+1))]
    if any(len(rows)>LIMIT for rows in (sessions,queue,original)):raise HTTPException(413,'Attendance history exceeds the safe reporting bound; archive/reporting partition required')
    by_id={row['id']:{**row,'payload':_json(row['payload_json'])} for row in queue if str(_json(row['payload_json']).get('store_id') or '')==shop}
    for row in original:
        by_id[row['id']]={**by_id.get(row['id'],{}),**row,'metadata':_json(row['metadata_json'])}
    return store.list_people(),sessions,list(by_id.values())


def local_policy(store,shop,person_id=''):
    with store._conn() as conn:
        rows=conn.execute('SELECT person_id,policy_json FROM attendance_workspace_policies WHERE store_id=? AND person_id IN (?,\'\')',(shop,person_id)).fetchall()
    result=WorkspacePolicy().model_dump()
    for row in sorted(rows,key=lambda r:bool(r['person_id'])):result.update(_json(row['policy_json']))
    return result


def local_evidence(event,config):
    data=event['metadata'];root=Path(config.evidence_dir).resolve()
    paths=data.get('snapshot_paths') or ([data.get('snapshot_path') or data.get('evidence_path')] if data.get('snapshot_path') or data.get('evidence_path') else [])
    def available(value):
        if not value:return False
        path=Path(value).resolve()
        return path.is_relative_to(root) and path.is_file()
    base='/api/v2/attendance/events/'+quote(event['event_id'],safe='')
    images=[{'url':base+'/evidence?index='+str(i),'captured_at':None} for i,path in enumerate(paths[:3]) if available(path)]
    video={'url':base+'/clip','captured_at':None} if available(data.get('clip_path')) else None
    return {'event_id':event['event_id'],'camera_id':event['camera_id'],'status': 'COMPLETE' if len(images)==3 and video else 'PARTIAL' if images or video else 'Evidence unavailable',
            'images':images,'video':video,'missing':data.get('evidence_missing') or {},'pending':bool(data.get('evidence_pending'))}


def install_local(app,get_store,get_config):
    from fastapi.staticfiles import StaticFiles
    app.mount('/attendance-assets',StaticFiles(directory=Path(__file__).parent/'web'/'attendance-assets'),name='attendance-assets')
    def report(request):
        store=get_store();config=get_config();query=parse_query(request)
        if query.shop_id and query.shop_id!=config.store_id:raise HTTPException(403,'Shop scope mismatch')
        people,sessions,events=local_sources(store,config.store_id)
        for person in people:
            faces=store.list_faces(person['id']);face=next((f for f in faces if f.get('image_path')),None)
            person['photo_url']=f"/api/v1/personnel/{person['id']}/faces/{face['id']}/image" if face else None
        result=build_workspace(people,sessions,events,query,evidence=lambda e:local_evidence(e,config),
            policies={p['id']:local_policy(store,config.store_id,p['id']) for p in people})
        from camera_service.attendance_workspace import apply_day_decisions
        apply_day_decisions(result,store.attendance_day_decisions(config.store_id),query)
        return result

    @app.get('/api/v2/attendance/workspace')
    def workspace(request:Request):
        result=report(request);result.pop('_all_items');return result

    @app.get('/api/v2/attendance/personnel')
    def people():
        return {'items':[{k:p.get(k) for k in ('id','full_name','employee_code')} for p in get_store().list_people()]}

    @app.get('/api/v2/attendance/export')
    def export(request:Request):
        return Response(export_csv(report(request)['_all_items']),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="attendance.csv"','Cache-Control':'no-store'})

    @app.get('/api/v2/attendance/personnel/{person_id}/state')
    def state(person_id:str):
        store=get_store();config=get_config()
        if not store.get_person(person_id):raise HTTPException(404,'Employee not found')
        session=store.open_session(person_id,config.store_id);value=session_state(session)
        return {'person_id':person_id,'session_id':session['id'] if session else None,'state':value,'label':STATES[value],'actions':ACTIONS.get(value,[])}

    @app.get('/api/v2/attendance/policy')
    def policy(person_id:str=''):
        store=get_store();shop=get_config().store_id
        if person_id and not store.get_person(person_id):raise HTTPException(404,'Employee not found')
        value=local_policy(store,shop,person_id)
        with store._conn() as conn:
            saved=conn.execute('SELECT person_id,policy_json,updated_at FROM attendance_workspace_policies WHERE store_id=? AND person_id IN (?,\'\') ORDER BY person_id',(shop,person_id)).fetchall()
        employee_values=next((_json(row['policy_json']) for row in saved if person_id and row['person_id']==person_id),{})
        shop_values=next((_json(row['policy_json']) for row in saved if row['person_id']==''),{})
        sources={key:'EMPLOYEE' if key in employee_values else 'SHOP' if key in shop_values else 'SYSTEM' for key in value}
        updated=max((row['updated_at'] for row in saved),default=None)
        return {'policy':value,'snapshot':policy_snapshot(value),'sources':sources,'updated_at':updated,'person_id':person_id,'scope':'LOCAL_ONLY','can_edit':True}

    @app.put('/api/v2/attendance/policy')
    def save_policy(body:WorkspacePolicy,person_id:str=''):
        store=get_store();shop=get_config().store_id
        if person_id and not store.get_person(person_id):raise HTTPException(404,'Employee not found')
        value=body.model_dump();validate_policy(value)
        with store._lock,store._conn() as conn:
            conn.execute('INSERT INTO attendance_workspace_policies(store_id,person_id,policy_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(store_id,person_id) DO UPDATE SET policy_json=excluded.policy_json,updated_at=excluded.updated_at',(shop,person_id,json.dumps(value),store.now()))
            if person_id:conn.execute('UPDATE personnel SET attendance_mode=?,updated_at=? WHERE id=?',(value['attendance_mode'],store.now(),person_id))
        return {'policy':value,'snapshot':policy_snapshot(value),'scope':'LOCAL_ONLY','updated_at':store.now(),'auto_logout_execution_enabled':False}

    def media(event_id,kind,index):
        store=get_store();config=get_config()
        _,_,events=local_sources(store,config.store_id)
        from camera_service.attendance_workspace import normalize_event
        event=next((normalize_event(e) for e in events if e['id']==event_id),None)
        if not event:raise HTTPException(404,'Event not found')
        data=event['metadata'];paths=data.get('snapshot_paths') or ([data.get('snapshot_path')] if data.get('snapshot_path') else [])
        value=data.get('clip_path') if kind=='clip' else paths[index] if 0<=index<len(paths[:3]) else None
        if not value:raise HTTPException(404,'Evidence unavailable')
        path=Path(value).resolve();root=Path(config.evidence_dir).resolve()
        if not path.is_relative_to(root) or not path.is_file():raise HTTPException(404,'Evidence unavailable')
        from fastapi.responses import FileResponse
        return FileResponse(path,headers={'Cache-Control':'no-store'})

    @app.get('/api/v2/attendance/events/{event_id}/evidence')
    def image(event_id:str,index:int=0):return media(event_id,'image',index)

    @app.get('/api/v2/attendance/events/{event_id}/clip')
    def clip(event_id:str):return media(event_id,'clip',0)


def validate_policy(value):
    if not value['out_of_camera_grace_minutes']<value['admin_notification_after_minutes']<value['mark_absent_after_minutes']:
        raise HTTPException(422,'Camera grace must be less than notification threshold and full-day absence threshold')
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(value['timezone'])
    except (KeyError,ValueError):raise HTTPException(422,'Invalid policy timezone') from None


def cloud_sources(store,tenant,shop):
    is_pg=hasattr(store,'engine')
    with store._conn() as conn:
        types=','.join("'"+kind+"'" for kind in sorted(EVENTS))
        sql='SELECT id,event_type,event_time,camera_id,shop_id,edge_id,payload_json FROM edge_events WHERE tenant_id=:tenant AND shop_id=:shop AND event_type IN ('+types+') ORDER BY event_time LIMIT :limit'
        params={'tenant':tenant,'shop':shop,'limit':LIMIT+1}
        rows=conn.execute(text(sql),params).mappings().all() if is_pg else conn.execute(sql,params).fetchall()
        events=[{**dict(r),'payload':_json(r['payload_json'])} for r in rows]
        receipt_sql='SELECT event_id,status,crm_message,error_code FROM edge_attendance_delivery WHERE tenant_id=:tenant AND shop_id=:shop'
        receipts=conn.execute(text(receipt_sql),params).mappings().all() if is_pg else conn.execute(receipt_sql,params).fetchall()
        statuses={r['event_id']:dict(r) for r in receipts}
        if is_pg:
            activities=conn.execute(text('SELECT * FROM attendance_activity WHERE tenant_id=:tenant AND shop_id=:shop ORDER BY occurred_at LIMIT :limit'),params).mappings().all()
            if len(activities)>LIMIT:raise HTTPException(413,'Activity history exceeds the safe reporting bound')
            known={e['id'] for e in events}
            for row in activities:
                if row['id'] not in known:
                    events.append({**dict(row),'person_id':row['local_person_id'],'metadata':row['metadata_json'],'delivery_status':'CONFIRMED_ACTIVITY'})
            transitions=conn.execute(text('SELECT t.*,m.local_person_id FROM person_attendance_transitions t JOIN crm_person_mappings m ON m.tenant_id=t.tenant_id AND m.shop_id=t.shop_id AND m.crm_user_id=t.crm_user_id WHERE t.tenant_id=:tenant AND t.shop_id=:shop AND t.transition=\'GRACE_EXCEEDED\' ORDER BY t.occurred_at LIMIT :limit'),params).mappings().all()
            for row in transitions:
                events.append({'id':str(row['crm_user_id'])+':grace:'+str(row['absence_started_at']),'person_id':row['local_person_id'],
                    'shop_id':shop,'event_type':'GRACE_EXCEEDED','occurred_at':row['occurred_at'],'source':'AUTO',
                    'camera_id':(row['details_json'] or {}).get('cameraId'),
                    'metadata':{'reason':'Absence grace exceeded'},'processing_status':'RECORDED'})
    if len(events)>LIMIT:raise HTTPException(413,'Attendance history exceeds the safe reporting bound; reporting partition required')
    people=store.list_cloud_people(tenant,shop)
    for event in events:
        receipt=statuses.get(event['id']) or {}
        event.setdefault('delivery_status',receipt.get('status') or 'NO_RECEIPT')
        event.setdefault('crm_message',receipt.get('crm_message'))
        event.setdefault('crm_error',receipt.get('error_code'))
    return people,[],events


def cloud_current_state(store,tenant,shop,person,user):
    if hasattr(store,'get_person_attendance_presence'):
        presence=store.get_person_attendance_presence(tenant,shop,user)
        state='OUT' if not presence or not presence.get('checked_in') else 'ON_BREAK' if presence.get('on_break') else 'IN'
    else:state='OUT'
    sql='SELECT d.* FROM edge_attendance_delivery d JOIN edge_events e ON e.id=d.event_id WHERE d.tenant_id=:tenant AND d.shop_id=:shop AND d.person_id=:person ORDER BY e.event_time DESC,d.event_id DESC LIMIT 1'
    with store._conn() as conn:
        params={'tenant':tenant,'shop':shop,'person':person}
        row=conn.execute(text(sql),params).mappings().first() if hasattr(store,'engine') else conn.execute(sql,params).fetchone()
    row=dict(row) if row else None
    if row and row['status'] in {'RECONCILIATION_REQUIRED','CRM_CONFIRMED','CLAIMED'}:state='NEEDS_REVIEW'
    elif row and row['status']=='SUCCEEDED':
        state={'ATTENDANCE_ENTRY':'IN','ATTENDANCE_EXIT':'OUT','BREAK_START':'ON_BREAK','BREAK_END':'IN'}.get(row['event_type'],state)
    return state,row


def apply_cloud_station(tenant,request,principal,api,_locked=False):
    """Versioned manual workflow reuses the existing durable CRM delivery bridge."""
    import hashlib
    from camera_service.attendance_state import transition
    api['_portal_scope'](tenant,principal);store=api['store']
    if principal.role.upper() not in {'OWNER','ADMIN','SUPERADMIN','MANAGER','OPERATOR'}:raise HTTPException(403,'Attendance operator access required')
    camera=api['_portal_camera_lookup'](tenant,principal.shop_id,request.edge_id,request.camera_id)
    if not camera or str(camera.get('camera_role','')).upper()!='ENTRANCE_EXIT':raise HTTPException(400,'Attendance camera required')
    recognition=store.get_event(tenant,principal.shop_id,request.recognition_event_id)
    if not recognition or recognition['event_type']!='PERSON_RECOGNIZED' or recognition.get('camera_id')!=request.camera_id or recognition.get('edge_id')!=request.edge_id:
        raise HTTPException(409,'Recognition candidate changed')
    payload=recognition.get('payload') or {};payload=payload.get('payload') if isinstance(payload.get('payload'),dict) else payload
    person=str(payload.get('person_id') or '');mapping=store.crm_person_mapping(tenant,principal.shop_id,person)
    if not mapping:raise HTTPException(409,'Scoped CRM employee mapping required')
    if not _locked:
        if not hasattr(store,'engine'):raise HTTPException(503,'PostgreSQL is required for concurrent cloud attendance confirmation')
        # A separate two-integer namespace avoids deadlocking the delivery bridge's
        # bigint lock. Serialize state validation through CRM/local finalization,
        # across portal workers; the bridge still owns delivery idempotency.
        with store._conn() as conn:
            conn.execute(text('SELECT pg_advisory_xact_lock(1096043854,hashtext(:scope))'),{'scope':tenant+'|'+principal.shop_id+'|'+person})
            return apply_cloud_station(tenant,request,principal,api,_locked=True)
    action=request.action.upper();canonical={'CHECK_IN':'ATTENDANCE_ENTRY','CHECK_OUT':'ATTENDANCE_EXIT','BREAK_START':'BREAK_START','BREAK_END':'BREAK_END'}
    if action not in canonical:raise HTTPException(422,'Unsupported attendance action')
    identity=hashlib.sha256((tenant+'|'+principal.shop_id+'|'+str(principal.user_id or '')+'|'+request.recognition_event_id+'|'+action+'|'+(getattr(request,'request_id',None) or '')).encode()).hexdigest()
    event_id='portal-manual:'+identity
    saved=store.get_event(tenant,principal.shop_id,event_id)
    if saved:
        receipt=store.attendance_delivery_receipt(tenant,principal.shop_id,event_id)
        return {'ok':bool(receipt and receipt['status']=='SUCCEEDED'),'duplicate':True,'audit_event_id':event_id,'delivery':(receipt or {}).get('status','RECONCILIATION_REQUIRED')}
    from camera_service.attendance_workspace import instant
    now=datetime.now(timezone.utc);seen=instant(recognition['event_time'])
    if not 0<=(now-seen).total_seconds()<=15:raise HTTPException(409,'Recognition expired; face the camera again')
    state,previous=cloud_current_state(store,tenant,principal.shop_id,person,str(mapping['crm_user_id']))
    try:new_state=transition(state,{'BREAK_START':'START_BREAK','BREAK_END':'END_BREAK'}.get(action,action))
    except ValueError as error:raise HTTPException(409,str(error)) from None
    session=event_id if action=='CHECK_IN' else (previous or {}).get('session_id')
    if not session:raise HTTPException(409,'Attendance session requires reconciliation before this action')
    shop_policy=store.attendance_policy(tenant,principal.shop_id) if hasattr(store,'attendance_policy') else {}
    person_policy=(store.person_attendance_policy(tenant,principal.shop_id,str(mapping['crm_user_id'])) if hasattr(store,'person_attendance_policy') else {}) or {}
    effective=resolve_attendance_policy(person_policy,shop_policy).values
    aliases={'gracePeriodMinutes':'grace_period_minutes','allowedBreakMinutes':'allowed_break_minutes','requiredWorkingMinutes':'total_working_minutes','maxLogoffTime':'max_logoff_time','absenceAutoLogoutEnabled':'absence_auto_logout_enabled'}
    captured_policy={**shop_policy,**person_policy,**{alias:effective[key] for key,alias in aliases.items()}}
    metadata={**(payload.get('metadata') or {}),'attendance_sync_bridge':True,'attendance_session_id':session,
        'attendance_source':'MANUAL','source':'CRM_ATTENDANCE_STATION','prior_state':state,'new_state':new_state,
        'predecessor_event_id':(previous or {}).get('event_id'),'recognition_event_id':request.recognition_event_id,
        'crm_confirmed_break':action in {'BREAK_START','BREAK_END'},'reason':'Operator-confirmed attendance',
        'attendance_policy_snapshot':policy_snapshot(captured_policy),
        'confirmed_by_user_id':principal.user_id,'break_closed_by_checkout':state=='ON_BREAK' and action=='CHECK_OUT'}
    envelope={'event_id':event_id,'tenant_id':tenant,'shop_id':principal.shop_id,'site_id':str(camera.get('site_id') or principal.shop_id),
        'company_code':principal.company_code,'edge_id':request.edge_id,'camera_id':request.camera_id,'event_type':canonical[action],
        'event_time':now.isoformat(),'payload':{'person_id':person,'store_id':principal.shop_id,'event_type':canonical[action],'metadata':metadata}}
    store.record_portal_event(envelope)
    receipt=api['_synchronize_attendance_bridge'](envelope)
    return {'ok':receipt.get('status')=='SUCCEEDED','audit_event_id':event_id,'action':action,'person_id':person,
        'state_before':state,'state_after':new_state if receipt.get('status')=='SUCCEEDED' else 'NEEDS_REVIEW','delivery':receipt.get('status')}


def install_cloud(app,get_store,require_session,scope,admin,resolve_media):
    from fastapi.staticfiles import StaticFiles
    app.mount('/attendance-assets',StaticFiles(directory=Path(__file__).parent/'web'/'attendance-assets'),name='attendance-assets')
    def report(tenant,request,principal):
        scope(tenant,principal)
        if principal.role.upper() not in {'OWNER','ADMIN','SUPERADMIN','MANAGER','OPERATOR'}:raise HTTPException(403,'Attendance reporting access required')
        query=parse_query(request)
        if query.shop_id and query.shop_id!=principal.shop_id:raise HTTPException(403,'Shop scope mismatch')
        store=get_store();people,sessions,events=cloud_sources(store,tenant,principal.shop_id)
        shop_policy=store.attendance_policy(tenant,principal.shop_id) if hasattr(store,'attendance_policy') else WorkspacePolicy().model_dump()
        policies={}
        for p in people:
            mapping=store.crm_person_mapping(tenant,principal.shop_id,p['id'])
            user=(store.person_attendance_policy(tenant,principal.shop_id,str(mapping['crm_user_id'])) if mapping and hasattr(store,'person_attendance_policy') else {}) or {}
            values=resolve_attendance_policy(user,shop_policy).values
            policies[p['id']]={**shop_policy,**values,**{alias:values[key] for key,alias in {'gracePeriodMinutes':'grace_period_minutes','allowedBreakMinutes':'allowed_break_minutes','requiredWorkingMinutes':'total_working_minutes','maxLogoffTime':'max_logoff_time','absenceAutoLogoutEnabled':'absence_auto_logout_enabled'}.items()}}
            p['photo_url']=f'/portal/v1/tenants/{quote(tenant,safe="")}/personnel/{quote(str(p["id"]),safe="")}/enrollment-image' if mapping else None
        def evidence(event):
            media_event_id=event['event_id']
            parent_id=(event.get('metadata') or {}).get('recognition_event_id')
            if not store.get_event(tenant,principal.shop_id,media_event_id) and parent_id:
                parent=store.get_event(tenant,principal.shop_id,str(parent_id))
                parent_payload=(parent or {}).get('payload') or {}
                parent_payload=parent_payload.get('payload') if isinstance(parent_payload.get('payload'),dict) else parent_payload
                if parent and parent.get('event_type')=='PERSON_RECOGNIZED' and str(parent_payload.get('person_id') or '')==event['person_id'] and parent.get('camera_id')==event['camera_id']:
                    media_event_id=str(parent_id)
            base=f'/portal/v1/tenants/{quote(tenant,safe="")}/events/{quote(media_event_id,safe="")}'
            images=[];video=None
            for index in range(3):
                try:resolve_media(tenant,principal.shop_id,media_event_id,'snapshot',index);images.append({'url':base+'/evidence?index='+str(index),'captured_at':None})
                except HTTPException:pass
            try:resolve_media(tenant,principal.shop_id,media_event_id,'video');video={'url':base+'/clip','captured_at':None}
            except HTTPException:pass
            return {'event_id':event['event_id'],'media_event_id':media_event_id,'camera_id':event['camera_id'],'images':images,'video':video,'status':'COMPLETE' if len(images)==3 and video else 'PARTIAL' if images or video else 'Evidence unavailable'}
        result=build_workspace(people,sessions,events,query,evidence=evidence,policies=policies)
        if hasattr(store,'attendance_day_decisions'):
            from camera_service.attendance_workspace import apply_day_decisions
            apply_day_decisions(result,store.attendance_day_decisions(tenant,principal.shop_id),query)
        result['can_correct_day']=principal.role.upper() in {'OWNER','ADMIN','SUPERADMIN'}
        return result

    @app.post('/portal/v2/tenants/{tenant_id}/attendance/days/{day}/correction')
    def correct_day(tenant_id:str,day:str,body:dict,principal=Depends(require_session)):
        from datetime import date
        scope(tenant_id,principal);admin(principal)
        try:date.fromisoformat(day)
        except ValueError:raise HTTPException(422,'Invalid attendance date') from None
        person=str(body.get('person_id') or '');note=str(body.get('note') or '').strip();status=body.get('status')
        if not note or len(note)>500 or status not in {'PRESENT','ABSENT'}:raise HTTPException(422,'Status and audit note required')
        store=get_store();mapping=store.crm_person_mapping(tenant_id,principal.shop_id,person)
        if not mapping:raise HTTPException(404,'Scoped employee mapping required')
        if not store.correct_attendance_day(tenant_id,principal.shop_id,mapping['crm_user_id'],day,str(principal.user_id),note,status):
            raise HTTPException(409,'No full-day absence decision for this date')
        return {'status':status,'audited':True,'crm_mutation':False}

    @app.get('/portal/v2/tenants/{tenant_id}/attendance/workspace')
    def workspace(tenant_id:str,request:Request,principal=Depends(require_session)):
        result=report(tenant_id,request,principal);result.pop('_all_items');return result

    @app.get('/portal/v2/tenants/{tenant_id}/attendance/personnel')
    def people(tenant_id:str,principal=Depends(require_session)):
        scope(tenant_id,principal)
        if principal.role.upper() not in {'OWNER','ADMIN','SUPERADMIN','MANAGER','OPERATOR'}:raise HTTPException(403,'Attendance reporting access required')
        return {'items':[{k:p.get(k) for k in ('id','full_name','employee_code')} for p in get_store().list_cloud_people(tenant_id,principal.shop_id)]}

    @app.get('/portal/v2/tenants/{tenant_id}/attendance/export')
    def export(tenant_id:str,request:Request,principal=Depends(require_session)):
        return Response(export_csv(report(tenant_id,request,principal)['_all_items']),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="attendance.csv"','Cache-Control':'no-store'})

    @app.get('/portal/v2/tenants/{tenant_id}/attendance/policy')
    def policy(tenant_id:str,person_id:str='',principal=Depends(require_session)):
        scope(tenant_id,principal)
        if principal.role.upper() not in {'OWNER','ADMIN','SUPERADMIN','MANAGER','OPERATOR'}:raise HTTPException(403,'Attendance policy access required')
        if not hasattr(get_store(),'attendance_policy'):raise HTTPException(503,'PostgreSQL policy storage required')
        store=get_store();shop_policy=store.attendance_policy(tenant_id,principal.shop_id)
        resolved_sources={k:'SHOP' for k in shop_policy}
        value=dict(shop_policy)
        if person_id:
            mapping=store.crm_person_mapping(tenant_id,principal.shop_id,person_id)
            if not mapping:raise HTTPException(404,'Scoped CRM employee mapping required')
            override=store.person_attendance_policy(tenant_id,principal.shop_id,str(mapping['crm_user_id'])) or {}
            resolved=resolve_attendance_policy(override,shop_policy);values=resolved.values;resolved_sources=resolved.sources
            for field,alias in {'gracePeriodMinutes':'grace_period_minutes','allowedBreakMinutes':'allowed_break_minutes','requiredWorkingMinutes':'total_working_minutes','maxLogoffTime':'max_logoff_time','absenceAutoLogoutEnabled':'absence_auto_logout_enabled','attendanceMode':'attendance_mode','absenceMonitoringEnabled':'absence_monitoring_enabled','markAbsentAfterMinutes':'mark_absent_after_minutes'}.items():value[alias]=values[field]
            value.update({k:v for k,v in override.items() if k in {'shift_start_time','late_grace_minutes','scheduled_weekdays','overtime_enabled','email_recipients','whatsapp_recipients'}})
            value['version']=override.get('version',shop_policy.get('version'));value['updatedAt']=override.get('updatedAt',shop_policy.get('updated_at'))
        else:
            value.update({k:v for k,v in shop_policy.items() if k in {'attendance_mode','attendanceMode','absence_monitoring_enabled','absenceMonitoringEnabled','mark_absent_after_minutes','markAbsentAfterMinutes'}})
            value['attendance_mode']=value.get('attendanceMode',value.get('attendance_mode','AUTO'))
            value['absence_monitoring_enabled']=value.get('absenceMonitoringEnabled',value.get('absence_monitoring_enabled',True))
            value['mark_absent_after_minutes']=value.get('markAbsentAfterMinutes',value.get('mark_absent_after_minutes',60))
        from cloud_portal.policy_resolution import SHOP_ALIASES
        applied=resolve_attendance_policy(override if person_id else {},shop_policy)
        for camel,snake in SHOP_ALIASES.items():
            value[snake]=applied.values[camel]
            resolved_sources[snake]=applied.sources[camel]
        return {'policy':value,'sources':resolved_sources,'updated_at':value.get('updatedAt') or value.get('updated_at'),
            'can_edit':principal.role.upper() in {'OWNER','ADMIN','SUPERADMIN'},'person_id':person_id}

    @app.put('/portal/v2/tenants/{tenant_id}/attendance/policy')
    def save_policy(tenant_id:str,body:WorkspacePolicy,person_id:str='',principal=Depends(require_session)):
        scope(tenant_id,principal);admin(principal)
        if not hasattr(get_store(),'upsert_attendance_policy'):raise HTTPException(503,'PostgreSQL policy storage required')
        value=body.model_dump();validate_policy(value)
        store=get_store()
        if person_id:
            mapping=store.crm_person_mapping(tenant_id,principal.shop_id,person_id)
            if not mapping:raise HTTPException(404,'Scoped CRM employee mapping required')
            existing=store.person_attendance_policy(tenant_id,principal.shop_id,str(mapping['crm_user_id'])) or {}
            aliases={'attendance_day_start_time':'attendanceDayStartTime','presence_update_interval_minutes':'presenceUpdateIntervalMinutes','out_of_camera_grace_minutes':'outOfCameraGraceMinutes','max_out_of_camera_occurrences_per_day':'maxOutOfCameraOccurrencesPerDay','admin_notification_after_minutes':'adminNotificationAfterMinutes','day_end_auto_logout_enabled':'dayEndAutoLogoutEnabled','grace_period_minutes':'gracePeriodMinutes','allowed_break_minutes':'allowedBreakMinutes','total_working_minutes':'requiredWorkingMinutes','max_logoff_time':'maxLogoffTime','absence_auto_logout_enabled':'absenceAutoLogoutEnabled','attendance_mode':'attendanceMode','absence_monitoring_enabled':'absenceMonitoringEnabled','mark_absent_after_minutes':'markAbsentAfterMinutes','late_grace_minutes':'late_grace_minutes'}
            saved=store.upsert_person_attendance_policy(tenant_id,principal.shop_id,str(mapping['crm_user_id']),{**existing,**{aliases.get(k,k):v for k,v in value.items()}})
        else:
            saved=store.upsert_attendance_policy(tenant_id,principal.shop_id,value)
            saved.update({'attendance_mode':saved.get('attendanceMode',saved.get('attendance_mode','MANUAL')),
                'absence_monitoring_enabled':saved.get('absenceMonitoringEnabled',saved.get('absence_monitoring_enabled',True)),
                'mark_absent_after_minutes':saved.get('markAbsentAfterMinutes',saved.get('mark_absent_after_minutes',60))})
        return {'policy':saved,'execution_flags_changed':False}

    @app.get('/portal/v2/tenants/{tenant_id}/attendance/events/{event_id}/notifications')
    def notification_status(tenant_id:str,event_id:str,principal=Depends(require_session)):
        scope(tenant_id,principal)
        if principal.role.upper() not in {'OWNER','ADMIN','SUPERADMIN','MANAGER','OPERATOR'}:raise HTTPException(403,'Attendance reporting access required')
        if not get_store().get_event(tenant_id,principal.shop_id,event_id):raise HTTPException(404,'Event not found')
        if not hasattr(get_store(),'notification_delivery_status'):return {'status':'UNAVAILABLE','channels':{}}
        return {'status':'RECORDED','deliveries':get_store().notification_delivery_status(tenant_id,principal.shop_id,event_id)}
