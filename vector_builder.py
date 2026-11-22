import numpy as np
import pandas as pd
import re
import ast
from pathlib import Path
from typing import Dict, Any, List, Tuple
 
 
class VectorBuilder:
    """
    Конвейер построения эмбеддинга для антифрод-анализа.
 
    Признаки (12 штук):
      Текстовые (6): top1/top2 вес слова, кол-во слов, сумма весов,
                     плотность фрода, доля источников с совпадениями
      Профильные (3): log(followers), log(following), followers/following
      Видео (1):      длительность
      Вовлечённость (2): likes/view, comments/view
 
    ВАЖНО: build_vector читает ТЕ ЖЕ поля что и _build_full_text в main.py:
           caption + bio + hashtags + audio_text + ocr_text
    """
 
    # ------------------------------------------------------------------
    # FEATURE_SPECS — mean/std для Z-score нормализации.
    # После получения реальных данных пересчитать через _calibrate().
    # ------------------------------------------------------------------
    FEATURE_SPECS: Dict[str, Dict[str, float]] = {
        'top1_word_weight':          {'mean': 12.5,   'std': 25.0},
        'top2_word_weight':          {'mean': 4.2,    'std': 12.0},
        'total_keyword_count':       {'mean': 1.8,    'std': 3.5},
        'sum_weights':               {'mean': 18.5,   'std': 42.0},
        'keyword_density':           {'mean': 0.05,   'std': 0.12},
        'text_sources_ratio':        {'mean': 0.2,    'std': 0.3},
 
        'followers_count_log':       {'mean': 5.18,   'std': 2.91},
        'following_count_log':       {'mean': 4.37,   'std': 2.14},
        'followers_following_ratio': {'mean': 49.95,  'std': 161.88},
 
        'video_duration_sec':        {'mean': 12.86,  'std': 21.57},
 
        'likes_per_view':            {'mean': 0.18,   'std': 0.12},
        'comments_per_view':         {'mean': 0.02,   'std': 0.04},
    }
 
    FEATURE_ORDER: List[str] = list(FEATURE_SPECS.keys())
 
    # ------------------------------------------------------------------
 
    def __init__(self, keywords_path: str | Path = 'data/final_keywords.csv'):
        self.keywords_path = Path(keywords_path)
        self.word_scores: Dict[str, float] = {}   # word → score
        self._load_keywords()
 
    # ── Загрузка CSV ──────────────────────────────────────────────────
 
    def _load_keywords(self) -> None:
        """Загружает словарь фрод-маркеров из CSV. Тихо пропускает ошибки."""
        if not self.keywords_path.exists():
            return
        try:
            df = pd.read_csv(self.keywords_path)
            for _, row in df.iterrows():
                word = str(row.get('стоп_слово', '')).lower().strip()
                if word:
                    self.word_scores[word] = float(row.get('скор', 0.0))
        except Exception:
            pass
 
    # ── Сборка полного текста (единая логика с main.py) ───────────────
 
    @staticmethod
    def _normalize_obfuscation(text: str) -> str:
        """
        Нормализует обфускацию — идентично main.py._normalize_obfuscation.

        Тип 2а: за•работок, за*работок → заработок
        Тип 2б: к.а.з.и.н.о → казино  (латиница/URL не трогается)
        Тип 4:  заработ0к → заработок
        """
        import re as _re
        t = text.lower()
        t = _re.sub(r'(?<=[а-яёa-z])[•·*](?=[а-яёa-z])', '', t)
        t = _re.sub(r'(?<=[а-яё])[.](?=[а-яё])', '', t)
        for digit, letter in (('0','о'), ('3','з'), ('4','ч'), ('6','б'), ('9','д')):
            t = _re.sub(rf'(?<=[а-яёa-z]){digit}(?=[а-яёa-z])', letter, t)
        return t

    @staticmethod
    def _extract_full_text(data: Dict[str, Any]) -> Tuple[str, List[str]]:
        """
        Возвращает (full_text, source_texts) где:
          full_text    — все поля склеены для keyword-поиска
          source_texts — список непустых исходных полей (для подсчёта sources_count)
 
        Читает: caption, bio, audio_text, ocr_text, hashtags
        Хэштеги: разворачивает из списка, убирает решётки.
        """
        source_texts = []
 
        def _add(val: str) -> None:
            v = (val or '').strip().lower()
            if v:
                source_texts.append(v)
 
        _add(str(data.get('caption',    '') or ''))
        _add(str(data.get('bio',        '') or ''))
        _add(str(data.get('audio_text', '') or ''))
        _add(str(data.get('ocr_text',   '') or ''))
 
        # hashtags — список или строка
        raw_ht = data.get('hashtags', []) or []
        if isinstance(raw_ht, str):
            try:
                raw_ht = ast.literal_eval(raw_ht)
            except Exception:
                raw_ht = re.split(r'[,\s]+', raw_ht)
        if isinstance(raw_ht, list) and raw_ht:
            ht_text = ' '.join(str(h) for h in raw_ht).lower().replace('#', ' ').strip()
            if ht_text:
                source_texts.append(ht_text)
 
        full_text = ' '.join(source_texts)
        full_text = full_text.replace('#', ' ')
        full_text = re.sub(r'\s+', ' ', full_text).strip()

        # Нормализация обфускации (типы 2 и 4)
        full_text = VectorBuilder._normalize_obfuscation(full_text)

        return full_text, source_texts
 
    # ── Keyword matching (единая логика с main.py) ────────────────────
 
    def _match_keywords(self, full_text: str) -> List[float]:
        """
        Ищет все стоп-слова в тексте.
        Поддерживает n-граммы и слитные хэштеги (#ставкинаспорт → ставки на спорт).
        Возвращает список весов совпавших слов (с дублями — для density).
        """
        compressed = full_text.replace(' ', '')   # для слитных хэштегов
        found: List[float] = []
        for word, score in self.word_scores.items():
            if word in full_text or word.replace(' ', '') in compressed:
                found.append(score)
        return found
 
    # ── Основной метод ────────────────────────────────────────────────
 
    def build_vector(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Монолитный конвейер: Extraction → Normalization → L2 Embedding.
 
        Возвращает:
          {
            "vector": np.ndarray (L2-нормализованный, dim=12),
            "meta":   {"raw_stats": {...}, "keywords_found": int}
          }
        """
 
        # 1. ТЕКСТ — читаем ВСЕ поля (caption + bio + audio + ocr + hashtags)
        full_text, source_texts = self._extract_full_text(data)
 
        # Считаем активные источники (поля где нашлись совпадения)
        sources_count = len(source_texts)   # сколько полей вообще заполнено
        active_sources = 0
 
        # Проходим по каждому источнику отдельно чтобы честно считать active_sources
        found_weights_all: List[float] = []
        compressed_full = full_text.replace(' ', '')
 
        for src in source_texts:
            compressed_src = src.replace(' ', '')
            src_has_match = False
            for word, score in self.word_scores.items():
                if word in src or word.replace(' ', '') in compressed_src:
                    found_weights_all.append(score)
                    src_has_match = True
            if src_has_match:
                active_sources += 1
 
        # Убираем дубли весов (одно слово не должно считаться дважды если есть в двух полях)
        # Но для density дубли важны — поэтому считаем отдельно
        unique_found_weights = sorted(
            self._match_keywords(full_text),   # дедуплицировано по полному тексту
            reverse=True
        )
 
        total_words = len(re.findall(r'\w+', full_text)) if full_text else 1
 
        # 2. ПРОФИЛЬ
        followers = max(int(data.get('followers_count', 0) or 0), 0)
        following = max(int(data.get('following_count', 0) or 0), 0)
 
        # views: если 0 — не делим (нет данных), guard через отдельный флаг
        views    = int(data.get('views_count', 0) or 0)
        likes    = int(data.get('likes_count',  0) or 0)
        comments = int(data.get('comments_count', 0) or 0)
        duration = float(data.get('video_duration', 0) or 0)
 
        # 3. ИЗВЛЕЧЕНИЕ СЫРЫХ ПРИЗНАКОВ
        kw = unique_found_weights   # отсортированный список весов (дедупл.)
 
        raw_features: Dict[str, float] = {
            # Текстовые
            'top1_word_weight':    float(kw[0] if kw else 0.0),
            'top2_word_weight':    float(kw[1] if len(kw) > 1 else 0.0),
            'total_keyword_count': float(len(kw)),
            'sum_weights':         float(sum(kw)),
            'keyword_density':     float(len(kw) / (total_words + 1)),
            'text_sources_ratio':  float(active_sources / max(sources_count, 1)),
 
            # Профильные
            'followers_count_log':       float(np.log1p(followers)),
            'following_count_log':       float(np.log1p(following)),
            'followers_following_ratio': float(
                np.clip(followers / max(following, 1), 0.0, 1000.0)
            ),
 
            # Видео
            'video_duration_sec': duration,
 
            # Вовлечённость — если views=0 → признак = 0 (нет данных, не ломаем вектор)
            'likes_per_view':    float(np.clip(likes    / views, 0.0, 1.0)) if views > 0 else 0.0,
            'comments_per_view': float(np.clip(comments / views, 0.0, 0.5)) if views > 0 else 0.0,
        }
 
        # 4. Z-SCORE + CLIP [-3, 3]
        normalized: List[float] = []
        for key in self.FEATURE_ORDER:
            val  = raw_features[key]
            mean = self.FEATURE_SPECS[key]['mean']
            std  = self.FEATURE_SPECS[key]['std']
            z    = (val - mean) / (std + 1e-6)
            normalized.append(float(np.clip(z, -3.0, 3.0)))
 
        base_vector = np.array(normalized, dtype=np.float64)
 
        # 5. L2 НОРМАЛИЗАЦИЯ
        norm = np.linalg.norm(base_vector)
        retrieval_vector = base_vector / (norm + 1e-8)
 
        return {
            "vector": retrieval_vector,
            "meta": {
                "raw_stats": {
                    "followers": followers,
                    "views":     views,
                    "likes":     likes,
                },
                "keywords_found":  len(kw),
                "active_sources":  active_sources,
                "sources_count":   sources_count,
            },
        }
 
    # ── Утилита: пересчёт FEATURE_SPECS по реальным данным ───────────
 
    @classmethod
    def calibrate(cls, records: List[Dict[str, Any]],
                  keywords_path: str | Path = 'data/final_keywords.csv') -> Dict[str, Dict[str, float]]:
        """
        Вызови после получения реальных данных от парсера.
        Пересчитывает mean/std по всей базе и обновляет FEATURE_SPECS.
 
        Использование:
            new_specs = VectorBuilder.calibrate(all_records)
            # Вставь результат в FEATURE_SPECS
        """
        builder = cls(keywords_path)
        all_raw: List[Dict[str, float]] = []
 
        for rec in records:
            try:
                res = builder.build_vector(rec)
                # Нам нужны raw_features до нормализации — пересобираем
                full_text, source_texts = cls._extract_full_text(rec)
                kw = sorted(builder._match_keywords(full_text), reverse=True)
                total_words = len(re.findall(r'\w+', full_text)) if full_text else 1
                sources_count  = len(source_texts)
                active_sources = res["meta"]["active_sources"]
 
                followers = max(int(rec.get('followers_count', 0) or 0), 0)
                following = max(int(rec.get('following_count', 0) or 0), 0)
                views     = int(rec.get('views_count',    0) or 0)
                likes     = int(rec.get('likes_count',    0) or 0)
                comments  = int(rec.get('comments_count', 0) or 0)
 
                all_raw.append({
                    'top1_word_weight':          kw[0] if kw else 0.0,
                    'top2_word_weight':          kw[1] if len(kw) > 1 else 0.0,
                    'total_keyword_count':       float(len(kw)),
                    'sum_weights':               float(sum(kw)),
                    'keyword_density':           float(len(kw) / (total_words + 1)),
                    'text_sources_ratio':        float(active_sources / max(sources_count, 1)),
                    'followers_count_log':       float(np.log1p(followers)),
                    'following_count_log':       float(np.log1p(following)),
                    'followers_following_ratio': float(np.clip(followers / max(following, 1), 0.0, 1000.0)),
                    'video_duration_sec':        float(rec.get('video_duration', 0) or 0),
                    'likes_per_view':            float(likes    / views) if views > 0 else 0.0,
                    'comments_per_view':         float(comments / views) if views > 0 else 0.0,
                })
            except Exception:
                continue
 
        if not all_raw:
            print("⚠️  calibrate(): нет данных для пересчёта")
            return cls.FEATURE_SPECS
 
        df = pd.DataFrame(all_raw)
        new_specs: Dict[str, Dict[str, float]] = {}
        for col in cls.FEATURE_ORDER:
            new_specs[col] = {
                'mean': float(round(df[col].mean(), 4)),
                'std':  float(round(df[col].std()  + 1e-6, 4)),
            }
            print(f"  {col:35s}  mean={new_specs[col]['mean']:8.4f}  std={new_specs[col]['std']:8.4f}")
 
        cls.FEATURE_SPECS = new_specs
        print("✅ FEATURE_SPECS обновлены. Вставь их в код как новые константы.")
        return new_specs