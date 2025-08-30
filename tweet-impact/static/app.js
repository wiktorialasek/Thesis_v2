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
async function apiPrice(startUnix, minutes){
  const r = await fetch('/api/price?' + new URLSearchParams({start:String(startUnix), minutes:String(minutes)}));
  if(!r.ok) throw new Error('price api');
  return r.json();
}
function toLocal(tsSec){ return new Date(tsSec * 1000); }

// ===== UI state =====
const state = {
  page: 1, per_page: 20,
  year: 'all', reply: 0, retweet: 0, quote: 0, q: '',
  total: 0, years: []
};

// ===== RENDER: list & filters =====
async function loadFiltersAndList(initial=false){
  const data = await apiList({
    page: state.page, per_page: state.per_page,
    year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q
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
      row.innerHTML = `
        <h4>Tweet #${item.tweet_id}</h4>
        <p>${escapeHtml(item.text || '')}</p>
        <div class="muted" style="font-size:12px;margin-top:4px">${item.created_at_display}</div>
      `;
      row.addEventListener('click', ()=> openDetail(item.tweet_id));
      list.appendChild(row);
    });
  }

  // pagination label
  const pagestat = document.getElementById('pagestat');
  const start = (state.page-1)*state.per_page + 1;
  const end = Math.min(state.page*state.per_page, state.total);
  pagestat.textContent = (state.total ? `${start}–${end} z ${state.total}` : '0');

  // enable/disable buttons
  document.getElementById('prev').disabled = (state.page<=1);
  document.getElementById('next').disabled = (end>=state.total);

  // open initial detail
  if(initial){
    const first = data.items[0];
    const id = window.INITIAL_TWEET_ID || (first && first.tweet_id);
    if(id) openDetail(id);
  }
}

// ===== RENDER: detail + chart + minute list =====
async function openDetail(tweetId){
  const detail = document.getElementById('detail');
  const chart = document.getElementById('chart');
  const minuteList = document.getElementById('minute-list');
  detail.innerHTML = '<div class="muted">Ładowanie…</div>';
  Plotly.purge('chart'); minuteList.textContent = '—';

  try{
    const t = await apiTweet(tweetId);
    // header
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

    // chart
    await renderChart(t.created_ts, 15);

    // minute list (text)
    const u = '/api/price?' + new URLSearchParams({start:String(t.created_ts), minutes:'15', format:'text'}).toString();
    const txt = await fetch(u).then(r=>r.text());
    minuteList.textContent = txt;
  }catch(e){
    console.error(e);
    detail.innerHTML = '<div class="muted">Błąd wczytywania tweeta.</div>';
    Plotly.newPlot('chart', [{x:[new Date()], y:[null]}], {title:'Błąd ładowania danych', margin:{t:40}});
  }
}

async function renderChart(startUnix, minutes){
  const payload = await apiPrice(startUnix, minutes);
  const pts = payload.points || [];
  const reason = payload.reason || 'ok';
  const usedStart = payload.used_start || startUnix;

  if(reason === 'fallback_next'){
    const msg = document.createElement('div');
    msg.className = 'muted';
    msg.style.margin = '6px 0';
    msg.textContent = 'Brak notowań w chwili tweeta (poza sesją). Pokazuję najbliższe 15 min od: '
      + toLocal(usedStart).toLocaleString();
    document.getElementById('detail').appendChild(msg);
  }
  if(!pts.length){
    Plotly.newPlot('chart', [{
      x:[toLocal(startUnix)], y:[null], mode:'lines', name:'brak danych'
    }], { title: 'Brak danych w tym oknie', margin:{t:40} }, {responsive:true});
    return;
  }

  const x = pts.map(p=>toLocal(p.t));
  const ohlcTrace = {
    type:'candlestick',
    x,
    open:pts.map(p=>p.open),
    high:pts.map(p=>p.high),
    low: pts.map(p=>p.low),
    close:pts.map(p=>p.close),
    name:'OHLC'
  };
  const lineTrace = { x, y: pts.map(p=>p.close), mode:'lines', name:'Close' };
  const layout = {
    margin:{l:40,r:20,t:30,b:40},
    xaxis:{title:'Czas (lokalny)'},
    yaxis:{title:'Cena'},
    showlegend:false
  };
  Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
}

