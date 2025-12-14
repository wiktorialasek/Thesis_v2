async function apiPrice(startUnix, minutes, pre){
  const r = await fetch('/api/price?' + new URLSearchParams({
    start: String(startUnix),
    minutes: String(minutes),
    pre: String(pre || 0),
  }));
  if(!r.ok) throw new Error('price api');
  return r.json();
}

function toDate(tsSec){ return new Date(tsSec * 1000); }

function nearestPoint(points, targetSec){
  if(!points || !points.length) return null;
  let best = null;
  let bestD = Infinity;
  for (const p of points){
    const d = Math.abs(p.t - targetSec);
    if (d < bestD){
      bestD = d;
      best = p;
    }
  }
  return best;
}

function fmtPL(tsSec){
  return new Date(tsSec*1000).toLocaleString('pl-PL', { timeZone: 'Europe/Warsaw' });
}

async function renderTradeChart({entryTs, exitTs, decision, tweetId, text}){
  const holdMin = Math.max(1, Math.round((exitTs - entryTs) / 60));
  // Okno ~6 minut: 2 min przed wejściem + (hold 3 min) + 1 min po wyjściu = 6
  const PRE_MIN = 2;
  const POST_MIN = 1;
  const minutesAfterStart = holdMin + POST_MIN;

  const titleEl = document.getElementById('bt-chart-title');
  const metaEl = document.getElementById('bt-chart-meta');

  if (titleEl) titleEl.textContent = `Transakcja #${tweetId} (${decision})`;
  if (metaEl) metaEl.textContent = `${fmtPL(entryTs)} → ${fmtPL(exitTs)} | ${text || ''}`;

  const payload = await apiPrice(entryTs, minutesAfterStart, PRE_MIN);
  const pts = payload.points || [];

  if (!pts.length){
    Plotly.newPlot('bt-chart', [{
      x: [toDate(entryTs)],
      y: [null],
      mode: 'lines',
      name: 'brak danych'
    }], {
      margin:{l:40,r:20,t:20,b:40},
      title: 'Brak danych cenowych w tym oknie',
      xaxis:{title:'Czas (lokalny)'},
      yaxis:{title:'Cena'}
    }, {responsive:true});
    return;
  }

  const x = pts.map(p => toDate(p.t));
  const y = pts.map(p => p.close);

  const entryP = nearestPoint(pts, entryTs);
  const exitP  = nearestPoint(pts, exitTs);

  // kolory markerów zależnie od kierunku (buy: wejście zielone / wyjście czerwone; sell odwrotnie)
  const entryIsBuy = (decision === 'buy');
  const entryLabel = entryIsBuy ? 'Kupno' : 'Sprzedaż';
  const exitLabel  = entryIsBuy ? 'Sprzedaż' : 'Kupno';

  const entryTrace = (entryP && entryP.close != null) ? [{
    x: [toDate(entryP.t)],
    y: [entryP.close],
    mode: 'markers',
    name: entryLabel,
    marker: { size: 10, color: entryIsBuy ? 'green' : 'red' }
  }] : [];

  const exitTrace = (exitP && exitP.close != null) ? [{
    x: [toDate(exitP.t)],
    y: [exitP.close],
    mode: 'markers',
    name: exitLabel,
    marker: { size: 10, color: entryIsBuy ? 'red' : 'green' }
  }] : [];

  const priceTrace = {
    x, y,
    mode: 'lines',
    name: 'Cena'
  };

  Plotly.newPlot('bt-chart', [priceTrace, ...entryTrace, ...exitTrace], {
    margin:{l:40,r:20,t:20,b:40},
    xaxis:{title:'Czas (lokalny)'},
    yaxis:{title:'Cena'},
    showlegend: true,
    shapes: [
      // linia wejścia
      {
        type:'line',
        x0: toDate(entryTs), x1: toDate(entryTs),
        y0: 0, y1: 1,
        xref:'x', yref:'paper',
        line:{dash:'dot', width:2}
      },
      // linia wyjścia
      {
        type:'line',
        x0: toDate(exitTs), x1: toDate(exitTs),
        y0: 0, y1: 1,
        xref:'x', yref:'paper',
        line:{dash:'dot', width:2}
      }
    ]
  }, {responsive:true});
}

window.addEventListener('DOMContentLoaded', () => {
  const rows = Array.from(document.querySelectorAll('.trade-row'));
  if (!rows.length) return;

  function activateRow(row){
    rows.forEach(r => r.classList.remove('active'));
    row.classList.add('active');
  }

  rows.forEach(row => {
    row.addEventListener('click', async () => {
      try{
        activateRow(row);
        const entryTs = parseInt(row.dataset.entryTs, 10);
        const exitTs = parseInt(row.dataset.exitTs, 10);
        const decision = (row.dataset.decision || '').toLowerCase();
        const tweetId = row.dataset.tweetId || '';
        const text = row.dataset.text || '';
        await renderTradeChart({entryTs, exitTs, decision, tweetId, text});
      } catch (e){
        console.error(e);
      }
    });
  });

  // auto: pokaż wykres dla pierwszej transakcji
  rows[0].click();
});
