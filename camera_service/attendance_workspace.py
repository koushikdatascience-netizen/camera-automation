"""Shared read model built from explicit attendance events, never CRM mutations."""
from __future__ import annotations
import csv
import hashlib
import io
import json
from datetime import date,datetime,time,timedelta,timezone
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel,Field,model_validator
from cloud_portal.policy_resolution import resolve_attendance_policy
from camera_service.attendance_state import STATES,session_state

UTC=timezone.utc
EVENTS={'ATTENDANCE_ENTRY','ATTENDANCE_EXIT','BREAK_START','BREAK_END','CHECK_IN','CHECK_OUT','GRACE_EXCEEDED',
        'AUTO_LOGIN','AUTO_LOGOUT','LOGIN','LOGOUT','BREAK_IN','BREAK_OUT','GRACE_PERIOD_EXPIRED','MANUAL_CORRECTION',
        'MANUAL_CHECK_IN','MANUAL_CHECK_OUT','MANUAL_START_BREAK','MANUAL_END_BREAK'}
LABELS={'MANUAL_CHECK_IN':'LOGIN','MANUAL_CHECK_OUT':'LOGOUT','MANUAL_START_BREAK':'BREAK_IN','MANUAL_END_BREAK':'BREAK_OUT',
        'BREAK_START':'BREAK_IN','BREAK_END':'BREAK_OUT','CHECK_IN':'LOGIN','CHECK_OUT':'LOGOUT'}


def instant(value):
    if isinstance(value,datetime): result=value
    elif value: result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    else:return None
    if result.tzinfo is None:raise ValueError('Legacy timestamp has no timezone; needs review')
    return result.astimezone(UTC)


class WorkspaceQuery(BaseModel):
    preset:Literal['today','yesterday','last7','month','custom']='today'
    start:date|None=None
    end:date|None=None
    timezone:str='Asia/Kolkata'
    search:str=Field(default='',max_length=128)
    person_id:str|None=None
    session_id:str|None=None
    full_session:bool=False
    shop_id:str|None=None
    camera_id:str|None=None
    activity:str|None=None
    status:Literal['OUT','IN','ON_BREAK','PENDING_AUTO_LOGOUT','NEEDS_REVIEW']|None=None
    source:Literal['MANUAL','AUTO','CRM','UNKNOWN']|None=None
    page:int=Field(default=1,ge=1)
    page_size:int=Field(default=25,ge=1,le=100)
    sort:Literal['login','employee','worked']='login'
    direction:Literal['asc','desc']='desc'

    @model_validator(mode='after')
    def check(self):
        ZoneInfo(self.timezone)
        if self.full_session and not self.session_id:raise ValueError('Complete timeline requires one scoped session')
        if self.preset=='custom' and (not self.start or not self.end):raise ValueError('Custom dates are required')
        if self.start and self.end and (self.end<self.start or (self.end-self.start).days>366):raise ValueError('Date range must be ordered and at most 367 days')
        return self

    def window(self,now):
        zone=ZoneInfo(self.timezone);today=now.astimezone(zone).date()
        begin,finish={'today':(today,today),'yesterday':(today-timedelta(days=1),today-timedelta(days=1)),
            'last7':(today-timedelta(days=6),today),'month':(today.replace(day=1),today),
            'custom':(self.start,self.end)}[self.preset]
        return datetime.combine(begin,time.min,zone).astimezone(UTC),datetime.combine(finish+timedelta(days=1),time.min,zone).astimezone(UTC)


def policy_snapshot(policy):
    # Only attendance rules belong in event snapshots: never recipients, credentials or tokens.
    values=resolve_attendance_policy({},policy or {}).values
    safe={k:v for k,v in values.items() if k not in {'email_recipients','whatsapp_recipients'}}
    safe.update({k:(policy or {}).get(k) for k in ('shift_start_time','late_grace_minutes','scheduled_weekdays','overtime_enabled')})
    safe['absenceAutoLogoutEnabled']=bool((policy or {}).get('absence_auto_logout_enabled',False))
    return {'id':hashlib.sha256(json.dumps(safe,sort_keys=True).encode()).hexdigest()[:20],'values':safe}


