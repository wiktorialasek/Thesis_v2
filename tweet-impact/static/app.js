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
  preMinutes: 10,

  // Etykietowanie
  useImp: 0,           // 0 = pokazuj precompute; 1 = licz w locie wg lab-*
  impMin: 8,
  impThr: 1.0,

  selected: new Set(),
  label: 'all',
  sortByChange: false
};

// ===== RENDER: list =====
async function loadFiltersAndList(initial=false){
  const params = {
    page: state.page, per_page: state.per_page,
    year: state.year, reply: state.reply, retweet: state.retweet, quote: state.quote, q: state.q,
    label: state.label 
  };
  // Jeśli sortujemy „biggest change”, wymuś globalny zasięg i większą stronę
  if (state.sortByChange && (state.label === 'up' || state.label === 'down')) {
    params.year = 'all';
    params.imp_sort = 1;
    params.page = 1;                 // pobierz pierwszy „pakiet”
    params.per_page = Math.max(state.per_page,200); // backend i tak przytnie do 100
    state.year = 'all';
    state.page = 1;
    state.per_page = params.per_page;
    const selYear = document.getElementById('f-year'); if (selYear) selYear.value = 'all';
  
  }


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

  // === NOWY KOD: przygotowanie sortowania „biggest change” ===
  const getPct = it => {
    if (it.imp_pct != null) return it.imp_pct;  // liczone w locie
    if (it.pre_pct != null) return it.pre_pct;  // precompute
    if (it.lab_pct != null) return it.lab_pct;  // fallback
    return null;
  };

  let itemsForRender = data.items.slice();

  if (state.sortByChange && (state.label === 'up' || state.label === 'down')) {
    itemsForRender.sort((a, b) => {
      const pa = getPct(a), pb = getPct(b);
      const aBad = (pa == null || !isFinite(pa));
      const bBad = (pb == null || !isFinite(pb));
      if (aBad && bBad) return 0;
      if (aBad) return 1;   // braki na koniec
      if (bBad) return -1;
      // up: malejąco; down: rosnąco
      return (state.label === 'up') ? (pb - pa) : (pa - pb);
    });
  }
  // === KONIEC NOWEGO KODU ===



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
  const start = (params.page-1)*params.per_page + 1;
  const end = Math.min(params.page*params.per_page, state.total);
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

    // BEZPIECZNIE: jeśli #f-label nie istnieje (bo używasz checkboxów), nie nadpisuj state.label
    const labSel = document.getElementById('f-label');
    if (labSel) {
      state.label = labSel.value || 'all';
    }
  };

  // const readBasics = ()=>{
  //   state.year    = document.getElementById('f-year').value || 'all';
  //   state.q       = (document.getElementById('f-q').value || '').trim();
  //   state.reply   = document.getElementById('f-reply').checked ? -1 : 0;
  //   state.retweet = document.getElementById('f-retweet').checked ? -1 : 0;
  //   state.quote   = document.getElementById('f-quote').checked ? -1 : 0;
  //   state.label   = document.getElementById('f-label').value || 'all';
  // };
  document.getElementById('btn-search').addEventListener('click', ()=>{
    readBasics();
    state.page = 1;

    // jeśli sort był włączony, wyłącz go i zdejmij podświetlenie przycisku
    if (state.sortByChange) {
      state.sortByChange = false;
      const btnSort = document.getElementById('btn-sort');
      if (btnSort) btnSort.classList.remove('primary');
    }

    loadFiltersAndList(false);
  });

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

    // --- checkboksy etykiet up/down/neutral (działają: dokładnie JEDEN = filtr) ---
  const ckUp  = document.getElementById('f-up');
  const ckDn  = document.getElementById('f-down');
  const ckNe  = document.getElementById('f-neutral');

  function applyLabelCheckboxes(){
    const picks = [];
    if (ckUp && ckUp.checked) picks.push('up');
    if (ckDn && ckDn.checked) picks.push('down');
    if (ckNe && ckNe.checked) picks.push('neutral');

    state.label = (picks.length === 1 ? picks[0] : 'all');
    state.page = 1;
    loadFiltersAndList(false);
  }

  [ckUp, ckDn, ckNe].forEach(el=>{
    if (el) el.addEventListener('change', applyLabelCheckboxes);
  });

    // --- sort „biggest change” w kontekście up/down ---
  const btnSort = document.getElementById('btn-sort');
  if (btnSort) {
  btnSort.addEventListener('click', () => {
    state.sortByChange = !state.sortByChange;
    btnSort.classList.toggle('primary', state.sortByChange);

    if (state.sortByChange) {
      // 1) Wymuś „wszystkie lata”
      state.year = 'all';
      const selYear = document.getElementById('f-year');
      if (selYear) selYear.value = 'all';

      // 2) Pokaż więcej rekordów na stronie (max 100 wg backendu)
      state.per_page = Math.max(state.per_page, 100);

      // 3) Zacznij od 1. strony
      state.page = 1;
    }

    loadFiltersAndList(false);
  });
}


  
  // start
  loadFiltersAndList(true);
});

