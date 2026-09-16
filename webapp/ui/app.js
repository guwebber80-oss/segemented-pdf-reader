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
  dark: false,
  translations: new Map(),   // 原文 → 译文（会话内缓存；服务端还有一份磁盘缓存）
  busy: 0,
  pendingIndex: null,
};

/* ---------------- 小工具 ---------------- */
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* 解析设置：与 Streamlit 版同名字段（写进同一份缓存记录），存在本地供下次打开沿用 */
function readSettings() {
  return {
    merge_on: $('set-merge').checked,
    dehyphenate_on: $('set-dehyphenate').checked,
    show_all: $('set-showall').checked,
    // 小标题补丁：用 GROBID 抓的行内小标题拆卡片段落（候选来自记录里的 grobid_heads）
    runin_patch: $('set-runin').checked,
    // 图区域截图（阶段 4.6）：矢量图页整块截图、图内小字移出正文（默认关）
    figure_region: $('set-figure').checked,
    target_words: Number($('set-words').value || 200),
    table_mode_label: document.querySelector('#table-seg button.on')?.dataset.table || '截图（推荐）',
  };
}

function applySettings(settings) {
  const s = settings || {};
  $('set-merge').checked = s.merge_on !== false;
  $('set-dehyphenate').checked = s.dehyphenate_on !== false;
  $('set-showall').checked = !!s.show_all;
  $('set-runin').checked = !!s.runin_patch;
  $('set-figure').checked = !!s.figure_region;
  $('set-words').value = s.target_words || 200;
  $('set-words-label').textContent = $('set-words').value;
  const label = s.table_mode_label || '截图（推荐）';
  document.querySelectorAll('#table-seg button').forEach((b) =>
    b.classList.toggle('on', b.dataset.table === label));
}

function settingsQuery() {
  const s = readSettings();
  return '?merge=' + (s.merge_on ? 1 : 0) + '&dehyphenate=' + (s.dehyphenate_on ? 1 : 0)
    + '&words=' + s.target_words + '&table=' + encodeURIComponent(s.table_mode_label)
    + '&show_all=' + (s.show_all ? 1 : 0) + '&runin=' + (s.runin_patch ? 1 : 0)
    + '&fig=' + (s.figure_region ? 1 : 0);
}

function savePrefs() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(
      { lang: state.lang, height: state.height, immersive: state.immersive, dark: state.dark }));
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
    state.dark = !!data.dark;
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
    const data = await api('/api/open' + settingsQuery(), {
      method: 'POST',
      headers: { 'X-Filename': encodeURIComponent(file.name) },
      body: file,                       // 直接传字节，不需要 multipart
    });
    if (!data.ok) { alert('解析失败：' + (data.error || '未知原因')); return; }
    state.paper = data;
    state.translations.clear();
    state.i = Math.min(data.position || 0, data.cards.length - 1);
    applySettings(data.settings);      // 记录里存着的设置（与 Streamlit 版共用）
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
  const label = { figure: '', table: '表格区域 · ', formula: '公式区域 · ',
    figure_region: '图区域 · ' }[seg.kind] || '区域 · ';
  const caption = (seg.kind === 'figure' ? (seg.meta || '') : label + (seg.meta || ''));
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
  $('btn-theme').textContent = state.dark ? '☀️ 亮色' : '🌙 暗色';
  $('btn-theme').classList.toggle('on', state.dark);
  // 主题与高度都只是 body 上的一个 class（自建前端的好处：样式全在自己手里）
  document.body.className = 'h-' + state.height
    + (state.immersive ? ' immersive' : '') + (state.dark ? ' dark' : '');
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
  // 十个字段都是**可编辑**的：改完失焦即保存（写到本地记录里，两个前端共用）
  const short = ['doi', 'first_author', 'corresponding', 'corresponding_email'];
  const fields = ['title', 'abstract'].concat(order);
  $('meta-form').innerHTML = fields.map((key) => {
    const field = meta[key] || { label: key, value: '', auto: '', source: '', found: false, note: '' };
    const rows = (key === 'abstract' || key === 'authors' || key === 'affiliations'
      || key === 'supplementary') ? 4 : (short.includes(key) ? 1 : 2);
    const tag = rows > 1
      ? `<textarea rows="${rows}" data-key="${esc(field.store_key || key)}">${esc(field.value || '')}</textarea>`
      : `<input type="text" data-key="${esc(field.store_key || key)}" value="${esc(field.value || '')}">`;
    const flag = field.found ? '' : ' ⚠️ 未找到';
    const edited = field.edited ? '<span class="edited">✎ 已人工修正</span>' : '';
    const note = field.note ? ` · ${esc(field.note)}` : '';
    return `<div class="field"><label>${esc(field.label)}${flag} ${edited}</label>${tag}`
      + `<span class="src">来源：${esc(field.source || '—')}${note}</span></div>`;
  }).join('');
  const ov = paper.overview;
  $('overview').innerHTML = [
    ['页数', ov.pages], ['阅读卡片', ov.cards], ['正文词数', ov.words.toLocaleString()],
    ['插图', ov.images + ' 张'], ['公式区域', ov.formula_regions], ['表格区域', ov.table_regions],
    ['正文字号', ov.body_size + ' pt'], ['解析耗时', ov.elapsed + ' 秒'],
  ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join('');
  $('status').innerHTML = `✅ ${esc(paper.file.name)}<br>`
    + `文本 ${ov.pages} 页 / ${ov.cards} 张卡 · 插图 ${ov.images} 张<br>`
    + `${paper.cache_hit ? '♻️ 已命中本地记录，接着上次读' : '🆕 已写入本地缓存'}`
    + `${paper.backend ? ' · 翻译后端 ' + esc(paper.backend) : ' · ⚠️ 没有可用翻译后端'}`;
  renderCache(paper.cache);
  refreshCoverage();
  renderDiagnostics();
}

