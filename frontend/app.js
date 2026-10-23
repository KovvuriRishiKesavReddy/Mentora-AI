// app.js — MentoraAI v3
// Persistence model: a random client id and the active session id live in localStorage. Every request sends
// the client id (X-Client-Id); the backend stores chats, quizzes, mastery and documents against it. A refresh
// re-opens the same session and reloads history/analytics from the backend instead of creating a new one.

const API = window.MENTORA_API || `${location.protocol === 'http:' || location.protocol === 'https:' ? location.protocol : 'http:'}//${location.hostname || 'localhost'}:8000`;
const MOOD_COLORS = { frustrated:'#d62839', confused:'#f08c00', confident:'#2f9e44', bored:'#868e96', neutral:'#1f8a8a' };
const LS_CLIENT = 'mentora.clientId', LS_SESSION = 'mentora.sessionId';

// ── State ─────────────────────────────────────────────────────────────────────
let clientId = null, sessionId = null;
let mediaRecorder = null, audioChunks = [], isRecording = false, recStream = null, recTimer = null, recognition = null;
let audioFeatures = null, questionStartTime = Date.now(), backspaceCount = 0, attemptNumber = 1;
let moodTimeline = [], currentQuiz = null, sttProvider = 'browser', maxAudioSeconds = 60;

function lsGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch {} }
function newClientId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
  const a = new Uint8Array(16); crypto.getRandomValues(a);
  return Array.from(a, b => b.toString(16).padStart(2, '0')).join('');
}

// ── API helper ────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = Object.assign({ 'X-Client-Id': clientId }, opts.headers || {});
  let res;
  try { res = await fetch(API + path, Object.assign({}, opts, { headers })); }
  catch { setOffline(true); const e = new Error('Cannot reach the backend. Is start.bat running?'); e.network = true; throw e; }
  setOffline(false);
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) { const e = new Error((data && data.detail) || `Request failed (${res.status})`); e.status = res.status; throw e; }
  return data;
}
function post(path, body) {
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}
function setOffline(off) { const b = document.getElementById('offline-banner'); if (b) b.style.display = off ? 'inline-block' : 'none'; }

// ── Init / sessions ───────────────────────────────────────────────────────────
async function init() {
  clientId = lsGet(LS_CLIENT) || newClientId(); lsSet(LS_CLIENT, clientId);
  try { const h = await (await fetch(`${API}/health`)).json(); sttProvider = h.stt_provider; maxAudioSeconds = h.max_audio_seconds || 60; setOffline(false); }
  catch { setOffline(true); showToast('⚠ Cannot reach backend. Run start.bat first.', 6000); }
  const saved = lsGet(LS_SESSION);
  let opened = false;
  if (saved) opened = await openSession(saved, true);
  if (!opened) await createSession(false);
  loadFiles(); loadSessions(); loadWeakTopics();
  questionStartTime = Date.now();
}

async function createSession(announce = true) {
  const data = await api('/sessions', { method: 'POST' });
  setActiveSession(data.session_id);
  resetChatView(announce ? '👋 New session started! Ask me anything about your notes.' : null);
  return data.session_id;
}

function setActiveSession(id) {
  sessionId = id; lsSet(LS_SESSION, id);
  document.getElementById('session-badge').textContent = `Session: ${id.slice(0, 8)}`;
}

function resetChatView(msg) {
  if (msg) document.getElementById('messages').innerHTML = `<div class="message assistant"><div class="bubble">${esc(msg)}</div></div>`;
  moodTimeline = []; renderMoodDots();
  document.getElementById('mood-orb').style.background = '#1f8a8a';
  document.getElementById('mood-label-top').textContent = 'Detecting mood…';
}

async function openSession(id, quiet = false) {
  try {
    const data = await api(`/sessions/${encodeURIComponent(id)}`);
    setActiveSession(id);
    const box = document.getElementById('messages');
    if (data.messages.length) {
      box.innerHTML = '';
      data.messages.forEach(m => m.role === 'user' ? appendUserMessage(m.content) : appendAssistantMessage(historyToView(m)));
    } else resetChatView('👋 Ask me anything about your notes.');
    moodTimeline = (data.mood_history && data.mood_history.timeline) || []; renderMoodDots();
    if (moodTimeline.length) updateMoodUI({ label: moodTimeline.at(-1), emoji: '', confidence: 0 }, true);
    loadSessions();
    return true;
  } catch (e) {
    if (e.status === 404) { try { localStorage.removeItem(LS_SESSION); } catch {} return false; }   // session gone: start fresh
    if (!quiet) showToast(`❌ ${e.message}`);
    if (e.network) { setActiveSession(id); return true; }     // backend down: keep the id so a later refresh can restore it
    return false;
  }
}

