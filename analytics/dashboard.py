"""Dashboard especializado del caso Abarrotes CL.

Aporta la narrativa del caso — concentración de cadenas, estacionalidad de
legumbres, plan de cotización — sobre el kit compartido de `analytics/viz_kit.py`.
Para cualquier dataset sin dashboard especializado existe el genérico
auto-perfilado en `analytics/generic_dashboard.py`.

Entrada: el `insights.json` de `analytics/abarrotes_bi.py`.
Salida: un HTML autocontenido, sin servidor ni dependencias externas.
"""

import argparse
import json
import os

from analytics.viz_kit import render_page

MATCH_COLUMNS = {"venta_clp", "grupo_cliente", "producto", "comuna"}

HEADER = """  <header class="top">
    <div class="eyebrow">Abarrotes CL · Categoría abarrotes</div>
    <h1>Ventas 2019-2022 y proyección de demanda __ANIO_PROY__</h1>
    <p class="sub">Análisis construido sobre __FILAS__ registros de venta mensual, limpiados y
    auditados por el pipeline de calidad de datos. Cada cifra de este tablero se puede rastrear
    hasta la fila de origen: la trazabilidad está en el reporte de calidad del run.</p>
    <div class="meta-row" id="meta"></div>
  </header>"""

FOOTER = """<div>MM$ = millones de pesos chilenos. Montos en CLP nominales (sin ajuste por inflación).</div>
    <div>Generado por el pipeline ETL + capa analítica · run <code id="runid"></code> · <span id="gen"></span></div>"""

