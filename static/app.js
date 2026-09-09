'use strict';

(() => {
  const el = (id) => document.getElementById(id);
  const state = { threadId: null, cwd: '', model: 'gpt-6-astra', effort: 'high', permissionMode: 'ask', voiceActive: false, activeTurnId: null };
  const messages = new Map();
  const approvals = new Map();
  const toolItems = new Map();
  const transcripts = new Map();
  let transcriptSequence = 0;
  let peer = null;
  let microphone = null;
  let channel = null;
  let voicePhase = 'idle';
  let voiceGeneration = 0;
  let muted = false;
  let sessionBusy = false;
  let textBusy = false;
  let voiceStartPending = false;
  let voiceStopPending = false;
  let spokenReplyId = null;
  let sessionRequest = null;
  let micMeter = null;
  let preparedAudioContext = null;
  let audioUnlockUrl = null;
  let wakeLock = null;
  let wakeLockPending = false;
  let disconnectTimer = null;
  let eventSource = null;
  let eventAbort = null;
  let eventCursor = 0;
  let eventGeneration = 0;
  let pageHidden = false;
  let authExpired = false;
  const memoryQueue = new Map();
  const recordedMemoryItems = new Set();
  const memoryClientId = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  let memoryFlush = null;
  let memoryRetryTimer = null;
  let memoryFailures = 0;
  let memoryError = false;
  const permissionStorageKey = 'codex-voice.permission-mode';
  let permissionChoiceExplicit = false;
  try {
    const savedMode = localStorage.getItem(permissionStorageKey);
    if (savedMode === 'ask' || savedMode === 'yolo') {
      el('permission-mode').value = savedMode;
      permissionChoiceExplicit = true;
    }
  } catch { /* Storage may be disabled; the default remains ask. */ }

  function renderPermissionChoice() {
    const yoloSelected = el('permission-mode').value === 'yolo';
    el('permission-hint').hidden = !yoloSelected;
    el('permission-mode').classList.toggle('yolo-selected', yoloSelected);
  }
  el('permission-mode').addEventListener('change', () => {
    const choice = el('permission-mode').value;
    if (choice === 'ask' || choice === 'yolo') {
      permissionChoiceExplicit = true;
      try { localStorage.setItem(permissionStorageKey, choice); } catch { /* Keep this page's explicit choice. */ }
    }
    renderPermissionChoice();
  });
  renderPermissionChoice();

  async function api(path, body, { signal, timeout = 65000 } = {}) {
    const options = { headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) { options.method = 'POST'; options.body = JSON.stringify(body); }
    const abort = new AbortController();
    options.signal = abort.signal;
    const cancel = () => abort.abort();
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) abort.abort();
    const timer = setTimeout(cancel, timeout);
    let response;
    try { response = await fetch(path, options); }
    catch (error) { if (error.name === 'AbortError' && !signal?.aborted) throw new Error('Die Anfrage dauert zu lange. Bitte starte die Verbindung erneut.'); throw error; }
    finally { clearTimeout(timer); signal?.removeEventListener('abort', cancel); }
    let result;
    try { result = await response.json(); } catch { result = {}; }
    if (!response.ok) {
      if (response.status === 401) {
        expireAuthentication();
        throw new Error('Deine Anmeldung ist abgelaufen. Melde dieses Gerät erneut an.');
      }
      const detail = typeof result.error === 'string' ? result.error : result.error?.message;
      throw new Error(detail || `Die Anfrage ist fehlgeschlagen (HTTP ${response.status}).`);
    }
    return result;
  }

  function notice(text) { el('notice-text').textContent = text; el('notice').hidden = false; }
  function clearNotice() { el('notice').hidden = true; }
  el('dismiss-notice').addEventListener('click', clearNotice);
  el('login-again').addEventListener('click', () => window.location.reload());

  function expireAuthentication() {
    authExpired = true;
    clearTimeout(memoryRetryTimer);
    memoryRetryTimer = null;
    stopEvents();
    voiceGeneration += 1;
    releaseAudio();
    setVoicePhase('error', 'Bitte melde dieses Gerät erneut an.');
    setEventConnection(false);
    notice('Deine Anmeldung ist abgelaufen. Melde dieses Gerät erneut an.');
    el('login-again').hidden = false;
    renderMemoryStatus();
  }

  function applyState(update) {
    if (!update || typeof update !== 'object') return;
    const oldThread = state.threadId;
    Object.assign(state, update);
    if (!permissionChoiceExplicit && ['ask', 'yolo'].includes(state.permissionMode)) {
      el('permission-mode').value = state.permissionMode;
      renderPermissionChoice();
    }
    if (state.threadId && state.threadId !== oldThread) {
      messages.clear();
      el('conversation').replaceChildren();
      for (const card of approvals.values()) card.remove();
      approvals.clear();
      spokenReplyId = null;
      transcripts.clear();
      toolItems.clear();
    }
    if (state.cwd && document.activeElement !== el('cwd')) el('cwd').value = state.cwd;
    if (state.effort && document.activeElement !== el('effort')) el('effort').value = state.effort;
    el('thread-label').textContent = state.threadId ? state.threadId.slice(0, 8) : 'Noch nicht gestartet';
    el('thread-label').title = state.threadId || '';
    el('model-label').textContent = `${state.model || 'gpt-6-astra'} · ${state.effort || 'high'}`;
    renderControls();
    renderMemoryStatus();
  }

  function renderMemoryStatus() {
    const status = el('memory-status');
    status.hidden = !state.memoryEnabled && !memoryQueue.size;
    const failed = memoryError || state.memoryStatus === 'error';
    status.classList.toggle('memory-error', failed);
    status.textContent = authExpired && memoryQueue.size ? 'Anmeldung abgelaufen. Noch nicht gespeicherte Gesprächsteile bleiben nur in diesem Tab.'
      : failed ? `Gesprächsprotokoll konnte nicht gespeichert werden.${memoryQueue.size ? ' Ausstehende Übertragungen werden erneut versucht.' : ''}`
      : memoryQueue.size || state.memoryStatus === 'saving' ? 'Gespräch wird gespeichert …'
      : state.memoryStatus === 'saved' ? 'Gesprächsprotokoll gespeichert.'
      : 'Gesprächsprotokoll aktiv.';
  }

  function captureTranscript(role, text, itemId, threadId = state.threadId) {
    // Production records final SSE transcripts on the Mac; never duplicate them here.
    if (!state.memoryEnabled || state.memoryServerTranscripts || !threadId || !['user', 'assistant'].includes(role) || typeof text !== 'string' || !text.trim()) return;
    const stableId = itemId || `${memoryClientId}-${++transcriptSequence}`;
    const key = `${threadId}:${role}:${stableId}`;
    if (recordedMemoryItems.has(key) || memoryQueue.has(key)) return;
    memoryQueue.set(key, { role, text, itemId: String(stableId), threadId });
    renderMemoryStatus();
    void drainMemory();
  }

  function scheduleMemoryRetry() {
    if (memoryRetryTimer || authExpired || pageHidden || !memoryQueue.size) return;
    memoryRetryTimer = setTimeout(() => {
      memoryRetryTimer = null;
      void drainMemory();
    }, Math.min(15000, 500 * 2 ** Math.min(memoryFailures, 5)));
  }

  function drainMemory() {
    if (memoryFlush) return memoryFlush;
    if (authExpired || pageHidden || !memoryQueue.size) return Promise.resolve();
    clearTimeout(memoryRetryTimer);
    memoryRetryTimer = null;
    memoryFlush = (async () => {
      for (const [key, item] of memoryQueue) {
        if (authExpired || pageHidden) return;
        try {
          await api('/api/memory/transcript', item, { timeout: 8000 });
          memoryQueue.delete(key);
          recordedMemoryItems.add(key);
          memoryFailures = 0;
          memoryError = false;
        } catch {
          memoryFailures += 1;
          memoryError = true;
          return;
        }
      }
    })().finally(() => {
      memoryFlush = null;
      renderMemoryStatus();
      scheduleMemoryRetry();
    });
    return memoryFlush;
  }

  async function flushMemoryBeforeTransition() {
    if (!memoryQueue.size || authExpired) return;
    let timer;
    await Promise.race([drainMemory(), new Promise((resolve) => { timer = setTimeout(resolve, 2500); })]);
    clearTimeout(timer);
    // A slow request keeps its original threadId in memory and retries after navigation within this page.
  }

  function renderControls() {
    const inVoice = ['connecting', 'connected', 'muted', 'background', 'interrupted'].includes(voicePhase);
    el('start-voice').hidden = inVoice;
    el('start-voice').disabled = authExpired || sessionBusy || voiceStartPending || voiceStopPending || (state.voiceActive && !peer);
    el('live-controls').hidden = !inVoice;
    el('mute-voice').disabled = !['connected', 'muted'].includes(voicePhase);
    el('mute-voice').textContent = muted ? 'Mikro an' : 'Mikro aus';
    el('mute-voice').setAttribute('aria-pressed', String(muted));
    el('new-session').disabled = authExpired || sessionBusy || inVoice || voiceStartPending || voiceStopPending || Boolean(state.activeTurnId);
    el('cwd').disabled = el('new-session').disabled;
    el('effort').disabled = el('new-session').disabled;
    el('permission-mode').disabled = el('new-session').disabled;
    el('yolo-active').hidden = !(state.threadId && state.permissionMode === 'yolo');
    el('send-text').disabled = authExpired || textBusy || sessionBusy;
    el('interrupt').disabled = authExpired || !state.activeTurnId;
    el('work-status').textContent = approvals.size ? 'Freigabe nötig' : state.activeTurnId ? 'Astra arbeitet' : 'Bereit';
    el('work-dot').classList.toggle('working', Boolean(state.activeTurnId));
  }

  function setVoicePhase(phase, detail) {
    voicePhase = phase;
    el('voice-control').dataset.phase = phase;
    const labels = {
      idle: ['Bereit, wenn du es bist.', 'Das Mikrofon ist aus.'],
      connecting: ['Gespräch wird verbunden …', 'Die Sprachverbindung startet.'],
      connected: ['Ich höre zu.', 'Du kannst einfach lossprechen.'],
      muted: ['Mikrofon pausiert.', 'Du hörst weiterhin Astra.'],
      background: ['Gespräch im Hintergrund.', 'Der Browser kann Mikrofon und Ton im Hintergrund pausieren.'],
      interrupted: ['Mikrofon unterbrochen.', 'Der Browser hat die Audioaufnahme pausiert.'],
      error: ['Verbindung unterbrochen.', 'Du kannst das Gespräch erneut starten.']
    };
    const [title, subtitle] = labels[phase] || labels.idle;
    el('voice-status').textContent = title;
    el('voice-detail').textContent = detail || subtitle;
    void updateWakeLock();
    renderControls();
  }

  async function updateWakeLock() {
    const wanted = peer && ['connected', 'muted'].includes(voicePhase) && document.visibilityState === 'visible' && !pageHidden;
    if (!wanted) {
      const previous = wakeLock;
      wakeLock = null;
      if (previous) void previous.release().catch(() => {});
      el('wake-status').hidden = true;
      return;
    }
    if (wakeLock || wakeLockPending || !navigator.wakeLock?.request) return;
    wakeLockPending = true;
    try {
      const lock = await navigator.wakeLock.request('screen');
      if (!peer || !['connected', 'muted'].includes(voicePhase) || document.visibilityState !== 'visible' || pageHidden) {
        await lock.release();
        return;
      }
      wakeLock = lock;
      el('wake-status').hidden = false;
      lock.addEventListener('release', () => {
        if (wakeLock === lock) { wakeLock = null; el('wake-status').hidden = true; }
      });
    } catch { /* Screen wake locks are optional and can be denied in low-power mode. */ }
    finally { wakeLockPending = false; }
  }

  function prepareAudioGesture() {
    // Run before any await: Safari requires the first play/resume in the tap handler.
    const audio = el('remote-audio');
    if (!audio.srcObject) {
      if (audioUnlockUrl) URL.revokeObjectURL(audioUnlockUrl);
      const silence = Uint8Array.from(atob('UklGRiYAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQIAAAAAAA=='), (byte) => byte.charCodeAt(0));
      audioUnlockUrl = URL.createObjectURL(new Blob([silence], { type: 'audio/wav' }));
      audio.src = audioUnlockUrl;
      void audio.play().catch(() => {});
    }
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!preparedAudioContext && AudioContextClass && !reducedMotion.matches) {
      try {
        preparedAudioContext = new AudioContextClass();
        void preparedAudioContext.resume().catch(() => {});
      } catch { /* The microphone and remote audio do not depend on the meter. */ }
    }
  }

  function scrollIfNearBottom(wasNear) {
    if (wasNear) el('conversation').scrollTop = el('conversation').scrollHeight;
  }

  function message(id, kind, label, text, append = false) {
    const pane = el('conversation');
    const wasNear = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 110;
    el('empty-state')?.remove();
    let entry = messages.get(id);
    if (!entry) {
      const article = document.createElement('article');
      article.className = `message ${kind}`;
      const heading = document.createElement('div');
      heading.className = 'message-label';
      heading.textContent = label;
      const time = document.createElement('time');
      time.dateTime = new Date().toISOString();
      time.textContent = new Intl.DateTimeFormat('de', { hour: '2-digit', minute: '2-digit' }).format(new Date());
      heading.append(time);
      const content = document.createElement('div');
      content.className = 'message-content';
      article.append(heading, content);
      pane.append(article);
      entry = { article, content };
      messages.set(id, entry);
    }
    entry.content.textContent = append ? entry.content.textContent + text : text;
    entry.article.classList.toggle('streaming', append);
    scrollIfNearBottom(wasNear);
    return entry;
  }

  function finishMessage(id) { messages.get(id)?.article.classList.remove('streaming'); }

  async function createSession() {
    if (sessionRequest) return sessionRequest;
    const cwd = el('cwd').value.trim();
    if (!cwd) { el('cwd').focus(); throw new Error('Bitte gib einen Projektordner an.'); }
    sessionBusy = true;
    renderControls();
    const request = { cwd, effort: el('effort').value, permissionMode: el('permission-mode').value };
    sessionRequest = (async () => {
      await flushMemoryBeforeTransition();
      const result = await api('/api/session', request);
      applyState(result.state || result);
      return state;
    })()
      .finally(() => { sessionBusy = false; sessionRequest = null; renderControls(); });
    return sessionRequest;
  }

  el('session-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    clearNotice();
    try { await createSession(); message(`session-${Date.now()}`, 'system', '', 'Neue Codex-Sitzung ist bereit.'); }
    catch (error) { notice(error.message); }
  });

  function stopMicMeter() {
    const meter = micMeter;
    micMeter = null;
    if (meter) {
      cancelAnimationFrame(meter.frame);
      try { meter.source.disconnect(); meter.analyser.disconnect(); } catch {}
      void meter.context.close().catch(() => {});
    }
    const control = el('voice-control');
    control.style.setProperty('--mic-level', '0');
    control.style.setProperty('--mic-inner-scale', '1');
    control.style.setProperty('--mic-outer-scale', '1');
    control.dataset.meter = 'idle';
  }

  function startMicMeter(stream) {
    const prepared = preparedAudioContext;
    preparedAudioContext = null;
    stopMicMeter();
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      if (prepared) void prepared.close().catch(() => {});
      return;
    }
    let context;
    try {
      context = prepared || new AudioContextClass();
      const source = context.createMediaStreamSource(stream);
      const analyser = context.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      // Analysis only: neither node is connected to context.destination.
      const meter = { context, source, analyser, samples: new Float32Array(analyser.fftSize), level: 0, frame: 0 };
      micMeter = meter;
      el('voice-control').dataset.meter = 'active';
      const sample = () => {
        if (micMeter !== meter) return;
        analyser.getFloatTimeDomainData(meter.samples);
        let sum = 0;
        for (const value of meter.samples) sum += value * value;
        const rms = Math.sqrt(sum / meter.samples.length);
        const target = Math.min(1, Math.max(0, rms - 0.006) * 7);
        meter.level += (target - meter.level) * (target > meter.level ? 0.42 : 0.12);
        const control = el('voice-control');
        control.style.setProperty('--mic-level', meter.level.toFixed(3));
        control.style.setProperty('--mic-inner-scale', (1 + meter.level * 0.12).toFixed(3));
        control.style.setProperty('--mic-outer-scale', (1 + meter.level * 0.22).toFixed(3));
        meter.frame = requestAnimationFrame(sample);
      };
      void context.resume().catch(() => { if (micMeter === meter) stopMicMeter(); });
      meter.frame = requestAnimationFrame(sample);
    } catch {
      if (context) void context.close().catch(() => {});
      // A visualization failure must not interrupt the voice connection.
      stopMicMeter();
    }
  }

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  reducedMotion.addEventListener('change', () => {
    if (reducedMotion.matches) stopMicMeter();
    else if (microphone && !muted) startMicMeter(microphone);
  });

  function releaseAudio() {
    clearTimeout(disconnectTimer);
    disconnectTimer = null;
    if (preparedAudioContext) { void preparedAudioContext.close().catch(() => {}); preparedAudioContext = null; }
    stopMicMeter();
    if (channel) { try { channel.close(); } catch {} channel = null; }
    if (peer) { peer.onconnectionstatechange = null; peer.ontrack = null; peer.close(); peer = null; }
    if (microphone) { microphone.getTracks().forEach((track) => { track.onmute = track.onunmute = track.onended = null; track.stop(); }); microphone = null; }
    const audio = el('remote-audio');
    audio.pause();
    audio.srcObject = null;
    audio.removeAttribute('src');
    if (audioUnlockUrl) { URL.revokeObjectURL(audioUnlockUrl); audioUnlockUrl = null; }
    el('enable-audio').hidden = true;
    muted = false;
    void updateWakeLock();
  }

  async function stopVoice({ reportError = true, phase = 'idle' } = {}) {
    voiceGeneration += 1;
    voiceStopPending = true;
    releaseAudio();
    setVoicePhase(phase);
    try { await api('/api/voice/stop', {}); state.voiceActive = false; }
    catch (error) { if (reportError) notice(`Mikrofon ist aus. Die serverseitige Sprachsitzung konnte nicht bestätigt beendet werden: ${error.message}`); }
    finally { await flushMemoryBeforeTransition(); voiceStopPending = false; renderControls(); }
  }

  function realtimeEvent(event) {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    const type = data.type || '';
    if (type === 'conversation.item.input_audio_transcription.completed') {
      captureTranscript('user', data.transcript, data.item_id || data.item?.id);
    } else if (['response.output_audio_transcript.done', 'response.audio_transcript.done'].includes(type)) {
      captureTranscript('assistant', data.transcript || data.text, data.item_id || data.item?.id || data.response_id);
    } else if (type === 'response.done') {
      const output = Array.isArray(data.response?.output) ? data.response.output : [];
      for (const [index, item] of output.entries()) {
        if (item.role && item.role !== 'assistant') continue;
        const content = Array.isArray(item.content) ? item.content : [];
        const audioText = content.filter((part) => typeof part.transcript === 'string').map((part) => part.transcript).join('\n');
        const text = audioText || content.filter((part) => typeof part.text === 'string').map((part) => part.text).join('\n');
        const responseId = data.response?.id || data.event_id;
        captureTranscript('assistant', text, item.id || (responseId ? `${responseId}-${index}` : undefined));
      }
    }
    if (type === 'conversation.item.input_audio_transcription.completed' && data.transcript) {
      message(`voice-user-${data.item_id || Date.now()}`, 'user', 'Du · Sprache', data.transcript);
    } else if (['response.output_audio_transcript.delta', 'response.audio_transcript.delta', 'response.output_text.delta', 'response.text.delta'].includes(type)) {
      const id = `voice-assistant-${data.response_id || data.item_id || spokenReplyId || Date.now()}`;
      spokenReplyId = id.replace('voice-assistant-', '');
      message(id, 'assistant', 'Sprachantwort', data.delta || '', true);
    } else if (type === 'response.done' || type.endsWith('_transcript.done') || type.endsWith('.text.done')) {
      if (spokenReplyId) finishMessage(`voice-assistant-${spokenReplyId}`);
      spokenReplyId = null;
    } else if (type === 'input_audio_buffer.speech_started') {
      if (!muted) el('voice-detail').textContent = 'Du sprichst …';
    } else if (type === 'input_audio_buffer.speech_stopped') {
      if (!muted) el('voice-detail').textContent = 'Ich höre zu. Du kannst jederzeit eingreifen.';
    } else if (type === 'error') {
      // A cancel without an active voice response is harmless.
      if (data.error?.code !== 'response_cancel_not_active') notice(data.error?.message || 'Die Sprachverbindung hat einen Fehler gemeldet.');
    }
  }

  async function startVoice() {
    if (authExpired || voiceStartPending || voiceStopPending || peer) return;
    clearNotice();
    voiceStartPending = true;
    const generation = ++voiceGeneration;
    setVoicePhase('connecting');
    try {
      if (!window.isSecureContext) throw new Error('Das Mikrofon braucht eine sichere Verbindung. Öffne auf dem iPhone den HTTPS-Link zu deinem Mac in Safari. Eine lokale HTTP-IP-Adresse reicht dafür nicht.');
      if (!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) throw new Error('Dieser Browser unterstützt keine Sprachverbindung. Öffne diese Seite direkt in einem aktuellen Safari oder Chrome.');
      prepareAudioGesture();
      if (!state.threadId) await createSession();
      if (generation !== voiceGeneration) return;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (generation !== voiceGeneration) { stream.getTracks().forEach((track) => track.stop()); return; }
      microphone = stream;
      startMicMeter(stream);
      stream.getAudioTracks().forEach((track) => {
        track.onmute = () => {
          if (generation !== voiceGeneration || muted) return;
          stopMicMeter();
          if (document.visibilityState !== 'visible') setVoicePhase('interrupted');
          else {
            notice('Der Browser hat das Mikrofon unterbrochen. Starte das Gespräch erneut, sobald es wieder verfügbar ist.');
            void stopVoice({ reportError: false, phase: 'error' });
          }
        };
        track.onunmute = () => {
          if (generation !== voiceGeneration || muted || document.visibilityState !== 'visible') return;
          startMicMeter(stream);
          if (peer?.connectionState === 'connected') setVoicePhase('connected');
        };
        track.onended = () => {
          if (generation !== voiceGeneration) return;
          notice('Der Mikrofonzugriff wurde beendet. Starte das Gespräch erneut.');
          void stopVoice({ reportError: false, phase: 'error' });
        };
      });
      const pc = new RTCPeerConnection();
      peer = pc;
      pc.ontrack = (event) => {
        const audio = el('remote-audio');
        audio.removeAttribute('src');
        if (audioUnlockUrl) { URL.revokeObjectURL(audioUnlockUrl); audioUnlockUrl = null; }
        audio.srcObject = event.streams[0] || new MediaStream([event.track]);
        audio.play().catch(() => { if (peer === pc) el('enable-audio').hidden = false; });
      };
      pc.onconnectionstatechange = () => {
        if (generation !== voiceGeneration) return;
        if (pc.connectionState === 'connected') {
          clearTimeout(disconnectTimer);
          disconnectTimer = null;
          setVoicePhase(document.visibilityState !== 'visible' ? 'background' : muted ? 'muted' : 'connected');
        } else if (pc.connectionState === 'disconnected') {
          setVoicePhase('connecting', 'Verbindung wird wiederhergestellt …');
          if (!disconnectTimer) disconnectTimer = setTimeout(() => {
            if (generation !== voiceGeneration || peer !== pc || pc.connectionState === 'connected') return;
            notice('Die Sprachverbindung ist unterbrochen. Starte das Gespräch erneut.');
            void stopVoice({ reportError: false, phase: 'error' });
          }, 12000);
        }
        else if (pc.connectionState === 'failed') {
          notice('Die Sprachverbindung ist abgebrochen. Dein Mikrofon wurde ausgeschaltet.');
          void stopVoice({ reportError: false, phase: 'error' });
        }
      };
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));
      channel = pc.createDataChannel('oai-events');
      channel.addEventListener('message', realtimeEvent);
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      if (generation !== voiceGeneration) return;
      const answer = await api('/api/voice/start', { sdp: pc.localDescription.sdp });
      if (generation !== voiceGeneration) { await api('/api/voice/stop', {}).catch(() => {}); return; }
      if (!answer.sdp) throw new Error('Die Sprachschnittstelle hat keine Verbindungsantwort geliefert.');
      if (answer.threadId) applyState({ threadId: answer.threadId });
      await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp });
      state.voiceActive = true;
    } catch (error) {
      if (generation !== voiceGeneration) return;
      const friendly = error.name === 'NotAllowedError' ? 'Mikrofonzugriff wurde nicht erlaubt. Erlaube das Mikrofon in den Website-Einstellungen für diese Seite und starte erneut.'
        : error.name === 'NotFoundError' ? 'Kein Mikrofon gefunden. Verbinde ein Mikrofon und starte erneut.'
        : error.name === 'NotReadableError' ? 'Das Mikrofon ist gerade nicht verfügbar. Prüfe die Browser- und Systemeinstellungen.' : error.message;
      await stopVoice({ reportError: false, phase: 'error' });
      notice(friendly);
    } finally { voiceStartPending = false; renderControls(); }
  }

  el('start-voice').addEventListener('click', startVoice);
  el('stop-voice').addEventListener('click', () => { void stopVoice(); });
  el('mute-voice').addEventListener('click', () => {
    if (!microphone) return;
    muted = !muted;
    microphone.getAudioTracks().forEach((track) => { track.enabled = !muted; });
    if (muted) stopMicMeter();
    else startMicMeter(microphone);
    setVoicePhase(muted ? 'muted' : 'connected');
  });
  el('enable-audio').addEventListener('click', () => {
    el('remote-audio').play().then(() => { el('enable-audio').hidden = true; }).catch(() => { notice('Der Browser konnte die Audioausgabe nicht starten. Prüfe die Tonfreigabe für diese Seite.'); });
  });
  el('remote-audio').addEventListener('pause', () => {
    if (peer && el('remote-audio').srcObject && document.visibilityState === 'visible') el('enable-audio').hidden = false;
  });
  el('interrupt').addEventListener('click', async () => {
    try { await api('/api/interrupt', {}); }
    catch (error) { notice(error.message); }
  });

  el('message').addEventListener('input', () => {
    el('message').style.height = 'auto';
    el('message').style.height = `${Math.min(el('message').scrollHeight, 140)}px`;
  });
  el('message').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); el('text-form').requestSubmit(); }
  });
  el('text-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = el('message').value.trim();
    if (!text || textBusy) return;
    textBusy = true;
    renderControls();
    clearNotice();
    try {
      if (!state.threadId) await createSession();
      await api('/api/text', { text });
      message(`text-user-${Date.now()}`, 'user', 'Du', text);
      if (el('message').value.trim() === text) { el('message').value = ''; el('message').style.height = 'auto'; }
    } catch (error) { notice(error.message); }
    finally { textBusy = false; renderControls(); }
  });

  function approvalRequest(request) {
    if (request.id === undefined || approvals.has(String(request.id))) return;
    const id = String(request.id);
    const params = request.params || {};
    const card = document.createElement('article');
    card.className = 'approval-card';
    const title = document.createElement('h3');
    title.textContent = 'Astra braucht deine Freigabe';
    card.append(title);
    if (params.reason) { const reason = document.createElement('p'); reason.textContent = params.reason; card.append(reason); }
    const command = params.command;
    const isFileChange = (request.method || '').includes('fileChange');
    let detailsAvailable = Boolean(command);
    if (command) {
      const code = document.createElement('pre');
      code.textContent = Array.isArray(command) ? command.map((part) => JSON.stringify(part)).join(' ') : String(command);
      card.append(code);
    } else if (isFileChange) {
      const changes = params.changes || toolItems.get(params.itemId)?.changes;
      const entries = Array.isArray(changes) ? changes : Object.entries(changes || {}).map(([path, change]) => ({ path, ...change }));
      const code = document.createElement('pre');
      detailsAvailable = entries.length > 0 && entries.every((change) => typeof change.diff === 'string' || typeof change.content === 'string' || change.kind === 'delete' || change.type === 'delete' || change.kind?.type === 'delete');
      code.textContent = entries.map((change) => `${change.path || 'Datei'}\n${change.diff ?? change.content ?? ((change.kind === 'delete' || change.type === 'delete' || change.kind?.type === 'delete') ? 'Datei wird gelöscht.' : 'Änderungsdetails fehlen.')}`).join('\n\n') || 'Änderungsdetails fehlen. Bitte prüfe diese Freigabe in der Codex-Terminaloberfläche.';
      card.append(code);
    } else {
      const description = document.createElement('p');
      description.textContent = `Anfrage: ${request.method || 'Aktion freigeben'}`;
      card.append(description);
    }
    if (params.cwd) { const cwd = document.createElement('p'); cwd.textContent = `Ordner: ${params.cwd}`; card.append(cwd); }
    const buttons = document.createElement('div');
    buttons.className = 'approval-buttons';
    const available = params.availableDecisions;
    const permitted = Array.isArray(available) ? available.filter((decision) => typeof decision === 'string') : ['accept', 'acceptForSession', 'decline'];
    const proposal = params.proposedExecpolicyAmendment;
    const hasPrefix = Array.isArray(proposal) && proposal.length > 0 && proposal.every((part) => typeof part === 'string' && part.length > 0);
    const offeredPrefix = !Array.isArray(available) || available.some((decision) => decision && typeof decision === 'object' && JSON.stringify(decision.acceptWithExecpolicyAmendment?.execpolicy_amendment) === JSON.stringify(proposal));
    if (!isFileChange && hasPrefix && offeredPrefix) {
      permitted.push('always');
      const scope = document.createElement('div');
      scope.className = 'approval-scope';
      const label = document.createElement('p');
      label.textContent = 'Dauerhaft erlauben gilt für Befehle mit diesem Präfix:';
      const prefix = document.createElement('pre');
      prefix.textContent = proposal.map((part) => JSON.stringify(part)).join(' ');
      scope.append(label, prefix);
      card.append(scope);
    }
    if (permitted.includes('acceptForSession')) {
      const scope = document.createElement('p');
      scope.className = 'approval-session-scope';
      scope.textContent = 'Sitzungsfreigaben gelten bis zum Ende dieser Codex-Sitzung.';
      card.append(scope);
    }
    for (const [decision, label, className] of [['decline', 'Ablehnen', 'secondary'], ['cancel', 'Abbrechen', 'secondary'], ['acceptForSession', 'Für diese Sitzung', 'secondary'], ['always', 'Dauerhaft erlauben', 'secondary'], ['accept', 'Einmal erlauben', 'primary']]) {
      if (!permitted.includes(decision)) continue;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `button ${className}`;
      button.textContent = label;
      const requiresDetails = ['accept', 'acceptForSession', 'always'].includes(decision);
      if (requiresDetails && !detailsAvailable) button.disabled = true;
      button.addEventListener('click', async () => {
        buttons.querySelectorAll('button').forEach((item) => { item.disabled = true; });
        try {
          await api('/api/approval', { id: request.id, decision });
          card.remove(); approvals.delete(id); renderControls();
        } catch (error) { notice(error.message); buttons.querySelectorAll('button').forEach((item) => { item.disabled = ['accept', 'acceptForSession', 'always'].includes(item.dataset.decision) && !detailsAvailable; }); }
      });
      button.dataset.decision = decision;
      buttons.append(button);
    }
    card.append(buttons);
    approvals.set(id, card);
    el('approvals').append(card);
    renderControls();
  }

  function serverEvent(payload) {
    const { method, params = {} } = payload;
    if (method === 'state') { applyState(params.state || params); return; }
    if (params.threadId && state.threadId && params.threadId !== state.threadId) return;
    if (method === 'approval/request') approvalRequest(params);
    else if (method === 'serverRequest/resolved') {
      const id = String(params.requestId);
      approvals.get(id)?.remove(); approvals.delete(id); renderControls();
    }
    else if (method === 'thread/realtime/transcript/delta' || method === 'thread/realtime/transcript/done') {
      const role = params.role === 'user' ? 'user' : 'assistant';
      if (!transcripts.has(role)) transcripts.set(role, `voice-${role}-${++transcriptSequence}`);
      const id = transcripts.get(role);
      const isDelta = method.endsWith('/delta');
      message(id, role, role === 'user' ? 'Du · Sprache' : 'Sprachantwort', isDelta ? (params.delta || '') : (params.text || ''), isDelta);
      if (!isDelta) {
        captureTranscript(role, params.text, params.itemId || params.item_id || `${memoryClientId}-${id}`, params.threadId || state.threadId);
        finishMessage(id); transcripts.delete(role);
      }
    }
    else if (method === 'turn/started') applyState({ activeTurnId: params.turn?.id || params.turnId || 'active' });
    else if (method === 'turn/completed') {
      applyState({ activeTurnId: null });
      for (const id of messages.keys()) finishMessage(id);
      if (params.turn?.error?.message) notice(params.turn.error.message);
    } else if (method === 'item/agentMessage/delta') {
      message(`astra-${params.itemId || params.turnId || 'active'}`, 'assistant', 'Astra', params.delta || '', true);
    } else if (method === 'item/started') {
      if (params.item?.id) toolItems.set(params.item.id, params.item);
    } else if (method === 'item/completed') {
      const item = params.item || {};
      if (item.id) toolItems.set(item.id, item);
      if (item.type === 'agentMessage' && item.text) message(`astra-${item.id || params.itemId || params.turnId || 'active'}`, 'assistant', 'Astra', item.text);
      finishMessage(`astra-${item.id || params.itemId || params.turnId || 'active'}`);
    } else if (method === 'thread/realtime/error') {
      notice(params.message || params.error?.message || 'Die Sprachsitzung hat einen Fehler gemeldet.');
      void stopVoice({ reportError: false, phase: 'error' });
    } else if (method === 'thread/realtime/stopped' || method === 'thread/realtime/closed') {
      state.voiceActive = false;
      if (peer) { voiceGeneration += 1; releaseAudio(); setVoicePhase('idle'); }
    } else if (method === 'client/notice') notice(params.message || 'Bitte prüfe den lokalen Codex-Dienst.');
    else if (method === 'error') notice(params.error?.message || params.message || 'Codex hat einen Fehler gemeldet.');
  }

  function setEventConnection(connected) {
    const status = el('connection-status');
    status.replaceChildren();
    const dot = document.createElement('span');
    dot.className = `connection-dot${connected ? ' connected' : ''}`;
    const label = authExpired ? 'Anmeldung abgelaufen' : connected
      ? (state.clientTransport === 'poll' ? 'Mit deinem Mac verbunden' : 'Lokal verbunden')
      : navigator.onLine === false ? 'Keine Internetverbindung' : 'Verbindung wird wiederhergestellt …';
    status.append(dot, document.createTextNode(label));
  }

  function stopEvents() {
    eventGeneration += 1;
    eventSource?.close();
    eventSource = null;
    eventAbort?.abort();
    eventAbort = null;
  }

  function retryDelay(milliseconds, signal) {
    return new Promise((resolve) => {
      const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve(); };
      const timer = setTimeout(finish, milliseconds);
      signal.addEventListener('abort', finish, { once: true });
      if (signal.aborted) finish();
    });
  }

  async function pollEvents(generation, signal) {
    let failures = 0;
    while (!signal.aborted && generation === eventGeneration && !authExpired && !pageHidden) {
      try {
        const result = await api(`/api/events/poll?after=${eventCursor}&wait=20`, undefined, { signal, timeout: 30000 });
        if (signal.aborted || generation !== eventGeneration) return;
        const events = Array.isArray(result.events) ? result.events : [];
        for (const event of events) {
          if (!Number.isSafeInteger(event.id) || event.id <= eventCursor) continue;
          try { serverEvent(event); } catch { /* One unsupported event must not break following updates. */ }
          eventCursor = event.id;
        }
        if (result.state) applyState(result.state);
        if (Number.isSafeInteger(result.cursor)) eventCursor = Math.max(eventCursor, result.cursor);
        failures = 0;
        setEventConnection(true);
        // Keep immediate mock/proxy responses from becoming a busy loop.
        await retryDelay(100, signal);
      } catch {
        if (signal.aborted || generation !== eventGeneration || authExpired) return;
        setEventConnection(false);
        failures += 1;
        await retryDelay(Math.min(5000, 500 * 2 ** Math.min(failures - 1, 4)), signal);
      }
    }
  }

  function startEvents() {
    stopEvents();
    if (authExpired || pageHidden) return;
    const generation = eventGeneration;
    if (state.clientTransport === 'poll') {
      eventAbort = new AbortController();
      void pollEvents(generation, eventAbort.signal);
      return;
    }
    const events = new EventSource('/api/events');
    eventSource = events;
    events.onopen = () => {
      if (generation !== eventGeneration) return;
      setEventConnection(true);
      void api('/api/state').then(applyState).catch(() => {});
    };
    events.onerror = () => {
      if (generation !== eventGeneration) return;
      setEventConnection(false);
      // EventSource hides HTTP status; the state request can detect expired pairing.
      void api('/api/state').then(applyState).catch(() => {});
    };
    events.onmessage = (event) => {
      if (generation !== eventGeneration) return;
      try { serverEvent(JSON.parse(event.data)); } catch { /* Ignore malformed or unsupported notifications. */ }
    };
  }

  window.addEventListener('pagehide', () => {
    // A second tab must not stop a voice connection that it does not own.
    const wasActive = Boolean(peer || microphone || voiceStartPending);
    pageHidden = true;
    clearTimeout(memoryRetryTimer);
    memoryRetryTimer = null;
    for (const item of memoryQueue.values()) navigator.sendBeacon('/api/memory/transcript', new Blob([JSON.stringify(item)], { type: 'application/json' }));
    voiceGeneration += 1;
    releaseAudio();
    stopEvents();
    setVoicePhase(wasActive ? 'error' : 'idle', wasActive ? 'Nach der Rückkehr kannst du das Gespräch erneut starten.' : undefined);
    if (wasActive) navigator.sendBeacon('/api/voice/stop', new Blob(['{}'], { type: 'application/json' }));
  });

  window.addEventListener('pageshow', (event) => {
    if (!event.persisted) return;
    pageHidden = false;
    void init();
  });

  document.addEventListener('visibilitychange', () => {
    void updateWakeLock();
    if (!peer) return;
    if (document.visibilityState !== 'visible') {
      stopMicMeter();
      setVoicePhase('background');
      return;
    }
    const tracks = microphone?.getAudioTracks() || [];
    if (!tracks.length || tracks.some((track) => track.readyState === 'ended' || (!muted && track.muted))) {
      notice('Das Mikrofon wurde im Hintergrund pausiert. Starte das Gespräch erneut.');
      void stopVoice({ reportError: false, phase: 'error' });
      return;
    }
    if (peer.connectionState === 'connected') {
      if (!muted) startMicMeter(microphone);
      setVoicePhase(muted ? 'muted' : 'connected');
      const audio = el('remote-audio');
      if (audio.srcObject) void audio.play().catch(() => { if (peer) el('enable-audio').hidden = false; });
    } else if (['failed', 'closed'].includes(peer.connectionState)) {
      void stopVoice({ reportError: false, phase: 'error' });
    } else setVoicePhase('connecting', 'Verbindung wird nach der Rückkehr wiederhergestellt …');
  });

  window.addEventListener('offline', () => setEventConnection(false));
  window.addEventListener('online', () => { if (!authExpired && !pageHidden) { startEvents(); void drainMemory(); } });

  async function init() {
    try { applyState(await api('/api/state')); startEvents(); void drainMemory(); }
    catch (error) {
      if (!authExpired) {
        notice(`Der Codex-Dienst antwortet nicht: ${error.message}`);
        setEventConnection(false);
        setTimeout(() => { if (!pageHidden && !authExpired) void init(); }, 2000);
      }
    }
    renderControls();
  }

  void init();
})();
