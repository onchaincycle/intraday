"""
Portfolio Sizer & Allocation Master for Gemini ShortHunter v2.0

Menyediakan kalkulasi terpadu Core-Satellite Portfolio Allocation:
- Spot Vault (Scout / Sinyal Bot): 20% (Akumulasi riil skala besar di Cold Wallet)
- Swing 3-Day (Reversal / Dump 2x): 50%
- Intraday Day-Trade (Precision 2x-3x): 20%
- Free Margin Buffer (Liquid Cash Hyperliquid): 10%
"""

import os
import json
from typing import Dict, Any, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT_DIR, "data", "portfolio_sizing_config.json")

DEFAULT_CONFIG = {
    "spot_pct": 20.0,
    "swing_pct": 50.0,
    "intraday_pct": 20.0,
    "buffer_pct": 10.0,
    "num_spot_coins": 5,
    "swing_slots": 5,
    "intraday_slots": 4,
    "swing_risk_per_trade_pct": 2.5,
    "intraday_risk_per_trade_pct": 1.5,
    "swing_leverage": 2,
    "intraday_leverage": 2,
}


class PortfolioSizer:
    @staticmethod
    def get_config() -> Dict[str, Any]:
        """Mengambil konfigurasi sizing portofolio tersimpan atau fallback ke default."""
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    cfg = DEFAULT_CONFIG.copy()
                    cfg.update(data)
                    return cfg
            except Exception:
                pass
        return DEFAULT_CONFIG.copy()

    @staticmethod
    def save_config(config: Dict[str, Any]) -> bool:
        """Menyimpan konfigurasi sizing portofolio ke file data/portfolio_sizing_config.json."""
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            return True
        except Exception as e:
            print(f"[!] Gagal menyimpan portfolio_sizing_config.json: {e}")
            return False

    @classmethod
    def calculate_allocation(cls, total_equity: float, custom_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Menghitung breakdown alokasi modal dan sizing posisi per trade
        berdasarkan total ekuitas akun dan konfigurasi portofolio.
        """
        cfg = custom_config or cls.get_config()
        equity = max(0.0, float(total_equity))

        spot_pct = float(cfg.get("spot_pct", 55.0))
        swing_pct = float(cfg.get("swing_pct", 25.0))
        intraday_pct = float(cfg.get("intraday_pct", 10.0))
        buffer_pct = float(cfg.get("buffer_pct", 10.0))

        # Normalisasi persentase jika total != 100%
        total_pct = spot_pct + swing_pct + intraday_pct + buffer_pct
        if abs(total_pct - 100.0) > 0.01 and total_pct > 0:
            spot_pct = (spot_pct / total_pct) * 100.0
            swing_pct = (swing_pct / total_pct) * 100.0
            intraday_pct = (intraday_pct / total_pct) * 100.0
            buffer_pct = (buffer_pct / total_pct) * 100.0

        # 1. Spot Vault Allocation
        spot_budget = round(equity * (spot_pct / 100.0), 2)
        num_spot = max(1, int(cfg.get("num_spot_coins", 5)))
        budget_per_spot_coin = round(spot_budget / num_spot, 2) if num_spot > 0 else 0.0
        t1_scout = round(budget_per_spot_coin * 0.30, 2)
        t2_pullback = round(budget_per_spot_coin * 0.40, 2)
        t3_breakout = round(budget_per_spot_coin * 0.30, 2)

        # 2. Swing Allocation
        swing_budget = round(equity * (swing_pct / 100.0), 2)
        swing_slots = max(1, int(cfg.get("swing_slots", 5)))
        swing_margin_per_slot = round(swing_budget / swing_slots, 2)
        swing_lev = int(cfg.get("swing_leverage", 2))
        swing_notional_per_slot = round(swing_margin_per_slot * swing_lev, 2)
        swing_risk_pct = float(cfg.get("swing_risk_per_trade_pct", 2.0))
        swing_risk_usd = round(equity * (swing_risk_pct / 100.0), 2)

        # 3. Intraday Allocation
        intra_budget = round(equity * (intraday_pct / 100.0), 2)
        intra_slots = max(1, int(cfg.get("intraday_slots", 3)))
        intra_margin_per_slot = round(intra_budget / intra_slots, 2)
        intra_lev = int(cfg.get("intraday_leverage", 2))
        intra_notional_per_slot = round(intra_margin_per_slot * intra_lev, 2)
        intra_risk_pct = float(cfg.get("intraday_risk_per_trade_pct", 1.0))
        intra_risk_usd = round(equity * (intra_risk_pct / 100.0), 2)

        # 4. Buffer
        buffer_budget = round(equity * (buffer_pct / 100.0), 2)

        return {
            "total_equity": equity,
            "spot": {
                "pct": round(spot_pct, 1),
                "budget_usd": spot_budget,
                "num_coins": num_spot,
                "budget_per_coin": budget_per_spot_coin,
                "tranche_1_scout_usd": t1_scout,
                "tranche_2_pullback_usd": t2_pullback,
                "tranche_3_breakout_usd": t3_breakout,
                "strategy": "Spot Murni (Zero Leverage, Hold 1-24 Bulan, Wyckoff + Nansen)",
            },
            "swing": {
                "pct": round(swing_pct, 1),
                "budget_usd": swing_budget,
                "max_slots": swing_slots,
                "margin_per_slot": swing_margin_per_slot,
                "leverage": swing_lev,
                "notional_per_slot": swing_notional_per_slot,
                "risk_per_trade_pct": swing_risk_pct,
                "risk_usd_per_trade": swing_risk_usd,
                "strategy": "Futures 2x (Dump/Reversal 3 Hari, SL On-Chain 10%, Trailing HWM)",
            },
            "intraday": {
                "pct": round(intraday_pct, 1),
                "budget_usd": intra_budget,
                "max_slots": intra_slots,
                "margin_per_slot": intra_margin_per_slot,
                "leverage": intra_lev,
                "notional_per_slot": intra_notional_per_slot,
                "risk_per_trade_pct": intra_risk_pct,
                "risk_usd_per_trade": intra_risk_usd,
                "strategy": "Futures 2x-3x (Precision 15m, SL 2.5-4.5%, Auto-Close 6 Jam)",
            },
            "buffer": {
                "pct": round(buffer_pct, 1),
                "budget_usd": buffer_budget,
                "description": "Free Margin Hyperliquid (Buffer Likuiditas & Proteksi Margin Call)",
            }
        }

    @classmethod
    def calculate_dynamic_spot_allocation(
        cls,
        total_equity: float,
        spot_remaining: float,
        available_cash: Optional[float] = None,
        ss_score: float = 70.0,
        es_score: float = 70.0,
        drs_score: float = 10.0,
        signal_tier: str = "PRIME_CONVICTION",
        max_alloc_pct: float = 100.0,
        config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Menghitung nilai rekomendasi default pembelian Spot DCA yang merupakan
        fungsi matematis terpadu dari:
        1. Saldo Total (total_equity)
        2. Saldo Sisa (spot_remaining & available_cash)
        3. Seberapa Confidence (ss_score, es_score, drs_score, signal_tier, max_alloc_pct)

        Safeguards:
        - Rekomendasi default TIDAK BOLEH melebihi saldo sisa (spot_remaining maupun available_cash).
        - Rekomendasi default TIDAK BOLEH melebihi total saldo (total_equity).
        - Jika saldo sisa <= 0 atau saldo total <= 0, rekomendasi = 0.0.
        - Alokasi diskalakan proporsional terhadap skor confidence dan tier koin.
        """
        cfg = config or cls.get_config()
        spot_pct = float(cfg.get("spot_vault_pct", 0.55))
        spot_coins = max(1, int(cfg.get("spot_target_coins", 5)))

        total_equity = max(0.0, float(total_equity))
        spot_remaining = max(0.0, float(spot_remaining))

        # 1. Batas saldo sisa efektif (effective remaining balance)
        if available_cash is not None and available_cash > 0:
            effective_remaining = min(spot_remaining, float(available_cash))
        else:
            effective_remaining = spot_remaining

        if total_equity <= 0 or effective_remaining <= 0:
            return {
                "recommended_budget": 0.0,
                "target_budget": 0.0,
                "base_slot_budget": 0.0,
                "confidence_score": 0.0,
                "confidence_factor": 0.0,
                "effective_remaining": effective_remaining,
                "total_equity": total_equity,
                "is_capped_by_remaining": True,
                "reason": "Saldo sisa atau total saldo tidak mencukupi (0 USD)."
            }

        # 2. Base slot budget (proporsi modal ideal per koin dari total saldo)
        base_slot_budget = round((total_equity * spot_pct) / spot_coins, 2)

        # 3. Hitung Seberapa Confidence (0 - 100%)
        # SS (Setup Score) bobot 40%, ES (Whale Engine Score) bobot 40%, DRS (Distribution Risk) bobot 20%
        whale_holding_score = max(0.0, min(100.0, 100.0 - drs_score))
        confidence_score = round(
            (ss_score * 0.40) + (es_score * 0.40) + (whale_holding_score * 0.20),
            1
        )

        # Tier factor multiplier (PRIME: 1.0, CHALLENGER: 0.75, SCOUT: 0.50)
        tier_multiplier = max(0.2, min(1.0, max_alloc_pct / 100.0))

        # Fine confidence modifier: scale between 0.60x and 1.15x based on confidence_score / 75.0
        fine_mod = max(0.60, min(1.15, confidence_score / 75.0))
        confidence_factor = round(tier_multiplier * fine_mod, 3)

        # 4. Target nominal sebelum pembatasan saldo
        target_budget = round(base_slot_budget * confidence_factor, 2)

        # 5. Safeguard: Fungsi dari saldo total & saldo sisa
        # Rekomendasi tidak boleh melebihi sisa kuota spot, sisa kas, maupun total ekuitas
        recommended_budget = min(target_budget, effective_remaining)
        recommended_budget = min(recommended_budget, total_equity)
        recommended_budget = max(0.0, round(recommended_budget, 2))

        is_capped = recommended_budget < target_budget

        return {
            "recommended_budget": recommended_budget,
            "target_budget": target_budget,
            "base_slot_budget": base_slot_budget,
            "confidence_score": confidence_score,
            "confidence_factor": confidence_factor,
            "effective_remaining": effective_remaining,
            "total_equity": total_equity,
            "is_capped_by_remaining": is_capped,
            "reason": (
                f"Dibatasi saldo sisa (${effective_remaining:,.2f})" if is_capped
                else "Sesuai kalkulasi alokasi & confidence"
            )
        }

    @classmethod
    def format_allocation_report(cls, alloc: Dict[str, Any]) -> str:
        """Membuat tampilan ringkasan alokasi teks yang rapi."""
        eq = alloc["total_equity"]
        sp = alloc["spot"]
        sw = alloc["swing"]
        it = alloc["intraday"]
        bf = alloc["buffer"]

        lines = [
            "=" * 78,
            f"   ⚖️  REKOMENDASI SIZING PORTOFOLIO CORE-SATELLITE (Total Ekuitas: ${eq:,.2f})",
            "=" * 78,
            f"🏦 1. SPOT ACCUMULATION VAULT  [{sp['pct']}% ➔ ${sp['budget_usd']:,.2f}]",
            f"   • Filosofi       : {sp['strategy']}",
            f"   • Target Koin    : {sp['num_coins']} Koin PRIME / CHALLENGER",
            f"   • Alokasi / Koin : ${sp['budget_per_coin']:,.2f} per koin",
            f"   • Sistem 3-DCA   : Tranche 1 Scout (${sp['tranche_1_scout_usd']:,.2f})",
            f"                      Tranche 2 Pullback -10% (${sp['tranche_2_pullback_usd']:,.2f})",
            f"                      Tranche 3 Breakout +12% (${sp['tranche_3_breakout_usd']:,.2f})",
            "",
            f"🌊 2. SWING TRADING (3-DAY)    [{sw['pct']}% ➔ ${sw['budget_usd']:,.2f}]",
            f"   • Filosofi       : {sw['strategy']}",
            f"   • Slot Posisi    : Max {sw['max_slots']} Slot Aktif Bersamaan",
            f"   • Margin / Trade : ${sw['margin_per_slot']:,.2f} (Notional: ${sw['notional_per_slot']:,.2f} @ {sw['leverage']}x)",
            f"   • Risiko / Trade : {sw['risk_per_trade_pct']:.1f}% Ekuitas (${sw['risk_usd_per_trade']:,.2f})",
            "",
            f"⚡ 3. INTRADAY DAY-TRADE (6H)   [{it['pct']}% ➔ ${it['budget_usd']:,.2f}]",
            f"   • Filosofi       : {it['strategy']}",
            f"   • Slot Posisi    : Max {it['max_slots']} Slot Aktif Bersamaan",
            f"   • Margin / Trade : ${it['margin_per_slot']:,.2f} (Notional: ${it['notional_per_slot']:,.2f} @ {it['leverage']}x)",
            f"   • Risiko / Trade : {it['risk_per_trade_pct']:.1f}% Ekuitas (${it['risk_usd_per_trade']:,.2f})",
            "",
            f"🛡️  4. FREE MARGIN BUFFER       [{bf['pct']}% ➔ ${bf['budget_usd']:,.2f}]",
            f"   • Fungsi         : {bf['description']}",
            "=" * 78,
        ]
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # DYNAMIC RISK ENHANCEMENTS: VOLATILITY SIZING, CORRELATION & HEAT GATES
    # -------------------------------------------------------------------------
    @classmethod
    def calculate_atr_volatility_sizing(
        cls,
        total_equity: float,
        strategy: str,
        entry_px: float,
        atr_14: float,
        sl_atr_mult: float = 1.5,
        custom_config: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Volatility-Parity Sizing: Menghitung ukuran posisi dolar berdasarkan volatilitas ATR.
        Memastikan kerugian saat SL tercapai tepat setara dengan target risk % akun.
        """
        cfg = custom_config or cls.get_config()
        equity = max(0.0, float(total_equity))
        risk_pct = float(cfg.get("swing_risk_per_trade_pct" if strategy == "SWING" else "intraday_risk_per_trade_pct", 1.5))
        target_risk_usd = equity * (risk_pct / 100.0)

        sl_distance_usd = max(0.0001, atr_14 * sl_atr_mult)
        sl_distance_pct = (sl_distance_usd / entry_px) * 100.0 if entry_px > 0 else 3.0

        # Posisi size nominal agar kerugian pas di target_risk_usd
        calculated_pos_size_usd = target_risk_usd / (sl_distance_pct / 100.0) if sl_distance_pct > 0 else 0.0

        # Cap dengan batas slot maksimal dari konfigurasi Core-Satellite
        alloc = cls.calculate_allocation(equity, cfg)
        max_allowed_pos_usd = float(alloc["swing" if strategy == "SWING" else "intraday"]["notional_per_slot"])

        final_pos_size_usd = min(calculated_pos_size_usd, max_allowed_pos_usd)
        lev = int(cfg.get("swing_leverage" if strategy == "SWING" else "intraday_leverage", 2))
        required_margin = final_pos_size_usd / lev if lev > 0 else final_pos_size_usd

        return {
            "target_risk_usd": round(target_risk_usd, 2),
            "sl_distance_pct": round(sl_distance_pct, 2),
            "calculated_pos_usd": round(calculated_pos_size_usd, 2),
            "capped_pos_usd": round(final_pos_size_usd, 2),
            "required_margin_usd": round(required_margin, 2),
            "leverage": lev,
            "is_capped_by_slot": calculated_pos_size_usd > max_allowed_pos_usd
        }

    @classmethod
    def check_cross_asset_correlation_veto(
        cls,
        candidate_coin: str,
        active_positions: list,
        db = None,
        max_correlation: float = 0.70
    ) -> Tuple[bool, str]:
        """
        Cross-Asset Correlation Veto:
        Mencegah penumpukan posisi short pada koin yang berkorelasi tinggi (> 0.70)
        untuk melindungi akun dari drawdown serentak (misal sektor meme atau L1 pump bersamaan).
        """
        if not active_positions:
            return True, "No active positions (correlation clear)"

        def clean_kilo(sym: str) -> str:
            sym = str(sym).upper()
            if sym.startswith("K") and sym[1:] in {"PEPE", "BONK", "SHIB", "FLOKI", "LUNC", "DOGS", "NEIRO"}:
                return sym[1:]
            return sym

        # Ekosistem klaster statis sebagai pelindung cepat (fast gate)
        CLUSTERS = {
            "MEME": {"PEPE", "KPEPE", "BONK", "KBONK", "SHIB", "KSHIB", "FLOKI", "KFLOKI", "WIF", "DOGE", "POPCAT", "PUMP", "NEIRO", "KNEIRO"},
            "AI": {"FET", "NEAR", "RENDER", "TAO", "IO", "ATH", "WLD", "GRT"},
            "DEFI": {"UNI", "AAVE", "CRV", "MKR", "SNX", "LDO", "ENA", "PENDLE"},
            "SOL_ECO": {"SOL", "JUP", "RAY", "JTO", "PYTH", "W"}
        }

        cand = clean_kilo(candidate_coin)
        cand_cluster = None
        for cname, coins in CLUSTERS.items():
            if candidate_coin.upper() in coins or cand in coins:
                cand_cluster = cname
                break

        if cand_cluster:
            for pos in active_positions:
                p_coin = str(pos.get("coin", "")).upper()
                p_clean = clean_kilo(p_coin)
                if p_coin in CLUSTERS[cand_cluster] or p_clean in CLUSTERS[cand_cluster]:
                    return False, f"VETO CORRELATION: {candidate_coin} berada di klaster yang sama ({cand_cluster}) dengan posisi aktif {p_coin}"

        return True, "Correlation check PASSED"

    @classmethod
    def check_portfolio_heat_limit(
        cls,
        total_equity: float,
        active_positions: list,
        new_risk_usd: float,
        max_heat_pct: float = 6.0
    ) -> Tuple[bool, str]:
        """
        Portfolio Heat Ceiling:
        Memastikan total akumulasi risiko terbuka dari seluruh posisi <= 6% ekuitas.
        """
        max_allowed_heat_usd = total_equity * (max_heat_pct / 100.0)
        current_open_risk_usd = 0.0

        for p in active_positions:
            # Posisi short di Hyperliquid bernilai negatif (szi < 0), gunakan abs()
            pos_sz_usd = abs(float(p.get("size", 0.0))) * float(p.get("entry_px", 0.0))
            sl_px = float(p.get("current_sl_px", 0.0))
            entry = float(p.get("entry_px", 0.0))
            if entry > 0 and sl_px > 0:
                risk_dist_pct = abs(sl_px - entry) / entry
                current_open_risk_usd += (pos_sz_usd * risk_dist_pct)
            else:
                current_open_risk_usd += (pos_sz_usd * 0.015)

        projected_heat_usd = current_open_risk_usd + new_risk_usd
        if projected_heat_usd > max_allowed_heat_usd:
            return False, f"PORTFOLIO HEAT VETO: Total risk (${projected_heat_usd:.2f}) melebihi batas {max_heat_pct}% (${max_allowed_heat_usd:.2f})"

        return True, f"Heat check PASSED: Projected heat ${projected_heat_usd:.2f} / ${max_allowed_heat_usd:.2f}"

    @classmethod
    def calculate_cycle_sizing_modifier(cls, asof: Optional[Any] = None) -> Dict[str, Any]:
        """
        Menghitung pengali sizing dinamis kalender berbasis:
        1. Siklus Mingguan (Day of Week Effect: Selasa Golden Day 1.2x, Weekend 0.5x)
        2. Siklus Bulanan (Turn of Month Squeeze Risk 0.75x, Deribit Expiry Harvest 1.2x)
        """
        try:
            from src.cycle_engine import evaluate_weekly_cycle, evaluate_monthly_cycle
            w_res = evaluate_weekly_cycle(asof=asof)
            m_res = evaluate_monthly_cycle(asof=asof)
            raw_mult = w_res.sizing_multiplier * m_res.sizing_multiplier
            clamped_mult = round(max(0.40, min(1.35, raw_mult)), 2)
            return {
                "composite_multiplier": clamped_mult,
                "weekly_cycle": w_res,
                "monthly_cycle": m_res,
                "weekly_phase": w_res.phase_name,
                "weekly_multiplier": w_res.sizing_multiplier,
                "weekly_stance": w_res.tactical_stance,
                "monthly_phase": m_res.phase_name,
                "monthly_multiplier": m_res.sizing_multiplier,
                "monthly_stance": m_res.tactical_stance,
                "is_options_expiry_week": m_res.is_options_expiry_week,
                "is_last_friday": m_res.is_last_friday,
                "rules": w_res.actionable_rules + m_res.actionable_rules
            }
        except Exception as e:
            return {
                "composite_multiplier": 1.0,
                "error": str(e),
                "weekly_phase": "STANDARD",
                "monthly_phase": "STANDARD",
                "weekly_multiplier": 1.0,
                "monthly_multiplier": 1.0,
                "rules": []
            }
