"""
PLAKSIK AI — Anti-Fraud Intelligence Platform
Streamlit Dashboard: запуск парсинга, аналитика, статистика, экспорт.
"""

import os
import re
import sys
import ast
import json
import time
import asyncio
import sqlite3
import threading
import subprocess
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="PLAKSIK AI",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# ГЛОБАЛЬНЫЕ СТИЛИ
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;900&family=JetBrains+Mono:wght@400;600&display=swap');

html, body, [class*="css"] {
    font-family: 'Inter', sans-serif;
}

/* Общий фон */
.stApp {
    background: #0a0d14;
    color: #e2e8f0;
}

/* Боковая панель */
section[data-testid="stSidebar"] {
    background: #0f1520 !important;
    border-right: 1px solid #1e2d45;
}

/* Убираем стандартный заголовок streamlit */
#MainMenu, footer, header { visibility: hidden; }

/* Метрические карточки */
.metric-card {
    background: linear-gradient(135deg, #111827 0%, #0f1520 100%);
    border: 1px solid #1e2d45;
    border-radius: 16px;
    padding: 24px;
    text-align: center;
    transition: all 0.3s ease;
    position: relative;
    overflow: hidden;
}
.metric-card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: var(--accent, #3b82f6);
}
.metric-card:hover {
    border-color: #2d4a6b;
    transform: translateY(-2px);
}
.metric-value {
    font-size: 2.4rem;
    font-weight: 900;
    line-height: 1;
    color: var(--accent, #3b82f6);
    letter-spacing: -0.02em;
}
.metric-label {
    font-size: 0.78rem;
    font-weight: 500;
    color: #64748b;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-top: 8px;
}
.metric-sub {
    font-size: 0.72rem;
    color: #475569;
    margin-top: 4px;
}

/* Hero заголовок */
.hero {
    text-align: center;
    padding: 40px 0 32px;
}
.hero-logo {
    font-size: 3.2rem;
    font-weight: 900;
    letter-spacing: -0.03em;
    background: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 50%, #06b6d4 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    line-height: 1;
}
.hero-sub {
    font-size: 0.9rem;
    color: #475569;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    margin-top: 8px;
    font-weight: 500;
}
.hero-badge {
    display: inline-block;
    background: rgba(59, 130, 246, 0.1);
    border: 1px solid rgba(59, 130, 246, 0.3);
    color: #60a5fa;
    padding: 4px 14px;
    border-radius: 20px;
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    font-weight: 600;
    margin-top: 12px;
}

/* Секционные заголовки */
.section-title {
    font-size: 1.05rem;
    font-weight: 700;
    color: #cbd5e1;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding-bottom: 12px;
    border-bottom: 1px solid #1e2d45;
    margin-bottom: 20px;
    display: flex;
    align-items: center;
    gap: 8px;
}

/* Карточка риска */
.risk-card {
    border-radius: 12px;
    padding: 16px 20px;
    margin-bottom: 10px;
    border-left: 4px solid;
    background: #111827;
}
.risk-card.critical {
    border-color: #ef4444;
    background: rgba(239, 68, 68, 0.06);
}
.risk-card.warning {
    border-color: #f59e0b;
    background: rgba(245, 158, 11, 0.06);
}
.risk-card.clean {
    border-color: #10b981;
    background: rgba(16, 185, 129, 0.06);
}

/* Бейджи */
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 6px;
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
}
.badge-critical { background: rgba(239,68,68,0.15); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
.badge-warning  { background: rgba(245,158,11,0.15); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
.badge-clean    { background: rgba(16,185,129,0.15); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
.badge-taxonomy { background: rgba(139,92,246,0.15); color: #a78bfa; border: 1px solid rgba(139,92,246,0.3); }
.badge-platform { background: rgba(6,182,212,0.15); color: #22d3ee; border: 1px solid rgba(6,182,212,0.3); }

/* Лог терминала */
.log-box {
    background: #05080f;
    border: 1px solid #1e2d45;
    border-radius: 12px;
    padding: 16px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    color: #4ade80;
    max-height: 320px;
    overflow-y: auto;
    line-height: 1.7;
}

/* Прогресс */
.stProgress > div > div { background: #1e2d45; }
.stProgress > div > div > div { background: linear-gradient(90deg, #3b82f6, #8b5cf6); }

/* Кнопки */
.stButton > button {
    background: linear-gradient(135deg, #3b82f6, #6366f1) !important;
    color: white !important;
    border: none !important;
    border-radius: 10px !important;
    font-weight: 600 !important;
    letter-spacing: 0.03em !important;
    transition: all 0.2s !important;
}
.stButton > button:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 8px 24px rgba(59,130,246,0.3) !important;
}

/* Input fields */
.stTextInput > div > div > input,
.stTextArea > div > div > textarea,
.stSelectbox > div > div > select,
.stNumberInput > div > div > input {
    background: #111827 !important;
    border: 1px solid #1e2d45 !important;
    color: #e2e8f0 !important;
    border-radius: 8px !important;
}

/* Таблица */
.dataframe { background: #111827 !important; }

/* Divider */
hr { border-color: #1e2d45 !important; }

/* Tabs */
.stTabs [data-baseweb="tab-list"] { background: #0f1520; border-bottom: 1px solid #1e2d45; }
.stTabs [data-baseweb="tab"] { color: #64748b; font-weight: 500; }
.stTabs [aria-selected="true"] { color: #3b82f6 !important; border-bottom-color: #3b82f6 !important; }

/* Expander */
.streamlit-expanderHeader { background: #111827 !important; border: 1px solid #1e2d45 !important; border-radius: 8px !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# ПУТИ ПРОЕКТА (берутся из config.py для полной синхронизации с main.py)
# ─────────────────────────────────────────────────────────────────────────────
from config import DB_PATH, KEYWORDS_FILE, CSV_EXPORT_PATH

PROJECT_ROOT     = Path(__file__).resolve().parent
ANALYTICS_JSON   = PROJECT_ROOT / "analytics_results.json"
KEYWORDS_CSV     = PROJECT_ROOT / "data" / "final_keywords.csv"
LOG_FILE         = PROJECT_ROOT / "plaksik_run.log"

SIMILARITY_THRESHOLD     = 0.85
CRITICAL_SCORE_THRESHOLD = 65.0
WARNING_SCORE_THRESHOLD  = 35.0

# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────────────────────
def init_state():
    defaults = {
        "parsing_running": False,
        "analytics_running": False,
        "log_lines": [],
        "run_pid": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

# ─────────────────────────────────────────────────────────────────────────────
# УТИЛИТЫ ДАННЫХ
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_data(ttl=15)
def load_db_records() -> pd.DataFrame:
    """Загружает все записи из SQLite."""
    if not DB_PATH.exists():
        return pd.DataFrame()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        df = pd.read_sql_query("SELECT * FROM media_analytics", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=15)
def load_analytics_json() -> dict:
    """Загружает результаты аналитики из JSON."""
    if not ANALYTICS_JSON.exists():
        return {}
    try:
        with open(ANALYTICS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def db_stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    return {
        "total": len(df),
        "tiktok": int((df["platform"] == "tiktok").sum()) if "platform" in df.columns else 0,
        "instagram": int((df["platform"] == "instagram").sum()) if "platform" in df.columns else 0,
        "avg_score": round(df["match_percentage"].mean(), 1) if "match_percentage" in df.columns else 0.0,
        "max_score": round(df["match_percentage"].max(), 1) if "match_percentage" in df.columns else 0.0,
        "total_views": int(df["views_count"].sum()) if "views_count" in df.columns else 0,
        "total_likes": int(df["likes_count"].sum()) if "likes_count" in df.columns else 0,
        "unique_authors": df["author_username"].nunique() if "author_username" in df.columns else 0,
    }


def read_log_tail(n: int = 80) -> List[str]:
    if not LOG_FILE.exists():
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("""
    <div style="text-align:center; padding: 20px 0 24px;">
        <div style="font-size:1.8rem; font-weight:900; background: linear-gradient(135deg,#3b82f6,#8b5cf6);
            -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;">
            🛡️ PLAKSIK AI
        </div>
        <div style="font-size:0.68rem; color:#475569; letter-spacing:0.15em; text-transform:uppercase; margin-top:4px;">
            Anti-Fraud Intelligence
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # Статус системы
    db_ok  = DB_PATH.exists()
    kw_ok  = KEYWORDS_FILE.exists()
    csv_ok = KEYWORDS_CSV.exists()

    st.markdown("**⚙️ Статус системы**")
    st.markdown(f"{'🟢' if db_ok else '🔴'} База данных {'найдена' if db_ok else 'не найдена'}")
    st.markdown(f"{'🟢' if kw_ok else '🔴'} keywords.txt {'OK' if kw_ok else 'не найден'}")
    st.markdown(f"{'🟢' if csv_ok else '🔴'} keywords.csv {'OK' if csv_ok else 'не найден'}")

    st.markdown("---")

    # Быстрые настройки
    st.markdown("**🎛️ Конфигурация запуска**")
    enable_instagram = st.toggle("Instagram", value=True)
    enable_tiktok    = st.toggle("TikTok", value=True)
    enable_vpn       = st.toggle("VPN ротация", value=True)

    st.markdown("---")

    # Пути
    with st.expander("📁 Пути проекта"):
        st.code(f"DB:      {DB_PATH}\nJSON:    {ANALYTICS_JSON}\nCSV:     {CSV_EXPORT_PATH}", language="text")

    st.markdown("---")
    df_check = load_db_records()
    record_count = len(df_check) if not df_check.empty else 0
    st.markdown(f"**📊 Записей в БД:** `{record_count:,}`")
    if ANALYTICS_JSON.exists():
        mtime = datetime.fromtimestamp(ANALYTICS_JSON.stat().st_mtime)
        st.markdown(f"**🕐 Последний анализ:**")
        st.markdown(f"`{mtime.strftime('%d.%m.%Y %H:%M')}`")


# ─────────────────────────────────────────────────────────────────────────────
# HERO HEADER
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero">
    <div class="hero-logo">PLAKSIK AI</div>
    <div class="hero-sub">Social Media Anti-Fraud Intelligence Platform</div>
    <div class="hero-badge">v2.0 · TikTok & Instagram · ML-Powered</div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНЫЕ ТАБЫ
# ─────────────────────────────────────────────────────────────────────────────
tab_overview, tab_parser, tab_analytics, tab_data, tab_export = st.tabs([
    "📊 Обзор",
    "🚀 Запуск парсинга",
    "🔍 Аналитика",
    "🗄️ База данных",
    "📤 Экспорт",
])

# ═════════════════════════════════════════════════════════════════════════════
# ТАБ 1: ОБЗОР
# ═════════════════════════════════════════════════════════════════════════════
with tab_overview:
    df = load_db_records()
    analytics = load_analytics_json()
    stats = db_stats(df)

    if not df.empty:
        # ── Главные метрики ──────────────────────────────────────────────
        c1, c2, c3, c4, c5, c6 = st.columns(6)

        summary = analytics.get("summary", {})
        critical_count = summary.get("critical_count", 0)
        warning_count  = summary.get("warning_count", 0)
        clean_count    = summary.get("clean_count", 0)

        with c1:
            st.markdown(f"""<div class="metric-card" style="--accent:#3b82f6;">
                <div class="metric-value">{stats.get('total', 0):,}</div>
                <div class="metric-label">Всего записей</div>
                <div class="metric-sub">в базе данных</div>
            </div>""", unsafe_allow_html=True)
        with c2:
            st.markdown(f"""<div class="metric-card" style="--accent:#ef4444;">
                <div class="metric-value">{critical_count}</div>
                <div class="metric-label">Critical</div>
                <div class="metric-sub">высокий риск</div>
            </div>""", unsafe_allow_html=True)
        with c3:
            st.markdown(f"""<div class="metric-card" style="--accent:#f59e0b;">
                <div class="metric-value">{warning_count}</div>
                <div class="metric-label">Warning</div>
                <div class="metric-sub">требует проверки</div>
            </div>""", unsafe_allow_html=True)
        with c4:
            st.markdown(f"""<div class="metric-card" style="--accent:#10b981;">
                <div class="metric-value">{clean_count}</div>
                <div class="metric-label">Clean</div>
                <div class="metric-sub">норма</div>
            </div>""", unsafe_allow_html=True)
        with c5:
            st.markdown(f"""<div class="metric-card" style="--accent:#8b5cf6;">
                <div class="metric-value">{stats.get('unique_authors', 0)}</div>
                <div class="metric-label">Авторов</div>
                <div class="metric-sub">уникальных</div>
            </div>""", unsafe_allow_html=True)
        with c6:
            fraud_density = summary.get("fraud_density_percent", 0)
            st.markdown(f"""<div class="metric-card" style="--accent:#06b6d4;">
                <div class="metric-value">{fraud_density}%</div>
                <div class="metric-label">Fraud Rate</div>
                <div class="metric-sub">плотность угроз</div>
            </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Графики ──────────────────────────────────────────────────────
        col_left, col_right = st.columns([1.2, 1])

        with col_left:
            st.markdown('<div class="section-title">📈 Активность по времени</div>', unsafe_allow_html=True)
            if "publish_date" in df.columns:
                df_time = df.copy()
                df_time["date"] = pd.to_datetime(df_time["publish_date"], errors="coerce")
                df_time = df_time.dropna(subset=["date"])
                if not df_time.empty:
                    daily = df_time.groupby(df_time["date"].dt.date).size().reset_index()
                    daily.columns = ["date", "count"]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=daily["date"], y=daily["count"],
                        fill="tozeroy",
                        line=dict(color="#3b82f6", width=2),
                        fillcolor="rgba(59,130,246,0.12)",
                        name="Записи"
                    ))
                    fig.update_layout(
                        paper_bgcolor="#111827", plot_bgcolor="#111827",
                        font=dict(color="#94a3b8", size=11),
                        margin=dict(l=0, r=0, t=10, b=0), height=200,
                        xaxis=dict(showgrid=False, color="#374151"),
                        yaxis=dict(showgrid=True, gridcolor="#1e2d45", color="#374151"),
                        showlegend=False,
                    )
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.info("Нет данных с датами для построения графика.")

        with col_right:
            st.markdown('<div class="section-title">🎯 Распределение рисков</div>', unsafe_allow_html=True)
            if critical_count + warning_count + clean_count > 0:
                fig_pie = go.Figure(go.Pie(
                    labels=["Critical", "Warning", "Clean"],
                    values=[critical_count, warning_count, clean_count],
                    hole=0.6,
                    marker=dict(colors=["#ef4444", "#f59e0b", "#10b981"]),
                    textinfo="percent+value",
                    textfont=dict(size=12, color="white"),
                ))
                fig_pie.update_layout(
                    paper_bgcolor="#111827", plot_bgcolor="#111827",
                    font=dict(color="#94a3b8"),
                    margin=dict(l=0, r=0, t=0, b=0), height=200,
                    showlegend=True,
                    legend=dict(
                        orientation="v", x=1, y=0.5,
                        font=dict(color="#94a3b8", size=11),
                        bgcolor="rgba(0,0,0,0)",
                    ),
                )
                st.plotly_chart(fig_pie, use_container_width=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Платформы и скор ─────────────────────────────────────────────
        col_a, col_b = st.columns(2)

        with col_a:
            st.markdown('<div class="section-title">📱 Платформы</div>', unsafe_allow_html=True)
            if "platform" in df.columns:
                platform_counts = df["platform"].value_counts().reset_index()
                platform_counts.columns = ["platform", "count"]
                fig_bar = go.Figure(go.Bar(
                    x=platform_counts["platform"],
                    y=platform_counts["count"],
                    marker=dict(
                        color=["#3b82f6" if p == "tiktok" else "#8b5cf6"
                               for p in platform_counts["platform"]],
                        line=dict(width=0),
                    ),
                    text=platform_counts["count"],
                    textposition="auto",
                    textfont=dict(color="white", size=13),
                ))
                fig_bar.update_layout(
                    paper_bgcolor="#111827", plot_bgcolor="#111827",
                    font=dict(color="#94a3b8", size=11),
                    margin=dict(l=0, r=0, t=10, b=0), height=180,
                    xaxis=dict(showgrid=False),
                    yaxis=dict(showgrid=True, gridcolor="#1e2d45"),
                )
                st.plotly_chart(fig_bar, use_container_width=True)

        with col_b:
            st.markdown('<div class="section-title">⚡ Распределение скора</div>', unsafe_allow_html=True)
            if "match_percentage" in df.columns:
                scores = df["match_percentage"].dropna()
                fig_hist = go.Figure(go.Histogram(
                    x=scores,
                    nbinsx=20,
                    marker=dict(
                        color="rgba(139,92,246,0.7)",
                        line=dict(color="#8b5cf6", width=1),
                    ),
                ))
                fig_hist.add_vline(x=WARNING_SCORE_THRESHOLD, line=dict(color="#f59e0b", dash="dash", width=1.5))
                fig_hist.add_vline(x=CRITICAL_SCORE_THRESHOLD, line=dict(color="#ef4444", dash="dash", width=1.5))
                fig_hist.update_layout(
                    paper_bgcolor="#111827", plot_bgcolor="#111827",
                    font=dict(color="#94a3b8", size=11),
                    margin=dict(l=0, r=0, t=10, b=0), height=180,
                    xaxis=dict(showgrid=False, title="Match %"),
                    yaxis=dict(showgrid=True, gridcolor="#1e2d45", title="Кол-во"),
                )
                st.plotly_chart(fig_hist, use_container_width=True)

        # ── Graph метрики если есть аналитика ────────────────────────────
        if analytics and "graph" in analytics:
            graph = analytics["graph"]
            st.markdown("---")
            st.markdown('<div class="section-title">🕸️ Граф связей</div>', unsafe_allow_html=True)
            gc1, gc2, gc3, gc4 = st.columns(4)
            with gc1:
                st.metric("Рёбер в графе", graph.get("total_edges", 0))
            with gc2:
                st.metric("Крупнейший кластер", graph.get("largest_cluster_size", 0))
            with gc3:
                st.metric("Кластеров", summary.get("total_clusters", 0))
            with gc4:
                st.metric("Плотность графа", f"{graph.get('density', 0):.4f}")

    else:
        st.markdown("""
        <div style="text-align:center; padding:80px 40px; background:#111827; border-radius:20px;
             border: 1px dashed #1e2d45; margin-top:20px;">
            <div style="font-size:3rem; margin-bottom:16px;">🛡️</div>
            <div style="font-size:1.2rem; font-weight:700; color:#cbd5e1;">База данных пуста</div>
            <div style="font-size:0.9rem; color:#475569; margin-top:8px;">
                Перейди во вкладку <strong>🚀 Запуск парсинга</strong>, чтобы собрать первые данные
            </div>
        </div>
        """, unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════════════════════
# ТАБ 2: ЗАПУСК ПАРСИНГА
# ═════════════════════════════════════════════════════════════════════════════
with tab_parser:
    st.markdown('<div class="section-title">🚀 Управление парсером</div>', unsafe_allow_html=True)

    col_cfg, col_run = st.columns([1.5, 1])

    with col_cfg:
        st.markdown("**📝 Ключевые слова**")

        # Загрузить текущие keywords если файл есть
        default_kw = ""
        if KEYWORDS_FILE.exists():
            try:
                default_kw = KEYWORDS_FILE.read_text(encoding="utf-8")
            except Exception:
                pass

        keywords_text = st.text_area(
            "Одно ключевое слово на строку",
            value=default_kw,
            height=220,
            placeholder="казино\nставки\nинвестиции\nbitcoin\n...",
            label_visibility="collapsed",
        )

        save_kw = st.button("💾 Сохранить ключевые слова", use_container_width=True)
        if save_kw:
            try:
                KEYWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
                KEYWORDS_FILE.write_text(keywords_text, encoding="utf-8")
                st.success("✅ keywords.txt обновлён!")
            except Exception as e:
                st.error(f"Ошибка: {e}")

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("**⚙️ Параметры**")

        p1, p2 = st.columns(2)
        with p1:
            max_videos = st.number_input("Видео на ключ", min_value=1, max_value=200, value=30)
            parallel_workers = st.number_input("Воркеров", min_value=1, max_value=10, value=3)
        with p2:
            delay_min = st.number_input("Задержка мин (сек)", min_value=0.0, max_value=30.0, value=1.0, step=0.5)
            delay_max = st.number_input("Задержка макс (сек)", min_value=0.0, max_value=60.0, value=3.0, step=0.5)

    with col_run:
        st.markdown("**🎮 Управление**")

        # Статус
        running = st.session_state.get("parsing_running", False)
        if running:
            st.markdown("""
            <div style="background:rgba(59,130,246,0.1); border:1px solid rgba(59,130,246,0.3);
                 border-radius:10px; padding:12px 16px; margin-bottom:16px;">
                <div style="color:#60a5fa; font-weight:700; font-size:0.9rem;">
                    ⚡ Парсер запущен...
                </div>
                <div style="color:#475569; font-size:0.78rem; margin-top:4px;">
                    Не закрывай браузер
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown("""
            <div style="background:rgba(16,185,129,0.1); border:1px solid rgba(16,185,129,0.3);
                 border-radius:10px; padding:12px 16px; margin-bottom:16px;">
                <div style="color:#34d399; font-weight:700; font-size:0.9rem;">
                    ✅ Готов к запуску
                </div>
                <div style="color:#475569; font-size:0.78rem; margin-top:4px;">
                    Настрой параметры и жми старт
                </div>
            </div>
            """, unsafe_allow_html=True)

        btn_start = st.button(
            "▶️ ЗАПУСТИТЬ ПАРСИНГ" if not running else "⏸️ Парсер работает...",
            use_container_width=True,
            disabled=running,
        )

        custom_tags_input = st.text_input(
            "🏷️ Теги для запуска (опционально)",
            placeholder="casino, 1xbet, #слоты (если пусто — из keywords.txt)",
        )
        platform_choice = st.selectbox(
            "📱 Платформа",
            options=["all", "instagram", "tiktok"],
            index=0,
        )

        btn_stop = st.button("⏹️ ОСТАНОВИТЬ", use_container_width=True, disabled=not running)
        btn_analytics = st.button("🔍 Запустить аналитику", use_container_width=True)

        st.markdown("---")
        st.markdown("**📋 Чеклист запуска**")

        checks = {
            "keywords.txt найден": KEYWORDS_FILE.exists(),
            "final_keywords.csv найден": KEYWORDS_CSV.exists(),
            "main.py найден": (PROJECT_ROOT / "main.py").exists(),
            "config.py найден": (PROJECT_ROOT / "config.py").exists(),
        }
        for label, ok in checks.items():
            icon = "✅" if ok else "❌"
            color = "#34d399" if ok else "#f87171"
            st.markdown(f'<span style="color:{color}; font-size:0.82rem;">{icon} {label}</span>', unsafe_allow_html=True)

    # Обработка кнопок
    if btn_start and not running:
        parsed_custom_tags = [t.strip() for t in custom_tags_input.split(",") if t.strip()]
        if not parsed_custom_tags and not KEYWORDS_FILE.exists():
            st.error("❌ Файл keywords.txt не найден и теги не указаны.")
        else:
            st.session_state.parsing_running = True
            st.session_state.log_lines = [f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Запуск парсера..."]
            cmd = [sys.executable, str(PROJECT_ROOT / "main.py"), "--platform", platform_choice]
            if parsed_custom_tags:
                cmd.extend(["--tags", *parsed_custom_tags])
            try:
                with open(LOG_FILE, "w", encoding="utf-8") as log_fh:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=log_fh, stderr=log_fh,
                        cwd=str(PROJECT_ROOT),
                    )
                st.session_state.run_pid = proc.pid
                st.success(f"✅ Парсер запущен (PID: {proc.pid})")
            except Exception as e:
                st.error(f"❌ Ошибка запуска: {e}")
                st.session_state.parsing_running = False
            st.rerun()

    if btn_stop:
        pid = st.session_state.get("run_pid")
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                st.success(f"⏹️ Процесс {pid} остановлен")
            except Exception as e:
                st.warning(f"Не удалось остановить: {e}")
        st.session_state.parsing_running = False
        st.session_state.run_pid = None
        st.rerun()

    if btn_analytics:
        with st.spinner("🔍 Запускаю аналитику..."):
            try:
                result = subprocess.run(
                    [sys.executable, str(PROJECT_ROOT / "main.py"), "--only-analytics"],
                    capture_output=True, text=True, timeout=300, cwd=str(PROJECT_ROOT),
                )
                if result.returncode == 0:
                    st.cache_data.clear()
                    st.success("✅ Аналитика завершена! Данные обновлены.")
                    st.rerun()
                else:
                    err_msg = (result.stderr or result.stdout or "неизвестная ошибка")[-3000:]
                    st.error(f"❌ Ошибка аналитики:\n```\n{err_msg}\n```")
            except subprocess.TimeoutExpired:
                st.error("⏱️ Таймаут аналитики (5 минут). Попробуй с меньшим числом записей.")
            except Exception as e:
                st.error(f"❌ {e}")

    st.markdown("---")

    # ── Лог ──────────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">🖥️ Лог выполнения</div>', unsafe_allow_html=True)

    log_col1, log_col2 = st.columns([5, 1])
    with log_col2:
        auto_refresh = st.toggle("Авто-обновление", value=running)

    log_lines = read_log_tail(100)
    if log_lines:
        log_html = "<br>".join(
            f'<span style="color:{"#4ade80" if "SUCCESS" in l or "✅" in l else "#f87171" if "ERROR" in l or "❌" in l else "#fbbf24" if "WARNING" in l or "⚠️" in l else "#94a3b8"}">{l}</span>'
            for l in log_lines
        )
        st.markdown(f'<div class="log-box">{log_html}</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="log-box" style="color:#374151;">Лог пуст. Запусти парсер.</div>', unsafe_allow_html=True)

    if auto_refresh and running:
        # Проверяем, не завершился ли процесс — если да, сбрасываем флаг
        pid = st.session_state.get("run_pid")
        if pid:
            try:
                os.kill(pid, 0)  # 0 = просто проверить, не посылать сигнал
            except (ProcessLookupError, OSError):
                # Процесс уже завершился
                st.session_state.parsing_running = False
                st.session_state.run_pid = None
        time.sleep(3)
        st.rerun()

# ═════════════════════════════════════════════════════════════════════════════
# ТАБ 3: АНАЛИТИКА
# ═════════════════════════════════════════════════════════════════════════════
with tab_analytics:
    analytics = load_analytics_json()

    if not analytics:
        st.markdown("""
        <div style="text-align:center; padding:60px 40px; background:#111827; border-radius:20px;
             border: 1px dashed #1e2d45; margin-top:20px;">
            <div style="font-size:2.5rem; margin-bottom:12px;">🔍</div>
            <div style="font-size:1.1rem; font-weight:700; color:#cbd5e1;">Нет данных аналитики</div>
            <div style="font-size:0.85rem; color:#475569; margin-top:8px;">
                Запусти парсинг и аналитику во вкладке <strong>🚀 Запуск парсинга</strong>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        summary = analytics.get("summary", {})
        videos  = analytics.get("videos", [])

        # ── Сводные метрики ──────────────────────────────────────────────
        st.markdown('<div class="section-title">📊 Сводная статистика аналитики</div>', unsafe_allow_html=True)

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        metrics_data = [
            (m1, summary.get("total_processed", 0), "Обработано", "#3b82f6"),
            (m2, summary.get("critical_count", 0), "Critical", "#ef4444"),
            (m3, summary.get("warning_count", 0), "Warning", "#f59e0b"),
            (m4, summary.get("clean_count", 0), "Clean", "#10b981"),
            (m5, f"{summary.get('avg_priority_score', 0)}", "Средний скор", "#8b5cf6"),
            (m6, summary.get("keywords_database_size", 0), "Стоп-слов", "#06b6d4"),
        ]
        for col, val, label, color in metrics_data:
            with col:
                st.markdown(f"""<div class="metric-card" style="--accent:{color};">
                    <div class="metric-value" style="font-size:1.8rem;">{val}</div>
                    <div class="metric-label">{label}</div>
                </div>""", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)

        # ── Фильтры ──────────────────────────────────────────────────────
        st.markdown('<div class="section-title">🎯 Список угроз</div>', unsafe_allow_html=True)
        f1, f2, f3 = st.columns([1, 1, 2])
        with f1:
            sev_filter = st.multiselect(
                "Уровень угрозы",
                ["CRITICAL", "WARNING", "CLEAN"],
                default=["CRITICAL", "WARNING"],
            )
        with f2:
            tax_options = list({v.get("taxonomy", "") for v in videos if v.get("taxonomy")})
            tax_filter = st.multiselect("Таксономия", tax_options)
        with f3:
            search_term = st.text_input("🔎 Поиск по автору / video_id / caption", placeholder="казино, @username, ...")

        # Применяем фильтры
        filtered = [v for v in videos if v.get("severity", "") in sev_filter]
        if tax_filter:
            filtered = [v for v in filtered if v.get("taxonomy", "") in tax_filter]
        if search_term:
            s = search_term.lower()
            filtered = [
                v for v in filtered
                if s in v.get("author", "").lower()
                or s in v.get("video_id", "").lower()
                or s in v.get("caption", "").lower()
            ]

        st.markdown(f"**Найдено:** {len(filtered)} записей", unsafe_allow_html=False)

        # Список записей
        for v in filtered[:50]:
            sev  = v.get("severity", "CLEAN")
            card_class = {"CRITICAL": "critical", "WARNING": "warning"}.get(sev, "clean")
            badge_class = {"CRITICAL": "badge-critical", "WARNING": "badge-warning"}.get(sev, "badge-clean")

            score_color = "#ef4444" if sev == "CRITICAL" else "#f59e0b" if sev == "WARNING" else "#10b981"

            with st.expander(
                f"{'🔴' if sev=='CRITICAL' else '🟡' if sev=='WARNING' else '🟢'} "
                f"{v.get('author', 'unknown')} · score: {v.get('priority_score', 0):.1f} "
                f"· {v.get('taxonomy', '')} · {v.get('video_id', '')}",
                expanded=(sev == "CRITICAL"),
            ):
                ex1, ex2, ex3 = st.columns([2, 1, 1])

                with ex1:
                    st.markdown(f"**📝 Caption:**")
                    st.markdown(f"_{v.get('caption', 'N/A')[:300]}_")
                    st.markdown(f"**🔗 URL:** [{v.get('url', '')}]({v.get('url', '')})")

                with ex2:
                    st.markdown("**📊 Метрики:**")
                    metrics = v.get("metrics", {})
                    st.markdown(f"👥 Подписчики: `{metrics.get('followers', 0):,}`")
                    st.markdown(f"👁️ Просмотры: `{metrics.get('views', 0):,}`")
                    st.markdown(f"❤️ Лайки: `{metrics.get('likes', 0):,}`")
                    st.markdown(f"🔑 Ключей: `{v.get('detected_keywords_count', 0)}`")

                with ex3:
                    st.markdown("**⚙️ Feature Scores:**")
                    fc = v.get("feature_contribution", {})
                    for feat, val in fc.items():
                        color = "#ef4444" if val > 60 else "#f59e0b" if val > 30 else "#10b981"
                        st.markdown(f'<span style="color:{color}; font-size:0.82rem;">■ {feat}: {val}%</span>', unsafe_allow_html=True)

                # Объяснение
                explanation = v.get("explanation", [])
                if explanation:
                    st.markdown("**💡 Причины скора:**")
                    for reason in explanation:
                        st.markdown(f"- {reason}")

                # Похожие видео
                similar = v.get("similar_videos", [])
                if similar:
                    st.markdown("**🕸️ Похожие видео:**")
                    for sim in similar:
                        st.markdown(
                            f"- `{sim.get('video_id', '')}` · "
                            f"сходство: **{sim.get('similarity_percent', 0)}%** · "
                            f"{sim.get('reason', '')} · "
                            f"[ссылка]({sim.get('video_url', '')})"
                        )

# ═════════════════════════════════════════════════════════════════════════════
# ТАБ 4: БАЗА ДАННЫХ
# ═════════════════════════════════════════════════════════════════════════════
with tab_data:
    df = load_db_records()

    if df.empty:
        st.info("База данных пуста. Сначала запусти парсинг.")
    else:
        st.markdown('<div class="section-title">🗄️ Содержимое базы данных</div>', unsafe_allow_html=True)

        # Фильтры
        fc1, fc2, fc3, fc4 = st.columns(4)
        with fc1:
            if "platform" in df.columns:
                plat_opt = ["Все"] + list(df["platform"].unique())
                plat_sel = st.selectbox("Платформа", plat_opt)
        with fc2:
            if "status" in df.columns:
                stat_opt = ["Все"] + list(df["status"].dropna().unique())
                stat_sel = st.selectbox("Статус", stat_opt)
        with fc3:
            if "match_percentage" in df.columns:
                score_min = st.slider("Мин. скор", 0, 100, 0)
        with fc4:
            search_db = st.text_input("🔎 Поиск", placeholder="автор, видео ID...")

        df_filtered = df.copy()
        if "platform" in df.columns and plat_sel != "Все":
            df_filtered = df_filtered[df_filtered["platform"] == plat_sel]
        if "status" in df.columns and stat_sel != "Все":
            df_filtered = df_filtered[df_filtered["status"] == stat_sel]
        if "match_percentage" in df.columns:
            df_filtered = df_filtered[df_filtered["match_percentage"] >= score_min]
        if search_db:
            mask = df_filtered.astype(str).apply(
                lambda col: col.str.contains(search_db, case=False, na=False)
            ).any(axis=1)
            df_filtered = df_filtered[mask]

        st.markdown(f"**{len(df_filtered):,}** записей из **{len(df):,}** total")

        # Выбор колонок для отображения
        display_cols = [c for c in [
            "id", "platform", "author_username", "video_id",
            "likes_count", "views_count", "comments_count", "followers_count",
            "match_percentage", "status", "publish_date",
        ] if c in df_filtered.columns]

        st.dataframe(
            df_filtered[display_cols].sort_values("match_percentage", ascending=False)
            if "match_percentage" in df_filtered.columns else df_filtered[display_cols],
            use_container_width=True,
            height=500,
        )

        # Топ авторов
        st.markdown("---")
        st.markdown('<div class="section-title">🏆 Топ авторов по скору</div>', unsafe_allow_html=True)
        if "author_username" in df.columns and "match_percentage" in df.columns:
            top_authors = (
                df.groupby("author_username")
                .agg(videos=("video_id", "count"), avg_score=("match_percentage", "mean"))
                .sort_values("avg_score", ascending=False)
                .head(15)
                .reset_index()
            )
            fig_top = go.Figure(go.Bar(
                y=top_authors["author_username"],
                x=top_authors["avg_score"].round(1),
                orientation="h",
                marker=dict(
                    color=top_authors["avg_score"],
                    colorscale=[[0, "#10b981"], [0.35, "#f59e0b"], [0.65, "#ef4444"], [1, "#7f1d1d"]],
                    line=dict(width=0),
                ),
                text=top_authors["avg_score"].round(1).astype(str) + "%",
                textposition="auto",
                textfont=dict(color="white", size=11),
            ))
            fig_top.update_layout(
                paper_bgcolor="#111827", plot_bgcolor="#111827",
                font=dict(color="#94a3b8", size=11),
                margin=dict(l=0, r=0, t=10, b=0), height=400,
                xaxis=dict(showgrid=True, gridcolor="#1e2d45", range=[0, 105]),
                yaxis=dict(showgrid=False),
            )
            st.plotly_chart(fig_top, use_container_width=True)

# ═════════════════════════════════════════════════════════════════════════════
# ТАБ 5: ЭКСПОРТ
# ═════════════════════════════════════════════════════════════════════════════
with tab_export:
    st.markdown('<div class="section-title">📤 Экспорт данных</div>', unsafe_allow_html=True)

    df = load_db_records()
    analytics = load_analytics_json()

    ec1, ec2 = st.columns(2)

    with ec1:
        st.markdown("**📊 Экспорт базы данных**")
        if not df.empty:
            csv_data = df.to_csv(index=False, encoding="utf-8")
            st.download_button(
                label="⬇️ Скачать CSV (вся БД)",
                data=csv_data,
                file_name=f"plaksik_db_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True,
            )

            # Только угрозы
            if "match_percentage" in df.columns:
                df_threats = df[df["match_percentage"] >= WARNING_SCORE_THRESHOLD]
                if not df_threats.empty:
                    threats_csv = df_threats.to_csv(index=False, encoding="utf-8")
                    st.download_button(
                        label=f"🔴 Скачать CSV (только угрозы, {len(df_threats)} записей)",
                        data=threats_csv,
                        file_name=f"plaksik_threats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        mime="text/csv",
                        use_container_width=True,
                    )
        else:
            st.info("База данных пуста.")

        st.markdown("---")
        st.markdown("**🗃️ Статистика для экспорта**")
        if not df.empty:
            st.metric("Всего записей", f"{len(df):,}")
            if "match_percentage" in df.columns:
                st.metric("Средний скор", f"{df['match_percentage'].mean():.1f}%")
                threats = df[df["match_percentage"] >= WARNING_SCORE_THRESHOLD]
                st.metric("Угроз (Warning+Critical)", f"{len(threats):,}")

    with ec2:
        st.markdown("**📋 Экспорт отчёта аналитики**")
        if analytics:
            json_str = json.dumps(analytics, ensure_ascii=False, indent=2)
            st.download_button(
                label="⬇️ Скачать JSON (полный отчёт)",
                data=json_str.encode("utf-8"),
                file_name=f"plaksik_analytics_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
                use_container_width=True,
            )

            # Топ угроз в CSV
            videos = analytics.get("videos", [])
            if videos:
                top_df = pd.DataFrame([{
                    "video_id": v.get("video_id"),
                    "author": v.get("author"),
                    "url": v.get("url"),
                    "priority_score": v.get("priority_score"),
                    "severity": v.get("severity"),
                    "taxonomy": v.get("taxonomy"),
                    "risk_level": v.get("risk_level"),
                    "keywords_count": v.get("detected_keywords_count"),
                    "followers": v.get("metrics", {}).get("followers"),
                    "views": v.get("metrics", {}).get("views"),
                    "likes": v.get("metrics", {}).get("likes"),
                } for v in videos])
                top_csv = top_df.to_csv(index=False, encoding="utf-8")
                st.download_button(
                    label="⬇️ Скачать CSV (список угроз с ML-скорами)",
                    data=top_csv,
                    file_name=f"plaksik_scored_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

            st.markdown("---")
            st.markdown("**📈 Превью отчёта**")
            summary = analytics.get("summary", {})
            for k, v in summary.items():
                st.markdown(f"- **{k}**: `{v}`")

        else:
            st.info("Нет данных аналитики. Сначала запусти анализ.")

        st.markdown("---")
        st.markdown("**🔄 Управление данными**")
        if st.button("🗑️ Очистить кэш данных", use_container_width=True):
            st.cache_data.clear()
            st.success("✅ Кэш очищен. Данные будут перезагружены.")

        st.markdown("---")
        st.markdown("**⚠️ Опасная зона**")
        with st.expander("🔴 Сбросить базу данных"):
            st.warning("Это удалит ВСЕ собранные данные из БД и JSON-отчёт. Действие необратимо.")
            if st.button("💣 Очистить БД и отчёт", use_container_width=True, type="primary"):
                try:
                    if DB_PATH.exists():
                        conn_reset = sqlite3.connect(str(DB_PATH))
                        conn_reset.execute("DELETE FROM media_analytics")
                        conn_reset.commit()
                        conn_reset.close()
                    if ANALYTICS_JSON.exists():
                        ANALYTICS_JSON.unlink()
                    st.cache_data.clear()
                    st.success("✅ БД и отчёт очищены. Готово к новому парсингу.")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Ошибка сброса: {e}")

        if DB_PATH.exists():
            db_size = DB_PATH.stat().st_size / 1024 / 1024
            st.markdown(f"**Размер БД:** `{db_size:.2f} MB`")
        if ANALYTICS_JSON.exists():
            json_size = ANALYTICS_JSON.stat().st_size / 1024
            st.markdown(f"**Размер отчёта:** `{json_size:.1f} KB`")