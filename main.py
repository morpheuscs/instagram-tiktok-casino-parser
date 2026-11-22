import os
import re
import sys
import ast
import json
import argparse
import asyncio
import numpy as np
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from loguru import logger

from config import (
    KEYWORDS_FILE, CSV_EXPORT_PATH,
    ENABLE_INSTAGRAM, ENABLE_TIKTOK,
)
from db_manager import DatabaseManager
from ml_processor import MlProcessor
from vpn_rotator import VPNRotator
from data_loader import DataLoader
from vector_builder import VectorBuilder

if ENABLE_INSTAGRAM:
    from parsers.instagram import InstagramParser

if ENABLE_TIKTOK:
    from parsers.tiktok import TikTokParser

# ── Пути ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
KEYWORDS_CSV = PROJECT_ROOT / "data" / "final_keywords.csv"
ANALYTICS_OUT = PROJECT_ROOT / "analytics_results.json"

# ── Пороги аналитики ──────────────────────────────────────────────────────────
SIMILARITY_THRESHOLD     = 0.85
CRITICAL_SCORE_THRESHOLD = 65.0
WARNING_SCORE_THRESHOLD  = 35.0


# ══════════════════════════════════════════════════════════════════════════════
#  ЧАСТЬ 1 — ПАРСИНГ (твой код)
# ══════════════════════════════════════════════════════════════════════════════

def setup_logging() -> None:
    logger.remove()
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<magenta><b>{level: <8}</b></magenta> | "
        "<cyan>CORE_SYSTEM</cyan> - "
        "<level>{message}</level>"
    )
    logger.add(sys.stdout, format=log_format, colorize=True, level="DEBUG")


async def _run_instagram(parser, keyword: str, ml_processor: MlProcessor) -> list:
    if parser is None:
        return []
    try:
        results = await parser.parse(keyword, ml_processor=ml_processor)
        logger.info(f"[Instagram] «{keyword}» → {len(results)} записей.")
        return results or []
    except Exception as exc:
        logger.error(f"[Instagram] Сбой при парсинге «{keyword}»: {exc}")
        return []


