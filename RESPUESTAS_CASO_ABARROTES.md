# Caso Abarrotes CL — Informe de asesoría

**Para:** Analista de adquisiciones · Jefatura directa · Comité ejecutivo
**Alcance:** ventas enero 2019 – diciembre 2022, categoría abarrotes, canal retail
**Fuente:** `Ventas_2019-al-2022.xlsx` (2 hojas, 315.306 registros)
**Artefactos:** dashboard en `C:\Users\marku\Downloads\El cubo\abarrotes-cl\index.html` ·
datos y auditoría en `output/<run_id>/`

> **Sobre el cuestionario.** El enunciado del caso pide responder un cuestionario que no
> venía adjunto en el material entregado. Este informe responde las preguntas de negocio que
> el caso plantea de forma explícita — cuánto se vendió, a quién, dónde, qué productos y
> **cuánto hay que cotizar a los proveedores** — y deja el dataset, los agregados y el
> dashboard listos para responder cualquier pregunta adicional. Pásame el cuestionario y
> respondo pregunta por pregunta con estas mismas cifras.

---

## 0. Resumen ejecutivo

1. **La venta cayó un escalón y se estabilizó ahí.** 2019 y 2020 promediaron MM$ 3.633 al
   año; 2021 y 2022, MM$ 2.350. La caída no es una tendencia gradual: es un quiebre de nivel
   entre el segundo semestre de 2020 y 2021, después del acopio por pandemia (marzo 2020
   vendió MM$ 611: 2,4 veces un mes promedio). Desde ese piso, 2022 recupera: **+2,1% en
   venta** y **+8,3% en unidades por mes** contra 2021.
2. **La proyección 2023 es de 2.101.170 unidades (MM$ 2.759)**, +4,2% en unidades sobre 2022.
   El modelo se eligió compitiendo seis métodos contra el año 2022 sin dejárselo ver: el
   ganador erra **7,2%** en promedio mensual. Ese es el número que se le puede prometer a la
   jefatura, no el ajuste al pasado.
3. **Hay que cotizar 2.205.707 unidades** para 2023: 2.135.880 de demanda proyectada más
   69.821 de stock de seguridad al 95% de nivel de servicio. El detalle por SKU está en el
   dashboard y en `insights.json`.
4. **El riesgo no es la demanda, es la concentración**: una sola cadena (Cencosud) es el 50%
   de la venta. Un cambio de contrato marco mueve más plata que cualquier error de
   proyección.
5. **Diez SKU no deben cotizarse**: dejaron de venderse antes de 2022. Nueve de los diez son
   del formato RRP, una línea de empaque que se apagó entre 2020 y 2021.

---

## 1. ¿Los datos son confiables? (lo primero que hay que responder)

Antes de proyectar nada, el archivo pasó por el pipeline de calidad. El resultado:
**315.304 de 315.306 filas quedaron utilizables (99,999%)**. Pero "utilizable" no es
"perfecto", y estos hallazgos cambian cómo hay que leer las cifras:

| Hallazgo | Magnitud | Tratamiento y por qué |
|---|---|---|
| **El archivo trae dos hojas con esquemas distintos** | 187.343 + 127.963 filas | La hoja 2021-2022 tiene una columna extra (`COD_Local_Descripcion`) y escribe el nombre del cliente con el código pegado (`J501-JUMBO BILBAO`). Se unificaron a un esquema único. **Si se analiza una sola hoja se pierde la mitad del período.** |
| **Falta noviembre de 2022** | 1 de 48 meses | El archivo no lo trae: no es un mes con venta cero, es un mes ausente. Se imputó (nivel local × estacionalidad) sólo para la proyección, y queda declarado como imputado. Conviene pedir ese mes al KAM. |
| **El mismo local escrito de varias formas** | 333 códigos de cliente | `983 - CORONEL-MANUEL MONTT`, `N983 - CORONEL MANUEL MONTT`, `N983 - N983 - …`. Si se agrupa por nombre, un cliente se parte en tres. **Se agrupa por código**, y la etiqueta usa el nombre más frecuente. |
| **Ventas bajo costo** | 7.445 filas (2,36%) | Se marcan y **se conservan**: son venta real (promoción, liquidación). Borrarlas descuadraría el total vendido contra contabilidad. |
| **Costo en cero con venta positiva** | 30 filas | Falta el costo en el maestro, el producto no es gratis. El margen de esas filas no es confiable; el impacto en el margen global es despreciable. |
| **Unidades y venta negativas** | 2 filas | A cuarentena: son devoluciones, no demanda futura. |
| **Nombres de producto sucios** | 48 → 47 SKU | Dobles espacios y typos de marca (`POROTO MARITNI`, `POTAGE GARBANZO MART INI`) hacían aparecer productos duplicados. Normalizados, y clasificados en categoría / familia / formato / empaque. |

