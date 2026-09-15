/* ============================================================================
   科研文献 PDF 智能阅读器 · 网页版逻辑（自建前端 → 自己掌控 DOM）

   与 Streamlit 版的关系：**同一个引擎（utils/），两套前端**。
   界面上的两个需求在这套代码里是"理所当然"的：
     ① 固定页面 + 卡内滚动 —— 全在 app.css（html/body 锁死 + 三块 overflow:auto + min-height:0）；
     ② 沉浸模式 —— 只给 body 加一个 class（body.immersive 由 CSS 隐藏左右两栏），
        这正是 demo 里验证过的做法。
   本文件负责：上传 → 取数据 → 渲染卡片 → 按需翻译 → 记阅读位置 → 图片灯箱。
   ========================================================================== */

const $ = (id) => document.getElementById(id);
const LS_KEY = 'reader-web-prefs-v1';

const state = {
  paper: null,          // /api/open 的响应
  i: 0,                 // 当前第几张卡
  lang: 'zh',
  height: 'normal',
  immersive: false,
  translations: new Map(),   // 原文 → 译文（会话内缓存；服务端还有一份磁盘缓存）
  busy: 0,
  pendingIndex: null,
};

/* ---------------- 小工具 ---------------- */
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function savePrefs() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(
      { lang: state.lang, height: state.height, immersive: state.immersive }));
  } catch (e) { /* 隐私模式下写不了，忽略 */ }
}
function loadPrefs() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    state.lang = data.lang || 'zh';
    state.height = data.height || 'normal';
    state.immersive = !!data.immersive;
  } catch (e) { /* 坏了就用默认值 */ }
}

function busy(on, title, sub) {
  state.busy += on ? 1 : -1;
  if (state.busy < 0) state.busy = 0;
  if (title) $('busy-title').textContent = title;
  if (sub) $('busy-sub').textContent = sub;
  $('busy').classList.toggle('on', state.busy > 0);
}

/* 正文渲染：按空行分段；[12] 这类文内引文标记淡色显示（引文不翻译，只保留原样） */
function bodyHTML(text) {
  return String(text).split(/\n+/).map((p) => p.trim()).filter(Boolean)
    .map((p) => '<p>' + esc(p)
      .replace(/\[([\d,\u2013\-\s]+)\]/g, '<span class="cite">[$1]</span>') + '</p>')
    .join('');
}

/* ---------------- 与后端通信 ---------------- */
async function api(path, options) {
  const response = await fetch(path, options);
  const kind = response.headers.get('Content-Type') || '';
  if (!kind.includes('application/json')) {
    throw new Error(`服务返回了非 JSON（HTTP ${response.status}）`);
  }
  const data = await response.json();
  if (!response.ok && !data.error) throw new Error(`HTTP ${response.status}`);
  return data;
}

async function openFile(file) {
  if (!file) return;
  busy(true, '正在处理文献…', `${file.name} · 解析 → 提取图片 → 读元数据`);
  try {
    const data = await api('/api/open', {
      method: 'POST',
      headers: { 'X-Filename': encodeURIComponent(file.name) },
      body: file,                       // 直接传字节，不需要 multipart
    });
    if (!data.ok) { alert('解析失败：' + (data.error || '未知原因')); return; }
    state.paper = data;
    state.translations.clear();
    state.i = Math.min(data.position || 0, data.cards.length - 1);
    renderAll();
  } catch (err) {
    alert('打开失败：' + err.message);
  } finally {
    busy(false);
  }
}

async function savePosition(index) {
  try { await api('/api/position', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index }),
  }); } catch (e) { /* 记不上不影响阅读 */ }
}