function renderCache(cache) {
  if (!cache) return;
  $('cache-info').innerHTML = `缓存目录：<code>${esc(cache.root || '')}</code><br>`
    + `已保存 <b>${cache.papers}</b> 篇文献记录 · 译文 <b>${cache.translations}</b> 条 · `
    + `占用 <b>${cache.kb}</b> KB<br>`
    + `图片不落盘：插图与区域截图按需渲染。`;
}

/* 译文覆盖度：告诉用户"断网前还差多少"，并给一个一次补齐的入口 */
async function refreshCoverage() {
  const button = $('btn-translate-all');
  if (!state.paper) {
    $('coverage').textContent = '未打开文献。';
    button.textContent = '🌐 请先选择一篇文献';
    button.disabled = true;
    button.title = '选择 PDF 之后这里可以一次性把整篇补齐';
    return;
  }
  try {
    const status = await api('/api/status');
    const cov = status.coverage || {};
    const pending = status.pending_count || 0;
    const total = cov.cards || 0;
    const covered = cov.covered_cards || 0;
    $('coverage').innerHTML = `已能离线读中文：<b>${covered} / ${total}</b> 张卡 · `
      + `段落 <b>${cov.covered_texts || 0} / ${cov.texts || 0}</b> 段`
      + (pending ? `<br>还有 <b>${pending}</b> 段没有译文（没翻到的卡片不会自动翻译）。`
                 : '<br>整篇都有译文了：断网也能看中文。');
    // ⚠️ 这里**不能因为"待译 0 段"就把按钮禁用**：用户看到的是"点不动"，会以为坏了。
    // 只有"没有文献 / 没有后端"才禁用（并且文案说明原因）；"已译好"时保持可点，点了就复查一次。
    if (!status.backend) {
      button.textContent = '⚠️ 没有可用的翻译后端';
      button.disabled = true;
      button.title = '请在项目根目录的 .env 里配置翻译 API Key，然后重启服务';
      $('coverage-note').textContent = '⚠️ 没有可用的翻译后端（请在 .env 里配置 Key）。';
      return;
    }
    button.textContent = pending
      ? `🌐 翻译整篇（补齐 ${pending} 段）`
      : '✅ 整篇都已译好（点一次复查）';
    button.disabled = false;
    button.title = pending
      ? `把剩下 ${pending} 段一次翻完并存入本地缓存；已译过的段落不会重复请求`
      : '已经整篇都有译文；点一下可以重新核对覆盖度';
    $('coverage-note').textContent = pending
      ? '平时是「读到哪翻到哪」：没翻到的卡片不会自动翻译。断网前点一次「翻译整篇」就能把整篇补齐。'
      : '整篇都有译文了：断网也能看中文。';
  } catch (e) {
    // 状态拿不到时也**不禁用**，让用户还能点一下试试（点了会给出真实错误）
    button.disabled = false;
    $('coverage-note').textContent = '拿不到覆盖度（状态接口异常）：仍可点按钮试一次。';
  }
}

