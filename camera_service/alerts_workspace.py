"""Scoped alert read model and operator journal; original detector events are immutable."""
import csv
import hashlib
import io
import json
from datetime import datetime,timezone
from urllib.parse import quote
from fastapi import Depends,HTTPException,Request,Response
from pydantic import BaseModel,Field
from sqlalchemy import text
from camera_service.attendance_workspace import WorkspaceQuery,instant

DDL=["""CREATE TABLE IF NOT EXISTS alert_operator_state(
 tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,alert_id TEXT NOT NULL,status TEXT NOT NULL,
 updated_at TEXT NOT NULL,PRIMARY KEY(tenant_id,shop_id,alert_id))""", """CREATE TABLE IF NOT EXISTS alert_operator_history(
 id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,alert_id TEXT NOT NULL,
 previous_status TEXT NOT NULL,new_status TEXT NOT NULL,actor TEXT NOT NULL,reason TEXT NOT NULL,occurred_at TEXT NOT NULL)""", "CREATE INDEX IF NOT EXISTS idx_alert_operator_history_scope ON alert_operator_history(tenant_id,shop_id,alert_id,occurred_at)"]
ALERT_TYPES={'UNKNOWN_INCIDENT','UNKNOWN_INSIDE_ALERT','CROWD_ALERT','LONG_BREAK_ALERT','AUTO_CHECK_OUT',
 'MAX_LOGOFF_AUTO_CHECK_OUT','ATTENDANCE_POLICY_VIOLATION','CAMERA_OFFLINE','CAMERA_ONLINE','RECOGNITION_FAILED',
 'RECOGNITION_UNAVAILABLE','SECURITY_ALERT','SHOPLIFTING_ALERT','OBJECT_SECURITY_ALERT','ATTENDANCE_EXCEPTION'}


def execute(store,conn,sql,params=None):
    return conn.execute(text(sql),params or {}) if hasattr(store,'engine') else conn.execute(sql,params or {})


def initialize(store):
    with store._conn() as conn:
        for sql in DDL:execute(store,conn,sql)


class AlertAction(BaseModel):
    action:str=Field(pattern='^(ACKNOWLEDGE|RESOLVE)$')
    expected_status:str=Field(pattern='^(OPEN|ACKNOWLEDGED)$')
    request_id:str=Field(min_length=1,max_length=128)
    reason:str=Field(min_length=1,max_length=1000)


def local_alerts(store,shop,public):
    rows=[]
    with store._conn() as conn:
        for table,kind,stamp,field in [('unknown_incidents','unknown-incidents','confirmed_unknown_at','UNKNOWN_INCIDENT'),('security_alerts','security-alerts','event_time',None)]:
            source=conn.execute('SELECT * FROM '+table+' WHERE store_id=:shop ORDER BY '+stamp+' DESC LIMIT 100001',{'shop':shop}).fetchall()
            if len(source)>100000:raise HTTPException(413,'Alert reporting partition required')
            for record in source:
                item=dict(record);safe=public(item,kind,store)
                rows.append({'id':kind+':'+item['id'],'timestamp':item[stamp],'type':field or item['alert_type'],
                    'camera_id':item['camera_id'],'shop_id':shop,'person_id':None,'employee_name':None,'employee_code':None,
                    'source':'CAMERA','reason':(item.get('object_label') or 'Confirmed unknown person') if field else item.get('object_label') or item['alert_type'],
                    'severity':(item.get('metadata_json') or {}).get('severity') if isinstance(item.get('metadata_json'),dict) else None,
                    'status':item.get('status','OPEN'),'images':[safe['snapshot_url']] if safe.get('snapshot_url') else [],'video':safe.get('clip_url')})
        source=conn.execute("SELECT * FROM person_events WHERE store_id=:shop AND (event_type LIKE '%ALERT%' OR event_type LIKE '%VIOLATION%' OR event_type IN ('CAMERA_OFFLINE','RECOGNITION_FAILED','ATTENDANCE_EXCEPTION')) ORDER BY event_time DESC LIMIT 100001",{'shop':shop}).fetchall()
        if len(source)>100000:raise HTTPException(413,'Alert reporting partition required')
        for record in source:
            item=dict(record);metadata=json.loads(item.get('metadata_json') or '{}');person=store.get_person(item['person_id']) if item.get('person_id') else None
            rows.append({'id':'event:'+item['id'],'timestamp':item['event_time'],'type':item['event_type'],'camera_id':item['camera_id'],
                'shop_id':shop,'person_id':item.get('person_id'),'employee_name':(person or {}).get('full_name'),'employee_code':(person or {}).get('employee_code'),
                'source':'EDGE','reason':metadata.get('reason') or metadata.get('reason_code') or item['event_type'],'status':'OPEN','images':[],'video':None})
    return rows


