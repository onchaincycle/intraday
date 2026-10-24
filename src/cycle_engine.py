# Standalone Cycle Engine
from __future__ import annotations
import os
import sys
import json
import sqlite3
import calendar
import datetime as dt
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
import numpy as np
import pandas as pd

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

DB_PATH = os.path.join(ROOT_DIR, 'data', 'shorthunter_trade.db')

CYCLE_DAYS = 1460
HALVINGS = [
    dt.date(2012, 11, 28),
    dt.date(2016, 7, 9),
    dt.date(2020, 5, 11),
    dt.date(2024, 4, 20)
]
NEXT_HALVING_EST = dt.date(2028, 4, 15)

MACRO_PHASES = {
    'A': ('Akumulasi pasca-halving', 'Tahun pertama setelah halving: pasokan berkurang, harga konsolidasi mendatar.'),
    'B': ('Ekspansi (Bull Market)', 'Harga rally kuat dan mencetak rekor baru; momentum publik masuk.'),
    'C': ('Puncak / Distribusi', 'Dekat ATH siklus; risiko tertinggi, kenaikan jenuh & smart money distribusi.'),
    'D': ('Bear Market Penurunan', 'Harga terkoreksi dalam dari puncak.'),
    'E': ('Bear Lanjut / Jendela Dasar', 'Sudah setahun lebih sejak puncak; jendela pembentukan dasar siklus.'),
    'F': ('Pemulihan Awal', 'Harga mantul >30% dari low siklus dan menembus SMA200 hari.'),
}

@dataclass
class MacroCycleResult:
    asof_date: str
    btc_price: float
    days_since_halving: int
    cycle_progress_pct: float
    cycle_year: int
    sma200: float
    mayer_multiple: float
    peak_price: float
    peak_date: str
    days_since_peak: int
    drawdown_from_peak_pct: float
    trough_price: float
    trough_date: str
    gain_from_trough_pct: float
    phase: str
    phase_name: str
    phase_desc: str


def evaluate_macro_cycle(btc_daily_df: pd.DataFrame, asof: Optional[dt.date] = None) -> MacroCycleResult:
    asof = asof or dt.date.today()
    df = btc_daily_df.sort_index().copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        if 'timestamp' in df.columns:
            df.index = pd.to_datetime(df['timestamp'], unit='ms' if df['timestamp'].iloc[0] > 1e11 else None)
        elif 'date' in df.columns:
            df.index = pd.to_datetime(df['date'])
    close = df['close'].dropna()
    cur_price = float(close.iloc[-1])
    last_halving = HALVINGS[-1]
    dsh = max(0, (asof - last_halving).days)
    cycle_pct = round(min(1.0, dsh / CYCLE_DAYS), 4)
    cycle_year = min(4, int(dsh / 365) + 1)
    sma200 = float(close.rolling(200, min_periods=50).mean().iloc[-1])
    mayer = round(cur_price / sma200, 3) if sma200 > 0 else 1.0
    post_halving_close = close[close.index >= pd.Timestamp(last_halving)]
    if len(post_halving_close) == 0:
        post_halving_close = close.iloc[-300:]
    peak_idx = post_halving_close.idxmax()
    peak_price = float(post_halving_close.max())
    peak_date = str(peak_idx.date()) if hasattr(peak_idx, 'date') else str(peak_idx)[:10]
    dsp = max(0, (asof - dt.date.fromisoformat(peak_date)).days)
    dd = round((cur_price - peak_price) / peak_price, 4)
    post_peak_close = close[close.index >= pd.Timestamp(peak_date)]
    if len(post_peak_close) > 0:
        trough_idx = post_peak_close.idxmin()
        trough_price = float(post_peak_close.min())
        trough_date = str(trough_idx.date()) if hasattr(trough_idx, 'date') else str(trough_idx)[:10]
    else:
        trough_idx = peak_idx
        trough_price = peak_price
        trough_date = peak_date
    gain_from_trough = round((cur_price - trough_price) / trough_price, 4) if trough_price > 0 else 0.0
    above200 = cur_price > sma200
    if dsp == 0 or (dsh < 365 and dd > -0.25):
        phase = 'A'
    elif dd > -0.25:
        phase = 'C' if (dsp < 120 and dsh >= 365) else 'B'
    elif gain_from_trough > 0.30 and above200 and dsp > 200:
        phase = 'F'
    elif dsp >= 300 or dd <= -0.50:
        phase = 'E'
    else:
        phase = 'D'
    name, desc = MACRO_PHASES[phase]
    return MacroCycleResult(
        asof_date=str(asof), btc_price=cur_price, days_since_halving=dsh,
        cycle_progress_pct=cycle_pct, cycle_year=cycle_year, sma200=round(sma200, 2),
        mayer_multiple=mayer, peak_price=round(peak_price, 2), peak_date=peak_date,
        days_since_peak=dsp, drawdown_from_peak_pct=dd, trough_price=round(trough_price, 2),
        trough_date=trough_date, gain_from_trough_pct=gain_from_trough, phase=phase,
        phase_name=name, phase_desc=desc
    )

