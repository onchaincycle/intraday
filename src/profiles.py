"""
ShortHunter Strategy Profiles — INTRADAY SAJA (2026-10-23).
Profil SWING multi-hari DIHAPUS atas keputusan user. Satu-satunya profil: DAYTRADE (intraday 15m).
Label lama "SWING" pada posisi/jurnal historis tetap dikenali sebagai label (legacy), tetapi
get_profile() selalu mengembalikan profil DAYTRADE dan tidak ada jalur untuk membuka posisi swing.
"""

import os
import json
from typing import Dict, Optional

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_CONFIG_FILE = os.path.join(ROOT_DIR, "data", "active_profile.json")

PROFILES = {
    "DAYTRADE": {
        "key": "DAYTRADE",
        "name": "Mode Intraday Day-Trade (Precision v2.3)",
        "badge": "⚡ INTRADAY",
        "description": "Eksekusi intraday 15m: Dynamic 2x-3x leverage (Grade A), SL teknis 2.5-4.5%, TP1 1.0R (2.5-4.5%), TP2 2.0R (5.0-9.0%), auto-close 6 jam.",
        # --- Screening ---
        "timeframe": "15m / 1H",
        "min_score": 75.0,
        # --- Exit model: kelipatan risiko (R) dari SL dinamis per posisi ---
        "exit_model": "R_MULTIPLE",
        "sl_pct": 3.2,              # default bila SL dinamis tidak tersedia
        "sl_min_pct": 2.5,
        "sl_max_pct": 4.5,
        "half_risk_r": 0.45,        # Step 1 (0.45R / ~1.4% move): Perketat SL on-chain 50%
        "early_be_r": 0.65,         # Step 2 (0.65R / ~2.1% move): Geser SL on-chain ke Breakeven + Fee
        "tier1_trigger_r": 1.5,     # Step 2.5 (+1.5R move / >=+4.0%): Trigger Tier-1 Profit Lock
        "tier1_lock_r": 0.5,        # Kunci minimal +0.5R profit (~1.5% move)
        "tier1_lock_profit_pct": 1.5, # Minimum net profit lock %
        "continuous_trail_enabled": True, # Continuous Trailing Engine
        "continuous_trail_start_r": 1.2,   # Aktif sejak >= 1.2R (~3.0-3.8% move) atau >= 4.0%
        "continuous_trail_ratio": 0.50,     # Kunci 50% dari peak R / move
        "continuous_trail_step_r": 0.25,   # Ratchet on-chain per kenaikan peak >= 0.25R
        "tp1_r": 1.0,               # Step 3 (1.0R / 2.5-4.5% move): Trim 50% & kunci SL di profit +0.3R
        "tp2_r": 2.0,               # Step 4 (2.0R / 5.0-9.0% move): Tutup sisa (Hard Cap 9.0% gerak harga)
        "trim_pct": 3.2,            # = 1.0R dari default SL (fallback)
        "tp2_pct": 6.4,             # = 2.0R dari default SL (fallback)
        "tp2_max_pct": 9.0,         # Hard cap TP2 gerak harga spot (maks 9.0% / 18-27% ROE)
        "runner_pct": None,
        "max_hold_hours": 6,
        "time_exit_enabled": True,
        "stalemate_hours": 6,
        # --- Risk & portfolio ---
        "leverage": 2,              # Baseline 2x (auto-boost ke 3x untuk Grade A / A+)
        "dynamic_leverage_enabled": True,
        "risk_pct": 1.5,            # risiko per trade dinaikkan sedikit (1.0% -> 1.5%)
        "max_concurrent": 4,        # maksimal 4 slot intraday terbuka bersamaan (sebelumnya 3)
        "max_concurrent_short": 2,  # alokasi slot intraday short
        "max_concurrent_long": 2,   # alokasi slot intraday long
        "max_daily_losses": 3,      # circuit breaker: stop entry intraday setelah 3 loss/hari
        # --- Eksekusi & monitoring ---
        "session_gate": False,      # Dinonaktifkan atas instruksi user (buka 24 jam / sesi Asia aktif untuk trade riil)
        "poll_seconds": 10,
        "fee_roundtrip_pct": 0.09,
        "target_desc": "Half-Risk @ 0.45R ➔ Early BE @ 0.65R ➔ Continuous Trailing (50% HWM) ➔ TP1 1.0R (Trim 50%) ➔ TP2 2.0R | Auto-Close: 6 Jam"
    }
}

STRATEGY_ALIASES = {
    "DAYTRADE": "DAYTRADE", "INTRADAY": "DAYTRADE", "DAY": "DAYTRADE",
}
LEGACY_LABELS = {"SWING", "MULTIDAY", "MULTI_DAY"}   # label historis; tidak ada profilnya lagi


def normalize_strategy(key: Optional[str]) -> str:
    """INTRADAY/DAYTRADE/kosong -> DAYTRADE. Label lama (SWING) dipertahankan sebagai 'SWING' = legacy (tidak dikelola)."""
    if not key:
        return "DAYTRADE"
    k = str(key).upper()
    if k in LEGACY_LABELS:
        return "SWING"
    return STRATEGY_ALIASES.get(k, "DAYTRADE")


def get_profile(key: Optional[str] = None) -> Dict:
    """Selalu profil DAYTRADE (satu-satunya stream)."""
    return PROFILES["DAYTRADE"]


def get_active_profile() -> Dict:
    return PROFILES["DAYTRADE"]

def set_active_profile(profile_key: str) -> Dict:
    """Sets and persists the active (default UI) profile."""
    profile_key = normalize_strategy(profile_key)
    if profile_key not in PROFILES:
        return {"success": False, "error": f"Profil {profile_key} tidak valid. Pilih SWING atau DAYTRADE."}

    os.makedirs(os.path.dirname(PROFILE_CONFIG_FILE), exist_ok=True)
    with open(PROFILE_CONFIG_FILE, "w") as f:
        json.dump({"active_key": profile_key}, f, indent=2)

    return {"success": True, "profile": PROFILES[profile_key]}