function historyToView(m) {
  const grounding = m.grounding || 'strong';
  return { response: m.content, tone_explanation: m.mood_label ? `Mood: ${m.mood_label}` : '',
           retrieval: { grounding, is_grounded: grounding !== 'none', sources: m.sources || [] } };
}

async function startNewChat() { try { await createSession(true); loadSessions(); showToast('✅ New chat started (your previous chat is saved)'); } catch (e) { showToast(`❌ ${e.message}`); } }

async function loadSessions() {
  const el = document.getElementById('sessions-list');
  try {
    const data = await api('/sessions');
    if (!data.sessions.length) { el.innerHTML = '<div style="font-size:12px;color:var(--text2)">No saved chats yet</div>'; return; }
    el.innerHTML = '';
    data.sessions.forEach(s => {
      const d = document.createElement('div'); d.className = 'session-item' + (s.id === sessionId ? ' active' : '');
      const t = document.createElement('span'); t.className = 's-title'; t.textContent = s.title; t.title = s.title;
      const x = document.createElement('button'); x.className = 's-del'; x.textContent = '🗑'; x.title = 'Delete this chat';
      d.append(t, x);
      d.addEventListener('click', () => { if (s.id !== sessionId) { switchTab('chat'); openSession(s.id); } });
      x.addEventListener('click', async ev => {
        ev.stopPropagation();
        if (!confirm('Delete this chat? Your quiz progress is kept.')) return;
        try { await api(`/sessions/${encodeURIComponent(s.id)}`, { method: 'DELETE' }); if (s.id === sessionId) await createSession(true); loadSessions(); }
        catch (e) { showToast(`❌ ${e.message}`); }
      });
      el.appendChild(d);
    });
  } catch { /* banner already shown */ }
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  ['chat','quiz','study','plan','analytics'].forEach(t => {
    document.getElementById(`${t}-panel`).style.display = t === tab ? 'flex' : 'none';
    document.getElementById(`tab-${t}`).classList.toggle('active', t === tab);
  });
  document.querySelector('.sidebar').classList.remove('open');
  if (tab === 'analytics') loadAnalytics();
  if (tab === 'study') loadStudyDocs();
  if (tab === 'plan') loadPlan();
}

// ── Files ─────────────────────────────────────────────────────────────────────
function handleDragOver(e) { e.preventDefault(); document.getElementById('upload-zone').classList.add('drag-over'); }
function handleDragLeave() { document.getElementById('upload-zone').classList.remove('drag-over'); }
function handleDrop(e) { e.preventDefault(); handleDragLeave(); if (e.dataTransfer.files[0]) uploadFile(e.dataTransfer.files[0]); }

async function uploadFile(file) {
  if (!file) return;
  const status = document.getElementById('upload-status'), bar = document.getElementById('upload-progress'), fill = document.getElementById('upload-progress-fill');
  status.textContent = 'Uploading…'; bar.style.display = 'block'; fill.style.width = '0%';
  const form = new FormData(); form.append('file', file);
  try {
    const data = await uploadWithProgress('/upload', form, f => {
      fill.style.width = `${Math.round(f * 100)}%`;
      status.innerHTML = f < 1 ? `Uploading… ${Math.round(f * 100)}%` : '<span class="spinner"></span> Reading the notes and detecting chapters…';
    });
    const msg = { added: `✅ ${data.chunks_created} chunks indexed`, replaced: `✅ Replaced with new version (${data.chunks_created} chunks)`,
                  unchanged: 'ℹ Already indexed — no change', duplicate: `ℹ Same content already indexed as "${data.filename}"` }[data.status];
    status.textContent = `${msg} · ${data.chapters_detected.length} sections found`; showToast(`${msg} — ${data.chapters_detected.length} sections`);
    loadFiles();
  } catch (e) { status.textContent = `❌ ${e.message}`; showToast(`❌ ${e.message}`); }
  bar.style.display = 'none';
  document.getElementById('file-input').value = '';
}

async function loadFiles() { try { const files = (await api('/files')).files || []; renderFiles(files); setStudyDocs(files); } catch {} }

