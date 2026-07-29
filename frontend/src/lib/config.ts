/**
 * Central API base URLs for the whole dashboard. Two backends sit behind the one
 * Next.js app (both reachable from localhost:3000):
 *   - NEWS_API  — the ingestion middleware (GET /api/news), default :8000
 *   - PRED_API  — the prediction/agent API (create_app: /predictions, /metrics,
 *                 /catalysts/*, /presets, /screener, /agents/*), default :8001
 *
 * Override either in frontend/.env.local (NEXT_PUBLIC_API_URL /
 * NEXT_PUBLIC_PREDICTION_API_URL) for a deployed backend.
 */
export const NEWS_API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const PRED_API = process.env.NEXT_PUBLIC_PREDICTION_API_URL ?? "http://localhost:8001";
