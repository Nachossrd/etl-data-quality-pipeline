"""Kit de visualización compartido: estilos + librería de gráficos en SVG.

Aquí vive una sola vez lo que antes estaba embebido en el dashboard del caso
Abarrotes: la paleta, el layout y las funciones de dibujo. Los dashboards
concretos —el especializado de un caso y el genérico auto-perfilado— sólo
aportan su propio guion de armado y comparten estas piezas.

Sin dependencias externas: el HTML resultante se abre desde el disco, sin
servidor ni CDN. Los gráficos se dibujan como SVG con JS plano.

Convenciones de diseño aplicadas (y por qué):
    - Una serie = un color; magnitud en un solo tono; polaridad en paleta
      divergente. El color codifica un significado, no decora.
    - Marcas delgadas con el extremo del dato redondeado y la base cuadrada
      sobre la línea cero.
    - La separación entre marcas es aire (2px del color de fondo), nunca un
      borde: un borde agrega tinta que no es dato.
    - Leyenda siempre que haya 2+ series; etiquetas directas selectivas.
    - El texto nunca lleva el color de la serie — la identidad la da la marca
      de color al lado.
    - Todo gráfico trae su tabla de datos desplegable (accesibilidad y
      verificación).
"""

import json
from typing import Optional

# ─────────────────────────────── estilos ───────────────────────────────────
CSS = r"""
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --grid: #e1e0d9;
    --axis: #c3c2b7;
    --border: rgba(11,11,11,0.10);
    --series-1: #2a78d6;
    --series-2: #eb6834;
    --series-3: #1baf7a;
    --series-4: #eda100;
    --series-5: #e87ba4;
    --series-6: #008300;
    --series-7: #4a3aa7;
    --series-8: #e34948;
    --pos: #2a78d6;
    --neg: #e34948;
    --good: #0ca30c;
    --warning: #fab219;
    --critical: #d03b3b;
    --shadow: 0 1px 2px rgba(11,11,11,.04), 0 8px 24px rgba(11,11,11,.05);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1: #1a1a19;
      --page: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
      --border: rgba(255,255,255,0.10);
      --series-1: #3987e5;
      --series-2: #d95926;
      --series-3: #199e70;
      --series-4: #c98500;
      --series-5: #d55181;
      --series-6: #008300;
      --series-7: #9085e9;
      --series-8: #e66767;
      --pos: #3987e5;
      --neg: #e66767;
      --shadow: none;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --page: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
    --series-5: #d55181;
    --series-6: #008300;
    --series-7: #9085e9;
    --series-8: #e66767;
    --pos: #3987e5;
    --neg: #e66767;
    --shadow: none;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 32px 20px 80px; }

  header.top { margin-bottom: 28px; }
  .eyebrow {
    font-size: 12px; letter-spacing: .10em; text-transform: uppercase;
    color: var(--text-muted); font-weight: 600; margin-bottom: 6px;
  }
  h1 { font-size: clamp(26px, 4vw, 38px); line-height: 1.15; margin: 0 0 8px; font-weight: 650; }
  .sub { color: var(--text-secondary); max-width: 76ch; margin: 0; }

  h2 {
    font-size: 19px; font-weight: 620; margin: 44px 0 4px;
    padding-top: 20px; border-top: 1px solid var(--border);
  }
  h2:first-of-type { border-top: 0; padding-top: 0; }
  .lede { color: var(--text-secondary); margin: 0 0 18px; max-width: 80ch; font-size: 14.5px; }

  .card {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px 18px 14px; box-shadow: var(--shadow);
  }
  .grid { display: grid; gap: 16px; }
  .g2 { grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
  .g3 { grid-template-columns: repeat(auto-fit, minmax(215px, 1fr)); }
  .g4 { grid-template-columns: repeat(auto-fit, minmax(196px, 1fr)); }

  .hero { display: flex; flex-wrap: wrap; gap: 28px; align-items: flex-end; }
  .hero-fig { font-size: clamp(44px, 7vw, 62px); font-weight: 660; letter-spacing: -.02em; line-height: 1; }
  .hero-label { color: var(--text-secondary); font-size: 14px; margin-top: 6px; }

  .tile-label { font-size: 12.5px; color: var(--text-secondary); margin-bottom: 4px; }
  .tile-value { font-size: 24px; font-weight: 640; letter-spacing: -.01em; white-space: nowrap; }
  .tile-note { font-size: 12px; color: var(--text-muted); margin-top: 3px; }
  .delta { font-size: 13px; font-weight: 600; margin-top: 4px; }
  .delta.up { color: var(--good); }
  .delta.down { color: var(--critical); }

  .chart-title { font-size: 14.5px; font-weight: 600; margin: 0 0 2px; }
  .chart-sub { font-size: 12.5px; color: var(--text-muted); margin: 0 0 12px; }
  .chart { width: 100%; position: relative; }
  svg { display: block; width: 100%; overflow: visible; }
  .legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 10px 0 0; font-size: 12.5px; color: var(--text-secondary); }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .key { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .key.line { width: 14px; height: 2px; border-radius: 2px; }

  table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
  th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; white-space: normal; }
  thead th { color: var(--text-secondary); font-weight: 600; font-size: 12px; }
  tbody tr:last-child td { border-bottom: 0; }
  .scroll { overflow-x: auto; }

  details { margin-top: 12px; }
  summary {
    cursor: pointer; font-size: 12.5px; color: var(--text-secondary);
    list-style: none; display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 0; user-select: none;
  }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: "\25B8"; font-size: 10px; color: var(--text-muted); }
  details[open] summary::before { content: "\25BE"; }

  .callout {
    border-left: 3px solid var(--series-1); background: var(--surface-1);
    border-radius: 0 10px 10px 0; padding: 12px 16px; margin: 14px 0;
    font-size: 14px; color: var(--text-secondary);
  }
  .callout.warn { border-left-color: var(--warning); }
  .callout strong { color: var(--text-primary); }

  .pill {
    display: inline-flex; align-items: center; gap: 5px; font-size: 12px;
    padding: 3px 9px; border-radius: 999px; border: 1px solid var(--border);
    color: var(--text-secondary); background: var(--surface-1);
  }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
  .meta-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }

  .role { font-size: 11px; padding: 2px 7px; border-radius: 4px; border: 1px solid var(--border); color: var(--text-secondary); }

  a.cardlink { text-decoration: none; color: inherit; display: block; transition: transform .08s, box-shadow .08s; }
  a.cardlink:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(11,11,11,.10); }

  #tip {
    position: fixed; pointer-events: none; opacity: 0; transition: opacity .08s;
    background: var(--surface-1); color: var(--text-primary);
    border: 1px solid var(--border); border-radius: 8px; padding: 8px 11px;
    font-size: 12.5px; box-shadow: 0 4px 16px rgba(0,0,0,.16); z-index: 50;
    font-variant-numeric: tabular-nums; max-width: 260px;
  }
  #tip b { display: block; font-size: 12px; color: var(--text-secondary); font-weight: 600; margin-bottom: 3px; }
  footer { margin-top: 56px; padding-top: 18px; border-top: 1px solid var(--border);
           color: var(--text-muted); font-size: 12.5px; }
  @media print { .card { break-inside: avoid; } details { display: none; } }
"""