function renderFiles(files) {
  const el = document.getElementById('files-list');
  if (!files.length) { el.innerHTML = '<div style="font-size:12px;color:var(--text2)">No files yet</div>'; return; }
  el.innerHTML = '';
  files.forEach(f => {
    const d = document.createElement('div'); d.className = 'file-item';
    const b = document.createElement('button'); b.className = 'file-delete-btn'; b.textContent = '🗑'; b.title = 'Remove file';
    b.addEventListener('click', () => deleteFile(f));
    const n = document.createElement('div'); n.className = 'file-name'; n.textContent = f.filename; n.title = f.filename;
    const m = document.createElement('div'); m.className = 'file-meta'; m.textContent = `${f.chunks} chunks`;
    const tags = document.createElement('div'); tags.className = 'chapter-tags';
    (f.chapters || []).slice(0, 3).forEach(c => { const s = document.createElement('span'); s.className = 'chapter-tag'; s.textContent = c.slice(0, 22); tags.appendChild(s); });
    if ((f.chapters || []).length > 3) { const s = document.createElement('span'); s.className = 'chapter-tag'; s.textContent = `+${f.chapters.length - 3}`; tags.appendChild(s); }
    const st = document.createElement('button'); st.className = 'file-study-btn'; st.textContent = '📚 Study tools';
    st.addEventListener('click', () => { switchTab('study'); document.getElementById('study-doc').value = f.doc_id; onStudyDocChange(); });
    d.append(b, n, m, tags, st); el.appendChild(d);
  });
}

async function deleteFile(f) {
  if (!confirm(`Remove "${f.filename}" from the index?`)) return;
  try { await api(`/files/${encodeURIComponent(f.doc_id)}`, { method: 'DELETE' }); showToast(`🗑 "${f.filename}" removed`); loadFiles(); }
  catch (e) { showToast(`❌ Could not remove: ${e.message}`); }
}

// ── Topics (mastery) ──────────────────────────────────────────────────────────
async function loadWeakTopics() {
  try { renderWeakTopics((await api('/gaps')).weak_topics || []); } catch {}
}

function statusLabel(t) { return t.reason === 'repeated confusion' ? 'asked for help repeatedly' : t.status === 'weak' ? 'weak' : 'needs revision'; }

function renderWeakTopics(topics) {
  const el = document.getElementById('weak-topics-list');
  if (!topics.length) { el.innerHTML = '<div style="font-size:12px;color:var(--text2)">Nothing to revise 🎉 (based on your quiz results)</div>'; return; }
  el.innerHTML = '';
  topics.forEach(t => {
    const d = document.createElement('div'); d.className = 'weak-topic-item' + (t.status === 'weak' ? '' : ' revise');
    const n = document.createElement('div'); n.className = 'weak-topic-name'; n.textContent = t.topic;
    const s = document.createElement('div'); s.className = 'weak-topic-score';
    s.textContent = t.attempts ? `${statusLabel(t)} · mastery ${(t.mastery * 100).toFixed(0)}% (${t.correct}/${t.attempts} correct)` : statusLabel(t);
    const b = document.createElement('button'); b.className = 'revision-btn'; b.textContent = '📚 Revise';
    b.addEventListener('click', () => startRevision(t.topic));
    d.append(n, s, b); el.appendChild(d);
  });
}

function startRevision(topic) {
  document.getElementById('quiz-topic').value = topic; switchTab('quiz');
  showToast(`📚 Generating revision quiz for "${topic}"`); setTimeout(generateQuiz, 300);
}

// ── Behavioural tracking ──────────────────────────────────────────────────────
function trackBackspace(e) { if (e.key === 'Backspace') backspaceCount++; }
function getLatencyMs() { return questionStartTime ? Date.now() - questionStartTime : 0; }
function resetSignals() { backspaceCount = 0; questionStartTime = Date.now(); }

// ── Voice ─────────────────────────────────────────────────────────────────────
// What gets recorded: pick a MIME type this browser really supports (Chrome/Edge: webm/opus, Firefox: ogg/opus,
// Safari: mp4). The recording is converted to 16 kHz mono WAV in the browser before upload, so the server needs no
// ffmpeg. Speech-to-text: "browser" = Web Speech API runs WHILE recording (Chrome/Edge only); "groq" = server transcribes.
function pickMime() {
  const c = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
  return (window.MediaRecorder && c.find(t => MediaRecorder.isTypeSupported(t))) || '';
}

async function toggleRecording() { isRecording ? stopRecording() : await startRecording(); }

