# -*- coding: utf-8 -*-
# post_trade_app.py — Relatório Pós-Trade MT5/CSV
# Melhorias:
# - Timezone fix (sem erro tz-aware)
# - Parser de setup compatível com o robô (ENTRY-<SETUP>)
# - MT5 com MAGIC: pareia BUY->SELL e puxa setup da ordem de ENTRADA
# - Custos B3 automáticos + fallback manual/URL
# - Candles: entrada/saída precisas (fallback só no CSV se faltar preço)
# - Stop inicial do CSV (stop_price) opcional
# - Tabelas formatadas (R$ e %), leaderboards, curvas, limites, export

import io, re
import importlib
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import streamlit as st
import altair as alt
import plotly.graph_objects as go  # Plotly para candles
import json
import os

# MT5 opcional
mt5 = None
try:
    mt5 = importlib.import_module("MetaTrader5")
    HAS_MT5 = True
except Exception:
    HAS_MT5 = False

BASE_DIR = os.path.dirname(__file__)
CANDLE_COLORS_FILE = os.path.join(BASE_DIR, "candle_colors.json")
ROBOT_PREFS_FILE = os.path.join(BASE_DIR, "stored_robot_prefs.json")

SESSION_DEFAULTS = {
    "use_csv": not HAS_MT5,
    "auto_b3": False,
    "b3_source_url": "",
    "initial_capital": 0.0,
    "fee_b3_pct_manual": 0.0,
    "fee_broker_in": 0.99,
    "fee_broker_out": 0.99,
    "base_pct": "notional",
    "max_concurrent_trades": 0,
    "tv_entry_line_color": "#4169E1",
    "tv_stop_line_color": "#DC143C",
    "tv_exit_line_color": "#2E8B57",
    "tv_marker_color": "#E2E8F0",
    "tv_bg_color": "#0B1220",
    "tv_dark_theme": True,
    "tv_watermark_text": "",
    "tv_watermark_color": "#E2E8F0",
    "tv_watermark_opacity": 0.60,
    "capital_scenario_mode": "Original (volume do arquivo)",
    "capital_first_trade_value": 1000.0,
    "capital_fixed_value": 1000.0,
    "capital_pct_entry": 10.0,
    "capital_pct_reapply": True,
    "capital_qty_integer": True,
    "capital_qty_min": 1.0,
    "capital_qty_step": 1.0,
    "pending_trade_sim_idx": None,
    "robot_executable_path": "",
    "robot_executable_path_input": "",
    "robot_csv_auto_path": "",
    "robot_last_launch_ts": 0.0,
    "csv_source_label": "",
}

ROBOT_EXEC_EXTENSIONS = {".exe", ".bat", ".cmd", ".com", ".py"}
CSV_NAME_HINTS = ("trade", "trades", "backtest", "relatorio", "report", "resultado")

TRADE_COL_ALIASES = {
    "ativo": "symbol",
    "symbol": "symbol",
    "ticker": "symbol",
    "direcao": "direction",
    "operacao": "direction",
    "tipodeoperacao": "direction",
    "resoperacao": "pnl",
    "resultado": "pnl",
    "resultadoliquido": "pnl",
    "resultadobruto": "pnl",
    "lucroprejuizo": "pnl",
    "lucroprejuizooperacao": "pnl",
    "pnl": "pnl",
    "entrada": "entry_time",
    "horarioentrada": "entry_time",
    "saida": "exit_time",
    "horariosaida": "exit_time",
    "comentario": "comment",
    "comment": "comment",
    "setup": "setup",
    "setupentrada": "setup_entry",
    "stop": "stop_price",
    "stopinicial": "stop_price",
    "stopprice": "stop_price",
}

def _init_session_defaults() -> None:
    for key, value in SESSION_DEFAULTS.items():
        st.session_state.setdefault(key, value)

def _normalize_token(value: str) -> str:
    norm = unicodedata.normalize("NFKD", str(value or ""))
    ascii_txt = norm.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_txt.lower())

def _smart_rename_trade_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    existing_targets = set(df.columns)
    for col in df.columns:
        target = TRADE_COL_ALIASES.get(_normalize_token(col))
        if target and target not in existing_targets:
            rename_map[col] = target
            existing_targets.add(target)
    if rename_map:
        return df.rename(columns=rename_map)
    return df

def _load_candle_colors() -> dict:
    if os.path.exists(CANDLE_COLORS_FILE):
        try:
            with open(CANDLE_COLORS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}

