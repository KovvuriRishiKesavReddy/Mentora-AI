// study.js — rendering helpers and the study features (summary, flashcards, mind map, revision plan),
// citations (passage modal), read-aloud, theme and mobile menu. Loaded before app.js; uses its api()/esc()/showToast().
// All user/model text is inserted with textContent or through escHtml(), never as raw HTML.

// ── Safe Markdown (bold, italics, code, headings, lists, tables) ──────────────────────────────────────
function escHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function mdInline(t) {
  return t.replace(/`([^`]+)`/g, '<code>$1</code>')
          .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
          .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?![\w*])/g, '$1<em>$2</em>');
}
function mdTableRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
}
function md(src) {
  const text = String(src ?? '').replace(/\r\n?/g, '\n');
  const parts = text.split(/```[a-zA-Z0-9_+-]*\n?([\s\S]*?)```/);      // odd indexes are code blocks
  let html = '';
  parts.forEach((part, idx) => {
    if (idx % 2 === 1) { html += `<pre><code>${escHtml(part.replace(/\n$/, ''))}</code></pre>`; return; }
    const lines = escHtml(part).split('\n');
    let i = 0, para = [];
    const flushPara = () => { if (para.length) { html += `<p>${mdInline(para.join('<br>'))}</p>`; para = []; } };
    while (i < lines.length) {
      const line = lines[i], trimmed = line.trim();
      if (!trimmed) { flushPara(); i++; continue; }
      let m;
      if ((m = trimmed.match(/^(#{1,4})\s+(.*)$/))) { flushPara(); const n = Math.min(m[1].length + 2, 6); html += `<h${n}>${mdInline(m[2])}</h${n}>`; i++; continue; }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) { flushPara(); html += '<hr>'; i++; continue; }
      if (trimmed.startsWith('|') && i + 1 < lines.length && /^\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1].trim())) {
        flushPara();
        const head = mdTableRow(trimmed); i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(mdTableRow(lines[i])); i++; }
        html += '<div class="md-table"><table><thead><tr>' + head.map(c => `<th>${mdInline(c)}</th>`).join('') + '</tr></thead><tbody>' +
                rows.map(r => '<tr>' + r.map(c => `<td>${mdInline(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>';
        continue;
      }
      if (/^([-*•]|\d+[.)])\s+/.test(trimmed)) {
        flushPara();
        const ordered = /^\d+[.)]\s+/.test(trimmed), items = [];
        while (i < lines.length && /^([-*•]|\d+[.)])\s+/.test(lines[i].trim())) {
          const indent = Math.min(3, Math.floor((lines[i].match(/^\s*/)[0].length) / 2));
          items.push(`<li style="margin-left:${indent * 16}px">${mdInline(lines[i].trim().replace(/^([-*•]|\d+[.)])\s+/, ''))}</li>`);
          i++;
        }
        html += `<${ordered ? 'ol' : 'ul'}>${items.join('')}</${ordered ? 'ol' : 'ul'}>`;
        continue;
      }
      para.push(trimmed); i++;
    }
    flushPara();
  });
  return html;
}
function mdPlain(src) {                                   // text for read-aloud / copy
  return String(src ?? '').replace(/```[\s\S]*?```/g, ' code omitted. ').replace(/[|#*_`>]/g, ' ').replace(/\s+/g, ' ').trim();
}

// ── Theme + mobile menu ───────────────────────────────────────────────────────────────────────────────
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  const b = document.getElementById('theme-btn'); if (b) b.textContent = t === 'light' ? '🌙' : '☀️';
  if (b) b.title = t === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
}
function toggleTheme() {
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  applyTheme(next); try { localStorage.setItem('mentora.theme', next); } catch {}
}
(function initTheme() {
  let t = null; try { t = localStorage.getItem('mentora.theme'); } catch {}
  applyTheme(t === 'dark' ? 'dark' : 'light');
})();
function toggleMenu() { document.querySelector('.sidebar').classList.toggle('open'); }

// ── Citations: open the exact passage ─────────────────────────────────────────────────────────────────
async function openPassage(docId, heading) {
  if (!docId) { showToast('This source has no document link.'); return; }
  try {
    const p = await api(`/passage?doc_id=${encodeURIComponent(docId)}&heading=${encodeURIComponent(heading)}`);
    document.getElementById('modal-title').textContent = p.heading;
    document.getElementById('modal-meta').textContent = `${p.filename}${p.page ? ` · page ${p.page}` : ''}${p.truncated ? ' · (shortened)' : ''}`;
    document.getElementById('modal-body').innerHTML = esc(p.text);
    document.getElementById('modal').classList.add('show');
  } catch (e) { showToast(`❌ ${e.message}`); }
}
function closeModal(ev) {
  if (ev && ev.target && ev.target.id !== 'modal') return;
  document.getElementById('modal').classList.remove('show');
}
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── Read aloud (browser speech synthesis; nothing is uploaded) ───────────────────────────────────────
let ttsBtn = null;
function speak(text, btn) {
  if (!('speechSynthesis' in window)) { showToast('Read-aloud is not supported in this browser'); return; }
  const synth = window.speechSynthesis, same = ttsBtn === btn && synth.speaking;
  synth.cancel(); if (ttsBtn) ttsBtn.classList.remove('on'); ttsBtn = null;
  if (same) return;
  const chunks = mdPlain(text).match(/[^.!?]+[.!?]*\s*/g) || [];
  const groups = []; let cur = '';
  chunks.forEach(c => { if ((cur + c).length > 180) { if (cur) groups.push(cur); cur = c; } else cur += c; });
  if (cur) groups.push(cur);
  ttsBtn = btn; btn.classList.add('on');
  groups.forEach((g, i) => {
    const u = new SpeechSynthesisUtterance(g);
    if (i === groups.length - 1) u.onend = () => { btn.classList.remove('on'); if (ttsBtn === btn) ttsBtn = null; };
    synth.speak(u);
  });
}

// ── Upload with a real progress bar ───────────────────────────────────────────────────────────────────
function uploadWithProgress(path, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', API + path);
    xhr.setRequestHeader('X-Client-Id', clientId);
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.upload.onload = () => onProgress(1);
    xhr.onerror = () => { setOffline(true); const e = new Error('Cannot reach the backend. Is start.bat running?'); e.network = true; reject(e); };
    xhr.onload = () => {
      setOffline(false);
      let data = null; try { data = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else { const e = new Error((data && data.detail) || `Request failed (${xhr.status})`); e.status = xhr.status; reject(e); }
    };
    xhr.send(form);
  });
}