# ─────────────────────────── librería de gráficos ──────────────────────────
JS_CORE = r"""
/* ── formato ─────────────────────────────────────────────────────────── */
const nf = new Intl.NumberFormat('es-CL');
const nf1 = new Intl.NumberFormat('es-CL', {minimumFractionDigits:1, maximumFractionDigits:1});
const nf2 = new Intl.NumberFormat('es-CL', {minimumFractionDigits:2, maximumFractionDigits:2});
const num = v => nf.format(Math.round(v ?? 0));
const clp = v => '$' + nf.format(Math.round(v ?? 0));
const mm = v => 'MM$ ' + nf1.format((v ?? 0) / 1e6);
const mmShort = v => nf.format(Math.round((v ?? 0) / 1e6));
const pct = (v, d=1) => (v ?? 0).toFixed(d).replace('.', ',') + '%';
const signed = (v, d=1) => (v > 0 ? '+' : '') + pct(v, d);
const MESES = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
const mLabel = p => {
  const m = /^(\d{4})-(\d{2})$/.exec(String(p));
  return m ? MESES[+m[2]-1] + ' ' + m[1].slice(2) : String(p);
};
/* Compacta cualquier magnitud sin saber su unidad: 1.284 / 12,9 K / 4,2 M */
const compact = v => {
  const a = Math.abs(v ?? 0);
  if (a >= 1e9) return nf1.format(v/1e9) + ' B';
  if (a >= 1e6) return nf1.format(v/1e6) + ' M';
  if (a >= 1e4) return nf1.format(v/1e3) + ' K';
  if (a >= 100) return nf.format(Math.round(v));
  if (Number.isInteger(v)) return nf.format(v);
  return nf2.format(v);
};
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const trunc = (s, n) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n-1) + '…' : s; };

const SVG = 'http://www.w3.org/2000/svg';
const el = (tag, attrs = {}, parent = null) => {
  const n = document.createElementNS(SVG, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(n);
  return n;
};
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

/* Tinta legible sobre un relleno: blanco o negro segun luminancia relativa. */
function inkOn(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
  if (!m) return '#fff';
  const n = parseInt(m[1], 16);
  const lin = c => { c /= 255; return c <= .03928 ? c/12.92 : Math.pow((c+.055)/1.055, 2.4); };
  const L = .2126*lin(n>>16 & 255) + .7152*lin(n>>8 & 255) + .0722*lin(n & 255);
  return L > 0.35 ? '#0b0b0b' : '#ffffff';
}

/* ── tooltip compartido ──────────────────────────────────────────────── */
const tip = document.getElementById('tip');
function showTip(evt, html) {
  tip.innerHTML = html;
  tip.style.opacity = 1;
  const r = tip.getBoundingClientRect();
  let x = evt.clientX + 14, y = evt.clientY - r.height - 10;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
  if (y < 8) y = evt.clientY + 18;
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}
const hideTip = () => { tip.style.opacity = 0; };

/* ── ejes ────────────────────────────────────────────────────────────── */
function niceTicks(max, count = 4) {
  if (max <= 0) return [0];
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].find(s => s * mag >= raw) * mag;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks;
}
function yAxis(g, ticks, scale, w, fmt) {
  ticks.forEach(t => {
    const y = scale(t);
    el('line', {x1:0, x2:w, y1:y, y2:y, stroke: css('--grid'), 'stroke-width':1}, g);
    const lb = el('text', {x:-8, y:y+4, 'text-anchor':'end', fill: css('--text-muted'),
                           'font-size':11}, g);
    lb.textContent = fmt(t);
  });
}

/* ── gráfico de líneas ───────────────────────────────────────────────── */
function lineChart(host, {series, fmtY, fmtTip, labelEvery = 6, labelFn = mLabel}) {
  host.innerHTML = '';
  const W = host.clientWidth || 700, H = 300;
  const m = {t: 14, r: 16, b: 30, l: 62};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const svg = el('svg', {viewBox:`0 0 ${W} ${H}`, role:'img'}, host);
  const g = el('g', {transform:`translate(${m.l},${m.t})`}, svg);

  // Eje continuo cuando las etiquetas son meses AAAA-MM: si falta un mes, el
  // hueco tiene que verse. Comprimirlo lo escondería.
  const all = series.flatMap(s => s.points.map(p => p.x)).sort();
  let labels = [];
  if (all.length && /^\d{4}-\d{2}$/.test(String(all[0]))) {
    for (let d = new Date(all[0] + '-01T00:00:00'),
             end = new Date(all[all.length-1] + '-01T00:00:00'); d <= end;
         d.setMonth(d.getMonth() + 1)) {
      labels.push(d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0'));
    }
  } else {
    labels = [...new Set(all)];
  }

  const max = Math.max(...series.flatMap(s => s.points.map(p => p.y))) * 1.08;
  const X = i => labels.length > 1 ? (i / (labels.length - 1)) * iw : iw / 2;
  const Y = v => ih - (v / max) * ih;

  yAxis(g, niceTicks(max), Y, iw, fmtY);
  // El salto pedido es un mínimo, no la última palabra: en un gráfico angosto
  // (tres por fila) hay que espaciar más o las etiquetas se encabalgan.
  const gap = iw / Math.max(1, labels.length - 1);
  const maxChars = Math.max(...labels.map(l => String(labelFn(l)).length));
  const every = Math.max(labelEvery, Math.ceil((maxChars * 6.4 + 14) / gap));
  labels.forEach((lb, i) => {
    if (i % every !== 0 && i !== labels.length - 1) return;
    const t = el('text', {x:X(i), y:ih+20, 'text-anchor':'middle',
                          fill: css('--text-muted'), 'font-size':11}, g);
    t.textContent = labelFn(lb);
  });
  el('line', {x1:0, x2:iw, y1:ih, y2:ih, stroke: css('--axis'), 'stroke-width':1}, g);

  series.forEach(s => {
    const by = new Map(s.points.map(p => [p.x, p.y]));
    let d = '', pen = false, last = null;
    labels.forEach((lb, i) => {
      if (!by.has(lb)) { pen = false; return; }
      const x = X(i), y = Y(by.get(lb));
      d += (pen ? 'L' : 'M') + x + ' ' + y + ' ';
      pen = true; last = [x, y];
    });
    el('path', {d: d.trim(), fill:'none', stroke: css(s.color), 'stroke-width':2,
                'stroke-linejoin':'round', 'stroke-linecap':'round',
                ...(s.dashed ? {'stroke-dasharray':'6 5'} : {})}, g);
    if (last) el('circle', {cx:last[0], cy:last[1], r:4.5, fill: css(s.color),
                            stroke: css('--surface-1'), 'stroke-width':2}, g);
  });

  const cross = el('line', {y1:0, y2:ih, stroke: css('--axis'), 'stroke-width':1, opacity:0}, g);
  labels.forEach((lb, i) => {
    const bw = iw / labels.length;
    const r = el('rect', {x: X(i) - bw/2, y:0, width:bw, height:ih, fill:'transparent'}, g);
    r.addEventListener('mousemove', e => {
      cross.setAttribute('x1', X(i)); cross.setAttribute('x2', X(i));
      cross.setAttribute('opacity', .5);
      const rows = series.map(s => {
        const p = s.points.find(p => p.x === lb);
        if (!p) return '';
        return `<div><span class="dot" style="background:${css(s.color)}"></span>
                ${esc(s.name)}: <strong>${fmtTip(p.y)}</strong></div>`;
      }).join('');
      showTip(e, `<b>${esc(labelFn(lb))}</b>${rows || '<div>Sin datos</div>'}`);
    });
    r.addEventListener('mouseleave', () => { cross.setAttribute('opacity', 0); hideTip(); });
  });
}

/* ── columnas (una serie, o divergente por signo) ────────────────────── */
function columnChart(host, {data, fmtY, fmtTip, refLine = null, diverging = false, baseline = 0}) {
  host.innerHTML = '';
  const W = host.clientWidth || 700, H = 260;
  const m = {t: 16, r: 12, b: 34, l: 58};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const svg = el('svg', {viewBox:`0 0 ${W} ${H}`, role:'img'}, host);
  const g = el('g', {transform:`translate(${m.l},${m.t})`}, svg);

  const vals = data.map(d => d.value);
  const lo = diverging ? Math.min(baseline, ...vals) : 0;
  const hi = Math.max(...vals) * (diverging ? 1.02 : 1.1) || 1;
  const Y = v => ih - ((v - lo) / (hi - lo)) * ih;

  if (diverging) {
    el('line', {x1:0, x2:iw, y1:Y(baseline), y2:Y(baseline),
                stroke: css('--axis'), 'stroke-width':1}, g);
    [lo, baseline, hi].forEach(t => {
      const lb = el('text', {x:-8, y:Y(t)+4, 'text-anchor':'end',
                             fill: css('--text-muted'), 'font-size':11}, g);
      lb.textContent = fmtY(t);
    });
  } else {
    yAxis(g, niceTicks(hi), Y, iw, fmtY);
    el('line', {x1:0, x2:iw, y1:ih, y2:ih, stroke: css('--axis'), 'stroke-width':1}, g);
  }

  const band = iw / data.length;
  const bw = Math.min(24, band * 0.62);
  // Cuántas etiquetas caben de verdad: se estima el ancho del texto más largo
  // contra el ancho de banda disponible. Un salto fijo funciona con "Ene" y
  // encabalga con "2019-01".
  const maxChars = Math.max(...data.map(d => trunc(d.label, 12).length));
  const showEvery = Math.max(1, Math.ceil((maxChars * 6.4 + 10) / band));
  data.forEach((d, i) => {
    const x = i * band + (band - bw) / 2;
    const y0 = Y(diverging ? baseline : 0), y1 = Y(d.value);
    const top = Math.min(y0, y1), h = Math.max(1, Math.abs(y1 - y0));
    const up = d.value >= baseline;
    const color = css(diverging ? (up ? '--pos' : '--neg') : (d.color || '--series-1'));
    const r = Math.min(4, bw / 2);
    const path = up
      ? `M${x},${top+h} L${x},${top+r} Q${x},${top} ${x+r},${top} L${x+bw-r},${top}
         Q${x+bw},${top} ${x+bw},${top+r} L${x+bw},${top+h} Z`
      : `M${x},${top} L${x},${top+h-r} Q${x},${top+h} ${x+r},${top+h} L${x+bw-r},${top+h}
         Q${x+bw},${top+h} ${x+bw},${top+h-r} L${x+bw},${top} Z`;
    el('path', {d: path, fill: color}, g);

    if (i % showEvery === 0) {
      const t = el('text', {x:i*band + band/2, y:ih+20, 'text-anchor':'middle',
                            fill: css('--text-muted'), 'font-size':11}, g);
      t.textContent = trunc(d.label, 12);
    }
    const hit = el('rect', {x:i*band, y:0, width:band, height:ih, fill:'transparent'}, g);
    hit.addEventListener('mousemove', e =>
      showTip(e, `<b>${esc(d.full || d.label)}</b>${fmtTip(d.value, d)}`));
    hit.addEventListener('mouseleave', hideTip);
  });

  if (refLine !== null) {
    el('line', {x1:0, x2:iw, y1:Y(refLine), y2:Y(refLine), stroke: css('--axis'),
                'stroke-width':1}, g);
  }
}

/* ── barras horizontales (magnitud, un solo tono) ────────────────────── */
function barChart(host, {data, fmtVal, fmtTip, color = '--series-1'}) {
  host.innerHTML = '';
  const W = host.clientWidth || 700;
  const rowH = 30, m = {t: 6, r: 108, b: 6};
  const labelW = Math.min(230, Math.max(110, W * 0.32));
  const H = data.length * rowH + m.t + m.b;
  const iw = Math.max(40, W - labelW - m.r);
  const svg = el('svg', {viewBox:`0 0 ${W} ${H}`, role:'img'}, host);
  const g = el('g', {transform:`translate(${labelW},${m.t})`}, svg);
  const max = Math.max(...data.map(d => d.value)) * 1.02 || 1;

  data.forEach((d, i) => {
    const y = i * rowH, bh = Math.min(18, rowH - 12);
    const w = Math.max(2, (d.value / max) * iw);
    const r = Math.min(4, w / 2);
    el('path', {d: `M0,${y+6} L${w-r},${y+6} Q${w},${y+6} ${w},${y+6+r}
                    L${w},${y+6+bh-r} Q${w},${y+6+bh} ${w-r},${y+6+bh} L0,${y+6+bh} Z`,
                fill: css(d.color || color)}, g);
    const lb = el('text', {x:-12, y:y+6+bh/2+4, 'text-anchor':'end',
                           fill: css('--text-secondary'), 'font-size':12.5}, g);
    lb.textContent = trunc(d.label, 34);
    const vl = el('text', {x:w+10, y:y+6+bh/2+4, fill: css('--text-primary'),
                           'font-size':12.5, 'font-variant-numeric':'tabular-nums'}, g);
    vl.textContent = fmtVal(d.value, d);

    const hit = el('rect', {x:-labelW, y:y, width:W, height:rowH, fill:'transparent'}, g);
    hit.addEventListener('mousemove', e => showTip(e, `<b>${esc(d.label)}</b>${fmtTip(d.value, d)}`));
    hit.addEventListener('mouseleave', hideTip);
  });
}

/* ── barra 100% apilada (composición) ────────────────────────────────── */
function stackedBar(host, {data, fmtTip}) {
  host.innerHTML = '';
  const W = host.clientWidth || 700, H = 56;
  const svg = el('svg', {viewBox:`0 0 ${W} ${H}`, role:'img'}, host);
  const total = data.reduce((a, d) => a + d.value, 0) || 1;
  const GAP = 2;                       // el separador es aire, no un borde
  let x = 0;
  data.forEach((d, i) => {
    const w = Math.max(2, (d.value / total) * W - (i < data.length - 1 ? GAP : 0));
    const first = i === 0, last = i === data.length - 1;
    el('rect', {x, y:8, width:w, height:30, fill: css(d.color),
                rx: (first || last) ? 4 : 0}, svg);
    if (w > 54) {
      const t = el('text', {x:x+w/2, y:28, 'text-anchor':'middle', fill: inkOn(css(d.color)),
                            'font-size':12, 'font-weight':600}, svg);
      t.textContent = (d.value / total * 100).toFixed(1).replace('.', ',') + '%';
    }
    const hit = el('rect', {x, y:0, width:w, height:H, fill:'transparent'}, svg);
    hit.addEventListener('mousemove', e => showTip(e, `<b>${esc(d.label)}</b>${fmtTip(d.value, d)}`));
    hit.addEventListener('mouseleave', hideTip);
    x += w + GAP;
  });
}

/* ── dispersión (relación entre dos medidas) ─────────────────────────── */
function scatterChart(host, {points, xLabel, yLabel, fmtX, fmtY, fmtTip}) {
  host.innerHTML = '';
  const W = host.clientWidth || 700, H = 300;
  const m = {t: 14, r: 18, b: 42, l: 62};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const svg = el('svg', {viewBox:`0 0 ${W} ${H}`, role:'img'}, host);
  const g = el('g', {transform:`translate(${m.l},${m.t})`}, svg);

  const xs = points.map(p => p.x), ys = points.map(p => p.y);
  const xMax = Math.max(...xs) * 1.06 || 1, yMax = Math.max(...ys) * 1.06 || 1;
  const X = v => (v / xMax) * iw, Y = v => ih - (v / yMax) * ih;

  yAxis(g, niceTicks(yMax), Y, iw, fmtY);
  niceTicks(xMax).forEach(t => {
    const lb = el('text', {x:X(t), y:ih+20, 'text-anchor':'middle',
                           fill: css('--text-muted'), 'font-size':11}, g);
    lb.textContent = fmtX(t);
  });
  el('line', {x1:0, x2:iw, y1:ih, y2:ih, stroke: css('--axis'), 'stroke-width':1}, g);
  const ax = el('text', {x:iw/2, y:ih+38, 'text-anchor':'middle',
                         fill: css('--text-secondary'), 'font-size':12}, g);
  ax.textContent = xLabel;
  const ay = el('text', {x:-46, y:-4, fill: css('--text-secondary'), 'font-size':12}, g);
  ay.textContent = yLabel;

  points.forEach(p => {
    const c = el('circle', {cx:X(p.x), cy:Y(p.y), r:5, fill: css('--series-1'),
                            stroke: css('--surface-1'), 'stroke-width':2,
                            'fill-opacity':.85}, g);
    c.addEventListener('mousemove', e => showTip(e, `<b>${esc(p.label)}</b>${fmtTip(p)}`));
    c.addEventListener('mouseleave', hideTip);
  });
}

/* ── helpers de layout ───────────────────────────────────────────────── */
/* Devuelve un fragmento cuando el HTML trae varios elementos hermanos: con
   `firstElementChild` se perdía todo menos el primero, y los párrafos que
   acompañan a cada título desaparecían sin aviso. */
const h = (html) => {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.childElementCount > 1 ? t.content : t.content.firstElementChild;
};
const app = document.getElementById('app');
const registry = [];

function card(title, sub, {legend = '', table = '', note = ''} = {}) {
  const c = h(`<div class="card">
    <p class="chart-title">${title}</p>
    <p class="chart-sub">${sub}</p>
    <div class="chart"></div>
    ${legend ? `<div class="legend">${legend}</div>` : ''}
    ${note ? `<p class="chart-sub" style="margin-top:10px">${note}</p>` : ''}
    ${table ? `<details><summary>Ver datos</summary><div class="scroll">${table}</div></details>` : ''}
  </div>`);
  return {node: c, chart: c.querySelector('.chart')};
}
function draw(host, fn) { registry.push({host, fn}); fn(host); }
function table(cols, rows) {
  return `<table><thead><tr>${cols.map(c => `<th>${c}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(r => `<tr>${r.map(v => `<td>${v}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const keyLine = (c, n) => `<span><i class="key line" style="background:${css(c)}"></i>${esc(n)}</span>`;
const keyBox = (c, n) => `<span><i class="key" style="background:${css(c)}"></i>${esc(n)}</span>`;
const SERIES = ['--series-1','--series-2','--series-3','--series-4',
                '--series-5','--series-6','--series-7','--series-8'];
"""

