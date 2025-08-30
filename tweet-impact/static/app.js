async function fetchWindow(startUnix, minutes) {
  const params = new URLSearchParams({ start: String(startUnix), minutes: String(minutes) });
  const res = await fetch(`/api/price?${params.toString()}`);
  if (!res.ok) throw new Error('API error');
  return res.json();
}
function toLocal(tsSec){ return new Date(tsSec * 1000); }

async function renderChartFromConfig() {
  if (!window.APP_CONFIG) return;
  const { startUnix, minutes } = window.APP_CONFIG;

  try {
    const payload = await fetchWindow(startUnix, minutes);
    const pts = payload.points || [];
    const reason = payload.reason || "ok";
    const usedStart = payload.used_start || startUnix;

    // komunikat nad wykresem (fallback/poza sesją)
    const chartEl = document.getElementById('chart');
    if (reason === "fallback_next") {
      const msg = document.createElement('div');
      msg.className = 'text-muted mb-2';
      msg.textContent = 'Brak notowań w czasie tweeta (poza sesją). Pokazuję najbliższe dostępne 15 min od: ' + toLocal(usedStart).toLocaleString();
      chartEl.parentElement.insertBefore(msg, chartEl);
    } else if (!pts.length) {
      Plotly.newPlot('chart', [{
        x: [toLocal(startUnix)], y: [null], mode: 'lines', name: 'brak danych'
      }], { title: 'Brak danych w tym oknie (poza sesją?)', margin:{t:40} });
      return;
    }

    const x = pts.map(p => toLocal(p.t));
    const close = pts.map(p => p.close);

    const ohlcTrace = {
      type: 'candlestick',
      x,
      open: pts.map(p => p.open),
      high: pts.map(p => p.high),
      low:  pts.map(p => p.low),
      close: close,
      name: 'OHLC'
    };
    const lineTrace = { x, y: close, mode: 'lines', name: 'Close' };

    const layout = {
      margin: { l:40, r:20, t:30, b:40 },
      xaxis: { title:'Czas (lokalny)' },
      yaxis: { title:'Cena' },
      showlegend: false
    };
    Plotly.newPlot('chart', [ohlcTrace, lineTrace], layout, {responsive:true});
  } catch(e) {
    console.error(e);
    Plotly.newPlot('chart', [{
      x:[toLocal(startUnix)], y:[null], mode:'lines', name:'błąd'
    }], { title: 'Błąd ładowania danych', margin:{t:40} });
  }
}

