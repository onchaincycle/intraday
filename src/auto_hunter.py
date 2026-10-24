"""
Auto-Hunt Intraday (2026-10-23: INTRADAY SAJA — swing dihapus atas keputusan user).
Memindai 2 kuadran: ⚡ Intraday Short & ⚡ Intraday Long (logika v3 di src/intraday_core.py), lalu:
  - Mengecualikan koin yang sudah punya posisi (anti-pyramiding)
  - Kuota slot per arah & circuit breaker harian
  - Ranking Alpha (skor x RRR x sesi x funding) + filter spread orderbook
  - AI Gemini gatekeeper (dipertahankan atas keputusan user)
"""

import time
import logging
from typing import Dict, List, Optional, Tuple
import pandas as pd

logger = logging.getLogger("AutoHuntSniper")

from src.database import Database
from src.downloader import DataDownloader
from src.daytrade_engine import IntradayDaytradeEngine
from src.daytrade_long_engine import IntradayLongEngine
from src.trader import HyperliquidTrader
from src.position_manager import PositionManager
from src.profiles import get_profile
from src.forward_test import ForwardTestManager
from src.ai_engine import ai_advisor
from src import position_tags


class AutoHuntSniper:
    def __init__(self, db: Database, trader: Optional[HyperliquidTrader] = None):
        self.db = db
        self.trader = trader or HyperliquidTrader()
        self.pm = PositionManager(self.trader)
        self.ft = ForwardTestManager()

    def run_hunt_cycle(self) -> Dict:
        """
        Executes a full Omni-Stream hunting cycle.
        Returns a structured dictionary with:
          - session_info: Current market session status
          - slot_status: Active slots per stream/direction
          - leaderboard: Top 3 ranked candidates across all 4 quadrants
          - winner: Selected candidate that passed AI audit (or None if all vetoed)
          - ai_audit: Full Gemini Flash analysis for the winner
          - waterfall_log: Log of candidates evaluated/rejected by AI
        """
        # Early Guard: Cek apakah mode otonom dibekukan untuk wallet migration
        try:
            from src.autonomous_engine import AutonomousEngine
            engine = AutonomousEngine(self.trader)
            gov_snap = engine.get_live_governor_snapshot()
            if not gov_snap.get("enabled", True) or not gov_snap.get("can_open_new_position", True):
                return {
                    "status": "BLOCKED",
                    "reason": gov_snap.get("gate_summary", "Mode Otonom dibekukan"),
                    "session_info": {},
                    "slot_status": {},
                    "leaderboard": [],
                    "winner": None,
                    "ai_audit": None,
                    "waterfall_log": []
                }
        except Exception as e:
            logger.warning(f"[AutoHunter] Gagal membaca governor snapshot ({e}), mengaktifkan fail-closed: BLOCKED")
            return {
                "status": "BLOCKED",
                "reason": f"Fail-closed guard: Error memeriksa governor ({e})",
                "session_info": {},
                "slot_status": {},
                "leaderboard": [],
                "winner": None,
                "ai_audit": None,
                "waterfall_log": []
            }

        # 1. Sync live market snapshot (~0.5s)
        try:
            DataDownloader(self.db).sync_snapshot_only()
        except Exception:
            pass

        # 2. Check live account state and active positions
        account_state = self.trader.get_account_state()
        equity = float(account_state.get("equity", 0.0))
        available = float(account_state.get("available", 0.0))

        slot_plan = HyperliquidTrader.get_equity_slot_plan(equity=equity)
        DUST_THRESHOLD_USD = float(slot_plan.get("dust_threshold_usd", 3.0))
        RUNNER_EXEMPT_USD = float(slot_plan.get("runner_exempt_margin_usd", 6.0))

        lifecycle = self.pm.sync_active_positions()
        active_positions_map = {}
        for p in account_state.get("positions", []):
            coin = p.get("coin")
            if coin and float(p.get("size", 0)) > 0:
                side = p.get("side", "SHORT")
                lc = lifecycle.get(coin, {})
                strat = "DAYTRADE"
                margin_used = float(p.get("margin_used", 0.0))
                is_risk_free_runner = bool(lc.get("trimmed_10pct") or lc.get("early_be_shield")) and margin_used <= RUNNER_EXEMPT_USD
                active_positions_map[coin] = {
                    "strategy": strat,
                    "side": side,
                    "margin_used": margin_used,
                    "is_dust": (margin_used < DUST_THRESHOLD_USD) or is_risk_free_runner
                }

        # Mode PAPER: trade kertas terbuka ikut dihitung sebagai posisi (slot & anti-dobel koin) agar simulasi realistis
        try:
            from src import trading_mode, paper_trader
            if trading_mode.get_mode() == "PAPER":
                for t in paper_trader.open_trades():
                    active_positions_map.setdefault(t["coin"], {"strategy": "DAYTRADE", "side": t["side"],
                                                                "margin_used": float(t.get("margin_usd", 0.0)),
                                                                "is_dust": False, "paper": True})
        except Exception:
            pass

        # 3. Kuota slot intraday per arah (dust & runner bebas-risiko tidak dihitung)
        dt_short_coins = [c for c, s in active_positions_map.items() if s.get("side") == "SHORT" and not s.get("is_dust")]
        dt_long_coins = [c for c, s in active_positions_map.items() if s.get("side") == "LONG" and not s.get("is_dust")]
        dust_coins = [f"{c} (${s.get('margin_used', 0):.2f})" for c, s in active_positions_map.items() if s.get("is_dust")]
        dt_short_count = len(dt_short_coins)
        dt_long_count = len(dt_long_coins)

        prof_dt = get_profile("DAYTRADE")
        max_total = min(int(prof_dt.get("max_concurrent", 4)), int(slot_plan.get("max_intraday", 4)))
        max_dt_short = int(prof_dt.get("max_concurrent_short", 2))
        max_dt_long = int(prof_dt.get("max_concurrent_long", 2))
        total_open = (dt_short_count + dt_long_count) < max_total

        # 4. Sesi & circuit breaker intraday
        day_engine = IntradayDaytradeEngine()
        session_info = day_engine.get_market_session()
        losses_today = self.ft.count_losses_today("DAYTRADE")
        max_losses = prof_dt.get("max_daily_losses") or 2
        circuit_breaker_active = (losses_today >= max_losses)
        session_blocked = bool(prof_dt.get("session_gate")) and not session_info.get("is_prime")

        slot_status = {
            "dt_short": {"active": dt_short_count, "max": max_dt_short, "coins": dt_short_coins,
                         "open": dt_short_count < max_dt_short and total_open and not circuit_breaker_active and not session_blocked},
            "dt_long": {"active": dt_long_count, "max": max_dt_long, "coins": dt_long_coins,
                        "open": dt_long_count < max_dt_long and total_open and not circuit_breaker_active and not session_blocked},
            "circuit_breaker": circuit_breaker_active,
            "session_blocked": session_blocked,
            "losses_today": losses_today,
            "session": session_info,
            "dust_coins": dust_coins
        }

        print("\n🔍 PRA-SCAN: STATUS SLOT INTRADAY")
        try:
            from src.portfolio_sizer import PortfolioSizer
            cycle_mod = PortfolioSizer.calculate_cycle_sizing_modifier()
            w_phase = cycle_mod.get("weekly_phase", "NORMAL")
            w_mult = cycle_mod.get("weekly_multiplier", 1.0)
            m_phase = cycle_mod.get("monthly_phase", "NORMAL")
            m_mult = cycle_mod.get("monthly_multiplier", 1.0)
            c_mult = cycle_mod.get("composite_multiplier", 1.0)
            print(f"  • 📅 Siklus Mingguan: {w_phase} ({w_mult}x)")
            print(f"  • 🗓️ Siklus Bulanan : {m_phase} ({m_mult}x)")
            print(f"  • ⚖️ Pengali Siklus : {c_mult}x (Pengali Risiko Kalender)")
        except Exception:
            pass
        if dust_coins:
            print(f"  • 🧹 Dust / runner bebas-risiko: {', '.join(dust_coins)} (tidak dihitung slot)")
        for key, label in (("dt_short", "⚡ Intraday Short"), ("dt_long", "⚡ Intraday Long ")):
            st = slot_status[key]
            if st["open"]:
                print(f"  • [{label}] : 🔍 Slot terbuka ({st['active']}/{st['max']}) -> memindai candle 15m...")
            else:
                why = ("Circuit breaker aktif" if circuit_breaker_active else
                       "Di luar sesi London/NY" if session_blocked else
                       f"Slot penuh ({st['active']}/{st['max']}; total maks {max_total})")
                print(f"  • [{label}] : 🛑 {why} -> dilewati")

        # 5. Scan 2 kuadran intraday
        all_candidates: List[Dict] = []

        # Quadrant 1: Intraday Short
        if slot_status["dt_short"]["open"]:
            try:
                df_dt_s = day_engine.scan_intraday_candidates(top_n=20)
                if not df_dt_s.empty:
                    for _, r in df_dt_s.iterrows():
                        c = r["coin"]
                        if c in active_positions_map:
                            continue
                        all_candidates.append(self._format_intraday_candidate(r, "SHORT", session_info))
            except Exception as e:
                print(f"[!] Warning: Gagal scan Intraday Short: {e}")

        # Quadrant 2: Intraday Long
        if slot_status["dt_long"]["open"]:
            try:
                day_long_engine = IntradayLongEngine()
                df_dt_l = day_long_engine.scan_intraday_candidates(top_n=20)
                if not df_dt_l.empty:
                    for _, r in df_dt_l.iterrows():
                        c = r["coin"]
                        if c in active_positions_map:
                            continue
                        all_candidates.append(self._format_intraday_candidate(r, "LONG", session_info))
            except Exception as e:
                print(f"[!] Warning: Gagal scan Intraday Long: {e}")

        if not all_candidates:
            return {
                "equity": equity,
                "available": available,
                "session_info": session_info,
                "slot_status": slot_status,
                "leaderboard": [],
                "winner": None,
                "ai_audit": None,
                "waterfall_log": ["Tidak ada koin yang memenuhi kriteria setup intraday pada slot yang masih terbuka."]
            }

        # 6. Compute Unified Alpha Score & Enrich Top Candidates with 5-Minute Orderbook Spread Filter
        for cand in all_candidates:
            cand["alpha_score"] = self._compute_alpha_score(cand, session_info)

        all_candidates.sort(key=lambda x: x["alpha_score"], reverse=True)

        # Check live L2 Orderbook 5-minute spread & depth on top 5 finalists ($200+ equity slippage guard)
        try:
            from src.hyperliquid_client import HyperliquidClient
            hl_client = HyperliquidClient()
            for cand in all_candidates[:5]:
                ob = hl_client.get_l2_orderbook(cand["coin"], depth=15)
                if ob.get("available"):
                    sp_bps = float(ob.get("avg_spread_5m_bps", ob.get("spread_bps", 5.0)))
                    liq_grade = ob.get("liquidity_grade", "HEALTHY_LIQUIDITY")
                    cand["avg_spread_5m_bps"] = sp_bps
                    cand["liquidity_grade"] = liq_grade
                    if sp_bps <= 5.0:
                        cand["alpha_score"] = round(cand["alpha_score"] * 1.03, 2)  # Tight spread bonus
                    elif sp_bps > 15.0:
                        cand["alpha_score"] = round(cand["alpha_score"] * 0.85, 2)  # Wide spread slippage penalty
        except Exception:
            pass

        # Re-sort descending by spread-adjusted alpha_score
        all_candidates.sort(key=lambda x: x["alpha_score"], reverse=True)
        leaderboard = all_candidates[:3]

        # 7. AI Waterfall Audit: Start with #1, fallback to #2, #3 if rejected
        waterfall_log = []
        winner = None
        winner_audit = None

        for idx, cand in enumerate(leaderboard):
            audit_res = self.audit_candidate(cand, equity)
            is_approved = audit_res.get("approve", False)
            conf_score = audit_res.get("confidence_score", 0)

            if is_approved and conf_score >= 60:
                winner = cand
                winner_audit = audit_res
                waterfall_log.append(f"Kandidat #{idx+1} [{cand['side']} {cand['coin']} | {cand['badge']}] DISIKAPI POSITIF (APPROVED Grade {audit_res.get('conviction_grade')} | Keyakinan: {conf_score}/100)")
                break
            else:
                reason = audit_res.get("verdict_reason", "Risiko terdeteksi tinggi")
                waterfall_log.append(f"Kandidat #{idx+1} [{cand['side']} {cand['coin']}] DITOLAK AI: {reason}")

        return {
            "equity": equity,
            "available": available,
            "session_info": session_info,
            "slot_status": slot_status,
            "leaderboard": leaderboard,
            "all_candidates": all_candidates,
            "winner": winner,
            "ai_audit": winner_audit,
            "waterfall_log": waterfall_log
        }

    def audit_candidate(self, cand: Dict, equity: float) -> Dict:
        """Runs Gemini Flash Pre-Trade Sanity Audit and calculates conviction sizing."""
        coin = cand["coin"]
        side = cand["side"]
        strat = cand["stream"]
        prof = get_profile(strat)
        sl = cand["sl_pct"]
        lev = prof.get("leverage", 2)
        risk_pct = prof.get("risk_pct", 1.0)

        # Baseline margin anchor
        base_risk = self.trader.calculate_risk_margin(equity=equity, risk_pct=risk_pct, sl_pct=sl, leverage=lev, multiplier=1.0)
        base_margin = base_risk["recommended_margin"]

        # Market Cycle Modifier (Day of Week & Monthly Expiry)
        try:
            from src.portfolio_sizer import PortfolioSizer
            cycle_mod = PortfolioSizer.calculate_cycle_sizing_modifier()
            cycle_mult = float(cycle_mod.get("composite_multiplier", 1.0))
        except Exception:
            cycle_mult = 1.0
            cycle_mod = {}

        pre_trade_context = {
            "strategy": strat,
            "side": side,
            "leverage": lev,
            "equity": equity,
            "base_margin": base_margin,
            "sl_pct": sl,
            "funding_1h_pct": cand.get("funding_1h", 0.0),
            "weekly_cycle": cycle_mod.get("weekly_phase"),
            "weekly_stance": cycle_mod.get("weekly_stance"),
            "monthly_cycle": cycle_mod.get("monthly_phase"),
            "monthly_stance": cycle_mod.get("monthly_stance"),
            "cycle_multiplier": cycle_mult
        }
        cand_summary = {
            "coin": coin,
            "side": side,
            "strategy": strat,
            "score": cand["score"],
            "price": cand["price"],
            "funding_rate": cand.get("funding_1h", 0.0),
            "squeeze_risk": cand.get("squeeze_risk", "NORMAL"),
            "pinbar_type": cand.get("pinbar_type"),
            "bos_status": cand.get("bos_status")
        }

        audit_res = ai_advisor.evaluate_pre_trade_signal(cand_summary, pre_trade_context)
        size_mult = float(audit_res.get("size_multiplier", 1.0))

        # Dynamic Leverage: Boost to 3x for Grade A / A+ Intraday setups
        conviction_grade = str(audit_res.get("conviction_grade", "B")).upper()
        if strat == "DAYTRADE" and conviction_grade in ["A", "A+"]:
            lev = 3
            audit_res["leverage"] = 3
            cand["leverage"] = 3
            cand["dynamic_leverage_note"] = "⚡ Leverage 3x (Boosted Grade A/A+)"
        else:
            audit_res["leverage"] = lev
            cand["leverage"] = lev
            cand["dynamic_leverage_note"] = f"Leverage {lev}x (Standard)"

        # Compute final conviction margin (blended with Calendar Cycle Multiplier)
        effective_mult = round(max(0.35, min(1.40, size_mult * cycle_mult)), 2)
        ai_risk = self.trader.calculate_risk_margin(equity=equity, risk_pct=risk_pct, sl_pct=sl, leverage=lev, multiplier=effective_mult)

        audit_res["base_margin"] = base_margin
        audit_res["cycle_multiplier"] = cycle_mult
        audit_res["weekly_phase"] = cycle_mod.get("weekly_phase")
        audit_res["monthly_phase"] = cycle_mod.get("monthly_phase")
        audit_res["final_margin"] = ai_risk["recommended_margin"]
        audit_res["max_risk_usd"] = ai_risk["max_risk_usd"]
        audit_res["effective_risk_pct"] = ai_risk["effective_risk_pct"]
        return audit_res

    def _format_intraday_candidate(self, r: pd.Series, side: str, session_info: Dict) -> Dict:
        coin = r["coin"]
        curr_px = float(r["current_px"])
        sl_px = float(r["sl_px"])
        sl_pct = float(r["sl_pct"])
        tp1_px = float(r["tp1_px"])
        tp1_pct = float(r["tp1_pct"])
        tp2_px = float(r["tp2_px"])
        tp2_pct = float(r["tp2_pct"])
        rrr = float(r.get("rrr", 2.0))
        score = float(r.get("score", 75.0))
        f_rate = float(r.get("funding_1h_pct", 0.0))

        from src.claude_crypto_engine import ClaudeCryptoEngine
        sec_info = ClaudeCryptoEngine.get_coin_sector(coin)

        badge = "⚡ INTRADAY SHORT" if side == "SHORT" else "⚡ INTRADAY LONG"
        reasons = r.get("reasons", [])
        if isinstance(reasons, str):
            reasons = [reasons]

        return {
            "coin": coin,
            "side": side,
            "stream": "DAYTRADE",
            "badge": badge,
            "score": round(score, 1),
            "price": curr_px,
            "sl_px": sl_px,
            "sl_pct": sl_pct,
            "tp1_px": tp1_px,
            "tp1_pct": tp1_pct,
            "tp2_px": tp2_px,
            "tp2_pct": tp2_pct,
            "runner_px": None,
            "runner_pct": None,
            "rrr": rrr,
            "verdict": r.get("verdict", "SIAP INTRADAY"),
            "funding_1h": f_rate,
            "squeeze_risk": "HIGH_SQUEEZE" if (side == "SHORT" and f_rate < -0.015) else "NORMAL",
            "reasons": reasons,
            "pinbar_type": "BULLISH_WICK" if side == "LONG" else "BEARISH_PINBAR",
            "bos_status": "CHoCH_CONFIRMED" if r.get("bos_detected") else "PENDING",
            "sector": sec_info["label"],
            "sector_badge": sec_info["badge"],
            "max_hold_hours": 6,
            "timeframe": "15-Menit",
            "ready": bool(r.get("ready", False)),
            "engine_version": r.get("engine_version", ""),
            "vetoes": r.get("vetoes", []) if isinstance(r.get("vetoes", []), list) else []
        }

    def _compute_alpha_score(self, cand: Dict, session_info: Dict) -> float:
        """
        Calculates unified multi-factor ranking score:
        Score * RRR factor * Session Factor * Funding Alignment * Claude_Crypto Phase
        """
        score = cand["score"]
        rrr = cand.get("rrr", 1.8)
        rrr_factor = min(1.25, max(0.85, rrr / 1.5))

        alpha = score * rrr_factor

        # Session Factor (intraday)
        alpha += 4.0 if session_info.get("is_prime") else -8.0

        # Funding rate alignment
        f_rate = cand.get("funding_1h", 0.0)
        if cand["side"] == "SHORT" and f_rate > 0.005:
            alpha += 3.0  # Longs pay shorts (favorable yield)
        elif cand["side"] == "LONG" and f_rate < -0.005:
            alpha += 3.0  # Shorts pay longs & potential squeeze pump

        # Market Cycle Factor (Day of Week & Monthly Seasonality)
        try:
            from src.portfolio_sizer import PortfolioSizer
            cycle_mod = PortfolioSizer.calculate_cycle_sizing_modifier()
            w_res = cycle_mod.get("weekly_cycle")
            m_res = cycle_mod.get("monthly_cycle")
            cand_side = cand.get("side", "SHORT")

            if w_res:
                # Tuesday Golden Day boost for SHORT
                if getattr(w_res, "day_of_week", -1) == 1 and cand_side == "SHORT":
                    alpha += 3.5
                # Weekend Liquidity Vacuum penalty
                elif getattr(w_res, "day_of_week", -1) in (5, 6):
                    alpha -= 5.0

            if m_res:
                # Monthly Turn-of-Month (Day 1-5) squeeze risk penalty for SHORT
                if getattr(m_res, "day_of_month", 99) <= 5 and cand_side == "SHORT":
                    alpha -= 4.0
                # Monthly Harvest (Day 25+) Deribit Expiry boost for SHORT
                elif getattr(m_res, "day_of_month", 0) >= 25 and cand_side == "SHORT":
                    alpha += 3.0
        except Exception:
            pass

        return round(alpha, 1)
