import React, { useEffect, useState } from 'react';
import { BarChart3, RefreshCw, Loader2, Award, CheckCircle2 } from 'lucide-react';
import { api } from '../api/client';

export default function MetricsPage() {
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadMetrics = async (recompute = false) => {
    setLoading(true);
    try {
      const data = await api.getEvaluation(recompute);
      setReport(data);
    } catch (err) {
      console.error('Error cargando evaluación:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadMetrics(false);
  }, []);

  const models = report?.models || [];
  const bestP5 = Math.max(...models.map((m) => m.precision_at_5 || 0), 0.001);
  const bestNdcg = Math.max(...models.map((m) => m.ndcg_at_10 || 0), 0.001);
  const bestMae = Math.min(...models.map((m) => m.mae ?? 99));
  const bestRmse = Math.min(...models.map((m) => m.rmse ?? 99));

  return (
    <div className="space-y-6">
      <section className="rounded-3xl border border-stone-200/90 bg-white p-6 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-brand-100 text-brand-700">
              <BarChart3 className="h-5 w-5" />
            </div>
            <div>
              <h1 className="font-editorial text-2xl sm:text-3xl font-bold text-stone-900">
                Evaluación Cuantitativa del Recomendador
              </h1>
              <p className="text-xs sm:text-sm text-stone-500">
                Comparativa experimental offline <strong>80 % Entrenamiento / 20 % Prueba</strong>{' '}
                sin fuga de datos entre filtrado colaborativo (Chebyshev, MSD/JMSD), ML (SVD) y el Modelo Híbrido.
              </p>
            </div>
          </div>

          <button
            disabled={loading}
            onClick={() => loadMetrics(true)}
            className="inline-flex items-center gap-2 rounded-xl border border-stone-200 bg-white px-4 py-2 text-xs font-semibold text-stone-700 hover:bg-stone-50 disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Recalcular evaluación
          </button>
        </div>

        {report?.protocol && (
          <div className="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Usuarios Evaluados</p>
              <p className="mt-1 text-xl font-bold text-stone-900">
                {report.protocol.evaluated_users}
              </p>
              <p className="text-[11px] text-stone-500">split 80/20 estratificado</p>
            </div>
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Ratings Train / Test</p>
              <p className="mt-1 text-xl font-bold text-stone-900">
                {report.protocol.train_ratings?.toLocaleString()} /{' '}
                {report.protocol.test_ratings?.toLocaleString()}
              </p>
              <p className="text-[11px] text-stone-500">relevante si rating ≥ {report.protocol.relevant_threshold}★</p>
            </div>
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Umbral Solapamiento</p>
              <p className="mt-1 text-xl font-bold text-stone-900">
                γ ≥ {report.protocol.min_overlap_gamma}
              </p>
              <p className="text-[11px] text-stone-500">sin imputación de ceros</p>
            </div>
            <div className="rounded-2xl bg-stone-50 p-3.5 ring-1 ring-stone-200/70">
              <p className="text-[11px] font-semibold uppercase text-stone-400">Pool Candidatos</p>
              <p className="mt-1 text-xl font-bold text-stone-900">
                {report.protocol.candidate_pool_size?.toLocaleString()}
              </p>
              <p className="text-[11px] text-stone-500">cómputo en {report.elapsed_ms} ms</p>
            </div>
          </div>
        )}
      </section>

      {loading ? (
        <div className="flex flex-col items-center justify-center py-20 text-stone-500">
          <Loader2 className="h-8 w-8 animate-spin text-brand-600" />
          <p className="mt-3 text-sm font-medium">
            Ejecutando validación cruzada 80/20 de los 5 modelos...
          </p>
        </div>
      ) : (
        <>
          {/* Tabla Comparativa Completa */}
          <section className="overflow-hidden rounded-3xl border border-stone-200 bg-white shadow-sm">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-stone-200 text-left text-xs">
                <thead className="bg-stone-50 text-[11px] font-bold uppercase tracking-wider text-stone-500">
                  <tr>
                    <th className="px-5 py-3.5">Modelo Evaluado</th>
                    <th className="px-3 py-3.5 text-center">MAE</th>
                    <th className="px-3 py-3.5 text-center">RMSE</th>
                    <th className="px-3 py-3.5 text-center">Cob. Pred.</th>
                    <th className="px-3 py-3.5 text-center">Precision@5</th>
                    <th className="px-3 py-3.5 text-center">Precision@10</th>
                    <th className="px-3 py-3.5 text-center">Recall@10</th>
                    <th className="px-3 py-3.5 text-center">NDCG@10</th>
                    <th className="px-3 py-3.5 text-center">Cob. Catálogo</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-stone-100">
                  {models.map((m) => {
                    const isHybrid = m.id === 'hybrid';
                    return (
                      <tr
                        key={m.id}
                        className={isHybrid ? 'bg-brand-50/60 font-semibold' : 'hover:bg-stone-50/70'}
                      >
                        <td className="px-5 py-4">
                          <div className="flex items-center gap-2">
                            {isHybrid && <Award className="h-4 w-4 text-brand-600 shrink-0" />}
                            <div>
                              <p className="text-sm font-bold text-stone-900">{m.name}</p>
                              <p className="text-[11px] font-normal text-stone-500">
                                {m.description}
                              </p>
                            </div>
                          </div>
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          <span className={m.mae === bestMae ? 'text-emerald-700 font-bold' : ''}>
                            {m.mae?.toFixed(4)}
                          </span>
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          <span className={m.rmse === bestRmse ? 'text-emerald-700 font-bold' : ''}>
                            {m.rmse?.toFixed(4)}
                          </span>
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          {m.prediction_coverage_pct.toFixed(1)}%
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          <span
                            className={
                              m.precision_at_5 === bestP5
                                ? 'rounded-md bg-emerald-100 px-2 py-0.5 text-emerald-800 font-bold'
                                : ''
                            }
                          >
                            {m.precision_at_5.toFixed(4)}
                          </span>
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          {m.precision_at_10.toFixed(4)}
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          {m.recall_at_10.toFixed(4)}
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          <span
                            className={
                              m.ndcg_at_10 === bestNdcg
                                ? 'rounded-md bg-brand-600 px-2 py-0.5 text-white font-bold'
                                : ''
                            }
                          >
                            {m.ndcg_at_10.toFixed(4)}
                          </span>
                        </td>
                        <td className="px-3 py-4 text-center font-mono">
                          {m.catalog_coverage_pct.toFixed(2)}%
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>

          {/* Gráficas visuales de barras para Ranking (Precision@5 y NDCG@10) */}
          <section className="grid grid-cols-1 gap-6 md:grid-cols-2">
            <div className="rounded-3xl border border-stone-200 bg-white p-6 shadow-sm">
              <h3 className="font-editorial text-lg font-bold text-stone-900">
                Calidad de Ranking Top-5 (Precision@5)
              </h3>
              <p className="mt-0.5 text-xs text-stone-500">
                Proporción de libros relevantes (≥ 4★ en Test) acertados en el Top 5.
              </p>
              <div className="mt-5 space-y-3.5">
                {models.map((m) => (
                  <div key={m.id}>
                    <div className="flex items-center justify-between text-xs font-medium">
                      <span className="text-stone-700">{m.name}</span>
                      <span className="font-mono font-bold text-stone-900">
                        {m.precision_at_5.toFixed(4)}
                      </span>
                    </div>
                    <div className="mt-1.5 h-2.5 w-full overflow-hidden rounded-full bg-stone-100">
                      <div
                        style={{ width: `${Math.max(3, (m.precision_at_5 / bestP5) * 100)}%` }}
                        className={`h-full rounded-full transition-all duration-500 ${m.id === 'hybrid' ? 'bg-brand-600' : 'bg-stone-400'
                          }`}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-3xl border border-stone-200 bg-white p-6 shadow-sm">
              <h3 className="font-editorial text-lg font-bold text-stone-900">
                Ordenamiento por Relevancia (NDCG@10)
              </h3>
              <p className="mt-0.5 text-xs text-stone-500">
                Evalúa no solo cuántos aciertos hay en el Top 10, sino qué tan arriba aparecen.
              </p>
              <div className="mt-5 space-y-3.5">
                {models.map((m) => (
                  <div key={m.id}>
                    <div className="flex items-center justify-between text-xs font-medium">
                      <span className="text-stone-700">{m.name}</span>
                      <span className="font-mono font-bold text-stone-900">
                        {m.ndcg_at_10.toFixed(4)}
                      </span>
                    </div>
                    <div className="mt-1.5 h-2.5 w-full overflow-hidden rounded-full bg-stone-100">
                      <div
                        style={{ width: `${Math.max(3, (m.ndcg_at_10 / bestNdcg) * 100)}%` }}
                        className={`h-full rounded-full transition-all duration-500 ${m.id === 'hybrid' ? 'bg-emerald-600' : 'bg-stone-400'
                          }`}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </section>

          <div className="flex items-start gap-2.5 rounded-2xl bg-emerald-50/80 px-4 py-3.5 text-xs text-emerald-900 ring-1 ring-emerald-200">
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-700" />
            {/* <span>
              <strong>Conclusión experimental:</strong> el <em>Recomendador Híbrido Propuesto</em>{' '}
              combina la precisión numérica de <strong>MSD/JMSD</strong> con la cobertura de{' '}
              <strong>TruncatedSVD</strong> y el perfil semántico <strong>TF-IDF</strong>, logrando
              100 % de cobertura de predicción, menor error que los modelos colaborativos aislados y
              más del doble de <strong>Precision@5</strong> y <strong>NDCG@10</strong>.
            </span> */}
          </div>
        </>
      )}
    </div>
  );
}
