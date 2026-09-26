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
    return fetch(url,{...options,headers});
  }
  async function bootstrapCrmSession() {
    const token=portalToken(); if(!token) return;
    const response=await authFetch("/session/status");
    if(!response.ok) throw new Error("Your Madhushala Camera session is invalid or expired.");
    const session=await response.json();
    const values={tenant_id:session.tenantId,company_code:session.companyCode,shop_id:session.shopCode};
    Object.entries(values).forEach(([key,value])=>{if(value) sessionStorage.setItem("snapkey_"+key,String(value));});
    if(session.displayName) sessionStorage.setItem("snapkey_display_name",session.displayName);
    if(session.role) sessionStorage.setItem("snapkey_role",session.role);
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
      throw new Error("Portal scope is not configured: " + missing.join(", ") + ". Open the portal from the authenticated Madhushala shop context.");
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

  async function loadSystemStatus() {
    if (!document.getElementById("configured-cameras")) return;
    try {
      const scope = requireScope(["tenant_id"]);
      const [healthResponse, summaryResponse, cameraResponse, unknownResponse] = await Promise.all([
        fetch("/health"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/summary"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/cameras"),
        authFetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/events?event_type=UNKNOWN_PERSON&limit=500")
      ]);
      if (!healthResponse.ok || !summaryResponse.ok || !cameraResponse.ok || !unknownResponse.ok) throw new Error("Portal API is unavailable.");
      const health = await healthResponse.json();
      await summaryResponse.json();
      const cameras = await cameraResponse.json();
      const unknown = await unknownResponse.json();
      setText("application-status", health.status === "ok" ? "● Online" : "● Degraded");
      setText("database-status", "● Connected");
      setText("configured-cameras", (cameras.items || []).length);
      const online = (cameras.items || []).filter(camera => camera.status && camera.status.online).length;
      setText("online-cameras", online);
      const today = new Date().toISOString().slice(0, 10);
      const todayUnknown = (unknown.items || []).filter(item => String(item.event_time || "").slice(0, 10) === today).length;
      setText("unknown-incidents", todayUnknown);
    } catch (error) {
      setText("application-status", "● Setup Required");
      setText("database-status", "Connected");
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

  async function createEdgeCommand(commandType, request) {
    const scope=requireScope(["tenant_id","shop_id","edge_id"]);
    const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/edge-commands",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tenant_id:scope.tenant_id,shop_id:scope.shop_id,edge_id:scope.edge_id,command_type:commandType,request})
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
    throw new Error("Edge device did not respond within 30 seconds. Confirm the edge agent is online.");
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

  async function testCameraConnection(){
    try{
      const source=document.getElementById("camera-source").value.trim();
      if(!source) throw new Error("Enter or discover a camera source first.");
      showMessage("Testing camera from the assigned edge device...");
      const command=await createEdgeCommand("CAMERA_TEST",{source});
      const result=await waitForEdgeCommand(command.id);
      if(result.connected===false || result.success===false) throw new Error(result.message||result.error||"Camera connection failed.");
      showMessage(result.message || "Camera connection succeeded on the edge device.");
    }catch(error){showMessage(error.message,true);}
  }

  async function loadConfiguredCameras(){
    const container=document.getElementById("configured-camera-list");
    if(!container) return;
    try{
      const scope=requireScope(["tenant_id","shop_id","edge_id"]);
      const response=await authFetch("/portal/v1/tenants/"+encodeURIComponent(scope.tenant_id)+"/cameras?shop_id="+encodeURIComponent(scope.shop_id)+"&edge_id="+encodeURIComponent(scope.edge_id));
      const body=await response.json(); if(!response.ok) throw new Error(body.detail||"Unable to load cameras.");
      const items=body.items||[];
      if(!items.length){container.innerHTML="<p>No cameras configured for this edge yet.</p>";return;}
      container.innerHTML=items.map(camera=>"<div style='display:flex;justify-content:space-between;align-items:center;padding:14px 0;border-bottom:1px solid #e5e7eb'><div><strong>"+escapeHtml(camera.name)+"</strong><br><small>"+escapeHtml(camera.camera_id)+" · "+escapeHtml(camera.source_type)+" · "+escapeHtml(camera.camera_role)+"</small></div><div><button class='btn btn-light edit-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Edit</button> <button class='btn btn-light delete-camera' data-id='"+escapeHtml(camera.camera_id)+"'>Delete</button></div></div>").join("");
      container.querySelectorAll(".edit-camera").forEach(button=>button.addEventListener("click",()=>editCamera(items.find(x=>x.camera_id===button.dataset.id))));
      container.querySelectorAll(".delete-camera").forEach(button=>button.addEventListener("click",()=>deleteCamera(button.dataset.id)));
    }catch(error){container.innerHTML="<p>"+escapeHtml(error.message)+"</p>";}
  }

  function escapeHtml(value){const div=document.createElement("div");div.textContent=String(value??"");return div.innerHTML;}

  function editCamera(camera){
    document.getElementById("camera-name").value=camera.name||"";
    document.getElementById("camera-id").value=camera.camera_id||"";
    document.getElementById("camera-source").value=camera.source||"";
    document.getElementById("source-type").value=camera.source_type||"rtsp";
    document.getElementById("camera-zone").value=camera.camera_zone||"";
    document.getElementById("crowd-threshold").value=camera.crowd_threshold||10;
    const role=document.getElementById("camera-role"); role.value=camera.camera_role==="GENERAL"?"General":"Entry";
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
      const source = document.getElementById("camera-source").value.trim();
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
        features: {
          person_detection: !!checks[0]?.checked,
          face_recognition: !!checks[1]?.checked,
          tracking: !!checks[2]?.checked,
          crowd_monitoring: !!checks[3]?.checked,
          object_security: !!checks[4]?.checked
        },
        settings: {
          tracking_frame_skip: Number(document.getElementById("frame-skip").value || 2),
          detection_interval: Number(document.getElementById("detection-interval").value || 10),
          max_frame_width: Number(document.getElementById("max-width").value || 960),
          face_recognition_skip: Number(document.getElementById("face-skip").value || 2)
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

  bootstrapCrmSession().then(()=>{loadSystemStatus();wireCameraPage();}).catch(error=>showMessage(error.message,true));
})();