(() => {
  const params = new URLSearchParams(window.location.search);
  const scopeKeys = ["tenant_id", "company_code", "shop_id", "site_id", "edge_id"];
  function portalToken() {
    const hash=new URLSearchParams(window.location.hash.replace(/^#/,""));
    const incoming=hash.get("session");
    if(incoming){sessionStorage.setItem("snapkey_portal_session",incoming);history.replaceState(null,"",window.location.pathname+window.location.search);}
    return incoming || sessionStorage.getItem("snapkey_portal_session") || "";
  }
  async function authFetch(url, options={}) {
    const token=portalToken();
    const headers=new Headers(options.headers||{});
    if(token) headers.set("Authorization","Bearer "+token);
    const timeoutMs=options.timeoutMs || 20000;
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),timeoutMs);
    try{
      const cleanOptions={...options,headers,signal:controller.signal};
      delete cleanOptions.timeoutMs;
      return await fetch(url,cleanOptions);
    }finally{
      clearTimeout(timer);
    }
  }
  function hydratePortalIdentity() {
    const name=sessionStorage.getItem("snapkey_display_name") || "Camera Eye User";
    const role=sessionStorage.getItem("snapkey_role") || "User";
    document.querySelectorAll(".profile-info strong").forEach(node=>node.textContent=name);
    document.querySelectorAll(".profile-info span").forEach(node=>node.textContent=role);
    const initials=name.split(/\s+/).filter(Boolean).slice(0,2).map(part=>part[0]?.toUpperCase()||"").join("") || "CE";
    document.querySelectorAll(".avatar").forEach(node=>node.textContent=initials);
    document.querySelectorAll(".header-date").forEach(node=>node.textContent=new Intl.DateTimeFormat(undefined,{weekday:"long",year:"numeric",month:"long",day:"numeric"}).format(new Date()));
  }

  async function bootstrapCrmSession() {
    const token=portalToken();
    if(!token){ window.location.replace("/login"); throw new Error("Login required."); }
    const response=await authFetch("/session/status");
    if(!response.ok){ sessionStorage.removeItem("snapkey_portal_session"); window.location.replace("/login"); throw new Error("Your Camera Eye session is invalid or expired."); }
    const session=await response.json();
    const values={tenant_id:session.tenantId,company_code:session.companyCode,shop_id:session.shopCode};
    Object.entries(values).forEach(([key,value])=>{if(value) sessionStorage.setItem("snapkey_"+key,String(value));});
    if(session.displayName) sessionStorage.setItem("snapkey_display_name",session.displayName);
    if(session.role) sessionStorage.setItem("snapkey_role",session.role);
    hydratePortalIdentity();
  }

  function portalScope() {
    const scope = {};
    for (const key of scopeKeys) {
      const value = params.get(key) || sessionStorage.getItem("snapkey_" + key) || "";
      if (value) {
        scope[key] = value;
        sessionStorage.setItem("snapkey_" + key, value);
      }
    }
    return scope;
  }

  function requireScope(keys) {
    const scope = portalScope();
    const missing = keys.filter(key => !scope[key]);
    if (missing.length) {
      throw new Error("Portal scope is not configured: " + missing.join(", ") + ". Sign in to Camera Eye again.");
    }
    return scope;
  }

  function setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  }

  function showMessage(message, isError = false) {
    let node = document.getElementById("portal-message");
    if (!node) {
      node = document.createElement("div");
      node.id = "portal-message";
      node.style.margin = "0 0 16px";
      node.style.padding = "12px 14px";
      node.style.borderRadius = "8px";
      const content = document.querySelector(".content");
      if (content) content.prepend(node);
    }
    node.style.background = isError ? "#fff1f2" : "#ecfdf5";
    node.style.color = isError ? "#b91c1c" : "#166534";
    node.textContent = message;
  }

  function edgeOnline(edge) {
    const stamp = Date.parse(edge.received_at || edge.last_seen_at || "");
    return Number.isFinite(stamp) && (Date.now() - stamp) < 90000;
  }

  function selectActiveEdge(edges, preferredEdgeId="") {
    if (!edges.length) return null;
    const preferred = edges.find(edge => edge.edge_id === preferredEdgeId);
    if (preferred && edgeOnline(preferred)) return preferred;
    return edges.find(edge => edgeOnline(edge)) || preferred || edges[0];
  }

  function rememberActiveEdge(edge) {
    if (!edge) return;
    sessionStorage.setItem("snapkey_edge_id", edge.edge_id);
    if (edge.site_id) sessionStorage.setItem("snapkey_site_id", edge.site_id);
  }

  function renderEdges(edges) {
    const container=document.getElementById("edge-device-list");
    if(!container) return;
    if(!edges.length){container.innerHTML="<p>No edge device has reported for this shop yet.</p>";return;}
    container.innerHTML=edges.map(edge=>{
      const status=edge.status||{};
      const cameras=status.cameras||[];
      const online=edgeOnline(edge);
      const cameraText=cameras.length
        ? cameras.map(camera=>escapeHtml(camera.name||camera.camera_id)+" ("+(camera.online?"online":"offline")+")").join(", ")
        : "No local cameras reported";
      return "<div style='padding:14px 0;border-bottom:1px solid #e5e7eb'>"+
        "<div style='display:flex;justify-content:space-between;gap:12px'><div><strong>"+escapeHtml(edge.edge_id)+"</strong>"+
        "<br><small>"+escapeHtml(edge.shop_id||"")+" · "+escapeHtml(edge.site_id||"")+"</small></div>"+
        "<div style='display:flex;align-items:center;gap:8px'><strong style='color:"+(online?"#166534":"#b91c1c")+"'>● "+(online?"Online":"Offline")+"</strong>"+
        (!online?"<button class='btn btn-light delete-edge-device' data-edge-id='"+escapeHtml(edge.edge_id)+"' style='padding:6px 10px'>Remove</button>":"")+"</div></div>"+
        "<div style='margin-top:8px'><small>"+cameraText+"</small></div></div>";
    }).join("");
    container.querySelectorAll(".delete-edge-device").forEach(button=>button.addEventListener("click",async()=>{
      const edgeId=button.dataset.edgeId;if(!edgeId||!confirm("Remove this offline edge device? Its credentials will be revoked and it must be paired again to reconnect."))return;
      const scope=requireScope(["tenant_id"]);button.disabled=true;
      try{
        const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edges/"+encodeURIComponent(edgeId),{method:"DELETE"});
        const body=await response.json();if(!response.ok)throw new Error(body.detail||"Unable to remove edge device.");
        if(sessionStorage.getItem("snapkey_edge_id")===edgeId)sessionStorage.removeItem("snapkey_edge_id");
        showMessage("Offline edge device removed.");await loadSystemStatus();
      }catch(error){showMessage(error.message,true);button.disabled=false;}
    }));
  }

  function wireEdgeSetup() {
    const open=document.getElementById("setup-edge-btn");
    const panel=document.getElementById("edge-setup-panel");
    const close=document.getElementById("close-edge-setup-btn");
    const generate=document.getElementById("generate-edge-code-btn");
    if(!open||!panel||!generate) return;
    open.addEventListener("click",()=>{panel.style.display="block";panel.scrollIntoView({behavior:"smooth",block:"nearest"});});
    close?.addEventListener("click",()=>{panel.style.display="none";});
    generate.addEventListener("click",async()=>{
      const result=document.getElementById("edge-activation-result");
      const original=generate.textContent;
      generate.disabled=true; generate.textContent="Generating...";
      try{
        const scope=requireScope(["tenant_id"]);
        const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edge-activation-codes",{
          method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expires_minutes:30})
        });
        const body=await response.json();
        if(!response.ok) throw new Error(body.detail||"Unable to generate activation code.");
        const expires=new Date(body.expiresAt).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
        result.innerHTML="<div style='margin-bottom:14px;padding:16px;border:1px solid #fde68a;border-radius:10px;background:#fffbeb'>"+
          "<div style='font-size:12px;color:#6b7280;margin-bottom:4px'>ONE-TIME ACTIVATION CODE</div>"+
          "<strong style='font-size:24px;letter-spacing:2px'>"+escapeHtml(body.activationCode)+"</strong>"+
          "<div style='margin-top:6px;font-size:12px;color:#6b7280'>Shop: "+escapeHtml(body.shopCode)+" · Expires at "+escapeHtml(expires)+"</div>"+
          "<button class='btn btn-light' id='copy-edge-code-btn' type='button' style='margin-top:10px'>Copy Code</button></div>";
        document.getElementById("copy-edge-code-btn")?.addEventListener("click",async event=>{
          await navigator.clipboard.writeText(body.activationCode);
          event.currentTarget.textContent="Copied";
        });
      }catch(error){result.innerHTML="<p style='color:#b91c1c'>"+escapeHtml(error.message)+"</p>";}
      finally{generate.disabled=false;generate.textContent=original;}
    });
  }

  let overviewActivityMode="attendance";
  let overviewActivityData={attendance:[],alerts:[]};
  function renderOverviewActivity(){
    const list=document.getElementById("overview-activity-list");if(!list)return;
    const items=overviewActivityData[overviewActivityMode]||[];
    list.innerHTML=items.length?items.slice(0,8).map(item=>{
      const label=overviewActivityMode==="attendance"?(item.full_name||"Unknown person")+" · "+String(item.event_type||"Event").replaceAll("_"," "):
        String(item.event_type||"Alert").replaceAll("_"," ");
      const detail=overviewActivityMode==="attendance"?(item.employee_code||item.camera_id||"-"):(item.camera_id||"Camera");
      return "<div class='overview-activity-row'><div><strong>"+escapeHtml(label)+"</strong><small>"+escapeHtml(detail)+"</small></div><small>"+escapeHtml(fmtTime(item.event_time))+"</small></div>";
    }).join(""):"<p>No "+(overviewActivityMode==="attendance"?"attendance records":"alerts")+" received from this shop yet.</p>";
  }
  function wireOverviewActivity(){
    if(!document.getElementById("overview-activity-list"))return;
    for(const [id,mode] of [["overview-attendance-tab","attendance"],["overview-alerts-tab","alerts"]]){
      document.getElementById(id).addEventListener("click",()=>{
        overviewActivityMode=mode;
        document.getElementById("overview-attendance-tab").setAttribute("aria-selected",String(mode==="attendance"));
        document.getElementById("overview-alerts-tab").setAttribute("aria-selected",String(mode==="alerts"));
        renderOverviewActivity();
      });
    }
    loadSystemStatus();setInterval(loadSystemStatus,15000);
  }
  async function loadSystemStatus() {
    if (!document.getElementById("configured-cameras")) return;
    try {
      const scope = requireScope(["tenant_id"]);
      const [healthResponse, summaryResponse, cameraResponse, eventsResponse, edgeResponse, attendanceResponse] = await Promise.all([
        fetch("/health"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/summary"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/cameras"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/events?limit=500"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/edges"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/attendance")
      ]);
      if (!healthResponse.ok || !summaryResponse.ok || !cameraResponse.ok || !eventsResponse.ok || !edgeResponse.ok || !attendanceResponse.ok) throw new Error("Portal API is unavailable or this session is not authorized.");
      const health = await healthResponse.json();
      await summaryResponse.json();
      const cameras = await cameraResponse.json();
      const events = await eventsResponse.json();
      const edgesBody = await edgeResponse.json();
      const attendance = await attendanceResponse.json();
      const edges=edgesBody.items||[];
      renderEdges(edges);
      const inventory=edges.flatMap(edge=>(edge.status?.cameras||[]).map(camera=>({...camera,edge_id:edge.edge_id})));
      const configuredIds=new Set((cameras.items||[]).map(camera=>camera.edge_id+"::"+camera.camera_id));
      const uniqueInventory=inventory.filter(camera=>!configuredIds.has(camera.edge_id+"::"+camera.camera_id));
      setText("application-status", health.status === "ok" ? "● Online" : "● Degraded");
      setText("database-status", "● Connected");
      setText("configured-cameras", (cameras.items || []).length + uniqueInventory.length);
      setText("online-cameras", inventory.filter(camera=>camera.online).length);
      setText("active-personnel", (attendance.presence || []).length);
      const overviewCameras=document.getElementById("overview-camera-list");
      if(overviewCameras){
        const configured=[...(cameras.items||[]),...uniqueInventory];
        overviewCameras.innerHTML=configured.length?configured.map(camera=>{
          const runtime=inventory.find(x=>x.camera_id===camera.camera_id && (!camera.edge_id || x.edge_id===camera.edge_id)); const online=!!runtime?.online;
          return "<a class='overview-camera-row' href='/portal/live.html'><strong>"+escapeHtml(camera.name||camera.camera_id)+"</strong><small>"+escapeHtml(camera.camera_role||"GENERAL")+" · "+escapeHtml(camera.camera_zone||"Shop")+"</small><span class='overview-state "+(online?"camera-state-online":"camera-state-offline")+"'>● "+(online?"Online":"Offline")+"</span></a>";
        }).join(""):"<p>No cameras configured for this shop yet.</p>";
      }
      overviewActivityData={attendance:attendance.events||[],alerts:(events.items||[]).filter(item=>/ALERT|UNKNOWN|INCIDENT|SHOPLIFTING/.test(item.event_type||""))};
      renderOverviewActivity();
      const today = new Date().toISOString().slice(0, 10);
      const todayUnknown = (events.items || []).filter(item => /UNKNOWN|INCIDENT/.test(item.event_type||"") && String(item.event_time || "").slice(0, 10) === today).length;
      setText("unknown-incidents", todayUnknown);
    } catch (error) {
      setText("application-status", "● Unavailable");
      setText("database-status", "Unavailable");
      renderEdges([]);
      showMessage(error.message, true);
    }
  }

  function sourceType(source) {
    const selected = document.getElementById("source-type")?.value;
    if (selected === "onvif") return "rtsp";
    if (selected) return selected;
    if (/^rtsp:\/\//i.test(source)) return "rtsp";
    if (/^\d+$/.test(source.trim())) return "webcam";
    return "file";
  }

  async function createEdgeCommand(commandType, request, options={}) {
    const scope=requireScope(["tenant_id","shop_id"]);
    const edgeId=options.edgeId || scope.edge_id;
    if(!edgeId) throw new Error("No edge device is selected. Wait for the edge to appear online, then try again.");
    const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edge-commands",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tenant_id:scope.tenant_id,shop_id:scope.shop_id,edge_id:edgeId,command_type:commandType,request}),
      timeoutMs:15000
    });
    const body=await response.json();
    if(!response.ok) throw new Error(body.detail || "Unable to send command to edge.");
    return body;
  }

  async function waitForEdgeCommand(commandId, timeoutMs=30000) {
    const scope=requireScope(["tenant_id"]);
    const deadline=Date.now()+timeoutMs;
    while(Date.now()<deadline){
      const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edge-commands/"+encodeURIComponent(commandId));
      const body=await response.json();
      if(!response.ok) throw new Error(body.detail || "Unable to read edge command.");
      if(body.status==="SUCCEEDED") return body.result || {};
      if(body.status==="FAILED") throw new Error(body.result?.error || "Edge command failed.");
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    throw new Error("Edge device did not respond within "+Math.round(timeoutMs/1000)+" seconds. Confirm the selected edge agent is online.");
  }

  async function discoverOnvifCamera(){
    try{
      const host=document.getElementById("onvif-host").value.trim();
      if(!host) throw new Error("Enter the camera IP/host first.");
      showMessage("Discovering camera through the assigned edge device...");
      const command=await createEdgeCommand("ONVIF_PROBE",{
        host,port:Number(document.getElementById("onvif-port").value||80),
        username:document.getElementById("onvif-username").value,
        password:document.getElementById("onvif-password").value,purpose:"ai"
      });
      const result=await waitForEdgeCommand(command.id);
      const profiles=result.profiles||[];
      const select=document.getElementById("onvif-profile");
      select.innerHTML="";
      profiles.forEach(profile=>{
        const option=document.createElement("option");
        option.value=profile.uri;
        option.textContent=(profile.name||profile.token)+" — "+(profile.width||"?")+"×"+(profile.height||"?")+" @ "+(profile.fps||"?")+" FPS";
        select.appendChild(option);
      });
      const recommended=result.recommended_profile;
      if(recommended?.uri){select.value=recommended.uri; document.getElementById("camera-source").value=recommended.uri;}
      document.getElementById("profile-group").style.display=profiles.length?"":"none";
      showMessage((result.manufacturer||"ONVIF")+" "+(result.model||"camera")+" discovered. "+profiles.length+" stream profile(s) found.");
    }catch(error){showMessage(error.message,true);}
  }

  let cameraTestInProgress=false;
  async function testCameraConnection(options={}){
    const button=options.button || document.getElementById("test-camera-btn");
    if(cameraTestInProgress) return;
    cameraTestInProgress=true;
    const originalText=button?.textContent || "Test Connection";
    if(button){button.disabled=true;button.textContent="Testing...";}
    try{
      let payload={};
      if(options.cameraId){
        payload={camera_id:options.cameraId,source_type:options.sourceType||""};
      }else{
        const source=document.getElementById("camera-source").value.trim();
        if(!source) throw new Error("Enter or discover a camera source first.");
        payload={source,source_type:sourceType(source)};
      }
      showMessage("Testing camera from the assigned edge device. Please wait...");
      const command=await createEdgeCommand("CAMERA_TEST",payload,{edgeId:options.edgeId});
      const result=await waitForEdgeCommand(command.id,45000);
      if(result.connected===false || result.success===false || result.ok===false)
        throw new Error(result.message||result.error||"Camera connection failed.");
      const resolution=result.resolution ? " · "+result.resolution.width+"×"+result.resolution.height : "";
      const frames=result.frames_received ? " · "+result.frames_received+" frames received" : "";
      showMessage((result.message || "Camera connected successfully")+resolution+frames+".");
    }catch(error){
      showMessage(error.message,true);
    }finally{
      cameraTestInProgress=false;
      if(button){button.disabled=false;button.textContent=originalText;}
    }
  }

  let detectionEditorState=null;
  function renderDetectionEditor(){
    const state=detectionEditorState;if(!state)return;
    const overlay=document.getElementById("zone-editor-overlay"),mode=document.getElementById("zone-editor-mode");
    mode.value=state.mode;overlay.innerHTML="";
    if(state.mode==="FULL_FRAME"){
      overlay.innerHTML="<div style='position:absolute;inset:0;border:3px solid #facc15;background:#facc1518;pointer-events:none'><span style='background:#111827;color:white;padding:3px 6px'>Full Frame</span></div>";
      document.getElementById("zone-editor-list").textContent="The whole frame is monitored.";return;
    }
    state.zones.forEach(zone=>{
      const box=document.createElement("div");box.dataset.zoneId=zone.id;
      Object.assign(box.style,{position:"absolute",left:(zone.x*100)+"%",top:(zone.y*100)+"%",width:(zone.width*100)+"%",height:(zone.height*100)+"%",border:"2px solid #facc15",background:"#facc1525",opacity:zone.enabled?"1":".45",boxSizing:"border-box",cursor:"move"});
      box.innerHTML="<span style='background:#111827;color:#fff;padding:2px 5px'>"+escapeHtml(zone.name)+(zone.enabled?"":" (disabled)")+"</span><i style='position:absolute;right:-5px;bottom:-5px;width:12px;height:12px;background:#facc15;cursor:nwse-resize'></i>";
      box.addEventListener("pointerdown",event=>{
        if(event.target.tagName==="SPAN")return;event.preventDefault();event.stopPropagation();
        const rect=overlay.getBoundingClientRect(),startX=event.clientX,startY=event.clientY,original={...zone},resize=event.target.tagName==="I";
        const move=e=>{const dx=(e.clientX-startX)/rect.width,dy=(e.clientY-startY)/rect.height;
          if(resize){zone.width=Math.max(.02,Math.min(1-zone.x,original.width+dx));zone.height=Math.max(.02,Math.min(1-zone.y,original.height+dy));}
          else{zone.x=Math.max(0,Math.min(1-zone.width,original.x+dx));zone.y=Math.max(0,Math.min(1-zone.height,original.y+dy));}renderDetectionEditor();};
        const up=()=>{overlay.removeEventListener("pointermove",move);overlay.removeEventListener("pointerup",up);};
        overlay.addEventListener("pointermove",move);overlay.addEventListener("pointerup",up,{once:true});
      });overlay.appendChild(box);
    });
    document.getElementById("zone-editor-list").innerHTML=state.zones.map(z=>"<div style='display:flex;gap:10px;align-items:center;padding:6px 0'><strong>"+escapeHtml(z.name)+"</strong><label><input type='checkbox' class='zone-enabled' data-zone='"+escapeHtml(z.id)+"' "+(z.enabled?"checked":"")+"> Enabled</label><button type='button' class='btn btn-light zone-delete' data-zone='"+escapeHtml(z.id)+"'>Delete</button></div>").join("")||"No custom zones. Detection is intentionally disabled in this mode.";
    document.querySelectorAll(".zone-enabled").forEach(el=>el.addEventListener("change",()=>{const z=state.zones.find(x=>x.id===el.dataset.zone);if(z){z.enabled=el.checked;renderDetectionEditor();}}));
    document.querySelectorAll(".zone-delete").forEach(el=>el.addEventListener("click",()=>{state.zones=state.zones.filter(x=>x.id!==el.dataset.zone);renderDetectionEditor();}));
  }
  async function openDetectionEditor(camera){
    try{
      const scope=requireScope(["tenant_id","shop_id"]),edgeId=camera.edge_id||scope.edge_id;
      const query="?shop_id="+encodeURIComponent(scope.shop_id)+"&edge_id="+encodeURIComponent(edgeId);
      const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/cameras/"+encodeURIComponent(camera.camera_id)+"/detection-config"+query);
      const body=await response.json();if(!response.ok)throw new Error(body.detail||"Unable to load detection configuration.");
      detectionEditorState={tenantId:scope.tenant_id,shopId:scope.shop_id,edgeId,cameraId:camera.camera_id,version:body.version||0,mode:body.mode||"FULL_FRAME",zones:body.zones||[]};
      const fw=Number(camera.frame_width||16),fh=Number(camera.frame_height||9);
      document.getElementById("zone-editor-canvas").style.aspectRatio=fw+" / "+fh;
      document.getElementById("zone-editor-title").textContent="Detection zones · "+camera.name;
      document.getElementById("zone-editor-status").textContent="Effective: "+body.effective_mode+" · Sync: "+body.sync_status+(body.applied_version!==undefined?" · edge applied v"+body.applied_version:" ");
      document.getElementById("detection-zone-editor").hidden=false;document.getElementById("zone-editor-message").textContent="";renderDetectionEditor();
      document.getElementById("detection-zone-editor").scrollIntoView({behavior:"smooth",block:"nearest"});
    }catch(error){showMessage(error.message,true);}
  }
  async function saveDetectionEditor(){
    const state=detectionEditorState;if(!state)return;
    try{
      const url="/portal/v1/tenants/"+encodeURIComponent(state.tenantId)+"/cameras/"+encodeURIComponent(state.cameraId)+"/detection-config?shop_id="+encodeURIComponent(state.shopId)+"&edge_id="+encodeURIComponent(state.edgeId);
      const response=await authFetch(url,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({mode:state.mode,zones:state.mode==="CUSTOM_ZONES"?state.zones:[],expected_version:state.version})});
      const body=await response.json();if(!response.ok)throw new Error(body.detail||"Unable to save detection configuration.");
      state.version=body.version;state.mode=body.mode;state.zones=body.zones||[];
      document.getElementById("zone-editor-status").textContent="Effective: "+body.effective_mode+" · Sync: "+body.sync_status;
      document.getElementById("zone-editor-message").textContent="Saved. Waiting for edge acknowledgement.";renderDetectionEditor();
    }catch(error){document.getElementById("zone-editor-message").textContent=error.message;}
  }
  async function loadConfiguredCameras(){
    const container=document.getElementById("configured-camera-list");
    if(!container) return;
    try{
      const scope=requireScope(["tenant_id","shop_id"]);
      const edgeResponse=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edges");
      const edgeBody=await edgeResponse.json(); if(!edgeResponse.ok) throw new Error(edgeBody.detail||"Unable to load edge devices.");
      const edges=edgeBody.items||[];
      const selectedEdgeRecord=selectActiveEdge(edges,scope.edge_id);
      rememberActiveEdge(selectedEdgeRecord);
      const selectedEdge=selectedEdgeRecord?.edge_id || "";
      const url="/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/cameras?shop_id="+encodeURIComponent(scope.shop_id)+(selectedEdge?"&edge_id="+encodeURIComponent(selectedEdge):"");
      const response=await authFetch(url);
      const body=await response.json(); if(!response.ok) throw new Error(body.detail||"Unable to load cameras.");
      const items=body.items||[];
      const edge=edges.find(item=>item.edge_id===selectedEdge);
      const inventory=edge?.status?.cameras||[];
      const configuredIds=new Set(items.map(camera=>camera.camera_id));
      const localOnly=inventory.filter(camera=>!configuredIds.has(camera.camera_id));
      if(!items.length && !localOnly.length){container.innerHTML="<p>No cameras configured or reported by this edge yet.</p>";return;}
      const configuredHtml=items.map(camera=>{
        const runtime=inventory.find(local=>local.camera_id===camera.camera_id);
        const state=runtime ? (runtime.state || (runtime.online?"ONLINE":"OFFLINE")) : "WAITING FOR EDGE";
        const online=!!runtime?.online;
        const statusColor=online?"#166534":(runtime?"#6b7280":"#92400e");
        return "<div style='display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid #e5e7eb'><div><strong>"+escapeHtml(camera.name)+"</strong><br><small>"+escapeHtml(camera.camera_id)+" · "+escapeHtml(camera.source_type)+" · "+escapeHtml(camera.camera_role)+" · Cloud managed</small></div><div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end'><strong style='color:"+statusColor+"'>● "+escapeHtml(state)+"</strong><button class='btn btn-light test-existing-camera' data-id='"+escapeHtml(camera.camera_id)+"' data-edge-id='"+escapeHtml(camera.edge_id||selectedEdge)+"' data-source-type='"+escapeHtml(camera.source_type||"")+"'>Test</button><button class='btn btn-light manage-zones' data-id='"+escapeHtml(camera.camera_id)+"'>Detection Zones</button><button class='btn btn-light edit-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Edit</button><button class='btn btn-light delete-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Delete</button></div></div>";
      }).join("");
      const localHtml=localOnly.map(camera=>"<div style='display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid #e5e7eb'><div><strong>"+escapeHtml(camera.name||camera.camera_id)+"</strong><br><small>"+escapeHtml(camera.camera_id)+" · "+escapeHtml(camera.source_type||"camera")+" · "+escapeHtml(camera.camera_role||"GENERAL")+" · Edge discovered</small></div><div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end'><strong style='color:"+(camera.online?"#166534":"#6b7280")+"'>● "+(camera.online?"Online":"Offline")+"</strong><button class='btn btn-light test-existing-camera' data-id='"+escapeHtml(camera.camera_id)+"' data-edge-id='"+escapeHtml(selectedEdge)+"' data-source-type='"+escapeHtml(camera.source_type||"")+"'>Test</button><button class='btn btn-light adopt-local-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Configure</button></div></div>").join("");
      container.innerHTML=configuredHtml+localHtml;
      container.querySelectorAll(".test-existing-camera").forEach(button=>button.addEventListener("click",()=>testCameraConnection({cameraId:button.dataset.id,edgeId:button.dataset.edgeId,sourceType:button.dataset.sourceType,button})));
      container.querySelectorAll(".adopt-local-camera").forEach(button=>button.addEventListener("click",()=>adoptLocalCamera(localOnly.find(x=>x.camera_id===button.dataset.id))));
      container.querySelectorAll(".edit-camera").forEach(button=>button.addEventListener("click",()=>editCamera(items.find(x=>x.camera_id===button.dataset.id))));
      container.querySelectorAll(".manage-zones").forEach(button=>button.addEventListener("click",()=>openDetectionEditor(items.find(x=>x.camera_id===button.dataset.id))));
      container.querySelectorAll(".delete-camera").forEach(button=>button.addEventListener("click",()=>deleteCamera(button.dataset.id,items.find(x=>x.camera_id===button.dataset.id)?.edge_id)));
    }catch(error){container.innerHTML="<p>"+escapeHtml(error.message)+"</p>";}
  }

  function escapeHtml(value){const div=document.createElement("div");div.textContent=String(value??"");return div.innerHTML.replaceAll('"','&quot;').replaceAll("'",'&#39;');}

  function editCamera(camera){
    document.getElementById("camera-name").value=camera.name||"";
    document.getElementById("camera-id").value=camera.camera_id||"";
    document.getElementById("camera-id").readOnly=true;
    document.getElementById("camera-id").dataset.enabled=String(camera.enabled !== false);
    const checks=Array.from(document.querySelectorAll(".feature-card input[type=checkbox]"));
    const flags=camera.features||{}, settings=camera.settings||{};
    [!!flags.attendance,!!flags.face_recognition,settings.tracking_mode==='track',!!flags.unknown_detection,!!flags.object_security].forEach((value,index)=>{if(checks[index])checks[index].checked=value;});
    document.getElementById("frame-skip").value=Math.max(1,Math.round(30/Number(settings.tracking_fps||15)));
    document.getElementById("max-width").value=settings.tracking_imgsz||settings.max_frame_width||640;
    const editSource=document.getElementById("camera-source");
    editSource.value="";
    editSource.dataset.edgeLocalCamera="";
    editSource.dataset.cloudExistingCamera=camera.camera_id||"";
    editSource.placeholder=camera.source_configured ? "Stored securely; leave blank to keep existing source" : "RTSP URL, video file path or webcam index";
    document.getElementById("source-type").value=camera.source_type||"rtsp";
    document.getElementById("camera-zone").value=camera.camera_zone||"";
    document.getElementById("crowd-threshold").value=camera.crowd_threshold||10;
    const role=document.getElementById("camera-role"); role.value=camera.camera_role==="GENERAL"?"GENERAL":"ENTRANCE_EXIT";
    updateCameraPurposeDescription();
    window.scrollTo({top:0,behavior:"smooth"});
  }

  function adoptLocalCamera(camera){
    if(!camera) return;
    document.getElementById("camera-name").value=camera.name||camera.camera_id||"";
    document.getElementById("camera-id").value=camera.camera_id||"";
    const source=document.getElementById("camera-source");
    source.value="";
    source.dataset.edgeLocalCamera=camera.camera_id||"";
    source.dataset.cloudExistingCamera="";
    source.placeholder="Stored securely on edge as "+(camera.camera_id||"local camera");
    document.getElementById("source-type").value=camera.source_type||"webcam";
    document.getElementById("camera-zone").value=camera.camera_zone||"";
    document.getElementById("crowd-threshold").value=camera.crowd_threshold||10;
    const role=document.getElementById("camera-role");
    role.value=(camera.camera_role==="ENTRANCE_EXIT")?"ENTRANCE_EXIT":"GENERAL";
    updateCameraPurposeDescription();
    showMessage("Camera loaded from edge. Choose role/features and save; source stays hidden on the local PC.");
    window.scrollTo({top:0,behavior:"smooth"});
  }

  async function deleteCamera(cameraId,edgeId){
    if(!confirm("Delete camera "+cameraId+"?")) return;
    try{
      const scope=requireScope(["tenant_id","shop_id","edge_id"]);
      if(edgeId) scope.edge_id=edgeId;
      const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/cameras/"+encodeURIComponent(cameraId)+"?shop_id="+encodeURIComponent(scope.shop_id)+"&edge_id="+encodeURIComponent(scope.edge_id),{method:"DELETE"});
      const body=await response.json(); if(!response.ok) throw new Error(body.detail||"Unable to delete camera.");
      showMessage("Camera deleted from cloud configuration."); await loadConfiguredCameras();
    }catch(error){showMessage(error.message,true);}
  }

  function updateCameraPurposeDescription(){
    const security=document.getElementById("camera-role")?.value==="GENERAL";
    const message=document.getElementById("camera-purpose-description");
    if(message)message.textContent=security
      ?"Unknown-person monitoring. Detection defaults to the entire frame; configure a bounded area using Detection Zones on the saved camera."
      :"Employee recognition and attendance. CRM check-in, checkout and break actions use configured attendance rules.";
  }

  async function saveCamera() {
    try {
      const scope = requireScope(["tenant_id", "shop_id", "site_id", "edge_id"]);
      const cameraId = document.getElementById("camera-id").value.trim();
      const name = document.getElementById("camera-name").value.trim();
      const sourceInput = document.getElementById("camera-source");
      const edgeLocalCamera = sourceInput.dataset.edgeLocalCamera || "";
      const cloudExistingCamera = sourceInput.dataset.cloudExistingCamera || "";
      const source = sourceInput.value.trim()
        || (edgeLocalCamera === cameraId ? "edge-local:" + cameraId : "")
        || (cloudExistingCamera === cameraId ? "__KEEP_EXISTING__" : "");
      const roleValue = document.getElementById("camera-role").value;
      if (!cameraId || !name || !source || !roleValue) throw new Error("Camera Name, Camera ID, Camera Source and Camera Role are required.");
      const securityCamera=roleValue==="GENERAL";
      const payload = {
        ...scope,
        company_code: scope.company_code || null,
        camera_id: cameraId,
        name,
        source_type: document.getElementById("source-type").value||sourceType(source),
        source,
        camera_role: securityCamera ? "GENERAL" : "ENTRANCE_EXIT",
        camera_zone: document.getElementById("camera-zone").value.trim() || null,
        crowd_threshold: Number(document.getElementById("crowd-threshold").value || 10),
        enabled: document.getElementById("camera-id").dataset.enabled !== 'false',
        // These keys intentionally match CameraFeatures/apply_cloud_camera on the edge.
        // UI-only labels must never silently create feature names the edge ignores.
        features: {
          attendance: !securityCamera,
          face_recognition: true,
          unknown_detection: securityCamera,
          shoplifting: false,
          object_security: false
        },
        settings: {
          // Person Tracking is a runtime mode, not an unrelated detection feature.
          tracking_fps: Math.max(1, Math.round(30 / Math.max(1, Number(document.getElementById("frame-skip").value || 2)))),
          tracking_imgsz: Number(document.getElementById("max-width").value || 640),
          tracking_quality: 65,
          tracking_mode: "track"
        }
      };
      const response = await authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/cameras/" + encodeURIComponent(cameraId), {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Unable to save camera.");
      showMessage("Camera configuration saved. The assigned edge will apply it automatically.");
      await loadConfiguredCameras();
    } catch (error) {
      showMessage(error.message, true);
    }
  }

  function resetCameraForm() {
    document.getElementById("camera-id").readOnly=false;
    delete document.getElementById("camera-id").dataset.enabled;
    ["camera-name", "camera-id", "camera-source", "camera-zone"].forEach(id => {
      const node = document.getElementById(id);
      if (node) node.value = "";
    });
    const source = document.getElementById("camera-source");
    if (source) {
      source.dataset.edgeLocalCamera = "";
      source.dataset.cloudExistingCamera = "";
      source.placeholder = "RTSP URL, video file path or webcam index";
    }
    const role = document.getElementById("camera-role");
    if (role) role.value = "ENTRANCE_EXIT";
    updateCameraPurposeDescription();
  }

  function wireCameraPage() {
    const save = document.getElementById("save-camera-btn");
    if (!save) return;
    save.addEventListener("click", saveCamera);
    document.getElementById("camera-role")?.addEventListener("change",updateCameraPurposeDescription);
    updateCameraPurposeDescription();
    document.getElementById("cancel-camera-btn")?.addEventListener("click", resetCameraForm);
    document.getElementById("test-camera-btn")?.addEventListener("click", testCameraConnection);
    document.getElementById("discover-camera-btn")?.addEventListener("click", discoverOnvifCamera);
    document.getElementById("zone-editor-close")?.addEventListener("click",()=>{document.getElementById("detection-zone-editor").hidden=true;detectionEditorState=null;});
    document.getElementById("zone-editor-save")?.addEventListener("click",saveDetectionEditor);
    document.getElementById("zone-editor-mode")?.addEventListener("change",event=>{if(detectionEditorState){detectionEditorState.mode=event.target.value;renderDetectionEditor();}});
    document.getElementById("zone-editor-overlay")?.addEventListener("pointerdown",event=>{
      if(!detectionEditorState||detectionEditorState.mode!=="CUSTOM_ZONES"||event.target.closest("[data-zone-id]"))return;
      const overlay=event.currentTarget,rect=overlay.getBoundingClientRect(),x=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),y=Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height));
      const draft=document.createElement("div");Object.assign(draft.style,{position:"absolute",border:"2px dashed #fff",pointerEvents:"none"});overlay.appendChild(draft);
      const move=e=>{const ex=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),ey=Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height));Object.assign(draft.style,{left:(Math.min(x,ex)*100)+"%",top:(Math.min(y,ey)*100)+"%",width:(Math.abs(ex-x)*100)+"%",height:(Math.abs(ey-y)*100)+"%"});};
      const up=e=>{overlay.removeEventListener("pointermove",move);overlay.removeEventListener("pointerup",up);draft.remove();const ex=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),ey=Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height)),w=Math.abs(ex-x),h=Math.abs(ey-y);if(w<.02||h<.02)return;const name=prompt("Detection zone name","Detection Zone");if(!name)return;detectionEditorState.zones.push({id:crypto.randomUUID(),name:name.trim(),x:Math.min(x,ex),y:Math.min(y,ey),width:w,height:h,enabled:true});renderDetectionEditor();};
      overlay.addEventListener("pointermove",move);overlay.addEventListener("pointerup",up,{once:true});
    });
    document.getElementById("onvif-profile")?.addEventListener("change", event => {
      document.getElementById("camera-source").value=event.target.value;
    });
    document.getElementById("source-type")?.addEventListener("change", event => {
      const visible=event.target.value==="onvif";
      ["onvif-host-group","onvif-port-group","onvif-user-group","onvif-password-group","onvif-actions"].forEach(id=>{
        document.getElementById(id).style.display=visible?"":"none";
      });
      if(!visible) document.getElementById("profile-group").style.display="none";
    });
    loadConfiguredCameras();
  }


  const cloudLiveRooms=new Map();
  const cameraKey=camera=>JSON.stringify([camera.edge_id,camera.camera_id]);
  const liveLabels={starting:"Starting live view…",waiting_for_edge:"Waiting for edge to join…",waiting_for_video:"Waiting for camera video…",playing:"Live",reconnecting:"Reconnecting…",stopped:"Remote video stopped. Edge AI monitoring continues.",error:"Live video failed"};
  async function requestLiveSession(target){
    const scope=requireScope(["tenant_id"]);
    const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/live/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({...target,ttl_seconds:600})});
    if(!response.ok)throw new Error("Unable to start secure live view.");
    return response.json();
  }
  async function releaseLiveSession(session,target){
    const scope=requireScope(["tenant_id"]);
    const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/live/stop",{method:"POST",keepalive:true,headers:{"Content-Type":"application/json"},body:JSON.stringify({...target,session_id:session.session_id})});
    if(!response.ok)throw new Error("Unable to stop remote session.");
  }
  function stopCloudLive(key){
    const viewer=cloudLiveRooms.get(key);cloudLiveRooms.delete(key);
    return viewer?.stop();
  }
  function startCloudLive(key,camera,button){
    if(cloudLiveRooms.get(key)?.active){void stopCloudLive(key);return;}
    if(!window.CameraLiveView)throw new Error("Live viewer failed to load.");
    const tile=button.closest(".cloud-camera-tile"),video=tile.querySelector("video"),message=tile.querySelector(".live-video-message");
    const viewer=new CameraLiveView.Viewer({sdk:window.LivekitClient,video,startSession:requestLiveSession,stopSession:releaseLiveSession,
      onState:(state,detail)=>{
        message.textContent=detail.message||liveLabels[state];message.style.display=state==="playing"?"none":"block";
        button.textContent=["stopped","error"].includes(state)?"View Live":"Stop Live";
        button.disabled=state==="starting";
      }});
    cloudLiveRooms.set(key,viewer);
    void viewer.start({camera_id:camera.camera_id,edge_id:camera.edge_id});
  }
  let cloudLiveLoadGeneration=0,cloudLivePageGeneration=0;
  async function loadCloudLiveCameras(){
    const grid=document.getElementById("cloud-live-grid");if(!grid)return;
    const pageGeneration=cloudLivePageGeneration,loadGeneration=++cloudLiveLoadGeneration;
    [...cloudLiveRooms.keys()].forEach(key=>void stopCloudLive(key));
    try{
      const scope=requireScope(["tenant_id"]);
      const [cameraResponse,edgeResponse]=await Promise.all([
        authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/cameras"),
        authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edges")
      ]);
      const cameraBody=await cameraResponse.json(),edgeBody=await edgeResponse.json();
      if(pageGeneration!==cloudLivePageGeneration||loadGeneration!==cloudLiveLoadGeneration)return;
      if(!cameraResponse.ok)throw new Error(cameraBody.detail||"Unable to load cameras.");
      if(!edgeResponse.ok)throw new Error(edgeBody.detail||"Unable to load edge status.");
      const edges=edgeBody.items||[],selected=edges.find(edge=>edgeOnline(edge))||edges[0],inventory=[];
      // Old edge heartbeats are snapshots, not current camera inventory. Never
      // resurrect their reported cameras as live tiles after an edge disconnects.
      edges.filter(edgeOnline).forEach(edge=>(edge.status?.cameras||[]).forEach(camera=>inventory.push({...camera,edge_id:edge.edge_id||edge.id})));
      const configured=cameraBody.items||[],byId=new Map();
      configured.forEach(camera=>byId.set(cameraKey(camera),camera));
      inventory.forEach(camera=>{
        const key=cameraKey(camera);
        byId.set(key,{...(byId.get(key)||{}),...camera});
      });
      const allCameras=[...byId.values()];
      const runtimeByKey=new Map(inventory.map(camera=>[cameraKey(camera),camera]));
      const onlineCameras=allCameras.filter(camera=>!!runtimeByKey.get(cameraKey(camera))?.online);
      const showOffline=!!document.getElementById("cloud-live-show-offline")?.checked;
      const cameras=showOffline?allCameras:onlineCameras;
      setText("cloud-live-summary",onlineCameras.length+" online · "+(allCameras.length-onlineCameras.length)+" offline"+(showOffline?"":" (hidden)"));
      const edgeState=document.getElementById("live-edge-state");if(edgeState)edgeState.textContent=(selected&&edgeOnline(selected))?"● Edge Online":"● Edge Offline";
      if(!cameras.length){grid.innerHTML="<div class='camera-panel' style='padding:36px;text-align:center'>"+(allCameras.length?"No cameras are currently online. Enable ‘Show offline cameras’ to review saved configurations.":"No cameras configured for this shop.")+"</div>";return;}
      grid.innerHTML=cameras.map(camera=>{
        const runtime=runtimeByKey.get(cameraKey(camera)),online=!!runtime?.online,features=camera.features||{};
        const pills=[features.face_recognition?"Face Recognition":null,(camera.settings?.tracking_mode==="track"||camera.tracking_mode==="track")?"Tracking":null,features.object_security?"Security":null].filter(Boolean);
        const role=String(camera.camera_role||runtime?.camera_role||"").toUpperCase(),attendance=role==="ENTRANCE_EXIT";
        return "<article class='cloud-camera-tile'><div class='cloud-camera-visual' style='position:relative'><div class='cloud-camera-overlay'><strong>"+escapeHtml(camera.name||camera.camera_id)+"</strong><span class='"+(online?"camera-state-online":"camera-state-offline")+"'>● "+(online?"ONLINE":"OFFLINE")+"</span></div><video class='live-video' autoplay playsinline muted style='width:100%;height:100%;object-fit:contain;display:none;background:#0f172a'></video><div class='live-video-message'><strong>Edge AI Live View</strong><br><small>"+(online?"Start live to see the same annotated detection, recognized names and unknown-person boxes produced by the EXE.":"Camera is not currently online")+"</small></div></div><div class='cloud-camera-meta'><div><strong>"+escapeHtml(camera.name||camera.camera_id)+"</strong><div class='camera-feature-pills'>"+pills.map(x=>"<span class='camera-feature-pill'>"+x+"</span>").join("")+"</div></div><div style='display:flex;gap:8px'><button class='btn btn-primary cloud-live-start' data-camera='"+escapeHtml(cameraKey(camera))+"' "+(!online?"disabled":"")+">View Live</button>"+(attendance?"<a class='btn btn-light' href='/portal/attendance.html'>Take Attendance</a>":"")+"<a class='btn btn-light' href='/portal/cameras.html'>Settings</a></div></div></article>";
      }).join("");
      grid.querySelectorAll(".cloud-live-start").forEach(button=>button.addEventListener("click",()=>{const camera=cameras.find(x=>cameraKey(x)===button.dataset.camera);if(camera)startCloudLive(button.dataset.camera,camera,button);}));
    }catch(error){if(pageGeneration!==cloudLivePageGeneration||loadGeneration!==cloudLiveLoadGeneration)return;grid.innerHTML="<div class='camera-panel' style='padding:28px'>"+escapeHtml(error.message)+"</div>";}
  }
  function wireCloudLivePage(){
    if(!document.getElementById("cloud-live-grid"))return;
    ++cloudLivePageGeneration;
    document.getElementById("refresh-cloud-live")?.addEventListener("click",loadCloudLiveCameras);
    document.getElementById("cloud-live-show-offline")?.addEventListener("change",loadCloudLiveCameras);
    document.querySelectorAll("[data-grid]").forEach(button=>button.addEventListener("click",()=>{const grid=document.getElementById("cloud-live-grid"),mode=button.dataset.grid;grid.className="professional-live-grid"+(mode==="1"?" cols-1":mode==="3"?" cols-3":"");}));
    window.addEventListener("pagehide",()=>{++cloudLivePageGeneration;[...cloudLiveRooms.keys()].forEach(key=>void stopCloudLive(key));});
    loadCloudLiveCameras();
  }

  function renderPersonnel(items){
    const body=document.getElementById("personnel-table-body");
    if(!body) return;
    const count=document.getElementById("personnel-count");
    if(count) count.textContent=items.length+" Personnel";
    if(!items.length){
      body.innerHTML="<tr class='empty-row'><td colspan='7'><div class='empty-personnel'><div class='empty-personnel-icon'>👥</div><h3>No personnel added yet</h3><p>Add your first person using the form above.</p></div></td></tr>";
      return;
    }
    body.innerHTML=items.map(person=>{
      const initials=String(person.full_name||"?").split(/\s+/).slice(0,2).map(x=>x[0]||"").join("").toUpperCase();
      const face=person.face_enrolled?"✓ Enrolled":"Face required";
      const sync=person.edge_synced?"✓ Edge synced":"Sync pending";
      const crm=person.crm_mapped?"✓ CRM mapped":"CRM not mapped";
      const diagnostic=person.sync_diagnostic;
      const compatibility=diagnostic?'Cloud: '+diagnostic.model_compatibility+' · '+Object.entries(diagnostic.template_status_counts||{}).filter(([,count])=>count>0).map(([status,count])=>count+' '+status).join(', '):'Compatibility not verified';
      return "<tr><td><div class='avatar'>"+escapeHtml(initials)+"</div>"+
        (person.enrollment_preview_url?"<img data-enrollment-preview='"+escapeHtml(person.enrollment_preview_url)+"' width='48' height='48' alt='CRM enrollment preview'>":"")+"</td>"+
        "<td><strong>"+escapeHtml(person.full_name||"")+"</strong><br><small>"+face+" · "+sync+" · "+crm+"</small><br><small>"+escapeHtml(compatibility)+"</small></td>"+
        "<td>"+escapeHtml(person.employee_code||"")+"</td><td>"+escapeHtml(person.role||"")+"</td>"+
        "<td>"+(person.active?"Active":"Inactive")+"</td><td>"+escapeHtml(String(person.created_at||"").slice(0,10))+"</td>"+
        "<td><span>"+(diagnostic?.model_compatibility==='COMPATIBLE'?"Compatible templates":person.face_enrolled?"Review compatibility":"Face not registered")+"</span></td></tr>";
    }).join("");
    body.querySelectorAll('[data-enrollment-preview]').forEach(async img=>{
      try {
        const response=await authFetch(img.dataset.enrollmentPreview);
        if(!response.ok) throw new Error('Preview unavailable');
        const url=URL.createObjectURL(await response.blob());
        img.onload=()=>URL.revokeObjectURL(url);img.onerror=()=>URL.revokeObjectURL(url);img.src=url;
      } catch (_) {img.remove();}
    });
  }

  async function loadPersonnel(){
    if(!document.getElementById("personnel-table-body")) return;
    const scope=requireScope(["tenant_id"]);
    const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/personnel");
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||"Unable to load personnel.");
    const diagnostics=await authFetch('/portal/v1/tenants/'+encodeURIComponent(scope.tenant_id)+'/personnel-diagnostics');
    let diagnosticData={items:[]};
    if(diagnostics.ok) diagnosticData=await diagnostics.json();
    const byPerson=new Map((diagnosticData.items||[]).map(item=>[item.person_id,item]));
    renderPersonnel((data.items||[]).map(item=>({...item,sync_diagnostic:byPerson.get(String(item.id))})));
    if(diagnostics.ok){
      const details=document.createElement('details');details.id='personnel-sync-diagnostics';
      const summary=document.createElement('summary');summary.textContent='Personnel synchronization diagnostics';
      const explanation=document.createElement('p');explanation.textContent='COMPATIBLE: ready for recognition. INCOMPATIBLE: re-enroll with the edge model. MODEL_UNAVAILABLE: restore models and restart. UNVERIFIED: legacy template; re-enrollment required. Each template and the last edge report are shown separately; only compatible templates can recognize.';
      const pre=document.createElement('pre');pre.textContent=JSON.stringify(diagnosticData,null,2);
      details.append(summary,explanation,pre);document.getElementById('personnel-sync-diagnostics')?.remove();
      document.getElementById('personnel-table-body').closest('table').after(details);
    }
  }

  function wirePersonnelPage(){
    if(!document.getElementById("personnel-table-body")) return;
    loadPersonnel().catch(error=>showMessage(error.message,true));
  }

  function fmtTime(value){if(!value)return "—";const d=new Date(value);return Number.isNaN(d.getTime())?"—":d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});}
  function fmtDate(value){if(!value)return "—";const d=new Date(value);return Number.isNaN(d.getTime())?"—":d.toLocaleDateString();}
  function renderAttendance(data){
    const records=document.getElementById("attendance-records-body");
    if(!records)return;
    const rows=data.records||[]; const count=document.getElementById("attendance-count"); if(count)count.textContent=rows.length+" Records";
    records.innerHTML=rows.length?rows.map(r=>"<tr><td><strong>"+escapeHtml(r.full_name||"Unknown")+"</strong></td><td>"+escapeHtml(r.employee_code||"—")+"</td><td>"+escapeHtml(r.role||"—")+"</td><td>"+fmtDate(r.entry_time||r.exit_time)+"</td><td>"+fmtTime(r.entry_time)+"</td><td>"+fmtTime(r.exit_time)+"</td><td>—</td><td><span class='status-badge "+(r.exit_time?"":"active")+"'>"+(r.exit_time?"Completed":"Present")+"</span></td><td>—</td></tr>").join(""):"<tr><td colspan='9'>No real attendance events received from this shop yet.</td></tr>";
    const presence=document.getElementById("presence-body"); const active=data.presence||[];
    if(presence) presence.innerHTML=active.length?active.map(r=>"<tr><td><strong>"+escapeHtml(r.full_name||"Unknown")+"</strong></td><td>"+escapeHtml(r.employee_code||"—")+"</td><td>"+escapeHtml(r.role||"—")+"</td><td>"+fmtTime(r.entry_time)+"</td><td>"+fmtTime(r.entry_time)+"</td><td>—</td><td>—</td><td>—</td><td><span class='status-badge active'>● Present</span></td></tr>").join(""):"<tr><td colspan='9'>No personnel currently present from confirmed attendance events.</td></tr>";
    const events=document.getElementById("person-events-body"); const ev=data.events||[];
    if(events){
      events.innerHTML=ev.length?ev.map(e=>"<tr><td>"+fmtTime(e.event_time)+"</td><td><strong>"+escapeHtml(e.full_name||"Unknown")+"</strong></td><td>"+escapeHtml(String(e.event_type||"").replaceAll("_"," "))+"</td><td>"+escapeHtml(e.camera_id||"—")+"</td><td>—</td><td>"+(e.has_evidence?"<button class='btn btn-light person-evidence' data-event-id='"+escapeHtml(e.event_id)+"'>View image</button>":"—")+"</td><td>"+(e.confidence!=null?Math.round(Number(e.confidence)*100)+"%":"—")+"</td><td>—</td></tr>").join(""):"<tr><td colspan='8'>No person events received yet.</td></tr>";
      events.querySelectorAll(".person-evidence").forEach(button=>button.addEventListener("click",()=>openPortalEvidence(button.dataset.eventId,"evidence")));
    }
  }
  async function loadAttendance(){
    if(document.getElementById('attendance-workspace')&&window.AttendanceWorkspace){
      const scope=requireScope(['tenant_id']);
      document.querySelectorAll('.attendance-content > .attendance-card:not(#attendance-station),.attendance-heading button').forEach(node=>node.hidden=true);
      window.AttendanceWorkspace.mount(document.getElementById('attendance-workspace'),{base:'/portal/v2/tenants/'+encodeURIComponent(scope.tenant_id)+'/attendance',peopleEndpoint:'/portal/v2/tenants/'+encodeURIComponent(scope.tenant_id)+'/attendance/personnel',fetcher:authFetch}).load();return;
    }
    if(!document.getElementById("attendance-records-body"))return;
    const scope=requireScope(["tenant_id"]); const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/attendance");
    const data=await response.json(); if(!response.ok)throw new Error(data.detail||"Unable to load attendance."); renderAttendance(data);
  }
  let attendanceStationTimer=null,attendanceStationCandidate=null,attendanceViewer=null,attendancePollGeneration=0;
  async function loadAttendanceStationCameras(){
    const select=document.getElementById("station-camera");if(!select)return;
    const scope=requireScope(["tenant_id","shop_id"]);
    const [cameraResponse,edgeResponse]=await Promise.all([
      authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/cameras?shop_id="+encodeURIComponent(scope.shop_id)),
      authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edges")
    ]);
    const data=await cameraResponse.json(),edgeData=await edgeResponse.json();
    if(!cameraResponse.ok)throw new Error(data.detail||"Unable to load attendance cameras.");
    if(!edgeResponse.ok)throw new Error(edgeData.detail||"Unable to load current edge inventory.");
    // Attendance must target the camera identity advertised by the currently-online
    // edge. Cloud camera_configs may legitimately contain an older camera id after
    // reinstall/reconfiguration, and must never win over the live heartbeat.
    const runtime=[];
    (edgeData.items||[]).filter(edge=>edgeOnline(edge)).forEach(edge=>{
      (edge.status?.cameras||[]).forEach(camera=>runtime.push({...camera,edge_id:edge.edge_id||edge.id}));
    });
    const configured=data.items||[],configuredByKey=new Map(configured.map(c=>[(c.edge_id||"")+"::"+c.camera_id,c]));
    const cameras=runtime.map(camera=>({...configuredByKey.get((camera.edge_id||"")+"::"+camera.camera_id),...camera}))
      .filter(c=>String(c.camera_role||"").toUpperCase()==="ENTRANCE_EXIT" && c.enabled!==false && c.online!==false);
    select.innerHTML=cameras.length?cameras.map(c=>"<option value='"+escapeHtml((c.edge_id||"")+"::"+c.camera_id)+"'>"+escapeHtml(c.name||c.camera_id)+" · Online</option>").join(""):"<option value=''>No online attendance camera configured</option>";
  }
  function renderStationCandidate(candidate){
    attendanceStationCandidate=candidate||null;const box=document.getElementById("station-candidate"),expiry=document.getElementById("station-expiry");
    document.querySelectorAll(".station-action").forEach(b=>b.disabled=!candidate||!candidate.crm_mapped||!(candidate.actions||[]).includes(b.dataset.action)||(b.dataset.action==="BREAK_START"&&!candidate.break_configured));
    if(!candidate){box.innerHTML="<p>No person selected. Ask the employee to face the attendance camera.</p>";expiry.textContent="Waiting for a fresh recognition.";return;}
    box.innerHTML="<div style='font-size:20px;font-weight:700'>"+escapeHtml(candidate.full_name||"Recognized person")+"</div><div style='margin-top:8px'>"+escapeHtml(candidate.employee_code||"—")+" · "+escapeHtml(candidate.role||"—")+"</div><div style='margin-top:8px'>Confidence: "+(candidate.confidence!=null?Math.round(Number(candidate.confidence)*100)+"%":"—")+"</div><div style='margin-top:8px'>Detected: "+escapeHtml(new Date(candidate.detected_at).toLocaleString())+"</div>"+(!candidate.crm_mapped?"<p style='color:#b91c1c'>CRM mapping required before an attendance action can be confirmed.</p>":"");
    expiry.textContent=(candidate.state_label||'Needs Review')+" · Recognition valid for "+candidate.expires_in_seconds+" seconds.";
  }
  async function pollAttendanceStation(){
    const generation=attendancePollGeneration;
    const select=document.getElementById("station-camera"),value=select?.value||"";if(!value)return;
    const [edgeId,cameraId]=value.split("::");const scope=requireScope(["tenant_id"]);
    const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/attendance-station/candidate?camera_id="+encodeURIComponent(cameraId)+"&edge_id="+encodeURIComponent(edgeId));
    const data=await response.json();if(generation!==attendancePollGeneration)return;
    if(!response.ok)throw new Error(data.detail||"Unable to read recognition candidate.");renderStationCandidate(data.candidate);
  }
  function resetAttendancePolling(){
    ++attendancePollGeneration;
    clearInterval(attendanceStationTimer);attendanceStationTimer=null;
    renderStationCandidate(null);
  }
  function stopAttendanceStation(){
    resetAttendancePolling();
    return attendanceViewer?.stop();
  }
  async function startAttendanceStation(){
    const value=document.getElementById("station-camera")?.value||"";
    if(!value){showMessage("Configure an Entrance / Attendance camera first.",true);return;}
    if(!window.CameraLiveView)throw new Error("Live viewer failed to load.");
    if(!attendanceViewer){
      attendanceViewer=new CameraLiveView.Viewer({sdk:window.LivekitClient,video:document.getElementById("station-live-video"),startSession:requestLiveSession,stopSession:releaseLiveSession,
        onState:(state,detail)=>{
          const ended=["stopped","error"].includes(state);
          document.getElementById("station-state").textContent="● "+liveLabels[state];
          document.getElementById("station-video-message").textContent=detail.message||liveLabels[state];
          document.getElementById("start-attendance-station").disabled=!ended;
          document.getElementById("stop-attendance-station").disabled=ended;
          document.getElementById("station-camera").disabled=!ended;
          if(ended)resetAttendancePolling();
        }});
    }
    resetAttendancePolling();
    const [edgeId,cameraId]=value.split("::");
    const pending=attendanceViewer.start({camera_id:cameraId,edge_id:edgeId});
    const generation=attendancePollGeneration;
    // Recognition polling is independent of media startup; a polling failure
    // must not stop a valid video session or erase a later session's candidate.
    const poll=()=>pollAttendanceStation().catch(()=>{
      if(generation===attendancePollGeneration)document.getElementById("station-expiry").textContent="Recognition update delayed. Retrying…";
    });
    const pollUntilStopped=async()=>{
      if(generation!==attendancePollGeneration||!attendanceViewer?.active)return;
      await poll();
      if(generation===attendancePollGeneration&&attendanceViewer?.active)
        attendanceStationTimer=setTimeout(pollUntilStopped,1000);
    };
    if(attendanceViewer.active)void pollUntilStopped();
    await pending;
  }
  async function confirmAttendanceStationAction(action){
    const candidate=attendanceStationCandidate,value=document.getElementById("station-camera")?.value||"";if(!candidate||!value){showMessage("Recognition expired. Ask the employee to face the camera again.",true);return;}
    const [edgeId,cameraId]=value.split("::");const scope=requireScope(["tenant_id"]);document.querySelectorAll(".station-action").forEach(b=>b.disabled=true);
    try{const response=await authFetch("/portal/v2/tenants/"+encodeURIComponent(scope.tenant_id)+"/attendance-station/action",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({camera_id:cameraId,edge_id:edgeId,recognition_event_id:candidate.recognition_event_id,action})});const data=await response.json();if(!response.ok||data.ok!==true)throw new Error(data.detail||("Attendance not confirmed: "+(data.delivery||"Needs Review")));showMessage(String(action).replaceAll("_"," ")+" confirmed for "+candidate.full_name+".");renderStationCandidate(null);await loadAttendance();}catch(error){showMessage(error.message,true);await pollAttendanceStation();}
  }
  function wireAttendanceStation(){
    if(!document.getElementById("attendance-station"))return;loadAttendanceStationCameras().catch(error=>showMessage(error.message,true));
    document.getElementById("start-attendance-station").addEventListener("click",()=>startAttendanceStation().catch(error=>showMessage(error.message,true)));
    document.getElementById("stop-attendance-station").addEventListener("click",()=>stopAttendanceStation());
    document.querySelectorAll(".station-action").forEach(button=>button.addEventListener("click",()=>confirmAttendanceStationAction(button.dataset.action)));
    window.addEventListener("pagehide",()=>void stopAttendanceStation());
  }
  function wireAttendancePage(){if(!document.getElementById("attendance-records-body"))return;loadAttendance().catch(error=>showMessage(error.message,true));wireAttendanceStation();setInterval(()=>loadAttendance().catch(()=>{}),15000);}

  async function openPortalEvidence(eventId,kind="evidence"){
    try{
      const scope=requireScope(["tenant_id"]);
      const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/events/"+encodeURIComponent(eventId)+"/"+kind);
      if(!response.ok)throw new Error(kind==="clip"?"Video unavailable.":"Snapshot unavailable.");
      const url=URL.createObjectURL(await response.blob());const dialog=document.createElement("dialog");
      const media=kind==="clip"?document.createElement("video"):document.createElement("img");media.src=url;
      if(kind==="clip"){media.controls=true;media.autoplay=true;}else media.alt="Event snapshot";
      media.style.maxWidth="min(85vw,1100px)";media.style.maxHeight="78vh";
      const close=document.createElement("button");close.className="btn btn-light";close.textContent="Close";close.addEventListener("click",()=>dialog.close());
      dialog.append(media,close);dialog.addEventListener("close",()=>{URL.revokeObjectURL(url);dialog.remove();});document.body.appendChild(dialog);dialog.showModal();
    }catch(error){showMessage(error.message,true);}
  }

  let alertBaseline=null,alertSoundEnabled=false,alertAudioContext=null;
  function notifyNewSecurityAlerts(items){
    const ids=new Set(items.map(event=>String(event.id||"")).filter(Boolean));
    if(alertBaseline===null){alertBaseline=ids;return;}
    const fresh=items.filter(event=>event.id&&!alertBaseline.has(String(event.id)));
    alertBaseline=ids;
    if(!alertSoundEnabled||!fresh.length)return;
    const event=fresh[0];
    try{
      if(alertAudioContext){
        const oscillator=alertAudioContext.createOscillator(),gain=alertAudioContext.createGain();
        oscillator.type="sine";oscillator.frequency.value=760;
        gain.gain.setValueAtTime(0.0001,alertAudioContext.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.12,alertAudioContext.currentTime+0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001,alertAudioContext.currentTime+0.35);
        oscillator.connect(gain).connect(alertAudioContext.destination);
        oscillator.start();oscillator.stop(alertAudioContext.currentTime+0.36);
      }
      if("Notification" in window&&Notification.permission==="granted"){
        const note=new Notification("Camera Eye · Security alert",{
          body:(event.camera_id||"Camera")+" · "+String(event.event_type||"Unknown person detected").replaceAll("_"," "),
          tag:"camera-eye-security-"+String(event.id)
        });
        note.onclick=()=>{window.focus();note.close();};
      }
    }catch(_){/* Browser permission or autoplay restrictions must not block alert history. */}
  }
  async function enableBrowserSecurityAlerts(){
    const button=document.getElementById("enable-alert-sound");
    try{
      const Context=window.AudioContext||window.webkitAudioContext;
      if(Context){alertAudioContext=alertAudioContext||new Context();await alertAudioContext.resume();}
      if("Notification" in window&&Notification.permission==="default")await Notification.requestPermission();
      alertSoundEnabled=true;
      if(button)button.textContent="Sound enabled"+(("Notification" in window&&Notification.permission==="granted")?" · Notifications enabled":"");
    }catch(_){if(button)button.textContent="Browser sound unavailable";}
  }
  async function loadAlerts(){
    const body=document.getElementById('alerts-body'); if(!body)return;
    const scope=requireScope(['tenant_id']);
    const response=await authFetch('/portal/v1/tenants/'+encodeURIComponent(scope.tenant_id)+'/events?limit=500&alerts_only=true');
    const data=await response.json(); if(!response.ok)throw new Error(data.detail||'Unable to load alerts.');
    const items=(data.items||[]).filter(e=>/ALERT|UNKNOWN|INCIDENT|SHOPLIFTING/.test(e.event_type||''));
    notifyNewSecurityAlerts(items);
    body.replaceChildren();
    for(const event of items){
      const payload=event.payload?.payload||event.payload||{}, metadata=payload.metadata||{};
      const row=document.createElement('tr');
      for(const text of [new Date(event.event_time).toLocaleString(),event.camera_id||'',String(event.event_type||'').replaceAll('_',' '),payload.full_name||metadata.object_label||payload.object_label||'Unknown']){
        const cell=document.createElement('td');cell.textContent=text;row.appendChild(cell);
      }
      const evidence=document.createElement('td');
      const actions=[];
      if(metadata.cloud_evidence){const button=document.createElement('button');button.className='btn btn-light';button.textContent='Snapshot';button.addEventListener('click',()=>openPortalEvidence(event.id,'evidence'));actions.push(button);}
      if(metadata.cloud_clip){const button=document.createElement('button');button.className='btn btn-light';button.textContent='Video';button.addEventListener('click',()=>openPortalEvidence(event.id,'clip'));actions.push(button);}
      if(actions.length){evidence.replaceChildren(...actions);}else{evidence.textContent='Unavailable';}
      row.appendChild(evidence);body.appendChild(row);
    }
    if(!items.length){const row=body.insertRow();const cell=row.insertCell();cell.colSpan=5;cell.textContent='No alerts received from this shop.';}
  }
  function wireAlertsPage(){
    if(!document.getElementById('alerts-body'))return;
    const refresh=()=>loadAlerts().catch(error=>showMessage(error.message,true));
    document.getElementById('refresh-alerts').addEventListener('click',refresh);
    document.getElementById('enable-alert-sound')?.addEventListener('click',enableBrowserSecurityAlerts);
    refresh();setInterval(refresh,15000);
  }
  document.querySelectorAll('.nav-menu').forEach(nav=>{
    if(!nav.querySelector('a[href="/portal/system-status.html"]')){
      const link=document.createElement('a');link.className='nav-item';link.href='/portal/system-status.html';link.innerHTML='⚡ <span>System Status</span>';
      const overview=nav.querySelector('a[href="/portal"]');overview?.insertAdjacentElement('afterend',link);
    }
    if(!nav.querySelector('a[href="/portal/alerts.html"]')){
      const link=document.createElement('a');link.className='nav-item';link.href='/portal/alerts.html';link.innerHTML='⚠ <span>Alerts</span>';nav.appendChild(link);
    }
  });
  function wireSystemStatusPage(){
    if(!document.getElementById("edge-device-list") || document.getElementById("overview-activity-list"))return;
    loadSystemStatus().catch(error=>showMessage(error.message,true));
    setInterval(loadSystemStatus,15000);
  }
  bootstrapCrmSession().then(()=>{wireOverviewActivity();wireSystemStatusPage();wireEdgeSetup();wireCameraPage();wireCloudLivePage();wirePersonnelPage();wireAttendancePage();wireAlertsPage();}).catch(error=>showMessage(error.message,true));
})();