async function startRecording() {
  if (!navigator.mediaDevices || !window.MediaRecorder) { showToast('❌ This browser cannot record audio. Type your question instead.'); return; }
  try { recStream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { showToast('❌ Microphone access denied'); return; }
  try {
    const mime = pickMime();
    audioChunks = [];
    mediaRecorder = new MediaRecorder(recStream, mime ? { mimeType: mime } : undefined);
    mediaRecorder.ondataavailable = e => { if (e.data.size > 0) audioChunks.push(e.data); };
    mediaRecorder.onstop = processAudio;
    mediaRecorder.start(250);
  } catch (e) { recStream.getTracks().forEach(t => t.stop()); showToast(`❌ Could not start recording: ${e.message}`); return; }
  isRecording = true;
  const btn = document.getElementById('mic-btn'); btn.classList.add('recording'); btn.textContent = '⏹';
  recTimer = setTimeout(() => { if (isRecording) { showToast('⏱ Maximum recording length reached'); stopRecording(); } }, maxAudioSeconds * 1000);
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (sttProvider === 'browser') {
    if (SR) {
      recognition = new SR(); recognition.lang = 'en-US'; recognition.continuous = true; recognition.interimResults = true;
      recognition.onresult = e => {
        const text = Array.from(e.results).map(r => r[0].transcript).join(' ');
        const inp = document.getElementById('msg-input'); inp.value = text; autoResize(inp);
      };
      recognition.onerror = () => showToast('ℹ Speech-to-text unavailable in this browser — type your question');
      try { recognition.start(); } catch {}
    } else showToast('ℹ This browser has no speech-to-text (use Chrome/Edge) — the recording only informs mood.');
  }
  showToast('🎤 Recording… click again to stop');
}

function stopRecording() {
  clearTimeout(recTimer);
  if (recognition) { try { recognition.stop(); } catch {} recognition = null; }
  if (mediaRecorder && isRecording) {
    mediaRecorder.stop();
    recStream && recStream.getTracks().forEach(t => t.stop());
    isRecording = false;
    const btn = document.getElementById('mic-btn'); btn.classList.remove('recording'); btn.textContent = '🎤';
  }
}

async function blobToWav(blob) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  const ctx = new Ctx();
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const sr = 16000, len = Math.max(1, Math.ceil(decoded.duration * sr));
    const off = new OfflineAudioContext(1, len, sr);
    const src = off.createBufferSource(); src.buffer = decoded; src.connect(off.destination); src.start();
    const pcm = (await off.startRendering()).getChannelData(0);
    const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); w(8, 'WAVEfmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
    v.setUint16(22, 1, true); v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    w(36, 'data'); v.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i])) * 0x7fff, true);
    return new Blob([buf], { type: 'audio/wav' });
  } finally { ctx.close && ctx.close(); }
}

async function processAudio() {
  const rec = new Blob(audioChunks, { type: (mediaRecorder && mediaRecorder.mimeType) || 'audio/webm' });
  console.info('[audio] recorded mime=%s bytes=%d', rec.type, rec.size);
  audioFeatures = null;
  if (rec.size < 500) { showToast('ℹ Recording was empty — nothing to analyse'); return; }
  let upload = rec, name = 'recording.webm';
  try { upload = await blobToWav(rec); name = 'recording.wav'; }
  catch (e) { console.warn('[audio] WAV conversion failed, uploading original', e); name = 'recording.' + (rec.type.includes('ogg') ? 'ogg' : rec.type.includes('mp4') ? 'm4a' : 'webm'); }
  const form = new FormData(); form.append('file', upload, name);
  try {
    const d = await api('/audio', { method: 'POST', body: form });
    audioFeatures = { pitch_mean: d.pitch_mean, pitch_variance: d.pitch_variance, speech_rate: d.speech_rate, pause_count: d.pause_count };
    if (d.transcript) { const inp = document.getElementById('msg-input'); inp.value = d.transcript; autoResize(inp); inp.focus(); showToast('🎤 Transcribed — check the text, then press send'); }
    else if (d.stt_error) showToast('ℹ Audio analysed for mood, but transcription failed — type your question');
    else showToast('🎵 Voice tone analysed (used for mood, not transcription)');
  } catch (e) { showToast(`❌ Audio: ${e.message}`, 5000); }     // audio errors never touch the chat history
}

// ── Chat ──────────────────────────────────────────────────────────────────────
function handleKeydown(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
function autoResize(el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 110) + 'px'; }

async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim();
  if (!text || !sessionId) return;
  const latency = getLatencyMs(), bs = backspaceCount;
  input.value = ''; input.style.height = 'auto';
  appendUserMessage(text);
  const typingEl = showTyping();
  try {
    const data = await post('/chat', { session_id: sessionId, message: text, attempt_number: attemptNumber,
                                       response_latency_ms: latency, backspace_count: bs, audio_features: audioFeatures });
    typingEl.remove();
    appendAssistantMessage(data); updateMoodUI(data.mood);
    moodTimeline.push(data.mood.label); renderMoodDots();
    audioFeatures = null; attemptNumber = 1; resetSignals();
    loadSessions(); loadWeakTopics();
  } catch (e) {
    typingEl.remove();
    if (e.status === 404) { try { localStorage.removeItem(LS_SESSION); } catch {} }
    const d = document.createElement('div'); d.className = 'message assistant';
    d.innerHTML = `<div class="bubble" style="color:var(--bad-text)">❌ ${esc(e.message)}</div>`;
    document.getElementById('messages').appendChild(d);
    input.value = text;                                               // let the user retry without retyping
  }
}

