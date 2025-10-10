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
    start:String(startUnix), minutes:String(minutes), pre:String(pre||0),
    grid: grid ? '1' : '0'
  }));
  if(!r.ok) throw new Error('price api');
  return r.json();
}
function toLocal(tsSec){ return new Date(tsSec * 1000); }

// ===== UI state =====
const state = {
  page: 1, per_page: 20,
  year: 'all', reply: 0, retweet: 0, quote: 0, q: '',
  total: 0, years: [],

  windowMinutes: 15,
  currentTweetId: null,
  preMinutes: 0,

  // Etykietowanie
  useImp: 0,           // 0 = pokazuj precompute; 1 = licz w locie wg lab-*
  impMin: 8,
  impThr: 1.0,

  selected: new Set(),
  label: 'all'
};

// ===== RENDER: list =====
async function loadFiltersAndList(initial=false){
  const params = {
    page: state.page, per_page: state.per_page,
    year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,
    label: state.label 
  };

  if (state.useImp === 1) {
    params.imp_filter = 1;
    params.imp_min = state.impMin;
    params.imp_thr = state.impThr;  // licz z progiem
    params.imp_sort = 0;
    params.imp_in = '';
  }

  const data = await apiList(params);

  // fill years select once
  if(!state.years.length && Array.isArray(data.years)){
    state.years = data.years;
    const sel = document.getElementById('f-year');
    state.years.forEach(y=>{
      const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
    });
  }

  state.total = data.total;

  const list = document.getElementById('list');
  list.innerHTML = '';
  if(data.items.length === 0){
    const empty = document.createElement('div');
    empty.className = 'row';
    empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
    list.appendChild(empty);
  } else {
    data.items.forEach(item=>{
      const row = document.createElement('div');
      row.className = 'row';
      row.dataset.id = item.tweet_id;

      // źródło pigułki: imp_* (jeśli liczono), inaczej precompute
      const label  = (item.imp_label != null ? item.imp_label : (item.pre_label ?? item.lab_label));
      const minute = (item.imp_min   != null ? item.imp_min   : (item.pre_min   ?? item.lab_min));
      const pct    = (item.imp_pct   != null ? item.imp_pct   : (item.pre_pct   ?? item.lab_pct));

      let pill = '';
      if (label === 'up')      pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
      else if (label === 'down') pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
      else                      pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';

      const meta = (minute != null) ? `m=${minute}, Δ=${pct == null ? '—' : (Number(pct).toFixed(2)+'%')}` : '';
      const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <h4>Tweet #${item.tweet_id}</h4>
            <p>${escapeHtml(item.text || '')}</p>
            <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
            <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
              ${pill}
              <span class="muted" style="font-size:12px">${meta}</span>
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

      list.appendChild(row);
    });
  }

  const pagestat = document.getElementById('pagestat');
  const start = (state.page-1)*state.per_page + 1;
  const end = Math.min(state.page*state.per_page, state.total);
  pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

  document.getElementById('prev').disabled = (state.page<=1);
  document.getElementById('next').disabled = (end>=state.total);

  if(initial){
    const first = data.items[0];
    const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
    if(id) openDetail(id);
  }
}

// ===== DETAIL + CHART =====
async function openDetail(tweetId){
  const detail = document.getElementById('detail');
  const minuteList = document.getElementById('minute-list');
  detail.innerHTML = '<div class="muted">Ładowanie…</div>';
  Plotly.purge('chart'); minuteList.textContent = '—';

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
  const xEnd   = payload.x_end   ? new Date(payload.x_end   * 1000) : toLocal(startUnix + minutes*60);
  const tweetX = toLocal(startUnix); tweetX.setSeconds(0, 0);

  if(!pts.length){
    Plotly.newPlot('chart', [{
      x:[tweetX], y:[null], mode:'lines', name:'brak danych'
    }], {
      title: (reason==='no_data' ? 'Brak danych w tym oknie' : ''),
      margin:{t:30},
      xaxis:{range:[xStart, xEnd], title:'Czas (lokalny)'},
      yaxis:{title:'Cena'},
      shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
    }, {responsive:true});
    return payload;
  }

  const x = pts.map(p=>toLocal(p.t));
  const ohlcTrace = {
    type:'candlestick', x,
    open:pts.map(p=>p.open),
    high:pts.map(p=>p.high),
    low: pts.map(p=>p.low),
    close:pts.map(p=>p.close),
    name:'OHLC'
  };
  const lineTrace = { x, y: pts.map(p=>p.open), mode:'lines', name:'Open' };

  Plotly.newPlot('chart', [ohlcTrace, lineTrace], {
    margin:{l:40,r:20,t:30,b:40},
    xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
    yaxis:{title:'Cena'},
    showlegend:false,
    shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
  }, {responsive:true});
  return payload;
}

// ===== OVERLAY =====
async function renderOverlay(){
  if(!state.selected.size){
    Plotly.purge('overlay'); return;
  }
  const traces = [];
  const mins = state.windowMinutes;
  const pre  = state.preMinutes;

  for (const id of state.selected){
    try{
      const t = await apiTweet(id);
      const payload = await apiPrice(t.created_ts, mins, pre, /*grid=*/true);
      const g = payload.grid || {};
      const minutesTs = g.minute_ts || [];
      const closes = g.close || [];
      let baseTs = g.tweet_minute_ts;

      // znajdź indeks minuty tweeta w siatce
      let baseIdx = minutesTs.indexOf(baseTs);

      // jeśli brak albo brak kursu w tej minucie -> fallback do pierwszej minuty ≥ tweet z nie-NULL kursem
      let base = null;
      if (baseIdx < 0) baseIdx = 0; // bezpieczeństwo (nie powinno się zdarzyć)
      if (baseIdx >= 0) base = closes[baseIdx];
      if (base == null) {
        for (let i = Math.max(0, baseIdx); i < closes.length; i++){
          if (closes[i] != null){
            base = closes[i];
            baseTs = minutesTs[i];
            baseIdx = i;
            break;
          }
        }
      }
      if (base == null) continue; // brak danych w całym oknie

      // buduj serię względem (być może) przesuniętej bazy
      const xs = [], ys = [];
      for (let i=0;i<minutesTs.length;i++){
        const v = closes[i];
        if (v == null) continue;
        const offsetMin = (minutesTs[i] - baseTs)/60; // minuty względem minuty bazowej
        xs.push(offsetMin);
        ys.push((v/base - 1)*100);
      }

      // ogranicz do sensownego zakresu (-pre .. +mins)
      const xFilt = [], yFilt = [];
      const left = -pre, right = mins;
      for (let i=0;i<xs.length;i++){
        if (xs[i] >= left && xs[i] <= right){
          xFilt.push(xs[i]); yFilt.push(ys[i]);
        }
      }
      if (xFilt.length) traces.push({ x: xFilt, y: yFilt, mode:'lines', name: `#${id}` });
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
    return (v === null || v === undefined)
      ? `+${m} min: — brak danych`
      : `+${m} min: ${v.toFixed(2)}%`;
  });
  minuteList.textContent = lines.join('\n');
}

