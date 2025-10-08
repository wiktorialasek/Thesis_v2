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
  preMinutes: 0,            // 0 lub 10
  impactLabel: 'all',       // all|up|down|neutral
  impactMinute: 5,          // 1..20,30,60
  impactThreshold: 0.5,     // %
  impactSort: 1,            // sortuj po |impact|
  selected: new Set()       // do overlay
};

// ===== RENDER: list & filters =====
async function loadFiltersAndList(initial=false){
  const data = await apiList({
    page: state.page, per_page: state.per_page,
    year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,
    imp_label: state.impactLabel, imp_min: state.impactMinute, imp_thr: state.impactThreshold, imp_sort: state.impactSort
  });

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

      const pill = item.impact === 'up'
        ? '<span class="pill" style="background:#ecfdf5;color:#065f46">↑ up</span>'
        : item.impact === 'down'
          ? '<span class="pill" style="background:#fef2f2;color:#991b1b">↓ down</span>'
          : '<span class="pill" style="background:#f3f4f6;color:#111">≈ neutral</span>';

      const checked = state.selected.has(item.tweet_id) ? 'checked' : '';

      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <h4>Tweet #${item.tweet_id}</h4>
            <p>${escapeHtml(item.text || '')}</p>
            <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
            <div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">
              ${pill}
              <span class="muted" style="font-size:12px">
                ${ (item.impact_min!==undefined && item.impact_pct!==undefined)
                    ? `m=${item.impact_min}, Δ=${item.impact_pct==null?'—':(item.impact_pct.toFixed(2)+'%')}`
                    : '' }
              </span>
            </div>
          </div>
          <label class="check" style="white-space:nowrap">
            <input type="checkbox" class="pick" ${checked} /> wybierz
          </label>
        </div>
      `;

      // zaznaczanie do overlay
      row.querySelector('.pick').addEventListener('change', (e)=>{
        if(e.target.checked) state.selected.add(item.tweet_id);
        else state.selected.delete(item.tweet_id);
      });

      // klik wiersza nadal otwiera szczegóły (poza checkboxem)
      row.addEventListener('click', (ev)=>{
        if(ev.target.classList.contains('pick')) return;
        openDetail(item.tweet_id);
      });

      list.appendChild(row);
    });
  }

  // pagination label
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

// ===== RENDER: detail + chart + minute list =====
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
  const tweetX = toLocal(startUnix); tweetX.setSeconds(0, 0); // początek minuty tweeta

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

  const layout = {
    margin:{l:40,r:20,t:30,b:40},
    xaxis:{title:'Czas (lokalny)', range:[xStart, xEnd]},
    yaxis:{title:'Cena'},
    showlegend:false,
    shapes: [{ type:'line', x0:tweetX, x1:tweetX, y0:0, y1:1, xref:'x', yref:'paper', line:{dash:'dot', width:2} }]
  };

  Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
  return payload;
}

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
      const baseTs = g.tweet_minute_ts;
      const baseIdx = minutesTs.indexOf(baseTs);
      const base = (baseIdx >= 0 ? closes[baseIdx] : null);
      if (base == null) continue;

      const xs = [], ys = [];
      for (let i=0;i<minutesTs.length;i++){
        const v = closes[i];
        if (v == null) continue;
        const offsetMin = (minutesTs[i] - baseTs)/60;
        xs.push(offsetMin);
        ys.push((v/base - 1)*100);
      }
      traces.push({ x: xs, y: ys, mode:'lines', name: `#${id}` });
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
  // nawigacja listy
  document.getElementById('prev').addEventListener('click', ()=>{ state.page = Math.max(1, state.page - 1); loadFiltersAndList(false); });
  document.getElementById('next').addEventListener('click', ()=>{ state.page = state.page + 1; loadFiltersAndList(false); });

  // filtry po lewej
  document.getElementById('btn-apply').addEventListener('click', ()=>{
    state.year    = document.getElementById('f-year').value || 'all';
    state.q       = (document.getElementById('f-q').value || '').trim();
    state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
    state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
    state.quote   = document.getElementById('f-quote').checked ? -1 : 0;

    // >>> nowość: filtry wpływu
    state.impactLabel     = document.getElementById('f-imp-label').value || 'all';
    state.impactMinute    = parseInt(document.getElementById('f-imp-min').value || '5', 10);
    state.impactThreshold = parseFloat(document.getElementById('f-imp-thr').value || '0.5');
    state.impactSort      = document.getElementById('f-imp-sort').checked ? 1 : 0;

    state.page = 1;
    loadFiltersAndList(false);
  });

  document.getElementById('btn-clear').addEventListener('click', ()=>{
    document.getElementById('f-year').value = 'all';
    document.getElementById('f-q').value = '';
    document.getElementById('f-reply').checked = false;
    document.getElementById('f-retweet').checked = false;
    document.getElementById('f-quote').checked = false;

    // reset nowości
    document.getElementById('f-imp-label').value = 'all';
    document.getElementById('f-imp-min').value = '5';
    document.getElementById('f-imp-thr').value = '0.5';
    document.getElementById('f-imp-sort').checked = true;

    state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0; state.page=1;
    state.impactLabel='all'; state.impactMinute=5; state.impactThreshold=0.5; state.impactSort=1;

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
    });
  }

  // overlay
  const btnOv = document.getElementById('btn-overlay');
  const btnOvClear = document.getElementById('btn-overlay-clear');
  if (btnOv) btnOv.addEventListener('click', renderOverlay);
  if (btnOvClear) btnOvClear.addEventListener('click', ()=>{
    state.selected.clear(); renderOverlay(); loadFiltersAndList(false);
  });

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