function appendUserMessage(text) {
  const msgs = document.getElementById('messages'), div = document.createElement('div');
  div.className = 'message user'; div.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight;
}

function appendAssistantMessage(data) {
  const msgs = document.getElementById('messages'), div = document.createElement('div');
  div.className = 'message assistant';
  const srcs = (data.retrieval && data.retrieval.sources) || [];
  const grounding = data.retrieval ? data.retrieval.grounding : 'strong';
  const bubble = document.createElement('div'); bubble.className = 'bubble md'; bubble.innerHTML = md(data.response);
  div.appendChild(bubble);

  const actions = document.createElement('div'); actions.className = 'msg-actions';
  if ('speechSynthesis' in window) {
    const t = document.createElement('button'); t.className = 'msg-act tts-btn'; t.textContent = '🔊 Read aloud'; t.title = 'Read this answer aloud';
    t.addEventListener('click', () => speak(data.response, t)); actions.appendChild(t);
  }
  const c = document.createElement('button'); c.className = 'msg-act'; c.textContent = '📋 Copy';
  c.addEventListener('click', async () => { try { await navigator.clipboard.writeText(data.response); showToast('Copied'); } catch { showToast('Copy is not available here'); } });
  actions.appendChild(c); div.appendChild(actions);

  const badge = (cls, text) => { const b = document.createElement('div'); b.className = cls; b.textContent = text; div.appendChild(b); };
  if (data.tone_explanation) badge('mood-badge', data.tone_explanation);
  if (data.teaching_strategy) badge('mood-badge strategy', `🎯 Teaching style: ${data.teaching_strategy.label}`);
  if (data.interpreted_as) badge('mood-badge', `↪ Understood as: ${data.interpreted_as}`);
  if (grounding === 'none') badge('not-grounded', '⚠ Not in your notes — general knowledge');
  else if (grounding === 'weak') badge('not-grounded weak', 'ℹ Notes only loosely related');
  if (data.retrieval && data.retrieval.truncated) badge('not-grounded weak', 'ℹ Notes were long — ask me to continue for the rest');

  if (srcs.length) {
    const tog = document.createElement('button'); tog.className = 'sources-toggle'; tog.textContent = `📎 ${srcs.length} source${srcs.length > 1 ? 's' : ''} — show`;
    tog.addEventListener('click', () => toggleSources(tog));
    const list = document.createElement('div'); list.className = 'sources-list';
    srcs.forEach(sr => {
      const it = document.createElement('div'); it.className = 'source-item';
      const st = document.createElement('strong'); st.textContent = sr.filename;
      it.append(st, document.createTextNode(` · ${sr.chapter}${sr.page ? ` · p.${sr.page}` : ''}`));
      const sc = document.createElement('span'); sc.className = 'source-score'; sc.textContent = `${(sr.score * 100).toFixed(0)}%`; it.appendChild(sc);
      if (sr.doc_id) { const o = document.createElement('button'); o.className = 'src-open'; o.textContent = 'open section'; o.addEventListener('click', () => openPassage(sr.doc_id, sr.chapter)); it.appendChild(o); }
      const pv = document.createElement('div'); pv.style.cssText = 'margin-top:2px;font-size:9px;'; pv.textContent = sr.preview || ''; it.appendChild(pv);
      list.appendChild(it);
    });
    div.append(tog, list);
  }

  if (data.retrieval) {                                               // "why this answer" — transparency panel
    const d = document.createElement('details'); d.className = 'why';
    const sm = document.createElement('summary'); sm.textContent = '🔍 Why this answer'; d.appendChild(sm);
    const line = (k, v) => { if (!v) return; const p = document.createElement('div'); p.className = 'why-line'; const b = document.createElement('strong'); b.textContent = k + ': '; p.append(b, document.createTextNode(v)); d.appendChild(p); };
    line('Understood as', data.interpreted_as || 'your message as written');
    line('Topic', data.active_topic);
    line('Evidence', { strong: 'strong match in your notes', weak: 'weak match — only loosely related', none: 'nothing relevant in your notes' }[grounding] + (srcs.length ? ` (${srcs.length} section${srcs.length > 1 ? 's' : ''}, best match ${(Math.max(...srcs.map(x => x.score)) * 100).toFixed(0)}%)` : ''));
    line('Mood', data.mood ? `${data.mood.label} (${Math.round(data.mood.confidence * 100)}% sure)` : '');
    if (data.teaching_strategy) line('Teaching style', `${data.teaching_strategy.label} — ${data.teaching_strategy.detail}`);
    div.appendChild(d);
  }
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight; questionStartTime = Date.now();
}