/* 按需翻译：只翻"当前这一张卡里还没译文的段落"；服务端有磁盘缓存，重复翻不花钱 */
async function ensureTranslations(card) {
  const texts = card.segments.filter((s) => s.kind === 'text' || s.kind === 'heading')
    .map((s) => s.text);
  const missing = texts.filter((t) => !state.translations.has(t));
  if (!missing.length) return { ok: true, error: null };
  const note = $('translate-note');
  note.textContent = `正在翻译 ${missing.length} 段…`;
  try {
    const data = await api('/api/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: missing, target: 'zh' }),
    });
    (data.translations || []).forEach((t, k) => {
      if (t) state.translations.set(missing[k], t);
    });
    note.textContent = data.error ? ('翻译失败：' + data.error) : '';
    return data;
  } catch (err) {
    note.textContent = '翻译请求失败：' + err.message;
    return { ok: false, error: err.message };
  }
}

/* ---------------- 渲染 ---------------- */
function figureHTML(seg) {
  // 图片用 id 引用，PNG 由 /api/img/<id> 提供；点击换成 size=full 看原尺寸
  const caption = seg.kind === 'figure' ? (seg.meta || '')
    : (seg.kind === 'table' ? '表格区域 · ' + (seg.meta || '') : '公式区域 · ' + (seg.meta || ''));
  return `<figure><img alt="${esc(caption)}" src="/api/img/${encodeURIComponent(seg.id)}?size=thumb"
      data-id="${esc(seg.id)}" data-full="/api/img/${encodeURIComponent(seg.id)}?size=full">`
    + `<figcaption>${esc(caption)}<span class="zoom">点击放大</span></figcaption></figure>`;
}

function renderCard(rev) {
  const paper = state.paper;
  const card = paper.cards[state.i];
  const zh = state.lang === 'zh';
  const title = [card.number, card.section].filter(Boolean).join(' ');
  const head = `<header><span class="chip">${esc(title || '文首')}</span>`
    + `<span class="badge">原文第 ${card.page}${card.page_end !== card.page ? '–' + card.page_end : ''} 页 · ${card.words} 词</span>`
    + `</header>`;

  let body = '';
  for (const seg of card.segments) {
    if (seg.kind === 'heading') {
      const text = zh ? (state.translations.get(seg.text) || seg.text) : seg.text;
      body += `<h3>▍${esc(text)}</h3>`;
    } else if (seg.kind === 'text') {
      const text = zh ? (state.translations.get(seg.text) || seg.text) : seg.text;
      body += bodyHTML(text);
    } else {
      body += figureHTML(seg);
    }
  }
  const hint = (zh && state.lang === 'zh' && !card.segments.some((s) => s.kind === 'text'))
    ? '' : '';
  $('deck').innerHTML = `<article class="card ${rev ? 'rev' : ''}">
      <div class="inner">${head}<div class="body">${body}</div></div></article>`;
  $('deck').scrollTop = 0;
  updateScrollHint();
}

function renderChrome() {
  const paper = state.paper;
  const total = paper ? paper.cards.length : 0;
  $('counter').textContent = paper
    ? `第 ${state.i + 1} / ${total} 张 · 章节：${[paper.cards[state.i].number, paper.cards[state.i].section].filter(Boolean).join(' ') || '文首'}`
    : '';
  $('dots').innerHTML = paper ? paper.cards.map((_, k) =>
    `<i class="${k === state.i ? 'on' : ''}" data-i="${k}"></i>`).join('') : '';
  $('nav').innerHTML = paper ? paper.cards.map((c, k) =>
    `<li><button data-i="${k}" class="${k === state.i ? 'on' : ''}">`
    + `${esc([c.number, c.section].filter(Boolean).join(' ') || '文首')}</button></li>`).join('') : '';
  $('prev').disabled = !paper || total < 2;
  $('next').disabled = !paper || total < 2;
  document.querySelectorAll('#lang-seg button, #lang-seg-top button').forEach((b) =>
    b.classList.toggle('on', b.dataset.lang === state.lang));
  document.querySelectorAll('#height-seg button').forEach((b) =>
    b.classList.toggle('on', b.dataset.h === state.height));
  // 常驻工具条上的按钮：文案随状态变，任何时候都点得到（沉浸模式也看得见）
  $('btn-immersive').textContent = state.immersive ? '⤡ 退出沉浸' : '⤢ 沉浸模式';
  $('btn-immersive').setAttribute('aria-pressed', String(state.immersive));
  $('btn-immersive').classList.toggle('on', state.immersive);
  document.body.className = 'h-' + state.height + (state.immersive ? ' immersive' : '');
  $('doc-title').textContent = paper ? `${paper.file.name} · ${paper.overview.layouts}` : $('doc-title').textContent;
  $('pill-cache').textContent = paper
    ? (paper.cache_hit ? '♻️ 命中本地缓存' : '🆕 首次解析')
    : '未打开文献';
  $('pill-cache').classList.toggle('ok', !!(paper && paper.cache_hit));
}