/* 翻译整篇：取待译清单 → 分批调用 /api/translate → 进度条推进；已译段落不会重复请求 */
async function translateAll() {
  const button = $('btn-translate-all');
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '⏳ 正在处理…';
  try {
    const pending = await api('/api/pending');
    if (!pending.ok) { alert(pending.error || '没有可用的翻译后端'); return; }
    const texts = pending.texts || [];
    if (!texts.length) {
      // 已经全部有译文：明确告诉用户"不用翻"，而不是让按钮灰着不出声
      $('coverage-note').textContent = '✅ 整篇都已经有译文了，无需翻译（这次检查没有发现缺段）。';
      return;
    }
    const chunk = 20;
    let done = 0;
    for (let start = 0; start < texts.length; start += chunk) {
      const batch = texts.slice(start, start + chunk);
      const result = await api('/api/translate', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ texts: batch, target: 'zh' }),
      });
      if (!result.ok) { alert('翻译中断：' + (result.error || '未知原因')); break; }
      batch.forEach((text, k) => { if (result.translations[k]) state.translations.set(text, result.translations[k]); });
      done += batch.length;
      $('cov-bar').style.width = Math.round((done / texts.length) * 100) + '%';
      $('coverage-note').textContent = `正在翻译整篇… ${done} / ${texts.length} 段`;
    }
    $('coverage-note').textContent = `整篇翻译完成：本次处理 ${done} 段（已存到本地缓存）。`;
    if (state.lang === 'zh') renderCard(false);
    await refreshCoverage();
  } catch (err) {
    alert('翻译整篇失败：' + err.message);
    button.textContent = original;
  } finally {
    button.disabled = false;
    setTimeout(() => { $('cov-bar').style.width = '0%'; }, 1200);
  }
}

/* 元数据保存：把十个输入框的值一起发给服务端（服务端会过滤掉与自动提取相同的项） */
async function saveMetadata() {
  if (!state.paper) return;
  const edits = {};
  document.querySelectorAll('#meta-form [data-key]').forEach((el) => {
    edits[el.dataset.key] = el.value;
  });
  try {
    const result = await api('/api/metadata', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ edits }),
    });
    if (result.metadata) state.paper.metadata = result.metadata;
    $('meta-saved').textContent = '已保存到本地缓存 ✓';
    $('meta-title').textContent = result.metadata.title.value || '（未识别到标题）';
  } catch (err) {
    $('meta-saved').textContent = '保存失败：' + err.message;
  }
}