function toggleSources(btn) {
  const list = btn.nextElementSibling, showing = list.style.display === 'block';
  list.style.display = showing ? 'none' : 'block';
  btn.textContent = btn.textContent.replace(showing ? '— hide' : '— show', showing ? '— show' : '— hide');
}

function showTyping() {
  const msgs = document.getElementById('messages'), div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML = '<div class="typing-indicator"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div>';
  msgs.appendChild(div); msgs.scrollTop = msgs.scrollHeight; return div;
}

// ── Mood UI ───────────────────────────────────────────────────────────────────
function updateMoodUI(mood, fromHistory) {
  const color = MOOD_COLORS[mood.label] || '#1f8a8a';
  document.getElementById('mood-orb').style.cssText = `background:${color};box-shadow:0 0 10px ${color}`;
  document.getElementById('mood-label-top').textContent = fromHistory ? `${mood.label}` : `${mood.emoji} ${mood.label} (${Math.round(mood.confidence * 100)}%)`;
}

function renderMoodDots() {
  const el = document.getElementById('mood-dots');
  if (!moodTimeline.length) { el.innerHTML = '<div style="font-size:12px;color:var(--text2)">No mood data yet</div>'; return; }
  el.innerHTML = moodTimeline.map((m, i) => `<div class="mood-dot" title="${esc(m)} (msg ${i + 1})" style="background:${MOOD_COLORS[m] || '#868e96'}"></div>`).join('');
}

// ── Quiz ──────────────────────────────────────────────────────────────────────
async function generateQuiz() {
  const topic = document.getElementById('quiz-topic').value.trim();
  const type = document.getElementById('quiz-type').value;
  const num = parseInt(document.getElementById('quiz-num').value) || 3;
  const btn = document.getElementById('gen-btn');
  if (!topic) { showToast('Enter a topic first'); return; }
  if (!sessionId) { showToast('No session — reload page'); return; }
  btn.disabled = true; btn.textContent = 'Generating…';
  document.getElementById('quiz-content').innerHTML = `<div style="color:var(--text2);display:flex;align-items:center;gap:8px;"><span class="spinner"></span> Generating ${num} questions on "${esc(topic)}"…</div>`;
  try {
    currentQuiz = await post('/quiz/generate', { session_id: sessionId, topic, question_type: type, num_questions: num, difficulty: document.getElementById('quiz-diff').value });
    renderQuiz(currentQuiz);
  } catch (e) {
    document.getElementById('quiz-content').innerHTML = `<div style="color:var(--bad-text)">❌ ${esc(e.message)}</div>`;
  } finally { btn.disabled = false; btn.textContent = 'Generate Quiz'; }
}

function renderQuiz(q) {
  const el = document.getElementById('quiz-content');
  el.innerHTML = '';
  if (q.difficulty) { const b = document.createElement('div'); b.className = 'mood-badge'; b.style.marginBottom = '12px';
    b.textContent = `🎚 Difficulty: ${q.difficulty} — ${q.difficulty_reason || ''}`; el.appendChild(b); }
  q.questions.forEach((it, i) => {
    const card = document.createElement('div'); card.className = 'quiz-question-card';
    card.innerHTML = `<div class="quiz-q-num">Question ${i + 1} / ${q.questions.length}</div><div class="quiz-q-text">${esc(it.question)}</div>`;
    const grade = document.createElement('div');
    if (q.question_type === 'mcq' && it.options) {
      const opts = document.createElement('div'); opts.className = 'mcq-options';
      Object.entries(it.options).forEach(([l, t]) => {
        const o = document.createElement('div'); o.className = 'mcq-option'; o.dataset.letter = l;
        o.innerHTML = `<strong>${esc(l)})</strong> ${esc(t)}`;
        o.addEventListener('click', () => submitMCQ(i, l, opts, grade));
        opts.appendChild(o);
      });
      card.append(opts, grade);
    } else {
      const ta = document.createElement('textarea'); ta.className = 'answer-input';
      ta.placeholder = q.question_type === 'feynman' ? 'Explain as simply as possible — like teaching a 12-year-old…' : 'Write your answer here…';
      const wrap = document.createElement('div'); wrap.style.marginTop = '8px';
      const b = document.createElement('button'); b.className = 'quiz-btn'; b.style.cssText = 'font-size:12px;padding:7px 14px;'; b.textContent = 'Submit Answer';
      b.addEventListener('click', () => submitOpen(i, ta, b, grade));
      wrap.appendChild(b); card.append(ta, wrap, grade);
    }
    el.appendChild(card);
  });
}

