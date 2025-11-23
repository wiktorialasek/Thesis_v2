// // ===== API helpers =====
// async function apiList(params){
//   const url = '/api/tweets?' + new URLSearchParams(params).toString();
//   const r = await fetch(url);
//   if(!r.ok) throw new Error('tweets api');
//   return r.json();
// }
// async function apiTweet(id){
//   const r = await fetch('/api/tweet/' + encodeURIComponent(id));
//   if(!r.ok) throw new Error('tweet api');
//   return r.json();
// }
// async function apiPrice(startUnix, minutes, pre, grid){
//   const r = await fetch('/api/price?' + new URLSearchParams({
//     start:String(startUnix), minutes:String(minutes), pre:String(pre||0),
//     grid: grid ? '1' : '0'
//   }));
//   if(!r.ok) throw new Error('price api');
//   return r.json();
// }
// function toLocal(tsSec){ return new Date(tsSec * 1000); }

// // ===== UI state =====
// const state = {
//   page: 1, per_page: 20,
//   year: 'all', reply: 0, retweet: 0, quote: 0, q: '',
//   total: 0, years: [],

//   windowMinutes: 15,
//   currentTweetId: null,
//   preMinutes: 10,

//   // Etykietowanie
//   useImp: 0,           // 0 = precompute; 1 = licz w locie
//   impMin: 8,
//   impThr: 1.0,

//   selected: new Set(),
//   label: 'all',
//   sortByChange: false
// };
// const DEFAULT_PER_PAGE = 20; // [NEW]
// let __reqEpoch = 0;
// let io;
// let isLoading = false;

// // [NEW] wypełnianie KPI w nagłówku
// function renderTopKpis(st){
//   const fmt = v => (v==null || !isFinite(v)) ? '—' : (Number(v).toFixed(2)+'%');
//   const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };

//   if(!st){ set('k-n','—'); set('k-up','—'); set('k-down','—'); set('k-neutral','—'); set('k-min','—'); set('k-med','—'); set('k-avg','—'); set('k-max','—'); return; }

//   set('k-n', String(st.n ?? '—'));
//   set('k-up', String(st.n_up ?? '—'));
//   set('k-down', String(st.n_down ?? '—'));
//   set('k-neutral', String(st.n_neutral ?? '—'));
//   set('k-min', fmt(st.pct_min));
//   set('k-med', fmt(st.pct_median));
//   set('k-avg', fmt(st.pct_mean));
//   set('k-max', fmt(st.pct_max));

//   const mode = document.getElementById('k-mode');
//   if (mode) {
//     mode.textContent = (st.mode === 'imp')
//       ? `m=${st.minute}${(st.threshold!=null && st.threshold!=='')?`, próg=${st.threshold}%`:''}`
//       : `pre m=${st.minute}, próg=${st.threshold}%`;
//   }
//   const filt = document.getElementById('k-filter');
//   if (filt) {
//     const f = st.label_filter || 'all';
//     filt.textContent = `filtr: ${f}`;
//     filt.classList.remove('up','down','neutral');
//     filt.classList.add((f==='up')?'up':(f==='down')?'down':'neutral');
//   }
// }

// // ===== RENDER: list =====
// async function loadFiltersAndList({reset=false, initial=false} = {}){
//   if (isLoading) return;
//   isLoading = true;
//   const myEpoch = ++__reqEpoch;

//   if (reset) {
//     if (io) io.disconnect();
//     state.page = 1;
//     const list = document.getElementById('list');
//     if (list) list.innerHTML = '';
//     const s = document.getElementById('sentinel');
//     if (s) { s.textContent = 'Loading…'; s.style.display = ''; }
//   }

//   const params = {
//     page: state.page, per_page: state.per_page,
//     year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,
//     label: state.label
//   };

//   // [NEW] sort globalny – backend
//   if (state.sortByChange && (state.label === 'up' || state.label === 'down')) {
//     params.imp_sort = 1;
//     params.year = 'all';
//     params.per_page = Math.max(state.per_page, 300);
//     state.per_page = params.per_page;
//     state.useImp = 1;
//     params.imp_filter = 1;
//     params.imp_min = state.impMin;
//     params.imp_thr = state.impThr;
//   }

//   if (state.useImp === 1) {
//     params.imp_filter = 1;
//     params.imp_min = state.impMin;
//     params.imp_thr = state.impThr;
//   }

//   const data = await apiList(params);
//   if (myEpoch !== __reqEpoch) { isLoading = false; return; }

//   // [NEW] KPI w nagłówku
//   renderTopKpis(data.stats);

//   // years select (raz)
//   if(!state.years.length && Array.isArray(data.years)){
//     state.years = data.years;
//     const sel = document.getElementById('f-year');
//     state.years.forEach(y=>{
//       const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
//     });
//   }

//   state.total = data.total || 0;

//   const list = document.getElementById('list');
//   const frag = document.createDocumentFragment();
//   const itemsForRender = (data.items || []).slice();  // [NEW] bez dodatkowego filtrowania w kliencie!

//   if(itemsForRender.length === 0){
//     const empty = document.createElement('div');
//     empty.className = 'row';
//     empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
//     list.appendChild(empty);
//   } else {
//     itemsForRender.forEach(item=>{
//       const row = document.createElement('div');
//       row.className = 'row';
//       row.dataset.id = item.tweet_id;