async def _run_tiktok(parser, keyword: str, ml_processor: MlProcessor) -> list:
    if parser is None:
        return []
    try:
        results = await parser.parse(keyword, ml_processor=ml_processor)
        logger.info(f"[TikTok] «{keyword}» → {len(results)} записей.")
        return results or []
    except Exception as exc:
        logger.error(f"[TikTok] Сбой при парсинге «{keyword}»: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
#  РЕАЛ-ТАЙМ СКОРИНГ — считаем вектор и скор для каждого видео сразу
#  после того, как оно спарсилось и сохранилось в БД (до полного отчёта).
#  Графовый признак (сходство со всей базой) здесь не считается —
#  он требует всей выборки и пересчитывается позже в run_analytics_pipeline.
# ══════════════════════════════════════════════════════════════════════════════

async def process_keyword(
    keyword: str,
    instagram_parser,
    tiktok_parser,
    ml_processor: MlProcessor,
    db_manager: DatabaseManager,
    live_scorer: "LiveScorer | None" = None,
) -> int:
    logger.info(f"{'=' * 15} ОБРАБОТКА: {keyword.upper()} {'=' * 15}")

    ig_results, tt_results = await asyncio.gather(
        _run_instagram(instagram_parser, keyword, ml_processor),
        _run_tiktok(tiktok_parser, keyword, ml_processor),
    )

    combined = ig_results + tt_results
    logger.info(
        f"[Merge] «{keyword}»: IG={len(ig_results)}, TT={len(tt_results)}, "
        f"итого={len(combined)}. Запись в БД..."
    )

    saved = 0
    for item in combined:
        if not item:
            continue
        item.pop("local_path", None)
        try:
            await db_manager.save_media_record(item)
            logger.success(f"[DB] → {item.get('platform', '?')}:{item.get('video_id', '?')}")
            saved += 1
        except Exception as exc:
            logger.warning(f"[DB Guard] Сбой буферизации {item.get('video_id', '?')}: {exc}")
            continue

        # ── Реал-тайм вектор + скор сразу после сохранения видео ─────
        if live_scorer is not None:
            try:
                live_scorer.score_and_print(item)
            except Exception as exc:
                logger.warning(f"[LiveScorer] Сбой расчёта вектора/скора: {exc}")

    return saved


def _parse_raw_tags(raw_items: List[str]) -> List[str]:
    """Разбирает список тегов (поддерживает запятые и пробелы) и убирает дубликаты."""
    expanded: List[str] = []
    for entry in raw_items:
        for part in entry.split(","):
            cleaned = part.strip()
            if cleaned and not cleaned.startswith("#comment"):
                expanded.append(cleaned)

    seen: set[str] = set()
    keywords: List[str] = []
    for kw in expanded:
        norm = re.sub(r"[#\W]+", "", kw).lower().strip()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        keywords.append(kw)
    return keywords


async def run_parsing_pipeline(
    tags: Optional[List[str]] = None,
    tags_file: Optional[Path] = None,
    platform: str = "all",
    max_videos: Optional[int] = None,
) -> int:
    """Запускает парсинг Instagram + TikTok. Возвращает кол-во сохранённых записей."""
    logger.info("=== ЭТАП 1: ПАРСИНГ ===")

    if max_videos is not None and max_videos > 0:
        import config as _cfg
        _cfg.MAX_VIDEOS_PER_KEYWORD = max_videos
        if ENABLE_INSTAGRAM:
            import parsers.instagram as _ig_mod
            _ig_mod.MAX_VIDEOS_PER_KEYWORD = max_videos
        if ENABLE_TIKTOK:
            import parsers.tiktok as _tt_mod
            _tt_mod.MAX_VIDEOS_PER_KEYWORD = max_videos
        logger.info(f"Лимит видео на тег: {max_videos}")

    if tags:
        keywords = _parse_raw_tags(tags)
    else:
        kw_path = Path(tags_file) if tags_file else Path(KEYWORDS_FILE)
        if not kw_path.exists():
            logger.error(f"Файл тегов не найден: {kw_path}")
            sys.exit(1)
        with open(kw_path, "r", encoding="utf-8") as fh:
            raw_keywords = [line.strip() for line in fh if line.strip() and not line.strip().startswith("//")]
        keywords = _parse_raw_tags(raw_keywords)

    if not keywords:
        logger.error("Список тегов пуст — нечего обрабатывать.")
        sys.exit(1)

    logger.info(f"Тегов/ключевых слов к обработке: {len(keywords)} → {', '.join(keywords[:10])}{' ...' if len(keywords) > 10 else ''}")

    use_ig = ENABLE_INSTAGRAM and platform in ("all", "instagram")
    use_tt = ENABLE_TIKTOK and platform in ("all", "tiktok")

    db_manager       = DatabaseManager()
    ml_processor     = MlProcessor()
    vpn_rotator      = VPNRotator()
    instagram_parser = InstagramParser(vpn_rotator) if use_ig else None
    tiktok_parser    = TikTokParser(vpn_rotator)    if use_tt else None

    active = [n for n, f in [("Instagram", use_ig), ("TikTok", use_tt)] if f]
    if not active:
        logger.error("Ни одна платформа не активна (проверьте --platform и .env). Выход.")
        sys.exit(1)
    logger.info(f"Активные платформы: {', '.join(active)}")

    await db_manager.initialize_db()

    live_scorer = LiveScorer(KEYWORDS_CSV)
    logger.info(f"[LiveScorer] Стоп-слов в базе для реал-тайм скоринга: {len(live_scorer.kl.keywords)}")

    total_saved = 0
    for idx, keyword in enumerate(keywords, start=1):
        logger.info(f"[{idx}/{len(keywords)}] «{keyword}»")
        saved = await process_keyword(
            keyword, instagram_parser, tiktok_parser, ml_processor, db_manager,
            live_scorer=live_scorer,
        )
        total_saved += saved

        # ── Аналитика запускается после каждого ключевого слова ──────
        if saved > 0:
            from config import DB_PATH
            db_path = Path(DB_PATH)
            if db_path.exists():
                logger.info(
                    f"[Аналитика] Обновляю отчёт после «{keyword}» "
                    f"(+{saved} записей, всего в БД: {total_saved})..."
                )
                try:
                    run_analytics_pipeline(db_path=db_path, output_path=ANALYTICS_OUT)
                except Exception as e:
                    logger.warning(f"[Аналитика] Ошибка при обновлении отчёта: {e}")

    logger.info(f"[Экспорт] CSV: {CSV_EXPORT_PATH}")
    await db_manager.export_to_csv(str(CSV_EXPORT_PATH))
    await db_manager.close()
    await vpn_rotator.disconnect()

    logger.success(f"=== ПАРСИНГ ЗАВЕРШЁН. Сохранено: {total_saved} записей ===")
    return total_saved


# ══════════════════════════════════════════════════════════════════════════════
#  ЧАСТЬ 2 — АНАЛИТИКА (код друга)
# ══════════════════════════════════════════════════════════════════════════════

TAXONOMY_MAP = {
    "CASINO_GAMBLING":  ["казино", "1win", "слоты", "вулкан", "букмекер", "ставки", "бк",
                         "gambling", "casino", "slots", "bet", "бонус казино"],
    "INVEST_SCAM":      ["инвестиции", "доход", "заработок", "трейдинг", "пассивный доход",
                         "гарантированный доход", "ai трейдер"],
    "PONZI_PATTERN":    ["схема", "пирамида", "умножу", "mmm", "мавроди", "рефералы",
                         "вступи быстрее", "реферальная система"],
    "MONEY_LAUNDERING": ["отмывание", "финансовый мул", "money mule", "биткоин миксер",
                         "btc mixer", "криптомиксер", "теневой банк"],
    "CRYPTO_FRAUD":     ["криптовалюта", "токен", "nft", "usdt", "btc", "eth", "binance",
                         "майнинг доход", "биткоин инвестировать"],
}


def _normalize_obfuscation(text: str) -> str:
    t = text.lower()
    t = re.sub(r'(?<=[а-яёa-z])[•·*](?=[а-яёa-z])', '', t)
    t = re.sub(r'(?<=[а-яё])[.](?=[а-яё])', '', t)
    for digit, letter in (('0','о'), ('3','з'), ('4','ч'), ('6','б'), ('9','д')):
        t = re.sub(rf'(?<=[а-яёa-z]){digit}(?=[а-яёa-z])', letter, t)
    return t


def _build_full_text(record: Dict[str, Any]) -> str:
    parts = []
    for field in ("caption", "bio", "audio_text", "ocr_text"):
        val = record.get(field, "") or ""
        parts.append(str(val))
    raw_hashtags = record.get("hashtags", [])
    if isinstance(raw_hashtags, str):
        try:
            raw_hashtags = ast.literal_eval(raw_hashtags)
        except Exception:
            raw_hashtags = re.split(r"[,\s]+", raw_hashtags)
    if isinstance(raw_hashtags, list):
        parts.append(" ".join(str(h) for h in raw_hashtags))
    full = " ".join(parts)
    full = full.replace("#", " ")
    full = re.sub(r"\s+", " ", full).strip()
    return _normalize_obfuscation(full)


def _resolve_taxonomy(detected_words: List[str], graph_score: float, behavior_score: float) -> str:
    word_set = set(detected_words)
    votes = {cat: 0 for cat in TAXONOMY_MAP}
    for cat, markers in TAXONOMY_MAP.items():
        for m in markers:
            if m in word_set:
                votes[cat] += 1
    best_cat   = max(votes, key=votes.get)
    best_votes = votes[best_cat]
    if best_votes > 0:
        return best_cat
    if graph_score > SIMILARITY_THRESHOLD and behavior_score > 0.5:
        return "BOT_FARM"
    return "FINANCIAL_FRAUD"


class UnionFind:
    def __init__(self, elements: List[str]):
        self.parent = {el: el for el in elements}
        self.size   = {el: 1   for el in elements}

    def find(self, i: str) -> str:
        if self.parent[i] != i:
            self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i: str, j: str) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri == rj:
            return
        if self.size[ri] < self.size[rj]:
            ri, rj = rj, ri
        self.parent[rj]  = ri
        self.size[ri]   += self.size[rj]

    def get_metrics(self) -> Tuple[int, int]:
        roots = {}
        for el in self.parent:
            r = self.find(el)
            roots[r] = roots.get(r, 0) + 1
        cluster_sizes = [s for s in roots.values() if s > 1]
        return len(cluster_sizes), (max(cluster_sizes) if cluster_sizes else 0)