def normalize_event(row):
    payload=row.get('payload') or {}
    if isinstance(payload,str):payload=json.loads(payload)
    if isinstance(payload.get('payload'),dict):payload=payload['payload']
    metadata=row.get('metadata') or row.get('metadata_json') or payload.get('metadata') or {}
    if isinstance(metadata,str):metadata=json.loads(metadata)
    raw=row.get('event_type') or row.get('activity_type') or payload.get('event_type')
    if raw not in EVENTS:return None
    source=str(metadata.get('attendance_source') or metadata.get('source') or row.get('source') or 'UNKNOWN').upper()
    source='AUTO' if source in {'RECOGNITION','CAMERA','CAMERA_EYE','AUTO'} or raw.startswith('AUTO_') else 'MANUAL' if raw.startswith('MANUAL_') or 'STATION' in source or source=='MANUAL' else 'CRM' if 'CRM' in source else 'UNKNOWN'
    label=LABELS.get(raw,raw)
    if raw=='GRACE_EXCEEDED':label='GRACE_PERIOD_EXPIRED'
    if raw=='ATTENDANCE_ENTRY':label='AUTO_LOGIN' if source=='AUTO' else 'LOGIN' if source=='MANUAL' else 'LEGACY_LOGIN'
    if raw=='ATTENDANCE_EXIT':label='AUTO_LOGOUT' if source=='AUTO' else 'LOGOUT' if source=='MANUAL' else 'LEGACY_LOGOUT'
    return {'event_id':str(row.get('id') or payload.get('event_id')),'person_id':str(row.get('person_id') or payload.get('person_id') or ''),
        'session_id':metadata.get('attendance_session_id'),'timestamp':row.get('event_time') or row.get('occurred_at') or payload.get('event_time'),
        'activity':label,'original_type':raw,'source':source,'camera_id':row.get('camera_id') or payload.get('camera_id'),
        'shop_id':row.get('store_id') or row.get('shop_id') or payload.get('store_id'),
        'reason':metadata.get('reason') or metadata.get('remarks') or row.get('reason_code') or '', 'confidence':metadata.get('confidence'),
        'previous_state':metadata.get('prior_state') or metadata.get('state_before'),
        'new_state':metadata.get('new_state') or metadata.get('state_after'),
        'processing_status':row.get('processing_status') or row.get('delivery_status') or row.get('sync_status') or 'UNKNOWN',
        'crm_message':row.get('crm_message') or metadata.get('crm_response_message'),
        'crm_error':row.get('crm_error') or row.get('last_error') or metadata.get('crm_error'),
        'policy_applied':metadata.get('attendance_policy_snapshot') or None,'metadata':metadata}