**Conclusión de calidad:** el dataset sostiene decisiones de compra. Las dos salvedades a
declarar en cualquier presentación son *noviembre 2022 imputado* y *el margen de 30 filas
sin costo*.

---

## 2. ¿Cuánto se vendió?

| Métrica | 2019-2022 |
|---|---|
| Venta | **$11.964.501.284** (MM$ 11.964,5) |
| Costo de producto | $9.279.586.118 |
| Margen bruto | **$2.684.915.166 — 22,44%** |
| Unidades | 9.479.709 |
| Precio medio por unidad | $1.262 (costo medio $979) |
| Registros de venta | 315.304 |

### Evolución anual

| Año | Venta | Var. | Unidades | Var. | Margen |
|---|---|---|---|---|---|
| 2019 | $3.547.992.329 | — | 2.860.015 | — | 22,90% |
| 2020 | $3.717.083.418 | **+4,8%** | 2.847.846 | −0,4% | 24,30% |
| 2021 | $2.325.783.550 | **−37,4%** | 1.892.895 | −33,5% | 20,21% |
| 2022 | $2.373.641.987 | **+2,1%** | 1.878.953 | −0,7% | 21,04% |

Dos lecturas que el promedio anual esconde y la serie mensual muestra:

- **2020 no fue un buen año parejo**: fue un marzo excepcional (MM$ 611, +185% sobre el mes
  promedio) seguido de una caída sostenida desde agosto. El año cierra positivo por el acopio
  del primer semestre.
- **2022 no cae, recupera.** Las unidades anuales bajan 0,7% sólo porque falta noviembre.
  Comparando promedio mensual con meses observados, 2022 está **8,3% sobre 2021**.

El margen se comprimió 2 puntos entre 2020 y 2021 y no ha vuelto: de 24,3% a ~21%.

---

## 3. ¿A quién se le vende?

### Por cadena

| Cadena | Venta | Part. | Margen |
|---|---|---|---|
| CENCOSUD RETAIL | $5.979.712.396 | **50,0%** | 20,81% |
| WALMART CHILE S.A. | $3.020.594.798 | 25,2% | 20,42% |
| GRUPO RENDIC | $2.377.109.967 | 19,9% | **27,79%** |
| TOTTUS S.A. | $587.084.123 | 4,9% | **27,82%** |

Dato accionable: **las dos cadenas más grandes son las de peor margen** (20,4-20,8%), y las
dos más chicas las de mejor margen (27,8%). Cada punto de mix ganado en Rendic o Tottus vale
7 puntos de margen sobre esa venta.

### Por local

841 locales. **Ninguno supera el 1,81% de la venta**; el top 10 suma 12,4% y se necesitan 324
locales para llegar al 80% de la venta. La venta está atomizada a nivel de local pero
concentrada a nivel de contrato: la negociación es con cuatro casas matrices.

Top 5: JUMBO ANTOFAGASTA ANGAMOS (1,81%), JUMBO BILBAO (1,62%), JUMBO VIÑA DEL MAR (1,60%),
JUMBO KENNEDY (1,25%), JUMBO COSTANERA (1,24%).