class KeywordLoader:
    def __init__(self, csv_path: Path):
        self.keywords:   List[Dict[str, Any]] = []
        self.word_scores: Dict[str, float]    = {}
        self._load(csv_path)

    def _load(self, csv_path: Path) -> None:
        if not csv_path.exists():
            print(f"⚠️  [KeywordLoader] CSV не найден: {csv_path}")
            return
        try:
            df = pd.read_csv(csv_path)
            loaded = 0
            for _, row in df.iterrows():
                word = str(row.get("стоп_слово", "")).lower().strip()
                if not word:
                    continue
                score    = float(row.get("скор", 0.0))
                category = str(row.get("категория", "unknown")).strip()
                self.keywords.append({"word": word, "score": score, "category": category})
                self.word_scores[word] = score
                loaded += 1
            print(f"✅ [KeywordLoader] Загружено стоп-слов: {loaded}")
        except Exception as e:
            print(f"❌ [KeywordLoader] Ошибка загрузки: {e}")

    def match(self, text: str) -> List[Dict[str, Any]]:
        compressed_text = text.replace(" ", "")
        seen   = set()
        result = []
        for item in self.keywords:
            word = item["word"]
            if word in seen:
                continue
            if word in text or word.replace(" ", "") in compressed_text:
                seen.add(word)
                result.append(item)
        return result