@dataclass
class CoinCycleResult:
    coin: str
    asof_date: str
    price: float
    bms_score: float
    base_days: int
    retests: int
    vol_compression: float
    proximity_to_low_pct: float
    coin_phase: str
    phase_label: str
    drs_score: float
    smart_money_status: str
    spot_verdict: str
    futures_verdict: str
    trading_verdict: str
    verdict_reason: str


@dataclass
class WeeklyCycleResult:
    asof_date: str
    day_name: str
    day_of_week: int  # 0=Monday, 6=Sunday
    phase_code: str
    phase_name: str
    historical_down_win_rate: float
    historical_avg_alt_return: float
    excess_vs_btc: float
    market_behavior: str
    tactical_stance: str
    sizing_multiplier: float
    actionable_rules: List[str]


@dataclass
class MonthlyCycleResult:
    asof_date: str
    day_of_month: int
    days_in_month: int
    cycle_phase: str
    phase_name: str
    is_options_expiry_week: bool
    is_last_friday: bool
    historical_avg_return: float
    institutional_flow: str
    tactical_stance: str
    sizing_multiplier: float
    actionable_rules: List[str]

def compute_bms(df_daily: pd.DataFrame, lookback: int = 180) -> Dict[str, Any]:
    c = df_daily['close'].iloc[-lookback:] if len(df_daily) >= lookback else df_daily['close']
    h = df_daily['high'].iloc[-lookback:] if len(df_daily) >= lookback else df_daily['high']
    l = df_daily['low'].iloc[-lookback:] if len(df_daily) >= lookback else df_daily['low']
    lo, hi = float(l.min()), float(h.max())
    rng = max(hi - lo, 1e-8)
    cur_px = float(c.iloc[-1])
    ceiling = max(lo + 0.30 * rng, lo * 1.25)
    in_base = (c <= ceiling).to_numpy()
    run_len = 0
    cur_len = 0
    gap = 0
    for b in in_base:
        if b:
            cur_len += 1
            gap = 0
        elif gap < 3:
            gap += 1
            cur_len += 1
        else:
            if cur_len > run_len: run_len = cur_len
            cur_len = 0; gap = 0
    if cur_len > run_len: run_len = cur_len
    retests = 0; last_touch = -99; l_vals = l.to_numpy()
    for i in range(len(l_vals)):
        if l_vals[i] <= lo * 1.06 and (i - last_touch) >= 5:
            retests += 1; last_touch = i
    tr = np.maximum(h - l, np.abs(h - c.shift(1)))
    atr14 = float(tr.tail(14).mean())
    atr60 = float(tr.tail(60).mean()) if len(tr) >= 60 else atr14
    vol_comp_ratio = round(atr14 / max(atr60, 1e-8), 3)
    vol_score = float(np.clip(100 * (1.2 - vol_comp_ratio) / 0.7, 0, 100))
    prox_ratio = (cur_px - lo) / rng
    prox_score = float(np.clip(100 * (1 - prox_ratio / 0.30), 0, 100))
    dur_score = float(np.clip(run_len / 120.0 * 100, 0, 100))
    stab_score = float(in_base[-90:].mean() * 100) if len(in_base) >= 90 else dur_score
    ret_score = float(np.clip(retests / 3.0 * 100, 0, 100))
    bms = round(0.30 * dur_score + 0.20 * stab_score + 0.20 * ret_score + 0.15 * vol_score + 0.15 * prox_score, 1)
    return {
        'bms': bms, 'base_days': int(run_len), 'retests': int(retests),
        'vol_comp_ratio': vol_comp_ratio, 'proximity_pct': round(prox_ratio * 100, 1),
        'low_180d': lo, 'high_180d': hi, 'ceiling': ceiling, 'in_base_now': bool(cur_px <= ceiling)
    }