//       // [NEW] własny tooltip zamiast napisu m=…, Δ=…
//       const minute = (item.imp_min != null ? item.imp_min : (item.pre_min ?? item.lab_min));
//       const pct    = (item.imp_pct != null ? item.imp_pct : (item.pre_pct ?? item.lab_pct));
//       const metaText = (minute != null) ? `m=${minute}, Δ=${pct == null ? '—' : (Number(pct).toFixed(2)+'%')}` : '';
//       if (metaText) row.title = metaText;

//       const label  = (item.imp_label != null ? item.imp_label : (item.pre_label ?? item.lab_label));
//       let pill = '';
//       if (label === 'up')      pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
//       else if (label === 'down') pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
//       else                      pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';

//       const rankBadge = (item.rank ? `<span class="pill" title="pozycja po sortowaniu">#${item.rank}</span>` : '');
//       const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

//       row.innerHTML = `
//         <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
//           <div style="flex:1;min-width:0">
//             <h4>Tweet #${item.tweet_id}</h4>
//             <p>${escapeHtml(item.text || '')}</p>
//             <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//             <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//               ${pill}
//               ${rankBadge}
//             </div>
//           </div>
//           <label class="check" style="white-space:nowrap">
//             <input type="checkbox" class="pick" ${checked} /> wybierz
//           </label>
//         </div>
//       `;

//       row.querySelector('.pick').addEventListener('change', (e)=>{
//         if(e.target.checked) state.selected.add(item.tweet_id);
//         else state.selected.delete(item.tweet_id);
//         renderOverlay();
//       });

//       row.addEventListener('click', (ev)=>{
//         if(ev.target.classList.contains('pick')) return;
//         openDetail(item.tweet_id);
//       });

//       frag.appendChild(row);
//     });
//     list.appendChild(frag);
//   }

//   const s = document.getElementById('sentinel');
//   if (s) {
//     const loaded = document.querySelectorAll('#list .row').length;
//     if (state.total === 0) {
//       s.textContent = '— brak danych —';
//     } else {
//       s.textContent = (loaded >= state.total) ? '— koniec —' : 'Loading…';
//     }
//   }

//   if (reset) setupInfiniteScroll();
//   isLoading = false;
// }

// // ===== DETAIL + CHART =====
// async function openDetail(tweetId){
//   const detail = document.getElementById('detail');
//   const minuteList = document.getElementById('minute-list');
//   detail.innerHTML = '<div class="muted">Ładowanie…</div>';
//   Plotly.purge('chart'); minuteList.textContent = '—';

//   try{
//     const t = await apiTweet(tweetId);
//     detail.innerHTML = `
//       <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
//         <div>
//           <div class="muted" style="font-size:12px">${t.created_display}</div>
//           <div style="margin-top:4px">${escapeHtml(t.text || '')}</div>
//           <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//             ${t.isReply ? '<span class="pill">reply</span>' : ''}
//             ${t.isRetweet ? '<span class="pill">retweet</span>' : ''}
//             ${t.isQuote ? '<span class="pill">quote</span>' : ''}
//           </div>
//         </div>
//         <div class="muted">Tweet #${t.tweet_id}</div>
//       </div>
//     `;
//     state.currentTweetId = tweetId;

//     const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
//     renderPctList(payload.pct_changes);
//   } catch(e) {
//     console.error(e);
//     detail.innerHTML = '<div class="muted">Błąd wczytywania tweeta.</div>';
//     Plotly.newPlot('chart', [{x:[new Date()], y:[null]}], {title:'Błąd ładowania danych', margin:{t:40}});
//   }
// }

// async function renderChart(startUnix, minutes, pre){
//   const payload = await apiPrice(startUnix, minutes, pre);
//   const pts = payload.points || [];
//   const reason = payload.reason || 'ok';

//   const xStart = payload.x_start ? new Date(payload.x_start * 1000) : toLocal(startUnix - (pre||0)*60);
//   const xEnd   = payload.x_end   ? new Date(payload.x_end   * 1000) : toLocal(startUnix + minutes*60);
//   const tweetX = toLocal(startUnix); tweetX.setSeconds(0, 0);

//   if(!pts.length){
//     Plotly.newPlot('chart', [{
//       x:[tweetX], y:[null], mode:'lines', name:'brak danych'
//     }], {
//       title: (reason==='no_data' ? 'Brak danych w tym oknie' : ''),
//       margin:{t:30},
//       xaxis:{range:[xStart, xEnd], title:'Czas (lokalny)'},
//       yaxis:{title:'Cena'},
//       shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
//     }, {responsive:true});
//     return payload;
//   }

//   const x = pts.map(p=>toLocal(p.t));
//   const ohlcTrace = {
//     type:'candlestick', x,
//     open:pts.map(p=>p.open),
//     high:pts.map(p=>p.high),
//     low: pts.map(p=>p.low),
//     close:pts.map(p=>p.close),
//     name:'OHLC'
//   };
//   const lineTrace = { x, y: pts.map(p=>p.open), mode:'lines', name:'Open' };