class IncidentPrioritizer:
    def __init__(self, keyword_loader: KeywordLoader,
                 threshold_critical: float = CRITICAL_SCORE_THRESHOLD,
                 threshold_warning:  float = WARNING_SCORE_THRESHOLD):
        self.kl = keyword_loader
        self.tc = threshold_critical
        self.tw = threshold_warning

    def evaluate(self, record: Dict[str, Any], top_similar: List[Dict[str, Any]]) -> Dict[str, Any]:
        full_text         = _build_full_text(record)
        detected_triggers = self.kl.match(full_text)
        keywords_count    = len(detected_triggers)
        matched_scores    = [t["score"] for t in detected_triggers]

        if matched_scores:
            top_score  = max(matched_scores)
            sum_scores = sum(matched_scores)
            text_score = min(1.0, (top_score + sum_scores * 0.3) / (100.0 + 100.0 * 0.3 * 3))
        else:
            text_score = 0.0

        high_risk_words = [t for t in detected_triggers if t["score"] >= 80]
        semantic_score  = min(1.0, len(high_risk_words) * 0.25) if high_risk_words else 0.0

        followers = record.get("followers_count", 0) or 0
        views     = record.get("views_count", 0)    or 0
        likes     = record.get("likes_count", 0)    or 0
        behavior_score = 0.0
        if views == 0 and followers > 1000:
            behavior_score += 0.5
        elif followers > 0 and views > 0 and (views / followers) < 0.001:
            behavior_score += 0.35
        if views > 0 and likes > 0 and (likes / views) > 0.8:
            behavior_score += 0.3
        behavior_score = min(1.0, behavior_score)

        max_net_sim = top_similar[0]["similarity"] if top_similar else 0.0
        graph_score = max_net_sim if max_net_sim > SIMILARITY_THRESHOLD else 0.0

        raw_priority = (
            text_score     * 0.25 +
            semantic_score * 0.20 +
            behavior_score * 0.30 +
            graph_score    * 0.25
        )
        priority_score = round(min(100.0, max(0.0, raw_priority * 100)), 1)

        if priority_score < self.tw:
            severity   = "CLEAN"
            risk_level = "LOW"
            taxonomy   = "NORMAL"
        else:
            severity   = "CRITICAL" if priority_score >= self.tc else "WARNING"
            risk_level = ("CRITICAL" if severity == "CRITICAL"
                          else ("HIGH" if priority_score >= 50.0 else "MEDIUM"))
            detected_words_list = [t["word"] for t in detected_triggers]
            taxonomy = _resolve_taxonomy(detected_words_list, graph_score, behavior_score)

        explanation = []
        if keywords_count > 0:
            top_words = ", ".join(t["word"] for t in detected_triggers[:5])
            explanation.append(
                f"✓ Обнаружено стоп-слов: {keywords_count} шт. (топ: {top_words}{'...' if keywords_count > 5 else ''})"
            )
        if high_risk_words:
            explanation.append(f"✓ Критически опасных слов (скор ≥ 80): {len(high_risk_words)} шт.")
        if behavior_score > 0:
            if views == 0 and followers > 1000:
                explanation.append(f"✓ Аномалия: {followers:,} подписчиков при 0 просмотров")
            elif followers > 0 and views > 0 and (views / max(followers, 1)) < 0.001:
                explanation.append(f"✓ Критически низкий CTR: {views}/{followers}")
            if views > 0 and likes > 0 and (likes / views) > 0.8:
                explanation.append(f"✓ Неестественный лайк-рейт: {likes/views*100:.1f}%")
        if graph_score > SIMILARITY_THRESHOLD:
            explanation.append(f"✓ Высокое сетевое сходство: {graph_score * 100:.1f}%")
        if not explanation:
            explanation.append("Поведенческие, сетевые и текстовые маркеры в норме.")

        return {
            "priority_score":         priority_score,
            "severity":               severity,
            "risk_level":             risk_level,
            "taxonomy":               taxonomy,
            "explanation":            explanation,
            "detected_keywords_list": detected_triggers,
            "feature_contribution": {
                "Text analysis":     round(text_score     * 100, 1),
                "Semantic analysis": round(semantic_score * 100, 1),
                "Behavior analysis": round(behavior_score * 100, 1),
                "Graph analysis":    round(graph_score    * 100, 1),
            },
        }