---

## 4. ¿Dónde se vende?

16 regiones, 188 comunas.

| Región | Part. |
|---|---|
| Metropolitana | 35,2% |
| Valparaíso | 17,8% |
| Antofagasta | 10,5% |
| Los Lagos | 6,3% |
| Coquimbo | 6,1% |
| Biobío | 5,0% |
| Resto (10 regiones) | 19,1% |

**Antofagasta es la anomalía interesante**: es la **comuna con mayor venta del país**
($1.001 millones, 8,4% del total), por sobre Las Condes ($664 millones), y sostiene sola el
80% de la venta de su región. Dos de los seis locales más grandes del ranking nacional están
ahí. Vale entender qué se está haciendo bien antes de intentar replicarlo.

---

## 5. ¿Qué se vende?

### Por categoría

| Categoría | Venta | Part. | Margen |
|---|---|---|---|
| Legumbres | $9.808.676.502 | **82,0%** | 22,02% |
| Cereales y harinas | $1.693.562.817 | 14,1% | **24,48%** |
| Preparados y sopas | $462.261.965 | 3,9% | 23,87% |

### Por familia (top)

| Familia | Part. | Margen |
|---|---|---|
| Lentejas | **39,9%** | 18,88% |
| Porotos | 19,0% | **27,64%** |
| Garbanzos | 14,4% | 22,94% |
| Arvejas | 8,6% | 22,63% |
| Chuño | 4,8% | 21,23% |

**El producto que más vende es el que menos margen deja.** Las lentejas son 4 de cada 10 pesos
vendidos con 18,9% de margen; los porotos, la mitad de volumen con 27,6%. Si la estrategia es
margen y no facturación, el foco de negociación con proveedores debería estar en el costo de
la lenteja, que es donde está toda la venta.

Top SKU por venta: LENTEJAS 7 MM 500 G (11,4%), LENTEJAS 6 MM 500 G (10,4%), GARBANZOS PELADOS
500 G (9,6%), POROTO TÓRTOLA 500 G (7,6%), LENTEJAS 5 MM 500 G (7,3%).

### Portafolio: 10 SKU que no hay que cotizar

De los 47 productos históricos, **sólo 37 tuvieron venta en 2022**. Los 10 descontinuados
—nueve de ellos formato **RRP**— dejaron de venderse entre abril 2020 y mayo 2021. El formato
RRP no desapareció del todo (sigue siendo 15,4% de la venta), pero se podó a la mitad del
surtido. Cotizar estos SKU sería comprar inventario muerto.

### Tipo de reposición

Interna 60,6% (margen 22,5%) · Externa 30,2% (21,6%) · Sin reposición 9,2% (24,5%).

---

## 6. ¿Cuánto hay que comprar? (la pregunta del caso)

### Cómo se eligió el modelo

No se eligió por preferencia: **compitieron seis métodos prediciendo 2022 sin haberlo visto**
(entrenamiento hasta diciembre 2021), y ganó el de menor error.

| Modelo | Error medio (MAPE) | Veredicto |
|---|---|---|
| **Nivel últimos 6 meses × índice estacional** | **7,2%** | **elegido** |
| Nivel últimos 12 meses × índice estacional | 7,3% | descartado |
| Naive estacional (mismo mes del año anterior) | 8,6% | descartado |
| Promedio móvil 12m (sin estacionalidad) | 16,4% | descartado |
| Descomposición clásica + tendencia sobre toda la serie | 18,3% | descartado |
| Descomposición clásica + tendencia últimos 24m | 41,2% | descartado |

Vale la pena explicar el perdedor: **la descomposición con tendencia lineal sobre los cuatro
años proyecta una caída de −21,6% para 2023**. Está arrastrando el desplome 2020→2021 hacia
el futuro como si fuera una tendencia, cuando en realidad fue un quiebre de nivel que ya
terminó. Haberla usado habría significado comprar 500.000 unidades de menos.

