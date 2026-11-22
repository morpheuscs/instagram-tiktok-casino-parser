import os
import cv2
import numpy as np
from loguru import logger

# Будем использовать более быстрые и точные аналоги
from faster_whisper import WhisperModel
import easyocr

class MlProcessor:
    def __init__(self):
        # Отложенная инициализация тяжелых моделей
        self._whisper_model = None
        self._ocr_reader = None

    @property
    def whisper_model(self):
        if self._whisper_model is None:
            logger.info("[ML] Локальная загрузка Faster-Whisper (Model: 'tiny', CPU)...")
            # compute_type="int8" позволяет модели работать быстро и жрать мало RAM на CPU
            self._whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        return self._whisper_model

    @property
    def ocr_reader(self):
        if self._ocr_reader is None:
            logger.info("[ML] Загрузка EasyOCR (RU/EN)...")
            # Загружаем распознавание для русского и английского. gpu=False если нет видюхи
            self._ocr_reader = easyocr.Reader(['ru', 'en'], gpu=False)
        return self._ocr_reader

    def extract_audio_text(self, video_path: str) -> str:
        """Извлечение аудиодорожки и ускоренное распознавание речи."""
        if not os.path.exists(video_path):
            logger.error(f"[ML] Аудио-декодер: файл не найден -> {video_path}")
            return ""
        try:
            # faster-whisper возвращает генератор сегментов
            segments, info = self.whisper_model.transcribe(video_path, beam_size=5)
            
            text_segments = [segment.text for segment in segments]
            return " ".join(text_segments).strip()
            
        except Exception as e:
            logger.debug(f"[ML] Faster-Whisper: нет аудио или пустая дорожка ({str(e)[:60]})")
            return ""

    def extract_ocr_text(self, video_path: str) -> str:
        """Покадровый захват 3 контрольных точек видео и распознавание текста через EasyOCR."""
        if not os.path.exists(video_path):
            logger.error(f"[ML] OCR-декодер: файл не найден -> {video_path}")
            return ""

        extracted_texts = set() # Используем set, чтобы сразу исключить дубликаты
        try:
            cap = cv2.VideoCapture(video_path)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames <= 0:
                cap.release()
                return ""

            # Расчет трех опорных кадров (25%, 50%, 75% хронометража)
            target_frames = [int(total_frames * 0.25), int(total_frames * 0.50), int(total_frames * 0.75)]

            for frame_idx in target_frames:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if not ret or frame is None:
                    continue

                # EasyOCR отлично работает с цветными изображениями (RGB)
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                
                # Распознаем текст на кадре
                results = self.ocr_reader.readtext(frame_rgb, detail=0) # detail=0 вернет только текст без координат
                
                for text in results:
                    clean_text = text.strip()
                    if len(clean_text) > 2: # Игнорируем случайный мусор из 1-2 символов
                        extracted_texts.add(clean_text)

            cap.release()
        except Exception as e:
            logger.error(f"[ML] Сбой видеозахвата/EasyOCR обработки: {str(e)}")

        return " | ".join(list(extracted_texts))