class LiveScorer:
    """Реал-тайм скоринг прямо в процессе парсинга (без графового компонента)."""

    def __init__(self, keywords_csv: Path):
        self.kl = KeywordLoader(keywords_csv)
        self.builder = VectorBuilder(keywords_path=str(keywords_csv) if keywords_csv.exists() else "")
        self.builder.word_scores = self.kl.word_scores
        self.prioritizer = IncidentPrioritizer(keyword_loader=self.kl)
        self.counter = 0

    def score_and_print(self, record: Dict[str, Any]) -> Dict[str, Any]:
        self.counter += 1
        res = self.builder.build_vector(record)
        vector = res["vector"]
        # Без top_similar — графовый компонент = 0 на этом этапе.
        risk = self.prioritizer.evaluate(record, top_similar=[])

        vec_preview = ", ".join(f"{x:.3f}" for x in vector[:8])
        if len(vector) > 8:
            vec_preview += ", ..."

        print(
            f"⚡ [LIVE #{self.counter}] {record.get('platform', '?')}:"
            f"{record.get('video_id', '?')} "
            f"score={risk['priority_score']:5.1f} ({risk['severity']:<8}) "
            f"vector[{len(vector)}]=[{vec_preview}]"
        )
        return {"vector": vector, "score": risk["priority_score"], "severity": risk["severity"]}


