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
        "<strong style='color:"+(online?"#166534":"#b91c1c")+"'>● "+(online?"Online":"Offline")+"</strong></div>"+
        "<div style='margin-top:8px'><small>"+cameraText+"</small></div></div>";
    }).join("");
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

  async function loadSystemStatus() {
    if (!document.getElementById("configured-cameras")) return;
    try {
      const scope = requireScope(["tenant_id"]);
      const [healthResponse, summaryResponse, cameraResponse, unknownResponse, edgeResponse] = await Promise.all([
        fetch("/health"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/summary"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/cameras"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/events?event_type=UNKNOWN_PERSON&limit=500"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/edges")
      ]);
      if (!healthResponse.ok || !summaryResponse.ok || !cameraResponse.ok || !unknownResponse.ok || !edgeResponse.ok) throw new Error("Portal API is unavailable or this session is not authorized.");
      const health = await healthResponse.json();
      await summaryResponse.json();
      const cameras = await cameraResponse.json();
      const unknown = await unknownResponse.json();
      const edgesBody = await edgeResponse.json();
      const edges=edgesBody.items||[];
      renderEdges(edges);
      const inventory=edges.flatMap(edge=>(edge.status?.cameras||[]).map(camera=>({...camera,edge_id:edge.edge_id})));
      const configuredIds=new Set((cameras.items||[]).map(camera=>camera.edge_id+"::"+camera.camera_id));
      const uniqueInventory=inventory.filter(camera=>!configuredIds.has(camera.edge_id+"::"+camera.camera_id));
      setText("application-status", health.status === "ok" ? "● Online" : "● Degraded");
      setText("database-status", "● Connected");
      setText("configured-cameras", (cameras.items || []).length + uniqueInventory.length);
      setText("online-cameras", inventory.filter(camera=>camera.online).length);
      const today = new Date().toISOString().slice(0, 10);
      const todayUnknown = (unknown.items || []).filter(item => String(item.event_time || "").slice(0, 10) === today).length;
      setText("unknown-incidents", todayUnknown);
    } catch (error) {
      setText("application-status", "● Setup Required");
      setText("database-status", "Connected");
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
        return "<div style='display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid #e5e7eb'><div><strong>"+escapeHtml(camera.name)+"</strong><br><small>"+escapeHtml(camera.camera_id)+" · "+escapeHtml(camera.source_type)+" · "+escapeHtml(camera.camera_role)+" · Cloud managed</small></div><div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end'><strong style='color:"+statusColor+"'>● "+escapeHtml(state)+"</strong><button class='btn btn-light test-existing-camera' data-id='"+escapeHtml(camera.camera_id)+"' data-edge-id='"+escapeHtml(camera.edge_id||selectedEdge)+"' data-source-type='"+escapeHtml(camera.source_type||"")+"'>Test</button><button class='btn btn-light edit-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Edit</button><button class='btn btn-light delete-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Delete</button></div></div>";
      }).join("");
      const localHtml=localOnly.map(camera=>"<div style='display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 0;border-bottom:1px solid #e5e7eb'><div><strong>"+escapeHtml(camera.name||camera.camera_id)+"</strong><br><small>"+escapeHtml(camera.camera_id)+" · "+escapeHtml(camera.source_type||"camera")+" · "+escapeHtml(camera.camera_role||"GENERAL")+" · Edge discovered</small></div><div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end'><strong style='color:"+(camera.online?"#166534":"#6b7280")+"'>● "+(camera.online?"Online":"Offline")+"</strong><button class='btn btn-light test-existing-camera' data-id='"+escapeHtml(camera.camera_id)+"' data-edge-id='"+escapeHtml(selectedEdge)+"' data-source-type='"+escapeHtml(camera.source_type||"")+"'>Test</button><button class='btn btn-light adopt-local-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Configure</button></div></div>").join("");
      container.innerHTML=configuredHtml+localHtml;
      container.querySelectorAll(".test-existing-camera").forEach(button=>button.addEventListener("click",()=>testCameraConnection({cameraId:button.dataset.id,edgeId:button.dataset.edgeId,sourceType:button.dataset.sourceType,button})));
      container.querySelectorAll(".adopt-local-camera").forEach(button=>button.addEventListener("click",()=>adoptLocalCamera(localOnly.find(x=>x.camera_id===button.dataset.id))));
      container.querySelectorAll(".edit-camera").forEach(button=>button.addEventListener("click",()=>editCamera(items.find(x=>x.camera_id===button.dataset.id))));
      container.querySelectorAll(".delete-camera").forEach(button=>button.addEventListener("click",()=>deleteCamera(button.dataset.id)));
    }catch(error){container.innerHTML="<p>"+escapeHtml(error.message)+"</p>";}
  }

  function escapeHtml(value){const div=document.createElement("div");div.textContent=String(value??"");return div.innerHTML;}

  function editCamera(camera){
    document.getElementById("camera-name").value=camera.name||"";
    document.getElementById("camera-id").value=camera.camera_id||"";
    const editSource=document.getElementById("camera-source");
    editSource.value="";
    editSource.dataset.edgeLocalCamera="";
    editSource.dataset.cloudExistingCamera=camera.camera_id||"";
    editSource.placeholder=camera.source_configured ? "Stored securely; leave blank to keep existing source" : "RTSP URL, video file path or webcam index";
    document.getElementById("source-type").value=camera.source_type||"rtsp";
    document.getElementById("camera-zone").value=camera.camera_zone||"";
    document.getElementById("crowd-threshold").value=camera.crowd_threshold||10;
    const role=document.getElementById("camera-role"); role.value=camera.camera_role==="GENERAL"?"General":"Entry";
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
    role.value=(camera.camera_role==="ENTRANCE_EXIT")?"Entry":"General";
    showMessage("Camera loaded from edge. Choose role/features and save; source stays hidden on the local PC.");
    window.scrollTo({top:0,behavior:"smooth"});
  }

  async function deleteCamera(cameraId){
    if(!confirm("Delete camera "+cameraId+"?")) return;
    try{
      const scope=requireScope(["tenant_id","shop_id","edge_id"]);
      const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/cameras/"+encodeURIComponent(cameraId)+"?shop_id="+encodeURIComponent(scope.shop_id)+"&edge_id="+encodeURIComponent(scope.edge_id),{method:"DELETE"});
      const body=await response.json(); if(!response.ok) throw new Error(body.detail||"Unable to delete camera.");
      showMessage("Camera deleted from cloud configuration."); await loadConfiguredCameras();
    }catch(error){showMessage(error.message,true);}
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
      const checks = Array.from(document.querySelectorAll(".feature-card input[type=checkbox]"));
      const payload = {
        ...scope,
        company_code: scope.company_code || null,
        camera_id: cameraId,
        name,
        source_type: sourceType(source),
        source,
        camera_role: roleValue.toLowerCase() === "general" ? "GENERAL" : "ENTRANCE_EXIT",
        camera_zone: document.getElementById("camera-zone").value.trim() || null,
        crowd_threshold: Number(document.getElementById("crowd-threshold").value || 10),
        enabled: true,
        // These keys intentionally match CameraFeatures/apply_cloud_camera on the edge.
        // UI-only labels must never silently create feature names the edge ignores.
        features: {
          attendance: !!checks[0]?.checked,
          face_recognition: !!checks[1]?.checked,
          unknown_detection: !!checks[2]?.checked,
          shoplifting: !!checks[3]?.checked,
          object_security: !!checks[4]?.checked
        },
        settings: {
          // Keep the portal contract aligned with CameraManager.apply_cloud_camera.
          tracking_fps: Math.max(1, Math.round(30 / Math.max(1, Number(document.getElementById("frame-skip").value || 2)))),
          max_frame_width: Number(document.getElementById("max-width").value || 960),
          tracking_quality: "balanced",
          tracking_mode: "auto"
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
    if (role) role.value = "";
  }

  function wireCameraPage() {
    const save = document.getElementById("save-camera-btn");
    if (!save) return;
    save.addEventListener("click", saveCamera);
    document.getElementById("cancel-camera-btn")?.addEventListener("click", resetCameraForm);
    document.getElementById("test-camera-btn")?.addEventListener("click", testCameraConnection);
    document.getElementById("discover-camera-btn")?.addEventListener("click", discoverOnvifCamera);
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
      const crm=person.crm_mapping?"✓ CRM mapped":"CRM not mapped";
      return "<tr><td><div class='avatar'>"+escapeHtml(initials)+"</div></td>"+
        "<td><strong>"+escapeHtml(person.full_name||"")+"</strong><br><small>"+face+" · "+sync+" · "+crm+"</small></td>"+
        "<td>"+escapeHtml(person.employee_code||"")+"</td><td>"+escapeHtml(person.role||"")+"</td>"+
        "<td>"+(person.active?"Active":"Inactive")+"</td><td>"+escapeHtml(String(person.created_at||"").slice(0,10))+"</td>"+
        "<td><button class='btn deactivate-person' data-person-id='"+escapeHtml(person.person_id)+"'>"+(person.active?"Deactivate":"Inactive")+"</button></td></tr>";
    }).join("");
    body.querySelectorAll(".deactivate-person").forEach(button=>button.addEventListener("click",async()=>{
      if(button.textContent==="Inactive") return;
      try{
        const scope=requireScope(["tenant_id"]);
        const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/personnel/"+encodeURIComponent(button.dataset.personId),{method:"DELETE"});
        if(!response.ok) throw new Error((await response.json()).detail||"Unable to deactivate person.");
        showMessage("Personnel profile deactivated. The edge will receive the change automatically.");
        await loadPersonnel();
      }catch(error){showMessage(error.message,true);}
    }));
  }

  async function loadPersonnel(){
    if(!document.getElementById("personnel-table-body")) return;
    const scope=requireScope(["tenant_id"]);
    const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/personnel");
    const data=await response.json();
    if(!response.ok) throw new Error(data.detail||"Unable to load personnel.");
    renderPersonnel(data.items||[]);
  }

  async function addPersonnel(){
    const button=document.getElementById("add-person-btn");
    try{
      const scope=requireScope(["tenant_id"]);
      const name=document.getElementById("person-name").value.trim();
      const code=document.getElementById("employee-code").value.trim();
      const role=document.getElementById("person-role").value;
      const file=document.getElementById("face-image").files[0];
      if(!name||!code||!role) throw new Error("Name, employee code and role are required.");
      if(!file) throw new Error("Upload one clear front-facing face image.");
      button.disabled=true; button.textContent="Creating & enrolling…";
      let response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/personnel",{
        method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({full_name:name,employee_code:code,role})
      });
      let person=await response.json();
      if(!response.ok) throw new Error(person.detail||"Unable to create personnel.");
      const form=new FormData(); form.append("file",file);
      response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/personnel/"+encodeURIComponent(person.id)+"/faces",{method:"POST",body:form,timeoutMs:120000});
      const face=await response.json();
      if(!response.ok){
        await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/personnel/"+encodeURIComponent(person.id),{method:"DELETE"});
        throw new Error(face.detail||"Face enrollment failed.");
      }
      ["person-name","employee-code","face-image"].forEach(id=>document.getElementById(id).value="");
      document.getElementById("person-role").value="";
      showMessage("Person enrolled successfully. The assigned edge will sync the face automatically.");
      await loadPersonnel();
    }catch(error){showMessage(error.message,true);}
    finally{if(button){button.disabled=false;button.textContent="+ Add Person";}}
  }

  function wirePersonnelPage(){
    const button=document.getElementById("add-person-btn");
    if(!button) return;
    button.addEventListener("click",addPersonnel);
    document.getElementById("focus-add-person-btn")?.addEventListener("click",()=>document.getElementById("person-name")?.focus());
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
    if(events) events.innerHTML=ev.length?ev.map(e=>"<tr><td>"+fmtTime(e.event_time)+"</td><td><strong>"+escapeHtml(e.full_name||"Unknown")+"</strong></td><td>"+escapeHtml(String(e.event_type||"").replaceAll("_"," "))+"</td><td>"+escapeHtml(e.camera_id||"—")+"</td><td>—</td><td>—</td><td>"+(e.confidence!=null?Math.round(Number(e.confidence)*100)+"%":"—")+"</td><td>—</td></tr>").join(""):"<tr><td colspan='8'>No person events received yet.</td></tr>";
  }
  async function loadAttendance(){
    if(!document.getElementById("attendance-records-body"))return;
    const scope=requireScope(["tenant_id"]); const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/attendance");
    const data=await response.json(); if(!response.ok)throw new Error(data.detail||"Unable to load attendance."); renderAttendance(data);
  }
  function wireAttendancePage(){if(!document.getElementById("attendance-records-body"))return;loadAttendance().catch(error=>showMessage(error.message,true));setInterval(()=>loadAttendance().catch(()=>{}),15000);}

  bootstrapCrmSession().then(()=>{loadSystemStatus();wireEdgeSetup();wireCameraPage();wirePersonnelPage();wireAttendancePage();}).catch(error=>showMessage(error.message,true));
})();
