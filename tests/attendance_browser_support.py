"""Opt-in real Chrome checks of the shared workspace against real loopback APIs."""
import os
import socket
import threading
import time
from pathlib import Path
import pytest
import uvicorn
from fastapi.responses import HTMLResponse


def verify_workspace_browser(app,base,label,employee):
    if os.getenv('CAMERA_EYE_WORKSPACE_BROWSER')!='1':
        pytest.skip('Set CAMERA_EYE_WORKSPACE_BROWSER=1 for installed Chrome acceptance')
    from playwright.sync_api import sync_playwright
    route='/__attendance_workspace_acceptance_'+label
    @app.get(route,include_in_schema=False)
    def isolated_page():
        return HTMLResponse('<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<link rel="stylesheet" href="/attendance-assets/workspace.css"><main id="workspace"></main>'
            '<script src="/attendance-assets/workspace.js"></script><script>'
            'AttendanceWorkspace.mount(document.getElementById("workspace"),{base:"'+base+'",peopleEndpoint:"'+base+'/personnel"});</script>')
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='error'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[listener]},daemon=True);thread.start()
    deadline=time.monotonic()+10
    while not server.started and time.monotonic()<deadline:time.sleep(.05)
    assert server.started
    home=Path(__file__).resolve().parents[1]/'artifacts'/'attendance-workspace-browser';home.mkdir(parents=True,exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe')
            page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{port}'+route)
            page.get_by_text('1 matching sessions',exact=True).wait_for()
            assert page.get_by_role('table').locator('tbody tr').filter(has_text=employee).count()==1
            page.wait_for_function('document.querySelector("select[name=person_id]").options.length > 1')
            page.get_by_label('Employee',exact=True).select_option(index=1)
            page.get_by_role('button',name='Apply filters',exact=True).click()
            page.get_by_text('1 matching sessions',exact=True).wait_for()
            page.get_by_label('Date range',exact=True).select_option('yesterday')
            page.get_by_role('button',name='Apply filters',exact=True).click()
            page.get_by_text('0 matching sessions',exact=True).wait_for()
            page.get_by_label('Date range',exact=True).select_option('today')
            page.get_by_role('button',name='Apply filters',exact=True).click()
            page.get_by_text('1 matching sessions',exact=True).wait_for()
            page.get_by_role('button',name='View activity',exact=True).click()
            page.locator('.aw-event').first.wait_for()
            assert page.locator('.aw-event').count()==4
            if label=='local':
                assert page.get_by_role('button',name='Image 1',exact=True).count()==1
                page.get_by_role('button',name='Image 1',exact=True).click()
                page.wait_for_function('document.querySelector("img.aw-preview")?.naturalWidth > 0')
                page.get_by_role('button',name='Close evidence',exact=True).click()
                page.get_by_role('button',name='Video clip',exact=True).click()
                page.wait_for_function('document.querySelector("video.aw-preview")?.readyState >= 2')
                assert page.locator('video.aw-preview').evaluate('(v)=>v.duration')>0
                page.get_by_role('button',name='Close evidence',exact=True).click()
            page.screenshot(path=str(home/(label+'-desktop.png')),full_page=True)
            page.get_by_label('Employee name or code').fill('NO-SUCH-SYNTHETIC-PERSON')
            page.get_by_role('button',name='Apply filters',exact=True).click()
            page.get_by_text('0 matching sessions',exact=True).wait_for()
            page.get_by_role('button',name='Clear',exact=True).click()
            page.get_by_text('1 matching sessions',exact=True).wait_for()
            with page.expect_download() as download:
                page.get_by_role('button',name='Export filtered CSV',exact=True).click()
            download.value.save_as(str(home/(label+'-filtered.csv')))
            page.set_viewport_size({'width':390,'height':844})
            page.screenshot(path=str(home/(label+'-mobile.png')),full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert not errors,errors
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=10);listener.close()
        app.router.routes[:]=[r for r in app.router.routes if getattr(r,'path',None)!=route]