async function resetMetadata() {
  if (!state.paper) return;
  try {
    const result = await api('/api/metadata/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    if (result.metadata) state.paper.metadata = result.metadata;
    renderMeta();
    $('meta-saved').textContent = '已还原为自动提取的结果';
  } catch (err) {
    $('meta-saved').textContent = '还原失败：' + err.message;
  }
}

/* GROBID 交叉校验：并排对照 + 逐字段一键采信（列表只补空不覆盖） */
async function grobidStatus() {
  // 探测是"当下这一刻"的事实，所以：① 启动时探一次；② 提供「↻ 重新探测」（刚拉起 GROBID 时用）；
  // ③ 运行校验前再探一次（避免因为页面早先加载而显示"未检测到"）
  try {
    const status = await api('/api/grobid/status');
    $('grobid-status').innerHTML = status.alive
      ? `✅ 服务正常（${esc(status.url)}）`
      : `⚠️ ${esc(status.message)}<br>不影响阅读——上面字段仍是本地规则的结果。`
        + `启动方式：<code>启动网页版.bat</code> 会自动拉起；手动则 <code>docker run --rm --init `
        + `--ulimit core=0 -p 8070:8070 grobid/grobid:0.9.1-crf</code>`
        + `<br>刚启动的话点「↻ 重新探测」（服务要十几秒才就绪）。`
        // 最常见的坑：容器确实在跑，但启动时漏了 -p 8070:8070，PORTS 一栏是空的 → 外部永远连不上
        + `<br>若 <code>docker ps</code> 里能看到 grobid 容器但 <b>PORTS 一栏是空的</b>，`
        + `说明启动时漏了 <code>-p 8070:8070</code>，容器再正常也连不上——删掉它重跑上面的命令即可。`;
  } catch (e) { $('grobid-status').textContent = '探测 GROBID 失败：' + e.message; }
}

async function runGrobid() {
  const button = $('btn-grobid');
  button.disabled = true;
  button.textContent = '⏳ 正在调用 GROBID（首次约 5~10 秒）…';
  await grobidStatus();                       // 先刷新服务状态，避免用早先的探测结果误判
  try {
    const result = await api('/api/grobid', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!result.ok) {
      $('grobid-status').innerHTML = '⚠️ ' + esc(result.error || '调用失败');
      $('grobid-rows').innerHTML = '';
      return;
    }
    // 小标题补丁：GROBID 顺手抓的那批「行内小标题候选」也一并报出来，
    // 让用户知道下一步该做什么（勾左栏开关 + 重新解析），而不是猜为什么没变化
    const count = (result.runin_heads || []).length;
    const runin = count
      ? `<br>📑 已记录 ${count} 条 GROBID 小标题候选；勾选左栏「用小标题补丁」并重新解析即可生效。`
      : '<br>📑 本次没有可用的 GROBID 小标题候选（图注/表注与整句正文都已被过滤掉）。';
    $('grobid-status').innerHTML = '✅ ' + esc(result.summary || '已完成') + runin;
    renderGrobidRows(result.rows || []);
  } catch (err) {
    alert('GROBID 调用失败：' + err.message);
  } finally {
    button.disabled = false;
    button.textContent = '🔍 运行 GROBID 校验';
  }
}

function renderGrobidRows(rows) {
  if (!rows.length) { $('grobid-rows').innerHTML = '<div class="cache">没有可比字段。</div>'; return; }
  $('grobid-rows').innerHTML = rows.map((row, index) => {
    const action = row.actionable
      ? `<button class="hbtn take" data-key="${esc(row.store_key)}" data-list="${row.is_list ? 1 : 0}"
           data-value="${esc(row.raw)}">采信 GROBID</button>` : '';
    return `<div class="grow-row"><div class="grow-head"><b>${esc(row.field)}</b>`
      + `<span class="verdict">${esc(row.verdict)}</span></div>`
      + `<div class="grow-line"><span>本地</span>${esc(row.local) || '—'}</div>`
      + `<div class="grow-line"><span>GROBID</span>${esc(row.grobid) || '—'}</div>${action}</div>`;
  }).join('');
}

async function takeGrobid(button) {
  button.disabled = true;
  try {
    const result = await api('/api/grobid/apply', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ store_key: button.dataset.key, value: button.dataset.value,
                             is_list: button.dataset.list === '1' }),
    });
    if (!result.ok) { alert(result.error || '采信失败'); return; }
    if (result.metadata) state.paper.metadata = result.metadata;
    renderMeta();
    button.textContent = '✅ ' + (result.note || '已采信');
  } catch (err) {
    alert('采信失败：' + err.message);
    button.disabled = false;
  }
}