// ── Study tab ─────────────────────────────────────────────────────────────────────────────────────────
let studyDocs = [], studySubTab = 'summary', lastSummary = null, cards = [], cardIdx = 0, cardFlipped = false, cardKnown = new Set();

function setStudyDocs(files) {
  studyDocs = files || [];
  const sel = document.getElementById('study-doc'); if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '';
  studyDocs.forEach(f => { const o = document.createElement('option'); o.value = f.doc_id; o.textContent = f.filename; sel.appendChild(o); });
  if (studyDocs.some(f => f.doc_id === prev)) sel.value = prev;
  const has = studyDocs.length > 0;
  document.getElementById('study-empty').style.display = has ? 'none' : 'block';
  sel.style.display = has ? '' : 'none';
  ['summary', 'cards', 'map'].forEach(k => {
    document.getElementById(`sub-${k}`).style.display = has ? '' : 'none';
    document.getElementById(`study-${k}`).style.display = has && k === studySubTab ? 'block' : 'none';
  });
}
function studyDocId() { const s = document.getElementById('study-doc'); return s ? s.value : ''; }
function loadStudyDocs() { if (studyDocs.length) onStudyDocChange(); }

function studySub(k) {
  studySubTab = k;
  ['summary', 'cards', 'map'].forEach(x => {
    document.getElementById(`sub-${x}`).classList.toggle('active', x === k);
    document.getElementById(`study-${x}`).style.display = x === k && studyDocs.length ? 'block' : 'none';
  });
  if (k === 'map') loadMap();
  if (k === 'summary') onStudyDocChange();
}

async function onStudyDocChange() {
  const id = studyDocId(); if (!id) return;
  lastSummary = null; cards = []; document.getElementById('cards-content').innerHTML = '';
  document.getElementById('sum-refresh').style.display = 'none'; document.getElementById('sum-dl').style.display = 'none';
  if (studySubTab === 'map') loadMap();
  try { renderSummary(await api(`/summary/${encodeURIComponent(id)}`)); }
  catch (e) {
    if (e.status === 404) document.getElementById('summary-content').innerHTML =
      '<div style="color:var(--text2);font-size:13px;line-height:1.7;">No summary yet for this document. Click <strong>Summarise the whole notes</strong> — it covers every chapter and is saved afterwards.</div>';
  }
}

