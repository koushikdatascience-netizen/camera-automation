(() => {
  const params = new URLSearchParams(window.location.search);
  const scopeKeys = ["tenant_id", "company_code", "shop_id", "site_id", "edge_id"];

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
        fetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/summary"),
        fetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/cameras"),
        fetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/events?event_type=UNKNOWN_PERSON&limit=500")
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
    if (/^rtsp:\/\//i.test(source)) return "rtsp";
    if (/^\d+$/.test(source.trim())) return "webcam";
    return "file";
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
      const response = await fetch("/portal/v1/tenants/" + encodeURIComponent(scope.tenant_id) + "/cameras/" + encodeURIComponent(cameraId), {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Unable to save camera.");
      showMessage("Camera configuration saved to the cloud. The assigned edge will apply it when configuration synchronization is enabled.");
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
    document.getElementById("test-camera-btn")?.addEventListener("click", () => {
      showMessage("RTSP connection testing must run on the assigned edge device because customer camera URLs are normally LAN-only.");
    });
  }

  loadSystemStatus();
  wireCameraPage();
})();