//   Plotly.newPlot('chart', [ohlcTrace, lineTrace], {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
//     yaxis:{title:'Cena'},
//     showlegend:false,
//     shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
//   }, {responsive:true});
//   return payload;
// }

// // ===== OVERLAY =====
// async function renderOverlay(){
//   if(!state.selected.size){
//     Plotly.purge('overlay'); return;
//   }
//   const traces = [];
//   const mins = state.windowMinutes;
//   const pre  = state.preMinutes;

//   for (const id of state.selected){
//     try{
//       const t = await apiTweet(id);
//       const payload = await apiPrice(t.created_ts, mins, pre, /*grid=*/true);
//       const g = payload.grid || {};
//       const minutesTs = g.minute_ts || [];
//       const closes = g.close || [];
//       let baseTs = g.tweet_minute_ts;

//       let baseIdx = minutesTs.indexOf(baseTs);
//       let base = null;
//       if (baseIdx < 0) baseIdx = 0;
//       if (baseIdx >= 0) base = closes[baseIdx];
//       if (base == null) {
//         for (let i = Math.max(0, baseIdx); i < closes.length; i++){
//           if (closes[i] != null){
//             base = closes[i];
//             baseTs = minutesTs[i];
//             baseIdx = i;
//             break;
//           }
//         }
//       }
//       if (base == null) continue;

//       const xs = [], ys = [];
//       for (let i=0;i<minutesTs.length;i++){
//         const v = closes[i];
//         if (v == null) continue;
//         const offsetMin = (minutesTs[i] - baseTs)/60;
//         xs.push(offsetMin);
//         ys.push((v/base - 1)*100);
//       }

//       const xFilt = [], yFilt = [];
//       const left = -pre, right = mins;
//       for (let i=0;i<xs.length;i++){
//         if (xs[i] >= left && xs[i] <= right){
//           xFilt.push(xs[i]); yFilt.push(ys[i]);
//         }
//       }
//       if (xFilt.length) traces.push({ x: xFilt, y: yFilt, mode:'lines', name: `#${id}`, line:{width:1} });
//     }catch(_){}
//   }

//   if(!traces.length){ Plotly.purge('overlay'); return; }

//   Plotly.newPlot('overlay', traces, {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'minuty względem tweeta'},
//     yaxis:{title:'% zmiany względem minuty tweeta'},
//     showlegend:true
//   }, {responsive:true});
// }

// function renderPctList(pct){
//   const minuteList = document.getElementById('minute-list');
//   if(!pct){ minuteList.textContent = 'Brak danych.'; return; }
//   const order = [1,2,3,4,5,6,7,8,9,10,15,30,60];
//   const lines = order.map(m=>{
//     const v = pct[m];
//     return (v === null || v === undefined)
//       ? `+${m} min: — brak danych`
//       : `+${m} min: ${v.toFixed(2)}%`;
//   });
//   minuteList.textContent = lines.join('\n');
// }

// // ===== utils =====
// function escapeHtml(s){ return (s||'').replace(/[&<>"']/g,m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m])); }

// function setupInfiniteScroll(){
//   const sentinel = document.getElementById('sentinel');
//   const rootEl = document.querySelector('.layout > .pane:first-child');
//   if (!sentinel) return;

//   if (io) io.disconnect();
//   io = new IntersectionObserver(async entries => {
//     const e = entries[0];
//     if (!e.isIntersecting) return;
//     if (isLoading) return;

//     const loaded = document.querySelectorAll('#list .row').length;
//     if (loaded >= state.total) {
//       if (sentinel) {
//         sentinel.textContent = (state.total === 0) ? '— brak danych —' : '— koniec —';
//       }
//       return;
//     }

//     state.page += 1;
//     try {
//       await loadFiltersAndList({reset:false, initial:false});
//     } catch (err) {
//       console.error(err);
//     }
//   }, {
//     root: rootEl || null,
//     rootMargin: '200px',
//     threshold: 0.01
//   });

//   io.observe(sentinel);
// }

// // ===== wiring =====
// window.addEventListener('DOMContentLoaded', ()=>{
//   const readBasics = ()=>{
//     state.year    = document.getElementById('f-year').value || 'all';
//     state.q       = (document.getElementById('f-q').value || '').trim();
//     state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//     state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//     state.quote   = document.getElementById('f-quote').checked ? -1 : 0;
//     const labSel = document.getElementById('f-label');
//     if (labSel) state.label = labSel.value || 'all';
//   };

//   document.getElementById('btn-search').addEventListener('click', ()=>{
//     readBasics();
//     state.page = 1;
//     if (state.sortByChange) {
//       state.sortByChange = false;
//       const btnSort = document.getElementById('btn-sort');
//       if (btnSort) btnSort.classList.remove('primary');
//       state.per_page = DEFAULT_PER_PAGE; // [NEW]
//     }
//     loadFiltersAndList({reset:true});
//   });

//   document.getElementById('f-q').addEventListener('keydown', (e)=>{ if(e.key==='Enter') document.getElementById('btn-search').click(); });