async function generateSummary(refresh) {
  const id = studyDocId(); if (!id) return;
  const btn = document.getElementById('sum-btn'); btn.disabled = true;
  document.getElementById('summary-content').innerHTML = '<div style="color:var(--text2);display:flex;align-items:center;gap:8px;"><span class="spinner"></span> Reading every chapter and summarising… this can take a minute for long notes.</div>';
  try { renderSummary(await post('/summary', { doc_id: id, refresh: !!refresh })); }
  catch (e) { document.getElementById('summary-content').innerHTML = `<div style="color:var(--bad-text)">❌ ${esc(e.message)}</div>`; }
  finally { btn.disabled = false; }
}

function renderSummary(s) {
  lastSummary = s;
  const el = document.getElementById('summary-content'); el.innerHTML = '';
  const add = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; el.appendChild(n); return n; };
  if (!s.complete) add('div', 'warn-note', 'Some parts could not be summarised by the model and show the first sentences of the text instead. Click Regenerate to try again.');
  add('h3', 'sum-h', 'Overview'); add('div', 'sum-overview').innerHTML = md(s.overview);
  if (s.key_terms && s.key_terms.length) { const t = add('div', 'chip-row'); s.key_terms.forEach(k => { const c = document.createElement('span'); c.className = 'chip'; c.textContent = k; t.appendChild(c); }); }
  s.chapters.forEach(c => {
    const d = document.createElement('details'); d.className = 'sum-chapter'; d.open = true;
    const sm = document.createElement('summary'); sm.textContent = c.title + (c.method === 'extractive' ? '  (extract)' : ''); d.appendChild(sm);
    const ul = document.createElement('ul'); c.bullets.forEach(b => { const li = document.createElement('li'); li.innerHTML = mdInline(escHtml(b)); ul.appendChild(li); });
    d.appendChild(ul); el.appendChild(d);
  });
  document.getElementById('sum-refresh').style.display = ''; document.getElementById('sum-dl').style.display = '';
  document.getElementById('sum-btn').textContent = 'Summarise again';
}
function downloadSummary() {
  if (!lastSummary) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([lastSummary.markdown || ''], { type: 'text/markdown' }));
  a.download = `summary-${(lastSummary.filename || 'notes').replace(/\.[^.]+$/, '')}.md`;
  document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ── Flashcards ────────────────────────────────────────────────────────────────────────────────────────
async function generateCards() {
  const id = studyDocId(); if (!id) return;
  const topic = document.getElementById('cards-topic').value.trim();
  const count = parseInt(document.getElementById('cards-count').value) || 10;
  const btn = document.getElementById('cards-btn'); btn.disabled = true;
  document.getElementById('cards-content').innerHTML = '<div style="color:var(--text2);display:flex;align-items:center;gap:8px;"><span class="spinner"></span> Making flashcards…</div>';
  try {
    const r = await post('/flashcards', { doc_id: id, topic: topic || null, count });
    cards = r.cards; cardIdx = 0; cardFlipped = false; cardKnown = new Set(); renderCard();
  } catch (e) { document.getElementById('cards-content').innerHTML = `<div style="color:var(--bad-text)">❌ ${esc(e.message)}</div>`; }
  finally { btn.disabled = false; }
}
function renderCard() {
  const el = document.getElementById('cards-content'); el.innerHTML = '';
  if (!cards.length) return;
  const c = cards[cardIdx];
  const card = document.createElement('div'); card.className = 'flashcard' + (cardFlipped ? ' flipped' : ''); card.tabIndex = 0;
  const side = document.createElement('div'); side.className = 'fc-side'; side.textContent = cardFlipped ? 'ANSWER' : 'QUESTION';
  const body = document.createElement('div'); body.className = 'fc-body'; body.textContent = cardFlipped ? c.back : c.front;
  const hint = document.createElement('div'); hint.className = 'fc-hint'; hint.textContent = 'click to flip';
  card.append(side, body, hint);
  const flip = () => { cardFlipped = !cardFlipped; renderCard(); };
  card.addEventListener('click', flip); card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } });
  const bar = document.createElement('div'); bar.className = 'study-actions'; bar.style.justifyContent = 'center';
  const mk = (label, fn, cls) => { const b = document.createElement('button'); b.className = cls || 'tab-btn'; b.textContent = label; b.addEventListener('click', fn); return b; };
  const go = d => { cardIdx = (cardIdx + d + cards.length) % cards.length; cardFlipped = false; renderCard(); };
  bar.append(mk('◀', () => go(-1)), mk('↻ Review again', () => { cardKnown.delete(cardIdx); go(1); }),
             mk('✓ I knew it', () => { cardKnown.add(cardIdx); go(1); }, 'quiz-btn'), mk('▶', () => go(1)),
             mk('🔀 Shuffle', () => { for (let i = cards.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [cards[i], cards[j]] = [cards[j], cards[i]]; } cardKnown = new Set(); cardIdx = 0; cardFlipped = false; renderCard(); }));
  const prog = document.createElement('div'); prog.className = 'fc-progress';
  prog.textContent = `Card ${cardIdx + 1} of ${cards.length} · ${cardKnown.size} known`;
  el.append(prog, card, bar);
}