BUILD_JS = r"""
// "Region de la Araucanía" -> "Araucanía"; deja intacto "Metropolitana de Santiago".
const shortRegion = r => r.replace(/^Region\s+/i, '').replace(/^(de la|del|de)\s+/i, '');

const K = DATA.kpis, M = DATA.meta, P = DATA.proyeccion;

document.getElementById('runid').textContent = M.run_id;
document.getElementById('gen').textContent = M.generado;
document.getElementById('meta').innerHTML = [
  `<span class="pill">${M.periodo_inicio} → ${M.periodo_fin}</span>`,
  `<span class="pill">${num(M.filas_limpias)} filas limpias</span>`,
  `<span class="pill"><span class="dot" style="background:var(--good)"></span>${pct(100 - M.ratio_cuarentena*100, 3)} de datos utilizables</span>`,
  M.meses_faltantes.length ? `<span class="pill"><span class="dot" style="background:var(--warning)"></span>${M.meses_faltantes.length} mes sin datos: ${M.meses_faltantes.join(', ')}</span>` : ''
].join('');

/* 1 · Titulares */
app.appendChild(h(`<h2>1 · Los números del período</h2>
  <p class="lede">Cuatro años de venta a las cuatro grandes cadenas del retail chileno.
  La cifra principal es la venta acumulada 2019-2022.</p>`));
const anios = DATA.por_anio.slice().sort((a,b) => a.anio - b.anio);
const ultimo = anios[anios.length - 1];
app.appendChild(h(`<div class="card hero">
  <div>
    <div class="hero-fig">${mm(K.venta_total)}</div>
    <div class="hero-label">Venta acumulada 2019-2022 · ${num(K.unidades_total)} unidades</div>
  </div>
  <div style="flex:1 1 240px">
    <div class="tile-label">Margen bruto acumulado</div>
    <div class="tile-value">${mm(K.margen_total)}</div>
    <div class="tile-note">${pct(K.margen_pct)} sobre venta</div>
  </div>
</div>`));

app.appendChild(h(`<div class="grid g4" style="margin-top:16px">${[
  ['Venta ' + ultimo.anio, mm(ultimo.venta), signed(ultimo.venta_yoy_pct) + ' vs ' + (ultimo.anio-1), ultimo.venta_yoy_pct >= 0],
  ['Unidades ' + ultimo.anio, num(ultimo.unidades), signed(ultimo.unidades_yoy_pct) + ' vs ' + (ultimo.anio-1), ultimo.unidades_yoy_pct >= 0],
  ['Precio medio por unidad', clp(K.precio_promedio_unidad), 'costo ' + clp(K.costo_promedio_unidad), null],
  ['Clientes (locales)', num(K.clientes), K.grupos + ' cadenas · ' + num(K.comunas) + ' comunas', null],
  ['Productos vendidos', num(K.productos), DATA.portafolio.productos_activos_ultimo_anio + ' activos en ' + ultimo.anio, null],
  ['Regiones', num(K.regiones), 'de las 16 del país', null],
  ['Venta media mensual', mm(K.venta_promedio_mensual), num(K.unidades_promedio_mensual) + ' unidades', null],
  ['Proyección ' + P.anio_proyectado, num(P.unidades_total) + ' u.', mm(P.venta_total), null],
].map(([l, v, n, up]) => `<div class="card">
    <div class="tile-label">${l}</div>
    <div class="tile-value">${v}</div>
    ${up === null ? `<div class="tile-note">${n}</div>`
                  : `<div class="delta ${up ? 'up' : 'down'}">${n}</div>`}
  </div>`).join('')}</div>`));

/* 2 · Evolución */
app.appendChild(h(`<h2>2 · Cómo evolucionó la venta</h2>
  <p class="lede">La serie mensual manda: el promedio anual esconde que el negocio cambió de nivel
  a mediados de 2020. Marzo de 2020 marca el peak de acopio por pandemia; desde entonces la venta
  se estabilizó en un piso más bajo, con recuperación durante 2022.</p>`));

const serie = DATA.serie_mensual.slice().sort((a,b) => a.periodo.localeCompare(b.periodo));
const c1 = card('Venta mensual y proyección ' + P.anio_proyectado,
  'Millones de pesos por mes · la proyección usa el modelo ganador del backtest',
  {legend: keyLine('--series-1','Venta real') + keyLine('--series-2','Proyección ' + P.anio_proyectado),
   table: table(['Período','Venta','Unidades','Margen','Líneas'],
     serie.map(r => [mLabel(r.periodo), clp(r.venta), num(r.unidades), clp(r.margen), num(r.lineas)]))});
app.appendChild(c1.node);
const ultReal = serie[serie.length - 1];
draw(c1.chart, host => lineChart(host, {
  series: [
    {name:'Venta real', color:'--series-1', points: serie.map(r => ({x:r.periodo, y:r.venta}))},
    {name:'Proyección', color:'--series-2', dashed:true,
     points: [{x:ultReal.periodo, y:ultReal.venta}].concat(P.mensual.map(r => ({x:r.periodo, y:r.venta_proy})))},
  ],
  fmtY: v => mmShort(v), fmtTip: v => clp(v), labelEvery: 6,
}));

const g2a = h('<div class="grid g2" style="margin-top:16px"></div>');
const c2 = card('Venta por año', 'Millones de pesos · variación sobre el año anterior',
  {table: table(['Año','Venta','Var.','Unidades','Margen %'],
     anios.map(r => [r.anio, clp(r.venta), r.venta_yoy_pct === null ? '—' : signed(r.venta_yoy_pct),
                     num(r.unidades), pct(r.margen_pct)]))});
g2a.appendChild(c2.node);
draw(c2.chart, host => columnChart(host, {
  data: anios.map(r => ({label: String(r.anio), value: r.venta, yoy: r.venta_yoy_pct, u: r.unidades})),
  fmtY: v => mmShort(v),
  fmtTip: (v, d) => `${clp(v)}<div>${num(d.u)} unidades</div>` +
                    (d.yoy === null ? '' : `<div>${signed(d.yoy)} vs año anterior</div>`),
}));

const est = DATA.estacionalidad;
const c3 = card('Estacionalidad de la venta', 'Índice sobre el promedio mensual · 1,00 = mes promedio',
  {legend: keyBox('--pos','Sobre el promedio') + keyBox('--neg','Bajo el promedio'),
   note: 'Marzo y el invierno concentran la demanda de legumbres; enero, febrero y diciembre son los meses valle.',
   table: table(['Mes','Índice','Venta promedio del mes'],
     est.map(r => [MESES[r.mes-1], r.indice.toFixed(2).replace('.', ','), clp(r.venta_promedio)]))});
g2a.appendChild(c3.node);
draw(c3.chart, host => columnChart(host, {
  data: est.map(r => ({label: MESES[r.mes-1], value: r.indice, full: MESES[r.mes-1]})),
  diverging: true, baseline: 1,
  fmtY: v => v.toFixed(2).replace('.', ','),
  fmtTip: v => `Índice ${v.toFixed(2).replace('.', ',')}<div>${signed((v-1)*100)} vs mes promedio</div>`,
}));
app.appendChild(g2a);

/* 3 · Clientes */
app.appendChild(h(`<h2>3 · Quién compra</h2>
  <p class="lede">Cuatro cadenas concentran el 100% de la venta, y una sola —Cencosud— pesa
  la mitad. Esa concentración es el principal riesgo comercial del portafolio: perder un
  contrato marco no se compensa con el resto de la base.</p>`));
const cGrupo = card('Participación por cadena', 'Porcentaje de la venta acumulada 2019-2022',
  {legend: DATA.por_grupo.map((r, i) => keyBox('--series-' + (i+1), r.grupo_cliente + ' · ' + mm(r.venta))).join(''),
   table: table(['Cadena','Venta','Participación','Unidades','Margen %'],
     DATA.por_grupo.map(r => [r.grupo_cliente, clp(r.venta), pct(r.participacion_pct),
                              num(r.unidades), pct(r.margen_pct)]))});
app.appendChild(cGrupo.node);
draw(cGrupo.chart, host => stackedBar(host, {
  data: DATA.por_grupo.map((r, i) => ({label:r.grupo_cliente, value:r.venta, color:'--series-'+(i+1)})),
  fmtTip: (v, d) => `${clp(v)}<div>${pct(v / K.venta_total * 100)} de la venta total</div>`,
}));

const g3 = h('<div class="grid g2" style="margin-top:16px"></div>');
const cCli = card('Top 10 locales por venta', 'Ningún local supera el 2% del total: la venta está atomizada',
  {table: table(['Código','Local','Venta','Part.','Unidades'],
     DATA.top_clientes.map(r => [r.cliente_codigo, r.cliente ?? '—', clp(r.venta),
                                 pct(r.participacion_pct, 2), num(r.unidades)]))});
g3.appendChild(cCli.node);
draw(cCli.chart, host => barChart(host, {
  data: DATA.top_clientes.slice(0, 10).map(r => ({label: r.cliente ?? r.cliente_codigo,
                                                  value: r.venta, p: r.participacion_pct})),
  fmtVal: v => mm(v),
  fmtTip: (v, d) => `${clp(v)}<div>${pct(d.p, 2)} de la venta total</div>`,
}));

const cReg = card('Top 10 regiones por venta', 'La Región Metropolitana concentra un tercio de la venta',
  {table: table(['Región','Venta','Part.','Unidades','Margen %'],
     DATA.por_region.map(r => [r.region, clp(r.venta), pct(r.participacion_pct),
                               num(r.unidades), pct(r.margen_pct)]))});
g3.appendChild(cReg.node);
draw(cReg.chart, host => barChart(host, {
  data: DATA.por_region.slice(0, 10).map(r => ({label: shortRegion(r.region),
                                                value: r.venta, p: r.participacion_pct})),
  fmtVal: v => mm(v),
  fmtTip: (v, d) => `${clp(v)}<div>${pct(d.p)} de la venta total</div>`,
}));
app.appendChild(g3);

/* 4 · Productos */
app.appendChild(h(`<h2>4 · Qué se vende</h2>
  <p class="lede">Las legumbres son el 82% de la venta y las lentejas, por sí solas, un tercio.
  El surtido se redujo de ${DATA.portafolio.productos_historicos} a
  ${DATA.portafolio.productos_activos_ultimo_anio} SKU activos: los descontinuados son casi todos
  formato RRP, una línea de empaque que se apagó entre 2020 y 2021.</p>`));
const g4 = h('<div class="grid g2"></div>');
const cProd = card('Top 10 productos por venta', 'Millones de pesos acumulados 2019-2022',
  {table: table(['Producto','Venta','Part.','Unidades','Precio unit.','Margen %'],
     DATA.top_productos_venta.map(r => [r.producto, clp(r.venta), pct(r.participacion_pct),
                                        num(r.unidades), clp(r.precio_unitario), pct(r.margen_pct)]))});
g4.appendChild(cProd.node);
draw(cProd.chart, host => barChart(host, {
  data: DATA.top_productos_venta.slice(0, 10).map(r => ({label:r.producto, value:r.venta,
                                                         p:r.participacion_pct, u:r.unidades})),
  fmtVal: v => mm(v),
  fmtTip: (v, d) => `${clp(v)}<div>${num(d.u)} unidades · ${pct(d.p)} del total</div>`,
}));

const cCat = card('Venta por categoría', 'Millones de pesos · margen bruto de cada categoría',
  {table: table(['Categoría','Venta','Part.','Unidades','Margen %'],
     DATA.por_categoria.map(r => [r.categoria, clp(r.venta), pct(r.participacion_pct),
                                  num(r.unidades), pct(r.margen_pct)]))});
g4.appendChild(cCat.node);
draw(cCat.chart, host => barChart(host, {
  data: DATA.por_categoria.map(r => ({label:r.categoria, value:r.venta,
                                      p:r.participacion_pct, mg:r.margen_pct})),
  fmtVal: v => mm(v),
  fmtTip: (v, d) => `${clp(v)}<div>${pct(d.p)} del total · margen ${pct(d.mg)}</div>`,
}));
app.appendChild(g4);

app.appendChild(h(`<div class="card" style="margin-top:16px">
  <p class="chart-title">Productos descontinuados — no cotizar</p>
  <p class="chart-sub">SKU con venta histórica pero sin movimiento en ${M.periodo_fin.slice(0,4)}</p>
  <div class="scroll">${table(['Producto','Última venta','Unidades históricas'],
    DATA.portafolio.descontinuados.map(r => [r.producto, r.ultima_venta, num(r.unidades_historicas)]))}</div>
</div>`));

/* 5 · Proyección */
app.appendChild(h(`<h2>5 · Proyección ${P.anio_proyectado} y plan de compra</h2>
  <p class="lede">El modelo no se eligió a dedo: seis métodos compitieron prediciendo
  ${M.periodo_fin.slice(0,4)} sin haberlo visto, y ganó el de menor error.
  <strong>${P.metodo}</strong>, con un error medio de ${pct(P.mape_backtest_unidades)} en unidades.</p>`));

const cBt = card('Validación del modelo: ' + M.periodo_fin.slice(0,4) + ' real vs predicho',
  'El modelo se entrenó sólo con datos hasta ' + (+M.periodo_fin.slice(0,4)-1) + ' y predijo el año siguiente',
  {legend: keyLine('--series-1','Real') + keyLine('--series-2','Predicho por el modelo'),
   note: 'Un error medio de ' + pct(P.mape_backtest_unidades) + ' es la precisión que se puede prometer a la jefatura. No es el ajuste al pasado: es el error contra datos que el modelo no vio.',
   table: table(['Período','Real','Predicho','Error'],
     P.backtest.map(r => [mLabel(r.periodo), num(r.real), num(r.predicho),
                          signed((r.predicho - r.real) / r.real * 100)]))});
app.appendChild(cBt.node);
draw(cBt.chart, host => lineChart(host, {
  series: [
    {name:'Real', color:'--series-1', points: P.backtest.map(r => ({x:r.periodo, y:r.real}))},
    {name:'Predicho', color:'--series-2', dashed:true,
     points: P.backtest.map(r => ({x:r.periodo, y:r.predicho}))},
  ],
  fmtY: v => num(v), fmtTip: v => num(v) + ' unidades', labelEvery: 2,
}));

const g5 = h('<div class="grid g2" style="margin-top:16px"></div>');
const cComp = h(`<div class="card">
  <p class="chart-title">Los seis modelos que compitieron</p>
  <p class="chart-sub">Error medio absoluto (MAPE) prediciendo ${M.periodo_fin.slice(0,4)}. Menor es mejor.</p>
  <div class="scroll">${table(['Modelo','MAPE','Veredicto'],
    P.comparacion_modelos.map((r, i) => [r.etiqueta, pct(r.mape_backtest),
      i === 0 ? '<strong>elegido</strong>' : 'descartado']))}</div>
  <p class="chart-sub" style="margin-top:10px">La descomposición con tendencia sobre toda la serie
  falla porque arrastra la caída 2020→2021 hacia el futuro: proyecta un declive que los dos
  últimos años ya no muestran.</p>
</div>`);
g5.appendChild(cComp);

const cProy = card('Proyección mensual ' + P.anio_proyectado, 'Unidades por mes',
  {table: table(['Período','Unidades','Venta proyectada'],
     P.mensual.map(r => [mLabel(r.periodo), num(r.unidades_proy), clp(r.venta_proy)]))});
g5.appendChild(cProy.node);
draw(cProy.chart, host => columnChart(host, {
  data: P.mensual.map(r => ({label: MESES[r.mes-1], value: r.unidades_proy,
                             full: mLabel(r.periodo), v: r.venta_proy})),
  fmtY: v => num(v),
  fmtTip: (v, d) => `${num(v)} unidades<div>${clp(d.v)} de venta</div>`,
}));
app.appendChild(g5);

const PC = DATA.plan_cotizacion;
app.appendChild(h(`<div class="card" style="margin-top:16px">
  <p class="chart-title">Plan de cotización ${P.anio_proyectado} por producto</p>
  <p class="chart-sub">${PC.criterio}</p>
  <div class="callout"><strong>${num(PC.total_unidades_a_cotizar)} unidades</strong> a cotizar
  en total: ${num(PC.total_unidades_proyectadas)} de demanda proyectada
  + ${num(PC.total_stock_seguridad)} de stock de seguridad al ${pct(PC.nivel_servicio*100, 0)}
  de nivel de servicio. El colchón se compra una vez y después sólo se repone lo consumido.</div>
  <div class="scroll">${table(
    ['Producto','Unidades ' + PC.anio_base, 'Proyección ' + P.anio_proyectado,
     'Pedido mensual', 'Stock seguridad', 'A cotizar', 'Part.'],
    PC.items.map(r => [r.producto, num(r.unidades_ultimo_anio), num(r.unidades_proyectadas),
                       num(r.pedido_mensual_promedio), num(r.stock_seguridad),
                       '<strong>' + num(r.cotizacion_anual_sugerida) + '</strong>',
                       pct(r.participacion_pct)]))}</div>
</div>`));

/* 6 · Calidad */
const Q = DATA.calidad;
app.appendChild(h(`<h2>6 · Calidad de los datos</h2>
  <p class="lede">Ninguna proyección vale más que el dato que la sostiene. Esto es lo que el
  pipeline encontró y qué hizo con cada hallazgo.</p>`));
app.appendChild(h(`<div class="grid g3">${[
  ['Filas procesadas', num(M.filas_extraidas), 'de las dos hojas del archivo original'],
  ['Filas utilizables', num(M.filas_limpias), pct(100 - M.ratio_cuarentena*100, 3) + ' del total'],
  ['Filas en cuarentena', num(M.filas_cuarentena), 'devoluciones con unidades negativas'],
  ['Meses sin datos', String(M.meses_faltantes.length), M.meses_faltantes.join(', ') + ' — imputado para la proyección'],
  ['Ventas bajo costo', num(Q.filas_margen_negativo), pct(Q.pct_filas_margen_negativo) + ' de las filas'],
  ['Costo en cero', String(Q.flags.costo_no_positivo ?? 0), 'margen ficticio en esas filas'],
].map(([l, v, n]) => `<div class="card">
    <div class="tile-label">${l}</div><div class="tile-value">${v}</div>
    <div class="tile-note">${n}</div></div>`).join('')}</div>`));

app.appendChild(h(`<div class="card" style="margin-top:16px">
  <p class="chart-title">Hallazgos y tratamiento</p>
  <div class="scroll">${table(['Hallazgo','Filas','Qué se hizo'], [
    ['Hoja 2021-2022 con columna extra (local) y nombres de cliente con prefijo de código',
     num(M.filas_limpias), 'Ambas hojas unificadas a un esquema único; el prefijo se quitó del nombre'],
    ['Mismo local escrito de varias formas entre meses',
     num(Q.clientes_nombre_inconsistente) + ' códigos', 'La llave es el código; la etiqueta usa el nombre más frecuente'],
    ['Venta bajo costo', num(Q.filas_margen_negativo), 'Se marca y se conserva: es venta real (promoción/liquidación)'],
    ['Costo cero con venta positiva', String(Q.flags.costo_no_positivo ?? 0), 'Se marca: el margen de esas filas no es confiable'],
    ['Unidades y venta negativas', num(M.filas_cuarentena), 'A cuarentena: son devoluciones, no demanda'],
    ['Noviembre 2022 ausente en el archivo', '—', 'Imputado con nivel local × estacionalidad, y declarado como imputado'],
    ['Nombres de producto con dobles espacios y typos de marca', '—', 'Normalizados y clasificados en categoría/familia/formato'],
  ])}</div>
</div>`));
"""