def evaluate_coin_cycle(
    coin: str,
    df_daily: pd.DataFrame,
    macro_cycle: Optional[MacroCycleResult] = None,
    nansen_sm_status: str = 'NEUTRAL',
    nansen_netflow_usd: float = 0.0,
    asof: Optional[dt.date] = None
) -> CoinCycleResult:
    asof = asof or dt.date.today()
    c = df_daily['close'].dropna()
    cur_px = float(c.iloc[-1])
    bms_res = compute_bms(df_daily)
    bms = bms_res['bms']
    sma30 = float(c.rolling(30, min_periods=10).mean().iloc[-1])
    above_sma30 = cur_px > sma30
    sma30_slope = (sma30 - float(c.rolling(30, min_periods=10).mean().iloc[-5])) / sma30 if len(c) >= 35 else 0.0
    lo_90 = float(df_daily['low'].tail(90).min()) if 'low' in df_daily else float(c.tail(90).min())
    hi_90 = float(df_daily['high'].tail(90).max()) if 'high' in df_daily else float(c.tail(90).max())
    dd_90 = (cur_px - hi_90) / hi_90
    gain_from_low_90 = (cur_px - lo_90) / lo_90 if lo_90 > 0 else 0.0
    drs = 15.0
    if nansen_sm_status == 'DUMPING': drs += 35.0
    elif nansen_sm_status == 'ACCUMULATING': drs = max(0.0, drs - 10.0)
    if not above_sma30 and sma30_slope < -0.01: drs += 20.0
    if cur_px > lo_90 * 2.0: drs += 25.0
    drs = round(float(np.clip(drs, 0, 100)), 1)
    whale_accumulating = (nansen_sm_status == 'ACCUMULATING' or nansen_netflow_usd >= 100_000)
    whale_dumping = (nansen_sm_status == 'DUMPING' or nansen_netflow_usd <= -100_000)
    if dd_90 <= -0.45 and gain_from_low_90 < 0.10:
        phase, label = 'P1', 'Capitulation (Penurunan Parah)'
    elif bms_res['in_base_now'] or (bms >= 65 and cur_px < bms_res['ceiling'] * 1.05):
        if whale_accumulating and bms >= 70:
            phase, label = 'P4', 'Supply Absorption (Whale Menyerap Pasokan)'
        elif whale_accumulating:
            phase, label = 'P3', 'Smart Money Accumulation'
        else:
            phase, label = 'P2', 'Seller Exhaustion (Dasar Konsolidasi)'
    elif above_sma30 and sma30_slope > 0 and gain_from_low_90 > 0.35:
        if cur_px > lo_90 * 1.80 or drs >= 55 or whale_dumping:
            phase, label = 'P7', 'Distribution (Tahap Jual/Pelepasan)'
        elif cur_px > lo_90 * 1.40:
            phase, label = 'P6', 'Expansion / Repricing (Tren Naik)'
        else:
            phase, label = 'P5', 'Demand Trigger (Awal Breakout Tren)'
    elif not above_sma30 and drs >= 50:
        phase, label = 'P7', 'Distribution (Breakdown Struktur)'
    else:
        phase, label = 'P2', 'Seller Exhaustion (Konsolidasi Dasar)'
    # Action / Verdict Mapping (Terpisah Presisi: Spot Vault vs Futures)
    if phase in ('P2', 'P3', 'P4'):
        spot_verdict = 'SPOT_ACCUMULATE' if (bms >= 70 and drs < 40) else 'SPOT_WATCH'
        futures_verdict = 'FUTURES_VETO_BASE'
        trading_verdict = spot_verdict if spot_verdict == 'SPOT_ACCUMULATE' else futures_verdict
        reason = f'Koin di dasar range ({phase}, BMS {bms}, DRS {drs}). Spot Accumulate valid, Futures Momentum diveto.'
    elif phase in ('P5', 'P6'):
        spot_verdict = 'SPOT_HOLD_TREND'
        futures_verdict = 'FUTURES_MOMENTUM_EXPANSION'
        trading_verdict = futures_verdict
        reason = f'Tren ekspansi valid ({phase}). Diizinkan untuk ImpulseHunter & Intraday Long.'
    elif phase == 'P7':
        spot_verdict = 'SPOT_HARVEST_REDUCE'
        futures_verdict = 'SHORT_DISTRIBUTION_WATCH'
        trading_verdict = futures_verdict
        reason = f'Tahap distribusi / whale dumping (DRS {drs}). Prioritas ShortHunter atau Harvest Spot.'
    else:
        spot_verdict = 'SPOT_WATCH'
        futures_verdict = 'FUTURES_NO_TRADE'
        trading_verdict = 'NEUTRAL_WATCH'
        reason = f'Fase {phase} belum matang untuk eksekusi.'
    return CoinCycleResult(
        coin=coin, asof_date=str(asof), price=cur_px, bms_score=bms, base_days=bms_res['base_days'],
        retests=bms_res['retests'], vol_compression=bms_res['vol_comp_ratio'], proximity_to_low_pct=bms_res['proximity_pct'],
        coin_phase=phase, phase_label=label, drs_score=drs, smart_money_status=nansen_sm_status,
        spot_verdict=spot_verdict, futures_verdict=futures_verdict,
        trading_verdict=trading_verdict, verdict_reason=reason
    )

