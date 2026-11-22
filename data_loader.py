import json
import re
import ast
import sqlite3
import pandas as pd
from pathlib import Path
from typing import List, Dict, Any

class DataLoader:
    
    @staticmethod
    def _clean_float(value: Any, default: float = 0.0) -> float:
        """Очищает дробные числовые поля (например, video_duration)."""
        if pd.isna(value) or value is None:
            return default
        if isinstance(value, (int, float)):
            return float(value)
        val_str = str(value).strip()
        try:
            return float(val_str)
        except Exception:
            return default

    @staticmethod
    def _clean_numeric(value: Any, default: int = 0) -> int:
        """Очищает числовые поля от мусора (например, '15k' -> 15000 или '1 500' -> 1500)"""
        if pd.isna(value) or value is None:
            return default
        if isinstance(value, (int, float)):
            return int(value)
        
        # Если пришла строка (например, из CSV или текстового поля БД)
        val_str = str(value).strip().lower()
        try:
            if 'k' in val_str:
                return int(float(val_str.replace('k', '').strip()) * 1000)
            if 'm' in val_str:
                return int(float(val_str.replace('m', '').strip()) * 1000000)
            # Удаляем все, кроме цифр
            val_cleaned = re.sub(r'[^\d]', '', val_str)
            return int(val_cleaned) if val_cleaned else default 
        except Exception:
            return default

    @staticmethod
    def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """Стандартизирует структуру данных под требования VectorBuilder с учетом возможных имен полей в реальной БД"""
        return {
            'video_id': str(record.get('video_id') or record.get('id') or 'unknown_id'),
            'video_url': str(record.get('video_url', '')),
            'platform': str(record.get('platform', 'unknown_platform')).lower(),
            'author_username': str(record.get('author_username') or record.get('username') or ''),
            'author_id': str(record.get('author_id', '')),
            
            # Текстовые поля для анализа
            'caption': str(record.get('caption') or record.get('text') or record.get('description') or ''),
            'bio': str(record.get('bio') or record.get('author_bio') or ''),
            'hashtags': record.get('hashtags', []) if isinstance(record.get('hashtags'), list) else [],
            'audio_text': str(record.get('audio_text', '')),
            'ocr_text': str(record.get('ocr_text', '')),
            
            # Метаданные (с очисткой)
            'followers_count': DataLoader._clean_numeric(record.get('followers_count') or record.get('followers')),
            'following_count': DataLoader._clean_numeric(record.get('following_count') or record.get('following')),
            'total_posts': DataLoader._clean_numeric(record.get('total_posts') or record.get('posts_count')),
            'account_age_days': DataLoader._clean_numeric(record.get('account_age_days'), default=999),
            'external_url': str(record.get('external_url', '')),
            'is_verified': bool(record.get('is_verified', False)),
            'likes_count': DataLoader._clean_numeric(record.get('likes_count') or record.get('likes')),
            'views_count': DataLoader._clean_numeric(record.get('views_count') or record.get('views') or record.get('play_count'), default=0),
            'comments_count': DataLoader._clean_numeric(record.get('comments_count') or record.get('comments')),
            'video_duration': DataLoader._clean_float(record.get('video_duration')),
        }

    @classmethod
    def load(cls, filepath: str | Path) -> List[Dict[str, Any]]:
        """
        Загружает, очищает и валидирует данные из JSON, CSV или SQLite (.db).
        Возвращает стандартизированный список словарей.
        """
        filepath = Path(filepath)
        
        if not filepath.exists():
            raise FileNotFoundError(f"❌ Файл не найден по пути: {filepath.resolve()}")
        
        raw_records = []
        
        # --- Разбор SQLite ---
        if filepath.suffix.lower() in ['.db', '.sqlite', '.sqlite3']:
            try:
                conn = sqlite3.connect(str(filepath))
                cursor = conn.cursor()
                
                # Ищем рабочую таблицу
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
                tables = [r[0] for r in cursor.fetchall() if r[0] != 'sqlite_sequence']
                if not tables:
                    raise ValueError("Файл базы данных SQLite пуст (нет таблиц)")
                
                target_table = tables[0]
                
                # Включаем чтение строк как словарей
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(f"SELECT * FROM {target_table};")
                rows = cursor.fetchall()
                conn.close()
                
                raw_records = [dict(row) for row in rows]
                print(f"📡 [DataLoader] Успешно распознан SQLite ({target_table}). Загружено строк: {len(raw_records)}")
            except Exception as e:
                raise ValueError(f"❌ Ошибка парсинга структуры SQLite DB: {e}")
        
        # --- Загрузка JSON ---
        elif filepath.suffix.lower() == '.json':
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                raw_records = data if isinstance(data, list) else [data]
            except json.JSONDecodeError as e:
                raise ValueError(f"❌ Ошибка чтения JSON (битый файл): {e}")
                
        # --- Загрузка CSV ---
        elif filepath.suffix.lower() == '.csv':
            try:
                df = pd.read_csv(filepath)
                if 'hashtags' in df.columns:
                    df['hashtags'] = df['hashtags'].apply(
                        lambda x: ast.literal_eval(x) if isinstance(x, str) and x.startswith('[') else (str(x).split(',') if pd.notna(x) else [])
                    )
                raw_records = df.to_dict('records')
            except Exception as e:
                raise ValueError(f"❌ Ошибка чтения CSV: {e}")
        
        else:
            raise ValueError(f"❌ Неподдерживаемый формат файла: {filepath.suffix}")
        
        # --- Валидация и нормализация ---
        clean_records = []
        for idx, record in enumerate(raw_records):
            try:
                normalized = cls._normalize_record(record)
                clean_records.append(normalized)
            except Exception as e:
                print(f"⚠️ Предупреждение: Пропущена запись №{idx} из-за ошибки валидации: {e}")
        
        return clean_records