// ===== utils =====
function escapeHtml(s){ return (s||'').replace(/[&<>"']/g,m=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[m])); }

// ===== wiring =====
window.addEventListener('DOMContentLoaded', ()=>{
  // buttons
  document.getElementById('prev').addEventListener('click', ()=>{ state.page=Math.max(1,state.page-1); loadFiltersAndList(false); });
  document.getElementById('next').addEventListener('click', ()=>{ state.page=state.page+1; loadFiltersAndList(false); });

  document.getElementById('btn-apply').addEventListener('click', ()=>{
    state.year = document.getElementById('f-year').value || 'all';
    state.q = document.getElementById('f-q').value.trim();
    state.reply   = document.getElementById('f-reply').checked ? 1 : 0;
    state.retweet = document.getElementById('f-retweet').checked ? 1 : 0;
    state.quote   = document.getElementById('f-quote').checked ? 1 : 0;
    state.page = 1;
    loadFiltersAndList(false);
  });
  document.getElementById('btn-clear').addEventListener('click', ()=>{
    document.getElementById('f-year').value = 'all';
    document.getElementById('f-q').value = '';
    document.getElementById('f-reply').checked = false;
    document.getElementById('f-retweet').checked = false;
    document.getElementById('f-quote').checked = false;
    state.year='all'; state.q=''; state.reply=0; state.retweet=0; state.quote=0; state.page=1;
    loadFiltersAndList(false);
  });

  // initial load
  loadFiltersAndList(true);
});



// async function fetchWindow(startUnix, minutes) {
//   const params = new URLSearchParams({ start: String(startUnix), minutes: String(minutes) });
//   const res = await fetch(`/api/price?${params.toString()}`);
//   if (!res.ok) throw new Error('API error');
//   return res.json();
// }
// function toLocal(tsSec){ return new Date(tsSec * 1000); }

// async function renderChartFromConfig() {
//   if (!window.APP_CONFIG) return;
//   const { startUnix, minutes } = window.APP_CONFIG;

//   try {
//     const payload = await fetchWindow(startUnix, minutes);
//     const pts = payload.points || [];
//     const reason = payload.reason || "ok";
//     const usedStart = payload.used_start || startUnix;

//     const chartEl = document.getElementById('chart');
//     if (!chartEl) return;

//     if (reason === "fallback_next") {
//       const msg = document.createElement('div');
//       msg.className = 'muted mb-2';
//       msg.textContent = 'Brak notowań w czasie tweeta (poza sesją). Pokazuję najbliższe dostępne 15 min od: '
//                         + toLocal(usedStart).toLocaleString();
//       chartEl.parentElement.insertBefore(msg, chartEl);
//     } else if (!pts.length) {
//       Plotly.newPlot('chart', [{
//         x: [toLocal(startUnix)], y: [null], mode: 'lines', name: 'brak danych'
//       }], { title: 'Brak danych w tym oknie (poza sesją?)', margin:{t:40} });
//       return;
//     }

//     const x = pts.map(p => toLocal(p.t));
//     const ohlcTrace = {
//       type: 'candlestick',
//       x,
//       open: pts.map(p => p.open),
//       high: pts.map(p => p.high),
//       low:  pts.map(p => p.low),
//       close: pts.map(p => p.close),
//       name: 'OHLC'
//     };
//     const lineTrace = { x, y: pts.map(p => p.close), mode: 'lines', name: 'Close' };

//     const layout = {
//       margin: { l:40, r:20, t:30, b:40 },
//       xaxis: { title:'Czas (lokalny)' },
//       yaxis: { title:'Cena' },
//       showlegend: false
//     };
//     Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   } catch(e) {
//     console.error(e);
//     Plotly.newPlot('chart', [{
//       x:[toLocal(startUnix)], y:[null], mode:'lines', name:'błąd'
//     }], { title: 'Błąd ładowania danych', margin:{t:40} });
//   }
// }





// async function fetchWindow(startUnix, minutes) {
//   const params = new URLSearchParams({ start: String(startUnix), minutes: String(minutes) });
//   const res = await fetch(`/api/price?${params.toString()}`);
//   if (!res.ok) throw new Error('API error');
//   return res.json();
// }
// function toLocal(tsSec){ return new Date(tsSec * 1000); }

// async function renderChartFromConfig() {
//   if (!window.APP_CONFIG) return;
//   const { startUnix, minutes } = window.APP_CONFIG;

//   try {
//     const payload = await fetchWindow(startUnix, minutes);
//     const pts = payload.points || [];
//     const reason = payload.reason || "ok";
//     const usedStart = payload.used_start || startUnix;

//     // komunikat nad wykresem (fallback/poza sesją)
//     const chartEl = document.getElementById('chart');
//     if (reason === "fallback_next") {
//       const msg = document.createElement('div');
//       msg.className = 'text-muted mb-2';
//       msg.textContent = 'Brak notowań w czasie tweeta (poza sesją). Pokazuję najbliższe dostępne 15 min od: ' + toLocal(usedStart).toLocaleString();
//       chartEl.parentElement.insertBefore(msg, chartEl);
//     } else if (!pts.length) {
//       Plotly.newPlot('chart', [{
//         x: [toLocal(startUnix)], y: [null], mode: 'lines', name: 'brak danych'
//       }], { title: 'Brak danych w tym oknie (poza sesją?)', margin:{t:40} });
//       return;
//     }

//     const x = pts.map(p => toLocal(p.t));
//     const close = pts.map(p => p.close);

//     const ohlcTrace = {
//       type: 'candlestick',
//       x,
//       open: pts.map(p => p.open),
//       high: pts.map(p => p.high),
//       low:  pts.map(p => p.low),
//       close: close,
//       name: 'OHLC'
//     };
//     const lineTrace = { x, y: close, mode: 'lines', name: 'Close' };

//     const layout = {
//       margin: { l:40, r:20, t:30, b:40 },
//       xaxis: { title:'Czas (lokalny)' },
//       yaxis: { title:'Cena' },
//       showlegend: false
//     };
//     Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
//   } catch(e) {
//     console.error(e);
//     Plotly.newPlot('chart', [{
//       x:[toLocal(startUnix)], y:[null], mode:'lines', name:'błąd'
//     }], { title: 'Błąd ładowania danych', margin:{t:40} });
//   }
// }