### La proyección

**2023: 2.101.170 unidades · MM$ 2.759** (+4,2% en unidades sobre 2022).

| Mes | Unidades | Venta proyectada |
|---|---|---|
| Ene | 129.821 | $167.612.577 |
| Feb | 134.171 | $172.366.456 |
| **Mar** | **234.770** | **$310.110.793** |
| Abr | 207.076 | $273.082.208 |
| May | 210.747 | $285.020.456 |
| Jun | 202.437 | $272.518.420 |
| Jul | 198.581 | $265.154.770 |
| Ago | 182.091 | $239.606.346 |
| Sep | 156.063 | $200.016.059 |
| Oct | 171.715 | $224.011.209 |
| Nov | 139.725 | $180.135.923 |
| Dic | 133.972 | $169.568.422 |

La venta proyectada crece más que las unidades (+8% vs +4%) porque el modelo toma el nivel de
precios de los últimos seis meses de 2022, que ya incorpora la inflación de ese año. Si se
quiere una proyección a precios constantes, hay que usar la columna de unidades.

### Estacionalidad: cuándo comprar

| Mes | Índice | | Mes | Índice |
|---|---|---|---|---|
| Ene | 0,74 | | Jul | 1,13 |
| Feb | 0,77 | | Ago | 1,04 |
| **Mar** | **1,34** | | Sep | 0,89 |
| Abr | 1,18 | | Oct | 0,98 |
| May | 1,20 | | Nov | 0,80 |
| Jun | 1,16 | | Dic | 0,77 |

**Marzo vende 34% sobre el mes promedio y 81% más que enero.** La curva es coherente con el
producto: las legumbres se consumen en otoño-invierno, y marzo suma el reabastecimiento
post-verano de los locales. Operativamente: **el abastecimiento de marzo se negocia en
diciembre-enero**, que es justo el valle de venta y el peor momento para pedir capacidad al
proveedor si se avisa tarde.

### Plan de cotización 2023

| Concepto | Unidades |
|---|---|
| Demanda proyectada (suma por SKU) | 2.135.880 |
| Stock de seguridad (95% nivel de servicio) | 69.821 |
| **Total a cotizar** | **2.205.707** |

Método por SKU: unidades 2022 anualizadas (×12/11, porque falta noviembre) × (1 + 4,2% de
crecimiento proyectado), más un colchón de z·σ sobre la variabilidad mensual de ese SKU. **El
colchón se compra una vez** y después sólo se repone lo consumido: no es un extra mensual.

El total bottom-up (2.135.880) y el top-down (2.101.170) difieren 1,6%. Esa diferencia es la
holgura razonable entre proyectar el agregado y proyectar SKU por SKU; para negociar volumen,
usar el bottom-up.

Los cinco SKU que concentran el 47% del volumen a cotizar:

| Producto | 2022 | Proyección 2023 | Pedido mensual | Colchón | A cotizar |
|---|---|---|---|---|---|
| LENTEJAS 6 MM 500 G | 240.485 | 273.369 | 22.781 | 9.013 | **282.382** |
| LENTEJAS 7 MM 500 G | 209.276 | 237.892 | 19.824 | 5.277 | **243.169** |
| CHUÑO 250 G | 164.123 | 186.565 | 15.547 | 5.236 | **191.801** |
| LENTEJAS 5 MM 500 G | 138.956 | 157.957 | 13.163 | 2.835 | **160.792** |
| ARVEJAS VERDES 500 GR | 132.604 | 150.736 | 12.561 | 5.418 | **156.155** |

El detalle de los 37 SKU está en el dashboard y en `insights.json`.

---

## 7. Recomendaciones

1. **Cotizar 2.205.707 unidades para 2023**, con el escalonamiento estacional de la tabla:
   marzo-junio concentra el 40% del año.
