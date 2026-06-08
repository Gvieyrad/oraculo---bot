# REGLAS DE ACTIVACIÓN DE MERCADO — OBLIGATORIAS
> Escritas el 2026-06-07 tras pérdida -$43.87 en F5 ML (17% WR live, 12 picks).
> NINGÚN mercado pasa de shadow a live sin cumplir las tres.

---

## Por qué existen estas reglas

Activamos MLB F5 ML con WR=58.1% en shadow.
El número era falso: incluía 56 VOIDs en el denominador.
El WR real en los 60 días previos era 50% (9W/9L).
La fórmula FIP×4% nunca fue validada con backtest histórico.
Resultado: 2W/10L live = -$43.87 = -67% ROI.

---

## REGLA 1 — Backtest histórico obligatorio

Antes de activar cualquier mercado nuevo:
- Correr backtest contra datos históricos reales (mínimo 200 juegos/picks)
- El WR en backtest debe ser ≥ 54%
- Documentar el resultado del backtest con fecha y n de muestra
- Si no hay datos históricos suficientes: el mercado NO SE ACTIVA

**Violación que causó la pérdida:** F5 ML nunca tuvo backtest.
El backtest post-mortem mostró WR=51.3% en 759 juegos → sin edge.

---

## REGLA 2 — VOIDs excluidos del denominador siempre

El WR se calcula como:
    WR = Wins / (Wins + Losses)

Los VOIDs (apuestas anuladas/void) NO cuentan como Wins ni Losses.
NO se dividen W / (W + L + VOID).

Aplicar esto igual para TODOS los mercados, sin excepción.
La función de cálculo debe ser una sola, centralizada.

**Violación que causó la pérdida:** Shadow reportaba 58.1% pero
incluía VOIDs en el total. WR real era ~51%.

---

## REGLA 3 — Ventana reciente de 60 días

El WR histórico total no es suficiente.
Exigir además:
- WR ≥ 54% en los últimos 60 días de shadow
- Mínimo 30 picks decididos (no VOIDs) en esos 60 días
- Si la ventana reciente dice < 54%: NO SE ACTIVA aunque el histórico diga 58%

**Violación que causó la pérdida:** WR histórico total = 58%,
pero mayo-junio 2026 = 50% (9W/9L). La ventana reciente hubiera bloqueado la activación.

---

## CHECKLIST DE ACTIVACIÓN (llenar antes de cambiar _ENABLED = True)

Mercado: _______________
Fecha: _______________
Responsable: _______________

[ ] 1. Backtest corrido: SI/NO
      Archivo del script: _______________
      N juegos backtested: _______________
      WR en backtest: ______% (mínimo 54%)

[ ] 2. WR calculado sin VOIDs: SI/NO
      W: ___ L: ___ VOID (excluidos): ___
      WR real = W/(W+L) = ______%

[ ] 3. Ventana 60 días verificada: SI/NO
      Período: ___ a ___
      N picks en ventana: ___ (mínimo 30)
      WR en ventana: ______% (mínimo 54%)

Si algún checkbox es NO o no llega al mínimo: NO ACTIVAR.

---

---

## REGLA 4 — Cap de stake obligatorio por fase

Todo mercado nuevo arranca en Fase 1. Solo sube de fase con evidencia live.

| Fase | Condición para entrar | Cap por pick |
|------|-----------------------|--------------|
| Fase 0 (shadow) | Mercado nuevo | $0 (sin dinero real) |
| Fase 1 (live validacion) | Pasa las 3 reglas de activacion | $1.00 maximo |
| Fase 2 (escala moderada) | 50+ picks live, WR >= 54% en ventana 60d | $5.00 maximo |
| Fase 3 (escala normal) | 100+ picks live, WR >= 54% sostenido | Kelly libre con techo bankroll/20 |

Reglas adicionales:
- El cap NUNCA se sube manualmente sin verificar la fase
- Mercados con boost/elite/categoria especial: misma fase, mismo cap
- Si el WR cae por debajo de 50% en ventana de 30 dias: vuelve a Fase 1 automaticamente

**Violacion que causo la perdida:** F5 ML paso de $1 a $10 el mismo dia de activacion
(Jun 2→5, 2026) porque el cap estaba en $10-15 desde el inicio.
Kelly con edge inflado + cap alto = $8-10 por pick en un modelo sin edge real.

---

## CHECKLIST DE SUBIDA DE FASE (llenar antes de subir el cap)

Mercado: _______________
Fase actual: ___ → Fase destino: ___
Fecha: _______________

[ ] Picks live acumulados: ___ (minimo segun tabla)
[ ] WR en ventana 60 dias: ______%% (minimo 54%%)
[ ] Cap nuevo calculado: $___
[ ] Aprobado por: _______________

---

---

## REGLA 5 — Monitoreo de degradacion automatico

Todo mercado activo se revisa automaticamente cada semana.
El script monitor_degradacion.py corre cada lunes y genera una alerta si:

| Condicion | Accion automatica |
|-----------|-------------------|
| WR ventana 30d < 50%% con n >= 15 picks | ALERTA — bajar cap a $1 manualmente |
| WR ventana 30d < 45%% con n >= 10 picks | DESACTIVAR automaticamente (_ENABLED = False) |
| Sin picks en 14 dias (mercado inactivo) | AVISO — verificar que el scanner funciona |

Reglas adicionales:
- La ventana es ROLLING 30 dias, no el mes calendario
- El umbral de n minimo evita falsas alarmas por rachas cortas
- Si un mercado baja a $1 cap, debe pasar por checklist de Regla 4 para volver a escalar
- El reporte semanal se guarda en docs/degradacion_YYYY-MM-DD.txt

**Por que existe:** Tennis y soccer quedaron activos sin alerta de degradacion.
Si su WR cae silenciosamente de 67%% a 45%%, seguiriamos apostando normal
hasta perder tanto como en baseball.

---

## Consecuencias de saltarse estas reglas

- F5 ML 2026-06: -$43.87 en 12 picks (17% WR)
- El mercado fue desactivado manualmente el 2026-06-07