async function submitMCQ(i, letter, optsEl, gradeEl) {
  if (optsEl.dataset.done) return;
  optsEl.dataset.done = '1';                                           // one click = one submission
  optsEl.querySelectorAll('.mcq-option').forEach(o => o.classList.toggle('selected', o.dataset.letter === letter));
  try {
    const r = await post('/quiz/grade', { quiz_id: currentQuiz.quiz_id, question_index: i, student_answer: letter });
    optsEl.querySelectorAll('.mcq-option').forEach(o => {
      if (o.dataset.letter === r.correct_answer) o.classList.add('correct');
      else if (o.dataset.letter === letter && !r.correct) o.classList.add('incorrect');
    });
    gradeEl.innerHTML = `<div class="grade-result"><div class="score-display" style="color:${r.correct ? 'var(--success)' : 'var(--danger)'}">
      ${r.correct ? '✅ Correct!' : `❌ Incorrect — answer: ${esc(r.correct_answer)}`}</div>
      ${r.explanation ? `<div class="feedback-text">${esc(r.explanation)}</div>` : ''}${optionNotesHtml(r, letter)}${topicStatusHtml(r)}</div>`;
    loadWeakTopics();
  } catch (e) { delete optsEl.dataset.done; gradeEl.innerHTML = `<div style="color:var(--bad-text);margin-top:8px;">❌ ${esc(e.message)}</div>`; }
}

function optionNotesHtml(r, chosen) {
  const n = r.option_notes || {}, keys = Object.keys(n); if (!keys.length) return '';
  return `<div class="feedback-text"><strong>${r.correct ? 'Why the other options are wrong' : 'Why each wrong option is wrong'}:</strong>` +
    keys.map(k => `<div style="margin-top:3px;${k === chosen ? 'color:var(--bad-text)' : ''}">${esc(k)}) ${esc(n[k])}${k === chosen ? ' ← your choice' : ''}</div>`).join('') + '</div>';
}

function topicStatusHtml(r) {
  const s = r.topic_status; if (!s || s.status === 'not_assessed') return '';
  const label = { learning: 'building evidence', mastered: 'mastered ✅', needs_revision: 'needs revision', weak: 'weak' }[s.status] || s.status;
  return `<div class="feedback-text" style="opacity:.8">Topic status: ${label}${s.mastery != null ? ` · mastery ${(s.mastery * 100).toFixed(0)}%` : ''} (${s.attempts} graded answer${s.attempts !== 1 ? 's' : ''})</div>`;
}

async function submitOpen(i, ta, btn, gradeEl) {
  const answer = ta.value.trim();
  if (!answer) { showToast('Write an answer first'); return; }
  btn.disabled = true;
  gradeEl.innerHTML = '<div style="color:var(--text2);margin-top:8px;display:flex;align-items:center;gap:6px;"><span class="spinner"></span> Grading…</div>';
  try {
    const d = await post('/quiz/grade', { quiz_id: currentQuiz.quiz_id, question_index: i, student_answer: answer });
    const pct = d.score * 100, color = pct >= 70 ? 'var(--success)' : pct >= 40 ? 'var(--warning)' : 'var(--danger)';
    gradeEl.innerHTML = `<div class="grade-result"><div class="score-display" style="color:${color}">${esc(d.score_percent)}</div>
      <div class="score-bar-bg"><div class="score-bar-fill" style="background:${color};width:${pct}%"></div></div>
      <div class="gap-tags">${(d.concepts_present || []).map(c => `<span class="gap-tag present">✓ ${esc(c)}</span>`).join('')}
        ${(d.concepts_missing || []).map(c => `<span class="gap-tag missing">✗ ${esc(c)}</span>`).join('')}
        ${(d.misconceptions || []).map(c => `<span class="gap-tag warn">⚠ ${esc(c)}</span>`).join('')}</div>
      ${d.feedback ? `<div class="feedback-text">${esc(d.feedback)}</div>` : ''}${topicStatusHtml(d)}</div>`;
    ta.disabled = true; loadWeakTopics();
  } catch (e) { btn.disabled = false; gradeEl.innerHTML = `<div style="color:var(--bad-text);margin-top:8px;">❌ ${esc(e.message)}</div>`; }
}

// ── Analytics ─────────────────────────────────────────────────────────────────
async function loadAnalytics() {
  if (!sessionId) return;
  const el = document.getElementById('analytics-content');
  el.innerHTML = '<div style="color:var(--text2);display:flex;align-items:center;gap:8px;"><span class="spinner"></span> Loading analytics…</div>';
  try { renderAnalytics(await api(`/analytics/${encodeURIComponent(sessionId)}`)); }
  catch (e) { el.innerHTML = `<div style="color:var(--bad-text)">❌ Could not load analytics: ${esc(e.message)}</div>`; }
}