def run_analytics_pipeline(db_path: Path, output_path: Path) -> None:
    """Запускает аналитику поверх собранной БД и сохраняет JSON-отчёт."""
    print("=" * 80)
    print("🛡️  ЭТАП 2: ANTI-FRAUD ANALYTICS PIPELINE")
    print("=" * 80)

    kl = KeywordLoader(KEYWORDS_CSV)
    if not kl.keywords:
        print("❌ База стоп-слов пуста или не найдена. Аналитика пропущена.")
        return

    builder = VectorBuilder(keywords_path=str(KEYWORDS_CSV) if KEYWORDS_CSV.exists() else "")
    builder.word_scores = kl.word_scores
    prioritizer = IncidentPrioritizer(keyword_loader=kl)

    try:
        raw_videos = DataLoader.load(db_path)
        print(f"📥 Загружено записей: {len(raw_videos)}")
    except Exception as e:
        print(f"❌ Ошибка загрузки базы: {e}")
        return

    if not raw_videos:
        print("❌ База данных пуста.")
        return

    valid_records: List[Dict[str, Any]] = []
    vectors_list:  List[np.ndarray]     = []
    meta_list:     List[Dict[str, Any]] = []

    for idx, record in enumerate(raw_videos):
        try:
            res = builder.build_vector(record)
            valid_records.append(record)
            vectors_list.append(res["vector"])
            meta_list.append(res["meta"])
        except Exception as e:
            print(f"⚠️  Ошибка векторизации записи #{idx}: {e}")

    total_nodes = len(valid_records)
    print(f"🔢 Векторизовано: {total_nodes} записей")
    if total_nodes == 0:
        print("❌ Нет валидных записей.")
        return

    vectors_matrix = np.array(vectors_list)
    sim_matrix     = np.dot(vectors_matrix, vectors_matrix.T)

    video_ids = [r["video_id"] for r in valid_records]
    dsu = UnionFind(video_ids)

    output_videos: List[Dict[str, Any]] = []
    graph_edges:   List[Dict[str, Any]] = []
    stats = {"CLEAN": 0, "WARNING": 0, "CRITICAL": 0}
    all_similarities: List[float] = []

    for i in range(total_nodes):
        similar_videos: List[Dict[str, Any]] = []

        for j in range(total_nodes):
            if i == j:
                continue
            sim_value = float(sim_matrix[i, j])
            kw_i = meta_list[i].get("keywords_found", 0)
            kw_j = meta_list[j].get("keywords_found", 0)
            if sim_value > 0.92:
                edge_reason = "Shared semantic embedding similarity"
            elif kw_i > 0 and kw_j > 0:
                edge_reason = "Shared keyword distribution pattern"
            else:
                edge_reason = "Profile behavioral anomaly matching"

            similar_videos.append({
                "video_id":        valid_records[j]["video_id"],
                "author_username": valid_records[j]["author_username"],
                "video_url":       valid_records[j]["video_url"],
                "similarity":      sim_value,
                "reason":          edge_reason,
            })

            if i < j and sim_value > SIMILARITY_THRESHOLD:
                graph_edges.append({
                    "source": valid_records[i]["video_id"],
                    "target": valid_records[j]["video_id"],
                    "weight": round(sim_value, 4),
                    "reason": edge_reason,
                })
                all_similarities.append(sim_value)
                dsu.union(valid_records[i]["video_id"], valid_records[j]["video_id"])

        similar_videos.sort(key=lambda x: x["similarity"], reverse=True)
        top_similar = similar_videos[:3]

        risk = prioritizer.evaluate(valid_records[i], top_similar)
        stats[risk["severity"]] += 1

        # ── Вывод вектора и скора в реальном времени ────────────────
        vec = vectors_list[i]
        vec_preview = ", ".join(f"{x:.3f}" for x in vec[:8])
        if len(vec) > 8:
            vec_preview += ", ..."
        print(
            f"[{i + 1}/{total_nodes}] video_id={valid_records[i]['video_id']} "
            f"score={risk['priority_score']:5.1f} ({risk['severity']:<8}) "
            f"vector[{len(vec)}]=[{vec_preview}]"
        )

        output_videos.append({
            "video_id":       valid_records[i]["video_id"],
            "author":         valid_records[i]["author_username"],
            "url":            valid_records[i]["video_url"],
            "caption":        valid_records[i]["caption"],
            "priority_score": risk["priority_score"],
            "severity":       risk["severity"],
            "risk_level":     risk["risk_level"],
            "taxonomy":       risk["taxonomy"],
            "explanation":    risk["explanation"],
            "feature_contribution":    risk["feature_contribution"],
            "detected_keywords_count": len(risk["detected_keywords_list"]),
            "detected_keywords_list":  risk["detected_keywords_list"],
            "metrics": {
                "followers": valid_records[i]["followers_count"],
                "views":     valid_records[i]["views_count"],
                "likes":     valid_records[i]["likes_count"],
            },
            "similar_videos": [
                {
                    "video_id":           v["video_id"],
                    "video_url":          v["video_url"],
                    "similarity_percent": round(v["similarity"] * 100, 1),
                    "reason":             v["reason"],
                }
                for v in top_similar
            ],
        })

    total_clusters, largest_cluster_size = dsu.get_metrics()
    avg_sim  = float(np.mean(all_similarities)) if all_similarities else 0.0
    max_edges_possible = total_nodes * (total_nodes - 1) / 2
    density  = len(graph_edges) / max_edges_possible if max_edges_possible > 0 else 0.0
    scores_list = [v["priority_score"] for v in output_videos]

    analytics_results = {
        "summary": {
            "total_processed":       total_nodes,
            "clean_count":           stats["CLEAN"],
            "warning_count":         stats["WARNING"],
            "critical_count":        stats["CRITICAL"],
            "fraud_density_percent": round(
                (stats["WARNING"] + stats["CRITICAL"]) / total_nodes * 100, 1
            ) if total_nodes else 0.0,
            "avg_priority_score":    round(float(np.mean(scores_list)),  1) if scores_list else 0.0,
            "max_priority_score":    round(float(np.max(scores_list)),   1) if scores_list else 0.0,
            "avg_similarity":        round(avg_sim, 3),
            "total_clusters":        total_clusters,
            "keywords_database_size": len(kl.keywords),
        },
        "graph": {
            "total_edges":          len(graph_edges),
            "largest_cluster_size": largest_cluster_size,
            "average_similarity":   round(avg_sim, 3),
            "density":              round(density, 4),
            "edges":                graph_edges,
        },
        "videos": sorted(output_videos, key=lambda x: x["priority_score"], reverse=True),
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(analytics_results, f, ensure_ascii=False, indent=4)

    # ── Записываем match_percentage и status обратно в БД ────────────────────
    try:
        import sqlite3 as _sqlite3
        _conn = _sqlite3.connect(str(db_path))
        _cur  = _conn.cursor()
        for v in output_videos:
            _cur.execute(
                "UPDATE media_analytics SET match_percentage=?, status=? "
                "WHERE video_id=?",
                (v["priority_score"], v["severity"].lower(), v["video_id"]),
            )
        _conn.commit()
        _conn.close()
        print(f"💾 match_percentage и status обновлены в БД ({len(output_videos)} записей)")
    except Exception as _e:
        print(f"⚠️  Не удалось обновить БД после аналитики: {_e}")

    print("-" * 80)
    print(f"✅  АНАЛИТИКА ЗАВЕРШЕНА")
    print(f"💾  JSON → {output_path.resolve()}")
    print(f"📊  Кластеров: {total_clusters}  |  Крупнейший: {largest_cluster_size} видео")
    print(f"🔴  CRITICAL : {stats['CRITICAL']}")
    print(f"🟡  WARNING  : {stats['WARNING']}")
    print(f"🟢  CLEAN    : {stats['CLEAN']}")
    print(f"📖  Стоп-слов в базе: {len(kl.keywords)}")
    print("=" * 80)

    # ── Итоговый отчёт: все вектора со скором + объяснение "за что", сортировка по риску ──
    print()
    print("=" * 80)
    print("📋  ИТОГОВЫЙ ОТЧЁТ — ВЕКТОРА, СКОРЫ И ПРИЧИНЫ ПО ВСЕМ ЗАПИСЯМ")
    print("=" * 80)
    report_by_video = {v["video_id"]: v for v in output_videos}
    rows = []
    for idx, record in enumerate(valid_records):
        vid = record["video_id"]
        v = report_by_video.get(vid)
        rows.append((idx, vid, v, vectors_list[idx]))
    rows.sort(key=lambda r: (r[2]["priority_score"] if r[2] else 0), reverse=True)

    for idx, vid, v, vec in rows:
        if v is None:
            continue
        vec_preview = ", ".join(f"{x:.3f}" for x in vec[:8])
        if len(vec) > 8:
            vec_preview += ", ..."
        print("-" * 80)
        print(
            f"video_id={vid}  author={v['author']}  "
            f"score={v['priority_score']:5.1f} ({v['severity']:<8})  "
            f"risk_level={v['risk_level']}  taxonomy={v['taxonomy']}"
        )
        print(f"  url: {v['url']}")
        print(f"  caption: {v['caption'][:120]}")
        print(f"  vector[{len(vec)}] = [{vec_preview}]")
        print(f"  feature_contribution: {v['feature_contribution']}")
        print(f"  metrics: followers={v['metrics']['followers']}, "
              f"views={v['metrics']['views']}, likes={v['metrics']['likes']}")
        print(f"  detected_keywords_count: {v['detected_keywords_count']}")
        print("  почему такой скор:")
        for reason in v["explanation"]:
            print(f"    {reason}")
        if v["similar_videos"]:
            print("  похожие видео (граф связей):")
            for sim in v["similar_videos"]:
                print(
                    f"    ↳ {sim['video_id']}  сходство={sim['similarity_percent']}%  "
                    f"причина: {sim['reason']}  url={sim['video_url']}"
                )
    print("=" * 80)
    print(
        f"СВОДКА: всего={total_nodes}  CLEAN={stats['CLEAN']}  "
        f"WARNING={stats['WARNING']}  CRITICAL={stats['CRITICAL']}  "
        f"кластеров={total_clusters}  крупнейший_кластер={largest_cluster_size}  "
        f"средняя_похожесть={round(avg_sim, 3)}  плотность_графа={round(density, 4)}"
    )
    print("=" * 80)


# ══════════════════════════════════════════════════════════════════════════════
#  ЕДИНАЯ ТОЧКА ВХОДА
# ══════════════════════════════════════════════════════════════════════════════

def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PLAKSIK AI — парсер и антифрод-аналитика рекламы нелегальных казино (Instagram / TikTok)."
    )
    parser.add_argument(
        "-t", "--tags",
        nargs="+",
        default=None,
        help="Теги или ключевые слова для парсинга (через пробел или запятую), например: --tags casino 1xbet '#слоты'",
    )
    parser.add_argument(
        "--tags-file",
        type=Path,
        default=None,
        help="Путь к файлу со списком тегов (по умолчанию используется keywords.txt).",
    )
    parser.add_argument(
        "--platform",
        choices=["all", "instagram", "tiktok"],
        default="all",
        help="Платформа для парсинга (по умолчанию: all).",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=None,
        help="Максимум видео на один тег (переопределяет MAX_VIDEOS_PER_KEYWORD).",
    )
    parser.add_argument(
        "--only-analytics",
        action="store_true",
        help="Пропустить парсинг и запустить только аналитику по существующей БД.",
    )
    parser.add_argument(
        "--skip-analytics",
        action="store_true",
        help="Выполнить только парсинг без запуска финальной аналитики.",
    )
    return parser.parse_args()