def matches(columns) -> bool:
    """¿Este dataset es el del caso Abarrotes? Decide el registry de dashboards."""
    return MATCH_COLUMNS.issubset({str(c) for c in columns})


def render(insights: dict, standalone: bool = True) -> str:
    anio = str(insights["proyeccion"]["anio_proyectado"])
    filas = f'{insights["meta"]["filas_limpias"]:,}'.replace(",", ".")
    header = HEADER.replace("__ANIO_PROY__", anio).replace("__FILAS__", filas)
    return render_page(
        title=f"Abarrotes CL — Análisis de ventas 2019-2022 y proyección {anio}",
        header_html=header, build_js=BUILD_JS, data=insights,
        footer_html=FOOTER, standalone=standalone,
    )


def build(insights_path: str, out_path: str, standalone: bool = True) -> str:
    with open(insights_path, encoding="utf-8") as f:
        insights = json.load(f)
    html = render(insights, standalone=standalone)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Dashboard HTML del caso Abarrotes CL")
    parser.add_argument("--insights", required=True, help="Ruta a insights.json")
    parser.add_argument("--out", required=True, help="Ruta del HTML de salida")
    args = parser.parse_args()
    print(f"Dashboard escrito en {build(args.insights, args.out)}")


if __name__ == "__main__":
    main()