def init_cycle_db(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute('PRAGMA journal_mode=WAL;')
        conn.execute("""
            CREATE TABLE IF NOT EXISTS coin_cycle_scores (
                coin TEXT PRIMARY KEY,
                asof_date TEXT NOT NULL,
                price REAL NOT NULL,
                macro_phase TEXT NOT NULL,
                macro_phase_name TEXT NOT NULL,
                coin_phase TEXT NOT NULL,
                coin_phase_label TEXT NOT NULL,
                bms_score REAL NOT NULL,
                drs_score REAL NOT NULL,
                base_days INTEGER NOT NULL,
                retests INTEGER NOT NULL,
                vol_compression REAL NOT NULL,
                smart_money_status TEXT NOT NULL,
                spot_verdict TEXT NOT NULL,
                futures_verdict TEXT NOT NULL,
                trading_verdict TEXT NOT NULL,
                verdict_reason TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
    finally:
        conn.close()

def save_coin_cycle_results(results: List[Tuple[CoinCycleResult, MacroCycleResult]], db_path: str = DB_PATH):
    init_cycle_db(db_path)
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        for cr, mr in results:
            cur.execute("""
                INSERT INTO coin_cycle_scores (
                    coin, asof_date, price, macro_phase, macro_phase_name,
                    coin_phase, coin_phase_label, bms_score, drs_score,
                    base_days, retests, vol_compression, smart_money_status,
                    spot_verdict, futures_verdict, trading_verdict, verdict_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(coin) DO UPDATE SET
                    asof_date=excluded.asof_date, price=excluded.price, macro_phase=excluded.macro_phase,
                    macro_phase_name=excluded.macro_phase_name, coin_phase=excluded.coin_phase,
                    coin_phase_label=excluded.coin_phase_label, bms_score=excluded.bms_score,
                    drs_score=excluded.drs_score, base_days=excluded.base_days, retests=excluded.retests,
                    vol_compression=excluded.vol_compression, smart_money_status=excluded.smart_money_status,
                    spot_verdict=excluded.spot_verdict, futures_verdict=excluded.futures_verdict,
                    trading_verdict=excluded.trading_verdict, verdict_reason=excluded.verdict_reason,
                    updated_at=CURRENT_TIMESTAMP;
            """, (
                cr.coin, cr.asof_date, cr.price, mr.phase, mr.phase_name,
                cr.coin_phase, cr.phase_label, cr.bms_score, cr.drs_score,
                cr.base_days, cr.retests, cr.vol_compression, cr.smart_money_status,
                cr.spot_verdict, cr.futures_verdict, cr.trading_verdict, cr.verdict_reason
            ))
        conn.commit()
    finally:
        conn.close()

def _load_candle_to_daily(fpath: str) -> Optional[pd.DataFrame]:
    """Muat file 15m (feather atau csv) dan resample menjadi candle harian (1D). """
    try:
        if fpath.endswith('.feather'):
            df = pd.read_feather(fpath)
        elif fpath.endswith('.csv'):
            df = pd.read_csv(fpath)
        else:
            return None

        if 'timestamp' in df.columns and 'ts' not in df.columns:
            df = df.rename(columns={'timestamp': 'ts'})
        if 'datetime' in df.columns and 'ts' not in df.columns:
            df['ts'] = pd.to_datetime(df['datetime'])

        if 'ts' in df.columns:
            if not pd.api.types.is_datetime64_any_dtype(df['ts']):
                sample = float(df['ts'].dropna().iloc[0])
                if sample > 1e11:
                    df['ts'] = pd.to_datetime(df['ts'], unit='ms')
                else:
                    df['ts'] = pd.to_datetime(df['ts'], unit='s')
            df = df.set_index('ts')
        elif not isinstance(df.index, pd.DatetimeIndex):
            return None

        for col in ['open', 'high', 'low', 'close', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        daily = df.resample('1D').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
        }).dropna()
        return daily
    except Exception as e:
        return None

def scan_and_update_all_cycles(candle_dir: Optional[str] = None, db_path: str = DB_PATH) -> Tuple[MacroCycleResult, List[CoinCycleResult]]:
    """Scan semua koin di candle_dir (csv/feather), hitung macro cycle & cycle coin, simpan ke database."""
    if candle_dir is None:
        default_dirs = [
            '/Volumes/V-GEN/Riset_Kripto/15m',
            '/Volumes/V-GEN/Riset_Kripto/research/15m',
            '/Volumes/V-GEN/15m',
            os.path.join(ROOT_DIR, 'data', 'research', '15m'),
        ]
        for d in default_dirs:
            if os.path.isdir(d):
                candle_dir = d
                break
        if candle_dir is None:
            candle_dir = os.path.join(ROOT_DIR, 'data', 'research', '15m')

    # Cari file BTC
    btc_path = os.path.join(candle_dir, 'BTC.csv')
    if not os.path.exists(btc_path):
        btc_path = os.path.join(candle_dir, 'BTC.feather')
    if not os.path.exists(btc_path):
        raise FileNotFoundError(f'BTC candle (csv/feather) tidak ditemukan di {candle_dir}')

    df_btc_daily = _load_candle_to_daily(btc_path)
    if df_btc_daily is None or len(df_btc_daily) < 30:
        raise ValueError(f'Gagal memproses candle harian BTC dari {btc_path}')

    macro_res = evaluate_macro_cycle(df_btc_daily)

    # Cek ketersediaan Nansen cache jika ada
    nansen_cache = {}
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nansen_cache';")
        if cur.fetchone():
            cur.execute("SELECT endpoint, response_json FROM nansen_cache WHERE endpoint LIKE '%flow%'")
            for ep, resp_txt in cur.fetchall():
                try:
                    data = json.loads(resp_txt)
                except Exception:
                    pass
        conn.close()
    except Exception:
        pass

    results = []
    all_files = sorted(os.listdir(candle_dir))
    for fname in all_files:
        if not (fname.endswith('.csv') or fname.endswith('.feather')):
            continue
        coin = fname.rsplit('.', 1)[0]
        fpath = os.path.join(candle_dir, fname)
        df_daily = _load_candle_to_daily(fpath)
        if df_daily is None or len(df_daily) < 20:
            continue

        sm_status = nansen_cache.get(coin, {}).get('status', 'NEUTRAL')
        sm_flow = nansen_cache.get(coin, {}).get('netflow', 0.0)

        try:
            c_res = evaluate_coin_cycle(
                coin=coin,
                df_daily=df_daily,
                macro_cycle=macro_res,
                nansen_sm_status=sm_status,
                nansen_netflow_usd=sm_flow
            )
            results.append((c_res, macro_res))
        except Exception as e:
            print(f'Gagal evaluasi cycle {coin}: {e}', file=sys.stderr)

    save_coin_cycle_results(results, db_path)
    return macro_res, [r[0] for r in results]


WEEKLY_PROFILES = {
    0: {
        'phase_code': 'MON_RANGE_FORMATION',
        'phase_name': 'Monday Open & Initial Range Formation',
        'down_win_rate': 49.2,
        'alt_ret': 0.28,
        'excess_btc': -0.34,
        'behavior': 'Volume tertinggi mingguan (~1.02e8). Pembukaan institusi Asia/London. Pembentukan batas awal High-Low Senin.',
        'stance': 'RANGE_BUILD_OBSERVE',
        'sizing': 0.85,
        'rules': [
            'Tandai level High dan Low sesi Senin sebagai batas range mingguan.',
            'Waspadai fakeout awal di sesi London/NY awal sebelum arah mingguan terbentuk.',
            'Altcoin cenderung underperform BTC (-0.34% excess); hindari buy altcoin agresif.'
        ]
    },
    1: {
        'phase_code': 'TUE_BEARISH_EXPANSION',
        'phase_name': 'Tuesday High-Probability Breakdown (Golden Window)',
        'down_win_rate': 53.5,
        'alt_ret': -0.29,
        'excess_btc': -0.36,
        'behavior': 'Hari paling bearish dalam seminggu (53.5% koin merah, median -0.45%). Sering membentuk High of the Week diikuti breakdown tajam.',
        'stance': 'SHORT_OPTIMAL',
        'sizing': 1.20,
        'rules': [
            'Prioritaskan eksekusi setup short breakdown yang terkonfirmasi filter D1/D2.',
            'Gunakan sizing penuh / agresif (1.2x) untuk setup A+ quality.',
            'Biarkan trailing TP berjalan longgar pada koin tren untuk memanen tren turun mingguan.'
        ]
    },
    2: {
        'phase_code': 'WED_MIDWEEK_REVERSAL',
        'phase_name': 'Wednesday Midweek Rotation & Relief Bounce',
        'down_win_rate': 48.9,
        'alt_ret': 0.26,
        'excess_btc': 0.12,
        'behavior': 'Rotasi sektor dan relief bounce pasca dump Selasa. Altcoin cenderung outperform BTC sesaat (+0.12%).',
        'stance': 'SELECTIVE_MOMENTUM',
        'sizing': 1.00,
        'rules': [
            'Jangan short koin di dekat level support kuat koin Base.',
            'Aktifkan Early Breakeven (+0.50R Lock) agresif untuk melindungi open profit.',
            'Short hanya koin yang gagal memantul saat BTC hijau.'
        ]
    },
    3: {
        'phase_code': 'THU_PRE_EXPIRY_CONSOLIDATION',
        'phase_name': 'Thursday Pre-Expiry Positioning',
        'down_win_rate': 51.2,
        'alt_ret': -0.08,
        'excess_btc': -0.15,
        'behavior': 'Positioning menjelang settlement Deribit options mingguan. Volatilitas rentang menyusut.',
        'stance': 'NEUTRAL_HOLD',
        'sizing': 0.90,
        'rules': [
            'Saring kandidat koin lemah yang menunjukkan penumpukan open interest short.',
            'Hindari pembukaan posisi baru di luar sesi London/NY.'
        ]
    },
    4: {
        'phase_code': 'FRI_OPTIONS_EXPIRY_CME_SETTLE',
        'phase_name': 'Friday Expiry Release & CME Close',
        'down_win_rate': 50.7,
        'alt_ret': -0.03,
        'excess_btc': -0.16,
        'behavior': 'Deribit Expiry 08:00 UTC (15:00 WIB) + CME close 21:00 UTC. Max pain pinning pagi, ekspansi sore/malam.',
        'stance': 'POST_EXPIRY_BREAKOUT',
        'sizing': 1.00,
        'rules': [
            'Tunggu hingga lewat pukul 15:00 WIB (08:00 UTC Deribit settlement) sebelum entry breakout.',
            'Amankan profit sebelum sesi CME tutup di tengah malam WIB.'
        ]
    },
    5: {
        'phase_code': 'SAT_LIQUIDITY_VACUUM',
        'phase_name': 'Saturday Liquidity Drop & Slow Bleed',
        'down_win_rate': 52.2,
        'alt_ret': -0.19,
        'excess_btc': -0.12,
        'behavior': 'Volume anjlok >50% (4.70e7). Range menyempit (5.0%). Orderbook tipis rentan manipulasi.',
        'stance': 'WEEKEND_DEFENSIVE',
        'sizing': 0.50,
        'rules': [
            'Pangkas sizing hingga 50% (0.5x).',
            'Dilarang trading breakout naif; utamakan istirahat atau mode konservatif.',
            'Waspadai slippage eksekusi karena tipisnya kedalaman orderbook.'
        ]
    },
    6: {
        'phase_code': 'SUN_WEEKEND_TRAP',
        'phase_name': 'Sunday Pre-Monday Frontrunning / Trap',
        'down_win_rate': 49.7,
        'alt_ret': 0.24,
        'excess_btc': 0.18,
        'behavior': 'Sunday pump manipulatif menjelang open Asia Senin. Volume masih rendah.',
        'stance': 'PRE_WEEK_MONITOR',
        'sizing': 0.50,
        'rules': [
            'Jangan terprovokasi pump hari Minggu bervolume rendah.',
            'Gunakan pump Minggu untuk memetakan level resistensi ideal untuk shorting Senin/Selasa.'
        ]
    }
}

def evaluate_weekly_cycle(asof: Optional[dt.date] = None) -> WeeklyCycleResult:
    asof = asof or dt.date.today()
    dow = asof.weekday()
    days_names = ['Senin (Monday)', 'Selasa (Tuesday)', 'Rabu (Wednesday)', 'Kamis (Thursday)', 'Jumat (Friday)', 'Sabtu (Saturday)', 'Minggu (Sunday)']
    day_name = days_names[dow]
    p = WEEKLY_PROFILES[dow]
    return WeeklyCycleResult(
        asof_date=str(asof),
        day_name=day_name,
        day_of_week=dow,
        phase_code=p['phase_code'],
        phase_name=p['phase_name'],
        historical_down_win_rate=p['down_win_rate'],
        historical_avg_alt_return=p['alt_ret'],
        excess_vs_btc=p['excess_btc'],
        market_behavior=p['behavior'],
        tactical_stance=p['stance'],
        sizing_multiplier=p['sizing'],
        actionable_rules=p['rules']
    )

def evaluate_monthly_cycle(asof: Optional[dt.date] = None) -> MonthlyCycleResult:
    asof = asof or dt.date.today()
    day = asof.day
    _, days_in_month = calendar.monthrange(asof.year, asof.month)
    
    is_last_fri = (asof.weekday() == 4) and (day + 7 > days_in_month)
    is_expiry_week = (day + 7 > days_in_month)
    
    if day <= 5:
        phase_code = 'M1_INFLOW_EXPANSION'
        phase_name = 'Fase Awal Bulan (Turn-of-Month Inflow & Squeeze Risk)'
        avg_ret = 0.37
        flow = 'Inflow likuiditas ritel (gaji) & institusional (DCA dana pensiun, alokasi baru ETF).'
        stance = 'CAUTION_SHORT'
        sizing = 0.75
        rules = [
            'Waspadai short squeeze pada koin-koin ber-funding negatif di awal bulan.',
            'Keringkan agresivitas short; gunakan konfirmasi D1 taker flow ketat.',
            'Momen paling ramah untuk DCA spot portofolio akumulasi.'
        ]
    elif day <= 15:
        phase_code = 'M2_MID_MACRO_FLOW'
        phase_name = 'Fase Pertengahan Awal (Macro Data & Directional Discovery)'
        avg_ret = -0.09
        flow = 'Inflow awal bulan terserap; pasar sensitif terhadap data makro (US CPI/PPI/FOMC).'
        stance = 'TACTICAL_SELECTIVE'
        sizing = 1.00
        rules = [
            'Fokus pada koin dengan katalis orderflow murni.',
            'Waspadai lonjakan volatilitas di hari pengumuman CPI/FOMC AS.'
        ]
    elif day <= 24:
        phase_code = 'M3_PRE_EXPIRY_POSITIONING'
        phase_name = 'Fase Pertengahan Akhir (Pre-Monthly Expiry Positioning)'
        avg_ret = -0.06
        flow = 'Market maker derivatif mulai melakukan rehedging delta/gamma besar-besaran.'
        stance = 'POSITION_EXPANSION'
        sizing = 1.00
        rules = [
            'Petakan level Max Pain BTC dan ETH di Deribit.',
            'Cari altcoin dengan open interest tinggi yang mulai kehilangan volume spot.'
        ]
    else:
        phase_code = 'M4_MONTH_END_HARVEST'
        phase_name = 'Fase Akhir Bulan (Deribit Expiry & Institutional Rebalance)'
        avg_ret = -0.10
        flow = 'Jumat terakhir Deribit Expiry + Month-End Rebalancing hedge funds. Penutupan buku & de-risking.'
        stance = 'SHORT_HARVEST'
        sizing = 1.20
        rules = [
            'Golden Window untuk Shorting Altcoin: breakdown di akhir bulan memiliki kelanjutan tertinggi.',
            'Likuiditas bid institusi sering ditarik menjelang penutupan buku bulanan.',
            'Pantau lonjakan volume di Jumat terakhir bulan (Options Expiry) pasca 15:00 WIB.'
        ]
        
    return MonthlyCycleResult(
        asof_date=str(asof),
        day_of_month=day,
        days_in_month=days_in_month,
        cycle_phase=phase_code,
        phase_name=phase_name,
        is_options_expiry_week=is_expiry_week,
        is_last_friday=is_last_fri,
        historical_avg_return=avg_ret,
        institutional_flow=flow,
        tactical_stance=stance,
        sizing_multiplier=sizing,
        actionable_rules=rules
    )

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Update Market Cycle Scores')
    parser.add_argument('--candle-dir', default=None, help='Path direktori candle (csv/feather)')
    parser.add_argument('--db', default=DB_PATH, help='Path SQLite database')
    args = parser.parse_args()

    macro, coins = scan_and_update_all_cycles(args.candle_dir, args.db)
    print(f'Siklus Makro BTC: Fase {macro.phase} ({macro.phase_name}) | Mayer: {macro.mayer_multiple}x | Harga: ${macro.btc_price:,.2f}')
    print(f'Total {len(coins)} koin berhasil dievaluasi dan disimpan ke {args.db}.')
    
    # Rangkuman per fase
    phase_counts = pd.Series([c.coin_phase for c in coins]).value_counts()
    print('\nDistribusi Fase Wyckoff Koin:')
    for ph, count in phase_counts.items():
        print(f'  {ph}: {count} koin')
    
    spot_acc = [c.coin for c in coins if c.spot_verdict == 'SPOT_ACCUMULATE']
    fut_veto = [c.coin for c in coins if c.futures_verdict == 'FUTURES_VETO_BASE']
    fut_exp = [c.coin for c in coins if c.futures_verdict == 'FUTURES_MOMENTUM_EXPANSION']
    print(f'\n[SPOT VAULT] Akumulasi Wyckoff ({len(spot_acc)} koin): {", ".join(spot_acc[:15])}...')
    print(f'[FUTURES INTRADAY] Veto Base ({len(fut_veto)} koin): {", ".join(fut_veto[:15])}...')
    print(f'[FUTURES MOMENTUM] Ekspansi Tren ({len(fut_exp)} koin): {", ".join(fut_exp[:15])}...')