async def main() -> None:
    setup_logging()
    args = parse_cli_args()
    logger.info("=== ЗАПУСК АНТИФРОД СИСТЕМЫ ===")

    # ── ЭТАП 1: Парсинг ──────────────────────────────────────────────────────
    if not args.only_analytics:
        total_saved = await run_parsing_pipeline(
            tags=args.tags,
            tags_file=args.tags_file,
            platform=args.platform,
            max_videos=args.max_videos,
        )
        if total_saved == 0:
            logger.warning("Парсинг не собрал данных. Аналитика будет запущена по существующей БД (если есть).")
    else:
        logger.info("Режим --only-analytics: этап парсинга пропущен.")

    if args.skip_analytics:
        logger.info("Режим --skip-analytics: этап аналитики пропущен.")
        return

    # ── ЭТАП 2: Аналитика ────────────────────────────────────────────────────
    from config import DB_PATH
    db_path = Path(DB_PATH)

    if not db_path.exists():
        logger.error(f"БД не найдена по пути: {db_path}. Аналитика пропущена.")
        return

    logger.info("=== ЭТАП 2: ЗАПУСК АНАЛИТИКИ ===")
    run_analytics_pipeline(db_path=db_path, output_path=ANALYTICS_OUT)

    logger.success(f"=== ВСЁ ГОТОВО. Отчёт: {ANALYTICS_OUT} ===")


if __name__ == "__main__":
    asyncio.run(main())