function renderMeta() {
  const paper = state.paper;
  if (!paper) return;
  const meta = paper.metadata;
  $('meta-title').textContent = meta.title.value || '（未识别到标题）';
  const order = ['doi', 'authors', 'first_author', 'corresponding', 'corresponding_email',
    'affiliations', 'supplementary', 'author_emails'];
  $('meta-list').innerHTML = order.map((key) => {
    // 防御式取字段：后端一定会给全十项，但界面不该因为少一项就整页崩掉
    const field = meta[key] || { label: key, value: '', source: '', found: false, note: '' };
    const value = (field.value || '（未找到）').replace(/\n/g, '　');
    const flag = field.found ? '' : ' ⚠️';
    return `<dt>${esc(field.label)}${flag}</dt><dd>${esc(value.slice(0, 300))}`
      + `<span class="src">来源：${esc(field.source || field.note || '—')}</span></dd>`;
  }).join('');
  const ov = paper.overview;
  $('overview').innerHTML = [
    ['页数', ov.pages], ['阅读卡片', ov.cards], ['正文词数', ov.words.toLocaleString()],
    ['插图', ov.images + ' 张'], ['公式区域', ov.formula_regions], ['表格区域', ov.table_regions],
    ['正文字号', ov.body_size + ' pt'], ['解析耗时', ov.elapsed + ' 秒'],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join('');
  const cache = paper.cache;
  $('cache-info').innerHTML = `缓存目录：<code>${esc(cache.root)}</code><br>`
    + `已保存 <b>${cache.papers}</b> 篇文献记录 · 译文 <b>${cache.translations}</b> 条 · `
    + `占用 <b>${cache.kb}</b> KB<br>`
    + `图片不落盘：插图与区域截图按需渲染。`;
  $('status').innerHTML = `✅ ${esc(paper.file.name)}<br>`
    + `文本 ${ov.pages} 页 / ${ov.cards} 张卡 · 插图 ${ov.images} 张<br>`
    + `${paper.cache_hit ? '♻️ 已命中本地记录，接着上次读' : '🆕 已写入本地缓存'}`
    + `${paper.backend ? ' · 翻译后端 ' + esc(paper.backend) : ' · ⚠️ 没有可用翻译后端'}`;
}

function renderAll() {
  renderChrome();
  renderMeta();
  if (!state.paper) return;
  renderCard(false);
  if (state.lang === 'zh') {
    ensureTranslations(state.paper.cards[state.i]).then(() => {
      if (state.lang === 'zh') renderCard(false);
    });
  }
}

/* ---------------- 交互 ---------------- */
function goto(index, rev) {
  if (!state.paper) return;
  const total = state.paper.cards.length;
  const next = ((index % total) + total) % total;
  if (next === state.i) return;
  state.i = next;
  renderChrome();
  renderCard(rev === undefined ? next < state.i : rev);
  savePosition(state.i);
  if (state.lang === 'zh') {
    ensureTranslations(state.paper.cards[state.i]).then(() => {
      if (state.lang === 'zh') renderCard(false);
    });
  }
}

function setLang(lang) {
  state.lang = lang;
  savePrefs();
  renderChrome();
  if (state.lang === 'zh') {
    ensureTranslations(state.paper.cards[state.i]).then(() => renderCard(false));
  } else {
    renderCard(false);
  }
}

function setImmersive(on) {
  state.immersive = on;
  savePrefs();
  renderChrome();
  updateScrollHint();
}

/* 「还能往下滚」提示：内容超出卡片高度、且还没滚到底时显示 */
function updateScrollHint() {
  const deck = $('deck'), wrap = $('deck-wrap');
  const remain = deck.scrollHeight - deck.clientHeight - deck.scrollTop;
  wrap.classList.toggle('more', remain > 6);
}

/* 图片放大 */
function lightboxOpen() { return $('lightbox').classList.contains('on'); }
function openLightbox(src, name) {
  $('lb-img').src = src;
  $('lb-img').alt = name || '';
  $('lb-name').textContent = name || '';
  $('lightbox').classList.add('on');
  $('lightbox').setAttribute('aria-hidden', 'false');
}
function closeLightbox() {
  $('lightbox').classList.remove('on');
  $('lightbox').setAttribute('aria-hidden', 'true');
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $('btn-upload').addEventListener('click', () => $('file-input').click());
  $('file-input').addEventListener('change', (e) => openFile(e.target.files[0]));

  $('lang-seg').addEventListener('click', (e) => {
    const btn = e.target.closest('button'); if (!btn || !state.paper) return;
    setLang(btn.dataset.lang);
  });
  $('lang-seg-top').addEventListener('click', (e) => {
    const btn = e.target.closest('button'); if (!btn || !state.paper) return;
    setLang(btn.dataset.lang);
  });
  $('height-seg').addEventListener('click', (e) => {
    const btn = e.target.closest('button'); if (!btn) return;
    state.height = btn.dataset.h; savePrefs(); renderChrome(); updateScrollHint();
  });
  $('btn-immersive').addEventListener('click', () => setImmersive(!state.immersive));

  $('prev').addEventListener('click', () => goto(state.i - 1, true));
  $('next').addEventListener('click', () => goto(state.i + 1, false));
  $('dots').addEventListener('click', (e) => {
    const dot = e.target.closest('i'); if (!dot) return;
    goto(Number(dot.dataset.i));
  });
  $('nav').addEventListener('click', (e) => {
    const btn = e.target.closest('button'); if (!btn) return;
    goto(Number(btn.dataset.i));
  });

  // 点图放大：事件委托（卡片每次重渲染，绑在每个 img 上会失效）
  $('deck').addEventListener('click', (e) => {
    const img = e.target.closest('figure img');
    if (img) openLightbox(img.dataset.full || img.src, img.alt);
  });
  $('deck').addEventListener('scroll', updateScrollHint, { passive: true });
  $('lb-close').addEventListener('click', closeLightbox);
  $('lightbox').addEventListener('click', (e) => {
    if (e.target.id !== 'lb-img') closeLightbox();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && lightboxOpen()) { closeLightbox(); return; }
    if (lightboxOpen()) return;                       // 灯箱开着时方向键不再翻卡
    const key = e.key.toLowerCase();
    // ⚠️ 沉浸开关必须放在「有没有打开文献」的判断**之前**：
    // 实测踩到——原来它在后面，没打开文献时按 I 没有任何反应，而按钮又在被隐藏的左栏里，
    // 结果进了沉浸就出不来。Esc 也一并作为退出键（熟悉的"返回"语义）。
    if (key === 'i') { setImmersive(!state.immersive); return; }
    if (e.key === 'Escape' && state.immersive) { setImmersive(false); return; }
    if (!state.paper) return;
    if (e.key === 'ArrowLeft') goto(state.i - 1, true);
    else if (e.key === 'ArrowRight') goto(state.i + 1, false);
    else if (key === 'l') setLang(state.lang === 'zh' ? 'en' : 'zh');
  });

  window.addEventListener('resize', updateScrollHint);
}

/* ---------------- 启动 ---------------- */
loadPrefs();
bind();
renderChrome();
api('/api/status').then((status) => {
  if (!status.has_paper) return;
  $('status').innerHTML = `上次打开过 <b>${esc(status.file.name)}</b>（${status.cache_hit ? '已命中缓存' : '未命中'}）<br>`
    + `再选一次同一个 PDF 即可接着上次读。缓存里已有 <b>${status.cache.papers}</b> 篇记录。`;
}).catch(() => { });
