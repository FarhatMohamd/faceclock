/* =====================================================================
   FaceClock - kiosk client
   ---------------------------------------------------------------------
   * Face identification with a short burst of frames (basic liveness)
   * One-key confirmation (ENTER) or "Not me" correction (ESC -> PIN)
   * Code + secret PIN fallback (camera failure, not recognised, no consent)
   * Offline queue: punches are stored locally and synced when the
     server is reachable again
   ===================================================================== */
(() => {
  'use strict';

  // -------------------------------------------------------------------
  // Translations (kiosk is used by every employee: FR / EN)
  // -------------------------------------------------------------------
  const I18N = {
    en: {
      welcome: 'Welcome', lookCamera: 'Look at the camera to clock in.', usePin: 'Use my code + PIN',
      keysIdle: 'P: code + PIN', hello: 'Hello', notMe: 'Not me (Esc)', confirm: 'Confirm (Enter)',
      keysConfirm: 'Enter: confirm · Esc: not me · 1-5: action · A-D: break type',
      enterCode: 'Enter your employee code', enterPin: 'Enter your secret PIN',
      cancel: 'Cancel (Esc)', next: 'OK (Enter)', cameraOff: 'Camera unavailable — use your code + PIN',
      offline: 'Server unreachable — offline mode: punches are saved and will be sent automatically',
      forgotten: 'Your previous shift was not closed. Your manager will be notified.',
      noFace: '', notRecognized: 'Face not recognised. Try again or use your code + PIN.',
      multiple: 'One person at a time, please.', tooSmall: 'Please come closer.',
      liveness: 'Please look at the camera and move slightly.', checking: 'Identifying…',
      recorded: 'Recorded', queued: 'Saved offline', error: 'Error', notConfigured: 'Kiosk not configured: open /kiosk?token=…',
      states: { OFF: 'Not clocked in', WORKING: 'Working', ON_BREAK: 'On break' },
      actions: { IN: 'Arrival', OUT: 'Departure', BREAK_START: 'Start break', BREAK_END: 'End break', SITE_CHANGE: 'Arriving in this building' },
      breaks: {}, pending: n => `${n} punch(es) waiting to be sent`, at: 'at',
    },
    fr: {
      welcome: 'Bienvenue', lookCamera: 'Regardez la caméra pour pointer.', usePin: 'Utiliser mon code + PIN',
      keysIdle: 'P : code + PIN', hello: 'Bonjour', notMe: "Ce n'est pas moi (Échap)", confirm: 'Valider (Entrée)',
      keysConfirm: 'Entrée : valider · Échap : pas moi · 1-5 : action · A-D : type de pause',
      enterCode: 'Saisissez votre matricule', enterPin: 'Saisissez votre code secret',
      cancel: 'Annuler (Échap)', next: 'OK (Entrée)', cameraOff: 'Caméra indisponible — utilisez votre code + PIN',
      offline: 'Serveur injoignable — mode hors ligne : les pointages sont enregistrés et seront envoyés automatiquement',
      forgotten: "Votre précédente journée n'a pas été clôturée. Votre responsable sera informé.",
      noFace: '', notRecognized: 'Visage non reconnu. Réessayez ou utilisez votre code + PIN.',
      multiple: 'Une seule personne à la fois, merci.', tooSmall: 'Approchez-vous de la caméra.',
      liveness: 'Regardez la caméra en bougeant légèrement.', checking: 'Identification…',
      recorded: 'Pointage enregistré', queued: 'Enregistré hors ligne', error: 'Erreur', notConfigured: 'Borne non configurée : ouvrez /kiosk?token=…',
      states: { OFF: 'Non pointé', WORKING: 'En poste', ON_BREAK: 'En pause' },
      actions: { IN: 'Arrivée', OUT: 'Départ', BREAK_START: 'Début de pause', BREAK_END: 'Fin de pause', SITE_CHANGE: 'Arrivée dans ce bâtiment' },
      breaks: { SMOKE: 'Pause cigarette', COFFEE: 'Pause café', LUNCH: 'Déjeuner', PERSONAL: 'Pause personnelle' },
      pending: n => `${n} pointage(s) en attente d'envoi`, at: 'à',
    },
  };
  let lang = localStorage.getItem('fc_lang') || ((navigator.language || 'en').startsWith('fr') ? 'fr' : 'en');
  const t = key => I18N[lang][key];

  // -------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  if (params.get('token')) {
    localStorage.setItem('fc_kiosk_token', params.get('token'));
    history.replaceState(null, '', location.pathname);          // hide the token from the URL bar
  }
  const TOKEN = localStorage.getItem('fc_kiosk_token');
  const ALL_ACTIONS = ['IN', 'BREAK_START', 'BREAK_END', 'OUT'];
  const BREAK_KEYS = ['a', 'b', 'c', 'd', 'e', 'f'];

  const S = {
    mode: 'idle', config: null, offline: false, cameraOk: false, inflight: false,
    current: null, action: null, breakType: null, ttlTimer: null,
    pin: { step: 'code', code: '', reason: 'USER_CHOICE' },
    failures: 0, lastThumb: null, lastAttempt: 0, cooldownUntil: 0,
  };

  // -------------------------------------------------------------------
  // Server API (network errors and 5xx switch the kiosk to offline mode)
  // -------------------------------------------------------------------
  async function api(path, body, method = 'POST') {
    let res;
    try {
      res = await fetch(`/api/kiosk${path}`, {
        method, headers: { 'Content-Type': 'application/json', 'X-Kiosk-Token': TOKEN || '' },
        body: method === 'GET' ? undefined : JSON.stringify(body || {}),
      });
    } catch (e) { setOffline(true); throw { offline: true }; }
    if (res.status >= 500) { setOffline(true); throw { offline: true }; }
    setOffline(false);
    const data = await res.json().catch(() => ({ ok: false, error: 'BAD_RESPONSE' }));
    return { status: res.status, data };
  }

  function setOffline(value) {
    if (S.offline === value) return;
    S.offline = value;
    $('offline-banner').classList.toggle('hidden', !value);
    if (!value) flushQueue();
  }

  // -------------------------------------------------------------------
  // Offline queue (localStorage)
  // -------------------------------------------------------------------
  const queue = {
    load: () => JSON.parse(localStorage.getItem('fc_queue') || '[]'),
    save: q => { localStorage.setItem('fc_queue', JSON.stringify(q)); renderQueueInfo(); },
    push(item) { const q = this.load(); q.push(item); this.save(q); },
  };

  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
    });
  }

  async function flushQueue() {
    const items = queue.load();
    if (!items.length || S.offline) return;
    try {
      const { data } = await api('/offline-sync', { punches: items });
      const done = new Set((data.results || []).map(r => r.client_uuid));
      (data.results || []).filter(r => !r.ok).forEach(r => console.warn('Offline punch refused', r));
      queue.save(queue.load().filter(i => !done.has(i.client_uuid)));
    } catch (e) { /* still offline, retry later */ }
  }

  function renderQueueInfo() {
    const n = queue.load().length;
    $('queue-info').textContent = n ? t('pending')(n) : '';
  }

  // -------------------------------------------------------------------
  // Camera
  // -------------------------------------------------------------------
  const video = $('video');
  const canvas = document.createElement('canvas');

  async function startCamera() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' }, audio: false });
      video.srcObject = stream;
      stream.getVideoTracks()[0].addEventListener('ended', () => cameraFailed('track ended'));
      S.cameraOk = true;
      $('camera-off').classList.add('hidden');
      api('/heartbeat', { camera_ok: true }).catch(() => {});
    } catch (e) {
      cameraFailed(e.name || String(e));
    }
  }

  function cameraFailed(message) {
    S.cameraOk = false;
    $('camera-off').classList.remove('hidden');
    api('/heartbeat', { camera_ok: false, message }).catch(() => {});
  }

  function grabFrame(width = 640) {
    const ratio = video.videoHeight / video.videoWidth || 0.75;
    canvas.width = width; canvas.height = Math.round(width * ratio);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', 0.85);
  }

  // Cheap scene-change detector: avoid sending frames when nobody is there.
  function sceneChanged() {
    if (!video.videoWidth) return false;
    const c = document.createElement('canvas'); c.width = 32; c.height = 24;
    const ctx = c.getContext('2d'); ctx.drawImage(video, 0, 0, 32, 24);
    const px = ctx.getImageData(0, 0, 32, 24).data;
    const gray = new Float32Array(32 * 24);
    for (let i = 0; i < gray.length; i++) gray[i] = (px[i * 4] + px[i * 4 + 1] + px[i * 4 + 2]) / 3;
    let diff = 255;
    if (S.lastThumb) { diff = 0; for (let i = 0; i < gray.length; i++) diff += Math.abs(gray[i] - S.lastThumb[i]); diff /= gray.length; }
    S.lastThumb = gray;
    return diff > 4 || Date.now() - S.lastAttempt > 4000;
  }

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  // -------------------------------------------------------------------
  // Identification loop
  // -------------------------------------------------------------------
  async function tick() {
    if (S.mode !== 'idle' || S.inflight || S.offline || !S.cameraOk || !S.config || !S.config.biometrics) return;
    if (Date.now() < S.cooldownUntil || !sceneChanged()) return;
    S.inflight = true; S.lastAttempt = Date.now();
    try {
      const frames = [];
      for (let i = 0; i < 3; i++) { frames.push(grabFrame()); await sleep(220); }
      if (S.mode !== 'idle') return;
      const { data } = await api('/identify', { frames });
      if (data.ok) { S.failures = 0; showConfirm(data); return; }
      handleIdentifyError(data.error);
    } catch (e) { /* offline: handled by api() */ }
    finally { S.inflight = false; }
  }

  function handleIdentifyError(code) {
    const messages = { NO_FACE: t('noFace'), NOT_RECOGNIZED: t('notRecognized'), AMBIGUOUS: t('notRecognized'),
                       MULTIPLE_FACES: t('multiple'), FACE_TOO_SMALL: t('tooSmall'), LIVENESS_FAILED: t('liveness') };
    $('idle-status').textContent = messages[code] ?? '';
    if (code === 'NOT_RECOGNIZED' || code === 'AMBIGUOUS') {
      S.failures += 1; S.cooldownUntil = Date.now() + 2500;
      if (S.failures >= 3) { S.failures = 0; openPin('NOT_RECOGNIZED'); }
    }
    if (code === 'ENGINE_UNAVAILABLE') S.config.biometrics = false;
  }

  // -------------------------------------------------------------------
  // Views
  // -------------------------------------------------------------------
  function show(view) {
    ['idle', 'confirm', 'pin', 'result'].forEach(v => $(`view-${v}`).classList.toggle('hidden', v !== view));
    S.mode = view;
  }

  function goIdle() {
    clearInterval(S.ttlTimer);
    S.current = null; S.action = null; S.breakType = null;
    $('idle-status').textContent = S.config && !S.config.biometrics ? t('cameraOff') : '';
    show('idle');
  }

  function actionLabel(a) { return t('actions')[a] || a; }
  function breakLabel(b) { return t('breaks')[b.code] || b.label; }

  function showConfirm(data) {
    S.current = data;
    S.action = data.suggested_action || data.allowed_actions[0];
    S.breakType = null;
    $('emp-name').textContent = data.employee.name;
    let state = t('states')[data.state] || data.state;
    if (data.state === 'ON_BREAK' && data.on_break_type) {
      const bt = (S.config.break_types || []).find(b => b.code === data.on_break_type);
      if (bt) state += ` · ${breakLabel(bt)}`;
    }
    $('emp-state').textContent = state;
    $('forgot-warning').classList.toggle('hidden', !data.previous_shift_forgotten);
    renderActions();
    $('btn-confirm').textContent = t('confirm');
    show('confirm');
    startTtl(data.ttl || 30);
  }

  function renderActions() {
    const box = $('actions'); box.innerHTML = '';
    S.current.allowed_actions.forEach((a, i) => {
      const b = document.createElement('button');
      b.innerHTML = `<span class="k">${i + 1}</span>${actionLabel(a)}`;
      b.classList.toggle('selected', a === S.action);
      b.onclick = () => selectAction(a);
      box.appendChild(b);
    });
    const bbox = $('break-types');
    bbox.classList.toggle('hidden', S.action !== 'BREAK_START');
    bbox.innerHTML = '';
    if (S.action === 'BREAK_START') {
      const types = S.config.break_types || [];
      if (!S.breakType && types.length) S.breakType = types[0].code;
      types.forEach((bt, i) => {
        const b = document.createElement('button');
        b.innerHTML = `<span class="k">${(BREAK_KEYS[i] || '').toUpperCase()}</span>${breakLabel(bt)}`;
        b.classList.toggle('selected', bt.code === S.breakType);
        b.onclick = () => { S.breakType = bt.code; renderActions(); };
        bbox.appendChild(b);
      });
    }
  }

  function selectAction(a) { S.action = a; renderActions(); }

  function startTtl(seconds) {
    clearInterval(S.ttlTimer);
    let left = seconds;
    $('ttl-bar').style.width = '100%';
    S.ttlTimer = setInterval(() => {
      left -= 1;
      $('ttl-bar').style.width = `${Math.max(0, left / seconds * 100)}%`;
      if (left <= 0) goIdle();
    }, 1000);
  }

  async function confirmPunch() {
    if (S.mode !== 'confirm' || !S.current) return;
    clearInterval(S.ttlTimer);
    const action = S.action, breakType = action === 'BREAK_START' ? S.breakType : null;

    if (S.current.offline) {                         // offline: queue locally
      queue.push({ client_uuid: uuid(), employee_code: S.current.code, pin: S.current.pin,
                   action, break_type: breakType, ts: new Date().toISOString() });
      return showResult(true, t('queued'), actionLabel(action));
    }
    try {
      const { data } = await api('/confirm', { ticket: S.current.ticket, action, break_type: breakType });
      if (data.ok) {
        const time = new Date().toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit' });
        showResult(true, `${t('recorded')}`, `${data.employee.name} — ${actionLabel(action)} ${t('at')} ${time}`);
      } else {
        showResult(false, t('error'), data.message);
      }
    } catch (e) {
      // Lost connection between identification and confirmation: queue only if
      // we know the employee code + PIN (PIN flow); a face ticket alone is not enough.
      showResult(false, t('error'), t('offline'));
    }
  }

  async function notMe() {
    if (S.mode !== 'confirm') return;
    clearInterval(S.ttlTimer);
    if (S.current && S.current.method === 'FACE' && S.current.ticket) {
      api('/reject', { ticket: S.current.ticket }).catch(() => {});
      openPin('MISIDENTIFIED');
    } else {
      goIdle();
    }
  }

  function showResult(ok, title, detail) {
    $('result-icon').textContent = ok ? '✔' : '✖';
    $('result-icon').classList.toggle('bad', !ok);
    $('result-title').textContent = title;
    $('result-detail').textContent = detail || '';
    show('result');
    beep(ok);
    S.cooldownUntil = Date.now() + 3500;           // do not re-identify the same person immediately
    setTimeout(() => { if (S.mode === 'result') goIdle(); }, ok ? 2800 : 4500);
  }

  // -------------------------------------------------------------------
  // Code + secret PIN
  // -------------------------------------------------------------------
  function openPin(reason) {
    clearInterval(S.ttlTimer);
    S.pin = { step: 'code', code: '', reason: reason || (S.cameraOk ? 'USER_CHOICE' : 'CAMERA_FAILURE') };
    $('pin-title').textContent = t('enterCode');
    $('pin-input').type = 'text';
    $('pin-input').value = '';
    $('pin-error').textContent = '';
    show('pin');
    $('pin-input').focus();
  }

  async function pinNext() {
    const value = $('pin-input').value.trim();
    if (!value) return;
    if (S.pin.step === 'code') {
      S.pin.code = value; S.pin.step = 'pin';
      $('pin-title').textContent = t('enterPin');
      $('pin-input').type = 'password'; $('pin-input').value = ''; $('pin-input').focus();
      return;
    }
    const pin = value;
    if (S.offline) {                                // PIN is checked by the server at sync time
      return showConfirm({ offline: true, code: S.pin.code, pin, method: 'OFFLINE', ticket: null,
                           employee: { name: S.pin.code }, state: '', allowed_actions: ALL_ACTIONS,
                           suggested_action: 'IN', ttl: 45 });
    }
    try {
      const { data } = await api('/pin', { employee_code: S.pin.code, pin, reason: S.pin.reason });
      if (data.ok) return showConfirm(data);
      $('pin-error').textContent = data.message;
      openPinKeepError(data.message);
    } catch (e) {
      // server went down while typing: retry the same step offline
      showConfirm({ offline: true, code: S.pin.code, pin, method: 'OFFLINE', ticket: null,
                    employee: { name: S.pin.code }, state: '', allowed_actions: ALL_ACTIONS,
                    suggested_action: 'IN', ttl: 45 });
    }
  }

  function openPinKeepError(message) {
    openPin(S.pin.reason);
    $('pin-error').textContent = message;
  }

  function buildKeypad() {
    const pad = $('keypad');
    ['1', '2', '3', '4', '5', '6', '7', '8', '9', '⌫', '0', '✔'].forEach(k => {
      const b = document.createElement('button'); b.textContent = k;
      b.onclick = () => {
        const input = $('pin-input');
        if (k === '⌫') input.value = input.value.slice(0, -1);
        else if (k === '✔') pinNext();
        else input.value += k;
        input.focus();
      };
      pad.appendChild(b);
    });
  }

  // -------------------------------------------------------------------
  // Keyboard shortcuts
  // -------------------------------------------------------------------
  document.addEventListener('keydown', e => {
    const k = e.key;
    if (S.mode === 'idle') {
      if (k === 'p' || k === 'P' || k === 'Enter') { e.preventDefault(); openPin(); }
    } else if (S.mode === 'confirm') {
      if (k === 'Enter') { e.preventDefault(); confirmPunch(); }
      else if (k === 'Escape') { e.preventDefault(); notMe(); }
      else if (/^[1-9]$/.test(k)) {
        const a = S.current.allowed_actions[Number(k) - 1]; if (a) selectAction(a);
      } else if (S.action === 'BREAK_START' && BREAK_KEYS.includes(k.toLowerCase())) {
        const bt = (S.config.break_types || [])[BREAK_KEYS.indexOf(k.toLowerCase())];
        if (bt) { S.breakType = bt.code; renderActions(); }
      }
    } else if (S.mode === 'pin') {
      if (k === 'Enter') { e.preventDefault(); pinNext(); }
      else if (k === 'Escape') { e.preventDefault(); goIdle(); }
    } else if (S.mode === 'result') {
      goIdle();
    }
  });

  // -------------------------------------------------------------------
  // Misc: clock, sound, language
  // -------------------------------------------------------------------
  function renderClock() {
    const now = new Date();
    $('clock-time').textContent = now.toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    $('clock-date').textContent = now.toLocaleDateString(lang, { weekday: 'long', day: 'numeric', month: 'long' });
  }

  function beep(ok) {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator(); const gain = ctx.createGain();
      osc.frequency.value = ok ? 880 : 220; gain.gain.value = 0.08;
      osc.connect(gain); gain.connect(ctx.destination);
      osc.start(); osc.stop(ctx.currentTime + (ok ? 0.15 : 0.4));
    } catch (e) { /* audio not available */ }
  }

  function applyLang() {
    document.documentElement.lang = lang;
    document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
    $('lang-btn').textContent = lang === 'fr' ? 'EN' : 'FR';
    renderClock(); renderQueueInfo();
    if (S.mode === 'confirm') { renderActions(); $('btn-confirm').textContent = t('confirm'); }
  }

  $('lang-btn').onclick = () => { lang = lang === 'fr' ? 'en' : 'fr'; localStorage.setItem('fc_lang', lang); applyLang(); };
  $('btn-pin').onclick = () => openPin();
  $('btn-confirm').onclick = confirmPunch;
  $('btn-notme').onclick = notMe;
  $('btn-pin-next').onclick = pinNext;
  $('btn-pin-cancel').onclick = goIdle;

  // -------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------
  async function loadConfig() {
    try {
      const { status, data } = await api('/config', null, 'GET');
      if (status === 401) { $('idle-status').textContent = t('notConfigured'); return false; }
      S.config = data;
      $('site-name').textContent = data.site.name;
      $('kiosk-name').textContent = data.kiosk.name;
      return true;
    } catch (e) {
      // Offline at boot: keep a cached configuration so PIN punches still work
      S.config = JSON.parse(localStorage.getItem('fc_config') || 'null') ||
                 { biometrics: false, break_types: [], site: { name: '' }, kiosk: { name: '' } };
      return true;
    } finally {
      if (S.config && S.config.site) localStorage.setItem('fc_config', JSON.stringify(S.config));
    }
  }

  async function boot() {
    buildKeypad(); applyLang(); setInterval(renderClock, 1000);
    if (!TOKEN) { $('idle-status').textContent = t('notConfigured'); return; }
    await loadConfig();
    $('site-name').textContent = (S.config.site || {}).name || '';
    $('kiosk-name').textContent = (S.config.kiosk || {}).name || '';
    goIdle();
    if (S.config.biometrics) await startCamera(); else $('camera-off').classList.remove('hidden');
    setInterval(tick, 700);
    setInterval(() => api('/heartbeat', { camera_ok: S.cameraOk }).then(flushQueue).catch(() => {}), 30000);
    flushQueue();
  }

  boot();
})();
