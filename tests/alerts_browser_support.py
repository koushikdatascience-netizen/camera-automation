"""Installed Chrome checks against real loopback alert APIs and synthetic events."""
import os,socket,threading,time
from pathlib import Path
import pytest,uvicorn
from fastapi.responses import HTMLResponse


def verify_alerts_browser(app,base,label,filter_value):
    if os.getenv('CAMERA_EYE_WORKSPACE_BROWSER')!='1':pytest.skip('Set CAMERA_EYE_WORKSPACE_BROWSER=1 to run installed Chrome alert acceptance')
    from playwright.sync_api import sync_playwright
    route='/__alerts_acceptance_'+label
    @app.get(route,include_in_schema=False)
    def page():return HTMLResponse('<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/attendance-assets/workspace.css"><main id="alerts"></main><script src="/attendance-assets/alerts.js"></script><script>AlertsWorkspace.mount(document.getElementById("alerts"),{base:"'+base+'"});</script>')
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:time.sleep(.05)
    assert server.started
    folder=Path(__file__).resolve().parents[1]/'artifacts'/'attendance-alerts-browser';folder.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
            page=browser.new_page(viewport={'width':1440,'height':950});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}'+route)
            page.get_by_text('1 matching alerts',exact=True).wait_for()
            page.screenshot(path=str(folder/(label+'-desktop.png')),full_page=True)
            page.get_by_label('Original alert type').fill(filter_value);page.get_by_role('button',name='Apply filters').click()
            page.get_by_text('1 matching alerts',exact=True).wait_for()
            page.get_by_role('button',name='View details').click();page.get_by_text('Acknowledgment and resolution history').wait_for()
            if label=='local':
                page.get_by_role('button',name='Image 1').click();page.wait_for_function('document.querySelector("img.aw-preview")?.naturalWidth>0')
                page.get_by_role('button',name='Video clip').click();page.wait_for_function('document.querySelector("video.aw-preview")?.readyState>=2')
                assert page.locator('video.aw-preview').evaluate('(v)=>v.duration')>0
            page.get_by_label('Action reason').fill('Synthetic browser review')
            with page.expect_response(lambda response: response.url.endswith('/action')) as acknowledged:
                page.get_by_role('button',name='Acknowledge').click()
            assert acknowledged.value.status==200 and acknowledged.value.json()['status']=='ACKNOWLEDGED'
            page.wait_for_function("document.querySelector('section')?.innerText.includes('Status: ACKNOWLEDGED')")
            page.get_by_label('Action reason').fill('Synthetic browser resolved')
            with page.expect_response(lambda response: response.url.endswith('/action')) as resolved:
                page.get_by_role('button',name='Resolve').click()
            assert resolved.value.status==200 and resolved.value.json()['status']=='RESOLVED', f"resolve response: {resolved.value.status} {resolved.value.text()}"
            page.wait_for_function("document.querySelector('section')?.innerText.includes('Status: RESOLVED')")
            page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(folder/(label+'-mobile.png')),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            assert not errors,errors;browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);listener.close()
        app.router.routes[:]=[r for r in app.router.routes if getattr(r,'path',None)!=route]
