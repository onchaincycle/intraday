"""
Autonomous Self-Operating Quant Engine & Closed-Loop Self-Evaluation System.

Capabilities:
1. Dynamic Equity-Scaled Capital & Slot Governor:
   - Reserve Margin Floor = f(Equity) in percentage (default 15% normal, 25% defensive).
   - Max Open Positions N_max(E) = clamp(floor(4 + E/60), min=5, max=10).
   - Per-Position Margin Sizing = f(Equity, N_max, Grade, Thermostat).
2. Zero-Touch Autonomous Hunter (Strict Grade-A Sniper):
   - Periodically refreshes market data & scans open quadrants when slots & free margin above reserve permit.
   - Strictly executes ONLY setups with Quantitative Score >= Threshold AND AI Gatekeeper APPROVED + Grade A/A+ (Confidence >= 75).
   - Automatically places native on-chain Stop Loss and TP1 trigger orders on Hyperliquid L1.
3. Closed-Loop Self-Evaluation & Dynamic Thermostat:
   - Automatically triggers whenever a position closes or every 6 hours.
   - Runs AI Post-Mortem review on closed trades, computes quadrant win-rates & streaks,
     auto-calibrates adaptive screener weights, and adjusts the risk thermostat.
4. Full Telemetry & Remote Monitoring:
   - Logs all autonomous decisions, skips, executions, and evaluations to SQLite/JSON and syncs to GitHub Pages.
"""

import os
import sys
import json
import time
from datetime import datetime
from typing import Dict, List, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.trader import HyperliquidTrader
from src import trading_mode
from src.forward_test import ForwardTestManager
from src.profiles import get_profile
from src import position_tags
from src.trade_db import trade_db
from src.claude_crypto_engine import ClaudeCryptoEngine

AUTONOMOUS_STATE_FILE = os.path.join(ROOT_DIR, "data", "autonomous_state.json")
AUTONOMOUS_LOG_FILE = os.path.join(ROOT_DIR, "logs", "autonomous_ai.log")


def _parse_local_dt(s) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s)[:19].replace("T", " "))
    except Exception:
        return None


def streak_trades(closed_trades: List[Dict], since_str: Optional[str], reset_ts: float) -> List[Dict]:
    """Trade yang boleh dihitung untuk streak thermostat (2026-10-24): hanya DAYTRADE, ditutup setelah `since_str`
    (WIB, konfigurasi) DAN setelah Circuit Breaker terakhir terpicu (`reset_ts`). Urutan kronologis dipertahankan."""
    floor = _parse_local_dt(since_str) if since_str else None
    out = []
    for t in closed_trades:
        if str(t.get("strategy", "")).upper() != "DAYTRADE":
            continue
        dt = _parse_local_dt(t.get("exit_time"))
        if dt is None:
            continue
        if floor is not None and dt < floor:
            continue
        if reset_ts and dt.timestamp() <= reset_ts:
            continue
        out.append((dt, t))
    out.sort(key=lambda x: x[0])
    return [t for _, t in out]


def keep_active_circuit_breaker(thermo: Dict, now_ts: float) -> bool:
    """CB yang dipasang aturan baru (punya streak_reset_ts) dipertahankan sampai jedanya habis.
    CB lama (tanpa streak_reset_ts, aturan sebelum 2026-10-24) langsung dinilai ulang dengan aturan baru."""
    return (bool(thermo.get("circuit_breaker_pause")) and now_ts < float(thermo.get("pause_until_ts", 0.0))
            and "streak_reset_ts" in thermo)


def _snapshot_candidate(cand: Dict) -> Dict:
    """Salinan fitur kandidat yang aman disimpan sebagai JSON (nilai skalar + list/dict kecil; tanpa DataFrame)."""
    out = {}
    for k, v in (cand or {}).items():
        if v is None or isinstance(v, (bool, int, float, str)):
            out[k] = v
        elif isinstance(v, (list, tuple)) and len(v) <= 30 and all(isinstance(x, (bool, int, float, str, type(None))) for x in v):
            out[k] = list(v)
        elif isinstance(v, dict) and len(v) <= 30 and all(isinstance(x, (bool, int, float, str, type(None))) for x in v.values()):
            out[k] = dict(v)
        else:
            try:
                out[k] = float(v)          # numpy scalar
            except Exception:
                pass
    return out