//   document.getElementById('btn-label-apply').addEventListener('click', ()=>{
//     state.impMin = parseInt(document.getElementById('lab-min').value || '8', 10);
//     const thrRaw = document.getElementById('lab-thr').value || '1';
//     state.impThr = parseFloat(String(thrRaw).replace(',', '.'));
//     state.useImp = 1;
//     state.page = 1;
//     loadFiltersAndList({reset:true});
//   });

//   document.getElementById('btn-clear-overlay')?.addEventListener('click', ()=>{
//     state.selected.clear();
//     Plotly.purge('overlay');
//     document.querySelectorAll('#list .row .pick').forEach(ch => ch.checked = false);
//   });

//   const selWin  = document.getElementById('win-min');
//   const btnWin  = document.getElementById('win-apply');
//   const preCk   = document.getElementById('pre-10');

//   if (selWin) selWin.value = String(state.windowMinutes || 15);
//   if (preCk)  preCk.checked = !!state.preMinutes;

//   if (selWin && btnWin) {
//     btnWin.addEventListener('click', async ()=>{
//       const v = parseInt(selWin.value || '15', 10);
//       state.windowMinutes = (isNaN(v) ? 15 : v);
//       state.preMinutes = (preCk && preCk.checked) ? 10 : 0;

//       if (state.currentTweetId) {
//         try {
//           const t = await apiTweet(state.currentTweetId);
//           const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
//           renderPctList(payload.pct_changes);
//         } catch (e) { console.error(e); }
//       }
//       renderOverlay();
//     });
//   }
//   if (preCk) {
//     preCk.addEventListener('change', async ()=>{
//       state.preMinutes = preCk.checked ? 10 : 0;
//       if (state.currentTweetId) {
//         try {
//           const t = await apiTweet(state.currentTweetId);
//           const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
//           renderPctList(payload.pct_changes);
//         } catch (e) { console.error(e); }
//       }
//       renderOverlay();
//     });
//   }

//   // checkboksy etykiet
//   const ckUp  = document.getElementById('f-up');
//   const ckDn  = document.getElementById('f-down');
//   const ckNe  = document.getElementById('f-neutral');
//   function applyLabelCheckboxes(){
//     const picks = [];
//     if (ckUp && ckUp.checked) picks.push('up');
//     if (ckDn && ckDn.checked) picks.push('down');
//     if (ckNe && ckNe.checked) picks.push('neutral');

//     state.label = (picks.length === 1 ? picks[0] : 'all');

//     // jeśli filtr up/down – licz po znaku (pusty próg)
//     if (state.label === 'up' || state.label === 'down') {
//       state.useImp = 1;
//       state.impThr = ''; // [NEW] => backend wejdzie w tryb "tylko znak"
//     }

//     state.page = 1;
//     loadFiltersAndList({reset: true});
//   }
//   [ckUp, ckDn, ckNe].forEach(el=>{ if (el) el.addEventListener('change', applyLabelCheckboxes); });

//   // sort biggest change
//   const btnSort = document.getElementById('btn-sort');
//   if (btnSort) {
//     btnSort.addEventListener('click', () => {
//       state.sortByChange = !state.sortByChange;
//       btnSort.classList.toggle('primary', state.sortByChange);

//       if (state.sortByChange) {
//         state.year = 'all';
//         const selYear = document.getElementById('f-year');
//         if (selYear) selYear.value = 'all';
//         state.per_page = Math.max(state.per_page, 300);
//         state.page = 1;
//       } else {
//         state.per_page = DEFAULT_PER_PAGE; // [NEW] powrót do normalnej paginacji
//         state.page = 1;
//       }
//       loadFiltersAndList({reset:true});
//     });
//   }

//   // start
//   loadFiltersAndList({reset:true, initial:true});
//   setupInfiniteScroll();
// });

// ===== API helpers =====
async function apiList(params){
  const url = '/api/tweets?' + new URLSearchParams(params).toString();
  const r = await fetch(url);
  if(!r.ok) throw new Error('tweets api');
  return r.json();
}
async function apiTweet(id){
  const r = await fetch('/api/tweet/' + encodeURIComponent(id));
  if(!r.ok) throw new Error('tweet api');
  return r.json();
}
async function apiPrice(startUnix, minutes, pre, grid){
  const r = await fetch('/api/price?' + new URLSearchParams({
    start:String(startUnix), minutes:String(minutes), pre:String(pre||0), grid: grid ? '1' : '0'
  }));
  if(!r.ok) throw new Error('price api');
  return r.json();
}

// [NEW] ML API
async function apiModelPredict(tweetId, windowM){
  const r = await fetch('/api/model/predict', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ tweet_id: String(tweetId), window: Number(windowM) })
  });
  if(!r.ok) throw new Error('predict api');
  return r.json();
}
async function apiStrategyBacktest(params){
  const r = await fetch('/api/strategy/backtest', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(params)
  });
  if(!r.ok) throw new Error('backtest api');
  return r.json();
}

function toLocal(tsSec){ return new Date(tsSec * 1000); }

// ===== UI state =====
const state = {
  page: 1, per_page: 20, year: 'all',
  reply: 0, retweet: 0, quote: 0,
  q: '', total: 0, years: [],
  windowMinutes: 15, currentTweetId: null, preMinutes: 10,
  // Etykietowanie
  useImp: 0, impMin: 8, impThr: 1.0,
  selected: new Set(), label: 'all', sortByChange: false
};
const DEFAULT_PER_PAGE = 20;
let __reqEpoch = 0;
let io;
let isLoading = false;