def _save_candle_colors(colors: dict) -> None:
    try:
        with open(CANDLE_COLORS_FILE, "w", encoding="utf-8") as f:
            json.dump(colors, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _load_robot_prefs() -> dict:
    if os.path.exists(ROBOT_PREFS_FILE):
        try:
            with open(ROBOT_PREFS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}

def _save_robot_prefs(robot_exec_path: str) -> None:
    try:
        payload = {"robot_executable_path": _normalize_local_path(robot_exec_path)}
        with open(ROBOT_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _normalize_local_path(path_value: str) -> str:
    path = str(path_value or "").strip().strip('"').strip("'")
    if not path:
        return ""
    return os.path.normpath(os.path.expanduser(path))

def _pick_robot_executable_dialog() -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        selected = filedialog.askopenfilename(
            title="Selecione o executável do robô",
            filetypes=[
                ("Executáveis/Scripts", "*.exe *.bat *.cmd *.com *.py"),
                ("Todos os arquivos", "*.*"),
            ],
        )
        root.destroy()
        chosen = _normalize_local_path(selected)
        return chosen if chosen else None
    except Exception:
        return None

def _hydrate_robot_prefs_to_session() -> None:
    saved_path = _normalize_local_path(_load_robot_prefs().get("robot_executable_path", ""))
    if not saved_path:
        return
    if not st.session_state.get("robot_executable_path"):
        st.session_state["robot_executable_path"] = saved_path
    if not st.session_state.get("robot_executable_path_input"):
        st.session_state["robot_executable_path_input"] = saved_path

def _find_latest_csv_in_folder(folder_path: str, modified_after: float | None = None) -> str | None:
    folder = _normalize_local_path(folder_path)
    if not folder:
        return None
    base = Path(folder)
    if not base.exists() or not base.is_dir():
        return None

    try:
        csv_files = [p for p in base.iterdir() if p.is_file() and p.suffix.lower() == ".csv"]
    except Exception:
        return None
    if not csv_files:
        return None

    if modified_after:
        recent = [p for p in csv_files if p.stat().st_mtime >= float(modified_after)]
    else:
        recent = []
    base_pool = recent if recent else csv_files

    hinted = [p for p in base_pool if any(h in p.name.lower() for h in CSV_NAME_HINTS)]
    pool = hinted if hinted else base_pool
    latest = max(pool, key=lambda p: p.stat().st_mtime)
    return str(latest.resolve())

def _auto_csv_from_robot_path(robot_path: str, modified_after: float | None = None) -> str | None:
    robo = _normalize_local_path(robot_path)
    if not robo:
        return None
    robot_file = Path(robo)
    if not robot_file.exists():
        return None
    if robot_file.is_dir():
        return _find_latest_csv_in_folder(str(robot_file), modified_after=modified_after)
    return _find_latest_csv_in_folder(str(robot_file.parent), modified_after=modified_after)

def _launch_robot_executable(robot_path: str) -> tuple[bool, str]:
    robo = _normalize_local_path(robot_path)
    if not robo:
        return False, "Selecione o executável do robô primeiro."
    robot_file = Path(robo)
    if not robot_file.exists() or not robot_file.is_file():
        return False, "Executável do robô não encontrado no caminho informado."

    suffix = robot_file.suffix.lower()
    if suffix not in ROBOT_EXEC_EXTENSIONS:
        return False, "Formato não suportado. Use .exe, .bat, .cmd, .com ou .py."

    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    cwd = str(robot_file.parent)
    try:
        if suffix in {".bat", ".cmd"}:
            subprocess.Popen(["cmd", "/c", str(robot_file)], cwd=cwd, creationflags=creationflags)
        elif suffix == ".py":
            subprocess.Popen([sys.executable, str(robot_file)], cwd=cwd, creationflags=creationflags)
        else:
            subprocess.Popen([str(robot_file)], cwd=cwd, creationflags=creationflags)
        return True, f"Robô iniciado: {robot_file.name}"
    except Exception as e:
        return False, f"Falha ao iniciar o robô: {e}"

# =============================== Utils ===============================

def br_money(x):
    try:
        return ("R$ " + f"{float(x):,.2f}").replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "R$ 0,00"

def pct(x):
    try:
        return f"{100*float(x):,.2f}%".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "0,00%"

def format_days_hours(days_float: float) -> str:
    if days_float is None or (isinstance(days_float, float) and np.isnan(days_float)):
        return "0d 0h"
    total_hours = int(round(float(days_float) * 24))
    d = total_hours // 24
    h = total_hours % 24
    return f"{d}d {h}h"

def _to_naive_series(s: pd.Series) -> pd.Series:
    """Converte Series de datas para datetime naive (sem tz), tolerando misto tz-aware/naive."""
    x = pd.to_datetime(s, errors="coerce")
    try:
        if pd.api.types.is_datetime64tz_dtype(x.dtype):
            return x.dt.tz_convert("UTC").dt.tz_localize(None)
        if getattr(x.dt, "tz", None) is not None:
            return x.dt.tz_localize(None)
    except Exception:
        pass
    return x

def _tz_naive_df(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    for c in d.columns:
        try:
            if pd.api.types.is_datetime64_any_dtype(d[c].dtype):
                d[c] = pd.to_datetime(d[c], errors="coerce")
                if pd.api.types.is_datetime64tz_dtype(d[c].dtype):
                    d[c] = d[c].dt.tz_convert("UTC").dt.tz_localize(None)
                elif getattr(d[c].dt, "tz", None) is not None:
                    d[c] = d[c].dt.tz_localize(None)
        except Exception:
            try:
                d[c] = pd.to_datetime(d[c], errors="coerce")
                d[c] = d[c].dt.tz_localize(None)
            except Exception:
                pass
    return d

def _drawdown_stats_from_equity(eq_values):
    peak = -np.inf; max_dd_r = 0.0; max_dd_pct = 0.0; dd_len = 0; cur_len = 0
    for v in eq_values:
        if v > peak:
            peak = v; cur_len = 0
        dd_r = peak - v
        dd_p = (dd_r/peak) if peak not in (0, np.inf, -np.inf) else 0.0
        if dd_r > max_dd_r:
            max_dd_r = dd_r; max_dd_pct = dd_p; dd_len = max(dd_len, cur_len + 1)
        if v < peak:
            cur_len += 1
        else:
            cur_len = 0
    return float(max_dd_r), float(max_dd_pct), int(dd_len)

def _max_underwater_run(equity_values, baseline=0.0):
    peak = float(baseline)
    best = 0
    cur = 0
    for raw_v in equity_values:
        try:
            v = float(raw_v)
        except Exception:
            continue
        if not np.isfinite(v):
            continue
        if v >= peak:
            peak = v
            cur = 0
        else:
            cur += 1
            best = max(best, cur)
    return int(best)

def _longest_run(signs, want=1):
    best = 0; cur = 0
    for s in signs:
        if s == want:
            cur += 1; best = max(best, cur)
        else:
            cur = 0
    return best

def _percentage_time_in_market(df, entry_col="entry_time", exit_col="exit_time"):
    if df is None or df.empty or entry_col not in df.columns or exit_col not in df.columns:
        return 0.0
    entries = pd.to_datetime(df[entry_col], errors="coerce")
    exits   = pd.to_datetime(df[exit_col], errors="coerce")
    spans = []
    for s, e in zip(entries, exits):
        if pd.isna(s) or pd.isna(e):
            continue
        if e < s:
            s, e = e, s
        spans.append((s.normalize(), e.normalize()))
    if not spans:
        return 0.0
    spans.sort(key=lambda x: x[0])
    merged = []
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e + pd.Timedelta(days=1):
            if e > cur_e:
                cur_e = e
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    in_days = sum((e - s).days + 1 for s, e in merged)
    global_start = min(s for s,_ in merged)
    global_end   = max(e for _,e in merged)
    total_days = (global_end - global_start).days + 1
    return (in_days / total_days) if total_days>0 else 0.0

# ============== Parser de setup (compatível com o robô) ==============
_SETUP_KEYWORDS = ["123","GAP","DESFARCE","TRAP1","BVI","MACD"]

def parse_setup_from_comment(comment: str) -> str:
    if not comment:
        return ""
    s = str(comment).upper()
    # 1) prioriza padrão do robô: "ENTRY-<SETUP>"
    m = re.search(r"\bENTRY[\-\_\s]*([A-Z0-9_]+)\b", s, flags=re.IGNORECASE)
    if m:
        cand = re.sub(r"[^A-Z0-9_]", "", m.group(1).upper())
        if cand in _SETUP_KEYWORDS:
            return cand
    # 2) fallback: qualquer menção clara de keyword
    for k in _SETUP_KEYWORDS:
        if re.search(rf"\b{k}\b", s):
            return k
    # 3) fallback: nada
    return ""

# =============================== Custos ===============================

def apply_costs(df, b3_pct=0.0, brok_in_fixed=0.0, brok_out_fixed=0.0, base_pct="notional"):
    dff = df.copy()
    dff["costs"] = 0.0
    have_prices = all(c in dff.columns for c in ["price_open","price_close","volume"])
    if have_prices and base_pct == "notional":
        cs = dff.get("contract_size", 1.0)
        try:
            cs = pd.to_numeric(cs, errors="coerce").fillna(1.0)
        except Exception:
            cs = 1.0
        buy_val  = (dff["price_open"]  * dff["volume"] * cs).abs()
        sell_val = (dff["price_close"] * dff["volume"] * cs).abs()
        fees_pct = (buy_val + sell_val) * float(b3_pct)
        fees_fix = float(brok_in_fixed) + float(brok_out_fixed)
        dff["costs"] = fees_pct + fees_fix
    else:
        base = dff.get("pnl", dff.get("profit", 0.0)).abs()
        dff["costs"] = base * float(b3_pct) + (float(brok_in_fixed) + float(brok_out_fixed))

    if "pnl" in dff.columns:
        dff["pnl_net"] = dff["pnl"] - dff["costs"]
    elif "profit" in dff.columns:
        dff["pnl_net"] = dff["profit"] - dff["costs"]
    else:
        dff["pnl_net"] = -dff["costs"]
    return dff

def _prepare_mt5_faithful_trades(df: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    d = df.copy()
    if d.empty:
        return d

    if "pnl_mt5" in d.columns:
        pnl_series = pd.to_numeric(d["pnl_mt5"], errors="coerce").fillna(0.0)
    elif any(c in d.columns for c in ["mt5_profit", "mt5_commission", "mt5_swap", "mt5_fee"]):
        pnl_series = (
            _num_series(d, "mt5_profit", 0.0)
            + _num_series(d, "mt5_commission", 0.0)
            + _num_series(d, "mt5_swap", 0.0)
            + _num_series(d, "mt5_fee", 0.0)
        )
    elif "profit" in d.columns:
        pnl_series = _num_series(d, "profit", 0.0)
    elif "pnl" in d.columns:
        pnl_series = _num_series(d, "pnl", 0.0)
    else:
        pnl_series = pd.Series(np.zeros(len(d), dtype=float), index=d.index, dtype=float)

    mt5_adjustments = (
        _num_series(d, "mt5_commission", 0.0)
        + _num_series(d, "mt5_swap", 0.0)
        + _num_series(d, "mt5_fee", 0.0)
    )
    d["costs"] = (-mt5_adjustments).clip(lower=0.0)
    d["pnl"] = pnl_series
    d["profit"] = pnl_series
    d["pnl_net"] = pnl_series
    d["pnl_used"] = pnl_series
    return _annotate_capital_path(d, initial_capital=initial_capital)

# =============================== Métricas ===============================

def compute_stats(sim_df):
    dff = sim_df.copy()
    n = len(dff)
    pnl = dff["pnl_used"].astype(float).values if "pnl_used" in dff.columns else np.array([])
    wins = pnl[pnl > 0]; losses = pnl[pnl < 0]
    win_rate = (len(wins)/n) if n>0 else np.nan
    avg_win = np.mean(wins) if wins.size else np.nan
    avg_loss = np.mean(losses) if losses.size else np.nan
    payoff = (avg_win/abs(avg_loss)) if (losses.size and avg_loss!=0 and not np.isnan(avg_loss)) else np.nan
    expectancy = (win_rate)*avg_win + (1 - (win_rate if not np.isnan(win_rate) else 0.0))*(avg_loss if not np.isnan(avg_loss) else 0.0) if n>0 else np.nan

    # holding em dias (float)
    avg_hold = float(np.nanmean(dff["holding_days"])) if "holding_days" in dff.columns and dff["holding_days"].notna().any() else np.nan

    # curva
    eq = dff["equity"].astype(float).values if "equity" in dff.columns else np.array([])
    if eq.size: max_dd_r, max_dd_pct, dd_len = _drawdown_stats_from_equity(eq)
    else: max_dd_r, max_dd_pct, dd_len = np.nan, np.nan, np.nan

    # sequências por trade
    best_trade = float(np.max(pnl)) if pnl.size else np.nan
    worst_trade = float(np.min(pnl)) if pnl.size else np.nan
    signs = [1 if x>0 else (-1 if x<0 else 0) for x in pnl]
    max_wins = int(_longest_run(signs, 1)) if pnl.size else np.nan
    max_losses = int(_longest_run(signs, -1)) if pnl.size else np.nan

    # por mês (usar sort_time NAIVE)
    media_mensal = 0.0; total_meses = 0; pct_meses_pos = 0.0
    max_pos_month_streak = 0; max_neg_month_streak = 0
    max_underwater_months = 0
    trades_por_mes = 0.0
    bym = pd.DataFrame(columns=["year_month", "pnl_used"])
    if "sort_time" in dff.columns and dff["sort_time"].notna().any():
        m = dff.copy()
        st_naive = _to_naive_series(m["sort_time"])
        m["year_month"] = st_naive.dt.to_period("M").astype(str)
        bym = m.groupby("year_month", as_index=False)["pnl_used"].sum()
        total_meses = len(bym)
        if total_meses > 0:
            media_mensal = float(bym["pnl_used"].mean())
            pct_meses_pos = float((bym["pnl_used"] > 0).sum() / total_meses)
            month_signs = [1 if v>0 else (-1 if v<0 else 0) for v in bym["pnl_used"].tolist()]
            max_pos_month_streak = _longest_run(month_signs, 1)
            max_neg_month_streak = _longest_run(month_signs, -1)
            trades_por_m = m.groupby("year_month", as_index=False)["pnl_used"].count()
            trades_por_mes = float(trades_por_m["pnl_used"].mean()) if not trades_por_m.empty else 0.0
            monthly_equity = bym["pnl_used"].astype(float).cumsum()
            max_underwater_months = _max_underwater_run(monthly_equity.values, baseline=0.0)

    media_mes_positivo = float(bym["pnl_used"][bym["pnl_used"]>0].mean()) if total_meses > 0 and (bym["pnl_used"]>0).any() else 0.0
    media_mes_negativo = float(bym["pnl_used"][bym["pnl_used"]<0].mean()) if total_meses > 0 and (bym["pnl_used"]<0).any() else 0.0
    return dict(
        trades=int(n),
        pnl_sum=float(np.nansum(pnl)) if pnl.size else 0.0,
        pnl_mean=float(np.nanmean(pnl)) if pnl.size else 0.0,
        win_rate=float(win_rate) if not np.isnan(win_rate) else 0.0,
        avg_win=float(avg_win) if not np.isnan(avg_win) else 0.0,
        avg_loss=float(avg_loss) if not np.isnan(avg_loss) else 0.0,
        payoff=float(payoff) if not np.isnan(payoff) else 0.0,
        expectancy=float(expectancy) if not np.isnan(expectancy) else 0.0,
        avg_holding_days=float(avg_hold) if not np.isnan(avg_hold) else 0.0,
        max_drawdown=float(max_dd_r) if not np.isnan(max_dd_r) else 0.0,
        max_drawdown_pct=float(max_dd_pct) if not np.isnan(max_dd_pct) else 0.0,
        dd_len=int(dd_len) if not np.isnan(dd_len) else 0,
        best_trade=float(best_trade) if not np.isnan(best_trade) else 0.0,
        worst_trade=float(worst_trade) if not np.isnan(worst_trade) else 0.0,
        max_consecutive_wins=int(max_wins) if not np.isnan(max_wins) else 0,
        max_consecutive_losses=int(max_losses) if not np.isnan(max_losses) else 0,
        media_mensal=float(media_mensal),
        total_meses=int(total_meses),
        pct_meses_positivos=float(pct_meses_pos),
        max_pos_month_streak=int(max_pos_month_streak),
        max_neg_month_streak=int(max_neg_month_streak),
        max_underwater_months=int(max_underwater_months),
        trades_med_por_mes=float(trades_por_mes),
        media_mes_positivo=media_mes_positivo,
        media_mes_negativo=media_mes_negativo
    )

def simulate_equity(trades, initial_capital=0.0):
    dff = trades.sort_values("sort_time").copy()
    eq = float(initial_capital); equities=[]
    for v in dff["pnl_used"].astype(float).fillna(0.0).values:
        eq += v
        equities.append(eq)
    dff["equity"] = equities
    return dff

CAPITAL_SCENARIO_OPTIONS = [
    "Original (volume do arquivo)",
    "Entrada fixa por trade (R$)",
    "Composto pelo remanescente do trade",
    "% do capital por trade",
]

def _to_num(v, default=0.0) -> float:
    try:
        n = float(v)
        return n if np.isfinite(n) else float(default)
    except Exception:
        return float(default)

def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    return pd.Series(np.full(len(df), float(default)), index=df.index, dtype=float)

def _quantize_qty(raw_qty: float, min_qty: float, step_qty: float, integer_qty: bool) -> float:
    q = max(_to_num(raw_qty, 0.0), 0.0)
    mn = max(_to_num(min_qty, 0.0), 0.0)
    stp = max(_to_num(step_qty, 1.0), 1e-9)
    if integer_qty:
        mn = float(max(int(round(mn)), 1))
        stp = float(max(int(round(stp)), 1))
    if q < mn:
        return 0.0
    steps = np.floor((q - mn) / stp)
    qty = mn + (steps * stp)
    if integer_qty:
        qty = float(int(np.floor(qty)))
    return max(_to_num(qty, 0.0), 0.0)

def _annotate_capital_path(df: pd.DataFrame, initial_capital: float) -> pd.DataFrame:
    if "sort_time" in df.columns:
        d = df.sort_values("sort_time").copy().reset_index(drop=True)
    else:
        d = df.copy().reset_index(drop=True)
    if "pnl_used" not in d.columns:
        d["pnl_used"] = 0.0
    pnl_series = pd.to_numeric(d["pnl_used"], errors="coerce").fillna(0.0)
    cap_before = float(initial_capital) + pnl_series.shift(fill_value=0.0).cumsum()
    cap_after = cap_before + pnl_series
    d["capital_before"] = cap_before
    d["capital_after"] = cap_after
    if {"price_open", "volume"}.issubset(d.columns):
        cs = _num_series(d, "contract_size", 1.0)
        px_open = _num_series(d, "price_open", 0.0).abs()
        vol = _num_series(d, "volume", 0.0).abs()
        d["capital_alocado"] = (px_open * vol * cs).fillna(0.0)
    else:
        d["capital_alocado"] = 0.0
    d["capital_target"] = d["capital_alocado"]
    d["qtd_acoes"] = _num_series(d, "volume", 0.0).abs()
    d["size_factor"] = 1.0
    d["scenario_executed"] = True
    d["scenario_skipped_reason"] = ""
    return d

def simulate_capital_scenario(
    trades_df: pd.DataFrame,
    *,
    initial_capital: float,
    scenario_mode: str,
    first_trade_value: float,
    fixed_trade_value: float,
    pct_entry: float,
    pct_reapply: bool,
    qty_integer: bool,
    qty_min: float,
    qty_step: float,
    b3_pct: float,
    brok_in_fixed: float,
    brok_out_fixed: float,
    base_pct: str,
) -> tuple[pd.DataFrame, dict]:
    if "sort_time" in trades_df.columns:
        d = trades_df.sort_values("sort_time").copy().reset_index(drop=True)
    else:
        d = trades_df.copy().reset_index(drop=True)
    if d.empty:
        return d, {"rows_total": 0, "executed": 0, "skipped": 0, "skipped_no_price": 0}

    if "pnl" in d.columns:
        base_pnl = pd.to_numeric(d["pnl"], errors="coerce").fillna(0.0)
    elif "profit" in d.columns:
        base_pnl = pd.to_numeric(d["profit"], errors="coerce").fillna(0.0)
    elif "pnl_used" in d.columns:
        base_pnl = pd.to_numeric(d["pnl_used"], errors="coerce").fillna(0.0)
    elif "pnl_net" in d.columns:
        base_pnl = pd.to_numeric(d["pnl_net"], errors="coerce").fillna(0.0)
    else:
        base_pnl = pd.Series(np.zeros(len(d)), index=d.index, dtype=float)

    vol_ref_series = _num_series(d, "volume", 1.0).abs()
    vol_ref_series = vol_ref_series.where(vol_ref_series > 0, 1.0)
    px_open_series = _num_series(d, "price_open", np.nan)
    px_close_series = _num_series(d, "price_close", np.nan)
    cs_series = _num_series(d, "contract_size", 1.0).abs()
    cs_series = cs_series.where(cs_series > 0, 1.0)

    cap_before_arr = []
    cap_after_arr = []
    target_arr = []
    alloc_arr = []
    qty_arr = []
    factor_arr = []
    gross_arr = []
    costs_arr = []
    net_arr = []
    executed_arr = []
    skip_reason_arr = []

    cur_capital = float(initial_capital)
    rolling_alloc = max(_to_num(first_trade_value, 0.0), 0.0)
    fees_fix = _to_num(brok_in_fixed, 0.0) + _to_num(brok_out_fixed, 0.0)
    pct_ratio = max(_to_num(pct_entry, 0.0), 0.0) / 100.0

    for i in range(len(d)):
        cap_before = float(cur_capital)
        available_cap = max(cap_before, 0.0)

        base_pnl_i = _to_num(base_pnl.iat[i], 0.0)
        vol_ref_i = _to_num(vol_ref_series.iat[i], 1.0)
        px_open_i = _to_num(px_open_series.iat[i], np.nan)
        px_close_i = _to_num(px_close_series.iat[i], np.nan)
        cs_i = _to_num(cs_series.iat[i], 1.0)
        has_valid_price = np.isfinite(px_open_i) and px_open_i > 0 and np.isfinite(cs_i) and cs_i > 0

        if scenario_mode == "Entrada fixa por trade (R$)":
            target_cap = max(_to_num(fixed_trade_value, 0.0), 0.0)
        elif scenario_mode == "Composto pelo remanescente do trade":
            target_cap = max(rolling_alloc, 0.0)
        elif scenario_mode == "% do capital por trade":
            base_for_pct = cap_before if pct_reapply else float(initial_capital)
            target_cap = max(base_for_pct * pct_ratio, 0.0)
        else:
            target_cap = np.nan

        if scenario_mode == "Original (volume do arquivo)":
            qty = max(vol_ref_i, 0.0)
            alloc_cap = (abs(px_open_i) * qty * cs_i) if has_valid_price else 0.0
            size_factor = (qty / vol_ref_i) if vol_ref_i > 0 else 0.0
            skip_reason = ""
        else:
            target_cap = min(_to_num(target_cap, 0.0), available_cap)
            if has_valid_price:
                notional_per_unit = abs(px_open_i) * cs_i
                raw_qty = (target_cap / notional_per_unit) if notional_per_unit > 0 else 0.0
                qty = _quantize_qty(raw_qty, qty_min, qty_step, qty_integer)
                alloc_cap = qty * notional_per_unit
                size_factor = (qty / vol_ref_i) if vol_ref_i > 0 else 0.0
                skip_reason = ""
            else:
                qty = 0.0
                alloc_cap = 0.0
                size_factor = 0.0
                skip_reason = "Sem preço de entrada válido para ajustar quantidade."

        executed = qty > 0
        if not executed:
            gross = 0.0
            costs = 0.0
            net = 0.0
            cap_after = cap_before
            if not skip_reason:
                skip_reason = "Capital insuficiente para abrir posição."
        else:
            gross = base_pnl_i * size_factor
            if base_pct == "notional" and np.isfinite(px_open_i) and px_open_i > 0 and np.isfinite(px_close_i) and px_close_i > 0:
                buy_val = abs(px_open_i * qty * cs_i)
                sell_val = abs(px_close_i * qty * cs_i)
                fees_pct = (buy_val + sell_val) * _to_num(b3_pct, 0.0)
            else:
                fees_pct = abs(gross) * _to_num(b3_pct, 0.0)
            costs = fees_pct + fees_fix
            net = gross - costs
            cap_after = cap_before + net

        if scenario_mode == "Composto pelo remanescente do trade":
            rolling_alloc = max(0.0, alloc_cap + net) if executed else 0.0

        cap_before_arr.append(cap_before)
        cap_after_arr.append(cap_after)
        target_arr.append(_to_num(target_cap, 0.0) if np.isfinite(_to_num(target_cap, np.nan)) else 0.0)
        alloc_arr.append(alloc_cap)
        qty_arr.append(qty)
        factor_arr.append(size_factor)
        gross_arr.append(gross)
        costs_arr.append(costs)
        net_arr.append(net)
        executed_arr.append(bool(executed))
        skip_reason_arr.append(skip_reason)

        cur_capital = cap_after

    d["capital_before"] = cap_before_arr
    d["capital_after"] = cap_after_arr
    d["capital_target"] = target_arr
    d["capital_alocado"] = alloc_arr
    d["qtd_acoes"] = qty_arr
    d["size_factor"] = factor_arr
    d["pnl_gross_scenario"] = gross_arr
    d["costs_scenario"] = costs_arr
    d["pnl_net_scenario"] = net_arr
    d["scenario_executed"] = executed_arr
    d["scenario_skipped_reason"] = skip_reason_arr

    d_exec = d[d["scenario_executed"]].copy().reset_index(drop=True)
    if d_exec.empty:
        meta = {
            "rows_total": int(len(d)),
            "executed": 0,
            "skipped": int((~d["scenario_executed"]).sum()),
            "skipped_no_price": int((~d["scenario_executed"] & d["scenario_skipped_reason"].str.contains("Sem preço", na=False)).sum()),
        }
        return d_exec, meta

    d_exec["volume"] = d_exec["qtd_acoes"]
    d_exec["pnl"] = d_exec["pnl_gross_scenario"]
    d_exec["costs"] = d_exec["costs_scenario"]
    d_exec["pnl_net"] = d_exec["pnl_net_scenario"]
    d_exec["pnl_used"] = d_exec["pnl_net_scenario"]

    meta = {
        "rows_total": int(len(d)),
        "executed": int(len(d_exec)),
        "skipped": int((~d["scenario_executed"]).sum()),
        "skipped_no_price": int((~d["scenario_executed"] & d["scenario_skipped_reason"].str.contains("Sem preço", na=False)).sum()),
    }
    return d_exec, meta

# --------- Limite de Trades Simultâneos ---------
def _prepare_intervals_for_concurrency(df: pd.DataFrame):
    ent = pd.to_datetime(df.get("entry_time", pd.Series([pd.NaT]*len(df))), errors="coerce")
    ext = pd.to_datetime(df.get("exit_time", pd.Series([pd.NaT]*len(df))), errors="coerce")
    stt = pd.to_datetime(df.get("sort_time", pd.Series([pd.NaT]*len(df))), errors="coerce")

    start = ent.copy(); end = ext.copy()
    mask_no_entry_yes_exit = start.isna() & end.notna()
    start[mask_no_entry_yes_exit] = end[mask_no_entry_yes_exit]
    mask_yes_entry_no_exit = start.notna() & end.isna()
    end[mask_yes_entry_no_exit] = start[mask_yes_entry_no_exit]
    mask_none = start.isna() & end.isna()
    start[mask_none] = stt[mask_none]; end[mask_none] = stt[mask_none]
    swap = start > end
    tmp = start[swap].copy(); start[swap] = end[swap]; end[swap] = tmp
    return start, end

def apply_max_concurrent_limit(df: pd.DataFrame, max_open: int) -> pd.DataFrame:
    if max_open is None or int(max_open) <= 0 or df.empty:
        return df.copy()
    d = df.copy().reset_index(drop=True)
    start, end = _prepare_intervals_for_concurrency(d)
    d["_start"] = start; d["_end"] = end
    order = np.lexsort([d["_end"].values.astype("datetime64[ns]"), d["_start"].values.astype("datetime64[ns]")])
    idxs = list(order)
    active = []; included = np.zeros(len(d), dtype=bool)
    for i in idxs:
        s = d.loc[i, "_start"]; e = d.loc[i, "_end"]
        active = [(te, ti) for (te, ti) in active if (pd.isna(te) or te >= s)]
        if len(active) < max_open:
            included[i] = True
            active.append((e, i))
            active.sort(key=lambda tup: (pd.Timestamp.max if pd.isna(tup[0]) else tup[0]))
        else:
            included[i] = False
    out = d[included].drop(columns=["_start","_end"])
    out = out.sort_values("sort_time").reset_index(drop=True)
    return out

# =============================== Leitura de dados ===============================

@st.cache_data(show_spinner=False)
def _cached_read_csv_bytes(raw_bytes, sep=None, decimal=None, encoding=None):
    return pd.read_csv(io.BytesIO(raw_bytes), sep=sep, decimal=decimal, encoding=encoding)

def _read_csv_source_bytes(source) -> bytes:
    if isinstance(source, (str, os.PathLike)):
        with open(str(source), "rb") as f:
            return f.read()

    if hasattr(source, "seek"):
        try:
            source.seek(0)
        except Exception:
            pass

    raw = source.read()
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="ignore")
    return raw

def _try_read_csv(source) -> pd.DataFrame:
    raw = _read_csv_source_bytes(source)
    tried = []
    for enc in ["utf-8-sig", "latin1"]:
        for sep in [",", ";", "\t", "|"]:
            for dec in [".", ","]:
                key = (enc, sep, dec)
                if key in tried:
                    continue
                tried.append(key)
                try:
                    df = _cached_read_csv_bytes(raw, sep=sep, decimal=dec, encoding=enc)
                    if df.shape[1] >= 2:
                        return df
                except Exception:
                    pass
    return _cached_read_csv_bytes(raw)

def read_from_csv(source):
    df = _try_read_csv(source)

    # Renomeia colunas de forma robusta (ignora acentos, caixa e separadores)
    df = _smart_rename_trade_columns(df)

    # Parser de setup
    if "setup" in df.columns:
        base = df["setup"].astype(str)
    elif "setup_entry" in df.columns:
        base = df["setup_entry"].astype(str)
    elif "comment" in df.columns:
        base = df["comment"].astype(str)
    else:
        base = pd.Series([""]*len(df))
    df["setup"] = base.map(parse_setup_from_comment).fillna("")

    # Datas → NAIVE
    for c in ["entry_time","exit_time","sort_time"]:
        if c in df.columns:
            df[c] = _to_naive_series(df[c])

    if "sort_time" not in df.columns:
        if "exit_time" in df.columns and df["exit_time"].notna().any():
            df["sort_time"] = _to_naive_series(df["exit_time"])
        elif "entry_time" in df.columns:
            df["sort_time"] = _to_naive_series(df["entry_time"])

    # holding_days
    if {"entry_time","exit_time"}.issubset(df.columns):
        dt_e = pd.to_datetime(df["entry_time"], errors="coerce")
        dt_s = pd.to_datetime(df["exit_time"], errors="coerce")
        df["holding_days"] = (dt_s - dt_e).dt.total_seconds() / 86400.0

    return df

def read_from_mt5(magic, date_from, date_to):
    if not HAS_MT5:
        raise RuntimeError("MetaTrader5 não está instalado no ambiente.")
    if not mt5.initialize():
        raise RuntimeError(f"Falha ao inicializar MT5: {mt5.last_error()}")

    d0 = datetime(date_from.year, date_from.month, date_from.day, 0, 0, 0, tzinfo=timezone.utc)
    d1 = datetime(date_to.year,   date_to.month,   date_to.day,   23,59,59, tzinfo=timezone.utc)

    deals = mt5.history_deals_get(d0, d1)
    orders = mt5.history_orders_get(d0, d1)
    if deals is None:
        mt5.shutdown()
        raise RuntimeError(f"Sem negócios no período. {mt5.last_error()}")

    # Mapa: order_ticket -> setup (prioriza ENTRY-<SETUP>)
    order_setup = {}
    if orders:
        for o in orders:
            try:
                if magic and getattr(o, "magic", 0) != magic:
                    continue
                oc = str(getattr(o, "comment", "") or "")
                s = parse_setup_from_comment(oc)
                if s:
                    order_setup[getattr(o, "ticket", None)] = s
            except Exception:
                pass

    # Coleta deals do MAGIC
    rows = []
    for d in deals or []:
        try:
            if magic and getattr(d, "magic", 0) != magic:
                continue
            rows.append(dict(
                time=datetime.fromtimestamp(d.time, tz=timezone.utc),
                symbol=d.symbol,
                type=int(getattr(d, "type", 0)),  # 0/2 buy; 1/3 sell
                entry=int(getattr(d, "entry", -1)),  # IN/OUT/INOUT
                price=float(getattr(d, "price", 0.0)),
                volume=float(getattr(d, "volume", 0.0)),
                order=int(getattr(d, "order", 0)),
                ticket=int(getattr(d, "ticket", 0)),
                position_id=int(getattr(d, "position_id", 0) or 0),
                comment=str(getattr(d, "comment", "") or ""),
                deal_profit=float(getattr(d, "profit", 0.0) or 0.0),
                deal_commission=float(getattr(d, "commission", 0.0) or 0.0),
                deal_swap=float(getattr(d, "swap", 0.0) or 0.0),
                deal_fee=float(getattr(d, "fee", 0.0) or 0.0),
            ))
        except Exception:
            pass

    mt5.shutdown()

    if not rows:
        return pd.DataFrame()

    dd = pd.DataFrame(rows).sort_values(["time", "ticket"]).reset_index(drop=True)
    dd["side"] = np.where(dd["type"].isin([0,2]), "BUY", "SELL")

    # Pareamento por posição/símbolo só para referência de entrada/saída.
    # O P&L usado no relatório é o realizado do próprio MT5 (deal profit/commission/swap/fee).
    trades = []
    eps = 1e-12
    open_longs: dict[tuple[str, int], list[dict]] = defaultdict(list)
    open_shorts: dict[tuple[str, int], list[dict]] = defaultdict(list)

    def _consume(queue: list[dict], qty: float) -> tuple[list[tuple[dict, float]], float]:
        parts: list[tuple[dict, float]] = []
        rem = float(qty)
        while rem > eps and queue:
            lot = queue[0]
            lot_vol = float(lot["volume"])
            take = min(lot_vol, rem)
            parts.append((lot, take))
            lot_vol -= take
            rem -= take
            if lot_vol <= eps:
                queue.pop(0)
            else:
                lot["volume"] = lot_vol
        return parts, rem

    for _, r in dd.iterrows():
        sym = str(r["symbol"])
        pos_id_raw = int(r.get("position_id", 0) or 0)
        pos_key = pos_id_raw if pos_id_raw > 0 else 0
        key = (sym, pos_key)

        close_parts: list[tuple[dict, float, str]] = []
        rem = float(r["volume"])
        if rem <= eps:
            continue

        if r["side"] == "BUY":
            consumed, rem = _consume(open_shorts[key], rem)
            close_parts.extend([(lot, take, "SHORT") for lot, take in consumed])
            if rem > eps:
                open_longs[key].append(
                    {
                        "time": r["time"],
                        "price": float(r["price"]),
                        "volume": rem,
                        "order": int(r["order"]),
                        "ticket": int(r["ticket"]),
                        "comment": str(r["comment"] or ""),
                    }
                )
        else:
            consumed, rem = _consume(open_longs[key], rem)
            close_parts.extend([(lot, take, "LONG") for lot, take in consumed])
            if rem > eps:
                open_shorts[key].append(
                    {
                        "time": r["time"],
                        "price": float(r["price"]),
                        "volume": rem,
                        "order": int(r["order"]),
                        "ticket": int(r["ticket"]),
                        "comment": str(r["comment"] or ""),
                    }
                )

        matched_vol = float(sum(take for _, take, _ in close_parts))
        is_close_like = int(r.get("entry", -1)) in {1, 2, 3}

        # Se não foi possível parear, mas o deal é de saída, cria trade sintético
        # para não perder o P&L realizado do MT5 (ex.: abertura fora do período filtrado).
        if matched_vol <= eps and is_close_like:
            close_parts = [
                (
                    {
                        "time": pd.NaT,
                        "price": np.nan,
                        "volume": float(r["volume"]),
                        "order": 0,
                        "ticket": 0,
                        "comment": "",
                    },
                    float(r["volume"]),
                    "LONG" if r["side"] == "SELL" else "SHORT",
                )
            ]
            matched_vol = float(r["volume"])

        if matched_vol <= eps:
            continue

        deal_profit = float(r.get("deal_profit", 0.0) or 0.0)
        deal_comm = float(r.get("deal_commission", 0.0) or 0.0)
        deal_swap = float(r.get("deal_swap", 0.0) or 0.0)
        deal_fee = float(r.get("deal_fee", 0.0) or 0.0)
        deal_total = deal_profit + deal_comm + deal_swap + deal_fee

        for lot, take, direction in close_parts:
            frac = float(take / matched_vol) if matched_vol > eps else 0.0
            setup = (
                order_setup.get(lot.get("order", None), "")
                or parse_setup_from_comment(lot.get("comment", ""))
                or parse_setup_from_comment(r["comment"])
            )
            entry_px = float(lot.get("price", np.nan))
            exit_px = float(r["price"])
            if direction == "SHORT":
                pnl_theoretical = (entry_px - exit_px) * float(take)
            else:
                pnl_theoretical = (exit_px - entry_px) * float(take)

            trades.append(
                dict(
                    symbol=sym,
                    direction=direction,
                    entry_time=lot.get("time", pd.NaT),
                    exit_time=r["time"],
                    price_open=entry_px,
                    price_close=exit_px,
                    volume=float(take),
                    setup=setup,
                    comment_entry=str(lot.get("comment", "") or ""),
                    comment_exit=str(r["comment"] or ""),
                    order_entry=int(lot.get("order", 0) or 0),
                    order_exit=int(r["order"]),
                    ticket=int(r["ticket"]),
                    position_id=pos_id_raw if pos_id_raw > 0 else np.nan,
                    pnl_theoretical=float(pnl_theoretical) if np.isfinite(pnl_theoretical) else np.nan,
                    mt5_profit=deal_profit * frac,
                    mt5_commission=deal_comm * frac,
                    mt5_swap=deal_swap * frac,
                    mt5_fee=deal_fee * frac,
                    pnl_mt5=deal_total * frac,
                )
            )

    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    if df.empty:
        return df

    # sort_time e holding_days (timezone-aware -> tz local SP para exibição; guardamos NAIVE depois)
    df["sort_time"] = pd.to_datetime(df["exit_time"], utc=True).dt.tz_convert("America/Sao_Paulo")
    df["holding_days"] = (pd.to_datetime(df["exit_time"], utc=True) - pd.to_datetime(df["entry_time"], utc=True)).dt.total_seconds()/86400.0

    # P&L canônico do MT5 (realizado por deal): profit + commission + swap + fee
    df["pnl"] = pd.to_numeric(df["pnl_mt5"], errors="coerce").fillna(0.0)
    df["profit"] = df["pnl"]
    df["pnl_net"] = df["pnl"]
    df["costs"] = 0.0
    # Remover tz para o restante do app
    df = _tz_naive_df(df)
    return df

# =============================== OHLC (MT5 e CSV) ===============================

if HAS_MT5:
    MT5_TF_MAP = {"M15": mt5.TIMEFRAME_M15, "H1":  mt5.TIMEFRAME_H1, "D1":  mt5.TIMEFRAME_D1}
else:
    MT5_TF_MAP = {}

def _tf_to_seconds(tf_str: str) -> int:
    if tf_str == "M15": return 15*60
    if tf_str == "H1":  return 60*60
    return 24*60*60  # D1 padrão

@st.cache_data(show_spinner=False)
def fetch_mt5_ohlc(symbol: str, tf_str: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    if not HAS_MT5:
        return pd.DataFrame()
    if not mt5.initialize():
        return pd.DataFrame()
    tf = MT5_TF_MAP.get(tf_str, mt5.TIMEFRAME_D1)
    rates = mt5.copy_rates_range(symbol, tf, start_dt, end_dt)
    mt5.shutdown()
    if rates is None or len(rates) == 0:
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.rename(columns={"open":"open", "high":"high", "low":"low", "close":"close", "tick_volume":"volume"}, inplace=True)
    return df[["time","open","high","low","close","volume"]]

def _ensure_ohlc_lib():
    if "ohlc_lib" not in st.session_state:
        st.session_state["ohlc_lib"] = {}
    return st.session_state["ohlc_lib"]

def upload_ohlc_csv(symbol_hint=""):
    up = st.file_uploader("OHLC CSV (colunas: time,open,high,low,close,volume)", type=["csv"], key=f"up_ohlc_{symbol_hint}")
    if up:
        df = _try_read_csv(up)
        cols = {c.lower(): c for c in df.columns}
        needed = ["time","open","high","low","close"]
        if not all(n in [k.lower() for k in df.columns] for n in needed):
            st.error("CSV deve conter colunas: time,open,high,low,close[,volume]")
            return None
        df = df.rename(columns={cols.get("time","time"): "time",
                                cols.get("open","open"): "open",
                                cols.get("high","high"): "high",
                                cols.get("low","low"): "low",
                                cols.get("close","close"): "close",
                                cols.get("volume","volume"): "volume"})
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time","open","high","low","close"]).sort_values("time")
        return df
    return None

# =============================== App ===============================

st.set_page_config(page_title="Relatório Pós-Trade (MT5/CSV)", layout="wide")

def _inject_dashboard_styles() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Cormorant+Garamond:wght@600;700&display=swap');
        :root {
            --bg-base: #060B16;
            --bg-panel: rgba(11, 18, 32, 0.74);
            --bg-panel-soft: rgba(11, 18, 32, 0.48);
            --border-soft: rgba(148, 163, 184, 0.30);
            --border-strong: rgba(130, 240, 255, 0.36);
            --txt-main: #E6EEFF;
            --txt-soft: #AFC0DA;
            --accent-cyan: #00D4FF;
            --accent-mint: #00F0A4;
            --accent-amber: #F59E0B;
            --font-body: "Manrope", "Segoe UI", "Inter", sans-serif;
            --font-head: "Cormorant Garamond", "Georgia", "Times New Roman", serif;
        }

        html, body, [class*="css"] {
            font-family: var(--font-body) !important;
        }

        .stApp {
            background:
                radial-gradient(1200px 620px at 8% -10%, rgba(0, 212, 255, 0.16), transparent 52%),
                radial-gradient(900px 560px at 92% -12%, rgba(0, 240, 164, 0.10), transparent 48%),
                linear-gradient(180deg, #060B16 0%, #050A14 100%);
            color: var(--txt-main);
        }

        [data-testid="stHeader"] {
            background: transparent;
        }

        [data-testid="stAppViewContainer"] > .main {
            background: transparent;
        }

        [data-testid="stSidebar"] {
            background:
                linear-gradient(180deg, rgba(9, 14, 27, 0.96), rgba(7, 12, 23, 0.96));
            border-right: 1px solid rgba(148, 163, 184, 0.18);
        }

        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] [data-baseweb="select"] {
            color: var(--txt-soft);
        }

        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] p,
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] li,
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] span {
            font-family: var(--font-body);
            letter-spacing: 0.002em;
        }

        [data-testid="stAppViewContainer"] .main h1,
        [data-testid="stAppViewContainer"] .main h2,
        [data-testid="stAppViewContainer"] .main h3 {
            font-family: var(--font-head) !important;
            color: var(--txt-main);
            letter-spacing: 0.012em;
            text-align: center;
            text-wrap: balance;
            margin-left: auto;
            margin-right: auto;
            width: fit-content;
            max-width: 100%;
        }

        /* Centraliza especificamente os títulos gerados por st.markdown("## ...") */
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h1,
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h2,
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h3,
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h4,
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h5,
        [data-testid="stAppViewContainer"] .main [data-testid="stMarkdownContainer"] h6 {
            font-family: var(--font-head) !important;
            width: 100% !important;
            text-align: center !important;
            margin-left: 0 !important;
            margin-right: 0 !important;
            display: block !important;
        }

        [data-testid="stAppViewContainer"] .main [data-testid="stHeadingWithActionElements"] {
            width: 100%;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 0.35rem;
            text-align: center;
        }

        [data-testid="stAppViewContainer"] .main [data-testid="stHeadingWithActionElements"] > h1,
        [data-testid="stAppViewContainer"] .main [data-testid="stHeadingWithActionElements"] > h2,
        [data-testid="stAppViewContainer"] .main [data-testid="stHeadingWithActionElements"] > h3 {
            margin-left: 0;
            margin-right: 0;
        }

        [data-testid="stAppViewContainer"] .main h1 {
            font-weight: 700;
            line-height: 1.08;
            background: linear-gradient(90deg, #EAF4FF 0%, #CFE7FF 45%, #A6D9FF 100%);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
            text-shadow: 0 8px 24px rgba(2, 6, 23, 0.42);
            padding-bottom: 0.2rem;
        }

        [data-testid="stAppViewContainer"] .main h2,
        [data-testid="stAppViewContainer"] .main h3 {
            font-weight: 600;
            color: #DCE8FB;
        }

        [data-testid="stAppViewContainer"] .main h2::after {
            content: "";
            display: block;
            width: 72px;
            height: 2px;
            margin: 8px auto 0;
            border-radius: 99px;
            background: linear-gradient(90deg, rgba(0, 212, 255, 0.15), rgba(0, 212, 255, 0.95), rgba(0, 240, 164, 0.9));
        }

        hr {
            border: none;
            border-top: 1px solid rgba(148, 163, 184, 0.22);
            margin: 0.95rem 0 1.05rem;
        }

        div[data-testid="stMetric"] {
            background: linear-gradient(180deg, var(--bg-panel), var(--bg-panel-soft));
            border: 1px solid var(--border-soft);
            border-radius: 12px;
            padding: 0.60rem 0.72rem;
            box-shadow: 0 8px 20px rgba(2, 6, 23, 0.28);
            transition: transform 0.22s ease, box-shadow 0.26s ease, border-color 0.26s ease;
        }

        div[data-testid="stMetric"]:hover {
            transform: translateY(-2px);
            border-color: rgba(130, 240, 255, 0.66);
            box-shadow:
                0 0 12px rgba(0, 212, 255, 0.30),
                0 0 30px rgba(0, 212, 255, 0.16),
                0 12px 24px rgba(2, 6, 23, 0.38);
        }

        div[data-testid="stMetricLabel"] > div {
            color: #9FB3D2;
            font-size: 0.74rem;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            font-weight: 600;
        }

        div[data-testid="stMetricValue"] {
            color: #ECF3FF;
            font-weight: 800;
            line-height: 1.1;
        }

        div[data-testid="stTabs"] button[role="tab"] {
            border: 1px solid rgba(148, 163, 184, 0.26);
            background: rgba(11, 18, 32, 0.36);
            border-radius: 10px 10px 0 0;
            margin-right: 0.22rem;
            color: #AFBFD8;
            transition: all 0.20s ease;
        }

        div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
            color: #ECF6FF;
            border-color: rgba(0, 212, 255, 0.62);
            box-shadow: inset 0 -2px 0 rgba(0, 212, 255, 0.92);
            background: linear-gradient(180deg, rgba(0, 212, 255, 0.16), rgba(11, 18, 32, 0.46));
        }

        div[data-testid="stTabs"] button[role="tab"]:hover {
            color: #D4E6FF;
            border-color: rgba(0, 212, 255, 0.44);
        }

        [data-testid="stExpander"] {
            border: 1px solid rgba(148, 163, 184, 0.26);
            border-radius: 12px;
            background: linear-gradient(180deg, rgba(11, 18, 32, 0.50), rgba(11, 18, 32, 0.34));
            overflow: hidden;
            transition: transform 0.22s ease, box-shadow 0.26s ease, border-color 0.26s ease;
        }

        [data-testid="stExpander"]:hover {
            transform: translateY(-2px);
            border-color: rgba(130, 240, 255, 0.58);
            box-shadow:
                0 0 10px rgba(0, 212, 255, 0.24),
                0 0 24px rgba(0, 212, 255, 0.12),
                0 10px 18px rgba(2, 6, 23, 0.34);
        }

        [data-testid="stDataFrame"] {
            border: 1px solid rgba(148, 163, 184, 0.22);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 7px 16px rgba(2, 6, 23, 0.24);
            transition: transform 0.22s ease, box-shadow 0.26s ease, border-color 0.26s ease;
        }

        [data-testid="stDataFrame"]:hover {
            transform: translateY(-2px);
            border-color: rgba(130, 240, 255, 0.56);
            box-shadow:
                0 0 10px rgba(0, 212, 255, 0.22),
                0 0 24px rgba(0, 212, 255, 0.11),
                0 10px 18px rgba(2, 6, 23, 0.32);
        }

        .stAlert {
            border-radius: 12px;
            border: 1px solid rgba(148, 163, 184, 0.30);
            transition: transform 0.22s ease, box-shadow 0.26s ease, border-color 0.26s ease;
        }

        .stAlert:hover {
            transform: translateY(-2px);
            border-color: rgba(130, 240, 255, 0.60);
            box-shadow:
                0 0 10px rgba(0, 212, 255, 0.24),
                0 0 24px rgba(0, 212, 255, 0.12),
                0 10px 18px rgba(2, 6, 23, 0.32);
        }

        .jt-report-card {
            position: relative;
            transition: transform 0.24s ease, box-shadow 0.28s ease, filter 0.28s ease;
        }

        .jt-report-card:hover {
            transform: translateY(-2px);
            filter: saturate(1.08);
            box-shadow:
                0 0 12px rgba(0, 212, 255, 0.34),
                0 0 30px rgba(0, 212, 255, 0.18),
                0 12px 24px rgba(2, 6, 23, 0.36) !important;
        }

        .jt-report-card::after {
            content: "";
            position: absolute;
            inset: -1px;
            border-radius: 12px;
            pointer-events: none;
            border: 1px solid rgba(130, 240, 255, 0.0);
            transition: border-color 0.28s ease, box-shadow 0.28s ease;
        }

        .jt-report-card:hover::after {
            border-color: rgba(130, 240, 255, 0.56);
            box-shadow:
                0 0 8px rgba(130, 240, 255, 0.34),
                inset 0 0 12px rgba(0, 212, 255, 0.10);
        }

        :is(
            .stButton>button,
            .stDownloadButton>button,
            .stFormSubmitButton>button,
            div[data-testid="stButton"]>button,
            div[data-testid="stDownloadButton"]>button,
            div[data-testid="stFormSubmitButton"]>button,
            button[data-testid="stBaseButton-primary"],
            button[data-testid="stBaseButton-secondary"],
            button[data-testid="stBaseButton-tertiary"],
            button[kind="primary"],
            button[kind="secondary"],
            button[kind="tertiary"]
        ) {
            position: relative;
            isolation: isolate;
            overflow: hidden;
            border-radius: 10px;
            background: linear-gradient(120deg, rgba(0, 212, 255, 0.18), rgba(0, 240, 164, 0.14));
            color: #eafcff;
            border: 1px solid rgba(130, 240, 255, 0.56);
            padding: 0.48rem 1rem;
            font-weight: 700;
            letter-spacing: 0.02em;
            text-shadow: 0 0 4px rgba(180, 250, 255, 0.42);
            box-shadow:
                0 0 6px rgba(0, 212, 255, 0.28),
                0 0 14px rgba(0, 212, 255, 0.18),
                inset 0 0 8px rgba(0, 212, 255, 0.14);
            transition: transform 0.22s ease, box-shadow 0.22s ease, border-color 0.22s ease;
            animation: neonButtonPulse 3.4s ease-in-out infinite;
        }
        :is(
            .stButton>button,
            .stDownloadButton>button,
            .stFormSubmitButton>button,
            div[data-testid="stButton"]>button,
            div[data-testid="stDownloadButton"]>button,
            div[data-testid="stFormSubmitButton"]>button,
            button[data-testid="stBaseButton-primary"],
            button[data-testid="stBaseButton-secondary"],
            button[data-testid="stBaseButton-tertiary"],
            button[kind="primary"],
            button[kind="secondary"],
            button[kind="tertiary"]
        )::before {
            content: "";
            position: absolute;
            inset: 0;
            z-index: -1;
            opacity: 0.36;
            background: linear-gradient(135deg, rgba(0, 212, 255, 0.20), rgba(0, 240, 164, 0.12));
            filter: blur(7px);
        }
        :is(
            .stButton>button,
            .stDownloadButton>button,
            .stFormSubmitButton>button,
            div[data-testid="stButton"]>button,
            div[data-testid="stDownloadButton"]>button,
            div[data-testid="stFormSubmitButton"]>button,
            button[data-testid="stBaseButton-primary"],
            button[data-testid="stBaseButton-secondary"],
            button[data-testid="stBaseButton-tertiary"],
            button[kind="primary"],
            button[kind="secondary"],
            button[kind="tertiary"]
        ):hover {
            transform: translateY(-1px);
            border-color: rgba(162, 247, 255, 0.72);
            box-shadow:
                0 0 8px rgba(0, 212, 255, 0.36),
                0 0 18px rgba(0, 212, 255, 0.22),
                inset 0 0 10px rgba(0, 212, 255, 0.18);
        }
        :is(
            .stButton>button,
            .stDownloadButton>button,
            .stFormSubmitButton>button,
            div[data-testid="stButton"]>button,
            div[data-testid="stDownloadButton"]>button,
            div[data-testid="stFormSubmitButton"]>button,
            button[data-testid="stBaseButton-primary"],
            button[data-testid="stBaseButton-secondary"],
            button[data-testid="stBaseButton-tertiary"],
            button[kind="primary"],
            button[kind="secondary"],
            button[kind="tertiary"]
        ):focus-visible {
            outline: none;
            border-color: rgba(194, 253, 255, 0.82);
            box-shadow:
                0 0 0 2px rgba(0, 18, 32, 0.95),
                0 0 0 3px rgba(0, 212, 255, 0.34),
                0 0 12px rgba(0, 212, 255, 0.32);
        }
        :is(
            .stButton>button,
            .stDownloadButton>button,
            .stFormSubmitButton>button,
            div[data-testid="stButton"]>button,
            div[data-testid="stDownloadButton"]>button,
            div[data-testid="stFormSubmitButton"]>button,
            button[data-testid="stBaseButton-primary"],
            button[data-testid="stBaseButton-secondary"],
            button[data-testid="stBaseButton-tertiary"],
            button[kind="primary"],
            button[kind="secondary"],
            button[kind="tertiary"]
        ):disabled {
            animation: none;
            text-shadow: none;
            border-color: rgba(120, 148, 180, 0.40);
            box-shadow: none;
            opacity: 0.65;
            cursor: not-allowed;
        }
        @keyframes neonButtonPulse {
            0% {
                box-shadow:
                    0 0 5px rgba(0, 212, 255, 0.24),
                    0 0 12px rgba(0, 212, 255, 0.16),
                    inset 0 0 6px rgba(0, 212, 255, 0.12);
            }
            50% {
                box-shadow:
                    0 0 7px rgba(0, 212, 255, 0.34),
                    0 0 16px rgba(0, 212, 255, 0.22),
                    inset 0 0 8px rgba(0, 212, 255, 0.16);
            }
            100% {
                box-shadow:
                    0 0 5px rgba(0, 212, 255, 0.24),
                    0 0 12px rgba(0, 212, 255, 0.16),
                    inset 0 0 6px rgba(0, 212, 255, 0.12);
            }
        }

        @keyframes jtLogoNeonPulse {
            0% {
                border-color: rgba(138, 221, 255, 0.56);
                box-shadow:
                    0 0 16px rgba(0, 212, 255, 0.28),
                    0 0 36px rgba(0, 212, 255, 0.16),
                    0 12px 24px rgba(1, 8, 19, 0.34),
                    inset 0 0 0 1px rgba(208, 235, 255, 0.10);
            }
            50% {
                border-color: rgba(176, 243, 255, 0.92);
                box-shadow:
                    0 0 26px rgba(0, 212, 255, 0.52),
                    0 0 56px rgba(0, 212, 255, 0.34),
                    0 14px 26px rgba(1, 8, 19, 0.40),
                    inset 0 0 0 1px rgba(220, 246, 255, 0.18);
            }
            100% {
                border-color: rgba(138, 221, 255, 0.56);
                box-shadow:
                    0 0 16px rgba(0, 212, 255, 0.28),
                    0 0 36px rgba(0, 212, 255, 0.16),
                    0 12px 24px rgba(1, 8, 19, 0.34),
                    inset 0 0 0 1px rgba(208, 235, 255, 0.10);
            }
        }

        @keyframes jtLogoSweep {
            0% {
                transform: translateX(-130%) rotate(9deg);
                opacity: 0;
            }
            20% {
                opacity: 0.78;
            }
            55% {
                opacity: 0.16;
            }
            100% {
                transform: translateX(150%) rotate(9deg);
                opacity: 0;
            }
        }

        @keyframes jtLogoDiamondPulse {
            0% {
                filter: brightness(1.00);
                box-shadow:
                    0 0 15px rgba(104, 223, 255, 0.52),
                    0 0 30px rgba(104, 223, 255, 0.25),
                    inset 0 0 12px rgba(245, 252, 255, 0.18);
            }
            50% {
                filter: brightness(1.24);
                box-shadow:
                    0 0 24px rgba(104, 223, 255, 0.82),
                    0 0 44px rgba(104, 223, 255, 0.40),
                    inset 0 0 18px rgba(245, 252, 255, 0.30);
            }
            100% {
                filter: brightness(1.00);
                box-shadow:
                    0 0 15px rgba(104, 223, 255, 0.52),
                    0 0 30px rgba(104, 223, 255, 0.25),
                    inset 0 0 12px rgba(245, 252, 255, 0.18);
            }
        }

        @keyframes jtLogoSparkle {
            0% {
                opacity: 0.30;
                transform: scale(0.80);
            }
            50% {
                opacity: 1;
                transform: scale(1.16);
            }
            100% {
                opacity: 0.30;
                transform: scale(0.80);
            }
        }

        @keyframes jtLogoTitleGlow {
            0% {
                text-shadow: 0 0 6px rgba(142, 232, 255, 0.34), 0 0 14px rgba(0, 212, 255, 0.20);
            }
            50% {
                text-shadow: 0 0 12px rgba(180, 244, 255, 0.64), 0 0 28px rgba(0, 212, 255, 0.36);
            }
            100% {
                text-shadow: 0 0 6px rgba(142, 232, 255, 0.34), 0 0 14px rgba(0, 212, 255, 0.20);
            }
        }

        .jt-logo-wrap {
            position: relative;
            display: inline-flex;
            align-items: center;
            gap: 0.64rem;
            margin: 0.08rem auto 0.52rem auto;
            padding: 0.38rem 0.90rem 0.40rem 0.42rem;
            border-radius: 14px;
            border: 1px solid rgba(138, 221, 255, 0.56);
            background: linear-gradient(160deg, rgba(8, 19, 34, 0.96), rgba(6, 15, 29, 0.94));
            box-shadow:
                0 0 16px rgba(0, 212, 255, 0.28),
                0 0 36px rgba(0, 212, 255, 0.16),
                0 12px 24px rgba(1, 8, 19, 0.34),
                inset 0 0 0 1px rgba(208, 235, 255, 0.10);
            overflow: hidden;
            isolation: isolate;
            animation: jtLogoNeonPulse 3.9s ease-in-out infinite;
        }

        .jt-logo-wrap::before {
            content: "";
            position: absolute;
            left: 0;
            right: 0;
            top: 0;
            height: 1px;
            background: linear-gradient(90deg, rgba(92, 201, 255, 0.0), rgba(176, 243, 255, 1), rgba(92, 201, 255, 0.0));
            pointer-events: none;
        }

        .jt-logo-wrap::after {
            content: "";
            position: absolute;
            top: -40%;
            bottom: -40%;
            width: 34%;
            left: 0;
            pointer-events: none;
            background: linear-gradient(90deg, rgba(255, 255, 255, 0.0), rgba(209, 244, 255, 0.68), rgba(255, 255, 255, 0.0));
            filter: blur(8px);
            opacity: 0;
            animation: jtLogoSweep 4.8s linear infinite;
        }

        .jt-logo-mark {
            position: relative;
            z-index: 1;
            width: 37px;
            height: 37px;
            min-width: 37px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-family: var(--font-body);
            font-size: 0.77rem;
            font-weight: 800;
            letter-spacing: 0.07em;
            color: #F1FAFF;
            text-shadow: 0 0 8px rgba(228, 251, 255, 0.56);
            clip-path: polygon(50% 2%, 98% 50%, 50% 98%, 2% 50%);
            border: 1px solid rgba(176, 237, 255, 0.88);
            background:
                linear-gradient(145deg, rgba(100, 219, 255, 0.18), rgba(120, 237, 255, 0.10)),
                linear-gradient(145deg, rgba(20, 118, 167, 0.98), rgba(14, 87, 140, 0.98));
            box-shadow:
                0 0 15px rgba(104, 223, 255, 0.52),
                0 0 30px rgba(104, 223, 255, 0.25),
                inset 0 0 12px rgba(245, 252, 255, 0.18);
            animation: jtLogoDiamondPulse 2.9s ease-in-out infinite;
            overflow: visible;
        }

        .jt-logo-mark::before,
        .jt-logo-mark::after {
            content: "";
            position: absolute;
            pointer-events: none;
            border-radius: 999px;
            background: radial-gradient(circle, rgba(238, 253, 255, 0.94) 0%, rgba(238, 253, 255, 0.0) 68%);
            animation: jtLogoSparkle 1.9s ease-in-out infinite;
        }

        .jt-logo-mark::before {
            width: 10px;
            height: 10px;
            top: 2px;
            right: 2px;
            animation-delay: 0.2s;
        }

        .jt-logo-mark::after {
            width: 7px;
            height: 7px;
            left: 3px;
            bottom: 3px;
            animation-delay: 0.95s;
        }

        .jt-logo-text {
            position: relative;
            z-index: 1;
            display: inline-flex;
            flex-direction: column;
            line-height: 1.03;
            text-align: left;
        }

        .jt-logo-kicker {
            font-family: var(--font-body);
            font-size: 0.48rem;
            font-weight: 700;
            letter-spacing: 0.18em;
            text-transform: uppercase;
            color: #95B4D7;
        }

        .jt-logo-title {
            margin-top: 0.03rem;
            font-family: var(--font-head);
            font-size: 1.12rem;
            font-weight: 700;
            letter-spacing: 0.01em;
            background: linear-gradient(92deg, #F2F9FF 0%, #B7EEFF 46%, #D8FBFF 100%);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
            animation: jtLogoTitleGlow 3.2s ease-in-out infinite;
        }

        .jt-logo-sub {
            margin-top: 0.10rem;
            font-family: var(--font-body);
            font-size: 0.54rem;
            font-weight: 700;
            letter-spacing: 0.14em;
            color: #A7C3E1;
            text-transform: uppercase;
        }

        @media (max-width: 680px) {
            .jt-logo-wrap {
                gap: 0.52rem;
                padding: 0.31rem 0.64rem 0.33rem 0.34rem;
            }
            .jt-logo-mark {
                width: 32px;
                height: 32px;
                min-width: 32px;
                font-size: 0.68rem;
            }
            .jt-logo-kicker {
                font-size: 0.44rem;
                letter-spacing: 0.15em;
            }
            .jt-logo-title {
                font-size: 0.94rem;
            }
            .jt-logo-sub {
                font-size: 0.49rem;
                letter-spacing: 0.12em;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

ALT_POS_COLOR = "#3DDC97"
ALT_NEG_COLOR = "#FF5D73"
ALT_LINE_COLOR = "#5CC9FF"
ALT_EQ_POS_COLOR = "#8CEFC0"
ALT_EQ_NEG_COLOR = "#FF9FAF"
ALT_AXIS_COLOR = "#AFC2DF"
ALT_GRID_COLOR = "rgba(138, 162, 193, 0.22)"

def _style_altair_chart(chart: alt.Chart, height: int = 320, title: str | None = None) -> alt.Chart:
    styled = chart.properties(height=height)
    if title:
        styled = styled.properties(title=alt.TitleParams(title, anchor="start"))
    return (
        styled
        .configure_view(strokeOpacity=0)
        .configure_axis(
            labelColor=ALT_AXIS_COLOR,
            titleColor=ALT_AXIS_COLOR,
            domainColor="rgba(138,162,193,0.45)",
            tickColor="rgba(138,162,193,0.45)",
            gridColor=ALT_GRID_COLOR,
            labelFontSize=12,
            titleFontSize=13,
            titleFontWeight=600,
        )
        .configure_axisX(labelAngle=-20)
        .configure_axisY(format=",.2f")
        .configure_legend(
            labelColor="#CFE0F7",
            titleColor="#CFE0F7",
            symbolType="circle",
        )
        .configure_title(
            color="#E6EEFF",
            fontSize=16,
            fontWeight=700,
            orient="top",
            anchor="start",
            offset=8,
        )
    )

def _centered_heading(text: str, level: int = 2) -> None:
    lvl = int(max(1, min(6, level)))
    st.markdown(
        f"""
        <h{lvl} style="text-align:center; width:100%; margin:0.20rem auto 0.55rem auto;">
            {text}
        </h{lvl}>
        """,
        unsafe_allow_html=True,
    )

def _render_junior_trades_logo() -> None:
    st.markdown(
        """
        <div style="display:flex; justify-content:center; width:100%;">
            <div class="jt-logo-wrap" aria-label="Junior Trades">
                <div class="jt-logo-mark" aria-hidden="true">JT</div>
                <div class="jt-logo-text">
                    <span class="jt-logo-kicker">Trading Lab</span>
                    <span class="jt-logo-title">Junior Trades</span>
                    <span class="jt-logo-sub">Relatório de Performance</span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

_inject_dashboard_styles()
_init_session_defaults()
_hydrate_robot_prefs_to_session()
_centered_heading("Relatório Pós-Trade — MT5/CSV", level=1)
_render_junior_trades_logo()
st.caption("Completo: timezone fix, setups do robô, MT5 via MAGIC, custos B3 auto, candles com entrada/saída, stop inicial do CSV, tabelas formatadas, leaderboards e exportações.")

# --------- Sidebar (parte 1): fonte e custos + botão carregar ---------
def _render_data_sidebar() -> bool:
    with st.sidebar:
        st.header("Fonte de dados")
        if not HAS_MT5:
            st.session_state["use_csv"] = True
            st.caption("MT5 indisponível neste ambiente: modo CSV ativado automaticamente.")
        use_csv = st.checkbox(
            "Usar CSV (em vez de MT5)",
            value=bool(st.session_state.get("use_csv", not HAS_MT5)),
            disabled=not HAS_MT5,
        )
        st.session_state["use_csv"] = use_csv
        if use_csv:
            st.file_uploader("Selecione o CSV de trades", type=["csv","txt"], key="up_trades")
            st.caption("Opcional: selecione e execute o robô para leitura automática do CSV na mesma pasta.")
            saved_robot_path = _normalize_local_path(st.session_state.get("robot_executable_path", ""))
            if saved_robot_path:
                st.caption(f"Robô salvo: {saved_robot_path}")

            col_pick, col_refresh = st.columns(2)
            if col_pick.button("Procurar executável do robô", key="btn_pick_robot_exec"):
                chosen = _pick_robot_executable_dialog()
                if chosen:
                    st.session_state["robot_executable_path"] = chosen
                    st.session_state["robot_executable_path_input"] = chosen
                    _save_robot_prefs(chosen)
                    st.success(f"Executável selecionado: {chosen}")
                else:
                    st.info("Seleção cancelada ou indisponível neste ambiente.")

            if col_refresh.button("Atualizar CSV da pasta", key="btn_refresh_robot_csv"):
                csv_now = _auto_csv_from_robot_path(
                    st.session_state.get("robot_executable_path", ""),
                    modified_after=float(st.session_state.get("robot_last_launch_ts", 0.0) or 0.0),
                )
                st.session_state["robot_csv_auto_path"] = csv_now or ""
                if csv_now:
                    st.success(f"CSV detectado: {os.path.basename(csv_now)}")
                else:
                    st.warning("Nenhum CSV encontrado na pasta do robô.")

            st.session_state.setdefault(
                "robot_executable_path_input",
                st.session_state.get("robot_executable_path", ""),
            )
            robot_path_input = st.text_input(
                "Executável do robô (opcional)",
                key="robot_executable_path_input",
                placeholder=r"C:\pasta_do_robo\seu_robo.exe",
            )
            prev_robot_path = _normalize_local_path(st.session_state.get("robot_executable_path", ""))
            st.session_state["robot_executable_path"] = _normalize_local_path(robot_path_input)
            if st.session_state["robot_executable_path"] != prev_robot_path:
                _save_robot_prefs(st.session_state["robot_executable_path"])

            col_run, col_clear = st.columns(2)
            if col_run.button("Abrir robô", key="btn_run_robot_exec"):
                ok, msg = _launch_robot_executable(st.session_state.get("robot_executable_path", ""))
                if ok:
                    st.session_state["robot_last_launch_ts"] = datetime.now().timestamp()
                    st.success(msg)
                else:
                    st.error(msg)
            if col_clear.button("Limpar robô salvo", key="btn_clear_robot_saved"):
                st.session_state["robot_executable_path"] = ""
                st.session_state["robot_executable_path_input"] = ""
                st.session_state["robot_csv_auto_path"] = ""
                st.session_state["robot_last_launch_ts"] = 0.0
                _save_robot_prefs("")
                st.success("Caminho do robô salvo foi removido.")

            csv_auto = _auto_csv_from_robot_path(
                st.session_state.get("robot_executable_path", ""),
                modified_after=float(st.session_state.get("robot_last_launch_ts", 0.0) or 0.0),
            )
            st.session_state["robot_csv_auto_path"] = csv_auto or ""
            if csv_auto:
                st.caption(f"CSV automático: {csv_auto}")
            elif st.session_state.get("robot_executable_path", ""):
                st.caption("Sem CSV encontrado na pasta do robô.")
        else:
            st.session_state["mt5_magic"] = st.number_input(
                "Magic do robô",
                min_value=0,
                value=int(st.session_state.get("mt5_magic", 20250902)),
                step=1,
            )
            st.session_state["mt5_d0"] = st.date_input(
                "Data inicial",
                value=st.session_state.get("mt5_d0", datetime.now().date() - timedelta(days=365)),
            )
            st.session_state["mt5_d1"] = st.date_input(
                "Data final",
                value=st.session_state.get("mt5_d1", datetime.now().date()),
            )
            if st.session_state["mt5_d0"] > st.session_state["mt5_d1"]:
                st.session_state["mt5_d0"], st.session_state["mt5_d1"] = st.session_state["mt5_d1"], st.session_state["mt5_d0"]
                st.caption("Período MT5 invertido ajustado automaticamente.")

        st.markdown("---")
        st.header("Parâmetros & Custos")
        st.subheader("Custos (B3) — Automático/Manual")
        st.session_state["auto_b3"] = st.checkbox(
            "Tentar buscar custos B3 automaticamente",
            value=bool(st.session_state.get("auto_b3", False)),
            help="Se falhar, mantém o modo manual e avisa.",
        )
        st.session_state["b3_source_url"] = st.text_input(
            "URL de tabela/arquivo de custos (opcional)",
            value=str(st.session_state.get("b3_source_url", "")),
            help="CSV com uma coluna 'b3_pct' (decimal). Se vazio, usa heurística por tipo de mercado.",
        )
        st.session_state["initial_capital"] = st.number_input(
            "Capital inicial (R$)",
            value=float(st.session_state.get("initial_capital", 0.0)),
            step=100.0,
            format="%.2f",
        )
        st.session_state["fee_b3_pct_manual"] = (
            st.number_input(
                "Taxa B3 (%) [manual]",
                value=float(st.session_state.get("fee_b3_pct_manual", 0.0)),
                step=0.01,
                format="%.2f",
            )
            / 100.0
        )
        st.session_state["fee_broker_in"] = st.number_input(
            "Corretagem ENTRADA (R$)",
            value=float(st.session_state.get("fee_broker_in", 0.99)),
            step=0.50,
            format="%.2f",
        )
        st.session_state["fee_broker_out"] = st.number_input(
            "Corretagem SAÍDA (R$)",
            value=float(st.session_state.get("fee_broker_out", 0.99)),
            step=0.50,
            format="%.2f",
        )
        st.session_state["base_pct"] = st.selectbox(
            "Base do % de custos",
            ["notional", "pnl"],
            index=0 if st.session_state.get("base_pct", "notional") == "notional" else 1,
        )

        st.markdown("---")
        st.header("Risco Operacional")
        st.session_state["max_concurrent_trades"] = st.number_input(
            "Máx. trades simultâneos (0 = sem limite)",
            min_value=0,
            value=int(st.session_state.get("max_concurrent_trades", 0)),
            step=1,
            help="Limita a quantidade de operações contabilizadas ao mesmo tempo nas curvas (Macro e Curva 2).",
        )

        st.markdown("---")
        st.header("Cenários de Capital")
        if use_csv:
            st.session_state["capital_scenario_mode"] = st.selectbox(
                "Modo de alocação por trade",
                options=CAPITAL_SCENARIO_OPTIONS,
                index=CAPITAL_SCENARIO_OPTIONS.index(
                    st.session_state.get("capital_scenario_mode", CAPITAL_SCENARIO_OPTIONS[0])
                ) if st.session_state.get("capital_scenario_mode", CAPITAL_SCENARIO_OPTIONS[0]) in CAPITAL_SCENARIO_OPTIONS else 0,
                help="Teste entradas fixas, composto e percentual para simular cenários mais realistas.",
            )

            mode_now = st.session_state["capital_scenario_mode"]
            if mode_now == "Entrada fixa por trade (R$)":
                st.session_state["capital_fixed_value"] = st.number_input(
                    "Valor fixo por trade (R$)",
                    min_value=0.0,
                    value=float(st.session_state.get("capital_fixed_value", 1000.0)),
                    step=100.0,
                    format="%.2f",
                )
            elif mode_now == "Composto pelo remanescente do trade":
                st.session_state["capital_first_trade_value"] = st.number_input(
                    "Valor do 1º trade (R$)",
                    min_value=0.0,
                    value=float(st.session_state.get("capital_first_trade_value", 1000.0)),
                    step=100.0,
                    format="%.2f",
                    help="Os próximos trades usam o capital remanescente (alocado + P&L líquido) do trade anterior.",
                )
            elif mode_now == "% do capital por trade":
                st.session_state["capital_pct_entry"] = st.slider(
                    "% do capital por entrada",
                    min_value=0.1,
                    max_value=100.0,
                    value=float(st.session_state.get("capital_pct_entry", 10.0)),
                    step=0.1,
                )
                st.session_state["capital_pct_reapply"] = st.checkbox(
                    "Reaplicar capital remanescente (composto)",
                    value=bool(st.session_state.get("capital_pct_reapply", True)),
                    help="Marcado: usa % sobre capital atual. Desmarcado: usa % sobre capital inicial.",
                )

            st.session_state["capital_qty_integer"] = st.checkbox(
                "Quantidade inteira de ações/lotes",
                value=bool(st.session_state.get("capital_qty_integer", True)),
            )
            q1, q2 = st.columns(2)
            with q1:
                st.session_state["capital_qty_min"] = st.number_input(
                    "Qtd mínima",
                    min_value=0.0,
                    value=float(st.session_state.get("capital_qty_min", 1.0)),
                    step=1.0,
                    format="%.4f",
                )
            with q2:
                st.session_state["capital_qty_step"] = st.number_input(
                    "Passo da qtd",
                    min_value=0.0001,
                    value=float(st.session_state.get("capital_qty_step", 1.0)),
                    step=1.0,
                    format="%.4f",
                )
        else:
            st.caption(
                "MT5 por Magic ativo: cenários de capital ficam desativados para manter o relatório fiel às operações reais."
            )

        st.markdown("---")
        st.header("Cores — Trade Viewer")
        _candle_colors = _load_candle_colors()
        st.session_state["tv_entry_line_color"] = st.color_picker(
            "Linha de Entrada",
            st.session_state.get("tv_entry_line_color", _candle_colors.get("tv_entry_line_color", "#4169E1")),
        )
        st.session_state["tv_stop_line_color"] = st.color_picker(
            "Linha de Stop",
            st.session_state.get("tv_stop_line_color", _candle_colors.get("tv_stop_line_color", "#DC143C")),
        )
        st.session_state["tv_exit_line_color"] = st.color_picker(
            "Linha de Saída",
            st.session_state.get("tv_exit_line_color", _candle_colors.get("tv_exit_line_color", "#2E8B57")),
        )
        st.session_state["tv_marker_color"] = st.color_picker(
            "Marcadores (Entrada/Saída)",
            st.session_state.get("tv_marker_color", _candle_colors.get("tv_marker_color", "#E2E8F0")),
        )
        st.session_state["tv_dark_theme"] = st.checkbox(
            "Tema escuro (Trade Viewer)",
            value=bool(st.session_state.get("tv_dark_theme", True)),
        )
        st.session_state["tv_bg_color"] = st.color_picker(
            "Fundo do grafico (candles)",
            st.session_state.get("tv_bg_color", _candle_colors.get("tv_bg_color", "#0B1220")),
        )
        wm_default = st.session_state.get("tv_watermark_text", "")
        if wm_default == "Trade Viewer":
            wm_default = ""
        st.session_state["tv_watermark_text"] = st.text_input(
            "Marca d'agua (Trade Viewer)",
            value=wm_default,
            placeholder="Deixe em branco para usar o ativo",
        )
        st.session_state["tv_watermark_color"] = st.color_picker(
            "Cor da marca d'agua",
            st.session_state.get("tv_watermark_color", _candle_colors.get("tv_watermark_color", "#E2E8F0")),
        )
        st.session_state["tv_watermark_opacity"] = st.slider(
            "Opacidade da marca d'agua",
            min_value=0.0,
            max_value=1.0,
            value=float(st.session_state.get("tv_watermark_opacity", _candle_colors.get("tv_watermark_opacity", 0.60))),
            step=0.01,
        )

        _save_candle_colors(
            {
                "tv_entry_line_color": st.session_state["tv_entry_line_color"],
                "tv_stop_line_color": st.session_state["tv_stop_line_color"],
                "tv_exit_line_color": st.session_state["tv_exit_line_color"],
                "tv_marker_color": st.session_state["tv_marker_color"],
                "tv_bg_color": st.session_state["tv_bg_color"],
                "tv_dark_theme": st.session_state["tv_dark_theme"],
                "tv_watermark_color": st.session_state["tv_watermark_color"],
                "tv_watermark_opacity": st.session_state["tv_watermark_opacity"],
            }
        )

        st.markdown("---")
        return st.button("Carregar/Atualizar dados")

load_clicked = _render_data_sidebar()

# --------- Custos automáticos ----------
def _detect_market_kind(symbols: list[str]) -> str:
    s = " ".join([str(x).upper() for x in symbols])
    fut_keys = ["WIN","IND","WDO","DOL","CCM","BGI","FUT"]
    if any(k in s for k in fut_keys):
        return "futures"
    if re.search(r"[A-Z]{4}\d", s):
        return "equities"
    return "unknown"

@st.cache_data(show_spinner=False, ttl=1800)
def _read_costs_csv_url_cached(url: str) -> pd.DataFrame:
    return pd.read_csv(url)

def get_b3_costs_auto(trades_df: pd.DataFrame, url: str | None) -> float | None:
    if url and url.strip():
        try:
            df_cost = _read_costs_csv_url_cached(url.strip())
            col = None
            for cand in ["b3_pct","b3_percent","emoluments","emolumentos","tax_b3"]:
                if cand in df_cost.columns:
                    col = cand; break
            if col is not None:
                val = pd.to_numeric(df_cost[col], errors="coerce").dropna()
                if not val.empty:
                    return float(val.iloc[0])
        except Exception as e:
            st.warning(f"Falha ao ler custos via URL: {e}")

    try:
        symbols = trades_df["symbol"].astype(str).dropna().unique().tolist() if "symbol" in trades_df.columns else []
        kind = _detect_market_kind(symbols)
        defaults = {"equities": 0.00030, "futures":  0.00000, "unknown":  0.00015}
        return float(defaults.get(kind, 0.0))
    except Exception:
        return None

def _load_trades_into_state():
    st.session_state["csv_source_label"] = ""
    if st.session_state["use_csv"]:
        up = st.session_state.get("up_trades", None)
        if up:
            try:
                df = read_from_csv(up)
            except Exception as e:
                st.error(f"Falha ao ler o CSV enviado: {e}")
                return False
            st.session_state["trades_raw"] = df
            up_name = getattr(up, "name", "upload.csv")
            st.session_state["csv_source_label"] = f"Upload: {up_name}"
            return True

        auto_csv = _auto_csv_from_robot_path(
            st.session_state.get("robot_executable_path", ""),
            modified_after=float(st.session_state.get("robot_last_launch_ts", 0.0) or 0.0),
        )
        if auto_csv:
            try:
                df = read_from_csv(auto_csv)
            except Exception as e:
                st.error(f"Falha ao ler o CSV automático ({auto_csv}): {e}")
                return False
            st.session_state["robot_csv_auto_path"] = auto_csv
            st.session_state["trades_raw"] = df
            st.session_state["csv_source_label"] = auto_csv
            return True

        st.error("Envie um CSV de trades ou selecione/execute o robô para leitura automática.")
        return False
    else:
        if not HAS_MT5:
            st.error("MT5 não está disponível.")
            return False
        try:
            date_from = st.session_state["mt5_d0"]
            date_to = st.session_state["mt5_d1"]
            if date_from > date_to:
                date_from, date_to = date_to, date_from
            df = read_from_mt5(
                magic=int(st.session_state["mt5_magic"]),
                date_from=date_from,
                date_to=date_to
            )
            st.session_state["trades_raw"] = df
            st.session_state["csv_source_label"] = "MT5"
            return True
        except Exception as e:
            st.error(f"Erro ao ler MT5: {e}")
            return False

if load_clicked:
    ok = _load_trades_into_state()
    if ok:
        source_lbl = str(st.session_state.get("csv_source_label", "")).strip()
        if source_lbl:
            st.success(f"Dados carregados/atualizados com sucesso. Fonte: {source_lbl}")
        else:
            st.success("Dados carregados/atualizados com sucesso.")
        st.session_state["selected_trade_sim_idx"] = None
        st.rerun()

if "trades_raw" not in st.session_state or st.session_state["trades_raw"] is None or st.session_state["trades_raw"].empty:
    st.info("Carregue os dados na barra lateral para continuar.")
    st.stop()

trades_raw = st.session_state["trades_raw"].copy()

# --------- Sidebar (parte 2): Filtros ----------
with st.sidebar:
    st.markdown("---")
    st.header("Filtros")

    # MULTISSELEÇÃO DE ATIVOS (vazio = não restringe)
    symbols = sorted(trades_raw["symbol"].astype(str).dropna().unique().tolist()) if "symbol" in trades_raw.columns else []
    default_symbols = [s for s in st.session_state.get("filter_symbols", []) if s in symbols]
    st.session_state["filter_symbols"] = st.multiselect(
        "Ativo(s)",
        options=symbols,
        default=default_symbols,
        help="Deixe vazio para não restringir por ativo."
    )

    # Opções de setup (vazio = não restringe)
    if "setup" in trades_raw.columns:
        setup_opts = (trades_raw["setup"].astype(str)
                                      .str.upper()
                                      .replace({"NONE":"","NAN":""})
                                      .dropna())
        setup_opts = sorted([s for s in setup_opts.unique().tolist() if s.strip() != ""])
    else:
        setup_opts = []
    default_setups = [s for s in st.session_state.get("filter_setups", []) if s in setup_opts]
    st.session_state["filter_setups"] = st.multiselect(
        "Setup(s)",
        options=setup_opts,
        default=default_setups,
        help="Deixe vazio para não restringir por setup."
    )

    # Datas
    st.session_state["f_date_from"] = st.date_input("De (data)", value=st.session_state.get("f_date_from", None))
    st.session_state["f_date_to"]   = st.date_input("Até (data)", value=st.session_state.get("f_date_to", None))

# ---------------- Filtros ----------------
def _apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()

    # sort_time -> naive para comparações
    if "sort_time" in d.columns:
        st_series = _to_naive_series(d["sort_time"])
        d["__sort_time_naive"] = st_series
    else:
        d["__sort_time_naive"] = pd.NaT

    # símbolo
    symbols_sel = st.session_state.get("filter_symbols", [])
    if symbols_sel and "symbol" in d.columns:
        d = d[d["symbol"].astype(str).isin(symbols_sel)]

    # setups
    setups_sel = st.session_state.get("filter_setups", [])
    if setups_sel and "setup" in d.columns:
        d = d[d["setup"].astype(str).str.upper().isin([s.upper() for s in setups_sel])]

    # data
    dfrom = st.session_state.get("f_date_from", None)
    dto   = st.session_state.get("f_date_to", None)
    if dfrom is not None and dto is not None and dfrom > dto:
        dfrom, dto = dto, dfrom
    if dfrom is not None:
        start_naive = pd.Timestamp(dfrom)
        d = d[d["__sort_time_naive"] >= start_naive]
    if dto is not None:
        end_naive = pd.Timestamp(dto) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        d = d[d["__sort_time_naive"] <= end_naive]

    d = d.drop(columns=["__sort_time_naive"], errors="ignore")
    return d
# ============== Custos, limite de simultaneidade, pnl_used, curva, ids =================
source_is_mt5_magic = not bool(st.session_state.get("use_csv", not HAS_MT5))

# Decide b3_pct efetivo (auto com fallback para manual)
if source_is_mt5_magic:
    fee_b3_pct_effective = 0.0
else:
    if st.session_state.get("auto_b3", False):
        auto_pct = get_b3_costs_auto(trades_raw, st.session_state.get("b3_source_url",""))
        if auto_pct is None:
            st.warning("Não foi possível obter os custos da B3 automaticamente. Mantendo valores manuais.")
            fee_b3_pct_effective = st.session_state["fee_b3_pct_manual"]
        else:
            fee_b3_pct_effective = float(auto_pct)
            st.info(f"Custos B3 automáticos aplicados: {pct(fee_b3_pct_effective)} (você pode ajustar manualmente se quiser).")
    else:
        fee_b3_pct_effective = st.session_state["fee_b3_pct_manual"]

trades_filtered = _apply_filters(trades_raw)
if trades_filtered.empty:
    st.warning("Nenhum trade após filtros/seleção.")
    st.stop()

# holding_days se ainda não existir
if "holding_days" not in trades_filtered.columns and {"entry_time","exit_time"}.issubset(trades_filtered.columns):
    dt_e = pd.to_datetime(trades_filtered["entry_time"], errors="coerce")
    dt_s = pd.to_datetime(trades_filtered["exit_time"], errors="coerce")
    trades_filtered["holding_days"] = (dt_s - dt_e).dt.total_seconds() / 86400.0

# Limite de simultaneidade para a Curva Macro
max_open_saved = int(st.session_state.get("max_concurrent_trades", 0))
max_open = 0 if source_is_mt5_magic else max_open_saved
if source_is_mt5_magic and max_open_saved != 0:
    st.caption("Fonte MT5/Magic: limite de simultaneidade forçado para 0 para manter fidelidade ao histórico real.")
trades_limited_base = apply_max_concurrent_limit(trades_filtered, max_open=max_open)

if trades_limited_base.empty:
    st.warning("Nenhum trade após limite de simultaneidade.")
    st.stop()

scenario_mode_saved = st.session_state.get("capital_scenario_mode", "Original (volume do arquivo)")
scenario_mode = "Original (volume do arquivo)" if source_is_mt5_magic else scenario_mode_saved
if source_is_mt5_magic and scenario_mode_saved != "Original (volume do arquivo)":
    st.caption("Fonte MT5/Magic: cenário de capital forçado para 'Original (volume do arquivo)'.")
if source_is_mt5_magic:
    st.caption("Fonte MT5/Magic: P&L realizado vem do próprio MT5 (profit + commission + swap + fee).")
    trades = _prepare_mt5_faithful_trades(
        trades_limited_base,
        initial_capital=float(st.session_state["initial_capital"]),
    )
    trades_costed = trades.copy()
    scenario_meta = {
        "rows_total": int(len(trades_limited_base)),
        "executed": int(len(trades)),
        "skipped": 0,
        "skipped_no_price": 0,
    }
elif scenario_mode == "Original (volume do arquivo)":
    trades_costed = apply_costs(
        trades_limited_base,
        b3_pct=fee_b3_pct_effective,
        brok_in_fixed=st.session_state["fee_broker_in"],
        brok_out_fixed=st.session_state["fee_broker_out"],
        base_pct=st.session_state["base_pct"]
    )
    if "pnl_net" in trades_costed.columns:
        trades_costed["pnl_used"] = trades_costed["pnl_net"]
    elif "profit" in trades_costed.columns:
        trades_costed["pnl_used"] = trades_costed["profit"]
    elif "pnl" in trades_costed.columns:
        trades_costed["pnl_used"] = trades_costed["pnl"]
    else:
        trades_costed["pnl_used"] = 0.0
    trades = _annotate_capital_path(trades_costed, initial_capital=st.session_state["initial_capital"])
    scenario_meta = {
        "rows_total": int(len(trades_limited_base)),
        "executed": int(len(trades)),
        "skipped": 0,
        "skipped_no_price": 0,
    }
else:
    trades, scenario_meta = simulate_capital_scenario(
        trades_limited_base,
        initial_capital=float(st.session_state["initial_capital"]),
        scenario_mode=scenario_mode,
        first_trade_value=float(st.session_state.get("capital_first_trade_value", 1000.0)),
        fixed_trade_value=float(st.session_state.get("capital_fixed_value", 1000.0)),
        pct_entry=float(st.session_state.get("capital_pct_entry", 10.0)),
        pct_reapply=bool(st.session_state.get("capital_pct_reapply", True)),
        qty_integer=bool(st.session_state.get("capital_qty_integer", True)),
        qty_min=float(st.session_state.get("capital_qty_min", 1.0)),
        qty_step=float(st.session_state.get("capital_qty_step", 1.0)),
        b3_pct=float(fee_b3_pct_effective),
        brok_in_fixed=float(st.session_state["fee_broker_in"]),
        brok_out_fixed=float(st.session_state["fee_broker_out"]),
        base_pct=str(st.session_state["base_pct"]),
    )
    if trades.empty:
        st.warning("Cenário escolhido não executou trades (capital/preço insuficientes). Ajuste os parâmetros.")
        st.stop()
    if scenario_meta.get("skipped", 0) > 0:
        st.info(
            f"Cenário '{scenario_mode}': {scenario_meta['executed']} executados de {scenario_meta['rows_total']} trades "
            f"(pulados: {scenario_meta['skipped']})."
        )
        if scenario_meta.get("skipped_no_price", 0) > 0:
            st.caption("Observação: parte dos trades foi ignorada por ausência de preço de entrada válido para dimensionar a quantidade.")

trades = trades.reset_index(drop=True)
trades["sim_idx"] = np.arange(len(trades))
sim = simulate_equity(trades, initial_capital=st.session_state["initial_capital"])
trades_costed = trades.copy()

if scenario_mode != "Original (volume do arquivo)":
    cfg_txt = []
    if scenario_mode == "Entrada fixa por trade (R$)":
        cfg_txt.append(f"entrada fixa {br_money(st.session_state.get('capital_fixed_value', 0.0))}")
    elif scenario_mode == "Composto pelo remanescente do trade":
        cfg_txt.append(f"1º trade {br_money(st.session_state.get('capital_first_trade_value', 0.0))}")
    elif scenario_mode == "% do capital por trade":
        cfg_txt.append(f"{float(st.session_state.get('capital_pct_entry', 0.0)):.2f}% por trade")
        cfg_txt.append("com reaplicação" if st.session_state.get("capital_pct_reapply", True) else "sem reaplicação")
    cfg_txt.append(
        "qtd inteira" if st.session_state.get("capital_qty_integer", True) else "qtd fracionária"
    )
    st.caption(
        f"Cenário ativo: **{scenario_mode}** ({' | '.join(cfg_txt)}). "
        f"Trades executados: **{scenario_meta.get('executed', len(trades))}**."
    )
    if st.session_state.get("capital_qty_integer", True):
        st.caption("Nota: com quantidade inteira, o capital executado pode ficar igual por alguns trades até o tamanho de lote mudar.")

# herda colunas para sim
extra_cols = [
    "setup","symbol","entry_time","exit_time","price_open","price_close","stop_price",
    "volume","contract_size","qtd_acoes","capital_target","capital_alocado","capital_before","capital_after",
    "size_factor","scenario_executed","scenario_skipped_reason",
    "sim_idx","holding_days","sort_time","order_entry","order_exit","ticket",
    "direction","position_id","pnl_mt5","mt5_profit","mt5_commission","mt5_swap","mt5_fee"
]
for c in extra_cols:
    if c in trades.columns:
        sim[c] = trades[c]

# GARANTE sort_time NAIVE para evitar tz-errors
if "sort_time" in sim.columns:
    sim["sort_time"] = _to_naive_series(sim["sort_time"])

# =============================== DASHBOARD ===============================
stats = compute_stats(sim)

total_costs = float(trades["costs"].sum()) if "costs" in trades.columns else 0.0
custos_por_trade = (total_costs / stats["trades"]) if stats["trades"] > 0 else 0.0
custos_por_mes = (total_costs / stats["total_meses"]) if stats["total_meses"] > 0 else 0.0
pct_time_mkt = _percentage_time_in_market(trades) if {"entry_time","exit_time"}.issubset(trades.columns) else 0.0

def _safe_num(v: float, default: float = 0.0) -> float:
    try:
        num = float(v)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(num):
        return default
    return num

def _clip01(v: float) -> float:
    return max(0.0, min(1.0, _safe_num(v)))

def _sigmoid01(value: float, center: float, scale: float) -> float:
    s = max(abs(_safe_num(scale)), 1e-9)
    z = (_safe_num(value) - _safe_num(center)) / s
    z = float(np.clip(z, -60.0, 60.0))
    return 1.0 / (1.0 + float(np.exp(-z)))

def _shrink_to_neutral(raw_score: float, reliability: float, neutral: float = 55.0) -> float:
    r = _clip01(reliability)
    return (_safe_num(raw_score) * r) + (_safe_num(neutral) * (1.0 - r))

def _score_band(score: float) -> tuple[str, str]:
    if score >= 75:
        return "Excelente", "#22C55E"
    if score >= 55:
        return "Bom", "#38BDF8"
    if score >= 35:
        return "Atenção", "#F59E0B"
    return "Crítico", "#EF4444"

def _qual_band(value: float, good: float, mid: float, reverse: bool = False) -> str:
    v = _safe_num(value)
    if not reverse:
        if v >= good:
            return "Excelente"
        if v >= mid:
            return "Bom"
        return "Atenção"
    if v <= good:
        return "Excelente"
    if v <= mid:
        return "Bom"
    return "Atenção"

def _qual_band_compensated(base_label: str, compensated: bool) -> str:
    if base_label == "Atenção" and compensated:
        return "Compensado"
    return base_label

def _pillar_reason_map(
    win_rate_v: float,
    payoff_v: float,
    dd_pct_v: float,
    cost_ratio_v: float,
    expect_v: float,
    underwater_months_v: float,
) -> dict[str, str]:
    return {
        "Consistência": f"Win Rate {pct(win_rate_v)} e meses positivos {pct(pct_meses_pos)}.",
        "Eficiência": f"Payoff {payoff_v:.2f} e expectativa {br_money(expect_v)} por trade.",
        "Risco": (
            f"Drawdown máximo {pct(dd_pct_v)}, sequência de perdas {int(_safe_num(stats.get('max_consecutive_losses', 0)))} "
            f"e {int(_safe_num(underwater_months_v))} meses abaixo do topo."
        ),
        "Custos": f"Custos equivalem a {cost_ratio_v:.2%} do |P&L| e {br_money(custos_por_trade)} por trade.",
    }

def _compensation_diagnosis(
    pnl_sum_v: float,
    win_rate_v: float,
    payoff_v: float,
    expectancy_v: float,
    dd_pct_v: float,
    cost_ratio_v: float,
    pct_meses_pos_v: float,
    max_neg_month_streak_v: float,
    max_underwater_months_v: float,
) -> tuple[str, str, list[str], list[str]]:
    reasons: list[str] = []
    alerts: list[str] = []

    has_edge = expectancy_v > 0 and payoff_v >= 1.35
    high_quality_edge = expectancy_v > 0 and payoff_v >= 2.0 and pct_meses_pos_v >= 0.55
    low_hitrate_compensated = win_rate_v < 0.45 and payoff_v >= 2.0 and expectancy_v > 0
    risk_stretched = dd_pct_v > 0.14
    risk_critical = dd_pct_v > 0.20
    costs_heavy = cost_ratio_v > 0.25
    costs_critical = cost_ratio_v > 0.40
    neg_months_stretched = max_neg_month_streak_v >= 9
    neg_months_critical = max_neg_month_streak_v >= 18
    underwater_stretched = max_underwater_months_v >= 12
    underwater_critical = max_underwater_months_v >= 24

    if low_hitrate_compensated:
        reasons.append("Taxa de acerto baixa está sendo compensada por payoff alto com expectativa positiva.")
    elif win_rate_v < 0.45 and payoff_v < 2.0:
        alerts.append("Taxa de acerto baixa sem compensação suficiente de payoff.")

    if costs_heavy and has_edge:
        reasons.append("Custos elevados ainda são absorvidos pela vantagem estatística líquida.")
    elif costs_heavy and not has_edge:
        alerts.append("Custos elevados corroem o resultado e não há edge suficiente para compensar.")

    if risk_stretched and high_quality_edge:
        reasons.append("Drawdown acima da zona ideal está parcialmente compensado por retorno/consistência.")
    elif risk_stretched:
        alerts.append("Risco esticado (drawdown alto) sem compensação robusta de consistência.")

    if underwater_stretched and high_quality_edge and not underwater_critical:
        reasons.append("Tempo prolongado em drawdown está parcialmente compensado por edge líquido e recuperação final.")
    elif underwater_stretched:
        alerts.append("A curva ficou muito tempo abaixo do topo, aumentando pressão psicológica e risco de abandono.")

    if neg_months_stretched and high_quality_edge and not neg_months_critical:
        reasons.append("Sequência longa de meses negativos foi parcialmente compensada por payoff/expectativa.")
    elif neg_months_stretched:
        alerts.append("Sequência extensa de meses negativos reduz robustez operacional.")

    if pnl_sum_v <= 0 or expectancy_v <= 0:
        status = "Não compensa"
        color = "#EF4444"
        if pnl_sum_v <= 0:
            alerts.append("Resultado líquido do período não é positivo.")
        if expectancy_v <= 0:
            alerts.append("Expectativa por trade está nula/negativa.")
        return status, color, reasons, alerts

    if underwater_critical or neg_months_critical:
        status = "Não compensa"
        color = "#EF4444"
        if underwater_critical:
            alerts.append(
                f"A estratégia passou {int(_safe_num(max_underwater_months_v))} meses abaixo do topo da curva."
            )
        if neg_months_critical:
            alerts.append(
                f"Houve {int(_safe_num(max_neg_month_streak_v))} meses negativos consecutivos."
            )
        return status, color, reasons, alerts

    if (risk_critical and not high_quality_edge) or (costs_critical and payoff_v < 2.0):
        status = "Não compensa"
        color = "#EF4444"
        if risk_critical:
            alerts.append("Drawdown crítico para o nível de retorno entregue.")
        if costs_critical:
            alerts.append("Custos muito altos em proporção ao P&L.")
        return status, color, reasons, alerts

    if underwater_stretched or neg_months_stretched:
        status = "Compensa com ressalvas"
        color = "#F59E0B"
        return status, color, reasons, alerts

    if (risk_stretched or costs_heavy) and not high_quality_edge:
        status = "Compensa com ressalvas"
        color = "#F59E0B"
        return status, color, reasons, alerts

    if not reasons:
        reasons.append("Retorno, eficiência e risco estão equilibrados em nível operacional saudável.")
    status = "Compensa"
    color = "#22C55E"
    return status, color, reasons, alerts

def _compensation_explanation(status: str, reasons: list[str], alerts: list[str]) -> str:
    if status == "Não compensa":
        if alerts:
            return alerts[0]
        if reasons:
            return reasons[0]
        return "O retorno líquido e a expectativa por trade ainda não sustentam o risco e os custos."
    if status == "Compensa com ressalvas":
        if alerts:
            return alerts[0]
        if reasons:
            return reasons[0]
        return "Há edge, mas com fragilidades de risco/custos que pedem ajuste."
    if reasons:
        return reasons[0]
    return "Retorno, eficiência e risco estão equilibrados em nível operacional saudável."

def _render_pillar_chip(title: str, score: float, detail: str) -> None:
    label, color = _score_band(score)
    score_10 = _safe_num(score) / 10.0
    st.markdown(
        f"""
        <div class="jt-report-card" style="border:1px solid rgba(148,163,184,0.28); border-left:4px solid {color};
                    border-radius:12px; padding:11px 12px;
                    background:linear-gradient(180deg, rgba(15,23,42,0.70), rgba(15,23,42,0.46));
                    box-shadow:0 6px 18px rgba(2,6,23,0.22); min-height:122px;">
            <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                <div style="font-size:0.79rem; letter-spacing:0.03em; color:#A7B7D1;">{title}</div>
                <div style="font-size:0.68rem; color:{color}; border:1px solid {color};
                            border-radius:999px; padding:2px 8px; font-weight:700;">
                    {label}
                </div>
            </div>
            <div style="font-size:1.38rem; font-weight:800; color:#E2E8F0; margin-top:4px;">{score_10:.1f}/10</div>
            <div style="font-size:0.78rem; color:#C8D5EA; margin-top:3px; line-height:1.35;">{detail}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

win_rate = _safe_num(stats.get("win_rate", 0.0))
pct_meses_pos = _safe_num(stats.get("pct_meses_positivos", 0.0))
payoff = _safe_num(stats.get("payoff", 0.0))
expectancy = _safe_num(stats.get("expectancy", 0.0))
dd_pct_abs = abs(_safe_num(stats.get("max_drawdown_pct", 0.0)))
loss_streak = _safe_num(stats.get("max_consecutive_losses", 0.0))
max_neg_month_streak = _safe_num(stats.get("max_neg_month_streak", 0.0))
underwater_months = _safe_num(stats.get("max_underwater_months", 0.0))
pnl_abs = abs(_safe_num(stats.get("pnl_sum", 0.0)))
cost_ratio = (total_costs / pnl_abs) if pnl_abs > 0 else 1.0

trades_n = _safe_num(stats.get("trades", 0.0))
months_n = _safe_num(stats.get("total_meses", 0.0))
trades_pm = _safe_num(stats.get("trades_med_por_mes", 0.0))
dd_len = _safe_num(stats.get("dd_len", 0.0))
avg_loss_abs = abs(_safe_num(stats.get("avg_loss", 0.0)))
avg_month_pos = _safe_num(stats.get("media_mes_positivo", 0.0))
avg_month_neg_abs = abs(_safe_num(stats.get("media_mes_negativo", 0.0)))

# qualidade dos sinais
win_score = _sigmoid01(win_rate, center=0.52, scale=0.08)
month_pos_score = _sigmoid01(pct_meses_pos, center=0.60, scale=0.10)
payoff_score = _sigmoid01(payoff, center=1.60, scale=0.35)
edge_ratio = (expectancy / avg_loss_abs) if avg_loss_abs > 0 else 0.0
edge_score = _sigmoid01(edge_ratio, center=0.20, scale=0.20)
dd_ok_score = 1.0 - _sigmoid01(dd_pct_abs, center=0.12, scale=0.04)
loss_ok_score = 1.0 - _sigmoid01(loss_streak, center=6.0, scale=2.5)
dd_len_ok_score = 1.0 - _sigmoid01(dd_len, center=20.0, scale=8.0)
underwater_ok_score = 1.0 - _sigmoid01(underwater_months, center=8.0, scale=3.0)
cost_ok_score = 1.0 - _sigmoid01(cost_ratio, center=0.18, scale=0.08)
friction_ratio = (custos_por_trade / expectancy) if expectancy > 0 else 1.0
friction_ok_score = 1.0 - _sigmoid01(friction_ratio, center=0.35, scale=0.18)

if avg_month_pos > 0:
    month_asymmetry = (avg_month_neg_abs / avg_month_pos)
    month_balance_score = 1.0 - _sigmoid01(month_asymmetry, center=0.90, scale=0.25)
else:
    month_balance_score = 0.5

cadence_distance = abs(trades_pm - 6.0)
cadence_score = 1.0 - _clip01(cadence_distance / 8.0)

# confiabilidade amostral: evita nota extrema com poucos dados
trade_reliability = _clip01((trades_n - 30.0) / 120.0)
month_reliability = _clip01((months_n - 4.0) / 14.0)
reliability_consistency = (0.60 * trade_reliability) + (0.40 * month_reliability)
reliability_efficiency = (0.65 * trade_reliability) + (0.35 * month_reliability)
reliability_risk = (0.70 * trade_reliability) + (0.30 * month_reliability)
reliability_cost = (0.80 * trade_reliability) + (0.20 * month_reliability)

consistency_raw = 100.0 * (
    0.40 * _clip01(win_score)
    + 0.35 * _clip01(month_pos_score)
    + 0.25 * _clip01(month_balance_score)
)
efficiency_raw = 100.0 * (
    0.35 * _clip01(payoff_score)
    + 0.45 * _clip01(edge_score)
    + 0.20 * _clip01(cadence_score)
)
risk_raw = 100.0 * (
    0.40 * _clip01(dd_ok_score)
    + 0.25 * _clip01(loss_ok_score)
    + 0.15 * _clip01(dd_len_ok_score)
    + 0.20 * _clip01(underwater_ok_score)
)
cost_raw = 100.0 * (
    0.60 * _clip01(cost_ok_score)
    + 0.40 * _clip01(friction_ok_score)
)

consistency_score = _shrink_to_neutral(consistency_raw, reliability_consistency, neutral=55.0)
efficiency_score = _shrink_to_neutral(efficiency_raw, reliability_efficiency, neutral=55.0)
risk_score = _shrink_to_neutral(risk_raw, reliability_risk, neutral=55.0)
cost_score = _shrink_to_neutral(cost_raw, reliability_cost, neutral=55.0)
overall_score = float(np.mean([consistency_score, efficiency_score, risk_score, cost_score]))

pillars = {
    "Consistência": consistency_score,
    "Eficiência": efficiency_score,
    "Risco": risk_score,
    "Custos": cost_score,
}
strongest = max(pillars, key=pillars.get)
bottleneck = min(pillars, key=pillars.get)
pillar_reasons = _pillar_reason_map(win_rate, payoff, dd_pct_abs, cost_ratio, expectancy, underwater_months)
priority_map = {
    "Consistência": "Prioridade: estabilizar frequência/qualidade de setups para elevar previsibilidade mensal.",
    "Eficiência": "Prioridade: melhorar payoff e expectativa por trade (ajuste de alvos/stops e filtro de entradas).",
    "Risco": "Prioridade: reduzir drawdown e sequência de perdas com regras de proteção e exposição.",
    "Custos": "Prioridade: otimizar custos operacionais e evitar excesso de giro em trades marginais.",
}
comp_status, comp_color, comp_reasons, comp_alerts = _compensation_diagnosis(
    pnl_sum_v=_safe_num(stats.get("pnl_sum", 0.0)),
    win_rate_v=win_rate,
    payoff_v=payoff,
    expectancy_v=expectancy,
    dd_pct_v=dd_pct_abs,
    cost_ratio_v=cost_ratio,
    pct_meses_pos_v=pct_meses_pos,
    max_neg_month_streak_v=max_neg_month_streak,
    max_underwater_months_v=underwater_months,
)
comp_why = _compensation_explanation(comp_status, comp_reasons, comp_alerts)

qual_entry_base = _qual_band(payoff, 2.0, 1.4)
qual_stability_base = _qual_band(win_rate, 0.58, 0.47)
qual_risk_base = _qual_band(dd_pct_abs, 0.08, 0.14, reverse=True)
qual_cost_base = _qual_band(cost_ratio, 0.12, 0.25, reverse=True)

entry_compensated = qual_entry_base == "Atenção" and win_rate >= 0.58 and expectancy > 0
stability_compensated = qual_stability_base == "Atenção" and payoff >= 2.0 and expectancy > 0
risk_compensated = (
    qual_risk_base == "Atenção"
    and payoff >= 2.0
    and expectancy > 0
    and pct_meses_pos >= 0.55
    and underwater_months <= 12
)
cost_compensated = qual_cost_base == "Atenção" and payoff >= 1.35 and expectancy > 0 and cost_ratio <= 0.40

qual_entry = _qual_band_compensated(qual_entry_base, entry_compensated)
qual_stability = _qual_band_compensated(qual_stability_base, stability_compensated)
qual_risk = _qual_band_compensated(qual_risk_base, risk_compensated)
qual_cost = _qual_band_compensated(qual_cost_base, cost_compensated)

uncompensated_attention: list[tuple[str, str]] = []
if qual_entry_base == "Atenção" and not entry_compensated:
    uncompensated_attention.append(("Qualidade de entrada/saída", "Eficiência"))
if qual_stability_base == "Atenção" and not stability_compensated:
    uncompensated_attention.append(("Estabilidade de resultado", "Consistência"))
if qual_risk_base == "Atenção" and not risk_compensated:
    uncompensated_attention.append(("Pressão de risco", "Risco"))
if qual_cost_base == "Atenção" and not cost_compensated:
    uncompensated_attention.append(("Eficiência de custos", "Custos"))

if uncompensated_attention:
    mesa_attention_text = "⚠️ Em atenção: " + ", ".join([item[0] for item in uncompensated_attention]) + "."
    mesa_focus_key = uncompensated_attention[0][1]
    mesa_focus_text = priority_map[mesa_focus_key].replace("Prioridade: ", "")
else:
    mesa_attention_text = "✅ Não há pontos em atenção sem compensação no momento."
    mesa_focus_key = bottleneck
    mesa_focus_text = f"manter monitoramento em {bottleneck.lower()} para preservar consistência da curva."

_centered_heading("🧠 Cockpit Quant — Diagnóstico Inteligente", level=2)
r1, r2, r3, r4, r5 = st.columns(5)
with r1:
    st.metric("Saldo Líquido", br_money(stats["pnl_sum"]), help="Resultado acumulado do período.")
with r2:
    st.metric("Win Rate", pct(win_rate), help="Percentual de trades positivos.")
with r3:
    st.metric("Payoff", f"{payoff:.2f}", help="Relação média entre ganho e perda.")
with r4:
    st.metric("Máx. DD (%)", pct(stats["max_drawdown_pct"]), help="Maior drawdown percentual da curva.")
with r5:
    st.metric("Índice Geral", f"{overall_score/10.0:.1f}/10", help="Média dos pilares com modelo não linear e ajuste de confiabilidade amostral.")

st.markdown(
    f"""
    <div class="jt-report-card" style="border:1px solid rgba(148,163,184,0.30); border-radius:12px; padding:10px 12px;
                background:linear-gradient(180deg, rgba(15,23,42,0.72), rgba(15,23,42,0.48));">
        <div style="font-size:0.80rem; color:#A7B7D1; letter-spacing:0.03em;">LEITURA RÁPIDA DE MESA</div>
        <div style="font-size:0.95rem; color:#E2E8F0; margin-top:4px;">
            💬 <b>{strongest}</b> está sustentando a curva com <b>{pillars[strongest]/10.0:.1f}/10</b>.
            {mesa_attention_text}
            🎯 Foco sugerido: {mesa_focus_text}
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

p1, p2, p3, p4 = st.columns(4)
with p1:
    _render_pillar_chip("📊 Consistência", consistency_score, f"Meses positivos: {pct(pct_meses_pos)} | Sequência de ganhos: {stats['max_consecutive_wins']}")
with p2:
    _render_pillar_chip("⚙️ Eficiência", efficiency_score, f"Expectativa por trade: {br_money(expectancy)} | Trades por mês: {stats['trades_med_por_mes']:.2f}")
with p3:
    _render_pillar_chip(
        "🛡️ Risco",
        risk_score,
        f"Drawdown máximo: {pct(stats['max_drawdown_pct'])} | Underwater: {int(underwater_months)} meses",
    )
with p4:
    _render_pillar_chip("💸 Custos", cost_score, f"Custo por trade: {br_money(custos_por_trade)} | Custo mensal: {br_money(custos_por_mes)}")

tab_diag, tab_ret, tab_risk, tab_eff, tab_cost = st.tabs(
    ["🧠 Diagnósticos", "📈 Retorno & Qualidade", "🛡️ Exposição & Risco", "⚙️ Eficiência Operacional", "🪙 Custos Operacionais"]
)

with tab_diag:
    st.info(priority_map[mesa_focus_key])
    st.caption(
        f"Leitura do gargalo ({bottleneck}): {pillar_reasons[bottleneck]}"
    )
    st.markdown(
        f"""
        <div class="jt-report-card" style="border:1px solid rgba(148,163,184,0.30); border-left:5px solid {comp_color};
                    border-radius:12px; padding:10px 12px; background:rgba(15,23,42,0.55);">
            <div style="font-size:0.78rem; color:#A7B7D1; letter-spacing:0.03em;">DECISÃO DE COMPENSAÇÃO</div>
            <div style="font-size:1.25rem; font-weight:700; color:{comp_color};">{comp_status}</div>
            <div style="font-size:0.86rem; color:#E2ECFB; margin-top:4px;">
                <b>Por quê:</b> {comp_why}
            </div>
            <div style="font-size:0.80rem; color:#C4D3E8; margin-top:3px;">
                O diagnóstico considera compensações entre acerto, payoff, expectativa, drawdown, tempo de recuperação e custos.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("Ver detalhes do diagnóstico", expanded=False):
        if comp_reasons:
            st.markdown("**O que está compensando**")
            for msg in comp_reasons:
                st.caption(f"• {msg}")
        if comp_alerts:
            st.markdown("**Ressalvas críticas**")
            for msg in comp_alerts:
                st.caption(f"• {msg}")

        diag_col1, diag_col2 = st.columns(2)
        with diag_col1:
            st.markdown("**Diagnóstico Executivo**")
            quality_suffix = " (compensado por consistência e expectativa)" if entry_compensated else ""
            stability_suffix = " (compensado por payoff/expectativa)" if stability_compensated else ""
            risk_suffix = " (compensado por edge e consistência)" if risk_compensated else ""
            cost_suffix = " (compensado por edge líquido)" if cost_compensated else ""
            st.markdown(
                f"- Qualidade de entrada/saída: **{qual_entry}**{quality_suffix} (Payoff {payoff:.2f}).\n"
                f"- Estabilidade de resultado: **{qual_stability}**{stability_suffix} (Win Rate {pct(win_rate)}).\n"
                f"- Pressão de risco: **{qual_risk}**{risk_suffix} (DD {pct(dd_pct_abs)} | Underwater {int(underwater_months)} meses).\n"
                f"- Eficiência de custos: **{qual_cost}**{cost_suffix} (Custos/|P&L| {cost_ratio:.2%})."
            )
        with diag_col2:
            st.markdown("**Faixas de Referência**")
            st.markdown(
                "- Win Rate: Excelente >= 58% | Bom >= 47%\n"
                "- Payoff: Excelente >= 2.00 | Bom >= 1.40\n"
                "- Drawdown %: Excelente <= 8% | Bom <= 14%\n"
                "- Underwater (meses): Bom < 12 | Crítico >= 24\n"
                "- Custos/|P&L|: Excelente <= 12% | Bom <= 25%"
            )

with tab_ret:
    a1, a2, a3 = st.columns(3)
    with a1:
        st.metric("P&L Total", br_money(stats["pnl_sum"]))
        st.metric("Média Mensal", br_money(stats["media_mensal"]))
    with a2:
        st.metric("Expectativa por Trade", br_money(expectancy))
        st.metric("Ticket Médio", br_money(stats["pnl_mean"]))
    with a3:
        st.metric("Trade Médio Vencedor", br_money(stats["avg_win"]))
        st.metric("Trade Médio Perdedor", br_money(stats["avg_loss"]))
    st.caption("Leitura profissional: retorno robusto combina expectativa positiva, payoff saudável e média mensal estável.")

with tab_risk:
    b1, b2, b3, b4 = st.columns(4)
    with b1:
        st.metric("Máx. DD (R$)", br_money(stats["max_drawdown"]))
        st.metric(
            "Máx. DD (%)",
            pct(stats["max_drawdown_pct"]),
            help="Queda máxima da curva desde um topo até o fundo."
        )
    with b2:
        st.metric("% Tempo no Mercado", pct(pct_time_mkt))
        st.metric("Dias Médios em Operação", format_days_hours(stats["avg_holding_days"]))
    with b3:
        st.metric("Maior sequência de Losses", f"{stats['max_consecutive_losses']}")
        st.metric("Maior sequência de Meses Negativos", f"{stats['max_neg_month_streak']}")
    with b4:
        st.metric(
            "Maior período Underwater (meses)",
            f"{int(underwater_months)}",
            help="Maior tempo em meses que a curva ficou abaixo do último topo."
        )
        st.metric(
            "Duração do pior DD (trades)",
            f"{int(stats.get('dd_len', 0))}",
            help="Quantidade de trades do último topo até o fundo do pior drawdown (sem recuperação)."
        )
    st.caption("Leitura profissional: risco saudável mantém drawdown controlado e evita longos períodos abaixo do topo da curva.")

with tab_eff:
    e1, e2, e3 = st.columns(3)
    with e1:
        st.metric("Total de Trades", f"{stats['trades']}")
        st.metric("Trades por Mês", f"{stats['trades_med_por_mes']:.2f}")
    with e2:
        st.metric("Total de Meses", f"{int(stats.get('total_meses', 0))}")
        st.metric("% Meses Positivos", pct(pct_meses_pos))
    with e3:
        st.metric("Maior sequência de Wins", f"{stats['max_consecutive_wins']}")
        st.metric("Maior sequência de Meses Positivos", f"{stats['max_pos_month_streak']}")
    st.caption("Leitura profissional: eficiência operacional combina produtividade sustentável e estabilidade de resultados ao longo do tempo.")

with tab_cost:
    k1, k2, k3 = st.columns(3)
    with k1:
        st.metric("Total de Custos", br_money(total_costs))
    with k2:
        st.metric("Custo Médio por Trade", br_money(custos_por_trade))
    with k3:
        st.metric("Custo Médio Mensal", br_money(custos_por_mes))
    st.metric("Custos / |P&L|", f"{cost_ratio:.2%}")
    st.caption("Leitura profissional: custos devem ficar proporcionais ao resultado; aumento de giro sem ganho marginal reduz eficiência líquida.")

with st.expander("📘 Como interpretar o Cockpit Quant (método e fórmulas)"):
    st.markdown(
        "Este cockpit consolida 4 pilares (Consistência, Eficiência, Risco e Custos) em um índice geral de 0 a 10."
    )
    st.markdown(
        "As notas usam um modelo inteligente: leitura não linear dos indicadores + ajuste de confiabilidade para tamanho de amostra (trades e meses)."
    )
    st.markdown(
        "- **Consistência**: combina Win Rate e percentual de meses positivos.\n"
        "- **Eficiência**: combina Payoff, Expectativa por trade e produtividade (trades/mês).\n"
        "- **Risco**: penaliza drawdown percentual alto, sequências longas de perdas e longos períodos underwater.\n"
        "- **Custos**: penaliza custo/trade alto e custo desproporcional ao P&L."
    )
    ref_df = pd.DataFrame(
        [
            {"Indicador": "Win Rate", "Excelente": ">= 58%", "Bom": ">= 47%", "Atenção": "< 47%"},
            {"Indicador": "Payoff", "Excelente": ">= 2.00", "Bom": ">= 1.40", "Atenção": "< 1.40"},
            {"Indicador": "Drawdown (%)", "Excelente": "<= 8%", "Bom": "<= 14%", "Atenção": "> 14%"},
            {"Indicador": "Custos / |P&L|", "Excelente": "<= 12%", "Bom": "<= 25%", "Atenção": "> 25%"},
        ]
    )
    st.dataframe(ref_df, use_container_width=True, hide_index=True)
    st.caption(
        "Observação: as faixas são referência operacional para leitura rápida e não substituem validação estatística do setup."
    )

# ====== Formatação para as tabelas ======
def _fmt_money_cols(df, extra_money_cols=None):
    d = df.copy()
    if d.empty: return d
    percent_cols = {"win_rate", "Win Rate", "winrate", "WinRate",
                    "% Meses Positivos", "% Tempo no Mercado"}
    for col in d.columns:
        if str(col) in percent_cols:
            d[col] = d[col].map(pct)
    money_like = {
        "pnl_sum", "pnl_mean", "expectancy", "avg_win", "avg_loss",
        "P&L Total", "P&L Médio", "P&L (R$)", "Expectativa", "P&L"
    }
    if extra_money_cols:
        money_like.update(extra_money_cols)
    for col in d.columns:
        name = str(col)
        if (name in money_like) or ("P&L" in name):
            try: d[col] = d[col].map(br_money)
            except Exception: pass
    for cand in ["payoff", "Payoff"]:
        if cand in d.columns:
            d[cand] = d[cand].map(lambda x: f"{float(x):.2f}" if pd.notna(x) else "")
    return d

# =============================== Leaderboards de Setups ===============================
st.markdown("---")
_centered_heading("🏆 Relatório de Performance do Robô", level=2)
_centered_heading("Resumo Visual", level=3)
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Saldo Total", f"{br_money(sim['pnl_used'].sum())}", help="Lucro/prejuízo total do período")
with col2:
    st.metric("Payoff", f"{sim['pnl_used'][sim['pnl_used']>0].mean()/abs(sim['pnl_used'][sim['pnl_used']<0].mean()):.2f}", help="Relação média entre ganhos e perdas")
with col3:
    st.metric("Win Rate", f"{(sim['pnl_used']>0).mean():.1%}", help="Percentual de trades positivos")
st.markdown("---")
_centered_heading("Leaderboards de Setups 🥇🥈🥉", level=3)

def _agg_setup(df):
    g = df.groupby("setup", as_index=False).agg(
        trades=("pnl_used","count"),
        pnl_sum=("pnl_used","sum"),
        pnl_mean=("pnl_used","mean"),
        win_rate=("pnl_used", lambda s: (s>0).mean()),
        avg_win=("pnl_used", lambda s: s[s>0].mean() if (s>0).any() else 0.0),
        avg_loss=("pnl_used", lambda s: s[s<0].mean() if (s<0).any() else 0.0),
    )
    g["payoff"] = g.apply(lambda r: (r["avg_win"]/abs(r["avg_loss"])) if r["avg_loss"]<0 else 0.0, axis=1)
    g["expectancy"] = g["win_rate"]*g["avg_win"] + (1-g["win_rate"])*g["avg_loss"]
    g = g.sort_values(["pnl_sum","trades"], ascending=[False, False], kind="mergesort").reset_index(drop=True)
    medalhas = ["🥇", "🥈", "🥉"]
    g["Medalha"] = ""
    for i in range(min(3, len(g))):
        g.at[i, "Medalha"] = medalhas[i]
    return g

def _render_rank_chart(data: pd.DataFrame, category_col: str, container, title: str, label: str, sort_desc: bool = True):
    with container:
        st.subheader(title)
        if data.empty:
            st.info("Sem dados suficientes para gerar o grafico.")
            return
        chart_df = data.copy()
        chart_df[label] = chart_df[category_col].astype(str)
        chart_df["pnl_plot"] = chart_df["pnl_sum"].astype(float)
        sort_order = "-x" if sort_desc else "x"
        chart = alt.Chart(chart_df).mark_bar(cornerRadiusEnd=6).encode(
            x=alt.X("pnl_plot:Q", title="P&L (R$)"),
            y=alt.Y(f"{label}:N", sort=sort_order, title=label),
            color=alt.condition(alt.datum.pnl_plot >= 0, alt.value(ALT_POS_COLOR), alt.value(ALT_NEG_COLOR)),
            tooltip=[
                alt.Tooltip(f"{label}:N", title=label),
                alt.Tooltip("pnl_plot:Q", title="P&L (R$)", format=",.2f"),
                alt.Tooltip("trades:Q", title="Trades")
            ]
        )
        st.altair_chart(_style_altair_chart(chart, height=320), use_container_width=True)

def _build_equity_curve_chart(
    df: pd.DataFrame,
    x_col: str,
    x_type: str,
    x_title: str,
    equity_baseline: float = 0.0,
    area_opacity: float = 0.16,
):
    def _interp(v0, v1, frac: float):
        try:
            return v0 + (v1 - v0) * frac
        except Exception:
            return v1

    try:
        baseline_value = float(equity_baseline)
    except Exception:
        baseline_value = 0.0
    if not np.isfinite(baseline_value):
        baseline_value = 0.0

    def _sign_label(curr: float, prev: float | None = None) -> str:
        curr_rel = curr - baseline_value
        if curr_rel > 0:
            return "Positivo"
        if curr_rel < 0:
            return "Negativo"
        if prev is not None:
            prev_rel = prev - baseline_value
            if prev_rel > 0:
                return "Positivo"
            if prev_rel < 0:
                return "Negativo"
        return "Positivo"

    def _fmt_pct_or_nd(v: float) -> str:
        return pct(v) if np.isfinite(v) else "n/d"

    if df.empty:
        return alt.Chart(pd.DataFrame(columns=[x_col, "Equity", "Data"]))

    work = df.copy()
    work["Equity"] = pd.to_numeric(work["Equity"], errors="coerce")
    work = work.dropna(subset=[x_col, "Equity"]).copy()
    work = work.sort_values(x_col, kind="mergesort").reset_index(drop=True)
    if work.empty:
        return alt.Chart(pd.DataFrame(columns=[x_col, "Equity", "Data"]))

    eq_values = work["Equity"].astype(float).to_numpy()
    first_equity = baseline_value
    peak_idx = int(np.argmax(eq_values))
    peak_value = float(eq_values[peak_idx])
    peak_base = abs(first_equity)
    peak_pct = ((peak_value - first_equity) / peak_base) if peak_base > 1e-9 else np.nan

    running_peaks = np.empty_like(eq_values, dtype=float)
    running_peak_idx = np.zeros(len(eq_values), dtype=int)
    curr_peak = -np.inf
    curr_peak_idx = 0
    for i, val in enumerate(eq_values):
        if val > curr_peak:
            curr_peak = val
            curr_peak_idx = i
        running_peaks[i] = curr_peak
        running_peak_idx[i] = curr_peak_idx

    dd_values = running_peaks - eq_values
    dd_trough_idx = int(np.argmax(dd_values))
    dd_peak_idx = int(running_peak_idx[dd_trough_idx])
    max_dd_value = float(dd_values[dd_trough_idx])
    dd_peak_value = float(running_peaks[dd_trough_idx])
    dd_pct = (max_dd_value / abs(dd_peak_value)) if abs(dd_peak_value) > 1e-9 else 0.0
    dd_trough_value = float(eq_values[dd_trough_idx])

    peak_label = f"Topo: {br_money(peak_value)} ({_fmt_pct_or_nd(peak_pct)})"
    dd_label = f"Drawdown max: {br_money(-max_dd_value)} ({pct(-dd_pct)})"

    def _row_data_value(i: int):
        return work.iloc[i]["Data"] if "Data" in work.columns else work.iloc[i][x_col]

    records = []
    seg_id = 0
    prev_x = None
    prev_y = None
    prev_data = None

    for _, row in work.iterrows():
        x_val = row[x_col]
        y_val = float(row["Equity"])
        d_val = row["Data"] if "Data" in work.columns else x_val

        if prev_x is None:
            records.append(
                {
                    x_col: x_val,
                    "Equity": y_val,
                    "Data": d_val,
                    "Sinal": _sign_label(y_val),
                    "Segment": seg_id,
                }
            )
            prev_x, prev_y, prev_data = x_val, y_val, d_val
            continue

        prev_rel = prev_y - baseline_value
        curr_rel = y_val - baseline_value
        sign_changed = (prev_rel > 0 and curr_rel < 0) or (prev_rel < 0 and curr_rel > 0)
        if sign_changed:
            frac = abs(prev_rel) / (abs(prev_rel) + abs(curr_rel))
            x_zero = _interp(prev_x, x_val, frac)
            d_zero = _interp(prev_data, d_val, frac)

            records.append(
                {
                    x_col: x_zero,
                    "Equity": baseline_value,
                    "Data": d_zero,
                    "Sinal": _sign_label(baseline_value, prev_y),
                    "Segment": seg_id,
                }
            )
            seg_id += 1
            records.append(
                {
                    x_col: x_zero,
                    "Equity": baseline_value,
                    "Data": d_zero,
                    "Sinal": _sign_label(baseline_value, y_val),
                    "Segment": seg_id,
                }
            )

        records.append(
            {
                x_col: x_val,
                "Equity": y_val,
                "Data": d_val,
                "Sinal": _sign_label(y_val, prev_y),
                "Segment": seg_id,
            }
        )
        prev_x, prev_y, prev_data = x_val, y_val, d_val

    plot_df = pd.DataFrame(records)
    plot_df["_ord"] = np.arange(len(plot_df))

    area_base = alt.Chart(plot_df).encode(
        x=alt.X(f"{x_col}:{x_type}", title=x_title),
        detail=alt.Detail("Segment:N"),
        order=alt.Order("_ord:Q"),
        tooltip=[
            alt.Tooltip("Data:T", title="Data"),
            alt.Tooltip("Equity:Q", title="Equity (R$)", format=",.2f"),
        ],
    )
    area_pos = area_base.transform_filter(alt.datum.Sinal == "Positivo").mark_area(
        opacity=area_opacity,
        color=ALT_EQ_POS_COLOR,
    ).encode(
        y=alt.Y("Equity:Q", title="Equity (R$)"),
        y2=alt.value(baseline_value),
    )
    area_neg = area_base.transform_filter(alt.datum.Sinal == "Negativo").mark_area(
        opacity=area_opacity,
        color=ALT_EQ_NEG_COLOR,
    ).encode(
        y=alt.Y("Equity:Q", title="Equity (R$)"),
        y2=alt.value(baseline_value),
    )

    line = alt.Chart(plot_df).mark_line(strokeWidth=2.6).encode(
        x=alt.X(f"{x_col}:{x_type}", title=x_title),
        y=alt.Y("Equity:Q", title="Equity (R$)", stack=None),
        detail=alt.Detail("Segment:N"),
        order=alt.Order("_ord:Q"),
        color=alt.Color(
            "Sinal:N",
            legend=None,
            scale=alt.Scale(
                domain=["Positivo", "Negativo"],
                range=[ALT_EQ_POS_COLOR, ALT_EQ_NEG_COLOR],
            ),
        ),
        tooltip=[
            alt.Tooltip("Data:T", title="Data"),
            alt.Tooltip("Equity:Q", title="Equity (R$)", format=",.2f"),
        ],
    )

    peak_df = pd.DataFrame(
        [
            {
                x_col: work.iloc[peak_idx][x_col],
                "Equity": peak_value,
                "Data": _row_data_value(peak_idx),
                "Label": peak_label,
            }
        ]
    )
    dd_df = pd.DataFrame(
        [
            {
                x_col: work.iloc[dd_trough_idx][x_col],
                "Equity": dd_trough_value,
                "Data": _row_data_value(dd_trough_idx),
                "Label": dd_label,
            }
        ]
    )

    peak_point = alt.Chart(peak_df).mark_point(size=90, filled=True, color=ALT_POS_COLOR).encode(
        x=alt.X(f"{x_col}:{x_type}", title=x_title),
        y=alt.Y("Equity:Q", title="Equity (R$)"),
        tooltip=[
            alt.Tooltip("Data:T", title="Data"),
            alt.Tooltip("Equity:Q", title="Topo (R$)", format=",.2f"),
            alt.Tooltip("Label:N", title="Resumo"),
        ],
    )
    peak_text = alt.Chart(peak_df).mark_text(
        align="left",
        baseline="bottom",
        dx=8,
        dy=-8,
        color="#CFE0F7",
        fontSize=12,
        fontWeight=600,
    ).encode(
        x=alt.X(f"{x_col}:{x_type}", title=x_title),
        y=alt.Y("Equity:Q", title="Equity (R$)"),
        text=alt.Text("Label:N"),
    )

    dd_point = alt.Chart(dd_df).mark_point(size=90, filled=True, color=ALT_NEG_COLOR).encode(
        x=alt.X(f"{x_col}:{x_type}", title=x_title),
        y=alt.Y("Equity:Q", title="Equity (R$)"),
        tooltip=[
            alt.Tooltip("Data:T", title="Data"),
            alt.Tooltip("Equity:Q", title="Fundo DD (R$)", format=",.2f"),
            alt.Tooltip("Label:N", title="Resumo"),
        ],
    )
    dd_text = alt.Chart(dd_df).mark_text(
        align="left",
        baseline="top",
        dx=8,
        dy=8,
        color="#FFD3DA",
        fontSize=12,
        fontWeight=600,
    ).encode(
        x=alt.X(f"{x_col}:{x_type}", title=x_title),
        y=alt.Y("Equity:Q", title="Equity (R$)"),
        text=alt.Text("Label:N"),
    )

    chart = area_pos + area_neg + line + peak_point + peak_text + dd_point + dd_text

    if max_dd_value > 0 and dd_peak_idx != dd_trough_idx:
        dd_segment_df = pd.DataFrame(
            [
                {
                    x_col: work.iloc[dd_peak_idx][x_col],
                    "Equity": float(eq_values[dd_peak_idx]),
                    "Data": _row_data_value(dd_peak_idx),
                },
                {
                    x_col: work.iloc[dd_trough_idx][x_col],
                    "Equity": dd_trough_value,
                    "Data": _row_data_value(dd_trough_idx),
                },
            ]
        )
        dd_segment = alt.Chart(dd_segment_df).mark_line(
            strokeWidth=1.9,
            strokeDash=[7, 4],
            color=ALT_NEG_COLOR,
        ).encode(
            x=alt.X(f"{x_col}:{x_type}", title=x_title),
            y=alt.Y("Equity:Q", title="Equity (R$)"),
            tooltip=[
                alt.Tooltip("Data:T", title="Data"),
                alt.Tooltip("Equity:Q", title="Equity (R$)", format=",.2f"),
            ],
        )
        chart = chart + dd_segment

    return chart.interactive()

if "setup" in sim.columns and sim["setup"].astype(str).str.len().gt(0).any():
    base = sim.copy()
    base["setup"] = base["setup"].astype(str).str.upper().replace({"NONE":"", "NAN":""})
    base = base[base["setup"].str.len() > 0]
    if base.empty:
        st.info("Sem coluna 'setup' preenchida para montar os leaderboards de setups.")
    else:
        g_setup = _agg_setup(base)
        setup_view_mode = st.radio("Visualização", ["Leaderboards", "Gráficos"], index=0, horizontal=True, key="setup_view_mode")
        if setup_view_mode == "Leaderboards":
            colA, colB = st.columns(2)
            with colA:
                st.subheader("Geral - Melhores Setups 🥇")
                best_setup = g_setup.head(10).copy()
                best_setup["Setup"] = best_setup.apply(lambda r: f"{r['setup']} {r['Medalha']}" if r["Medalha"] else r["setup"], axis=1)
                best_setup_view = best_setup.rename(columns={
                    "trades":"Trades","pnl_sum":"P&L Total",
                    "pnl_mean":"P&L Médio","win_rate":"Win Rate","payoff":"Payoff","expectancy":"Expectativa",
                    "avg_win":"avg_win","avg_loss":"avg_loss"
                })[["Setup","Trades","P&L Total","P&L Médio","Win Rate","Payoff","Expectativa","avg_win","avg_loss"]]
                st.dataframe(_fmt_money_cols(best_setup_view), use_container_width=True)
                st.caption("🥇 Medalha de ouro para o melhor setup. Clique nas colunas para ordenar.")
            with colB:
                st.subheader("Geral - Piores Setups 😬")
                worst = g_setup.sort_values("pnl_sum", ascending=True).head(10).copy()
                worst["Setup"] = worst["setup"]
                worst_view = worst.rename(columns={
                    "trades":"Trades","pnl_sum":"P&L Total",
                    "pnl_mean":"P&L Médio","win_rate":"Win Rate","payoff":"Payoff","expectancy":"Expectativa",
                    "avg_win":"avg_win","avg_loss":"avg_loss"
                })[["Setup","Trades","P&L Total","P&L Médio","Win Rate","Payoff","Expectativa","avg_win","avg_loss"]]
                st.dataframe(_fmt_money_cols(worst_view), use_container_width=True)
                st.caption("😬 Setups com pior resultado. Use para evitar estratégias ruins.")
        else:
            colA, colB = st.columns(2)
            _render_rank_chart(g_setup.head(10), "setup", colA, "Geral - Melhores Setups", "Setup", sort_desc=True)
            worst_chart = g_setup.sort_values("pnl_sum", ascending=True).head(10)
            _render_rank_chart(worst_chart, "setup", colB, "Geral - Piores Setups", "Setup", sort_desc=False)
else:
    st.info("Sem coluna 'setup' preenchida para montar os leaderboards de setups.")
# =============================== Leaderboard de Ativos ===============================
st.markdown("---")
_centered_heading("Leaderboard de Ativos 💹", level=3)
if "symbol" in sim.columns and "pnl_used" in sim.columns:
    by_sym = (sim.groupby("symbol", as_index=False)
                .agg(
                    trades=("pnl_used","count"),
                    pnl_sum=("pnl_used","sum"),
                    pnl_mean=("pnl_used","mean"),
                    win_rate=("pnl_used", lambda s: (s>0).mean()),
                    avg_win=("pnl_used", lambda s: s[s>0].mean() if (s>0).any() else 0.0),
                    avg_loss=("pnl_used", lambda s: s[s<0].mean() if (s<0).any() else 0.0),
                 ))
    by_sym["payoff"] = by_sym.apply(lambda r: (r["avg_win"]/abs(r["avg_loss"])) if r["avg_loss"]<0 else 0.0, axis=1)
    by_sym["expectancy"] = by_sym["win_rate"]*by_sym["avg_win"] + (1-by_sym["win_rate"])*by_sym["avg_loss"]

    medalhas = ["🥇", "🥈", "🥉"]
    top10 = by_sym.sort_values(["pnl_sum","trades"], ascending=[False, False]).head(10).copy()
    top10["Medalha"] = ""
    for i in range(min(3, len(top10))):
        top10.at[top10.index[i], "Medalha"] = medalhas[i]
    # Concatenate medal to asset name
    top10["Ativo"] = top10.apply(lambda r: f"{r['symbol']} {r['Medalha']}" if r["Medalha"] else r["symbol"], axis=1)
    top10_view = top10.rename(columns={
        "trades":"Trades","pnl_sum":"P&L Total",
        "pnl_mean":"P&L Médio","win_rate":"Win Rate","payoff":"Payoff","expectancy":"Expectativa",
        "avg_win":"avg_win","avg_loss":"avg_loss"
    })[["Ativo","Trades","P&L Total","P&L Médio","Win Rate","Payoff","Expectativa","avg_win","avg_loss"]]

    low10 = by_sym.sort_values(["pnl_sum","trades"],  ascending=[True,  False]).head(10).copy()
    # Remove medal assignment for worst assets
    low10["Ativo"] = low10["symbol"]
    low10_view = low10.rename(columns={
        "trades":"Trades","pnl_sum":"P&L Total",
        "pnl_mean":"P&L Médio","win_rate":"Win Rate","payoff":"Payoff","expectancy":"Expectativa",
        "avg_win":"avg_win","avg_loss":"avg_loss"
    })[["Ativo","Trades","P&L Total","P&L Médio","Win Rate","Payoff","Expectativa","avg_win","avg_loss"]]

    asset_view_mode = st.radio("Visualização", ["Leaderboards", "Gráficos"], index=0, horizontal=True, key="asset_view_mode")
    if asset_view_mode == "Leaderboards":
        col_sym_a, col_sym_b = st.columns(2)
        with col_sym_a:
            st.subheader("Melhores Ativos (Top 10) 🥇")
            st.dataframe(_fmt_money_cols(top10_view), use_container_width=True)
            st.caption("🥇 Medalha de ouro para o melhor ativo. Clique nas colunas para ordenar.")
        with col_sym_b:
            st.subheader("Piores Ativos (Bottom 10) 😬")
            st.dataframe(_fmt_money_cols(low10_view), use_container_width=True)
            st.caption("😬 Ativos com pior resultado. Use para evitar ativos ruins.")
    else:
        col_sym_a, col_sym_b = st.columns(2)
        _render_rank_chart(top10, "symbol", col_sym_a, "Melhores Ativos (Top 10)", "Ativo", sort_desc=True)
        _render_rank_chart(low10, "symbol", col_sym_b, "Piores Ativos (Bottom 10)", "Ativo", sort_desc=False)
else:
    st.info("Não há colunas 'symbol' e 'pnl_used' para montar o leaderboard de ativos.")
# =============================== Curvas macro ===============================
st.markdown("---")
_centered_heading("Curvas Macro 📈", level=3)
hide_gaps_macro = st.checkbox("Ocultar períodos sem trades (comprimir eixo X) — Curva Macro", value=False)
st.caption("Curva de equity do robô ao longo do tempo. Verde claro = acima do capital inicial e vermelho claro = abaixo do capital inicial.")

eq_df = sim[["sort_time","equity"]].dropna().rename(columns={"sort_time":"Data","equity":"Equity"})
if not eq_df.empty:
    if hide_gaps_macro:
        eq_view = eq_df.reset_index(drop=True).copy()
        eq_view["Idx"] = eq_view.index + 1
        ch = _build_equity_curve_chart(
            eq_view,
            x_col="Idx",
            x_type="Q",
            x_title="Pontos da série (sem lacunas)",
            equity_baseline=float(st.session_state.get("initial_capital", 0.0)),
            area_opacity=0.16,
        )
    else:
        ch = _build_equity_curve_chart(
            eq_df,
            x_col="Data",
            x_type="T",
            x_title="Data",
            equity_baseline=float(st.session_state.get("initial_capital", 0.0)),
            area_opacity=0.16,
        )
    st.altair_chart(_style_altair_chart(ch, height=320), use_container_width=True)

# Barra mensal
ym = sim.copy()
ym["year_month"] = _to_naive_series(ym["sort_time"]).dt.to_period("M").astype(str)
pm = ym.groupby("year_month", as_index=False)["pnl_used"].sum().rename(columns={"year_month":"Mes","pnl_used":"P&L (R$)"})
st.markdown("---")
pnl_chart_mode = st.radio("P&L agregado por", ["Mensal", "Por trade"], index=0, horizontal=True)
st.caption("Visualize o P&L agregado por mês ou por trade.")

if pnl_chart_mode == "Mensal":
    if pm.empty:
        st.info("Sem dados mensais para montar o grafico de P&L.")
    else:
        bar = alt.Chart(pm).mark_bar(cornerRadiusTopLeft=6, cornerRadiusTopRight=6).encode(
            x=alt.X('Mes:O', title='Mes'),
            y=alt.Y('P&L (R$):Q', title='P&L (R$)'),
            color=alt.condition(alt.datum["P&L (R$)"] >= 0, alt.value(ALT_POS_COLOR), alt.value(ALT_NEG_COLOR)),
            tooltip=[alt.Tooltip('Mes:O', title='Mes'), alt.Tooltip('P&L (R$):Q', format=",.2f")]
        )
        st.altair_chart(_style_altair_chart(bar, height=320, title="P&L Mensal"), use_container_width=True)
        st.caption("Barra verde = mês positivo, vermelha = mês negativo.")
else:
    trades_chart = sim.copy() if ("pnl_used" in sim.columns) else pd.DataFrame()
    if trades_chart.empty:
        st.info("Sem trades para montar o grafico de P&L por trade.")
    else:
        if "sort_time" in trades_chart.columns:
            trades_chart = trades_chart.sort_values("sort_time")
            trades_chart["Data"] = pd.to_datetime(trades_chart["sort_time"], errors="coerce")
        trades_chart = trades_chart.reset_index(drop=True)
        trades_chart["Trade"] = np.arange(1, len(trades_chart) + 1)
        trades_chart["P&L (R$)"] = pd.to_numeric(trades_chart["pnl_used"], errors="coerce").fillna(0.0)
        tooltip_cols = [alt.Tooltip("Trade:O"), alt.Tooltip("P&L (R$):Q", format=",.2f")]
        if "Data" in trades_chart.columns:
            tooltip_cols.append(alt.Tooltip("Data:T", title="Data"))
        if "qtd_acoes" in trades_chart.columns:
            trades_chart["Qtd"] = pd.to_numeric(trades_chart["qtd_acoes"], errors="coerce").fillna(0.0)
            tooltip_cols.append(alt.Tooltip("Qtd:Q", title="Qtd ações", format=",.2f"))
        elif "volume" in trades_chart.columns:
            trades_chart["Qtd"] = pd.to_numeric(trades_chart["volume"], errors="coerce").fillna(0.0)
            tooltip_cols.append(alt.Tooltip("Qtd:Q", title="Qtd ações", format=",.2f"))
        if "capital_alocado" in trades_chart.columns:
            trades_chart["CapAloc (R$)"] = pd.to_numeric(trades_chart["capital_alocado"], errors="coerce").fillna(0.0)
            tooltip_cols.append(alt.Tooltip("CapAloc (R$):Q", format=",.2f"))
        bar = alt.Chart(trades_chart).mark_bar(cornerRadiusTopLeft=5, cornerRadiusTopRight=5).encode(
            x='Trade:O', y='P&L (R$):Q',
            color=alt.condition(alt.datum["P&L (R$)"] >= 0, alt.value(ALT_POS_COLOR), alt.value(ALT_NEG_COLOR)),
            tooltip=tooltip_cols
        )
        st.altair_chart(_style_altair_chart(bar, height=260, title="P&L por Trade"), use_container_width=True)
        st.caption("Barra verde = trade positivo, vermelha = trade negativo.")
st.markdown("---")
_centered_heading("Exportar Relatório 📤", level=3)
if st.button("Exportar para CSV"):
    sim.to_csv("relatorio_trades.csv", index=False)
    st.success("Relatório exportado como relatorio_trades.csv!")

# =============================== Curva 2 (filtros próprios) ===============================
_centered_heading("📈 Curva de Capital (Filtrável por Ativos e Setups)", level=2)
cur_col1, cur_col2, cur_col3 = st.columns(3)

all_symbols = sorted(trades_raw["symbol"].astype(str).dropna().unique().tolist()) if "symbol" in trades_raw.columns else []
if "setup" in trades_raw.columns:
    all_setups = (trades_raw["setup"].astype(str).str.upper().replace({"NONE":"","NAN":""}).dropna())
    all_setups = sorted([s for s in all_setups.unique().tolist() if s.strip() != ""])
else:
    all_setups = []

with cur_col1:
    st.session_state["curve2_symbols"] = st.multiselect(
        "Ativo(s) (Curva 2)",
        options=all_symbols,
        default=[s for s in st.session_state.get("curve2_symbols", []) if s in all_symbols],
        help="Vazio = não restringe por ativo."
    )
with cur_col2:
    st.session_state["curve2_setups"] = st.multiselect(
        "Setup(s) (Curva 2)",
        options=all_setups,
        default=[s for s in st.session_state.get("curve2_setups", []) if s in all_setups],
        help="Vazio = não restringe por setup."
    )
with cur_col3:
    st.session_state["curve2_initial_capital"] = st.number_input(
        "Capital inicial (Curva 2) (R$)",
        value=float(st.session_state.get("curve2_initial_capital", st.session_state.get("initial_capital", 0.0))),
        step=100.0, format="%.2f",
        help="Capital próprio da Curva 2 para comparação com a Curva Macro."
    )

hide_gaps_curve2 = st.checkbox("Ocultar períodos sem trades (comprimir eixo X) — Curva 2", value=False)

def _apply_curve2_filters(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    sel_syms = st.session_state.get("curve2_symbols", [])
    if sel_syms and "symbol" in d.columns:
        d = d[d["symbol"].astype(str).isin(sel_syms)]
    sel_sets = st.session_state.get("curve2_setups", [])
    if sel_sets and "setup" in d.columns:
        d = d[d["setup"].astype(str).str.upper().isin([s.upper() for s in sel_sets])]
    return d

curve2_base = _apply_curve2_filters(trades_limited_base.copy())
curve2_base = apply_max_concurrent_limit(curve2_base, max_open=max_open)

if curve2_base.empty:
    st.info("Curva 2: nenhum trade após filtros.")
else:
    if source_is_mt5_magic:
        c2_work = _prepare_mt5_faithful_trades(
            curve2_base,
            initial_capital=float(st.session_state["curve2_initial_capital"]),
        )
    elif scenario_mode == "Original (volume do arquivo)":
        c2_work = apply_costs(
            curve2_base,
            b3_pct=fee_b3_pct_effective,
            brok_in_fixed=st.session_state["fee_broker_in"],
            brok_out_fixed=st.session_state["fee_broker_out"],
            base_pct=st.session_state["base_pct"]
        )
        if "pnl_net" in c2_work.columns:
            c2_work["pnl_used"] = c2_work["pnl_net"]
        elif "profit" in c2_work.columns:
            c2_work["pnl_used"] = c2_work["profit"]
        elif "pnl" in c2_work.columns:
            c2_work["pnl_used"] = c2_work["pnl"]
        else:
            c2_work["pnl_used"] = 0.0
        c2_work = _annotate_capital_path(c2_work, initial_capital=st.session_state["curve2_initial_capital"])
    else:
        c2_work, c2_meta = simulate_capital_scenario(
            curve2_base,
            initial_capital=float(st.session_state["curve2_initial_capital"]),
            scenario_mode=scenario_mode,
            first_trade_value=float(st.session_state.get("capital_first_trade_value", 1000.0)),
            fixed_trade_value=float(st.session_state.get("capital_fixed_value", 1000.0)),
            pct_entry=float(st.session_state.get("capital_pct_entry", 10.0)),
            pct_reapply=bool(st.session_state.get("capital_pct_reapply", True)),
            qty_integer=bool(st.session_state.get("capital_qty_integer", True)),
            qty_min=float(st.session_state.get("capital_qty_min", 1.0)),
            qty_step=float(st.session_state.get("capital_qty_step", 1.0)),
            b3_pct=float(fee_b3_pct_effective),
            brok_in_fixed=float(st.session_state["fee_broker_in"]),
            brok_out_fixed=float(st.session_state["fee_broker_out"]),
            base_pct=str(st.session_state["base_pct"]),
        )
        if c2_work.empty:
            st.info("Curva 2: cenário não executou trades com esse capital/filtro.")
            c2_work = pd.DataFrame()
        elif c2_meta.get("skipped", 0) > 0:
            st.caption(
                f"Curva 2 ({scenario_mode}): {c2_meta['executed']} executados / {c2_meta['rows_total']}."
            )

    if c2_work.empty:
        st.info("Curva 2: sem dados para plotar.")
    else:
        c2 = simulate_equity(c2_work, initial_capital=st.session_state["curve2_initial_capital"])
        eq2 = c2[["sort_time","equity"]].dropna().rename(columns={"sort_time":"Data","equity":"Equity"})
        if eq2.empty:
            st.info("Curva 2: sem dados para plotar.")
        else:
            if hide_gaps_curve2:
                eq2_view = eq2.reset_index(drop=True).copy()
                eq2_view["Idx"] = eq2_view.index + 1
                ch2 = _build_equity_curve_chart(
                    eq2_view,
                    x_col="Idx",
                    x_type="Q",
                    x_title="Pontos da série (sem lacunas)",
                    equity_baseline=float(st.session_state.get("curve2_initial_capital", 0.0)),
                    area_opacity=0.15,
                )
            else:
                ch2 = _build_equity_curve_chart(
                    eq2,
                    x_col="Data",
                    x_type="T",
                    x_title="Data",
                    equity_baseline=float(st.session_state.get("curve2_initial_capital", 0.0)),
                    area_opacity=0.15,
                )
            st.altair_chart(_style_altair_chart(ch2, height=320), use_container_width=True)

# =============================== Trade Viewer (candles) ===============================
_centered_heading("🔍 Gráfico de um Trade (Candles)", level=2)

if "selected_trade_sim_idx" not in st.session_state:
    st.session_state["selected_trade_sim_idx"] = None

view_candidates = sim.copy().reset_index(drop=True)
for c in ["entry_time","exit_time"]:
    if c not in view_candidates.columns:
        view_candidates[c] = pd.NaT
view_candidates = view_candidates[(view_candidates["entry_time"].notna()) | (view_candidates["exit_time"].notna())]
view_candidates = view_candidates.reset_index(drop=True)

def _trade_label(row):
    lab = []
    if "symbol" in row: lab.append(str(row["symbol"]))
    if "entry_time" in row and pd.notna(row["entry_time"]):
        lab.append(str(pd.to_datetime(row["entry_time"]).strftime("%Y-%m-%d %H:%M")))
    qty_v = row.get("qtd_acoes", row.get("volume", np.nan))
    try:
        qty_f = float(qty_v)
        if np.isfinite(qty_f) and qty_f > 0:
            lab.append(f"Qtd {qty_f:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    except Exception:
        pass
    cap_v = row.get("capital_alocado", np.nan)
    try:
        cap_f = float(cap_v)
        if np.isfinite(cap_f) and cap_f > 0:
            lab.append(f"Cap {br_money(cap_f)}")
    except Exception:
        pass
    if "pnl_used" in row: lab.append(f"P&L {br_money(row['pnl_used'])}")
    return " | ".join(lab) if lab else str(row.get("sim_idx","?"))

def _pos_from_sim_idx(sim_idx_val):
    if sim_idx_val is None: return 0
    hits = view_candidates.index[view_candidates["sim_idx"] == sim_idx_val].tolist()
    return hits[0] if hits else 0

colg1, colg2, colg3, colg4 = st.columns(4)
with colg1:
    tf_options = ["D1", "H1", "M15"]
    tf_choice = st.selectbox("Timeframe", options=tf_options, index=0)
with colg2:
    pre_bars = st.number_input("Candles ANTES", min_value=0, value=20, step=5)
with colg3:
    post_bars = st.number_input("Candles DEPOIS", min_value=0, value=20, step=5)
with colg4:
    src_choice = st.selectbox("Fonte OHLC", options=["MT5","CSV manual"], index=0 if HAS_MT5 else 1)

labels = [_trade_label(row) for _, row in view_candidates.iterrows()]

force_sync_trade_idx = False
pending_trade = st.session_state.get("pending_trade_sim_idx", None)
if pending_trade is not None:
    try:
        st.session_state["selected_trade_sim_idx"] = int(pending_trade)
    except Exception:
        st.session_state["selected_trade_sim_idx"] = None
    st.session_state["pending_trade_sim_idx"] = None
    force_sync_trade_idx = True

idx_default = _pos_from_sim_idx(st.session_state["selected_trade_sim_idx"])

if labels:
    if ("select_trade_idx" not in st.session_state) or force_sync_trade_idx:
        st.session_state["select_trade_idx"] = idx_default
    else:
        try:
            cur_idx = int(st.session_state.get("select_trade_idx", idx_default))
        except Exception:
            cur_idx = idx_default
        cur_idx = max(0, min(cur_idx, len(labels) - 1))
        st.session_state["select_trade_idx"] = cur_idx
else:
    st.info("Sem trades com horário para exibir no Trade Viewer.")
    st.stop()

idx_sel = st.selectbox("Selecione o trade", options=list(range(len(labels))),
                       format_func=lambda i: labels[i], index=idx_default, key="select_trade_idx")
st.session_state["selected_trade_sim_idx"] = int(view_candidates.iloc[idx_sel]["sim_idx"])

trow = view_candidates.iloc[idx_sel]
sym = str(trow.get("symbol", ""))
entry_t = pd.to_datetime(trow.get("entry_time"))
exit_t  = pd.to_datetime(trow.get("exit_time"))
entry_px = float(trow.get("price_open") if pd.notna(trow.get("price_open")) else np.nan)
exit_px  = float(trow.get("price_close") if pd.notna(trow.get("price_close")) else np.nan)
stop_init = trow.get("stop_price", np.nan)
stop_init = float(stop_init) if pd.notna(stop_init) else np.nan

colm1, colm2 = st.columns(2)
with colm1:
    if np.isnan(entry_px):
        entry_px = st.number_input("Preço de ENTRADA (manual)", min_value=0.0, value=0.0, step=0.01, format="%.2f")
with colm2:
    show_tr_only = st.checkbox("Linhas apenas no período do trade", value=True)

# ====== Obter candles — janela baseada em pre/post e timeframe ======
ohlc_df = pd.DataFrame()
if src_choice == "MT5":
    if not HAS_MT5:
        st.error("MT5 indisponível; use CSV manual para candles.")
    elif not sym:
        st.error("Trade sem símbolo.")
    else:
        ref_start = entry_t if pd.notna(entry_t) else (exit_t - pd.Timedelta(days=5))
        ref_end   = exit_t  if pd.notna(exit_t)  else (entry_t + pd.Timedelta(days=5))
        sec_per_bar = _tf_to_seconds(tf_choice)
        pre_sec  = max(pre_bars, 5)  * sec_per_bar * 1.2
        post_sec = max(post_bars, 5) * sec_per_bar * 1.2
        start_fetch = (ref_start - pd.Timedelta(seconds=pre_sec)).to_pydatetime()
        end_fetch   = (ref_end   + pd.Timedelta(seconds=post_sec)).to_pydatetime()
        start_fetch = min(start_fetch, (ref_start - timedelta(days=5)))
        end_fetch   = max(end_fetch, (ref_end + timedelta(days=5)))
        ohlc_df = fetch_mt5_ohlc(sym, tf_choice, start_fetch, end_fetch)
else:
    lib = _ensure_ohlc_lib()
    st.markdown(f"#### OHLC por CSV para {sym or '(símbolo do trade)'}")
    uploaded_ohlc = upload_ohlc_csv(symbol_hint=sym or "sym")
    if uploaded_ohlc is not None and sym:
        lib[sym] = uploaded_ohlc
    if sym in lib:
        ohlc_df = lib[sym].copy()
    else:
        st.warning("Ainda não há OHLC carregado para este símbolo.")

if ohlc_df is not None and not ohlc_df.empty and pd.notna(entry_t):
    ohlc_df = ohlc_df.sort_values("time")

    def nearest_idx(ts):
        if pd.isna(ts):
            return None
        return int((np.abs(ohlc_df["time"].values.astype("datetime64[ns]") - np.datetime64(ts))).argmin())

    idx_entry = nearest_idx(entry_t)
    idx_exit  = nearest_idx(exit_t) if pd.notna(exit_t) else idx_entry

    start_i = max(0, (idx_entry or 0) - int(pre_bars))
    end_i   = min(len(ohlc_df)-1, (idx_exit or 0) + int(post_bars))
    win = ohlc_df.iloc[start_i:end_i+1].copy()

    # ====== Plotly Candlestick ======
    dark_theme = bool(st.session_state.get("tv_dark_theme", True))
    default_bg = "#0B1220" if dark_theme else "#FFFFFF"
    bg_color = st.session_state.get("tv_bg_color", default_bg)
    if dark_theme and str(bg_color).strip().lower() in {"#fff", "#ffffff", "white"}:
        bg_color = default_bg
    paper_bg = "#060B16" if dark_theme else bg_color
    up_color = "#22C55E" if dark_theme else "#2E8B57"
    down_color = "#EF4444" if dark_theme else "#B22222"
    font_color = "#E2E8F0" if dark_theme else "#111827"
    axis_line = "rgba(148,163,184,0.55)" if dark_theme else "rgba(75,85,99,0.45)"
    grid_color = "rgba(148,163,184,0.22)" if dark_theme else "rgba(75,85,99,0.16)"
    ann_bg = "rgba(2,11,26,0.78)" if dark_theme else "rgba(255,255,255,0.50)"
    plotly_template = "plotly_dark" if dark_theme else "plotly_white"
    spike_color = "rgba(148,163,184,0.65)" if dark_theme else "rgba(75,85,99,0.55)"

    fig = go.Figure(
        data=[
            go.Candlestick(
                x=win["time"],
                open=win["open"],
                high=win["high"],
                low=win["low"],
                close=win["close"],
                name=f"{sym} {tf_choice}",
                increasing=dict(line=dict(color=up_color, width=1.2), fillcolor="rgba(34,197,94,0.62)"),
                decreasing=dict(line=dict(color=down_color, width=1.2), fillcolor="rgba(239,68,68,0.62)"),
            )
        ]
    )

    # Preços efetivos
    entry_px_eff = float(entry_px) if (not np.isnan(entry_px) and entry_px > 0) else np.nan
    exit_px_eff  = float(exit_px)  if (not np.isnan(exit_px)  and exit_px  > 0) else np.nan
    entry_is_approx = False
    exit_is_approx  = False
    if (np.isnan(entry_px_eff) or entry_px_eff <= 0) and pd.notna(entry_t) and idx_entry is not None:
        entry_px_eff = float(ohlc_df.iloc[idx_entry]["close"]); entry_is_approx = True
    if (np.isnan(exit_px_eff) or exit_px_eff <= 0) and pd.notna(exit_t) and idx_exit is not None:
        exit_px_eff = float(ohlc_df.iloc[idx_exit]["close"]); exit_is_approx = True

    # Marcadores
    markers_x = []; markers_y = []; markers_text = []
    if pd.notna(entry_t) and (not np.isnan(entry_px_eff)) and entry_px_eff > 0:
        markers_x.append(entry_t); markers_y.append(entry_px_eff)
        markers_text.append("Entrada (aprox.)" if entry_is_approx else "Entrada")
    if pd.notna(exit_t) and (not np.isnan(exit_px_eff)) and exit_px_eff > 0:
        markers_x.append(exit_t); markers_y.append(exit_px_eff)
        markers_text.append("Saída (aprox.)" if exit_is_approx else "Saída")

    if markers_x:
        fig.add_trace(go.Scatter(
            x=markers_x, y=markers_y, mode="markers+text", text=markers_text, textposition="top center",
            marker=dict(
                size=10,
                color=st.session_state["tv_marker_color"],
                symbol="x",
                line=dict(width=1, color="#0B1220" if dark_theme else "#FFFFFF"),
            ),
        ))

    # Linhas horizontais (limitadas ao período do trade, se marcado)
    def _add_hline_y(y, name, color, x0=None, x1=None):
        fig.add_shape(
            type="line", xref="x", yref="y",
            x0=x0 if x0 is not None else win["time"].iloc[0],
            x1=x1 if x1 is not None else win["time"].iloc[-1],
            y0=y, y1=y, line=dict(color=color, width=1.5, dash="dash")
        )
        fig.add_annotation(x=(x0 if x0 is not None else win["time"].iloc[0]),
                           y=y, xanchor="left", yanchor="bottom", showarrow=False,
                           text=name, font=dict(size=10, color=color), bgcolor=ann_bg)

    x0_line = entry_t if (show_tr_only and pd.notna(entry_t)) else None
    x1_line = exit_t  if (show_tr_only and pd.notna(exit_t))  else None

    if (not np.isnan(entry_px_eff)) and entry_px_eff > 0:
        _add_hline_y(entry_px_eff, "Entrada" + (" (aprox.)" if entry_is_approx else ""), st.session_state["tv_entry_line_color"], x0=x0_line, x1=x1_line)

    # STOP INICIAL: só se existir e for válido (>0)
    if ("stop_price" in trow.index) and (not np.isnan(stop_init)) and stop_init > 0:
        _add_hline_y(stop_init, "Stop inicial", st.session_state["tv_stop_line_color"], x0=x0_line, x1=x1_line)

    if (not np.isnan(exit_px_eff)) and exit_px_eff > 0 and pd.notna(exit_t):
        _add_hline_y(exit_px_eff, "Saída" + (" (aprox.)" if exit_is_approx else ""), st.session_state["tv_exit_line_color"], x0=x0_line, x1=x1_line)

    # Rangebreaks: tira fins de semana
    rangebreaks = [dict(bounds=["sat", "mon"])]
    try:
        win_days = win["time"].dt.normalize()
        start_day = win_days.iloc[0]; end_day = win_days.iloc[-1]
        biz = pd.bdate_range(start_day, end_day)
        present = set(pd.to_datetime(win_days.unique()))
        holidays_missing = [pd.Timestamp(d).to_pydatetime() for d in biz if pd.Timestamp(d) not in present]
        if holidays_missing:
            rangebreaks.append(dict(values=holidays_missing))
    except Exception:
        pass
    fig.update_xaxes(rangebreaks=rangebreaks)

    # Setup no título
    setup_str = str(trow.get("setup", "") or "").upper()
    title_suffix = f" • Setup: {setup_str}" if setup_str else ""
    if setup_str:
        fig.add_annotation(
            x=win["time"].iloc[0], y=win["high"].max(),
            xref="x", yref="y", text=f"Setup: {setup_str}",
            showarrow=False, xanchor="left", yanchor="top",
            bgcolor=ann_bg, font=dict(size=12, color=font_color)
        )

    fig.update_layout(
        template=plotly_template,
        title=f"{sym} - Trade selecionado ({tf_choice}){title_suffix}",
        xaxis_title="Tempo",
        yaxis_title="Preço",
        xaxis_rangeslider_visible=False,
        height=560,
        margin=dict(l=10,r=10,t=40,b=10),
        plot_bgcolor=bg_color,
        paper_bgcolor=paper_bg,
        font=dict(color=font_color, size=12),
        hoverlabel=dict(
            bgcolor="rgba(2,11,26,0.94)" if dark_theme else "rgba(248,250,252,0.95)",
            font=dict(color=font_color),
        ),
        hovermode="x unified",
        dragmode="pan",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1.0,
            bgcolor="rgba(0,0,0,0)",
        ),
    )
    fig.update_xaxes(
        rangebreaks=rangebreaks,
        showgrid=True,
        gridcolor=grid_color,
        gridwidth=1,
        showline=True,
        linecolor=axis_line,
        tickfont=dict(color=font_color),
        title_font=dict(color=font_color),
        ticks="outside",
        tickcolor=axis_line,
        ticklen=6,
        zeroline=False,
        showspikes=True,
        spikecolor=spike_color,
        spikethickness=1,
        spikedash="dot",
        spikemode="across",
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor=grid_color,
        gridwidth=1,
        showline=True,
        linecolor=axis_line,
        tickfont=dict(color=font_color),
        title_font=dict(color=font_color),
        ticks="outside",
        tickcolor=axis_line,
        ticklen=6,
        zeroline=False,
        tickformat=",.2f",
        showspikes=True,
        spikecolor=spike_color,
        spikethickness=1,
        spikedash="dot",
        spikemode="across",
    )
    pnl_value = trow.get("pnl_used", np.nan)
    try:
        pnl_value = float(pnl_value)
    except (TypeError, ValueError):
        pnl_value = np.nan

    if np.isnan(pnl_value):
        vol_val = trow.get("volume", np.nan)
        contract_size = trow.get("contract_size", 1.0)
        try:
            vol_val = float(vol_val)
        except (TypeError, ValueError):
            vol_val = np.nan
        try:
            contract_size = float(contract_size)
        except (TypeError, ValueError):
            contract_size = 1.0
        if not np.isnan(entry_px_eff) and not np.isnan(exit_px_eff) and not np.isnan(vol_val):
            if np.isnan(contract_size) or contract_size <= 0:
                contract_size = 1.0
            pnl_value = (exit_px_eff - entry_px_eff) * vol_val * contract_size

    pct_result = np.nan
    if not np.isnan(entry_px_eff) and entry_px_eff != 0 and not np.isnan(exit_px_eff):
        pct_result = ((exit_px_eff - entry_px_eff) / entry_px_eff) * 100.0

    result_text = []
    qty_trade = trow.get("qtd_acoes", trow.get("volume", np.nan))
    try:
        qty_trade = float(qty_trade)
    except (TypeError, ValueError):
        qty_trade = np.nan
    capital_alocado_trade = trow.get("capital_alocado", np.nan)
    try:
        capital_alocado_trade = float(capital_alocado_trade)
    except (TypeError, ValueError):
        capital_alocado_trade = np.nan
    if np.isnan(capital_alocado_trade) or capital_alocado_trade <= 0:
        cs_val = trow.get("contract_size", 1.0)
        try:
            cs_val = float(cs_val)
        except (TypeError, ValueError):
            cs_val = 1.0
        if np.isnan(cs_val) or cs_val <= 0:
            cs_val = 1.0
        if not np.isnan(entry_px_eff) and not np.isnan(qty_trade):
            capital_alocado_trade = abs(entry_px_eff) * abs(qty_trade) * cs_val

    capital_before_trade = trow.get("capital_before", np.nan)
    capital_after_trade = trow.get("capital_after", np.nan)
    try:
        capital_before_trade = float(capital_before_trade)
    except (TypeError, ValueError):
        capital_before_trade = np.nan
    try:
        capital_after_trade = float(capital_after_trade)
    except (TypeError, ValueError):
        capital_after_trade = np.nan

    cap_m1, cap_m2, cap_m3 = st.columns(3)
    with cap_m1:
        if not np.isnan(capital_before_trade):
            st.metric("Capital antes do trade", br_money(capital_before_trade))
    with cap_m2:
        if not np.isnan(capital_alocado_trade):
            st.metric("Cap. aplicado", br_money(capital_alocado_trade))
    with cap_m3:
        if not np.isnan(capital_after_trade):
            st.metric("Capital após o trade", br_money(capital_after_trade))

    def _fmt_px(v: float) -> str:
        if np.isnan(v):
            return "-"
        txt = f"{float(v):,.5f}".rstrip("0").rstrip(".")
        return txt.replace(",", "X").replace(".", ",").replace("X", ".")

    if not np.isnan(pnl_value):
        result_text.append(f"P&L: {br_money(pnl_value)}")
    if not np.isnan(pct_result):
        result_text.append(f"{pct_result:+.2f}%")
    if not np.isnan(entry_px_eff):
        result_text.append(f"PxE: {_fmt_px(entry_px_eff)}")
    if not np.isnan(exit_px_eff):
        result_text.append(f"PxS: {_fmt_px(exit_px_eff)}")
    if not np.isnan(qty_trade):
        qty_txt = f"{qty_trade:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        result_text.append(f"Qtd ações: {qty_txt}")
    if not np.isnan(capital_alocado_trade):
        result_text.append(f"CapAloc: {br_money(capital_alocado_trade)}")
    result_text = " | ".join(result_text)


    def _rgba_from_hex(hex_color: str, alpha: float) -> str:
        hex_color = str(hex_color or "#000000").lstrip("#")
        if len(hex_color) == 3:
            hex_color = "".join(ch * 2 for ch in hex_color)
        try:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
        except ValueError:
            r = g = b = 0
        alpha = min(max(alpha, 0.0), 1.0)
        return f"rgba({r},{g},{b},{alpha:.2f})"

    wm_manual = str(st.session_state.get("tv_watermark_text", "") or "").strip()
    auto_watermark = str(sym or "").strip().upper()
    watermark_text = wm_manual if wm_manual else auto_watermark
    if watermark_text:
        wm_opacity = float(st.session_state.get("tv_watermark_opacity", 0.60))
        wm_color = _rgba_from_hex(st.session_state.get("tv_watermark_color", "#000000"), wm_opacity)
        fig.add_annotation(
            text=watermark_text,
            xref="paper", yref="paper",
            x=0.5, y=0.5,
            showarrow=False, xanchor="center", yanchor="middle",
            font=dict(size=64, color=wm_color),
            opacity=min(max(wm_opacity, 0.0), 1.0)
        )

    if result_text:
        fig.add_annotation(
            text=result_text,
            xref="paper", yref="paper",
            x=1.0, y=1.0,
            showarrow=False, xanchor="right", yanchor="top",
            bgcolor="rgba(2,11,26,0.75)" if dark_theme else "rgba(0,0,0,0.55)",
            font=dict(size=14, color=font_color if dark_theme else "#FFFFFF"),
            borderpad=6
        )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.info("Sem OHLC para plotar. Use MT5 ou carregue CSV de OHLC (e garanta que o trade tenha horário de entrada/saída).")

# =============================== Top Trades (com botões “Ver gráfico”) ===============================
_centered_heading("Top Trades (clique em Ver gráfico para abrir no viewer acima)", level=3)

# Garante colunas para evitar KeyError
for colname in ["entry_time","exit_time"]:
    if colname not in sim.columns:
        sim[colname] = pd.NaT

df_top = sim[["sim_idx","symbol","setup","entry_time","exit_time","pnl_used"]].copy()
if "qtd_acoes" in sim.columns:
    df_top["qtd_acoes"] = sim["qtd_acoes"]
elif "volume" in sim.columns:
    df_top["qtd_acoes"] = sim["volume"]
else:
    df_top["qtd_acoes"] = np.nan
if "capital_alocado" in sim.columns:
    df_top["capital_alocado"] = sim["capital_alocado"]
else:
    df_top["capital_alocado"] = np.nan

def _fmt_table_row(r):
    simbolo = str(r.get("symbol",""))
    setup   = str(r.get("setup",""))
    ent     = str(r.get("entry_time",""))
    pnlv    = r.get("pnl_used", 0.0)
    qtdv    = r.get("qtd_acoes", np.nan)
    capv    = r.get("capital_alocado", np.nan)
    try:
        qtd_txt = f"{float(qtdv):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        qtd_txt = "-"
    try:
        cap_txt = br_money(float(capv))
    except Exception:
        cap_txt = "-"
    return simbolo, setup, ent, qtd_txt, cap_txt, br_money(pnlv)

best = df_top.sort_values("pnl_used", ascending=False).head(10).reset_index(drop=True)
worst = df_top.sort_values("pnl_used", ascending=True).head(10).reset_index(drop=True)
colb, colw = st.columns(2)

with colb:
    st.subheader("Melhores")
    if best.empty:
        st.info("Sem dados.")
    else:
        cols = st.columns([4,2,2,2,3])
        cols[0].markdown("**Trade**"); cols[1].markdown("**Qtd**"); cols[2].markdown("**Capital**"); cols[3].markdown("**P&L**"); cols[4].markdown("**Ação**")
        for _, r in best.iterrows():
            c1,c2,c3,c4,c5 = st.columns([4,2,2,2,3])
            simbolo, setup, ent, qtd_txt, cap_txt, pnl_txt = _fmt_table_row(r)
            c1.write(f"{simbolo} | {setup} | {ent}")
            c2.write(qtd_txt)
            c3.write(cap_txt)
            c4.write(pnl_txt)
            if c5.button("🔍 Ver gráfico", key=f"btn_best_{int(r['sim_idx'])}"):
                target_sim_idx = int(r["sim_idx"])
                st.session_state["selected_trade_sim_idx"] = target_sim_idx
                st.session_state["pending_trade_sim_idx"] = target_sim_idx
                st.rerun()

with colw:
    st.subheader("Piores")
    if worst.empty:
        st.info("Sem dados.")
    else:
        cols = st.columns([4,2,2,2,3])
        cols[0].markdown("**Trade**"); cols[1].markdown("**Qtd**"); cols[2].markdown("**Capital**"); cols[3].markdown("**P&L**"); cols[4].markdown("**Ação**")
        for _, r in worst.iterrows():
            c1,c2,c3,c4,c5 = st.columns([4,2,2,2,3])
            simbolo, setup, ent, qtd_txt, cap_txt, pnl_txt = _fmt_table_row(r)
            c1.write(f"{simbolo} | {setup} | {ent}")
            c2.write(qtd_txt)
            c3.write(cap_txt)
            c4.write(pnl_txt)
            if c5.button("🔍 Ver gráfico", key=f"btn_worst_{int(r['sim_idx'])}"):
                target_sim_idx = int(r["sim_idx"])
                st.session_state["selected_trade_sim_idx"] = target_sim_idx
                st.session_state["pending_trade_sim_idx"] = target_sim_idx
                st.rerun()

# =============================== Exportações ===============================
_centered_heading("Exportar", level=3)
by_symbol_full = (sim.groupby("symbol", as_index=False)
                    .agg(trades=("pnl_used","count"), pnl_sum=("pnl_used","sum"), pnl_mean=("pnl_used","mean"))
                 ) if "symbol" in sim.columns else pd.DataFrame()
by_month_full = pm.rename(columns={"Mes":"year_month","P&L (R$)":"pnl_sum"}) if 'pm' in locals() and not pm.empty else pd.DataFrame()

by_setup_full = pd.DataFrame()
if "setup" in sim.columns and sim["setup"].astype(str).str.len().gt(0).any():
    tmp = sim.copy()
    tmp["setup"] = tmp["setup"].astype(str).str.upper().replace({"NONE":"","NAN":""})
    tmp = tmp[tmp["setup"].str.len() > 0]
    if not tmp.empty:
        by_setup_full = (tmp.groupby("setup", as_index=False)
                           .agg(trades=("pnl_used","count"),
                                pnl_sum=("pnl_used","sum"),
                                pnl_mean=("pnl_used","mean")))

# --- ADICIONE / SUBSTITUA ESTES HELPERS (perto dos outros utils) ---
from datetime import datetime as _dt_datetime

def _excel_sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Garante que o DataFrame não tenha nenhum datetime com timezone (nem escondido em colunas 'object'),
    nem Period nem categorias que atrapalhem o Excel.
    """
    d = df.copy()

    # 1) Trata colunas datetime nativas (com tz)
    for c in d.columns:
        try:
            if pd.api.types.is_datetime64tz_dtype(d[c].dtype):
                d[c] = pd.to_datetime(d[c], errors="coerce").dt.tz_convert("UTC").dt.tz_localize(None)
            elif pd.api.types.is_datetime64_any_dtype(d[c].dtype):
                # já é datetime "naive" -> garante coerção
                d[c] = pd.to_datetime(d[c], errors="coerce")
        except Exception:
            pass

    # 2) Trata colunas 'object' que podem conter datetimes tz-aware misturados
    for c in d.columns:
        if d[c].dtype == "object":
            def _strip_obj(x):
                # pandas Timestamp
                if isinstance(x, pd.Timestamp):
                    if x.tz is not None:
                        return x.tz_convert("UTC").tz_localize(None).to_pydatetime()
                    return x.to_pydatetime()
                # datetime do Python
                if isinstance(x, _dt_datetime):
                    if x.tzinfo is not None:
                        # converte para UTC e remove tz
                        try:
                            return x.astimezone(timezone.utc).replace(tzinfo=None)
                        except Exception:
                            return x.replace(tzinfo=None)
                    return x
                # Period -> string
                if isinstance(x, pd.Period):
                    return str(x)
                return x
            try:
                d[c] = d[c].map(_strip_obj)
            except Exception:
                pass

        # 3) Period dtype (inteiro) -> string
        if pd.api.types.is_period_dtype(d[c]):
            try:
                d[c] = d[c].astype(str)
            except Exception:
                pass

        # 4) Categorical -> string (evita surpresa no Excel)
        if pd.api.types.is_categorical_dtype(d[c].dtype):
            d[c] = d[c].astype(str)

    # 5) Index com tz?
    if isinstance(d.index, pd.DatetimeIndex):
        if d.index.tz is not None:
            d.index = d.index.tz_convert("UTC").tz_localize(None)

    return d


# --- SUBSTITUA sua função to_excel_report POR ESTA VERSÃO ---
def to_excel_report(sim, by_symbol, by_month, best, worst, by_setup=None):
    try:
        importlib.import_module("xlsxwriter")
    except ImportError:
        st.warning("📊 Biblioteca xlsxwriter não está instalada. Download do Excel não está disponível.")
        return None
    buf = io.BytesIO()

    # Sanitiza TUDO antes de escrever
    sim_x       = _excel_sanitize_df(sim if isinstance(sim, pd.DataFrame) else pd.DataFrame(sim))
    by_symbol_x = _excel_sanitize_df(by_symbol) if isinstance(by_symbol, pd.DataFrame) else pd.DataFrame()
    by_month_x  = _excel_sanitize_df(by_month)  if isinstance(by_month,  pd.DataFrame) else pd.DataFrame()
    best_x      = _excel_sanitize_df(best)      if isinstance(best,      pd.DataFrame) else pd.DataFrame()
    worst_x     = _excel_sanitize_df(worst)     if isinstance(worst,     pd.DataFrame) else pd.DataFrame()
    by_setup_x  = _excel_sanitize_df(by_setup)  if isinstance(by_setup,  pd.DataFrame) else pd.DataFrame()

    with pd.ExcelWriter(buf, engine="xlsxwriter", datetime_format="dd/mm/yyyy HH:MM", date_format="dd/mm/yyyy") as w:
        sim_x.to_excel(w, sheet_name="Trades", index=False)
        if not by_symbol_x.empty: by_symbol_x.to_excel(w, sheet_name="Por_Ativo", index=False)
        if not by_month_x.empty:  by_month_x.to_excel(w, sheet_name="Por_Mes", index=False)
        if not by_setup_x.empty:  by_setup_x.to_excel(w, sheet_name="Por_Setup", index=False)
        if not best_x.empty:      best_x.to_excel(w, sheet_name="Top_Best", index=False)
        if not worst_x.empty:     worst_x.to_excel(w, sheet_name="Top_Worst", index=False)

    buf.seek(0)
    return buf

best_full = sim.sort_values("pnl_used", ascending=False).head(20)
worst_full = sim.sort_values("pnl_used", ascending=True).head(20)
excel_buf = to_excel_report(sim, by_symbol_full, by_month_full, best_full, worst_full, by_setup=by_setup_full)
if excel_buf is not None:
    st.download_button("📥 Baixar Excel", data=excel_buf.getvalue(), file_name="Relatorio_Pos_Trade.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
csv_out = _tz_naive_df(sim).to_csv(index=False).encode("utf-8-sig")
st.download_button("📄 Exportar Trades (CSV)", data=csv_out, file_name="Trades_Filtrados.csv", mime="text/csv")


# =============================== Relatório de Lucratividade — Ativos ===============================
_centered_heading("✅ Relatório de Lucratividade — Ativos (para conta real)", level=3)

# 1) Base: tenta reaproveitar 'by_sym' do Leaderboard; se não existir, reconstrói a partir de 'sim'
def _build_by_symbol_from_sim(_sim: pd.DataFrame) -> pd.DataFrame:
    df = _sim.copy()
    if df is None or df.empty or "symbol" not in df.columns:
        return pd.DataFrame()
    # Garantir pnl_used
    if "pnl_used" not in df.columns:
        if "pnl_net" in df.columns: df["pnl_used"] = df["pnl_net"]
        elif "profit" in df.columns: df["pnl_used"] = df["profit"]
        elif "pnl" in df.columns:    df["pnl_used"] = df["pnl"]
        else:                        df["pnl_used"] = 0.0
    g = df.groupby("symbol", as_index=False).agg(
        trades   =("pnl_used","count"),
        pnl_sum  =("pnl_used","sum"),
        pnl_mean =("pnl_used","mean"),
        win_rate =("pnl_used", lambda s: (s>0).mean() if len(s)>0 else 0.0),
        avg_win  =("pnl_used", lambda s: s[s>0].mean() if (s>0).any() else 0.0),
        avg_loss =("pnl_used", lambda s: s[s<0].mean() if (s<0).any() else 0.0),
    )
    g["payoff"] = g.apply(lambda r: (r["avg_win"]/abs(r["avg_loss"])) if r["avg_loss"]<0 else 0.0, axis=1)
    g["expectancy"] = g["win_rate"]*g["avg_win"] + (1-g["win_rate"])*g["avg_loss"]
    return g

try:
    _sym = by_sym.copy() if ("by_sym" in locals() and isinstance(by_sym, pd.DataFrame) and not by_sym.empty) else _build_by_symbol_from_sim(sim if 'sim' in locals() else pd.DataFrame())
except Exception:
    _sym = _build_by_symbol_from_sim(sim if 'sim' in locals() else pd.DataFrame())

if _sym is None or _sym.empty:
    st.info("Sem dados suficientes para o relatório de lucratividade por ativo.")
else:
    # 2) Controles: modo, perfil e robustez
    c0, c1, c2, c3, c4 = st.columns([1.6,1.2,0.9,0.9,1.4])
    with c0:
        lucr_mode = st.selectbox(
            "Modo de Operáveis",
            ["Padrão (Lucrativos)", "Escolher critério"],
            index=0, key="lucr_mode",
            help="Se você não escolher nada, uso o modo Padrão: marca como operável se P&L Total > 0 e Expectativa ≥ 0."
        )
    with c1:
        crit_lucr = st.selectbox(
            "Critério (quando escolher)",
            ["P&L Total > 0", "Expectativa ≥ 0"],
            index=0, key="lucr_crit",
            disabled=(st.session_state.get("lucr_mode", "Padrão (Lucrativos)") != "Escolher critério")
        )
    with c2:
        min_tr = st.number_input("Mín. de trades por ativo", min_value=1, value=10, step=1, key="lucr_mintr")
    with c3:
        top_preview = st.number_input("Pré-visualizar até N por tabela", min_value=10, value=20, step=5, key="lucr_preview_n")
    with c4:
        perfil = st.selectbox(
            "Perfil",
            ["Nenhum (manual)", "Conservador", "Balanceado", "Agressivo"],
            index=0, key="lucr_perfil",
            help="Escolha um perfil para aplicar filtros adicionais (WR, Payoff, PF)."
        )

    # Perfis → thresholds
    _perfil_cfg = {
        "Conservador": {"min_tr": 30, "min_wr": 0.55, "min_pay": 1.3, "min_pf": 1.3},
        "Balanceado":  {"min_tr": 20, "min_wr": 0.50, "min_pay": 1.4, "min_pf": 1.2},
        "Agressivo":   {"min_tr": 15, "min_wr": 0.45, "min_pay": 1.6, "min_pf": 1.0},
    }
    _pcfg = _perfil_cfg.get(st.session_state.get("lucr_perfil", "Nenhum (manual)"), None)

    with st.expander("Ajustes avançados do perfil (opcional)"):
        # Defaults vindos do perfil (se existir) ou do estado atual
        _min_tr_prof = _pcfg["min_tr"] if _pcfg else int(st.session_state.get("lucr_mintr_prof", max(10, int(st.session_state.get('lucr_mintr', 10)))))
        _min_wr_prof = _pcfg["min_wr"] if _pcfg else float(st.session_state.get("lucr_minwr_prof", 0.0))
        _min_pay_prof = _pcfg["min_pay"] if _pcfg else float(st.session_state.get("lucr_minpay_prof", 0.0))
        _min_pf_prof = _pcfg["min_pf"] if _pcfg else float(st.session_state.get("lucr_minpf_prof", 0.0))

        colp1, colp2, colp3, colp4 = st.columns(4)
        with colp1:
            min_tr_prof = st.number_input("Perfil: mín. trades", min_value=1, value=int(_min_tr_prof), step=1, key="lucr_mintr_prof")
        with colp2:
            min_wr_prof = st.number_input("Perfil: WR mínimo (%)", min_value=0.0, max_value=100.0, value=float(_min_wr_prof*100 if _pcfg else _min_wr_prof), step=1.0, key="lucr_minwr_prof_ui")
        with colp3:
            min_pay_prof = st.number_input("Perfil: Payoff mínimo", min_value=0.0, value=float(_min_pay_prof), step=0.1, format="%.1f", key="lucr_minpay_prof")
        with colp4:
            min_pf_prof = st.number_input("Perfil: PF mínimo", min_value=0.0, value=float(_min_pf_prof), step=0.1, format="%.1f", key="lucr_minpf_prof")

        # Normaliza WR (%) → fração
        if _pcfg:
            st.session_state["lucr_minwr_prof"] = min_wr_prof/100.0
        else:
            # se usuário ajustou manualmente, respeita o valor direto como fração se <=1, ou % se >1
            st.session_state["lucr_minwr_prof"] = (min_wr_prof/100.0) if min_wr_prof>1 else min_wr_prof

    base_l = _sym.copy()
    base_l = base_l[base_l["trades"] >= int(min_tr)].copy()

    # Calcula Profit Factor (PF) a partir de agregados
    # PF = (wr * avg_win) / ((1-wr) * |avg_loss|)
    import numpy as _np
    denom = (1.0 - base_l["win_rate"]) * (base_l["avg_loss"].abs().replace(0, _np.nan))
    base_l["pf"] = _np.where(denom>0, (base_l["win_rate"] * base_l["avg_win"]) / denom, _np.nan)

    # Modo padrão: marca operável se P&L > 0 E Expectativa ≥ 0
    if st.session_state.get("lucr_mode", "Padrão (Lucrativos)") == "Padrão (Lucrativos)":
        base_l["lucrativo"] = (base_l["pnl_sum"] > 0) & (base_l["expectancy"] >= 0)
        # Ordenação dá preferência a P&L e, em seguida, nº de trades
        order_good = ["pnl_sum","expectancy","trades"]
        asc_good   = [False, False, False]
        order_bad  = ["pnl_sum","expectancy","trades"]
        asc_bad = [True, True, False]
    else:
        # Modo escolhido pelo usuário
        if crit_lucr == "P&L Total > 0":
            base_l["lucrativo"] = base_l["pnl_sum"] > 0
            order_good = ["pnl_sum","trades"]
            asc_good   = [False, False]
            order_bad  = ["pnl_sum","trades"]
            asc_bad = [True, False]
        else:
            base_l["lucrativo"] = base_l["expectancy"] >= 0
            order_good = ["expectancy","trades"]
            asc_good   = [False, False]
            order_bad  = ["expectancy","trades"]
            asc_bad = [True, False]

    # Aplica filtros de PERFIL, se houver
    _perfil = st.session_state.get("lucr_perfil", "Nenhum (manual)")
    if _perfil != "Nenhum (manual)":
        _min_tr_prof = int(st.session_state.get("lucr_mintr_prof", 10))
        _min_wr_prof = float(st.session_state.get("lucr_minwr_prof", 0.0))
        _min_pay_prof = float(st.session_state.get("lucr_minpay_prof", 0.0))
        _min_pf_prof = float(st.session_state.get("lucr_minpf_prof", 0.0))

        # Usa o maior entre o min_tr manual e o do perfil
        min_tr_use = max(int(min_tr), _min_tr_prof)
        base_l = base_l[base_l["trades"] >= min_tr_use].copy()

        # Aplica WR, Payoff e PF mínimos
        base_l = base_l[
            (base_l["win_rate"] >= _min_wr_prof) &
            (base_l["payoff"]   >= _min_pay_prof) &
            (base_l["pf"].fillna(0) >= _min_pf_prof)
        ].copy()

        st.caption(f"Perfil aplicado: {_perfil} — min_tr={min_tr_use}, WR≥{_min_wr_prof:.0%}, Payoff≥{_min_pay_prof:.1f}, PF≥{_min_pf_prof:.1f}")

    operaveis = base_l[base_l["lucrativo"]].sort_values(order_good, ascending=asc_good).copy()
    nao_oper  = base_l[~base_l["lucrativo"]].sort_values(order_bad,  ascending=asc_bad).copy()

    # 3) Tabelas lado a lado
    col_g, col_b = st.columns(2)
    rename_cols = {
        "symbol":"Ativo", "trades":"Trades", "pnl_sum":"P&L Total", "pnl_mean":"P&L Médio",
        "win_rate":"Win Rate", "payoff":"Payoff", "expectancy":"Expectativa"
    }

    with col_g:
        st.subheader(f"Operáveis (Lucrativos) — {len(operaveis)}")
        show_g = operaveis.rename(columns=rename_cols)
        st.dataframe(_fmt_money_cols(show_g).head(int(top_preview)), use_container_width=True)

    with col_b:
        st.subheader(f"Não-operáveis (Prejuízo) — {len(nao_oper)}")
        show_b = nao_oper.rename(columns=rename_cols)
        st.dataframe(_fmt_money_cols(show_b).head(int(top_preview)), use_container_width=True)

    # 4) Ações rápidas
    b1, b2, b3, b4 = st.columns(4)
    ops_list = operaveis["symbol"].astype(str).tolist()
    bad_list = nao_oper["symbol"].astype(str).tolist()

    with b1:
        if st.button("Aplicar Operáveis no filtro", key="btn_apply_operaveis"):
            st.session_state["filter_symbols"] = ops_list
            st.success(f"{len(ops_list)} ativo(s) aplicado(s) ao filtro.")
            st.rerun()
    with b2:
        if st.button("Salvar Operáveis como Conta Real", key="btn_save_operaveis_real"):
            st.session_state["real_symbols"] = ops_list
            st.success(f"Conta Real atualizada com {len(ops_list)} ativo(s).")
    with b3:
        csv_ops = pd.Series(ops_list, name="symbol").to_csv(index=False).encode("utf-8")
        st.download_button("Baixar Operáveis (CSV)", data=csv_ops, file_name="ativos_operaveis.csv",
                           mime="text/csv", key="dl_ops_csv")
    with b4:
        csv_bad = pd.Series(bad_list, name="symbol").to_csv(index=False).encode("utf-8")
        st.download_button("Baixar Não-operáveis (CSV)", data=csv_bad, file_name="ativos_nao_operaveis.csv",
                           mime="text/csv", key="dl_bad_csv")

    # 5) Rodapé: visão rápida da Conta Real atual
    _real = st.session_state.get("real_symbols", [])
    st.caption("Conta Real atual: " + (", ".join(_real) if _real else "(vazia)"))


