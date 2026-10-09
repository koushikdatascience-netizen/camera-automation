"""Run the actual cloud action handler with simulated HTTP outcomes; no CRM calls."""
import shutil
import subprocess
from pathlib import Path
import pytest


def test_cloud_ui_does_not_confirm_http_200_failed_crm_delivery():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for frontend handler regression')
    script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync('cloud_portal/static/js/main.js','utf8');
const handler=source.slice(source.indexOf('async function confirmAttendanceStationAction('),source.indexOf('function wireAttendanceStation()'));
const messages=[];let reloads=0;
const context={attendanceStationCandidate:{recognition_event_id:'synthetic',full_name:'Synthetic Employee'},
document:{getElementById:()=>({value:'edge::camera'}),querySelectorAll:()=>[]},
requireScope:()=>({tenant_id:'tenant'}),authFetch:async()=>({ok:true,json:async()=>({ok:false,delivery:'RECONCILIATION_REQUIRED'})}),
showMessage:(message,error)=>messages.push({message,error}),renderStationCandidate:()=>{},
loadAttendance:async()=>{reloads++;},pollAttendanceStation:async()=>{},encodeURIComponent};
vm.createContext(context);vm.runInContext(handler,context);
vm.runInContext('confirmAttendanceStationAction("CHECK_IN")',context).then(()=>{
assert.equal(reloads,0);assert.equal(messages.length,1);assert.equal(messages[0].error,true);
assert.ok(messages[0].message.includes('RECONCILIATION_REQUIRED'));assert.ok(!messages[0].message.includes('confirmed for'));
}).catch(error=>{console.error(error);process.exitCode=1;});
'''
    result=subprocess.run([node,'-e',script],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