/* 解析诊断：把"解析过程发生了什么"如实列出来，便于核对"有没有误杀/漏检" */
function figureDiagLine(d) {
  // 图区域（阶段 4.6）：开关关着时没有探测数字，如实说明；开着时给出定位/失败条数
  const stats = d.figure_stats || {};
  if (!stats.caption_page_count && !(d.figure_regions || []).length) {
    return (state.paper && state.paper.settings && state.paper.settings.figure_region)
      ? '本页无图注（未探测到矢量图页）' : '未启用（左栏「解析设置」可打开）';
  }
  const failed = (d.figure_failures || []).length;
  return `本篇 ${stats.caption_pages_no_bitmap_count || 0} 个图注页无面板级位图 · `
    + `过闸门 ${stats.gate_page_count || 0} 页 · 定位成功 ${(d.figure_regions || []).length} 个`
    + (failed ? ` · 定位失败 ${failed} 个` : '');
}

async function renderDiagnostics() {
  if (!state.paper) { $('diag-body').innerHTML = ''; return; }
  try {
    const data = await api('/api/diagnostics');
    const d = data.diagnostics || {};
    if (!d.cards && !d.pages) { $('diag-body').innerHTML = ''; return; }
    const kv = [
      ['原子块 / 段落', `${d.blocks} / ${d.paragraphs}`],
      ['阅读卡片', d.cards], ['全文提取字符', d.total_chars],
      ['正文字号基准', d.body_size + ' pt'], ['解析耗时', d.elapsed + ' 秒'],
      ['公式区域 / 表格区域', `${(d.formula_regions || []).length} / ${(d.table_regions || []).length}`],
      ['插图 / 跳过', `${(d.images || []).length} / ${(d.skipped_images || []).length}`],
      ['图区域（矢量图页）', figureDiagLine(d)],
    ].map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join('');
    const pages = (d.pages || []).map((p) => `第${p.page}页 ${p.layout}`).join(' · ');
    const regions = (d.formula_regions || []).slice(0, 12).map((r) =>
      `<li>第 ${r.page} 页 · ${r.members} 块 · ${esc(r.preview)}</li>`).join('');
    const tables = (d.table_regions || []).slice(0, 8).map((t) =>
      `<li>第 ${t.page} 页 · ${t.rows}×${t.cols} 列 · ${esc(t.caption || '')}</li>`).join('');
    const imgs = (d.images || []).slice(0, 10).map((m) =>
      `<li>第 ${m.page} 页 · ${esc(m.pixels)} px · ${esc(m.display)} · `
      + `${m.card ? '关联卡片 #' + esc(m.card) : '未关联卡片'}</li>`).join('');
    const figs = (d.figure_regions || []).slice(0, 8).map((r) =>
      `<li>第 ${r.page} 页 · ${r.members} 块图内小字 · ${esc(r.source || '')} · ${esc(r.caption || '')}</li>`)
      .join('');
    const figFails = (d.figure_failures || []).slice(0, 8).map((f) =>
      `<li>第 ${f.page} 页 · ${esc(f.reason || '')} · ${esc(f.caption || '')}（已放弃截图）</li>`).join('');
    $('diag-body').innerHTML = kv
      + `<div class="cache" style="margin-top:6px">排版：${esc(pages)}</div>`
      + (regions ? `<div class="cache"><b>公式区域</b><ul class="diag">${regions}</ul></div>` : '')
      + (tables ? `<div class="cache"><b>表格区域</b><ul class="diag">${tables}</ul></div>` : '')
      + (figs ? `<div class="cache"><b>图区域</b><ul class="diag">${figs}</ul></div>` : '')
      + (figFails ? `<div class="cache"><b>图区域定位失败</b><ul class="diag">${figFails}</ul></div>` : '')
      + (imgs ? `<div class="cache"><b>插图</b><ul class="diag">${imgs}</ul></div>` : '');
    $('diag-note').textContent = '这些数字来自本次解析；用它核对"有没有误杀真正文 / 漏掉公式表格"。';
  } catch (e) { /* 诊断拿不到不影响阅读 */ }
}