def cloud_alerts(store,tenant,shop,resolve):
    types=','.join("'"+kind+"'" for kind in sorted(ALERT_TYPES))
    with store._conn() as conn:
        source=execute(store,conn,'SELECT * FROM edge_events WHERE tenant_id=:tenant AND shop_id=:shop AND (event_type IN ('+types+") OR event_type LIKE '%ALERT%' OR event_type LIKE '%SECURITY%') ORDER BY event_time DESC LIMIT 100001",{'tenant':tenant,'shop':shop}).fetchall()
    if len(source)>100000:raise HTTPException(413,'Alert reporting partition required')
    rows=[]
    for record in source:
        item=dict(record._mapping) if hasattr(record,'_mapping') else dict(record)
        payload=item.get('payload_json') or {};payload=json.loads(payload) if isinstance(payload,str) else payload
        payload=payload.get('payload') if isinstance(payload.get('payload'),dict) else payload
        metadata=payload.get('metadata') or {};unknown='UNKNOWN' in item['event_type'];pid=None if unknown else payload.get('person_id')
        person=store.get_cloud_person(tenant,shop,pid) if pid else None
        base=f'/portal/v1/tenants/{quote(tenant,safe="")}/events/{quote(item["id"],safe="")}'
        images=[];video=None
        for index in range(3):
            try:resolve(tenant,shop,item['id'],'snapshot',index);images.append(base+'/evidence?index='+str(index))
            except HTTPException:pass
        try:resolve(tenant,shop,item['id'],'video');video=base+'/clip'
        except HTTPException:pass
        rows.append({'id':'event:'+item['id'],'event_id':item['id'],'timestamp':item['event_time'],'type':item['event_type'],
            'camera_id':item.get('camera_id'),'shop_id':shop,'person_id':pid,'employee_name':(person or {}).get('full_name'),
            'employee_code':(person or {}).get('employee_code'),'source':'CLOUD_EVENT','reason':metadata.get('reason') or metadata.get('reason_code') or item['event_type'],
            'severity':metadata.get('severity'),
            'status':'OPEN','images':images,'video':video})
    return rows


def decorate(store,tenant,shop,rows):
    with store._conn() as conn:
        states=execute(store,conn,'SELECT alert_id,status FROM alert_operator_state WHERE tenant_id=:tenant AND shop_id=:shop',{'tenant':tenant,'shop':shop}).fetchall()
    states={r[0]:r[1] for r in states}
    for row in rows:
        row['status']=states.get(row['id'],row['status'])
        recorded=row.get('severity') in {'INFO','WARNING','CRITICAL'}
        row['severity']=row.get('severity') if recorded else 'CRITICAL' if any(x in row['type'] for x in ('SHOPLIFT','SECURITY')) else 'WARNING' if any(x in row['type'] for x in ('UNKNOWN','OFFLINE','FAILED','VIOLATION','EXCEPTION')) else 'INFO'
        row['severity_basis']='RECORDED' if recorded else 'WORKSPACE_CLASSIFICATION'
    return rows


def report(rows,request):
    filters=dict(request.query_params)
    values={k:v for k,v in filters.items() if k in {'preset','start','end','timezone','page','page_size','search','person_id','shop_id','camera_id','direction'}}
    try:
        query=WorkspaceQuery.model_validate(values);start,end=query.window(datetime.now(timezone.utc))
        if filters.get('start_at') or filters.get('end_at'):
            start,end=instant(filters.get('start_at')),instant(filters.get('end_at'))
            if not start or not end or end<=start or (end-start).days>366:raise ValueError()
        if filters.get('status') not in {None,'','OPEN','ACKNOWLEDGED','RESOLVED'}:raise ValueError()
        if filters.get('severity') not in {None,'','INFO','WARNING','CRITICAL'}:raise ValueError()
        if filters.get('sort','time') not in {'time','severity','status'}:raise ValueError()
    except (ValueError,KeyError):raise HTTPException(422,'Invalid alert filters') from None
    selected=[]
    for row in rows:
        try:stamp=instant(row['timestamp'])
        except ValueError:continue
        if not stamp or not start<=stamp<end:continue
        if query.search.casefold() not in (str(row['employee_name'] or '')+' '+str(row['employee_code'] or '')+' '+str(row['reason'])).casefold():continue
        if any(filters.get(key) and str(row.get(key) or '')!=filters[key] for key in ('camera_id','shop_id','person_id','status','severity','type')):continue
        copy=dict(row);copy['timestamp']=stamp.astimezone(__import__('zoneinfo').ZoneInfo(query.timezone)).isoformat();selected.append(copy)
    sort=filters.get('sort','time')
    selected.sort(key=lambda r:(r['timestamp'] if sort=='time' else {'INFO':0,'WARNING':1,'CRITICAL':2}[r['severity']] if sort=='severity' else r['status'],r['id']),reverse=query.direction=='desc')
    offset=(query.page-1)*query.page_size
    return {'items':selected[offset:offset+query.page_size],'total':len(selected),'page':query.page,'page_size':query.page_size,
        'summary':{state:sum(r['status']==state for r in selected) for state in ('OPEN','ACKNOWLEDGED','RESOLVED')},'_all':selected}