// ===== KPI (top) =====
function renderTopKpis(st){
  const fmt = v => (v==null || !isFinite(v)) ? '—' : (Number(v).toFixed(2)+'%');
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
  if(!st){
    set('k-n','—'); set('k-up','—'); set('k-down','—'); set('k-neutral','—');
    set('k-min','—'); set('k-max','—');
    const mode = document.getElementById('k-mode'); if (mode) mode.textContent = '—';
    const filt = document.getElementById('k-filter'); if (filt) filt.textContent = 'filtr: all';
    return;
  }
  set('k-n', String(st.n ?? '—'));
  set('k-up', String(st.n_up ?? '—'));
  set('k-down', String(st.n_down ?? '—'));
  set('k-neutral', String(st.n_neutral ?? '—'));
  set('k-min', fmt(st.pct_min));
  set('k-max', fmt(st.pct_max));
  const mode = document.getElementById('k-mode');
  if (mode) {
    if (st.mode === 'imp') {
      const thr = (st.threshold!=null && st.threshold!=='') ? `, próg=${st.threshold}%` : '';
      mode.textContent = `imp m=${st.minute}${thr}`;
    } else {
      mode.textContent = `pre m=${st.minute}, próg=${st.threshold}%`;
    }
  }
  const filt = document.getElementById('k-filter');
  if (filt) {
    const f = st.label_filter || 'all';
    filt.textContent = `filtr: ${f}`;
    filt.classList.remove('up','down','neutral');
    filt.classList.add((f==='up')?'up':(f==='down')?'down':'neutral');
  }
}