/* 用当前设置重新解析（不用重新上传）：设置会随记录一起存下来，两个前端一致 */
async function reparse() {
  if (!state.paper) { alert('请先选择一篇文献'); return; }
  const settings = readSettings();
  busy(true, '正在重新解析…', '按新的解析设置重算卡片、公式与表格区域');
  try {
    const data = await api('/api/reparse', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    });
    if (!data.ok) { alert('重新解析失败：' + (data.error || '未知原因')); return; }
    state.paper = data;
    state.translations.clear();
    state.i = Math.min(data.position || 0, Math.max(0, data.cards.length - 1));
    applySettings(data.settings);
    renderAll();
    $('settings-note').textContent = `已按新设置重新解析：${data.overview.cards} 张卡片 · `
      + `公式区域 ${data.overview.formula_regions} 个 · 表格区域 ${data.overview.table_regions} 个`
      + (settings.show_all ? '（显示全部内容：核对模式，保持原文不翻译）' : '')
      // 补丁的候选存在记录里、由「运行 GROBID 校验」写入，所以在没跑过校验前勾它不会有变化
      + (settings.runin_patch
        ? '（已启用小标题补丁：候选来自上一次「运行 GROBID 校验」，没跑过就没有候选）' : '')
      + (settings.figure_region
        ? `（已启用图区域截图：${data.overview.figure_regions || 0} 个图区域，图内小字已移出正文）` : '');
  } catch (err) {
    alert('重新解析失败：' + err.message);
  } finally {
    busy(false);
  }
}

async function clearCache(scope) {
  const message = scope === 'all'
    ? '清空全部缓存：所有文献记录与全部译文都会被删除（下次阅读会重新解析、重新翻译）。继续？'
    : '只清除这篇文献的本地记录（元数据修正与阅读位置）；译文是全局的，不受影响。继续？';
  if (!confirm(message)) return;
  try {
    const result = await api('/api/cache/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope }),
    });
    if (scope === 'all') {
      state.translations.clear();
      state.paper.cache_hit = false;
      renderCard(false);
    } else {
      state.paper.cache_hit = false;
      state.paper.metadata = Object.fromEntries(Object.entries(state.paper.metadata)
        .map(([k, v]) => [k, v.value === undefined ? v : { ...v, value: v.auto, edited: false }]));
      renderMeta();
    }
    if (result.cache) renderCache({ ...state.paper.cache, ...result.cache });
    $('meta-saved').textContent = scope === 'all' ? '已清空全部缓存' : '已清除本篇记录';
  } catch (err) {
    alert('清除失败：' + err.message);
  }
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

function setDark(on) {
  state.dark = on;
  savePrefs();
  renderChrome();
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
  $('btn-theme').addEventListener('click', () => setDark(!state.dark));
  $('btn-translate-all').addEventListener('click', translateAll);
  $('btn-meta-reset').addEventListener('click', resetMetadata);
  $('btn-clear-one').addEventListener('click', () => clearCache('paper'));
  $('btn-clear-all').addEventListener('click', () => clearCache('all'));
  $('btn-reparse').addEventListener('click', reparse);
  $('btn-grobid').addEventListener('click', runGrobid);
  $('btn-grobid-probe').addEventListener('click', grobidStatus);
  $('grobid-rows').addEventListener('click', (e) => {
    const btn = e.target.closest('button.take'); if (btn) takeGrobid(btn);
  });
  $('set-words').addEventListener('input', (e) => { $('set-words-label').textContent = e.target.value; });
  $('table-seg').addEventListener('click', (e) => {
    const btn = e.target.closest('button'); if (!btn) return;
    document.querySelectorAll('#table-seg button').forEach((b) => b.classList.remove('on'));
    btn.classList.add('on');
  });

  // 元数据输入框：失焦即保存（含文本域），Ctrl+Enter 也算
  $('meta-form').addEventListener('change', (e) => {
    if (e.target && e.target.dataset && e.target.dataset.key) saveMetadata();
  });
  $('meta-form').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) saveMetadata();
  });

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
grobidStatus();
api('/api/status').then((status) => {
  if (!status.has_paper) return;
  $('status').innerHTML = `上次打开过 <b>${esc(status.file.name)}</b>（${status.cache_hit ? '已命中缓存' : '未命中'}）<br>`
    + `再选一次同一个 PDF 即可接着上次读。缓存里已有 <b>${status.cache.papers}</b> 篇记录。`;
}).catch(() => { });