// ===== utils =====
function escapeHtml(s){ return (s||'').replace(/[&<>"']/g,m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m])); }

// ===== wiring =====
window.addEventListener('DOMContentLoaded', ()=>{
  // paginacja
  document.getElementById('prev').addEventListener('click', ()=>{ state.page = Math.max(1, state.page - 1); loadFiltersAndList(false); });
  document.getElementById('next').addEventListener('click', ()=>{ state.page = state.page + 1; loadFiltersAndList(false); });

  // podstawowe filtry
  const readBasics = ()=>{
    state.year    = document.getElementById('f-year').value || 'all';
    state.q       = (document.getElementById('f-q').value || '').trim();
    state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
    state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
    state.quote   = document.getElementById('f-quote').checked ? -1 : 0;
    state.label   = document.getElementById('f-label').value || 'all';
  };
  document.getElementById('btn-search').addEventListener('click', ()=>{ readBasics(); state.page=1; loadFiltersAndList(false); });
  document.getElementById('f-q').addEventListener('keydown', (e)=>{ if(e.key==='Enter') document.getElementById('btn-search').click(); });

  // etykietowanie – policz w locie wg parametrów
  document.getElementById('btn-label-apply').addEventListener('click', ()=>{
    state.impMin = parseInt(document.getElementById('lab-min').value || '8', 10);
    state.impThr = parseFloat(document.getElementById('lab-thr').value || '1');
    state.useImp = 1; // włącz liczenie w locie
    state.page = 1;
    loadFiltersAndList(false);
  });

  // panel zakresu wykresu
  const selWin  = document.getElementById('win-min');
  const btnWin  = document.getElementById('win-apply');
  const preCk   = document.getElementById('pre-10');

  if (selWin) selWin.value = String(state.windowMinutes || 15);
  if (preCk)  preCk.checked = !!state.preMinutes;

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

  // start
  loadFiltersAndList(true);
});



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
//   preMinutes: 0,

//   // Filtry wyników (checkboxy)
//   showUp: false,
//   showDown: false,
//   showNeutral: false,

//   // Zaawansowana ocena (opcjonalnie, policz „imp_*” w backendzie)
//   impFilter: 0,        // 1 = licz imp_*; 0 = użyj precomputed
//   impactMinute: 10,
//   impactThreshold: null, // null => wyślemy '' i backend potraktuje jako brak progu (po samym znaku)
//   impactSort: 0,

//   selected: new Set()
// };

// // ===== RENDER: list & filters =====
// async function loadFiltersAndList(initial=false){
//   const imp_in = [];
//   if (state.showUp) imp_in.push('up');
//   if (state.showDown) imp_in.push('down');
//   if (state.showNeutral) imp_in.push('neutral');

//   const data = await apiList({
//     page: state.page, per_page: state.per_page,
//     year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,

//     // filtering / optional re-scoring
//     imp_filter: state.impFilter,          // 0/1
//     imp_min: state.impactMinute,
//     imp_thr: (state.impactThreshold == null ? '' : String(state.impactThreshold)),
//     imp_sort: state.impactSort,
//     imp_in: imp_in.join(',') // może być pusty -> oznacza „wszystkie”
//   });

//   // fill years select once
//   if(!state.years.length && Array.isArray(data.years)){
//     state.years = data.years;
//     const sel = document.getElementById('f-year');
//     state.years.forEach(y=>{
//       const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
//     });
//   }

//   state.total = data.total;

//   const list = document.getElementById('list');
//   list.innerHTML = '';
//   if(data.items.length === 0){
//     const empty = document.createElement('div');
//     empty.className = 'row';
//     empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
//     list.appendChild(empty);
//   } else {
//     data.items.forEach(item=>{
//       const row = document.createElement('div');
//       row.className = 'row';
//       row.dataset.id = item.tweet_id;

//       // Wybierz źródło pigułki: najpierw imp_* (jeśli są), inaczej PRE/lab_*
//       const label  = (item.imp_label != null ? item.imp_label : (item.pre_label ?? item.lab_label));
//       const minute = (item.imp_min   != null ? item.imp_min   : (item.pre_min   ?? item.lab_min));
//       const pct    = (item.imp_pct   != null ? item.imp_pct   : (item.pre_pct   ?? item.lab_pct));

//       let pill = '';
//       if (label === 'up') {
//         pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
//       } else if (label === 'down') {
//         pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
//       } else if (label === 'neutral') {
//         pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';
//       }

//       const metaLine = (minute != null)
//         ? ('m=' + minute + ', Δ=' + (pct == null ? '—' : (Number(pct).toFixed(2) + '%')))
//         : '';

//       const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

//       row.innerHTML = `
//         <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
//           <div style="flex:1;min-width:0">
//             <h4>Tweet #${item.tweet_id}</h4>
//             <p>${escapeHtml(item.text || '')}</p>
//             <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//             <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//               ${pill}
//               <span class="muted" style="font-size:12px">${metaLine}</span>
//             </div>
//           </div>
//           <label class="check" style="white-space:nowrap">
//             <input type="checkbox" class="pick" ${checked} /> wybierz
//           </label>
//         </div>
//       `;

//       // zaznaczanie do overlay (auto)
//       row.querySelector('.pick').addEventListener('change', (e)=>{
//         if(e.target.checked) state.selected.add(item.tweet_id);
//         else state.selected.delete(item.tweet_id);
//         renderOverlay(); // auto-odśwież overlay
//       });

//       // klik wiersza nadal otwiera szczegóły (poza checkboxem)
//       row.addEventListener('click', (ev)=>{
//         if(ev.target.classList.contains('pick')) return;
//         openDetail(item.tweet_id);
//       });

//       list.appendChild(row);
//     });
//   }

//   // pagination label
//   const pagestat = document.getElementById('pagestat');
//   const start = (state.page-1)*state.per_page + 1;
//   const end = Math.min(state.page*state.per_page, state.total);
//   pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

//   document.getElementById('prev').disabled = (state.page<=1);
//   document.getElementById('next').disabled = (end>=state.total);

//   if(initial){
//     const first = data.items[0];
//     const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
//     if(id) openDetail(id);
//   }
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

//   const layout = {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
//     yaxis:{title:'Cena'},
//     showlegend:false,
//     shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
//   };

//   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   return payload;
// }

// // ===== OVERLAY (auto) =====
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
//       const baseTs = g.tweet_minute_ts;
//       const baseIdx = minutesTs.indexOf(baseTs);
//       const base = (baseIdx >= 0 ? closes[baseIdx] : null);
//       if (base == null) continue;

//       const xs = [], ys = [];
//       for (let i=0;i<minutesTs.length;i++){
//         const v = closes[i];
//         if (v == null) continue;
//         const offsetMin = (minutesTs[i] - baseTs)/60;
//         xs.push(offsetMin);
//         ys.push((v/base - 1)*100);
//       }
//       traces.push({ x: xs, y: ys, mode:'lines', name: `#${id}` });
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

// // ===== wiring =====
// window.addEventListener('DOMContentLoaded', ()=>{
//   // paginacja
//   document.getElementById('prev').addEventListener('click', ()=>{ state.page = Math.max(1, state.page - 1); loadFiltersAndList(false); });
//   document.getElementById('next').addEventListener('click', ()=>{ state.page = state.page + 1; loadFiltersAndList(false); });

//   // podstawowe
//   const readBasics = ()=>{
//     state.year    = document.getElementById('f-year').value || 'all';
//     state.q       = (document.getElementById('f-q').value || '').trim();
//     state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//     state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//     state.quote   = document.getElementById('f-quote').checked ? -1 : 0;
//   };

//   // — Samodzielne wyszukiwanie (Rok + fraza + bez reply/retweet/quote) —
//   const btnSearch = document.getElementById('btn-search');
//   if (btnSearch) {
//     btnSearch.addEventListener('click', () => {
//       readBasics();
//       state.page = 1;
//       loadFiltersAndList(false);
//     });

//     const qInput = document.getElementById('f-q');
//     if (qInput) {
//       qInput.addEventListener('keydown', (e) => {
//         if (e.key === 'Enter') btnSearch.click();
//       });
//     }
//   }

//   // — Zastosuj filtr wyników —
//   document.getElementById('btn-filter-apply').addEventListener('click', ()=>{
//     readBasics();

//     state.showUp = document.getElementById('f-show-up').checked;
//     state.showDown = document.getElementById('f-show-down').checked;
//     state.showNeutral = document.getElementById('f-show-neutral').checked;

//     state.impactMinute = parseInt(document.getElementById('f-imp-min').value || '10', 10);
//     const thrStr2 = (document.getElementById('f-imp-thr').value || '').trim();
//     state.impactThreshold = (thrStr2 === '' ? null : parseFloat(thrStr2));
//     state.impactSort = document.getElementById('f-imp-sort').checked ? 1 : 0;

//     // Jeśli ustawiono próg/sort – włącz impFilter, inaczej użyj precompute i tylko checkboxów
//     state.impFilter = (state.impactThreshold != null || state.impactSort) ? 1 : 0;

//     state.page = 1;
//     loadFiltersAndList(false);
//   });

//   // — Wyczyść —
//   document.getElementById('btn-clear').addEventListener('click', ()=>{
//     document.getElementById('f-year').value = 'all';
//     document.getElementById('f-q').value = '';
//     document.getElementById('f-reply').checked = false;
//     document.getElementById('f-retweet').checked = false;
//     document.getElementById('f-quote').checked = false;

//     document.getElementById('f-show-up').checked = false;
//     document.getElementById('f-show-down').checked = false;
//     document.getElementById('f-show-neutral').checked = false;

//     document.getElementById('f-imp-min').value = '10';
//     document.getElementById('f-imp-thr').value = '';
//     document.getElementById('f-imp-sort').checked = false;

//     state.page=1; state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0;
//     state.showUp=false; state.showDown=false; state.showNeutral=false;
//     state.impFilter=0; state.impactMinute=10; state.impactThreshold=null; state.impactSort=0;
//     state.selected.clear();
//     loadFiltersAndList(false);
//     renderOverlay();
//   });

//   // panel zakresu wykresu
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

//   // start
//   loadFiltersAndList(true);
// });


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
//   preMinutes: 0,

//   // 1) Etykietowanie
//   labEnable: 0,
//   labMinute: 8,
//   labThreshold: 1.0,

//   // 2) Filtrowanie wyników
//   showUp: false,
//   showDown: false,
//   showNeutral: false,

//   impFilter: 0,        // 1 = filtruj po minucie/próg; 0 = tylko etykiety
//   impactMinute: 10,
//   impactThreshold: null, // null => wyślemy '' i backend to potraktuje jako brak progu
//   impactSort: 0,

//   selected: new Set()
// };

// // ===== RENDER: list & filters =====
// async function loadFiltersAndList(initial=false){
//   // zbierz imp_in jako CSV (up,down,neutral) — to rozumie backend
//   const imp_in = [];
//   if (state.showUp) imp_in.push('up');
//   if (state.showDown) imp_in.push('down');
//   if (state.showNeutral) imp_in.push('neutral');

//   const data = await apiList({
//     page: state.page, per_page: state.per_page,
//     year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,

//     // 1) Nadaj etykiety (lab_*)
//     lab_enable: state.labEnable,
//     lab_min: state.labMinute,
//     lab_thr: state.labThreshold,

//     // 2) Filtr/ocena (imp_*)
//     imp_filter: state.impFilter,          // 0/1
//     imp_min: state.impactMinute,
//     imp_thr: (state.impactThreshold == null ? '' : String(state.impactThreshold)),
//     imp_sort: state.impactSort,
//     imp_in: imp_in.join(',') // może być pusty -> oznacza „wszystkie”
//   });

//   // fill years select once
//   if(!state.years.length && Array.isArray(data.years)){
//     state.years = data.years;
//     const sel = document.getElementById('f-year');
//     state.years.forEach(y=>{
//       const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
//     });
//   }

//   state.total = data.total;

//   const list = document.getElementById('list');
//   list.innerHTML = '';
//   if(data.items.length === 0){
//     const empty = document.createElement('div');
//     empty.className = 'row';
//     empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
//     list.appendChild(empty);
//   } else {
//     data.items.forEach(item=>{
//       const row = document.createElement('div');
//       row.className = 'row';
//       row.dataset.id = item.tweet_id;

//       // preferuj imp_* jeśli jest, w przeciwnym razie lab_*
//       const label  = (item.imp_label != null ? item.imp_label : item.lab_label);
//       const minute = (item.imp_min   != null ? item.imp_min   : item.lab_min);
//       const pct    = (item.imp_pct   != null ? item.imp_pct   : item.lab_pct);

//       let pill = '';
//       if (label === 'up') {
//         pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
//       } else if (label === 'down') {
//         pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
//       } else if (label === 'neutral') {
//         pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';
//       }

//       // podpis „m=…, Δ=…%” — spójny ze źródłem pigułki
//       const metaLine = (minute != null)
//         ? ('m=' + minute + ', Δ=' + (pct == null ? '—' : (Number(pct).toFixed(2) + '%')))
//         : '';

//       const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

//       row.innerHTML = `
//         <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
//           <div style="flex:1;min-width:0">
//             <h4>Tweet #${item.tweet_id}</h4>
//             <p>${escapeHtml(item.text || '')}</p>
//             <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//             <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//               ${pill}
//               <span class="muted" style="font-size:12px">${metaLine}</span>
//             </div>
//           </div>
//           <label class="check" style="white-space:nowrap">
//             <input type="checkbox" class="pick" ${checked} /> wybierz
//           </label>
//         </div>
//       `;

//       // zaznaczanie do overlay (auto)
//       row.querySelector('.pick').addEventListener('change', (e)=>{
//         if(e.target.checked) state.selected.add(item.tweet_id);
//         else state.selected.delete(item.tweet_id);
//         renderOverlay(); // auto-odśwież overlay
//       });

//       // klik wiersza nadal otwiera szczegóły (poza checkboxem)
//       row.addEventListener('click', (ev)=>{
//         if(ev.target.classList.contains('pick')) return;
//         openDetail(item.tweet_id);
//       });

//       list.appendChild(row);
//     });
//   }

//   // pagination label
//   const pagestat = document.getElementById('pagestat');
//   const start = (state.page-1)*state.per_page + 1;
//   const end = Math.min(state.page*state.per_page, state.total);
//   pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

//   document.getElementById('prev').disabled = (state.page<=1);
//   document.getElementById('next').disabled = (end>=state.total);

//   if(initial){
//     const first = data.items[0];
//     const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
//     if(id) openDetail(id);
//   }
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

//   const layout = {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
//     yaxis:{title:'Cena'},
//     showlegend:false,
//     shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
//   };

//   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   return payload;
// }

// // ===== OVERLAY (auto) =====
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
//       const baseTs = g.tweet_minute_ts;
//       const baseIdx = minutesTs.indexOf(baseTs);
//       const base = (baseIdx >= 0 ? closes[baseIdx] : null);
//       if (base == null) continue;

//       const xs = [], ys = [];
//       for (let i=0;i<minutesTs.length;i++){
//         const v = closes[i];
//         if (v == null) continue;
//         const offsetMin = (minutesTs[i] - baseTs)/60;
//         xs.push(offsetMin);
//         ys.push((v/base - 1)*100);
//       }
//       traces.push({ x: xs, y: ys, mode:'lines', name: `#${id}` });
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

// // ===== wiring =====
// window.addEventListener('DOMContentLoaded', ()=>{
//   // paginacja
//   document.getElementById('prev').addEventListener('click', ()=>{ state.page = Math.max(1, state.page - 1); loadFiltersAndList(false); });
//   document.getElementById('next').addEventListener('click', ()=>{ state.page = state.page + 1; loadFiltersAndList(false); });

//   // podstawowe
//   const readBasics = ()=>{
//     state.year    = document.getElementById('f-year').value || 'all';
//     state.q       = (document.getElementById('f-q').value || '').trim();
//     state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//     state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//     state.quote   = document.getElementById('f-quote').checked ? -1 : 0;
//   };

//   // --- Samodzielne wyszukiwanie (Rok + fraza + bez reply/retweet/quote) ---
//   const btnSearch = document.getElementById('btn-search');
//   if (btnSearch) {
//     btnSearch.addEventListener('click', () => {
//       state.year    = document.getElementById('f-year').value || 'all';
//       state.q       = (document.getElementById('f-q').value || '').trim();
//       state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//       state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//       state.quote   = document.getElementById('f-quote').checked ? -1 : 0;

//       state.page = 1;
//       loadFiltersAndList(false);
//     });

//     // bonus: Enter w polu frazy uruchamia Szukaj
//     const qInput = document.getElementById('f-q');
//     if (qInput) {
//       qInput.addEventListener('keydown', (e) => {
//         if (e.key === 'Enter') {
//           btnSearch.click();
//         }
//       });
//     }
//   }

//   // 1) Nadaj etykiety
//   document.getElementById('btn-label-apply').addEventListener('click', ()=>{
//     readBasics();
//     state.labMinute = parseInt(document.getElementById('lab-min').value || '8', 10);
//     const thrStr = (document.getElementById('lab-thr').value || '').trim();
//     state.labThreshold = (thrStr === '' ? 0.0 : parseFloat(thrStr));
//     state.labEnable = 1;

//     // po nadaniu etykiet nie włączamy impFilter – same etykiety wystarczą
//     state.impFilter = 0;
//     state.page = 1;
//     loadFiltersAndList(false);
//   });

//   // 2) Zastosuj filtr wyników
//   document.getElementById('btn-filter-apply').addEventListener('click', ()=>{
//     readBasics();

//     state.showUp = document.getElementById('f-show-up').checked;
//     state.showDown = document.getElementById('f-show-down').checked;
//     state.showNeutral = document.getElementById('f-show-neutral').checked;

//     state.impactMinute = parseInt(document.getElementById('f-imp-min').value || '10', 10);
//     const thrStr2 = (document.getElementById('f-imp-thr').value || '').trim();
//     state.impactThreshold = (thrStr2 === '' ? null : parseFloat(thrStr2));
//     state.impactSort = document.getElementById('f-imp-sort').checked ? 1 : 0;

//     // Jeśli ustawiono minutę/próg lub sortowanie/etykiety – uruchom licznik „imp”
//     state.impFilter = (state.impactThreshold != null || state.impactSort || state.showUp || state.showDown || state.showNeutral) ? 1 : 0;

//     state.page = 1;
//     loadFiltersAndList(false);
//   });

//   // Wyczyść
//   document.getElementById('btn-clear').addEventListener('click', ()=>{
//     document.getElementById('f-year').value = 'all';
//     document.getElementById('f-q').value = '';
//     document.getElementById('f-reply').checked = false;
//     document.getElementById('f-retweet').checked = false;
//     document.getElementById('f-quote').checked = false;

//     document.getElementById('lab-min').value = '8';
//     document.getElementById('lab-thr').value = '1';

//     document.getElementById('f-show-up').checked = false;
//     document.getElementById('f-show-down').checked = false;
//     document.getElementById('f-show-neutral').checked = false;

//     document.getElementById('f-imp-min').value = '10';
//     document.getElementById('f-imp-thr').value = '';
//     document.getElementById('f-imp-sort').checked = false;

//     state.page=1; state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0;
//     state.labEnable=0; state.labMinute=8; state.labThreshold=1.0;
//     state.showUp=false; state.showDown=false; state.showNeutral=false;
//     state.impFilter=0; state.impactMinute=10; state.impactThreshold=null; state.impactSort=0;
//     state.selected.clear();
//     loadFiltersAndList(false);
//     renderOverlay();
//   });

//   // panel zakresu wykresu
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

//   // start
//   loadFiltersAndList(true);
// });



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
//   preMinutes: 0,

//   // 1) Etykietowanie
//   labEnable: 0,
//   labMinute: 8,
//   labThreshold: 1.0,

//   // 2) Filtrowanie wyników
//   showUp: false,
//   showDown: false,
//   showNeutral: false,

//   impFilter: 0,        // 1 = filtruj po minucie/próg; 0 = tylko etykiety
//   impactMinute: 10,
//   impactThreshold: null, // null => wyślemy '' i backend to potraktuje jako brak progu
//   impactSort: 0,

//   impEnable: 0,

//   selected: new Set()
// };

// // ===== RENDER: list & filters =====
// async function loadFiltersAndList(initial=false){
//   // backend ma tylko pojedynczy imp_label (up | down | neutral | all)
//   const singleLabel =
//     (state.showUp && !state.showDown && !state.showNeutral) ? 'up' :
//     (!state.showUp && state.showDown && !state.showNeutral) ? 'down' :
//     (!state.showUp && !state.showDown && state.showNeutral) ? 'neutral' :
//     'all';

//   const data = await apiList({
//     page: state.page, per_page: state.per_page,
//     year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,

//     // <<< KLUCZOWE >>>
//     imp_enable: state.impEnable,                                  // 0/1 – włącza liczenie etykiet
//     imp_min:     state.impactMinute,                               // minuta liczenia
//     imp_thr:     (state.impactThreshold == null ? '' : String(state.impactThreshold)), // '' => 0%
//     imp_sort:    state.impactSort,                                 // 0/1
//     imp_label:   singleLabel                                       // up | down | neutral | all
//   });


//   // fill years select once
//   if(!state.years.length && Array.isArray(data.years)){
//     state.years = data.years;
//     const sel = document.getElementById('f-year');
//     state.years.forEach(y=>{
//       const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
//     });
//   }

//   state.total = data.total;

//   const list = document.getElementById('list');
//   list.innerHTML = '';
//   if(data.items.length === 0){
//     const empty = document.createElement('div');
//     empty.className = 'row';
//     empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
//     list.appendChild(empty);
//   } else {
//     data.items.forEach(item=>{
//       const row = document.createElement('div');
//       row.className = 'row';
//       row.dataset.id = item.tweet_id;

//       // decyduj co pokazać na pigułce:
//       // preferuj etykietę z etykietowania (lab_*), jeśli dostępna; inaczej użyj imp_*.
//       // wybierz źródło etykiety: najpierw imp_* (jeśli działa filtr imp), inaczej lab_* (etykietowanie)
//       // backend zwraca: impact, impact_min, impact_pct
//       const label  = item.impact;
//       const minute = item.impact_min;
//       const pct    = item.impact_pct;


//       // let pill = '';
//       // if (label === 'up') {
//       //   pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
//       // } else if (label === 'down') {
//       //   pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
//       // } else if (label === 'neutral' && pct != null) {
//       //   pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';
//       // }
//       let pill = '';
//       if (label === 'up') {
//         pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
//       } else if (label === 'down') {
//         pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
//       } else if (label === 'neutral') {
//         pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';
//       }




//       // podpis „m=…, Δ=…%” – spójny ze źródłem pigułki (imp_* > lab_*)
//       const shownMin = (item.imp_min ?? item.lab_min);
//       const shownPct = (item.imp_pct ?? item.lab_pct);

//       const metaLine = (minute != null)
//         ? ('m=' + minute + ', Δ=' + (pct == null ? '—' : (Number(pct).toFixed(2) + '%')))
//         : '';



//       const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

//       row.innerHTML = `
//         <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
//           <div style="flex:1;min-width:0">
//             <h4>Tweet #${item.tweet_id}</h4>
//             <p>${escapeHtml(item.text || '')}</p>
//             <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//             <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//               ${pill}
//               <span class="muted" style="font-size:12px">${metaLine}</span>
//             </div>
//           </div>
//           <label class="check" style="white-space:nowrap">
//             <input type="checkbox" class="pick" ${checked} /> wybierz
//           </label>
//         </div>
//       `;

//       // zaznaczanie do overlay (auto)
//       row.querySelector('.pick').addEventListener('change', (e)=>{
//         if(e.target.checked) state.selected.add(item.tweet_id);
//         else state.selected.delete(item.tweet_id);
//         renderOverlay(); // auto-odśwież overlay
//       });

//       // klik wiersza nadal otwiera szczegóły (poza checkboxem)
//       row.addEventListener('click', (ev)=>{
//         if(ev.target.classList.contains('pick')) return;
//         openDetail(item.tweet_id);
//       });

//       list.appendChild(row);
//     });
//   }

//   // pagination label
//   const pagestat = document.getElementById('pagestat');
//   const start = (state.page-1)*state.per_page + 1;
//   const end = Math.min(state.page*state.per_page, state.total);
//   pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

//   document.getElementById('prev').disabled = (state.page<=1);
//   document.getElementById('next').disabled = (end>=state.total);

//   if(initial){
//     const first = data.items[0];
//     const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
//     if(id) openDetail(id);
//   }
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

//   const layout = {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
//     yaxis:{title:'Cena'},
//     showlegend:false,
//     shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
//   };

//   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   return payload;
// }

// // ===== OVERLAY (auto) =====
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
//       const baseTs = g.tweet_minute_ts;
//       const baseIdx = minutesTs.indexOf(baseTs);
//       const base = (baseIdx >= 0 ? closes[baseIdx] : null);
//       if (base == null) continue;

//       const xs = [], ys = [];
//       for (let i=0;i<minutesTs.length;i++){
//         const v = closes[i];
//         if (v == null) continue;
//         const offsetMin = (minutesTs[i] - baseTs)/60;
//         xs.push(offsetMin);
//         ys.push((v/base - 1)*100);
//       }
//       traces.push({ x: xs, y: ys, mode:'lines', name: `#${id}` });
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

// // ===== wiring =====
// window.addEventListener('DOMContentLoaded', ()=>{
//   // paginacja
//   document.getElementById('prev').addEventListener('click', ()=>{ state.page = Math.max(1, state.page - 1); loadFiltersAndList(false); });
//   document.getElementById('next').addEventListener('click', ()=>{ state.page = state.page + 1; loadFiltersAndList(false); });

//   // podstawowe
//   const readBasics = ()=>{
//     state.year    = document.getElementById('f-year').value || 'all';
//     state.q       = (document.getElementById('f-q').value || '').trim();
//     state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//     state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//     state.quote   = document.getElementById('f-quote').checked ? -1 : 0;
//   };

//   // --- Samodzielne wyszukiwanie (Rok + fraza + bez reply/retweet/quote) ---
//   const btnSearch = document.getElementById('btn-search');
//   if (btnSearch) {
//     btnSearch.addEventListener('click', () => {
//       // zaciągamy tylko podstawowe filtry tekstowe/rok/checkboxy
//       state.year    = document.getElementById('f-year').value || 'all';
//       state.q       = (document.getElementById('f-q').value || '').trim();
//       state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//       state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//       state.quote   = document.getElementById('f-quote').checked ? -1 : 0;

//       // WAŻNE: nie dotykamy tu żadnych pól impact/label/sort — to robi „Zastosuj”
//       // state.impEnable, state.impactLabel, impactMinute, impactThreshold, impactSort — bez zmian

//       state.page = 1;
//       loadFiltersAndList(false);
//     });

//     // bonus: Enter w polu frazy uruchamia Szukaj
//     const qInput = document.getElementById('f-q');
//     if (qInput) {
//       qInput.addEventListener('keydown', (e) => {
//         if (e.key === 'Enter') {
//           btnSearch.click();
//         }
//       });
//     }
//   }


//   // 1) Nadaj etykiety
//   document.getElementById('btn-label-apply').addEventListener('click', ()=>{
//     readBasics();
//     state.impactMinute = parseInt(document.getElementById('lab-min').value || '8', 10);
//     const thrStr = (document.getElementById('lab-thr').value || '').trim();
//     state.impactThreshold = (thrStr === '' ? 0.0 : parseFloat(thrStr));
//     state.impEnable = 1;     // <<< włącz liczenie etykiet po stronie backendu
//     state.impactSort = 0;    // etykietowanie nie musi sortować
//     state.page = 1;
//     loadFiltersAndList(false);

//   });

//   // 2) Zastosuj filtr wyników
//   document.getElementById('btn-filter-apply').addEventListener('click', ()=>{
//     readBasics();

//     state.showUp = document.getElementById('f-show-up').checked;
//     state.showDown = document.getElementById('f-show-down').checked;
//     state.showNeutral = document.getElementById('f-show-neutral').checked;

//     state.impactMinute = parseInt(document.getElementById('f-imp-min').value || '10', 10);
//     const thrStr2 = (document.getElementById('f-imp-thr').value || '').trim();
//     state.impactThreshold = (thrStr2 === '' ? null : parseFloat(thrStr2));
//     state.impactSort = document.getElementById('f-imp-sort').checked ? 1 : 0;

//     // Jeśli ustawiono minutę/próg lub sortowanie – uruchom licznik „imp”
//     state.impFilter = (state.impactThreshold != null || state.impactSort || state.showUp || state.showDown || state.showNeutral) ? 1 : 0;
//     state.impEnable = 1;  // backend musi policzyć etykiety, żeby było po czym filtrować
//     state.page = 1;
//     loadFiltersAndList(false);
//   });

//   // Wyczyść
//   document.getElementById('btn-clear').addEventListener('click', ()=>{
//     document.getElementById('f-year').value = 'all';
//     document.getElementById('f-q').value = '';
//     document.getElementById('f-reply').checked = false;
//     document.getElementById('f-retweet').checked = false;
//     document.getElementById('f-quote').checked = false;

//     document.getElementById('lab-min').value = '8';
//     document.getElementById('lab-thr').value = '1';

//     document.getElementById('f-show-up').checked = false;
//     document.getElementById('f-show-down').checked = false;
//     document.getElementById('f-show-neutral').checked = false;

//     document.getElementById('f-imp-min').value = '10';
//     document.getElementById('f-imp-thr').value = '';
//     document.getElementById('f-imp-sort').checked = false;

//     state.page=1; state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0;
//     state.labEnable=0; state.labMinute=8; state.labThreshold=1.0;
//     state.showUp=false; state.showDown=false; state.showNeutral=false;
//     state.impFilter=0; state.impactMinute=10; state.impactThreshold=null; state.impactSort=0;
//     state.impEnable = 0;
//     state.selected.clear();
//     loadFiltersAndList(false);
//     renderOverlay();
//   });

//   // panel zakresu wykresu
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

//   // start
//   loadFiltersAndList(true);
// });


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
//   preMinutes: 0,

//   // wpływ
//   impactLabel: 'all',
//   impactMinute: 10,
//   impactThreshold: null, // null => wyślij '' i backend potraktuje jak 0%
//   impactSort: 0,
//   impEnable: 0,          // 0 szybki start bez liczenia; 1 licz po "Zastosuj"

//   selected: new Set()
// };

// // ===== RENDER: list & filters =====
// async function loadFiltersAndList(initial=false){
//   const data = await apiList({
//     page: state.page, per_page: state.per_page,
//     year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,
//     imp_label: state.impactLabel,
//     imp_min: state.impactMinute,
//     imp_thr: (state.impactThreshold == null ? '' : String(state.impactThreshold)),
//     imp_sort: state.impactSort,
//     imp_enable: state.impEnable
//   });

//   // fill years select once
//   if(!state.years.length && Array.isArray(data.years)){
//     state.years = data.years;
//     const sel = document.getElementById('f-year');
//     state.years.forEach(y=>{
//       const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
//     });
//   }

//   state.total = data.total;

//   const list = document.getElementById('list');
//   list.innerHTML = '';
//   if(data.items.length === 0){
//     const empty = document.createElement('div');
//     empty.className = 'row';
//     empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
//     list.appendChild(empty);
//   } else {
//     data.items.forEach(item=>{
//       const row = document.createElement('div');
//       row.className = 'row';
//       row.dataset.id = item.tweet_id;

//       // pigułka tylko gdy backend policzył (impact != null)
//       let pill = '';
//       if (item.impact === 'up') {
//         pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
//       } else if (item.impact === 'down') {
//         pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
//       } else if (item.impact === 'neutral' && item.impact_pct != null) {
//         pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';
//       }

//       const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

//       row.innerHTML = `
//         <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
//           <div style="flex:1;min-width:0">
//             <h4>Tweet #${item.tweet_id}</h4>
//             <p>${escapeHtml(item.text || '')}</p>
//             <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//             <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//               ${pill}
//               <span class="muted" style="font-size:12px">
//                 ${
//                   (item.impact_min !== undefined && item.impact_pct !== undefined)
//                     ? ('m=' + item.impact_min + ', Δ=' + (item.impact_pct == null ? '—' : (item.impact_pct.toFixed(2) + '%')))
//                     : ''
//                 }
//               </span>
//             </div>
//           </div>
//           <label class="check" style="white-space:nowrap">
//             <input type="checkbox" class="pick" ${checked} /> wybierz
//           </label>
//         </div>
//       `;

//       // zaznaczanie do overlay
//       row.querySelector('.pick').addEventListener('change', (e)=>{
//         if(e.target.checked) state.selected.add(item.tweet_id);
//         else state.selected.delete(item.tweet_id);
//       });

//       // klik wiersza nadal otwiera szczegóły (poza checkboxem)
//       row.addEventListener('click', (ev)=>{
//         if(ev.target.classList.contains('pick')) return;
//         openDetail(item.tweet_id);
//       });

//       list.appendChild(row);
//     });
//   }

//   // pagination label
//   const pagestat = document.getElementById('pagestat');
//   const start = (state.page-1)*state.per_page + 1;
//   const end = Math.min(state.page*state.per_page, state.total);
//   pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

//   document.getElementById('prev').disabled = (state.page<=1);
//   document.getElementById('next').disabled = (end>=state.total);

//   if(initial){
//     const first = data.items[0];
//     const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
//     if(id) openDetail(id);
//   }
// }

// // ===== RENDER: detail + chart + minute list =====
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
//   const tweetX = toLocal(startUnix); tweetX.setSeconds(0, 0); // początek minuty tweeta

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

//   const layout = {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
//     yaxis:{title:'Cena'},
//     showlegend:false,
//     shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
//   };

//   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   return payload;
// }

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
//       const baseTs = g.tweet_minute_ts;
//       const baseIdx = minutesTs.indexOf(baseTs);
//       const base = (baseIdx >= 0 ? closes[baseIdx] : null);
//       if (base == null) continue;

//       const xs = [], ys = [];
//       for (let i=0;i<minutesTs.length;i++){
//         const v = closes[i];
//         if (v == null) continue;
//         const offsetMin = (minutesTs[i] - baseTs)/60;
//         xs.push(offsetMin);
//         ys.push((v/base - 1)*100);
//       }
//       traces.push({ x: xs, y: ys, mode:'lines', name: `#${id}` });
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

// // ===== wiring =====
// window.addEventListener('DOMContentLoaded', ()=>{
//   // nawigacja listy
//   document.getElementById('prev').addEventListener('click', ()=>{ state.page = Math.max(1, state.page - 1); loadFiltersAndList(false); });
//   document.getElementById('next').addEventListener('click', ()=>{ state.page = state.page + 1; loadFiltersAndList(false); });

//   // filtry po lewej
//   document.getElementById('btn-apply').addEventListener('click', ()=>{
//     state.year    = document.getElementById('f-year').value || 'all';
//     state.q       = (document.getElementById('f-q').value || '').trim();
//     state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//     state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//     state.quote   = document.getElementById('f-quote').checked ? -1 : 0;

//     // przyciski wpływu + minuta + próg
//     state.impactLabel =
//       document.getElementById('f-imp-up').checked ? 'up' :
//       document.getElementById('f-imp-down').checked ? 'down' :
//       document.getElementById('f-imp-neutral').checked ? 'neutral' : 'all';

//     state.impactMinute = parseInt(document.getElementById('f-imp-min').value || '10', 10);

//     const thrStr = (document.getElementById('f-imp-thr').value || '').trim();
//     state.impactThreshold = (thrStr === '' ? null : parseFloat(thrStr));

//     state.impactSort = document.getElementById('f-imp-sort').checked ? 1 : 0;

//     state.impEnable = 1; // włącz liczenie etykiet od teraz
//     state.page = 1;
//     loadFiltersAndList(false);
//   });

//   document.getElementById('btn-clear').addEventListener('click', ()=>{
//   // reset ogólny
//   document.getElementById('f-year').value = 'all';
//   document.getElementById('f-q').value = '';
//   document.getElementById('f-reply').checked = false;
//   document.getElementById('f-retweet').checked = false;
//   document.getElementById('f-quote').checked = false;

//   // reset wpływu
//   document.getElementById('f-imp-all').checked = true;
//   document.getElementById('f-imp-up').checked = false;
//   document.getElementById('f-imp-down').checked = false;
//   document.getElementById('f-imp-neutral').checked = false;
//   document.getElementById('f-imp-min').value = '10';
//   document.getElementById('f-imp-thr').value = '';
//   document.getElementById('f-imp-sort').checked = false;

//   state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0; state.page=1;
//   state.impactLabel='all'; state.impactMinute=10; state.impactThreshold=null; state.impactSort=0;
//   state.impEnable = 0;

//   loadFiltersAndList(false);
// });


//   // panel zakresu wykresu
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
//     });
//   }

//   // overlay
//   const btnOv = document.getElementById('btn-overlay');
//   const btnOvClear = document.getElementById('btn-overlay-clear');
//   if (btnOv) btnOv.addEventListener('click', renderOverlay);
//   if (btnOvClear) btnOvClear.addEventListener('click', ()=>{
//     state.selected.clear(); renderOverlay(); loadFiltersAndList(false);
//   });

//   // start
//   loadFiltersAndList(true);
// });



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
//   preMinutes: 0,
//   impactLabel: 'all',   // domyślnie: nie filtruj po wpływie
//   impactMinute: 10,      // wartość kontrolki
//   impactThreshold: null, // wartość kontrolki
//   impactSort: 0,        // DOMYŚLNIE: bez sortowania po |%|
//   selected: new Set()
// };

// // ===== RENDER: list & filters =====
// async function loadFiltersAndList(initial=false){
//   const data = await apiList({
//   page: state.page, per_page: state.per_page,
//   year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,
//   imp_label: state.impactLabel,
//   imp_min: state.impactMinute,
//   imp_thr: (state.impactThreshold == null ? '' : String(state.impactThreshold)), // '' => 0% na backendzie
//   imp_sort: state.impactSort
//   });


//   // fill years select once
//   if(!state.years.length && Array.isArray(data.years)){
//     state.years = data.years;
//     const sel = document.getElementById('f-year');
//     state.years.forEach(y=>{
//       const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
//     });
//   }

//   state.total = data.total;

//   const list = document.getElementById('list');
//   list.innerHTML = '';
//   if(data.items.length === 0){
//     const empty = document.createElement('div');
//     empty.className = 'row';
//     empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
//     list.appendChild(empty);
//   } else {
//     data.items.forEach(item=>{
//       const row = document.createElement('div');
//       row.className = 'row';
//       row.dataset.id = item.tweet_id;

//       let pill = '';
//       if (item.impact === 'up') {
//         pill = '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>';
//       } else if (item.impact === 'down') {
//         pill = '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>';
//       } else if (item.impact === 'neutral' && item.impact_pct != null) {
//         pill = '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';
//       }
//       // jeśli impact == null → brak pigułki


//       const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

//       row.innerHTML = `
//         <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
//           <div style="flex:1;min-width:0">
//             <h4>Tweet #${item.tweet_id}</h4>
//             <p>${escapeHtml(item.text || '')}</p>
//             <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//             <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//               ${pill}
//               <span class="muted" style="font-size:12px">
//                 ${ (item.impact_min!==undefined && item.impact_pct!==undefined)
//                     ? `m=${item.impact_min}, Δ=${item.impact_pct==null?'—':(item.impact_pct.toFixed(2)+'%')}`
//                     : '' }
//               </span>
//             </div>
//           </div>
//           <label class="check" style="white-space:nowrap">
//             <input type="checkbox" class="pick" ${checked} /> wybierz
//           </label>
//         </div>
//       `;

//       // zaznaczanie do overlay
//       row.querySelector('.pick').addEventListener('change', (e)=>{
//         if(e.target.checked) state.selected.add(item.tweet_id);
//         else state.selected.delete(item.tweet_id);
//       });

//       // klik wiersza nadal otwiera szczegóły (poza checkboxem)
//       row.addEventListener('click', (ev)=>{
//         if(ev.target.classList.contains('pick')) return;
//         openDetail(item.tweet_id);
//       });

//       list.appendChild(row);
//     });
//   }

//   // pagination label
//   const pagestat = document.getElementById('pagestat');
//   const start = (state.page-1)*state.per_page + 1;
//   const end = Math.min(state.page*state.per_page, state.total);
//   pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

//   document.getElementById('prev').disabled = (state.page<=1);
//   document.getElementById('next').disabled = (end>=state.total);

//   if(initial){
//     const first = data.items[0];
//     const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
//     if(id) openDetail(id);
//   }
// }

// // ===== RENDER: detail + chart + minute list =====
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
//   const tweetX = toLocal(startUnix); tweetX.setSeconds(0, 0); // początek minuty tweeta

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

//   const layout = {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
//     yaxis:{title:'Cena'},
//     showlegend:false,
//     shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
//   };

//   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   return payload;
// }

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
//       const baseTs = g.tweet_minute_ts;
//       const baseIdx = minutesTs.indexOf(baseTs);
//       const base = (baseIdx >= 0 ? closes[baseIdx] : null);
//       if (base == null) continue;

//       const xs = [], ys = [];
//       for (let i=0;i<minutesTs.length;i++){
//         const v = closes[i];
//         if (v == null) continue;
//         const offsetMin = (minutesTs[i] - baseTs)/60;
//         xs.push(offsetMin);
//         ys.push((v/base - 1)*100);
//       }
//       traces.push({ x: xs, y: ys, mode:'lines', name: `#${id}` });
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

// // ===== wiring =====
// window.addEventListener('DOMContentLoaded', ()=>{
//   // nawigacja listy
//   document.getElementById('prev').addEventListener('click', ()=>{ state.page = Math.max(1, state.page - 1); loadFiltersAndList(false); });
//   document.getElementById('next').addEventListener('click', ()=>{ state.page = state.page + 1; loadFiltersAndList(false); });

//   // filtry po lewej
//   document.getElementById('btn-apply').addEventListener('click', ()=>{
//     state.year    = document.getElementById('f-year').value || 'all';
//     state.q       = (document.getElementById('f-q').value || '').trim();
//     state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//     state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//     state.quote   = document.getElementById('f-quote').checked ? -1 : 0;

//     // --- nowość: przyciski wpływu + minuta + próg ---
//     state.impactLabel =
//       document.getElementById('f-imp-up').checked ? 'up' :
//       document.getElementById('f-imp-down').checked ? 'down' :
//       document.getElementById('f-imp-neutral').checked ? 'neutral' : 'all';

//     state.impactMinute = parseInt(document.getElementById('f-imp-min').value || '10', 10);

//     // puste pole = null -> wyślemy '' i backend zrobi z tego 0%
//     const thrStr = (document.getElementById('f-imp-thr').value || '').trim();
//     state.impactThreshold = (thrStr === '' ? null : parseFloat(thrStr));

//     state.impactSort = document.getElementById('f-imp-sort').checked ? 1 : 0;


//     state.page = 1;
//     loadFiltersAndList(false);
//   });

//   document.getElementById('btn-clear').addEventListener('click', ()=>{
//     document.getElementById('f-imp-all').checked = true;
//     document.getElementById('f-imp-up').checked = false;
//     document.getElementById('f-imp-down').checked = false;
//     document.getElementById('f-imp-neutral').checked = false;

//     document.getElementById('f-imp-min').value = '10';
//     document.getElementById('f-imp-thr').value = '';

//     document.getElementById('f-imp-sort').checked = false;

//     state.impactLabel = 'all';
//     state.impactMinute = 10;
//     state.impactThreshold = null;
//     state.impactSort = 0;
//     state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0; state.page=1;
//     state.impactLabel='all'; state.impactMinute=5; state.impactThreshold=0.5; state.impactSort=1;

//     loadFiltersAndList(false);
//   });

//   // panel zakresu wykresu
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
//     });
//   }

//   // overlay
//   const btnOv = document.getElementById('btn-overlay');
//   const btnOvClear = document.getElementById('btn-overlay-clear');
//   if (btnOv) btnOv.addEventListener('click', renderOverlay);
//   if (btnOvClear) btnOvClear.addEventListener('click', ()=>{
//     state.selected.clear(); renderOverlay(); loadFiltersAndList(false);
//   });

//   // start
//   loadFiltersAndList(true);
// });



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
// // async function apiPrice(startUnix, minutes, pre){
// //   const r = await fetch('/api/price?' + new URLSearchParams({
// //     start:String(startUnix), minutes:String(minutes), pre:String(pre||0)
// //   }));
// //   if(!r.ok) throw new Error('price api');
// //   return r.json();
// // }
// async function apiPrice(startUnix, minutes, pre, grid){
//   const r = await fetch('/api/price?' + new URLSearchParams({
//     start:String(startUnix), minutes:String(minutes), pre:String(pre||0), grid: grid ? '1' : '0'
//   }));
//   if(!r.ok) throw new Error('price api');
//   return r.json();
// }


// // async function apiPrice(startUnix, minutes){
// //   const r = await fetch('/api/price?' + new URLSearchParams({start:String(startUnix), minutes:String(minutes)}));
// //   if(!r.ok) throw new Error('price api');
// //   return r.json();
// // }
// function toLocal(tsSec){ return new Date(tsSec * 1000); }

// // ===== UI state =====
// const state = {
//   page: 1, per_page: 20,
//   year: 'all', reply: 0, retweet: 0, quote: 0, q: '',
//   total: 0, years: [],
//   windowMinutes: 15,           // << nowy stan: długość okna wykresu
//   currentTweetId: null,         // << zapamiętanie otwartego tweeta
//   preMinutes: 0,  // 0 lub 10
//   impactLabel: 'all',   // all|up|down|neutral
//   impactMinute: 5,      // 1..20,30,60
//   impactThreshold: 0.5, // w %
//   impactSort: 1,        // 1 sortuj po |impact|
//   selected: new Set()  // zaznaczone tweety do overlay
// };

// // ===== RENDER: list & filters =====
// async function loadFiltersAndList(initial=false){
//   // const data = await apiList({
//   //   page: state.page, per_page: state.per_page,
//   //   year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q
//   // });
//     const data = await apiList({
//     page: state.page, per_page: state.per_page,
//     year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,
//     imp_label: state.impactLabel, imp_min: state.impactMinute, imp_thr: state.impactThreshold, imp_sort: state.impactSort
//   });


//   // fill years select once
//   if(!state.years.length && Array.isArray(data.years)){
//     state.years = data.years;
//     const sel = document.getElementById('f-year');
//     state.years.forEach(y=>{
//       const opt = document.createElement('option'); opt.value = String(y); opt.textContent = y; sel.appendChild(opt);
//     });
//   }

//   state.total = data.total;

//   const list = document.getElementById('list');
//   list.innerHTML = '';
//   if(data.items.length === 0){
//     const empty = document.createElement('div');
//     empty.className = 'row';
//     empty.innerHTML = '<div class="muted">Brak wyników dla wybranych filtrów.</div>';
//     list.appendChild(empty);
//   } else {
//     // data.items.forEach(item=>{
//     //   const row = document.createElement('div');
//     //   row.className = 'row';
//     //   row.dataset.id = item.tweet_id;
//     //   row.innerHTML = `
//     //     <h4>Tweet #${item.tweet_id}</h4>
//     //     <p>${escapeHtml(item.text || '')}</p>
//     //     <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//     //   `;
//     //   row.addEventListener('click', ()=> openDetail(item.tweet_id));
//     //   list.appendChild(row);
//     // });
//     data.items.forEach(item=>{
//       const row = document.createElement('div');
//       row.className = 'row';
//       row.dataset.id = item.tweet_id;

//       const pill = item.impact === 'up'
//         ? '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>'
//         : item.impact === 'down'
//           ? '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>'
//           : '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';

//       const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

//       row.innerHTML = `
//         <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
//           <div style="flex:1;min-width:0">
//             <h4>Tweet #${item.tweet_id}</h4>
//             <p>${escapeHtml(item.text || '')}</p>
//             <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
//             <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
//               ${pill}
//               <span class="muted" style="font-size:12px">
//                 ${ (item.impact_min!==undefined && item.impact_pct!==undefined)
//                     ? `m=${item.impact_min}, Δ=${item.impact_pct==null?'—':(item.impact_pct.toFixed(2)+'%')}`
//                     : '' }
//               </span>
//             </div>
//           </div>
//           <label class="check" style="white-space:nowrap">
//             <input type="checkbox" class="pick" ${checked} /> wybierz
//           </label>
//         </div>
//       `;

//       // zaznaczanie do overlay
//       row.querySelector('.pick').addEventListener('change', (e)=>{
//         if(e.target.checked) state.selected.add(item.tweet_id);
//         else state.selected.delete(item.tweet_id);
//       });

//       // klik wiersza nadal otwiera szczegóły (poza checkboxem)
//       row.addEventListener('click', (ev)=>{
//         if(ev.target.classList.contains('pick')) return;
//         openDetail(item.tweet_id);
//       });

//       list.appendChild(row);
//     });
//   }

//   // pagination label
//   const pagestat = document.getElementById('pagestat');
//   const start = (state.page-1)*state.per_page + 1;
//   const end = Math.min(state.page*state.per_page, state.total);
//   pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

//   // enable/disable buttons
//   document.getElementById('prev').disabled = (state.page<=1);
//   document.getElementById('next').disabled = (end>=state.total);

//   // open initial detail
//   if(initial){
//     const first = data.items[0];
//     const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
//     if(id) openDetail(id);
//   }
// }

// // ===== RENDER: detail + chart + minute list =====
// async function openDetail(tweetId){
//   const detail = document.getElementById('detail');
//   const chart = document.getElementById('chart');
//   const minuteList = document.getElementById('minute-list');
//   detail.innerHTML = '<div class="muted">Ładowanie…</div>';
//   Plotly.purge('chart'); minuteList.textContent = '—';

//   try{
//     const t = await apiTweet(tweetId);
//     // header
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
//      // >>> [NOWE] zapamiętujemy bieżącego tweeta
//     state.currentTweetId = tweetId;

//     // >>> [NOWE] wykres z aktualnym oknem (state.windowMinutes)
//     const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);

//     // >>> [NOWE] % zmiany względem chwili tweeta
//     renderPctList(payload.pct_changes);

//   } catch(e) {
//     console.error(e);
//     detail.innerHTML = '<div class="muted">Błąd wczytywania tweeta.</div>';
//     Plotly.newPlot('chart', [{x:[new Date()], y:[null]}], {title:'Błąd ładowania danych', margin:{t:40}});
//   }

//   //   // chart
//   //   await renderChart(t.created_ts, 15);

//   //   // minute list (text)
//   //   const u = '/api/price?' + new URLSearchParams({start:String(t.created_ts), minutes:'15', format:'text'}).toString();
//   //   const txt = await fetch(u).then(r=>r.text());
//   //   minuteList.textContent = txt;
//   // }catch(e){
//   //   console.error(e);
//   //   detail.innerHTML = '<div class="muted">Błąd wczytywania tweeta.</div>';
//   //   Plotly.newPlot('chart', [{x:[new Date()], y:[null]}], {title:'Błąd ładowania danych', margin:{t:40}});
//   // }
// }

// async function renderChart(startUnix, minutes, pre){
//   const payload = await apiPrice(startUnix, minutes, pre);
//   const pts = payload.points || [];
//   const reason = payload.reason || 'ok';

//   // Dane czasu do zakresu X:
//   const xStart = payload.x_start ? new Date(payload.x_start * 1000) : toLocal(startUnix - (pre||0)*60);
//   const xEnd   = payload.x_end   ? new Date(payload.x_end   * 1000) : toLocal(startUnix + minutes*60);
//   const tweetX = toLocal(startUnix);
//   tweetX.setSeconds(0, 0); // początek tej minuty

//   if(!pts.length){
//     Plotly.newPlot('chart', [{
//       x:[tweetX], y:[null], mode:'lines', name:'brak danych'
//     }], {
//       title: (reason==='no_data' ? 'Brak danych w tym oknie' : ''),
//       margin:{t:30},
//       xaxis:{range:[xStart, xEnd], title:'Czas (lokalny)'},
//       yaxis:{title:'Cena'},
//       shapes: [{
//         type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper',
//         line:{dash:'dot', width:2}
//       }]
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

//   const layout = {
//     margin:{l:40,r:20,t:30,b:40},
//     xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
//     yaxis:{title:'Cena'},
//     showlegend:false,
//     // pionowa linia w chwili tweeta:
//     shapes: [{
//       type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper',
//       line:{dash:'dot', width:2}
//     }]
//   };

//   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   return payload;
// }

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
//       const baseTs = g.tweet_minute_ts;
//       const baseIdx = minutesTs.indexOf(baseTs);
//       const base = (baseIdx >= 0 ? closes[baseIdx] : null);
//       if (base == null) continue;

//       const xs = [], ys = [];
//       for (let i=0;i<minutesTs.length;i++){
//         const v = closes[i];
//         if (v == null) continue;
//         const offsetMin = (minutesTs[i] - baseTs)/60;
//         xs.push(offsetMin);
//         ys.push((v/base - 1)*100);
//       }
//       traces.push({ x: xs, y: ys, mode:'lines', name: `#${id}` });
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


// // async function renderChart(startUnix, minutes){
// //   const payload = await apiPrice(startUnix, minutes);
// //   const pts = payload.points || [];
// //   const reason = payload.reason || 'ok';
// //   const usedStart = payload.used_start || startUnix;

// //   if(reason === 'fallback_next'){
// //     const msg = document.createElement('div');
// //     msg.className = 'muted';
// //     msg.style.margin = '6px 0';
// //     msg.textContent = 'Brak notowań w chwili tweeta (poza sesją). Pokazuję najbliższe '
// //       + String(minutes) + ' min od: ' + toLocal(usedStart).toLocaleString();
// //     document.getElementById('detail').appendChild(msg);
// //   }
// //   if(!pts.length){
// //     Plotly.newPlot('chart', [{
// //       x:[toLocal(startUnix)], y:[null], mode:'lines', name:'brak danych'
// //     }], { title: 'Brak danych w tym oknie', margin:{t:40} }, {responsive:true});
// //     return payload;
// //   }

// //   const x = pts.map(p=>toLocal(p.t));
// //   const ohlcTrace = {
// //     type:'candlestick',
// //     x,
// //     open:pts.map(p=>p.open),
// //     high:pts.map(p=>p.high),
// //     low: pts.map(p=>p.low),
// //     close:pts.map(p=>p.close),
// //     name:'OHLC'
// //   };
// //   const lineTrace = { x, y: pts.map(p=>p.close), mode:'lines', name:'Close' };
// //   const layout = {
// //     margin:{l:40,r:20,t:30,b:40},
// //     xaxis:{title:'Czas (lokalny)'},
// //     yaxis:{title:'Cena'},
// //     showlegend:false
// //   };
// //   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});

// //   return payload;
// // }

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

// // async function renderChart(startUnix, minutes){
// //   const payload = await apiPrice(startUnix, minutes);
// //   const pts = payload.points || [];
// //   const reason = payload.reason || 'ok';
// //   const usedStart = payload.used_start || startUnix;

// //   if(reason === 'fallback_next'){
// //     const msg = document.createElement('div');
// //     msg.className = 'muted';
// //     msg.style.margin = '6px 0';
// //     msg.textContent = 'Brak notowań w chwili tweeta (poza sesją). Pokazuję najbliższe 15 min od: '
// //       + toLocal(usedStart).toLocaleString();
// //     document.getElementById('detail').appendChild(msg);
// //   }
// //   if(!pts.length){
// //     Plotly.newPlot('chart', [{
// //       x:[toLocal(startUnix)], y:[null], mode:'lines', name:'brak danych'
// //     }], { title: 'Brak danych w tym oknie', margin:{t:40} }, {responsive:true});
// //     return;
// //   }

// //   const x = pts.map(p=>toLocal(p.t));
// //   const ohlcTrace = {
// //     type:'candlestick',
// //     x,
// //     open:pts.map(p=>p.open),
// //     high:pts.map(p=>p.high),
// //     low: pts.map(p=>p.low),
// //     close:pts.map(p=>p.close),
// //     name:'OHLC'
// //   };
// //   const lineTrace = { x, y: pts.map(p=>p.close), mode:'lines', name:'Close' };
// //   const layout = {
// //     margin:{l:40,r:20,t:30,b:40},
// //     xaxis:{title:'Time [HH:MM:SS}'},
// //     yaxis:{title:'Quote'},
// //     showlegend:false
// //   };
// //   Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
// // }

// // ===== utils =====
// function escapeHtml(s){ return (s||'').replace(/[&<>"']/g,m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m])); }


// // ===== wiring =====
// window.addEventListener('DOMContentLoaded', ()=>{

//   // --- nawigacja listy ---
//   document.getElementById('prev').addEventListener('click', ()=>{
//     state.page = Math.max(1, state.page - 1);
//     loadFiltersAndList(false);
//   });
//   document.getElementById('next').addEventListener('click', ()=>{
//     state.page = state.page + 1;
//     loadFiltersAndList(false);
//   });

//   // --- filtry po lewej ---
//     document.getElementById('btn-apply').addEventListener('click', ()=>{
//     state.year   = document.getElementById('f-year').value || 'all';
//     state.q      = (document.getElementById('f-q').value || '').trim();
//     state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//     state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//     state.quote   = document.getElementById('f-quote').checked ? -1 : 0;

//     // >>> NOWE filtry wpływu:
//     state.impactLabel     = document.getElementById('f-imp-label').value || 'all';
//     state.impactMinute    = parseInt(document.getElementById('f-imp-min').value || '5', 10);
//     state.impactThreshold = parseFloat(document.getElementById('f-imp-thr').value || '0.5');
//     state.impactSort      = document.getElementById('f-imp-sort').checked ? 1 : 0;

//     state.page = 1;
//     loadFiltersAndList(false);
//   });

//   // document.getElementById('btn-apply').addEventListener('click', ()=>{
//   //   state.year   = document.getElementById('f-year').value || 'all';
//   //   state.q      = (document.getElementById('f-q').value || '').trim();
//   //   state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
//   //   state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
//   //   state.quote   = document.getElementById('f-quote').checked ? -1 : 0;
//   //   state.page = 1;
//   //   loadFiltersAndList(false);
//   // });

//   document.getElementById('btn-clear').addEventListener('click', ()=>{
//     document.getElementById('f-year').value = 'all';
//     document.getElementById('f-q').value = '';
//     document.getElementById('f-reply').checked = false;
//     document.getElementById('f-retweet').checked = false;
//     document.getElementById('f-quote').checked = false;
//   // >>> NOWE:
//     document.getElementById('f-imp-label').value = 'all';
//     document.getElementById('f-imp-min').value = '5';
//     document.getElementById('f-imp-thr').value = '0.5';
//     document.getElementById('f-imp-sort').checked = true;

//     // state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0; state.page=1;
//     state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0;
//     state.impactLabel='all'; state.impactMinute=5; state.impactThreshold=0.5; state.impactSort=1;
//     state.page=1;

//     loadFiltersAndList(false);
//   });

//   // --- [NOWE] panel zakresu wykresu (prawa kolumna) ---
//   const selWin  = document.getElementById('win-min');   // select z minutami
//   const btnWin  = document.getElementById('win-apply'); // "Zastosuj"
//   const preCk   = document.getElementById('pre-10');    // checkbox "pokaż −10 min"

//   // ustaw stan początkowy panelu (na wszelki wypadek)
//   if (selWin) selWin.value = String(state.windowMinutes || 15);
//   if (preCk)  preCk.checked = !!state.preMinutes;

//   // klik "Zastosuj" — zmieniamy długość okna i (ewentualnie) pre
//   if (selWin && btnWin) {
//     btnWin.addEventListener('click', async ()=>{
//       const v = parseInt(selWin.value || '15', 10);
//       state.windowMinutes = (isNaN(v) ? 15 : v);
//       state.preMinutes = (preCk && preCk.checked) ? 10 : 0;

//       // jeśli mamy otwartego tweeta — przerysuj wykres i % zmian
//       if (state.currentTweetId) {
//         try {
//           const t = await apiTweet(state.currentTweetId);
//           const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
//           renderPctList(payload.pct_changes);
//         } catch (e) {
//           console.error(e);
//         }
//       }
//     });
//   }

//   // zmiana samego checkboxa "−10 min" — natychmiast przerysuj
//   if (preCk) {
//     preCk.addEventListener('change', async ()=>{
//       state.preMinutes = preCk.checked ? 10 : 0;
//       if (state.currentTweetId) {
//         try {
//           const t = await apiTweet(state.currentTweetId);
//           const payload = await renderChart(t.created_ts, state.windowMinutes, state.preMinutes);
//           renderPctList(payload.pct_changes);
//         } catch (e) {
//           console.error(e);
//         }
//       }
//     });
//   }

//   // --- start ---
//   loadFiltersAndList(true);

//     // — overlay —
//   const btnOv = document.getElementById('btn-overlay');
//   const btnOvClear = document.getElementById('btn-overlay-clear');
//   if (btnOv) btnOv.addEventListener('click', renderOverlay);
//   if (btnOvClear) btnOvClear.addEventListener('click', ()=>{
//     state.selected.clear();
//     renderOverlay();
//     loadFiltersAndList(false);
//   });

// });


