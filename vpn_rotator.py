import asyncio
import os
import sys
from pathlib import Path

from config import BASE_DIR, ENABLE_VPN_ROTATION, VPN_CONFIG_FILE, VPN_CREDS_FILE

# Настройка логирования (если используешь стандартный logging, замени на logging.getLogger)
try:
    from loguru import logger
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("VPN_Rotator")


def _resolve_project_path(raw_path: str) -> str:
    if not raw_path:
        return ""
    p = Path(raw_path)
    return str(p if p.is_absolute() else BASE_DIR / p)


class VPNRotator:
    def __init__(self):
        """
        Инициализация модуля ротации IP-адресов через системный OpenVPN.
        Настраивается через переменные окружения ENABLE_VPN_ROTATION, VPN_CONFIG_FILE, VPN_CREDS_FILE.
        """
        self._lock = asyncio.Lock()
        self.enabled = ENABLE_VPN_ROTATION
        self.config_file = _resolve_project_path(VPN_CONFIG_FILE)
        self.creds_file = _resolve_project_path(VPN_CREDS_FILE)

    async def rotate_ip(self) -> bool:
        """
        Безопасная смена IP-адреса. Гарантирует, что если несколько параллельных
        корутин одновременно словят бан, сеть переключится строго один раз.
        """
        if not self.enabled:
            logger.info("[VPN] Ротация VPN отключена (ENABLE_VPN_ROTATION=0).")
            return False

        async with self._lock:
            logger.warning("[VPN] Обнаружен триггер блокировки (429/Капча). Запуск ротации IP...")

            # Проверяем, на месте ли конфигурационные файлы
            if not self.config_file or not os.path.exists(self.config_file):
                logger.critical(f"[VPN] Ошибка: Файл конфигурации '{self.config_file}' не найден!")
                return False
            if not self.creds_file or not os.path.exists(self.creds_file):
                logger.critical(f"[VPN] Ошибка: Файл с доступами '{self.creds_file}' не найден!")
                return False

            try:
                # 1. Жестко убиваем текущие сессии OpenVPN, если они зависли или активны
                logger.info("[VPN] Разрыв текущего VPN-соединения...")
                kill_proc = await asyncio.create_subprocess_exec(
                    "sudo", "killall", "openvpn",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await kill_proc.communicate()
                await asyncio.sleep(2)  # Пауза для корректного закрытия сетевых интерфейсов

                # 2. Формируем команду на запуск нового туннеля в фоне
                connect_command = [
                    "sudo", "openvpn",
                    "--config", self.config_file,
                    "--auth-user-pass", self.creds_file,
                    "--daemon"  # Ключевой флаг: отправляет OpenVPN в бэкграунд, чтобы скрипт не завис
                ]

                logger.info(f"[VPN] Подключение к новому серверу через {self.config_file}...")
                connect_proc = await asyncio.create_subprocess_exec(
                    *connect_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await connect_proc.communicate()

                if connect_proc.returncode == 0:
                    logger.success("[VPN] OpenVPN успешно демонизирован и запущен в фоне.")
                    logger.info("[VPN] Ожидание 6 секунд для стабилизации сетевого интерфейса и получения IP...")
                    await asyncio.sleep(6)  # Даем время на хэндшейк с сервером Протона
                    return True
                else:
                    error_msg = stderr.decode().strip() if stderr else "Неизвестная ошибка"
                    logger.error(f"[VPN] Ошибка при запуске OpenVPN: {error_msg}")
                    return False

            except Exception as e:
                logger.critical(f"[VPN] Критический сбой при выполнении ротации: {str(e)}")
                return False

    async def disconnect(self):
        """
        Метод для полной очистки соединений при завершении работы всего парсера.
        """
        if not self.enabled:
            return
        logger.info("[VPN] Отключение VPN и очистка сетевых интерфейсов перед выходом...")
        try:
            kill_proc = await asyncio.create_subprocess_exec(
                "sudo", "killall", "openvpn",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await kill_proc.communicate()
            logger.success("[VPN] Сеть возвращена в исходное состояние.")
        except Exception as e:
            logger.error(f"[VPN] Не удалось чисто закрыть OpenVPN: {str(e)}")


# Блок для быстрого ручного тестирования модуля ротации отдельно от основного парсера
if __name__ == "__main__":
    async def test():
        rotator = VPNRotator()
        print("--- СТАРТ ТЕСТА РОТАЦИИ ---")
        success = await rotator.rotate_ip()
        if success:
            print("--- ТЕСТ ПРОШЕЛ УСПЕШНО: IP изменен ---")
        else:
            print("--- ТЕСТ ПРОВАЛЕН: Проверь логи выше ---")
        await rotator.disconnect()

    asyncio.run(test())