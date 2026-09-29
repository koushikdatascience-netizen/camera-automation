// Isolated browser smoke check; never uses customer ProgramData or production.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const root = path.resolve(__dirname, '..');
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'camera-eye-browser-'));
const env = {...process.env, CAMERA_AUTOMATION_HOME:home,
  CAMERA_AUTOMATION_CONFIG:path.join(home,'missing.yaml'), SNAPKEY_PORTAL_DB:path.join(home,'portal.db'),
  SNAPKEY_ENV:'development', SNAPKEY_CRM_INTEGRATION_KEY:'isolated-browser-test-key', SNAPKEY_DATABASE_URL:''};
const children = [];
function server(module, port) {
  const child = spawn(path.join(root,'.venv311','Scripts','python.exe'),
    ['-m','uvicorn',module,'--host','127.0.0.1','--port',String(port)], {cwd:root,env,windowsHide:true,stdio:'ignore'});
  children.push(child);
}
async function wait(url) {
  for(let n=0;n<60;n++){
    try{if((await fetch(url,{signal:AbortSignal.timeout(2000)})).ok)return;}catch{}
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  throw new Error('Server failed to start: '+url);
}
async function json(url, body, headers={}) {
  const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
  if(!response.ok)throw new Error('Request failed: '+url+' '+response.status);
  return response.json();
}
(async()=>{
  let browser;
  try{
    server('camera_service.api:app',8099);server('cloud_portal.api:app',8100);
    await wait('http://127.0.0.1:8099/ready');await wait('http://127.0.0.1:8100/health');
    await json('http://127.0.0.1:8099/api/v1/cameras',{camera_id:'video-1',name:'Video Test',source_type:'file',rtsp_url:path.join(root,'test.mp4'),tracking_mode:'track',features:{}});
    await json('http://127.0.0.1:8099/api/v1/cameras',{camera_id:'offline',name:'Offline Camera',rtsp_url:'0',enabled:false,features:{}});
    const session=await json('http://127.0.0.1:8100/crm/session',{tenantId:'test-tenant',shopCode:'test-shop',userId:'test-owner',role:'OWNER'}, {'X-CRM-Integration-Key':'isolated-browser-test-key'});
    browser=await chromium.launch({
      headless: true,
      executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
    });
    const page=await browser.newPage({viewport:{width:1440,height:1000}});
    const errors=[];page.on('pageerror',error=>errors.push(error.message));
    await page.goto('http://127.0.0.1:8099/setup');
    await page.waitForFunction(()=>document.querySelector('#overview-section')?.classList.contains('active') && document.querySelectorAll('#operator-camera-tiles [data-camera-id]').length===2,{},{timeout:30000});
    await page.waitForFunction(()=>document.querySelector('#operator-records-body')?.textContent.includes('No attendance records'));
    await page.locator('#operator-alerts-tab').click();
    await page.waitForFunction(()=>document.querySelector('#operator-records-body')?.textContent.includes('No alerts'));
    await page.screenshot({path:path.join(home,'edge-overview-desktop.png'),fullPage:true});
    await page.locator('nav a[data-section="live"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('#focus-camera-list button').length===2,{},{timeout:30000});
    await page.waitForFunction(()=>document.querySelector('#focus-camera-image')?.naturalWidth>0,{},{timeout:45000});
    if(await page.locator('#live-layout-controls').isVisible())throw new Error('Wall controls visible in Focus');
    if(!await page.locator('#focus-camera-capture').isEnabled())throw new Error('Capture unavailable for online camera');
    await page.screenshot({path:path.join(home,'edge-focus-desktop.png'),fullPage:true});
    await page.locator('#live-wall-tab').click();
    if(!await page.locator('#live-layout-controls').isVisible())throw new Error('Wall controls hidden in Wall');
    await page.waitForFunction(()=>document.querySelectorAll('#live-camera-grid .live-tile').length===2,{},{timeout:30000});
    await page.waitForFunction(()=>[...document.querySelectorAll('#live-camera-grid img')].some(img=>img.naturalWidth>0),{},{timeout:45000});
    await page.screenshot({path:path.join(home,'edge-live-desktop.png'),fullPage:true});
    await page.locator('#live-overlay-btn').click();
    await page.waitForFunction(()=>document.querySelector('#live-camera-grid img')?.getAttribute('src')?.includes('/stream'));
    await page.locator('nav a[data-section="cameras"]').click();
    await page.waitForFunction(()=>document.querySelectorAll('#cameras-container .camera-card').length===2);
    await page.screenshot({path:path.join(home,'edge-camera-setup-desktop.png'),fullPage:true});
    if(await page.locator('#live-camera-grid img[src]').count())throw new Error('Hidden live images retained streams');
    if(await page.locator('#focus-camera-image[src]').count())throw new Error('Hidden focus image retained stream');
    await page.setViewportSize({width:390,height:844});
    await page.locator('nav a[data-section="overview"]').click();
    await page.waitForFunction(()=>document.querySelector('#overview-section')?.classList.contains('active'));
    await page.screenshot({path:path.join(home,'edge-overview-mobile.png'),fullPage:true});
    await page.locator('nav a[data-section="live"]').click();
    await page.locator('#live-focus-tab').click();
    await page.waitForFunction(()=>document.querySelector('#focus-camera-image')?.naturalWidth>0,{},{timeout:45000});
    await page.screenshot({path:path.join(home,'edge-focus-mobile.png'),fullPage:true});
    await page.locator('#live-wall-tab').click();
    await page.waitForFunction(()=>[...document.querySelectorAll('#live-camera-grid img')].some(img=>img.naturalWidth>0),{},{timeout:45000});
    await page.screenshot({path:path.join(home,'edge-live-mobile.png'),fullPage:true});
    await page.goto(session.launchUrl.startsWith('/')?'http://127.0.0.1:8100'+session.launchUrl:session.launchUrl);
    await page.setViewportSize({width:1440,height:1000});
    await page.waitForFunction(()=>document.querySelector('#configured-cameras')?.textContent.trim()==='0',{},{timeout:15000});
    await page.locator('#overview-alerts-tab').click();
    await page.waitForFunction(()=>document.querySelector('#overview-activity-list')?.textContent.includes('No alerts'));
    await page.screenshot({path:path.join(home,'cloud-overview-desktop.png'),fullPage:true});
    await page.goto('http://127.0.0.1:8100/portal/alerts.html');
    await page.waitForFunction(()=>document.querySelector('#alerts-body')?.textContent.includes('No alerts'));
    await page.setViewportSize({width:1440,height:1000});
    await page.screenshot({path:path.join(home,'cloud-alerts-desktop.png'),fullPage:true});
    if(errors.length)throw new Error(errors.join('; '));
    console.log(JSON.stringify({passed:true,pageErrors:errors,screenshots:home},null,2));
  }finally{
    if(browser)await browser.close();
    for(const child of children)child.kill();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