2. **Cerrar el abastecimiento de marzo antes de enero.** Es el peak de demanda y el valle de
   actividad; llegar tarde ahí es quiebre de stock en el mes que más vende.
3. **Sacar los 10 SKU descontinuados de la cotización.** Confirmar con el KAM que la baja del
   formato RRP es decisión comercial y no falta de suministro.
4. **Renegociar el costo de la lenteja.** Es el 40% de la venta con el peor margen del
   portafolio (18,9% contra 27,6% de los porotos): es donde un punto de costo vale más.
5. **Pedir noviembre 2022** al KAM y volver a correr la proyección. Es el único dato faltante
   y hoy está imputado.
6. **Poner un umbral de alerta sobre las ventas bajo costo.** Hoy son 2,36% de las filas; si
   ese porcentaje sube, el problema no es de datos sino de precios.
7. **Monitorear la dependencia de Cencosud.** El 50% de la venta en una cadena es el riesgo
   más grande del negocio, y ninguna proyección de demanda lo cubre.

---

## 8. Cómo se produjo esto (reproducibilidad)

```bash
python run_caso_abarrotes.py
```

Un comando encadena las tres etapas:

| Etapa | Qué hace | Salida |
|---|---|---|
| **1. Pipeline ETL** | Une las dos hojas, aplica el cleaner del dominio, valida 11 reglas declarativas, aísla lo dudoso | dataset limpio (CSV/Parquet/XLSX), `quality_report.json`, `quarantine/`, `manifest.json` |
| **2. Capa analítica** | KPIs, cortes por cliente/región/producto, estacionalidad, competencia de modelos y proyección | `insights.json` |
| **3. Presentación** | Tablero HTML autocontenido (sin servidor ni internet) | `index.html` |

Piezas construidas para este caso, dentro del pipeline existente:

- `core/cleaners/abarrotes_ventas.py` — cleaner del dominio: unifica ambas hojas en un
  esquema canónico, preserva el código de cliente (el detector genérico lo habría reescrito
  como `TRX-0501`, fundiendo clientes distintos), descompone el período, normaliza productos
  y marca las anomalías.
- `rules/datasets/abarrotes_ventas.yaml` — 11 reglas de calidad declarativas.
- `core/file_extraction.py` — corrección: antes se elegía "la mejor hoja" del Excel y **el
  resto se descartaba en silencio**. Ahora se unen las hojas de esquema compatible y se
  registra en el log cuál se descartó y por qué.
- `analytics/forecasting.py` — familia de modelos + selección por backtest.
- `analytics/abarrotes_bi.py` — agregados e insights (única fuente de verdad: el informe y
  el dashboard leen los mismos números).
- `analytics/dashboard.py` — render del tablero.
- `tests/test_abarrotes_ventas.py`, `tests/test_forecasting.py` — 45 pruebas nuevas.

**Trazabilidad:** cada cifra de este informe sale de `insights.json`, que se calcula sobre el
dataset limpio del run, que a su vez conserva `hoja_origen` y los flags de calidad por fila.
Cualquier número se puede rastrear hasta la fila original del Excel.

---

## 9. Supuestos y límites (léase antes de comprometer una compra)

- **La proyección supone continuidad**: mismo portafolio, mismos clientes, sin quiebres de
  stock. Ganar o perder una cadena invalida el número y obliga a intervenirlo a mano.
- **7,2% de error medio mensual** es la precisión medida contra 2022. En un mes puntual el
  error puede ser mayor; el stock de seguridad cubre la variabilidad de la demanda, no un
  cambio de escenario.
- **Montos en pesos nominales**, sin ajuste por inflación. La comparación 2019 vs 2022 en
  pesos sobreestima el crecimiento real; por eso las conclusiones de volumen se apoyan en
  unidades.
- **Noviembre 2022 es un valor imputado**, no observado.
- El modelo proyecta **demanda**, no venta comprometida. La decisión de compra final es del
  analista de adquisiciones: esto es el insumo cuantitativo, no el reemplazo del criterio.