function renderAnalytics(data) {
  const el = document.getElementById('analytics-content');
  const mood = data.mood_summary || {}, topics = (data.topic_stats || []).filter(t => t.attempts > 0), weak = data.weak_topics || [];
  const mastered = topics.filter(t => t.status === 'mastered'), learning = topics.filter(t => t.status === 'learning');
  const totalMsgs = (mood.timeline || []).length;
  const dominant = Object.entries({ frustrated: mood.frustrated || 0, confused: mood.confused || 0, confident: mood.confident || 0, bored: mood.bored || 0, neutral: mood.neutral || 0 }).sort((a, b) => b[1] - a[1])[0];
  const avg = topics.length ? (topics.reduce((s, t) => s + t.mastery, 0) / topics.length * 100).toFixed(0) : '—';
  const col = v => v >= 70 ? 'var(--success)' : v >= 40 ? 'var(--warning)' : 'var(--danger)';
  const max = Math.max(...['frustrated','confused','confident','bored','neutral'].map(m => mood[m] || 0), 1);
  const group = (title, list, color) => list.length ? `<div class="stat-card" style="margin-bottom:14px;"><h4>${title}</h4><div style="margin-top:8px;">${list.map(t => {
      const pct = ((t.mastery || 0) * 100).toFixed(0);
      return `<div class="topic-bar-wrap"><div class="topic-bar-label"><span>${esc(t.topic)}</span><span style="color:${color}">${pct}% · ${t.correct}/${t.attempts} correct${t.confusion_signals ? ` · ${t.confusion_signals} “I don't understand”` : ''}</span></div>
      <div class="topic-bar-bg"><div class="topic-bar-fill" style="width:${pct}%;background:${color}"></div></div></div>`; }).join('')}</div></div>` : '';
  el.innerHTML = `
    <div class="analytics-grid">
      <div class="stat-card"><h4>Messages</h4><div class="stat-value">${totalMsgs}</div><div class="stat-label">this chat</div></div>
      <div class="stat-card"><h4>Dominant Mood</h4><div class="stat-value" style="font-size:22px;">${dominant && dominant[1] ? esc(dominant[0]) : '—'}</div><div class="stat-label">${dominant && dominant[1] ? `${dominant[1]} times` : 'no data'}</div></div>
      <div class="stat-card"><h4>Avg Mastery</h4><div class="stat-value" style="color:${avg === '—' ? 'inherit' : col(avg)}">${avg}${avg !== '—' ? '%' : ''}</div><div class="stat-label">${topics.length} topic${topics.length !== 1 ? 's' : ''} quizzed</div></div>
      <div class="stat-card"><h4>To Revise</h4><div class="stat-value" style="color:${weak.length ? 'var(--danger)' : 'var(--success)'}">${weak.length}</div><div class="stat-label">${weak.length ? 'from quiz results' : 'all good!'}</div></div>
    </div>
    <div class="stat-card" style="margin-bottom:14px;"><h4>Mood Distribution (this chat)</h4><div class="mood-chart">
      ${['frustrated','confused','confident','bored','neutral'].map(m => { const c = mood[m] || 0; return `<div class="mood-bar-col"><div class="mood-bar" style="height:${c / max * 100}%;background:${MOOD_COLORS[m]};min-height:${c > 0 ? 4 : 2}px"></div><div class="mood-bar-lbl">${m.slice(0, 4)}</div></div>`; }).join('')}
    </div></div>
    ${group('⚠️ Needs revision / weak', weak.filter(t => t.attempts > 0), 'var(--danger)')}
    ${group('📈 Still building evidence', learning, 'var(--warning)')}
    ${group('✅ Mastered', mastered, 'var(--success)')}
    ${weak.filter(t => t.reason === 'repeated confusion').length ? `<div class="stat-card"><h4>🙋 You said you were stuck on</h4>${weak.filter(t => t.reason === 'repeated confusion').map(t => `<div style="padding:4px 0;font-size:13px;">${esc(t.topic)} <button class="revision-btn" data-topic="${esc(t.topic)}">Quiz me</button></div>`).join('')}</div>` : ''}`;
  el.querySelectorAll('button[data-topic]').forEach(b => b.addEventListener('click', () => startRevision(b.dataset.topic)));
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function esc(str) {
  if (str === null || str === undefined || str === '') return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/\n/g, '<br>');
}
let toastTimer;
function showToast(msg, duration = 3500) {
  const t = document.getElementById('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), duration);
}

init();