def build_workspace(people,sessions,events,query:WorkspaceQuery,*,now=None,evidence=None,policies=None):
    now=now or datetime.now(UTC);start,end=query.window(now);zone=ZoneInfo(query.timezone)
    policies=policies or {};identities={str(p['id']):p for p in people};normalized={}
    warnings=[]
    for row in events:
        event=normalize_event(row)
        if event and event['person_id'] in identities:
            try:at=instant(event['timestamp'])
            except (ValueError,TypeError):warnings.append('An event with an unzoned/invalid timestamp requires review');continue
            if not at:continue
            event['_at']=at;event['timestamp']=at.astimezone(zone).isoformat();event['timezone']=query.timezone
            normalized[event['event_id']]=event
    ordered=sorted(normalized.values(),key=lambda e:(e['_at'],e['event_id']))
    rows={str(s['id']):dict(s) for s in sessions if str(s['person_id']) in identities}
    current={};unpaired={}
    for event in ordered:
        pid=event['person_id'];sid=event['session_id'];action=event['activity']
        if action in {'LOGIN','AUTO_LOGIN','LEGACY_LOGIN'}:
            sid=sid or event['event_id'];unpaired[pid]=sid
        elif not sid:sid=unpaired.get(pid)
        event['session_id']=sid
        previous=current.get(pid)
        new={'LOGIN':'IN','AUTO_LOGIN':'IN','LEGACY_LOGIN':'IN','LOGOUT':'OUT','AUTO_LOGOUT':'OUT','LEGACY_LOGOUT':'OUT',
             'BREAK_IN':'ON_BREAK','BREAK_OUT':'IN'}.get(action)
        event['previous_state']=event['previous_state'] or previous or 'UNKNOWN'
        event['new_state']=event['new_state'] or new or previous or 'UNKNOWN'
        if new:current[pid]=new
        if sid and sid not in rows:
            rows[sid]=dict(id=sid,person_id=pid,store_id=event['shop_id'],entry_confirmed=0,arrival_time=None,exit_time=None)
        if sid:
            session=rows[sid]
            if action in {'LOGIN','AUTO_LOGIN','LEGACY_LOGIN'}:
                session.update(arrival_time=event['_at'].isoformat(),entry_confirmed=1,arrival_camera=event['camera_id'])
            elif action in {'LOGOUT','AUTO_LOGOUT','LEGACY_LOGOUT'}:
                session.update(exit_time=event['_at'].isoformat(),break_started_at=None,exit_camera=event['camera_id'])
                unpaired.pop(pid,None)
            elif action=='BREAK_IN':session['break_started_at']=event['_at'].isoformat()
            elif action=='BREAK_OUT':session['break_started_at']=None
    selected=[];timelines=[];daily={};daily_targets={};eligible_people=set();late=set();late_determined=False
    for person in people:
        pid=str(person['id']);haystack=(str(person.get('full_name',''))+' '+str(person.get('employee_code',''))).casefold()
        if query.person_id and query.person_id!=pid or query.search.casefold() not in haystack:continue
        eligible_people.add(pid)
    for sid,session in rows.items():
        pid=str(session['person_id'])
        if pid not in eligible_people or query.session_id and query.session_id!=sid:continue
        person=identities[pid];shop=str(session.get('store_id') or '')
        if query.shop_id and query.shop_id!=shop:continue
        history=[e for e in ordered if e['session_id']==sid and e['person_id']==pid]
        if query.camera_id and query.camera_id not in {session.get('arrival_camera'),session.get('exit_camera'),*[e['camera_id'] for e in history]}:continue
        try:login=instant(session.get('arrival_time'));logout=instant(session.get('exit_time'))
        except (ValueError,TypeError):warnings.append('A legacy session with an unzoned timestamp requires review');continue
        if not login or login>=end or (logout or now)<=start or not session.get('entry_confirmed'):continue
        visible=history if query.full_session else [e for e in history if start<=e['_at']<end]
        if query.activity and not any(e['activity']==query.activity for e in visible):continue
        if query.source and not any(e['source']==query.source for e in visible):continue
        state=session_state(session)
        if visible and visible[-1]['new_state']=='PENDING_AUTO_LOGOUT' and not logout:state='PENDING_AUTO_LOGOUT'
        receipt_review=any(e['processing_status']=='RECONCILIATION_REQUIRED' for e in history)
        if receipt_review:state='NEEDS_REVIEW'
        if query.status and state!=query.status:continue
        snapshot=next((e['policy_applied'] for e in history if e['policy_applied']),None)
        applied=(snapshot or policy_snapshot(policies.get(pid,{})))
        policy=applied.get('values') or {};required=int(policy.get('requiredWorkingMinutes',480))
        if policy.get('shift_start_time'):
            late_determined=True
            hh,mm=map(int,policy['shift_start_time'].split(':'))
            local_login=login.astimezone(ZoneInfo(policy.get('timezone',query.timezone)))
            expected=datetime.combine(local_login.date(),time(hh,mm),local_login.tzinfo)+timedelta(minutes=int(policy.get('late_grace_minutes') or 0))
            if local_login>expected:late.add((pid,local_login.date()))
        left=max(login,start);right=min(logout or now,end,now)
        break_at=None;breaks=[]
        for event in history:
            if event['activity']=='BREAK_IN' and break_at is None:break_at=event['_at']
            if event['activity']=='BREAK_OUT' and break_at is not None:
                breaks.append((break_at,event['_at']));break_at=None
        if break_at:breaks.append((break_at,logout or now))
        gross=max(0,(right-left).total_seconds())
        broken=sum(max(0,(min(b,right)-max(a,left)).total_seconds()) for a,b in breaks)
        worked=max(0,gross-broken);work_minutes=worked/60;break_minutes=min(gross,broken)/60
        cursor=left
        while cursor<right:
            tomorrow=datetime.combine(cursor.astimezone(zone).date()+timedelta(days=1),time.min,zone).astimezone(UTC)
            finish=min(right,tomorrow);pause=sum(max(0,(min(b,finish)-max(a,cursor)).total_seconds()) for a,b in breaks)
            key=(pid,cursor.astimezone(zone).date())
            daily_targets[key]=required if policy.get('overtime_enabled') is not False else float('inf')
            daily[key]=daily.get(key,0)+max(0,(finish-cursor).total_seconds()-pause)/60;cursor=finish
        assets=[]
        for event in history:
            manifest=evidence(event) if evidence else {'status':'UNAVAILABLE','images':[],'video':None}
            event['evidence']=manifest;assets.append(manifest)
            event['employee_name']=person.get('full_name');event['employee_code']=person.get('employee_code')
            event['shop_id']=event['shop_id'] or shop
        violations=[]
        if break_minutes>int(policy.get('allowedBreakMinutes',60)):violations.append('BREAK_LIMIT_EXCEEDED')
        if not logout and now.astimezone(ZoneInfo(policy.get('timezone',query.timezone))).strftime('%H:%M')>str(policy.get('maxLogoffTime','23:59')):violations.append('MAX_LOGOFF_PASSED')
        if receipt_review:violations.append('CRM_RECONCILIATION_REQUIRED')
        if not snapshot:violations.append('HISTORICAL_POLICY_UNKNOWN')
        selected.append({'session_id':sid,'person_id':pid,'employee_name':person.get('full_name'),'employee_code':person.get('employee_code'),
            'photo_url':person.get('photo_url'),'shop_id':shop,'login':login.astimezone(zone).isoformat(),
            'logout':logout.astimezone(zone).isoformat() if logout else None,'timezone':query.timezone,'status':state,'status_label':STATES[state],
            'worked_minutes':round(work_minutes,2),'break_minutes':round(break_minutes,2),
            'overtime_minutes':None,'policy':applied,'policy_is_historical':bool(snapshot),'compliance':violations or ['WITHIN_KNOWN_RULES'],
            'logout_reason':next((e['reason'] for e in reversed(history) if e['activity'] in {'LOGOUT','AUTO_LOGOUT','LEGACY_LOGOUT'}),None),
            'evidence_status':'AVAILABLE' if any(a.get('images') or a.get('video') for a in assets) else 'Evidence unavailable',
            'activity_count':len(history),'source':next((e['source'] for e in history if e['activity'] in {'LOGIN','AUTO_LOGIN','LEGACY_LOGIN'}),'UNKNOWN')})
        # Timeline respects the same date/action/source filters as the report.
        timelines.extend(e for e in visible if (not query.activity or e['activity']==query.activity) and (not query.source or e['source']==query.source))
    overtime=sum(max(0,minutes-daily_targets[key]) for key,minutes in daily.items())
    for row in selected:
        row['overtime_minutes']=round(sum(max(0,minutes-daily_targets[key]) for key,minutes in daily.items() if key[0]==row['person_id']),2)
        row['overtime_scope']='EMPLOYEE_DATE_RANGE'  # Never multiply overtime across sessions in summaries.
    scheduled={pid for pid in eligible_people if any(bool(policies.get(pid,{}).get('scheduled_weekdays')) and day.weekday() in policies[pid]['scheduled_weekdays']
        for day in [start.astimezone(zone).date()+timedelta(days=i) for i in range((end.astimezone(zone).date()-start.astimezone(zone).date()).days)])}
    attended={r['person_id'] for r in selected}
    sort_key={'login':'login','employee':'employee_name','worked':'worked_minutes'}[query.sort]
    selected.sort(key=lambda r:(r.get(sort_key) or (0 if query.sort=='worked' else ''),r['session_id']),reverse=query.direction=='desc')
    total=len(selected);offset=(query.page-1)*query.page_size
    for event in timelines:event.pop('metadata',None);event.pop('_at',None)
    active={r['person_id'] for r in selected if r['status'] in {'IN','ON_BREAK','PENDING_AUTO_LOGOUT'}}
    return {'items':selected[offset:offset+query.page_size],'total':total,'page':query.page,'page_size':query.page_size,
        'summary':{'total_employees':len(eligible_people),'present_employees':len(active),
            'absent_employees':len(scheduled-attended) if scheduled and end<=now and not any((query.activity,query.source,query.camera_id,query.status)) else None,'no_recorded_login':len(eligible_people-attended),
            'on_break':len({r['person_id'] for r in selected if r['status']=='ON_BREAK'}),'late_arrivals':len(late) if late_determined else None,
            'worked_minutes':round(sum(r['worked_minutes'] for r in selected),2),'overtime_minutes':round(overtime,2),
            'needs_attention':len({r['person_id'] for r in selected if r['compliance']!=['WITHIN_KNOWN_RULES']})},
        'summary_notes':['Absence and lateness require a verified work calendar and shift start; no recorded login is not an absence determination.'],
        'events':sorted(timelines,key=lambda e:(e['timestamp'],e['event_id'])),
        'warnings':sorted(set(warnings)),'range':{'start':start.isoformat(),'end_exclusive':end.isoformat(),'timezone':query.timezone},
        '_all_items':selected}


EXPORT_FIELDS=('employee_name','employee_code','shop_id','session_id','login','logout','timezone','status_label','worked_minutes','break_minutes','source','evidence_status')


def export_csv(rows):
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=EXPORT_FIELDS);writer.writeheader()
    for row in rows:
        values={key:row.get(key) for key in EXPORT_FIELDS}
        for key,value in values.items():
            if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):values[key]="'"+value
        writer.writerow(values)
    return stream.getvalue().encode('utf-8-sig')