JS_RESIZE = r"""
/* ── redibujado responsivo y por cambio de tema ──────────────────────── */
let raf;
addEventListener('resize', () => {
  cancelAnimationFrame(raf);
  raf = requestAnimationFrame(() => registry.forEach(r => r.fn(r.host)));
});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',
  () => registry.forEach(r => r.fn(r.host)));
"""


def render_page(*, title: str, header_html: str, build_js: str,
                data: Optional[dict] = None, footer_html: str = "",
                standalone: bool = True) -> str:
    """Ensambla un documento completo con el kit + el guion del dashboard.

    `standalone=True` incluye doctype y `<html>`: es lo que se necesita para
    abrir el archivo desde el disco (sin doctype el navegador entra en quirks
    mode y el layout se degrada).
    """
    data_js = json.dumps(data or {}, ensure_ascii=False)
    body = f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{CSS}</style>

<div class="wrap">
{header_html}
  <main id="app"></main>
  <footer>{footer_html}</footer>
</div>
<div id="tip" role="status" aria-live="polite"></div>

<script>
const DATA = {data_js};
{JS_CORE}
{build_js}
{JS_RESIZE}
</script>
"""
    if not standalone:
        return body
    return f'<!doctype html>\n<html lang="es">\n{body}\n</html>\n'