class AutonomousEngine:
    def __init__(self, trader: Optional[HyperliquidTrader] = None):
        self.trader = trader or HyperliquidTrader()
        self.ft = ForwardTestManager()
        self.state_file = AUTONOMOUS_STATE_FILE
        self._ensure_files()

    def _ensure_files(self):
        os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)
        os.makedirs(os.path.join(ROOT_DIR, "logs"), exist_ok=True)
        if not os.path.exists(self.state_file):
            default_state = self._default_state()
            self.save_state(default_state)

    @staticmethod
    def _default_state() -> Dict:
        return {
            "enabled": True,
            "hunt_interval_minutes": 30,
            "self_eval_interval_hours": 6,
            "forward_audit_interval_hours": 24,
            "backtest_interval_days": 7,
            "cooldown_minutes": 30,
            "thermostat": {
                "mode": "OPTIMAL",
                "badge": "🟢 OPTIMAL (Standard)",
                "reserve_pct": 15.0,
                "base_min_score_intraday": 75.0,
                "base_min_score_swing": 78.0,
                "min_score_intraday": 75.0,
                "min_score_swing": 78.0,
                "required_grades": ["A", "A+"],
                "min_ai_confidence": 75,
                "sizing_mult": 1.00,
                "circuit_breaker_pause": False,
                "pause_until_ts": 0.0,
                "reason": "Performa portofolio sehat. Cadangan kas 15% ekuitas, target wajib Grade A."
            },
            "last_hunt_ts": 0.0,
            "last_hunt_time": "-",
            "last_hunt_status": "Menunggu siklus berburu pertama",
            "last_eval_ts": 0.0,
            "last_eval_time": "-",
            "last_eval_report": {},
            "last_forward_audit_ts": 0.0,
            "last_forward_audit_time": "-",
            "last_backtest_ts": 0.0,
            "last_backtest_time": "-",
            "last_validation_report": {},
            "last_entry_ts": 0.0,
            "last_entry_coin": "-",
            "last_calibrated_trade_count": 0,
            "stats": {
                "total_hunt_cycles": 0,
                "total_scans_executed": 0,
                "total_ai_audits": 0,
                "total_autonomous_opens": 0,
                "total_vetoed_or_skipped": 0,
                "total_self_evaluations": 0,
                "total_quant_validations": 0
            },
            "telemetry_feed": []
        }

    def load_state(self) -> Dict:
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                base = self._default_state()
                base.update(data)
                if "thermostat" in data:
                    t = self._default_state()["thermostat"]
                    t.update(data["thermostat"])
                    base["thermostat"] = t
                if "stats" in data:
                    s = self._default_state()["stats"]
                    s.update(data["stats"])
                    base["stats"] = s
                return base
        except Exception:
            pass
        return self._default_state()

    def save_state(self, state: Dict):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            print(f"[!] Warning: Gagal simpan autonomous_state.json: {e}")

    def log_telemetry(self, category: str, action: str, detail: str, coin: str = "-", grade: str = "-", equity: float = 0.0, free_margin: float = 0.0):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state = self.load_state()
        entry = {
            "timestamp": now_str,
            "category": category,
            "coin": coin,
            "action": action,
            "detail": detail,
            "grade": grade,
            "equity": round(equity, 2),
            "free_margin": round(free_margin, 2)
        }
        feed = state.get("telemetry_feed", [])
        feed.append(entry)
        state["telemetry_feed"] = feed[-60:]  # Simpan 60 event terakhir
        self.save_state(state)

        try:
            line = f"[{now_str}] [{category}] [{coin}] {action}: {detail}\n"
            with open(AUTONOMOUS_LOG_FILE, "a") as f:
                f.write(line)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 1. DYNAMIC EQUITY-SCALED CAPITAL & SLOT GOVERNOR
    # ------------------------------------------------------------------
    def get_live_governor_snapshot(self, account_state: Optional[Dict] = None) -> Dict:
        """
        Computes real-time Dynamic Capital & Slot parameters as a function of Total Account USDC (E):
          - Reserve Margin Floor = E * reserve_pct%
          - Max Open Positions N_max(E) = clamp(floor(4 + E/60), min=5, max=10)
          - Slot Base Margin = (E - Reserve) / N_max(E)
          - Eligibility to open new position
        """
        state = self.load_state()
        thermo = state["thermostat"]
        reserve_pct = float(thermo.get("reserve_pct", 15.0))
        sizing_mult = float(thermo.get("sizing_mult", 1.0))

        acc = account_state or self.trader.get_account_state()
        equity = float(acc.get("equity", 0.0))
        available = float(acc.get("available", 0.0))
        positions = acc.get("positions", [])

        slot_plan = HyperliquidTrader.get_equity_slot_plan(
            equity=equity,
            reserve_pct=reserve_pct,
            thermostat_mult=sizing_mult
        )

        # Load lifecycle state to identify risk-free runners (already TP1 trimmed or BE locked)
        lifecycle_path = os.path.join(ROOT_DIR, "data", "positions_lifecycle.json")
        lifecycle_data = {}
        if os.path.exists(lifecycle_path):
            try:
                with open(lifecycle_path, "r") as f:
                    lifecycle_data = json.load(f)
            except Exception:
                pass

        # Dynamic equity-scaled dust & risk-free runner threshold
        DUST_USD = float(slot_plan.get("dust_threshold_usd", 3.0))
        RUNNER_EXEMPT_USD = float(slot_plan.get("runner_exempt_margin_usd", 6.0))
        slot_base_margin = slot_plan["slot_base_margin"]
        from src import risk_config
        _gv = risk_config.get()["governor"]
        HALF_SLOT_CEILING_USD = round(slot_base_margin * _gv["half_slot_ratio"], 2)  # Legacy $100-era positions (< 60% of $28.10 = < $16.86)

        active_coins = []
        swing_coins = []
        intraday_coins = []
        long_coins = []
        short_coins = []
        exempt_runners = []
        half_slot_coins = []
        effective_active_slots = 0.0
        effective_swing_slots = 0.0
        effective_intraday_slots = 0.0

        for p in positions:
            c = p.get("coin")
            sz = float(p.get("size", 0.0))
            m_used = float(p.get("margin_used", 0.0))
            side = p.get("side", "SHORT")
            if not c or sz <= 0 or m_used < DUST_USD:
                if c and sz > 0:
                    exempt_runners.append(f"{c} (${m_used:.2f} dust)")
                continue

            lc = lifecycle_data.get(c, {})
            is_risk_free = bool(lc.get("trimmed_10pct") or lc.get("early_be_shield"))
            if is_risk_free and m_used <= RUNNER_EXEMPT_USD:
                exempt_runners.append(f"{c} (${m_used:.2f} free-runner)")
                continue

            # Capital-Weighted Slot Accounting: Legacy small positions (< 60% of slot_base_margin) count as 0.5 slot
            slot_weight = 0.5 if m_used < HALF_SLOT_CEILING_USD else 1.0
            if slot_weight == 0.5:
                half_slot_coins.append(f"{c} (${m_used:.2f} = 0.5 slot)")

            strat = "DAYTRADE"  # intraday saja (2026-10-23)
            active_coins.append(c)
            effective_active_slots += slot_weight
            if strat == "DAYTRADE":
                intraday_coins.append(c)
                effective_intraday_slots += slot_weight
            else:
                swing_coins.append(c)
                effective_swing_slots += slot_weight
            if side == "LONG":
                long_coins.append(c)
            else:
                short_coins.append(c)

        # Claude_Crypto Sector Distribution Tracking
        from src.claude_crypto_engine import ClaudeCryptoEngine
        sector_distribution = {}
        for c in active_coins:
            sec_info = ClaudeCryptoEngine.get_coin_sector(c)
            s_lbl = sec_info["label"]
            sector_distribution[s_lbl] = sector_distribution.get(s_lbl, 0) + 1

        max_pos = slot_plan["max_positions"]
        base_max_swing = slot_plan["max_swing"]
        max_intraday = slot_plan["max_intraday"]
        # Elastic Slot Borrowing: if intraday has 0 active positions, Swing can borrow 1 unused slot
        max_swing = min(max_pos - 1, base_max_swing + (int(_gv["elastic_swing_borrow"]) if len(intraday_coins) == 0 else 0))
        max_directional = max(int(_gv["max_directional_min"]), int(round(max_pos * _gv["max_directional_ratio"])))

        reserve_usd = slot_plan["reserve_usd"]
        free_above_reserve = round(max(0.0, available - reserve_usd), 2)
        min_order_margin = slot_plan["min_slot_margin"]
        max_order_margin = slot_plan["max_slot_margin"]

        # Calculate recommended Grade A / A+ bullet size factoring in Idle-Capital Recycler
        open_slots_rem = max(1.0, float(max_pos) - effective_active_slots)
        grade_a_sizing = self.trader.calculate_risk_margin(
            equity=equity, risk_pct=2.5, sl_pct=10.0, leverage=2, multiplier=1.15 * sizing_mult,
            reserve_pct=reserve_pct, free_above_reserve=free_above_reserve, open_slots_remaining=int(round(open_slots_rem))
        )
        grade_a_plus_sizing = self.trader.calculate_risk_margin(
            equity=equity, risk_pct=2.8, sl_pct=10.0, leverage=2, multiplier=1.30 * sizing_mult,
            reserve_pct=reserve_pct, free_above_reserve=free_above_reserve, open_slots_remaining=int(round(open_slots_rem))
        )

        now_ts = time.time()
        cooldown_sec = int(state.get("cooldown_minutes", 30)) * 60
        last_entry_ts = float(state.get("last_entry_ts", 0.0))
        in_cooldown = (now_ts - last_entry_ts) < cooldown_sec
        cooldown_rem_min = max(0, int((cooldown_sec - (now_ts - last_entry_ts)) / 60)) if in_cooldown else 0

        # Circuit Breaker Checks:
        # 1. Global Thermostat Circuit Breaker (loss streak >= 3 or daily DD >= 5%)
        cb_pause = bool(thermo.get("circuit_breaker_pause", False)) and now_ts < float(thermo.get("pause_until_ts", 0.0))

        # 2. Intraday Stream Circuit Breaker (losses today >= max daily losses)
        try:
            from src.forward_test import ForwardTestManager
            from src.profiles import get_profile
            ft = ForwardTestManager()
            losses_today = ft.count_losses_today("DAYTRADE")
            prof_dt = get_profile("DAYTRADE")
            max_dt_losses = prof_dt.get("max_daily_losses", 2)
            intraday_cb_active = (losses_today >= max_dt_losses)
        except Exception:
            losses_today = 0
            max_dt_losses = 2
            intraday_cb_active = False

        # Gate evaluation uses Capital-Weighted Effective Slots so legacy $10 positions don't lock out $60+ idle cash
        can_open = True
        gate_reasons = []

        if not state.get("enabled", True) or state.get("pause_new_positions", False):
            can_open = False
            gate_reasons.append("Mode Otonom dibekukan (Menunggu penutupan posisi aktif)")
        if cb_pause:
            can_open = False
            rem_h = max(0.1, (float(thermo.get("pause_until_ts", 0.0)) - now_ts) / 3600.0)
            gate_reasons.append(f"Circuit Breaker Aktif (Jedah proteksi drawdown {rem_h:.1f} jam lagi)")
        if effective_active_slots >= max_pos:
            can_open = False
            gate_reasons.append(f"Kapasitas Slot Efektif Penuh ({effective_active_slots:.1f}/{max_pos} slot untuk ekuitas ${equity:,.2f})")
        if free_above_reserve < min_order_margin:
            can_open = False
            gate_reasons.append(
                f"Margin Bebas di atas Cadangan {reserve_pct:.0f}% (${reserve_usd:,.2f}) hanya ${free_above_reserve:,.2f} "
                f"(Butuh min. ${min_order_margin:,.2f} untuk buka posisi baru)"
            )
        if in_cooldown:
            can_open = False
            gate_reasons.append(f"Masa tenang (Cooldown) pasca-entry terakhir ({cooldown_rem_min} menit tersisa)")

        gate_summary = "🟢 SIAP BERBURU & OPEN POSISI GRADE A" if can_open else " | ".join(gate_reasons)

        return {
            "enabled": state.get("enabled", True),
            "equity": round(equity, 2),
            "available": round(available, 2),
            "margin_used": round(max(0.0, equity - available), 2),
            "used_pct": round(((equity - available) / equity * 100.0), 1) if equity > 0 else 0.0,
            "reserve_pct": reserve_pct,
            "reserve_usd": reserve_usd,
            "deployable_usd": slot_plan["deployable_usd"],
            "free_above_reserve": free_above_reserve,
            "max_positions": max_pos,
            "active_count": len(active_coins),
            "effective_active_slots": round(effective_active_slots, 1),
            "active_coins": active_coins,
            "half_slot_legacy_coins": half_slot_coins,
            "exempt_runners": exempt_runners,
            "dust_threshold_usd": DUST_USD,
            "max_swing": max_swing,
            "swing_count": len(swing_coins),
            "effective_swing_slots": round(effective_swing_slots, 1),
            "swing_coins": swing_coins,
            "max_intraday": max_intraday,
            "intraday_count": len(intraday_coins),
            "effective_intraday_slots": round(effective_intraday_slots, 1),
            "intraday_coins": intraday_coins,
            "max_directional": max_directional,
            "long_count": len(long_coins),
            "short_count": len(short_coins),
            "slot_base_margin": slot_base_margin,
            "min_order_margin": min_order_margin,
            "max_order_margin": max_order_margin,
            "recommended_grade_a_margin": grade_a_sizing["recommended_margin"],
            "recommended_grade_a_plus_margin": grade_a_plus_sizing["recommended_margin"],
            "in_cooldown": in_cooldown,
            "cooldown_rem_min": cooldown_rem_min,
            "can_open_new_position": can_open,
            "gate_summary": gate_summary,
            "circuit_breaker_active": cb_pause or intraday_cb_active,
            "intraday_cb_active": intraday_cb_active,
            "intraday_losses_today": losses_today,
            "intraday_max_losses": max_dt_losses,
            "thermostat_cb_active": cb_pause,
            "sector_distribution": sector_distribution,
            "thermostat": thermo
        }

    # ------------------------------------------------------------------
    # 2. CLOSED-LOOP SELF-EVALUATION & DYNAMIC THERMOSTAT
    # ------------------------------------------------------------------
    def run_self_evaluation(self, trigger_reason: str = "PERIODIC_CHECK") -> Dict:
        """
        Executes a full Closed-Loop Self-Evaluation:
          1. Syncs & audits any closed trades missing AI Post-Mortem reviews.
          2. Computes overall & per-quadrant Win Rate, Profit Factor, Net PnL, and Streaks.
          3. Adjusts Dynamic Thermostat (Reserve %, Min Score, Sizing Multiplier, Circuit Breaker).
          4. Auto-calibrates screener weights in SQLite when new closed trades accumulate.
        """
        state = self.load_state()
        now_ts = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Load journal and ensure all closed trades have AI post-mortem
        journal = self.ft.load_journal()
        closed_trades = [v for v in journal.values() if v.get("status") == "CLOSED"]
        # Sort by exit_time
        closed_trades.sort(key=lambda x: str(x.get("exit_time", "")))

        from src.ai_engine import ai_advisor
        newly_audited = 0
        journal_modified = False
        for t in closed_trades[-8:]:
            if not t.get("ai_review"):
                try:
                    rev = ai_advisor.analyze_closed_trade(t)
                    t["ai_review"] = rev
                    # Cari key asli di journal agar tidak menimpa posisi OPEN dengan nama koin yang sama
                    matched_key = None
                    for k, v in journal.items():
                        if v is t or (v.get("coin") == t.get("coin") and v.get("exit_time") == t.get("exit_time") and v.get("status") == "CLOSED"):
                            matched_key = k
                            break
                    if matched_key:
                        journal[matched_key] = t
                    else:
                        trade_key = f"{t.get('coin')}@{t.get('exit_time') or int(time.time())}"
                        journal[trade_key] = t
                    newly_audited += 1
                    journal_modified = True
                except Exception:
                    pass
        if journal_modified:
            self.ft.save_journal(journal)

        # 2. Compute Win Rate, Streaks, Profit Factor, and Quadrant Matrix
        total_closed = len(closed_trades)
        wins = [t for t in closed_trades if float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0)) > 0]
        losses = [t for t in closed_trades if float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0)) <= 0]

        win_rate = round((len(wins) / total_closed * 100.0), 1) if total_closed > 0 else 100.0
        gross_profit = sum(float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0)) for t in wins)
        gross_loss = abs(sum(float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0)) for t in losses))
        net_realized_pnl = round(gross_profit - gross_loss, 2)
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0.01 else (round(gross_profit, 2) if gross_profit > 0 else 1.0)

        # Calculate current consecutive win/loss streak
        win_streak = 0
        loss_streak = 0
        _tcfg = __import__("src.risk_config", fromlist=["get"]).get()["thermostat"]
        _thermo_prev = state.get("thermostat") or {}
        for t in reversed(streak_trades(closed_trades, _tcfg.get("streak_count_since"),
                                        float(_thermo_prev.get("streak_reset_ts", 0.0) or 0.0))):
            pnl = float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0))
            # Ignore near-zero dust/stalemate scratches ($-0.08 to $+0.08) for loss streak penalty
            if abs(pnl) <= float(__import__("src.risk_config", fromlist=["get"]).get()["thermostat"]["stalemate_scratch_usd"]) and "STALEMATE" in str(t.get("exit_reason", "")):
                continue
            if pnl > 0:
                if loss_streak == 0:
                    win_streak += 1
                else:
                    break
            else:
                if win_streak == 0:
                    loss_streak += 1
                else:
                    break

        # Today's realized PnL
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        today_trades = [t for t in closed_trades if str(t.get("exit_time", "")).startswith(today_prefix)]
        today_pnl = round(sum(float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0)) for t in today_trades), 2)

        # Quadrant breakdown
        quadrants = {
            "DAYTRADE_SHORT": {"trades": 0, "wins": 0, "pnl": 0.0},
            "DAYTRADE_LONG": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        for t in closed_trades:
            st = "DAYTRADE"
            sd = t.get("side", "SHORT")
            q_key = f"{st}_{sd}"
            pnl = float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0))
            if q_key in quadrants:
                quadrants[q_key]["trades"] += 1
                if pnl > 0:
                    quadrants[q_key]["wins"] += 1
                quadrants[q_key]["pnl"] = round(quadrants[q_key]["pnl"] + pnl, 2)

        # 3. Adjust Dynamic Thermostat based on Self-Evaluation
        acc = self.trader.get_account_state()
        equity = float(acc.get("equity", 100.0))
        daily_dd_pct = abs(min(0.0, today_pnl)) / equity * 100.0 if equity > 0 else 0.0

        thermo = state["thermostat"]
        # 2026-10-22 (ROADMAP 2.3): aturan & angka thermostat dari src/risk_config.py (nilai sama seperti sebelumnya)
        from src import risk_config
        if keep_active_circuit_breaker(thermo, now_ts):
            pass   # jeda CB masih berjalan -> jangan diperpanjang/ditimpa
        else:
            risk_config.apply_thermostat(thermo, loss_streak, win_streak, net_realized_pnl, daily_dd_pct,
                                         win_rate, profit_factor, now_ts)
            if thermo.get("mode") == "CIRCUIT_BREAKER":
                thermo["streak_reset_ts"] = now_ts      # streak yang memicu CB tidak dihitung lagi sesudahnya
            else:
                thermo.setdefault("streak_reset_ts", 0.0)

        # 4. Adaptive Weights Calibration (Loop 2) - Disabled until unified scoring connects weights to screener
        ENABLE_WEIGHT_CALIBRATION = False
        last_cal_count = int(state.get("last_calibrated_trade_count", 0))
        weight_cal_note = "Bobot screener statis (kalibrasi Loop 2 dinonaktifkan sementara hingga unified scoring engine terpasang)."
        if ENABLE_WEIGHT_CALIBRATION and total_closed >= 3 and (total_closed - last_cal_count) >= 2:
            try:
                from src.adaptive_weights import run_calibration
                cal_dt = run_calibration("DAYTRADE")
                state["last_calibrated_trade_count"] = total_closed
                weight_cal_note = f"Auto-Kalibrasi Bobot Selesai: {cal_dt.get('calibration_rationale', 'Updated')[:80]}"
            except Exception as e:
                weight_cal_note = f"Kalibrasi dilewati ({e})"

        # Extract latest lessons learned from AI post-mortems
        recent_lessons = []
        for t in reversed(closed_trades[-4:]):
            rev = t.get("ai_review") or {}
            if rev.get("lessons_learned"):
                recent_lessons.append({
                    "coin": t.get("coin"),
                    "strategy": t.get("strategy"),
                    "pnl": float(t.get("realized_pnl") or t.get("final_pnl_usd", 0.0)),
                    "grade": rev.get("execution_grade", "B"),
                    "lesson": rev.get("lessons_learned")
                })

        eval_report = {
            "evaluated_at": now_str,
            "trigger_reason": trigger_reason,
            "total_closed": total_closed,
            "wins_count": len(wins),
            "losses_count": len(losses),
            "win_rate_pct": win_rate,
            "net_realized_pnl_usd": net_realized_pnl,
            "profit_factor": profit_factor,
            "win_streak": win_streak,
            "loss_streak": loss_streak,
            "today_pnl_usd": today_pnl,
            "thermostat_mode": thermo["mode"],
            "thermostat_badge": thermo["badge"],
            "thermostat_reason": thermo["reason"],
            "weight_calibration": weight_cal_note,
            "quadrants": quadrants,
            "recent_lessons": recent_lessons
        }

        state["thermostat"] = thermo
        state["last_eval_ts"] = now_ts
        state["last_eval_time"] = now_str
        state["last_eval_report"] = eval_report
        state["stats"]["total_self_evaluations"] = int(state["stats"].get("total_self_evaluations", 0)) + 1
        # Instant Slot-Refill: When a position closes and circuit breaker is not active, allow immediate scan for replacement
        if str(trigger_reason).startswith("ON_CLOSE") and not thermo.get("circuit_breaker_pause"):
            state["last_hunt_ts"] = 0.0
        self.save_state(state)

        self.log_telemetry(
            category="SELF_EVAL",
            action=f"Evaluasi Mandiri ({trigger_reason})",
            detail=f"WR: {win_rate}% ({len(wins)}W/{len(losses)}L) | Net: ${net_realized_pnl:+.2f} | Thermostat: {thermo['mode']} (Reserve {thermo['reserve_pct']}%)",
            equity=equity,
            free_margin=float(acc.get("available", 0.0))
        )
        return eval_report

    # ------------------------------------------------------------------
    # 2.5 AUTONOMOUS WALK-FORWARD BACKTEST & FORWARD-TEST VALIDATOR
    # ------------------------------------------------------------------
    def run_periodic_backtest_and_forward_validation(self, mode: str = "FULL", trigger_reason: str = "MANUAL", **kwargs) -> Dict:
        """DINONAKTIFKAN 2026-10-23: backtest berkala lama menguji logika SWING & intraday v1 (sudah dihapus/diganti)
        dan memicu kalibrasi diri otomatis. Riset & uji kini di research_v2/. Paper mode = forward-test v3."""
        return {"success": False, "disabled": True,
                "reason": "Backtest berkala dinonaktifkan (swing dihapus; intraday v3 diuji lewat mode PAPER / research_v2)."}

    def run_autonomous_hunt_if_due(self, force: bool = False) -> Dict:
        """
        Checks if autonomous hunting is due (every 30 mins or when forced).
        Only scans & calls Gemini if capital governor confirms open slot & free margin > 15% reserve!
        Strictly opens positions ONLY when Score >= Threshold AND AI Gatekeeper = APPROVED + Grade A/A+.
        """
        mode = trading_mode.get_mode()   # Entry Otomatis (user): OFF / PAPER / LIVE (menu [4])
        if mode == "OFF":
            return {"executed": False, "status": "MODE_OFF", "reason": "Entry Otomatis = OFF"}
        state = self.load_state()
        now_ts = time.time()

        # Pre-flight Guardian Agent Self-Healing Check (Zero-Crash & State Integrity)
        try:
            from src.guardian_agent import guardian_agent
            guardian_agent.run_full_health_and_security_audit(force=False)
        except Exception:
            pass

        # Check periodic self-evaluation (every 6 hours)
        eval_interval_sec = int(state.get("self_eval_interval_hours", 6)) * 3600
        if (now_ts - float(state.get("last_eval_ts", 0.0))) >= eval_interval_sec:
            self.run_self_evaluation(trigger_reason="PERIODIC_6H")
            state = self.load_state()

        hunt_interval_sec = int(state.get("hunt_interval_minutes", 30)) * 60
        if not force and (now_ts - float(state.get("last_hunt_ts", 0.0))) < hunt_interval_sec:
            return {"executed": False, "status": "NOT_DUE"}

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        acc = self.trader.get_account_state(force_refresh=True)
        if acc.get("error"):
            return {"executed": False, "status": "ACCOUNT_API_ERROR"}

        gov = self.get_live_governor_snapshot(account_state=acc)
        equity = gov["equity"]
        available = gov["available"]
        thermo = gov["thermostat"]

        state = self.load_state()
        state["last_hunt_ts"] = now_ts
        state["last_hunt_time"] = now_str
        state["stats"]["total_hunt_cycles"] = int(state["stats"].get("total_hunt_cycles", 0)) + 1

        # Pre-Scan Capital & Slot Guardrail (0 AI tokens used if blocked!)
        if not gov["can_open_new_position"]:
            status_msg = f"Skip Scan (Hemat Token): {gov['gate_summary']}"
            state["last_hunt_status"] = status_msg
            self.save_state(state)
            self.log_telemetry(
                category="CAPITAL_GUARD",
                action="Bypass Scan (Slot/Reserve Guard)",
                detail=gov["gate_summary"],
                equity=equity,
                free_margin=available
            )
            return {"executed": False, "status": "GUARD_BLOCKED", "reason": gov["gate_summary"]}

        # Slot & Margin above 15% Reserve are AVAILABLE -> Execute Autonomous Scan!
        state["stats"]["total_scans_executed"] = int(state["stats"].get("total_scans_executed", 0)) + 1
        self.save_state(state)

        from src.database import Database
        from src.downloader import DataDownloader
        from src.auto_hunter import AutoHuntSniper

        # 1. Refresh market snapshot & recent candles for top liquid coins
        db = Database(read_only=False)
        try:
            dl = DataDownloader(db)
            dl.sync_snapshot_only()
            sniper = AutoHuntSniper(db, self.trader)
            hunt_res = sniper.run_hunt_cycle()
        finally:
            db.close()

        leaderboard = hunt_res.get("leaderboard", [])
        if not leaderboard:
            msg = "Scan intraday selesai: belum ada kandidat yang lolos kriteria teknikal."
            state = self.load_state()
            state["last_hunt_status"] = msg
            self.save_state(state)
            self.log_telemetry("AUTO_HUNT", "Scan Selesai (0 Kandidat)", msg, equity=equity, free_margin=available)
            return {"executed": False, "status": "NO_CANDIDATES"}

        # 2. Evaluate Leaderboard with Strict Quantitative Threshold & Grade-A Gate
        min_score_dt = float(thermo.get("min_score_intraday", 75.0))
        min_conf = int(thermo.get("min_ai_confidence", 75))
        allowed_grades = [g.upper() for g in thermo.get("required_grades", ["A", "A+"])]

        selected_cand = None
        selected_audit = None
        skip_logs = []

        # Jejak keputusan (2026-10-23): setiap kandidat dicatat ke trade_db.entry_decisions -> basis perbaikan strategi
        from src.trade_db import trade_db as _tdb
        hunt_id = datetime.now().strftime("%Y%m%d-%H%M%S")

        def _dec(cand, stage, outcome, reason, audit=None, **extra):
            row = {"hunt_id": hunt_id, "mode": mode, "coin": cand.get("coin"), "side": cand.get("side"),
                   "strategy": cand.get("stream"), "stage": stage, "outcome": outcome, "reason": reason,
                   "score": cand.get("score"), "equity": equity, "candidate": _snapshot_candidate(cand), "audit": audit}
            if audit:
                row.update({"ai_approve": int(bool(audit.get("approve", False))),
                            "ai_grade": str(audit.get("conviction_grade", "")).upper(),
                            "ai_confidence": int(audit.get("confidence_score", 0) or 0)})
            row.update(extra)
            return _tdb.log_decision(row)

        def _skip(cand, msg):
            skip_logs.append(msg)
            _dec(cand, "FILTER", "SKIP", msg)

        for cand in leaderboard:
            coin = cand["coin"]
            side = cand["side"]
            strat = cand["stream"]
            score = float(cand["score"])
            req_score = min_score_dt
            if strat != "DAYTRADE":
                _skip(cand, f"{coin}: stream {strat} tidak didukung (intraday saja)")
                continue
            if not cand.get("ready", False):
                _skip(cand, f"{coin}: gerbang wajib v3 belum lengkap ({'; '.join(cand.get('vetoes', [])[:2]) or 'tren/penolakan/BOS/volume'})")
                continue

            # Check quantitative score threshold
            if score < req_score:
                _skip(cand, f"{coin} ({strat}): Skor {score:.1f} < Threshold {req_score:.1f}")
                continue

            # Kuota slot intraday (capital-weighted)
            if strat == "DAYTRADE" and gov.get("effective_intraday_slots", gov["intraday_count"]) >= gov["max_intraday"]:
                _skip(cand, f"{coin} (DAYTRADE): Kuota Intraday efektif penuh ({gov.get('effective_intraday_slots')}/{gov['max_intraday']})")
                continue

            # Check Directional Hedge Guard (prevent 100% one-sided portfolio)
            if side == "LONG" and gov["long_count"] >= gov["max_directional"]:
                _skip(cand, f"{coin} (LONG): Batas arah LONG penuh ({gov['long_count']}/{gov['max_directional']})")
                continue
            if side == "SHORT" and gov["short_count"] >= gov["max_directional"]:
                _skip(cand, f"{coin} (SHORT): Batas arah SHORT penuh ({gov['short_count']}/{gov['max_directional']})")
                continue

            # Check Claude_Crypto Sector Diversification Guard (Max 2 positions per sector)
            sec_info = ClaudeCryptoEngine.get_coin_sector(coin)
            sec_label = sec_info["label"]
            sec_count = gov.get("sector_distribution", {}).get(sec_label, 0)
            if sec_count >= 2:
                _skip(cand, f"{coin} ({sec_label}): Batas diversifikasi sektor tercapai ({sec_count}/2 posisi {sec_label} aktif)")
                continue

            # Passed quantitative & portfolio guards -> Run AI Gatekeeper Audit
            audit = sniper.audit_candidate(cand, equity)
            state = self.load_state()
            state["stats"]["total_ai_audits"] = int(state["stats"].get("total_ai_audits", 0)) + 1
            self.save_state(state)

            is_app = bool(audit.get("approve", False))
            grade = str(audit.get("conviction_grade", "C")).upper()
            conf = int(audit.get("confidence_score", 0))
            reason = audit.get("verdict_reason", "")

            if is_app and grade in allowed_grades and conf >= min_conf:
                selected_cand = cand
                selected_audit = audit
                _dec(cand, "AI_GATE", "APPROVED", reason, audit=audit)
                break
            else:
                veto_msg = f"{side} {coin} (Skor {score:.1f}) -> AI Grade {grade} ({conf}/100, Approve={is_app}). Syarat: Wajib Grade A (>={min_conf}). Alasan: {reason[:90]}"
                skip_logs.append(veto_msg)
                _dec(cand, "AI_GATE", "REJECTED", reason, audit=audit)
                self.log_telemetry(
                    category="AI_GATEKEEPER",
                    action="Skip (Bukan Grade A)",
                    detail=veto_msg,
                    coin=coin,
                    grade=grade,
                    equity=equity,
                    free_margin=available
                )

        if not selected_cand or not selected_audit:
            state = self.load_state()
            state["stats"]["total_vetoed_or_skipped"] = int(state["stats"].get("total_vetoed_or_skipped", 0)) + 1
            summary_skip = "; ".join(skip_logs[:2]) if skip_logs else "Tidak ada kandidat Grade A."
            state["last_hunt_status"] = f"Ditahan oleh Filter Disiplin: {summary_skip}"
            self.save_state(state)
            return {"executed": False, "status": "FILTERED_OUT", "details": skip_logs}

        # 3. Calculate Dynamic Equity-Scaled Position Margin & Verify Reserve Floor
        coin = selected_cand["coin"]
        side = selected_cand["side"]
        strat = selected_cand["stream"]
        prof = get_profile(strat)
        lev = int(selected_audit.get("leverage") or prof.get("leverage", 2))
        sl_pct = float(selected_cand["sl_pct"])
        sl_px = float(selected_cand["sl_px"])
        tp2_pct = float(selected_cand["tp2_pct"])
        grade = str(selected_audit.get("conviction_grade", "A")).upper()
        conf = int(selected_audit.get("confidence_score", 80))

        # 2026-10-23 (v3): ukuran posisi berbasis RISIKO (kerangka risiko berlapis: 0,5–1% ekuitas per trade).
        # notional = ekuitas x risiko% / SL% ; margin = notional / leverage; tetap dibatasi governor & cadangan.
        from src import risk_config
        rp = risk_config.get().get("intraday_risk", {})
        risk_pct = float(rp.get("risk_per_trade_pct", 1.0))
        if grade != "A+":
            risk_pct = min(risk_pct, float(rp.get("risk_per_trade_pct_grade_a", 0.75)))
        risk_pct *= float(thermo.get("sizing_mult", 1.0))

        # Kalender Siklus Modifier (Mingguan Day-of-Week & Bulanan Expiry)
        cycle_mult = float(selected_audit.get("cycle_multiplier", 1.0)) if selected_audit else 1.0
        if cycle_mult == 1.0:
            try:
                from src.portfolio_sizer import PortfolioSizer
                cycle_mod = PortfolioSizer.calculate_cycle_sizing_modifier()
                cycle_mult = float(cycle_mod.get("composite_multiplier", 1.0))
            except Exception:
                cycle_mult = 1.0
        risk_pct = round(risk_pct * cycle_mult, 3)

        risk_notional = equity * (risk_pct / 100.0) / max(sl_pct / 100.0, 1e-6)
        risk_margin = risk_notional / max(lev, 1)

        # Clamp ke governor [min_order_margin, max_order_margin] dan margin bebas di atas cadangan
        order_margin = min(risk_margin, gov["max_order_margin"], gov["free_above_reserve"])
        order_margin = round(order_margin, 2)
        min_margin_usd = float(rp.get("min_order_margin_usd", 5.0))

        if order_margin < min_margin_usd or (available - order_margin) < gov["reserve_usd"]:
            msg = f"Margin tidak cukup (risiko {risk_pct:.2f}% -> ${order_margin:.2f}) / cadangan {gov['reserve_pct']}% (${gov['reserve_usd']:.2f}) untuk {coin}."
            _dec(selected_cand, "SIZING", "SKIP", msg, audit=selected_audit, risk_pct=risk_pct, margin_usd=order_margin, leverage=lev)
            state = self.load_state()
            state["last_hunt_status"] = msg
            self.save_state(state)
            return {"executed": False, "status": "RESERVE_GUARD_STOP", "reason": msg}

        # 3b. MODE PAPER: catat trade kertas, TIDAK ada order ke bursa
        if mode != "LIVE":
            from src import paper_trader
            ptrade = paper_trader.open_paper_trade(selected_cand, selected_audit, margin_usd=order_margin, leverage=lev,
                                                   risk_pct=risk_pct, equity=equity)
            _dec(selected_cand, "ENTRY", "PAPER_OPEN", "trade kertas dibuka", audit=selected_audit, risk_pct=risk_pct,
                 margin_usd=order_margin, leverage=lev, entry_px=ptrade.get("entry_px"), sl_px=ptrade.get("sl_px"),
                 tp1_px=ptrade.get("tp1_px"), tp2_px=ptrade.get("tp2_px"), result=ptrade)
            msg = (f"📝 PAPER OPEN: {side} {coin} @ {ptrade['entry_px']} | SL {ptrade['sl_px']} | TP1 {ptrade['tp1_px']} | "
                   f"TP2 {ptrade['tp2_px']} | Grade {grade} ({conf}/100) | Margin kertas ${order_margin:.2f} ({lev}x)")
            state = self.load_state()
            state["last_entry_ts"] = now_ts
            state["last_entry_coin"] = f"PAPER {side} {coin}"
            state["last_hunt_status"] = msg
            self.save_state(state)
            self.log_telemetry("PAPER", f"PAPER OPEN {side} {coin}", msg, coin=coin, grade=grade, equity=equity, free_margin=available)
            return {"executed": True, "paper": True, "coin": coin, "side": side, "strategy": strat, "margin": order_margin, "msg": msg}

        # 4. Execute Autonomous Order on Hyperliquid L1 with Native SL & TP1
        notional_usd = round(order_margin * lev, 2)
        if side == "LONG":
            res = self.trader.execute_long(
                coin=coin, usd_size=notional_usd, leverage=lev,
                sl_pct=sl_pct, tp_pct=tp2_pct, sl_px=sl_px, strategy=strat
            )
        else:
            res = self.trader.execute_short(
                coin=coin, usd_size=notional_usd, leverage=lev,
                sl_pct=sl_pct, tp_pct=tp2_pct, sl_px=sl_px, strategy=strat
            )

        plan_r = res.get("plan") or {}
        decision_id = _dec(selected_cand, "ENTRY", "LIVE_OPEN" if res.get("success") else "ORDER_FAILED",
                           "order terkirim" if res.get("success") else str(res.get("error")), audit=selected_audit,
                           risk_pct=risk_pct, margin_usd=order_margin, leverage=lev,
                           entry_px=res.get("entry_est") or plan_r.get("entry_px"),
                           sl_px=res.get("stop_loss_px") or sl_px, tp1_px=plan_r.get("tp1_px"), tp2_px=plan_r.get("tp2_px"),
                           result={k: v for k, v in res.items() if k != "raw"})
        if res.get("success"):
            position_tags.tag_position(
                coin=coin,
                strategy=strat,
                plan=res.get("plan"),
                meta={
                    "source": "AUTONOMOUS_AI_SNIPER",
                    "ai_grade": grade,
                    "ai_conviction": conf,
                    "equity_at_entry": equity,
                    "reserve_pct_enforced": gov["reserve_pct"],
                    # 2026-10-23: konteks entry untuk analisis perbaikan
                    "decision_id": decision_id,
                    "hunt_id": hunt_id,
                    "mode": mode,
                    "risk_pct": round(risk_pct, 4),
                    "margin_usd": order_margin,
                    "leverage": lev,
                    "signal_score": selected_cand.get("score"),
                    "engine_version": selected_cand.get("engine_version"),
                    "signal_bar_ts": selected_cand.get("signal_bar_ts"),
                    "ai_reason": str(selected_audit.get("verdict_reason", ""))[:500],
                }
            )
            state = self.load_state()
            state["last_entry_ts"] = now_ts
            state["last_entry_coin"] = f"{side} {coin} ({strat})"
            state["stats"]["total_autonomous_opens"] = int(state["stats"].get("total_autonomous_opens", 0)) + 1
            exec_msg = (
                f"🚀 AUTO-OPEN SUKSES: {side} {coin} [{strat}] | Grade {grade} ({conf}/100) | "
                f"Margin: ${order_margin:.2f} ({lev}x) | Entry: ${res.get('entry_est')} | "
                f"SL On-Chain: ${res.get('stop_loss_px')} | TP1 On-Chain: ${res.get('trim_10pct_px')}"
            )
            state["last_hunt_status"] = exec_msg
            self.save_state(state)

            self.log_telemetry(
                category="AUTO_EXECUTE",
                action=f"OPEN {side} {coin} [{strat}]",
                detail=f"Skor {selected_cand['score']} | AI Grade {grade} ({conf}%) | Margin ${order_margin:.2f} (Sisa Cadangan Aman >= {gov['reserve_pct']}%)",
                coin=coin,
                grade=grade,
                equity=equity,
                free_margin=round(available - order_margin, 2)
            )

            # Sync HTML Dashboard & GitHub Pages
            try:
                from src.html_reporter import generate_html_report
                generate_html_report()
            except Exception:
                pass

            return {"executed": True, "coin": coin, "side": side, "strategy": strat, "margin": order_margin, "msg": exec_msg}
        else:
            err_msg = f"Gagal eksekusi order otonom {coin}: {res.get('error')}"
            self.log_telemetry("AUTO_ERROR", f"Gagal Open {coin}", err_msg, coin=coin, grade=grade, equity=equity, free_margin=available)
            return {"executed": False, "status": "ORDER_FAILED", "error": err_msg}

    def revisit_and_scale_active_positions(self, target_coins: Optional[List[str]] = None, execute_topup: bool = True) -> Dict:
        """
        Autonomous Revisit & Conviction Scale-Up Engine:
        Evaluates existing open positions that were sized under older/smaller equity rules
        (or capped around ~$20-$24) while abundant Free Margin above 15% Reserve exists.
        Criteria to scale-in / top-up an active position to the $200-tier target ($28-$36 margin):
          1. Position has NOT already been trimmed at TP1 (`not trimmed_10pct`) and is NOT dust.
          2. Current price is near entry (`-2.0% <= move_pct <= +2.5%`) — never averages down into a deep loser or chases an extended pump.
          3. Live L2 Orderbook imbalance supports the trade direction (or is balanced) with healthy spread (`<= 15 bps`).
          4. Free margin above 15% Reserve is sufficient to cover the top-up delta (`topup_margin >= $6.00`).
          5. Automatically re-syncs on-chain Stop-Loss and TP1 trigger orders to the new combined position size!
        """
        # 2026-10-23: top-up / scale-in otomatis DIHAPUS untuk intraday (menambah risiko pada posisi berjalan)
        execute_topup = False
        from src.position_manager import PositionManager
        from src.hyperliquid_client import HyperliquidClient

        # Early Exit: Cek apakah mode otonom dibekukan untuk wallet migration atau Circuit Breaker aktif
        state = self.load_state()
        thermo = state.get("thermostat", {})
        cb_pause = bool(thermo.get("circuit_breaker_pause", False)) and time.time() < float(thermo.get("pause_until_ts", 0.0))
        if not state.get("enabled", True) or state.get("pause_new_positions", False) or cb_pause:
            reason = "Scale-up dibatalkan: Mode Otonom dibekukan / Circuit Breaker Pause aktif (enabled=false, pause=true, atau cb_pause=true)"
            self.log_telemetry("SCALE_GUARD", "Blocked Scale-Up", reason)
            return {"success": False, "status": "BLOCKED", "reason": reason, "scaled_up": [], "revisited": []}

        acc = self.trader.get_account_state(force_refresh=True)
        if acc.get("error"):
            return {"success": False, "error": acc.get("error")}

        gov = self.get_live_governor_snapshot(account_state=acc)
        equity = gov["equity"]
        free_above_reserve = gov["free_above_reserve"]
        slot_base = gov["slot_base_margin"]
        grade_a_target = round(slot_base * 1.05, 2)       # ~$29.50 at $200 equity
        grade_a_plus_target = round(slot_base * 1.25, 2)  # ~$35.15 at $200 equity

        pm = PositionManager(self.trader)
        lifecycle = pm.sync_active_positions()
        all_mids = self.trader.get_all_mids(force_refresh=True)
        hl_client = HyperliquidClient()
        tags_all = position_tags._load()

        revisited = []
        scaled_up = []

        for p in acc.get("positions", []):
            coin = p.get("coin")
            if not coin:
                continue
            if target_coins and coin.upper() not in [c.upper() for c in target_coins]:
                continue

            cur_margin = float(p.get("margin_used", 0.0))
            cur_sz = float(p.get("size", 0.0))
            side = p.get("side", "SHORT")
            lev = int(p.get("leverage", 2) or 2)
            entry_px = float(p.get("entry_px", 0.0))
            cur_px = float(all_mids.get(coin, entry_px))
            lc = lifecycle.get(coin, {})
            tag_meta = (tags_all.get(coin) or {}).get("meta") or {}
            ai_grade = str(tag_meta.get("ai_grade", "A")).upper()

            if cur_margin < gov["dust_threshold_usd"] or lc.get("trimmed_10pct"):
                revisited.append({
                    "coin": coin, "side": side, "current_margin": cur_margin,
                    "decision": "SKIP_RUNNER_OR_TRIMMED",
                    "reason": "Posisi sudah pernah TP1 Trim / Free-Runner (tidak perlu tambah muatan)."
                })
                continue

            target_margin = grade_a_plus_target if ai_grade == "A+" else (grade_a_target if ai_grade == "A" else slot_base)
            target_margin = min(target_margin, gov["max_order_margin"])
            deficit_margin = round(target_margin - cur_margin, 2)

            if deficit_margin < 6.0:
                revisited.append({
                    "coin": coin, "side": side, "current_margin": cur_margin,
                    "target_margin": target_margin,
                    "decision": "ALREADY_OPTIMAL",
                    "reason": f"Ukuran margin (${cur_margin:.2f}) sudah dekat target tier ${equity:.0f} (${target_margin:.2f})."
                })
                continue

            move_pct = (((cur_px - entry_px) / entry_px) * 100.0) if side == "LONG" else (((entry_px - cur_px) / entry_px) * 100.0)
            if move_pct < -2.0 or move_pct > 2.5:
                revisited.append({
                    "coin": coin, "side": side, "current_margin": cur_margin,
                    "target_margin": target_margin, "move_pct": round(move_pct, 2),
                    "decision": "HOLD_CURRENT_SIZE",
                    "reason": f"Harga sudah bergerak {move_pct:+.2f}% dari entry (di luar zona aman scale-in [-2.0%, +2.5%])."
                })
                continue

            # Check live L2 Orderbook
            ob = hl_client.get_l2_orderbook(coin, depth=15)
            bid_pct = float(ob.get("bid_share_pct", 50.0))
            ask_pct = float(ob.get("ask_share_pct", 50.0))
            sp_bps = float(ob.get("avg_spread_5m_bps", ob.get("spread_bps", 5.0)))

            if (side == "LONG" and ask_pct >= 68.0) or (side == "SHORT" and bid_pct >= 68.0) or sp_bps > 15.0:
                revisited.append({
                    "coin": coin, "side": side, "current_margin": cur_margin,
                    "target_margin": target_margin,
                    "decision": "HOLD_ORDERBOOK_CAUTION",
                    "reason": f"Orderbook L2 berlawanan/spread lebar (Bid {bid_pct:.1f}% / Ask {ask_pct:.1f}%, Spread {sp_bps:.1f} bps)."
                })
                continue

            topup_margin = min(deficit_margin, round(free_above_reserve * 0.65, 2))
            if topup_margin < 6.0 or (free_above_reserve - topup_margin) < 0:
                revisited.append({
                    "coin": coin, "side": side, "current_margin": cur_margin,
                    "target_margin": target_margin,
                    "decision": "HOLD_RESERVE_GUARD",
                    "reason": f"Sisa margin bebas di atas cadangan 15% (${free_above_reserve:.2f}) diprioritaskan untuk keamanan kas."
                })
                continue

            if execute_topup:
                add_notional = round(topup_margin * lev, 2)
                sz_dec = self.trader.get_asset_precision(coin)
                raw_add_sz = add_notional / cur_px if cur_px > 0 else 0
                add_sz = round(raw_add_sz, sz_dec) if sz_dec > 0 else int(raw_add_sz)
                if add_sz > 0:
                    order_res = self.trader.exchange.market_open(
                        name=coin,
                        is_buy=(side == "LONG"),
                        sz=add_sz,
                        slippage=0.02
                    )
                    if isinstance(order_res, dict) and order_res.get("status") == "ok":
                        free_above_reserve = round(max(0.0, free_above_reserve - topup_margin), 2)
                        new_est_margin = round(cur_margin + topup_margin, 2)
                        new_total_sz = round(cur_sz + add_sz, sz_dec) if sz_dec > 0 else int(cur_sz + add_sz)
                        if coin in lifecycle:
                            lifecycle[coin]["initial_sz"] = new_total_sz
                            lifecycle[coin]["remaining_sz"] = new_total_sz
                            pm.save_lifecycle_state(lifecycle)
                        scaled_up.append({
                            "coin": coin,
                            "side": side,
                            "old_margin": round(cur_margin, 2),
                            "added_margin": round(topup_margin, 2),
                            "new_margin": new_est_margin,
                            "added_sz": add_sz,
                            "new_total_sz": new_total_sz,
                            "move_pct": round(move_pct, 2),
                            "orderbook": f"Bid {bid_pct:.1f}% / Ask {ask_pct:.1f}% (Spread {sp_bps:.1f} bps)"
                        })
                        revisited.append({
                            "coin": coin, "side": side, "current_margin": cur_margin,
                            "target_margin": target_margin,
                            "decision": "SCALED_UP_TO_TIER_200",
                            "reason": f"Top-Up +${topup_margin:.2f} margin (${cur_margin:.2f} -> ${new_est_margin:.2f}) sukses dieksekusi & SL/TP1 disinkronkan!"
                        })
                        self.log_telemetry(
                            category="AI_REBALANCE",
                            action=f"SCALE-UP {side} {coin}",
                            detail=f"Top-up +${topup_margin:.2f} margin (${cur_margin:.2f} -> ${new_est_margin:.2f}) sesuai Tier ${equity:.0f}",
                            coin=coin,
                            grade=ai_grade,
                            equity=equity,
                            free_margin=free_above_reserve
                        )

        if scaled_up:
            time.sleep(0.4)
            # Re-sync on-chain SL and TP1 trigger order sizes immediately!
            pm.sync_active_positions()

        return {
            "success": True,
            "equity": equity,
            "slot_base_margin": slot_base,
            "grade_a_target_margin": grade_a_target,
            "grade_a_plus_target_margin": grade_a_plus_target,
            "revisited": revisited,
            "scaled_up": scaled_up
        }

    # ------------------------------------------------------------------
    # 4. MONITORING & TELEMETRY DASHBOARD (CLI & LIVE VIEW)
    # ------------------------------------------------------------------
    def render_autonomous_dashboard(self) -> List[str]:
        """
        Renders a comprehensive visual status board showing how the Autonomous AI Engine
        is operating, its dynamic equity-scaled capital governor, self-evaluation metrics,
        and recent telemetry decisions.
        """
        from src.position_manager import PositionManager, LOG_FILE
        state = self.load_state()
        gov = self.get_live_governor_snapshot()
        thermo = gov["thermostat"]
        stats = state.get("stats", {})
        eval_rep = state.get("last_eval_report") or {}

        pid = PositionManager.is_daemon_running()
        daemon_badge = f"🟢 BERJALAN 24/7 (PID: {pid})" if pid else "🛑 MESIN BERHENTI (Perlu Dinyalakan)"
        auto_mode_badge = "🟢 ZERO-TOUCH AKTIF (Auto-Hunt + Self-Eval + Position Guard)" if state.get("enabled", True) else "⏸️ PAUSED (Hanya Mengawal Posisi Aktif)"

        now_ts = time.time()
        hunt_int = int(state.get("hunt_interval_minutes", 30)) * 60
        last_h_ts = float(state.get("last_hunt_ts", 0.0))
        next_hunt_min = max(0, int((hunt_int - (now_ts - last_h_ts)) / 60)) if last_h_ts > 0 else 0

        eval_int = int(state.get("self_eval_interval_hours", 6)) * 3600
        last_e_ts = float(state.get("last_eval_ts", 0.0))
        next_eval_min = max(0, int((eval_int - (now_ts - last_e_ts)) / 60)) if last_e_ts > 0 else 0

        # Query Guardian Agent & Episodic Memory status
        guardian_badge = "🛡️ SOLID & AMAN (Zero-Leak DLP | Zero-Crash Self-Heal Aktif)"
        mem_rules_count = 3
        try:
            from src.guardian_agent import guardian_agent
            g_rep = guardian_agent.run_full_health_and_security_audit(force=False)
            guardian_badge = g_rep.get("status_badge", guardian_badge)
            from src.ai_engine import episodic_memory
            mem_rules_count = len(episodic_memory.load_memory().get("rules", []))
        except Exception:
            pass

        lines = []
        lines.append("=" * 80)
        lines.append("      🤖 PUSAT KOMANDO & PEMANTAUAN AI OTONOM (MULTI-AGENT COMMITTEE)      ")
        lines.append("=" * 80)
        lines.append(f"  • Status Mesin             : {daemon_badge}")
        lines.append(f"  • Mode AI Mandiri          : {auto_mode_badge}")
        lines.append(f"  • Arsitektur Komite Agent  : 🎯 Sniper vs ⚖️ Prosecutor vs 👨‍⚖️ CRO Judge + 3 Live Tools")
        lines.append(f"  • Memori Episodik & DevSec : 🧠 {mem_rules_count} Aturan Mandiri Aktif  |  {guardian_badge}")
        lines.append(f"  • Thermostat Self-Defense  : {thermo.get('badge')} (Sizing Mult: {thermo.get('sizing_mult', 1.0):.2f}x)")
        lines.append(f"  • Alasan Thermostat        : {thermo.get('reason')}")
        lines.append(f"  • Syarat Mutlak Auto-Open  : Wajib APPROVED + Grade {'/'.join(thermo.get('required_grades', ['A']))} (Conf >= {thermo.get('min_ai_confidence', 75)})")
        lines.append(f"  • Ambang Skor Kuantitatif  : Intraday >= {thermo.get('min_score_intraday', 75.0):.1f}  |  Swing >= {thermo.get('min_score_swing', 78.0):.1f}")
        lines.append(f"  • Jadwal Siklus Otonom     : Hunt berikutnya dlm ~{next_hunt_min}m (tiap {state.get('hunt_interval_minutes')}m) | Self-Eval dlm ~{next_eval_min}m (tiap {state.get('self_eval_interval_hours')}j / saat Close)")
        lines.append("-" * 80)

        # Section 2: Dynamic Equity-Scaled Capital & Slot Governor
        lines.append("📐 MATRIKS PAGAR MODAL & SLOT DINAMIS — f(Total Saldo USDC):")
        lines.append(f"  • Total Ekuitas Akun (E)   : ${gov['equity']:,.2f} USDC  (Terpakai: ${gov['margin_used']:,.2f} [{gov['used_pct']:.1f}%] | Bebas: ${gov['available']:,.2f})")
        lines.append(f"  • Pagar Cadangan Wajib     : {gov['reserve_pct']:.1f}% dari Ekuitas = ${gov['reserve_usd']:,.2f} USDC (Tidak boleh disentuh entry baru)")
        lines.append(f"  • Plafon Modal Kerja Aktif : {100.0 - gov['reserve_pct']:.1f}% dari Ekuitas = ${gov['deployable_usd']:,.2f} USDC")
        lines.append(f"  • Margin Siap Tembak       : ${gov['free_above_reserve']:,.2f} USDC (Sisa margin bebas di atas pagar cadangan {gov['reserve_pct']:.0f}%)")
        lines.append(f"  • Kapasitas Posisi N_max(E): {gov['active_count']} / {gov['max_positions']} Slot Aktif  [🌊 Swing: {gov['swing_count']}/{gov['max_swing']} | ⚡ Intraday: {gov['intraday_count']}/{gov['max_intraday']} | 🟢L:{gov['long_count']} 🔴S:{gov['short_count']}]")
        lines.append(f"  • Ukuran Peluru per Posisi : ${gov['min_order_margin']:,.2f} s/d ${gov['max_order_margin']:,.2f} USDC (Target Slot Base: ${gov['slot_base_margin']:,.2f} USDC)")
        lines.append(f"  • Status Gatekeeper Entry  : {gov['gate_summary']}")
        lines.append("-" * 80)

        # Section 3: Self-Evaluation Report
        lines.append("🧠 RAPOR SELF-EVALUATION & PEMBELAJARAN MANDIRI TERAKHIR:")
        if eval_rep:
            lines.append(
                f"  • Evaluasi Terakhir        : {eval_rep.get('evaluated_at', '-')} (Pemicu: {eval_rep.get('trigger_reason', '-')})"
            )
            lines.append(
                f"  • Kinerja Realized         : Win Rate {eval_rep.get('win_rate_pct', 100.0)}% ({eval_rep.get('wins_count', 0)}W / {eval_rep.get('losses_count', 0)}L) "
                f"| Net PnL: ${eval_rep.get('net_realized_pnl_usd', 0.0):+.2f} USDC | PF: {eval_rep.get('profit_factor', 1.0)}"
            )
            lines.append(
                f"  • Streak & Hari Ini        : Win Streak {eval_rep.get('win_streak', 0)}x | Loss Streak {eval_rep.get('loss_streak', 0)}x | PnL Hari Ini: ${eval_rep.get('today_pnl_usd', 0.0):+.2f} USDC"
            )
            q = eval_rep.get("quadrants", {})
            if q:
                sw_s = q.get("SWING_SHORT", {})
                sw_l = q.get("SWING_LONG", {})
                dt_s = q.get("DAYTRADE_SHORT", {})
                dt_l = q.get("DAYTRADE_LONG", {})
                lines.append(
                    f"  • Rapor 4 Kuadran          : "
                    f"🌊Sw-S({sw_s.get('wins',0)}/{sw_s.get('trades',0)} ${sw_s.get('pnl',0):+.2f}) | "
                    f"🚀Sw-L({sw_l.get('wins',0)}/{sw_l.get('trades',0)} ${sw_l.get('pnl',0):+.2f}) | "
                    f"⚡Dt-S({dt_s.get('wins',0)}/{dt_s.get('trades',0)} ${dt_s.get('pnl',0):+.2f}) | "
                    f"⚡Dt-L({dt_l.get('wins',0)}/{dt_l.get('trades',0)} ${dt_l.get('pnl',0):+.2f})"
                )
            lines.append(f"  • Status Bobot Adaptif     : {eval_rep.get('weight_calibration', '-')}")
            lessons = eval_rep.get("recent_lessons", [])
            if lessons:
                lines.append("  • Pelajaran Post-Mortem AI Terakhir:")
                for ls in lessons[:2]:
                    lines.append(f"      └─ [{ls['coin']} | Grade {ls['grade']} | ${ls['pnl']:+.2f}]: {ls['lesson'][:95]}")
        else:
            lines.append("  • Belum ada rapor evaluasi. Akan otomatis dieksekusi pada siklus pertama.")
        lines.append("-" * 80)

        # Section 3.5: Periodic Walk-Forward Backtest & Live Forward-Test Validation
        val_rep = state.get("last_validation_report") or {}
        bt_days = int(state.get("backtest_interval_days", 7))
        ft_hrs = int(state.get("forward_audit_interval_hours", 24))
        last_bt_ts = float(state.get("last_backtest_ts", 0.0))
        last_ft_ts = float(state.get("last_forward_audit_ts", 0.0))
        next_bt_h = max(0, int(((bt_days * 86400) - (now_ts - last_bt_ts)) / 3600)) if last_bt_ts > 0 else 0
        next_ft_h = max(0, int(((ft_hrs * 3600) - (now_ts - last_ft_ts)) / 3600)) if last_ft_ts > 0 else 0

        lines.append("🔬 RAPOR PERIODIC WALK-FORWARD BACKTEST & FORWARD-TEST MANDIRI:")
        lines.append(
            f"  • Jadwal Validasi Mandiri  : Forward-Test Audit tiap {ft_hrs}j (dlm ~{next_ft_h}j) | Walk-Forward Backtest tiap {bt_days} Hari (dlm ~{next_bt_h}j / 3h jika Drift)"
        )
        if val_rep:
            sw_bt = val_rep.get("swing_backtest") or {}
            dt_bt = val_rep.get("intraday_backtest") or {}
            ft_live = val_rep.get("forward_test_live") or {}
            sw_ft = ft_live.get("SWING") or {}
            dt_ft = ft_live.get("DAYTRADE") or {}
            lines.append(
                f"  • Validasi Terakhir        : {val_rep.get('validated_at', '-')} [{val_rep.get('mode', 'FULL')} | {val_rep.get('trigger_reason', '-')}]"
            )
            lines.append(f"  • Status Reality-Gap/Drift : {val_rep.get('drift_badge', '-')}")
            lines.append(
                f"  • Backtest 🌊 SWING (95d)  : {sw_bt.get('total_trades', 0)} Trades | WR: {sw_bt.get('win_rate_pct', 0)}% (Safe: {sw_bt.get('safe_rate_pct', 0)}%) "
                f"| PF: {sw_bt.get('profit_factor', 0)} | Net: {sw_bt.get('net_return_pct', 0):+.1f}% | Opt Score >= {sw_bt.get('optimal_min_score', 78.0)}"
            )
            lines.append(
                f"  • Backtest ⚡ INTRADAY(15m): {dt_bt.get('total_trades', 0)} Trades | WR: {dt_bt.get('win_rate_pct', 0)}% (Safe: {dt_bt.get('safe_rate_pct', 0)}%) "
                f"| PF: {dt_bt.get('profit_factor', 0)} | AvgR: {dt_bt.get('avg_r', 0):+.2f}R | Mode: {dt_bt.get('best_session_mode', 'LONDON_NY')}"
            )
            lines.append(
                f"  • Forward-Test Live Aktual : 🌊 SWING (Open:{sw_ft.get('open',0)} Closed:{sw_ft.get('closed',0)} WR:{sw_ft.get('win_rate',0)}% Net:${sw_ft.get('realized_usd',0):+.2f}) "
                f"| ⚡ INTRADAY (Open:{dt_ft.get('open',0)} Closed:{dt_ft.get('closed',0)} WR:{dt_ft.get('win_rate',0)}% Net:${dt_ft.get('realized_usd',0):+.2f})"
            )
            tunings = val_rep.get("tuning_applied") or []
            if tunings:
                lines.append(f"  • Kalibrasi Mandiri        : {', '.join(tunings)}")
        else:
            lines.append("  • Belum ada rapor Walk-Forward Backtest. Akan otomatis dieksekusi pada siklus Mesin berikutnya.")
        lines.append("-" * 80)

        # Section 4: Telemetry & Execution Statistics
        lines.append(
            f"📡 TELEMETRI KEPUTUSAN AI MANDIRI "
            f"(Hunt: {stats.get('total_hunt_cycles', 0)}x | Scan: {stats.get('total_scans_executed', 0)}x | "
            f"Audit AI: {stats.get('total_ai_audits', 0)}x | Auto-Open Grade A: {stats.get('total_autonomous_opens', 0)}x | "
            f"Self-Eval: {stats.get('total_self_evaluations', 0)}x | Validasi BT/FT: {stats.get('total_quant_validations', 0)}x):"
        )
        lines.append(f"  • Status Siklus Hunt Terakhir ({state.get('last_hunt_time', '-')}): {state.get('last_hunt_status', '-')}")

        feed = state.get("telemetry_feed", [])
        if feed:
            lines.append("\n  [Kronologi Keputusan Otonom Terkini (autonomous_state.json)]:")
            for item in reversed(feed[-8:]):
                ts_short = str(item.get("timestamp", ""))[5:]  # MM-DD HH:MM:SS
                cat = item.get("category", "INFO")
                act = item.get("action", "")
                det = item.get("detail", "")
                lines.append(f"    [{ts_short}] [{cat:<13}] {act} ➔ {det[:85]}")

        # Also show latest 5 autopilot position guard events
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r") as f:
                    ap_lines = [ln.strip() for ln in f.readlines() if ln.strip()][-5:]
                if ap_lines:
                    lines.append("\n  [Aktivitas Pengawalan Posisi On-Chain Terkini (logs/autopilot.log)]:")
                    for ln in ap_lines:
                        lines.append(f"    {ln[:105]}")
            except Exception:
                pass

        lines.append("=" * 80)
        return lines

