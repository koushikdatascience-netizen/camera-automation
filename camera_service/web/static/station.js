// Local attendance confirmations consume existing AI recognition observations.
function clearStationCandidate() {
    stationCandidate = null;
    document.getElementById('focus-person-name').textContent = 'Waiting for a recognized person';
    document.getElementById('focus-person-time').textContent = '';
    const photo = document.getElementById('focus-person-photo');
    photo.hidden = true; photo.removeAttribute('src');
    document.getElementById('station-actions').replaceChildren();
    for (const id of ['focus-time-in','focus-time-out','focus-duration']) document.getElementById(id).textContent = '-';
}
async function loadAttendanceStation() {
    const cameraId = selectedLiveCameraId;
    const camera = liveCameras.find(item => item.camera_id === cameraId);
    if (!livePreviewOpen || liveView !== 'focus' || camera?.camera_role !== 'ENTRANCE_EXIT') {
        clearStationCandidate(); document.getElementById('station-recent').replaceChildren(); return;
    }
    try {
        const response = await fetch(`${API_BASE}/cameras/${encodeURIComponent(cameraId)}/attendance-station`);
        if (!response.ok) throw new Error('Attendance Station unavailable');
        const data = await response.json();
        if (cameraId !== selectedLiveCameraId || !livePreviewOpen || liveView !== 'focus') return;
        const candidate = data.candidate;
        if (!candidate || Date.now() >= new Date(candidate.expires_at).getTime()) clearStationCandidate();
        else {
            stationCandidate = candidate;
            document.getElementById('focus-person-name').textContent = `${candidate.full_name} · ${candidate.employee_code}`;
            document.getElementById('focus-person-time').textContent = `${candidate.state} · ${Math.round(candidate.confidence*100)}% · ${formatDateTime(candidate.detected_at)}`;
            document.getElementById('focus-time-in').textContent = 'Current state';
            document.getElementById('focus-time-out').textContent = candidate.state;
            document.getElementById('focus-duration').textContent = 'Recognition valid for 15 seconds';
            const photo = document.getElementById('focus-person-photo'); photo.hidden = !candidate.snapshot_url;
            if (candidate.snapshot_url) {
                const url = `${candidate.snapshot_url}?t=${encodeURIComponent(candidate.detected_at)}`;
                if (photo.getAttribute('src') !== url) photo.src = url;
            } else photo.removeAttribute('src');
            const labels = {CHECK_IN:'Check In', CHECK_OUT:'Check Out', START_BREAK:'Start Break', END_BREAK:'End Break'};
            const actions = document.getElementById('station-actions'); actions.replaceChildren();
            for (const action of candidate.actions) {
                const button = document.createElement('button'); button.className = 'btn btn-success'; button.type = 'button';
                button.textContent = labels[action]; button.disabled = stationBusy;
                button.addEventListener('click', () => confirmStationAction(action)); actions.appendChild(button);
            }
        }
        document.getElementById('station-recent').innerHTML = '<h4>Recent attendance</h4>' + ((data.recent || []).map(item => {
            const snapshot = item.arrival_snapshot ? `<img width="48" height="48" style="object-fit:cover" alt="Attendance snapshot" src="${API_BASE}/attendance/${encodeURIComponent(item.id)}/snapshots/arrival">` : '';
            return `<div class="activity-row">${snapshot}<div><strong>${escapeHtmlLocal(item.full_name)} · ${escapeHtmlLocal(item.employee_code)}</strong><small>In ${escapeHtmlLocal(formatDateTime(item.arrival_time))} · Out ${item.exit_time ? escapeHtmlLocal(formatDateTime(item.exit_time)) : escapeHtmlLocal(item.attendance_state || 'Observation')}<br>Break ${item.last_break_start ? escapeHtmlLocal(formatDateTime(item.last_break_start)) : '-'} / ${item.last_break_end ? escapeHtmlLocal(formatDateTime(item.last_break_end)) : '-'} · ${escapeHtmlLocal(formatOperatorDuration(item.arrival_time,item.exit_time))}</small></div></div>`;
        }).join('') || '<div class="activity-empty">No attendance yet</div>');
    } catch (error) {
        clearStationCandidate(); document.getElementById('focus-control-message').textContent = error.message;
    }
}
async function confirmStationAction(action) {
    if (stationBusy || !stationCandidate || Date.now() >= new Date(stationCandidate.expires_at).getTime()) { clearStationCandidate(); return; }
    const cameraId = selectedLiveCameraId, candidate = stationCandidate;
    stationBusy = true;
    document.querySelectorAll('#station-actions button').forEach(button => button.disabled = true);
    try {
        const response = await fetch(`${API_BASE}/cameras/${encodeURIComponent(cameraId)}/attendance-station/action`, {
            method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({person_id:candidate.person_id, token:candidate.token,
            action, expected_state:candidate.state, request_id:crypto.randomUUID()})});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || 'Action rejected');
        document.getElementById('focus-control-message').textContent = 'Attendance saved locally';
    } catch (error) { document.getElementById('focus-control-message').textContent = error.message; }
    finally { stationBusy = false; await loadAttendanceStation(); }
}
document.getElementById('close-live-view').addEventListener('click', () => {
    livePreviewOpen = false;
    document.querySelectorAll('#live-camera-grid img, #focus-camera-image').forEach(img => img.removeAttribute('src'));
    switchLiveView('focus'); clearStationCandidate();
});
setInterval(() => {
    if (stationCandidate && Date.now() >= new Date(stationCandidate.expires_at).getTime()) clearStationCandidate();
}, 500);

// Fetch videos only after a click, then release the player on every close.
const evidenceDialog = document.createElement('dialog');
evidenceDialog.style.cssText = 'width:min(900px,90vw);background:#101828;color:white;border:0;border-radius:12px';
evidenceDialog.innerHTML = '<button type="button" class="btn btn-secondary">Close video</button><video controls preload="none" style="width:100%;max-height:75vh"></video>';
document.body.appendChild(evidenceDialog);
const evidenceVideo = evidenceDialog.querySelector('video');
evidenceDialog.querySelector('button').addEventListener('click', () => evidenceDialog.close());
evidenceDialog.addEventListener('close', () => {
    evidenceVideo.pause(); evidenceVideo.removeAttribute('src'); evidenceVideo.load();
});
document.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link || !/^\/api\/v1\/(unknown-incidents|security-alerts)\/[^/]+\/clip$/.test(link.getAttribute('href') || '')) return;
    event.preventDefault(); evidenceVideo.src = link.getAttribute('href'); evidenceDialog.showModal();
    evidenceVideo.play().catch(() => {});
});
window.addEventListener('pagehide', () => {
    document.querySelectorAll('#live-camera-grid img, #focus-camera-image').forEach(img => img.removeAttribute('src'));
    evidenceDialog.close();
});