// ── Mind map (chapters → sections), drawn as SVG from the document outline ───────────────────────────
async function loadMap() {
  const id = studyDocId(); const el = document.getElementById('map-content'); if (!id) return;
  el.innerHTML = '<div style="color:var(--text2);display:flex;align-items:center;gap:8px;"><span class="spinner"></span> Building map…</div>';
  try { renderMap(await api(`/outline/${encodeURIComponent(id)}`)); }
  catch (e) { el.innerHTML = `<div style="color:var(--bad-text)">❌ ${esc(e.message)}</div>`; }
}
function renderMap(o) {
  const NS = 'http://www.w3.org/2000/svg', rowH = 24, X = { root: 8, chap: 200, sec: 470 }, W = { root: 150, chap: 230, sec: 400 };
  const mk = (tag, attrs, text) => { const n = document.createElementNS(NS, tag); Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v)); if (text !== undefined) n.textContent = text; return n; };
  const clip = (s, w) => { const max = Math.floor(w / 6.4); return s.length > max ? s.slice(0, max - 1) + '…' : s; };
  let row = 0; const chapters = o.chapters.map(c => {
    const start = row; const secs = c.sections.map(s => ({ ...s, y: (row++) * rowH + 16 }));
    if (!secs.length) row++;
    const cy = secs.length ? (secs[0].y + secs[secs.length - 1].y) / 2 : start * rowH + 16;
    return { ...c, secs, y: cy };
  });
  const height = Math.max(row, 1) * rowH + 24, rootY = chapters.length ? (chapters[0].y + chapters[chapters.length - 1].y) / 2 : 20;
  const svg = mk('svg', { viewBox: `0 0 ${X.sec + W.sec + 8} ${height}`, width: '100%', role: 'img', 'aria-label': 'Mind map of the notes' });
  svg.style.maxWidth = '980px';
  const link = (x1, y1, x2, y2, color) => svg.appendChild(mk('path', { d: `M${x1} ${y1} C${(x1 + x2) / 2} ${y1},${(x1 + x2) / 2} ${y2},${x2} ${y2}`, fill: 'none', stroke: color, 'stroke-width': 1.6, opacity: .7 }));
  const node = (x, y, w, label, color, cls, onClick, title) => {
    const g = mk('g', { class: `mm-node ${cls}`, transform: `translate(${x},${y - 10})`, tabindex: onClick ? 0 : -1 });
    g.appendChild(mk('rect', { width: w, height: 20, rx: 10, fill: color, 'fill-opacity': cls === 'mm-sec' ? .13 : .9, stroke: color }));
    g.appendChild(mk('text', { x: 10, y: 14, class: cls === 'mm-sec' ? 'mm-text' : 'mm-text-on' }, clip(label, w - 12)));
    if (title) g.appendChild(mk('title', {}, title));
    if (onClick) { g.addEventListener('click', onClick); g.addEventListener('keydown', e => { if (e.key === 'Enter') onClick(); }); }
    svg.appendChild(g);
  };
  chapters.forEach((c, i) => {
    const color = `hsl(${4 + (i * 7) % 36}, 80%, 36%)`;
    link(X.root + W.root, rootY, X.chap, c.y, color);
    c.secs.forEach(s => link(X.chap + W.chap, c.y, X.sec, s.y, color));
  });
  node(X.root, rootY, W.root, o.filename.replace(/\.[^.]+$/, ''), '#c2410c', 'mm-root', null, o.filename);
  chapters.forEach((c, i) => {
    const color = `hsl(${4 + (i * 7) % 36}, 80%, 36%)`;
    node(X.chap, c.y, W.chap, c.title, color, 'mm-chap', () => askAbout(c.title.replace(/^\d+[.)]\s*/, '')), `Ask the tutor about: ${c.title}`);
    c.secs.forEach(s => node(X.sec, s.y, W.sec, s.title, color, 'mm-sec', () => openPassage(o.doc_id, s.path), `Open in your notes: ${s.title}`));
  });
  const el = document.getElementById('map-content'); el.innerHTML = ''; el.appendChild(svg);
}
function askAbout(topic) {
  switchTab('chat');
  const input = document.getElementById('msg-input'); input.value = `Explain ${topic}`; input.focus(); autoResize(input);
}