// ===== RENDER: list =====
async function loadFiltersAndList({reset=false, initial=false} = {}){
  if (isLoading) return;
  isLoading = true;
  const myEpoch = ++__reqEpoch;

  if (reset) {
    if (io) io.disconnect();
    state.page = 1;
    const list = document.getElementById('list'); if (list) list.innerHTML = '';
    const s = document.getElementById('sentinel'); if (s) { s.textContent = 'Loading…'; s.style.display = ''; }
  }

  const params = {
    page: state.page, per_page: state.per_page,
    year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote,
    q: state.q, label: state.label
  };

  if (state.sortByChange && (state.label === 'up' || state.label === 'down')) {
    params.imp_sort = 1;
    params.year = 'all';
    params.per_page = Math.max(state.per_page, 300);
    state.per_page = params.per_page;
    state.useImp = 1;
    params.imp_filter = 1;
    params.imp_min = state.impMin;
    params.imp_thr = state.impThr;
  }
  if (state.useImp === 1) {
    params.imp_filter = 1;
    params.imp_min = state.impMin;
    params.imp_thr = state.impThr;
  }

  const data = await apiList(params);
  if (myEpoch !== __reqEpoch) { isLoading = false; return; }

  renderTopKpis(data.stats);

  if(!state.years.length && Array.isArray(data.years)){
    state.years = data.years;
    const sel = document.getElementById('f-year');
    state.years.forEach(y=>{
      const opt = document.createElement('option');
      opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
    });
  }

  state.total = data.total || 0;
  const list = document.getElementById('list');
  const frag = document.createDocumentFragment();
  const itemsForRender = (data.items || []).slice();

  if(itemsForRender.length === 0){
    const empty = document.createElement('div');
    empty.className = 'row';
    empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
    list.appendChild(empty);
  } else {
    itemsForRender.forEach(item=>{
      const row = document.createElement('div');
      row.className = 'row';
      row.dataset.id = item.tweet_id;

      const minute = (item.imp_min != null ? item.imp_min : (item.pre_min ?? item.lab_min));
      const pct = (item.imp_pct != null ? item.imp_pct : (item.pre_pct ?? item.lab_pct));
      const metaText = (minute != null) ? `m=${minute}, Δ=${pct == null ? '—' : (Number(pct).toFixed(2)+'%')}` : '';
      if (metaText) row.title = metaText;

      const label = (item.imp_label != null ? item.imp_label : (item.pre_label ?? item.lab_label));
      let pill = '';
      if (label === 'up') pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
      else if (label === 'down') pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
      else pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';

      const rankBadge = (item.rank ? `<span class="pill" title="pozycja po sortowaniu">#${item.rank}</span>` : '');
      const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <h4>Tweet #${item.tweet_id}</h4>
            <p>${escapeHtml(item.text || '')}</p>
            <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
            <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
              ${pill} ${rankBadge}
            </div>
          </div>
          <label class="check" style="white-space:nowrap">
            <input type="checkbox" class="pick" ${checked} /> wybierz
          </label>
        </div>
      `;
      row.querySelector('.pick').addEventListener('change', (e)=>{
        if(e.target.checked) state.selected.add(item.tweet_id);
        else state.selected.delete(item.tweet_id);
        renderOverlay();
      });
      row.addEventListener('click', (ev)=>{
        if(ev.target.classList.contains('pick')) return;
        openDetail(item.tweet_id);
      });
      frag.appendChild(row);
    });
    list.appendChild(frag);
  }

  const s = document.getElementById('sentinel');
  if (s) {
    const loaded = document.querySelectorAll('#list .row').length;
    s.textContent = (state.total === 0) ? '— brak danych —' :
                    (loaded >= state.total) ? '— koniec —' : 'Loading…';
  }

  if (reset) setupInfiniteScroll();
  isLoading = false;
}

// ===== DETAIL + CHART =====
async function openDetail(tweetId){
  const detail = document.getElementById('detail');
  const minuteList = document.getElementById('minute-list');
  detail.innerHTML = '<div class="muted">Ładowanie…</div>';
  Plotly.purge('chart');
  minuteList.textContent = '—';
  try{
    const t = await apiTweet(tweetId);
    detail.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
        <div>
          <div class="muted" style="font-size:12px">${t.created_display}</div>
          <div style="margin-top:4px">${escapeHtml(t.text || '')}</div>
          <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
            ${t.isReply ? '<span class="pill">reply</span>' : ''}
            ${t.isRetweet ? '<span class="pill">retweet</span>' : ''}
            ${t.isQuote ? '<span class="pill">quote</span>' : ''}
          </div>
        </div>
        <div class="muted">Tweet #${t.tweet_id}</div>
      </div>
    `;
    state.currentTweetId = tweetId;
    const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
    renderPctList(payload.pct_changes);
  } catch(e) {
    console.error(e);
    detail.innerHTML = '<div class="muted">Błąd wczytywania tweeta.</div>';
    Plotly.newPlot('chart', [{x:[new Date()], y:[null]}], {title:'Błąd ładowania danych', margin:{t:40}});
  }
}

async function renderChart(startUnix, minutes, pre){
  const payload = await apiPrice(startUnix, minutes, pre);
  const pts = payload.points || [];
  const reason = payload.reason || 'ok';
  const xStart = payload.x_start ? new Date(payload.x_start * 1000) : toLocal(startUnix - (pre||0)*60);
  const xEnd = payload.x_end ? new Date(payload.x_end * 1000) : toLocal(startUnix + minutes*60);
  const tweetX = toLocal(startUnix); tweetX.setSeconds(0, 0);

  if(!pts.length){
    Plotly.newPlot('chart', [{ x:[tweetX], y:[null], mode:'lines', name:'brak danych' }], {
      title: (reason==='no_data' ? 'Brak danych w tym oknie' : ''), margin:{t:30},
      xaxis:{range:[xStart, xEnd], title:'Czas (lokalny)'},
      yaxis:{title:'Cena'},
      shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
    }, {responsive:true});
    return payload;
  }

  const x = pts.map(p=>toLocal(p.t));
  const ohlcTrace = { type:'candlestick', x,
    open:pts.map(p=>p.open), high:pts.map(p=>p.high),
    low: pts.map(p=>p.low),  close:pts.map(p=>p.close), name:'OHLC' };
  const lineTrace = { x, y: pts.map(p=>p.open), mode:'lines', name:'Open' };
  Plotly.newPlot('chart', [ohlcTrace, lineTrace], {
    margin:{l:40,r:20,t:30,b:40},
    xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
    yaxis:{title:'Cena'}, showlegend:false,
    shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
  }, {responsive:true});

  return payload;
}

// ===== OVERLAY =====
async function renderOverlay(){
  if(!state.selected.size){ Plotly.purge('overlay'); return; }
  const traces = [];
  const mins = state.windowMinutes;
  const pre = state.preMinutes;

  for (const id of state.selected){
    try{
      const t = await apiTweet(id);
      const payload = await apiPrice(t.created_ts, mins, pre, /*grid=*/true);
      const g = payload.grid || {};
      const minutesTs = g.minute_ts || [];
      const closes = g.close || [];
      let baseTs = g.tweet_minute_ts;
      let baseIdx = minutesTs.indexOf(baseTs);
      let base = null;
      if (baseIdx < 0) baseIdx = 0;
      if (baseIdx >= 0) base = closes[baseIdx];
      if (base == null) {
        for (let i = Math.max(0, baseIdx); i < closes.length; i++){
          if (closes[i] != null){ base = closes[i]; baseTs = minutesTs[i]; baseIdx = i; break; }
        }
      }
      if (base == null) continue;

      const xs = [], ys = [];
      for (let i=0;i<minutesTs.length;i++){
        const v = closes[i]; if (v == null) continue;
        const offsetMin = (minutesTs[i] - baseTs)/60;
        xs.push(offsetMin); ys.push((v/base - 1)*100);
      }
      const xFilt = [], yFilt = [];
      const left = -pre, right = mins;
      for (let i=0;i<xs.length;i++){
        if (xs[i] >= left && xs[i] <= right){ xFilt.push(xs[i]); yFilt.push(ys[i]); }
      }
      if (xFilt.length) traces.push({ x: xFilt, y: yFilt, mode:'lines', name: `#${id}`, line:{width:1} });
    }catch(_){}
  }
  if(!traces.length){ Plotly.purge('overlay'); return; }
  Plotly.newPlot('overlay', traces, {
    margin:{l:40,r:20,t:30,b:40},
    xaxis:{title:'minuty względem tweeta'},
    yaxis:{title:'% zmiany względem minuty tweeta'},
    showlegend:true
  }, {responsive:true});
}

function renderPctList(pct){
  const minuteList = document.getElementById('minute-list');
  if(!pct){ minuteList.textContent = 'Brak danych.'; return; }
  const order = [1,2,3,4,5,6,7,8,9,10,15,30,60];
  const lines = order.map(m=>{
    const v = pct[m];
    return (v === null || v === undefined) ? `+${m} min: — brak danych` : `+${m} min: ${v.toFixed(2)}%`;
  });
  minuteList.textContent = lines.join('\n');
}

// ===== utils =====
function escapeHtml(s){
  return (s||'').replace(/[&<>"']/g,m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m]));
}

function setupInfiniteScroll(){
  const sentinel = document.getElementById('sentinel');
  const rootEl = document.querySelector('.layout > .pane:first-child');
  if (!sentinel) return;
  if (io) io.disconnect();
  io = new IntersectionObserver(async entries => {
    const e = entries[0];
    if (!e.isIntersecting) return;
    if (isLoading) return;
    const loaded = document.querySelectorAll('#list .row').length;
    if (loaded >= state.total) {
      if (sentinel) {
        sentinel.textContent = (state.total === 0) ? '— brak danych —' : '— koniec —';
      }
      return;
    }
    state.page += 1;
    try { await loadFiltersAndList({reset:false, initial:false}); } catch (err) { console.error(err); }
  }, { root: rootEl || null, rootMargin: '200px', threshold: 0.01 });
  io.observe(sentinel);
}

// ===== wiring =====
window.addEventListener('DOMContentLoaded', ()=>{
  const readBasics = ()=>{
    state.year = document.getElementById('f-year').value || 'all';
    state.q = (document.getElementById('f-q').value || '').trim();
    state.reply = document.getElementById('f-reply').checked ? -1 : 0;
    state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
    state.quote = document.getElementById('f-quote').checked ? -1 : 0;
  };

  document.getElementById('btn-search').addEventListener('click', ()=>{
    readBasics();
    state.page = 1;
    if (state.sortByChange) {
      state.sortByChange = false;
      const btnSort = document.getElementById('btn-sort');
      if (btnSort) btnSort.classList.remove('primary');
      state.per_page = DEFAULT_PER_PAGE;
    }
    loadFiltersAndList({reset:true});
  });

  document.getElementById('f-q').addEventListener('keydown', (e)=>{
    if(e.key==='Enter') document.getElementById('btn-search').click();
  });

  document.getElementById('btn-label-apply').addEventListener('click', ()=>{
    state.impMin = parseInt(document.getElementById('lab-min').value || '8', 10);
    const thrRaw = document.getElementById('lab-thr').value || '1';
    state.impThr = parseFloat(String(thrRaw).replace(',', '.'));
    state.useImp = 1;
    state.page = 1;
    loadFiltersAndList({reset:true});
  });

  document.getElementById('btn-clear-overlay')?.addEventListener('click', ()=>{
    state.selected.clear(); Plotly.purge('overlay');
    document.querySelectorAll('#list .row .pick').forEach(ch => ch.checked = false);
  });

  const selWin = document.getElementById('win-min');
  const btnWin = document.getElementById('win-apply');
  const preCk = document.getElementById('pre-10');
  if (selWin) selWin.value = String(state.windowMinutes || 15);
  if (preCk) preCk.checked = !!state.preMinutes;

  if (selWin && btnWin) {
    btnWin.addEventListener('click', async ()=>{
      const v = parseInt(selWin.value || '15', 10);
      state.windowMinutes = (isNaN(v) ? 15 : v);
      state.preMinutes = (preCk && preCk.checked) ? 10 : 0;
      if (state.currentTweetId) {
        try {
          const t = await apiTweet(state.currentTweetId);
          const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
          renderPctList(payload.pct_changes);
        } catch (e) { console.error(e); }
      }
      renderOverlay();
    });
  }
  if (preCk) {
    preCk.addEventListener('change', async ()=>{
      state.preMinutes = preCk.checked ? 10 : 0;
      if (state.currentTweetId) {
        try {
          const t = await apiTweet(state.currentTweetId);
          const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
          renderPctList(payload.pct_changes);
        } catch (e) { console.error(e); }
      }
      renderOverlay();
    });
  }

  // checkboksy etykiet
  const ckUp = document.getElementById('f-up');
  const ckDn = document.getElementById('f-down');
  const ckNe = document.getElementById('f-neutral');
  function applyLabelCheckboxes(){
    const picks = [];
    if (ckUp && ckUp.checked) picks.push('up');
    if (ckDn && ckDn.checked) picks.push('down');
    if (ckNe && ckNe.checked) picks.push('neutral');
    state.label = (picks.length === 1 ? picks[0] : 'all');
    if (state.label === 'up' || state.label === 'down') {
      state.useImp = 1; state.impThr = '';
    }
    state.page = 1; loadFiltersAndList({reset: true});
  }
  [ckUp, ckDn, ckNe].forEach(el=>{ if (el) el.addEventListener('change', applyLabelCheckboxes); });

  // sort biggest change
  const btnSort = document.getElementById('btn-sort');
  if (btnSort) {
    btnSort.addEventListener('click', () => {
      state.sortByChange = !state.sortByChange;
      btnSort.classList.toggle('primary', state.sortByChange);
      if (state.sortByChange) {
        state.year = 'all';
        const selYear = document.getElementById('f-year'); if (selYear) selYear.value = 'all';
        state.per_page = Math.max(state.per_page, 300);
        state.page = 1;
      } else {
        state.per_page = DEFAULT_PER_PAGE;
        state.page = 1;
      }
      loadFiltersAndList({reset:true});
    });
  }

  // [NEW] PREDYKCJA
  const btnPred = document.getElementById('btn-predict');
  if (btnPred) {
    btnPred.addEventListener('click', async ()=>{
      const out = document.getElementById('pred-result');
      if (!state.currentTweetId) { out.textContent = 'Najpierw wybierz tweeta z listy.'; return; }
      const sel = document.getElementById('pred-window');
      const w = sel ? parseInt(sel.value||'5',10) : 5;
      out.textContent = 'Liczenie…';
      try{
        const resp = await apiModelPredict(state.currentTweetId, w);
        if (!resp.ok) { out.textContent = `Brak wyniku (${resp.reason || 'unknown'})`; return; }
        const r = resp.result;
        const probsTxt = Object.entries(r.probs||{}).map(([k,v])=>`${k}: ${(+v).toFixed(3)}`).join('  ');
        const realized = r.realized || {};
        const r1 = realized.after_1m, y1 = realized.y_1m_auto || '—';
        const r5 = realized.after_5m, y5 = realized.y_5m_auto || '—';
        out.innerHTML = `
          <div>Predykcja: <b>${r.prediction}</b> (conf=${(r.confidence||0).toFixed(3)})</div>
          <div class="muted" style="margin-top:4px">${probsTxt}</div>
          <div style="margin-top:6px">Prawda z bazy: +1m=<b>${r1==null?'—':r1.toFixed(3)+'%'}</b> (${y1}), +5m=<b>${r5==null?'—':r5.toFixed(3)+'%'}</b> (${y5})</div>
        `;
      } catch(e){ out.textContent = `Błąd: ${e}`; }
    });
  }

  // [NEW] BACKTEST
  const btnBt = document.getElementById('btn-backtest');
  if (btnBt) {
    btnBt.addEventListener('click', async ()=>{
      const summary = document.getElementById('bt-summary');
      const table = document.getElementById('bt-table');
      summary.textContent = 'Uruchamiam…';
      table.innerHTML = '';

      // zbierz parametry
      const params = {
        window: parseInt((document.getElementById('bt-window').value || '5'), 10),
        cost_bps: parseFloat((document.getElementById('bt-cost').value || '2')),
        min_prob: parseFloat((document.getElementById('bt-minprob').value || '0.45')),
        only_test_set: !!document.getElementById('bt-only-test').checked,
        // bieżące filtry ze state
        year: state.year, q: state.q, reply: state.reply, retweet: state.retweet, quote: state.quote, label: state.label
      };

      try{
        const resp = await apiStrategyBacktest(params);
        if (!resp.ok) { summary.textContent = `Brak wyników (${resp.reason || 'unknown'})`; return; }
        const s = resp.summary || {};
        summary.textContent = `Trades: ${s.trades}\nHit rate: ${s.hit_rate}\nAvg trade (%): ${s.avg_ret}\nTotal (%): ${s.total_ret}\nSharpe: ${s.sharpe}`;

        const makeRow = (r) => {
        const fmtPct = (v) => (v == null || !isFinite(Number(v))) ? '—' : `${Number(v).toFixed(3)}%`;
        const fmtNum = (v) => (v == null || !isFinite(Number(v))) ? '—' : Number(v).toFixed(3);

        const div = document.createElement('div');
        div.className = 'row';
        div.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:8px">
            <div style="flex:1;min-width:0">
              <h4 style="margin:0 0 4px">Tweet #${r.tweet_id} — ${r.signal}</h4>
              <p>${escapeHtml(r.text||'')}</p>
              <div class="muted" style="font-size:12px;margin-top:4px">${r.created_at}</div>
            </div>
            <div class="pill">ret: ${fmtPct(r["ret_%"])}</div>
            <div class="pill">net: ${fmtPct(r["ret_after_cost_%"])}</div>
            <div class="pill">p=${fmtNum(r.prob)}</div>
          </div>`;
        div.addEventListener('click', ()=>{ openDetail(r.tweet_id); });
        return div;
      };


        const title = (t)=> `<div class="muted" style="padding:6px 8px;border-bottom:1px solid var(--line)">${t}</div>`;
        table.innerHTML = '';
        table.insertAdjacentHTML('beforeend', title('TOP 5'));
        (resp.top5||[]).forEach(r=>table.appendChild(makeRow(r)));
        table.insertAdjacentHTML('beforeend', title('BOTTOM 5'));
        (resp.bottom5||[]).forEach(r=>table.appendChild(makeRow(r)));
      } catch(e){
        summary.textContent = `Błąd: ${e}`;
      }
    });
  }

  // start
  loadFiltersAndList({reset:true, initial:true});
  setupInfiniteScroll();
});