def action(store,tenant,shop,row,body,actor):
    identity=hashlib.sha256((tenant+'|'+shop+'|'+row['id']+'|'+body.request_id).encode()).hexdigest()
    args={'id':identity,'tenant':tenant,'shop':shop,'alert':row['id'],'actor':actor,'reason':body.reason.strip(),
        'new':'ACKNOWLEDGED' if body.action=='ACKNOWLEDGE' else 'RESOLVED','stamp':datetime.now(timezone.utc).isoformat()}
    if not args['reason']:raise HTTPException(422,'A reason is required')
    with store._conn() as conn:
        if hasattr(store,'engine'):execute(store,conn,'SELECT pg_advisory_xact_lock(1095520852,hashtext(:scope))',{'scope':tenant+'|'+shop+'|'+row['id']})
        else:conn.execute('BEGIN IMMEDIATE')
        previous=execute(store,conn,'SELECT new_status,reason FROM alert_operator_history WHERE id=:id',args).fetchone()
        if previous:
            if previous[0]!=args['new'] or previous[1]!=args['reason']:raise HTTPException(409,'Idempotency key reused with different action')
            return {'duplicate':True,'status':previous[0]}
        current=execute(store,conn,'SELECT status FROM alert_operator_state WHERE tenant_id=:tenant AND shop_id=:shop AND alert_id=:alert',args).fetchone()
        args['old']=current[0] if current else row['status']
        if args['old']!=body.expected_status or args['old']=='RESOLVED' or (args['new']=='ACKNOWLEDGED' and args['old']!='OPEN'):raise HTTPException(409,'Alert state changed; refresh')
        execute(store,conn,'INSERT INTO alert_operator_history VALUES(:id,:tenant,:shop,:alert,:old,:new,:actor,:reason,:stamp)',args)
        execute(store,conn,'INSERT INTO alert_operator_state VALUES(:tenant,:shop,:alert,:new,:stamp) ON CONFLICT(tenant_id,shop_id,alert_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at',args)
    return {'duplicate':False,'status':args['new']}


def install(app,get_store,get_scope,get_rows,prefix,dependency=None):
    def scoped(principal,request):
        tenant,shop,actor,can_edit=get_scope(principal,request)
        return tenant,shop,actor,can_edit
    deps=Depends(dependency) if dependency else None

    @app.get(prefix+'/workspace')
    def workspace(request:Request,principal=deps):
        tenant,shop,_,can_edit=scoped(principal,request)
        if request.query_params.get('shop_id') not in {None,'',shop}:raise HTTPException(403,'Shop scope mismatch')
        result=report(decorate(get_store(),tenant,shop,get_rows(tenant,shop)),request);result.pop('_all');result['can_edit']=can_edit;return result

    @app.get(prefix+'/export')
    def export(request:Request,principal=deps):
        tenant,shop,_,_=scoped(principal,request)
        if request.query_params.get('shop_id') not in {None,'',shop}:raise HTTPException(403,'Shop scope mismatch')
        rows=report(decorate(get_store(),tenant,shop,get_rows(tenant,shop)),request)['_all'];stream=io.StringIO(newline='')
        columns=['timestamp','type','severity','status','employee_name','employee_code','camera_id','shop_id','source','reason']
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
        for row in rows:writer.writerow({k:"'"+str(row[k]) if str(row[k] or '').lstrip().startswith(('=','+','-','@')) else row[k] for k in columns})
        return Response(stream.getvalue().encode('utf-8-sig'),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="alerts.csv"','Cache-Control':'no-store'})

    @app.get(prefix+'/{alert_id}')
    def detail(alert_id:str,request:Request,principal=deps):
        tenant,shop,_,can_edit=scoped(principal,request);store=get_store()
        row=next((r for r in decorate(store,tenant,shop,get_rows(tenant,shop)) if r['id']==alert_id),None)
        if not row:raise HTTPException(404,'Scoped alert not found')
        with store._conn() as conn:
            history=execute(store,conn,'SELECT previous_status,new_status,actor,reason,occurred_at FROM alert_operator_history WHERE tenant_id=:tenant AND shop_id=:shop AND alert_id=:alert ORDER BY occurred_at,id',{'tenant':tenant,'shop':shop,'alert':alert_id}).fetchall()
        row['history']=[dict(zip(('previous_status','new_status','actor','reason','occurred_at'),r)) for r in history]
        row['can_edit']=can_edit
        row['notifications']=store.notification_delivery_status(tenant,shop,row['event_id']) if row.get('event_id') and hasattr(store,'notification_delivery_status') else {'status':'UNAVAILABLE'}
        return row

    @app.post(prefix+'/{alert_id}/action')
    def mutate(alert_id:str,body:AlertAction,request:Request,principal=deps):
        tenant,shop,actor,can_edit=scoped(principal,request)
        if not can_edit:raise HTTPException(403,'Alert management permission required')
        row=next((r for r in get_rows(tenant,shop) if r['id']==alert_id),None)
        if not row:raise HTTPException(404,'Scoped alert not found')
        return action(get_store(),tenant,shop,row,body,actor)