// ── Revision plan ─────────────────────────────────────────────────────────────────────────────────────
async function loadPlan() {
  const el = document.getElementById('plan-content');
  el.innerHTML = '<div style="color:var(--text2);display:flex;align-items:center;gap:8px;"><span class="spinner"></span> Building your plan…</div>';
  try { renderPlan(await api('/revision-plan')); }
  catch (e) { el.innerHTML = `<div style="color:var(--bad-text)">❌ ${esc(e.message)}</div>`; }
}
const PLAN_STATUS = { weak: 'Weak', needs_revision: 'Needs revision', learning: 'Building evidence', mastered: 'Mastered', needs_attention: 'You were stuck' };
function planCard(t, due) {
  const d = document.createElement('div'); d.className = `plan-card ${t.status}`;
  const head = document.createElement('div'); head.className = 'plan-head';
  const name = document.createElement('strong'); name.textContent = t.topic;
  const chip = document.createElement('span'); chip.className = 'chip'; chip.textContent = PLAN_STATUS[t.status] || t.status;
  head.append(name, chip); d.appendChild(head);
  const meta = document.createElement('div'); meta.className = 'plan-meta';
  const when = due ? (t.due_in_days < -0.5 ? `overdue by ${Math.round(-t.due_in_days)} day(s)` : 'due now') : `due in ${Math.max(1, Math.round(t.due_in_days))} day(s)`;
  meta.textContent = [t.mastery != null ? `mastery ${(t.mastery * 100).toFixed(0)}%` : null, t.attempts ? `${t.attempts} graded answer(s)` : null, t.reason, when].filter(Boolean).join(' · ');
  d.appendChild(meta);
  const act = document.createElement('div'); act.className = 'plan-action'; act.textContent = t.action; d.appendChild(act);
  const row = document.createElement('div'); row.className = 'study-actions';
  const btn = (label, fn, cls) => { const b = document.createElement('button'); b.className = cls || 'tab-btn'; b.textContent = label; b.addEventListener('click', fn); row.appendChild(b); };
  btn('📝 Quiz me', () => startRevision(t.topic), 'quiz-btn');
  btn('💬 Explain simply', () => { switchTab('chat'); const i = document.getElementById('msg-input'); i.value = `Explain ${t.topic} in simple words`; sendMessage(); });
  if (t.where_to_look && t.where_to_look.doc_id) btn('📖 Open in notes', () => openPassage(t.where_to_look.doc_id, t.where_to_look.heading));
  d.appendChild(row); return d;
}
function renderPlan(p) {
  const el = document.getElementById('plan-content'); el.innerHTML = '';
  const s = document.createElement('div'); s.className = 'plan-summary'; s.textContent = p.summary; el.appendChild(s);
  if (p.today.length) { const h = document.createElement('h4'); h.className = 'plan-h'; h.textContent = `Today (${p.today.length})`; el.appendChild(h); p.today.forEach(t => el.appendChild(planCard(t, true))); }
  if (p.upcoming.length) { const h = document.createElement('h4'); h.className = 'plan-h'; h.textContent = 'Coming up'; el.appendChild(h); p.upcoming.forEach(t => el.appendChild(planCard(t, false))); }
  if (p.mastered_count) { const m = document.createElement('div'); m.className = 'plan-meta'; m.style.marginTop = '12px'; m.textContent = `✅ ${p.mastered_count} topic(s) mastered so far.`; el.appendChild(m); }
}
