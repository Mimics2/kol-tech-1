import os
import asyncio
import logging
from datetime import datetime, time
from typing import Optional, Dict, List
import sys

import pytz
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, ContentType, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Настройка логирования для Railway
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Создаем роутер
router = Router()

# Состояния для FSM
class PostStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_time = State()
    waiting_for_channel = State()

# Конфигурация из переменных окружения Railway
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '123456789').split(',')))
DEFAULT_TIMEZONE = os.getenv('TIMEZONE', 'Europe/Moscow')
DATABASE_URL = os.getenv('DATABASE_URL')

# Глобальные переменные
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None
scheduler: Optional[AsyncIOScheduler] = None
pool: Optional[asyncpg.Pool] = None

# ========== DATABASE FUNCTIONS (для PostgreSQL на Railway) ==========

async def create_db_pool():
    """Создаем пул подключений к PostgreSQL на Railway"""
    if not DATABASE_URL:
        logger.error("DATABASE_URL не установлен!")
        raise ValueError("DATABASE_URL не установлен в переменных окружения")
    
    # Парсим DATABASE_URL от Railway
    import urllib.parse
    parsed = urllib.parse.urlparse(DATABASE_URL)
    
    db_config = {
        'user': parsed.username,
        'password': parsed.password,
        'database': parsed.path[1:],
        'host': parsed.hostname,
        'port': parsed.port or 5432,
        'ssl': 'require'  # Railway требует SSL
    }
    
    logger.info(f"Подключаемся к БД: {db_config['host']}:{db_config['port']}")
    
    return await asyncpg.create_pool(
        **db_config,
        min_size=5,
        max_size=20,
        ssl='require'
    )

async def init_database():
    """Инициализируем таблицы в базе данных"""
    async with pool.acquire() as conn:
        # Таблица пользователей
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                tariff TEXT DEFAULT 'free',
                channels_limit INTEGER DEFAULT 1,
                posts_per_day INTEGER DEFAULT 3,
                is_admin BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # Таблица каналов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                channel_id BIGINT,
                channel_username TEXT,
                channel_title TEXT,
                added_at TIMESTAMP DEFAULT NOW(),
                is_active BOOLEAN DEFAULT TRUE,
                FOREIGN KEY (user_id) REFERENCES users(id),
                UNIQUE(channel_id)
            )
        ''')
        
        # Таблица запланированных постов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                channel_id BIGINT,
                message_text TEXT,
                media_path TEXT,
                media_type TEXT,
                scheduled_time TIMESTAMP,
                status TEXT DEFAULT 'scheduled',
                created_at TIMESTAMP DEFAULT NOW(),
                published_at TIMESTAMP,
                error_message TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        
        # Таблица тарифов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tariffs (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE,
                price_usd DECIMAL(10,2),
                channels_limit INTEGER,
                posts_per_day INTEGER,
                description TEXT
            )
        ''')
        
        # Наполняем тарифы если пусто
        tariffs_count = await conn.fetchval('SELECT COUNT(*) FROM tariffs')
        if tariffs_count == 0:
            await conn.execute('''
                INSERT INTO tariffs (name, price_usd, channels_limit, posts_per_day, description) 
                VALUES 
                ('free', 0, 1, 3, 'Бесплатный тариф'),
                ('standard', 5, 2, 6, 'Стандартный тариф: 2 канала, 6 постов в день'),
                ('vip', 8, 3, 12, 'VIP тариф: 3 канала, 12 постов в день')
            ''')
        
        logger.info("✅ Таблицы в БД инициализированы")

async def add_user(user_id: int, username: str, full_name: str):
    """Добавляем пользователя в БД"""
    async with pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (id, username, full_name, tariff, is_admin)
            VALUES ($1, $2, $3, 'free', $4)
            ON CONFLICT (id) DO UPDATE 
            SET username = EXCLUDED.username,
                full_name = EXCLUDED.full_name
        ''', user_id, username, full_name, user_id in ADMIN_IDS)

async def get_user_channels(user_id: int) -> List[Dict]:
    """Получаем активные каналы пользователя"""
    async with pool.acquire() as conn:
        rows = await conn.fetch('''
            SELECT channel_id, channel_username, channel_title 
            FROM channels 
            WHERE user_id = $1 AND is_active = TRUE
            ORDER BY added_at
        ''', user_id)
        return [dict(row) for row in rows]

async def get_user_info(user_id: int) -> Dict:
    """Получаем информацию о пользователе и его тарифе"""
    async with pool.acquire() as conn:
        row = await conn.fetchrow('''
            SELECT u.*, t.*
            FROM users u
            LEFT JOIN tariffs t ON u.tariff = t.name
            WHERE u.id = $1
        ''', user_id)
        return dict(row) if row else None

# ========== COMMAND HANDLERS ==========

@router.message(Command("start"))
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    await add_user(message.from_user.id, 
                  message.from_user.username or "", 
                  message.from_user.full_name or "")
    
    user_info = await get_user_info(message.from_user.id)
    
    if message.from_user.id in ADMIN_IDS:
        await message.answer(
            "👑 <b>Привет, админ!</b>\n\n"
            "📊 <b>Доступные команды:</b>\n"
            "/newpost - создать новый пост\n"
            "/mychannels - мои каналы\n"
            "/addchannel - добавить канал\n"
            "/schedule - запланированные посты\n"
            "/stats - статистика\n"
            "/users - список пользователей\n\n"
            f"💎 <b>Ваш тариф:</b> {user_info['tariff'].upper() if user_info else 'FREE'}"
        )
    else:
        tariff_info = f"""
💎 <b>Ваш тариф:</b> {user_info['tariff'].upper() if user_info else 'FREE'}
📢 <b>Каналов можно добавить:</b> {user_info['channels_limit'] if user_info else 1}
📝 <b>Постов в день:</b> {user_info['posts_per_day'] if user_info else 3}
        """ if user_info else ""
        
        await message.answer(
            f"🤖 <b>Привет, {message.from_user.first_name}!</b>\n"
            f"Я бот для автоматической публикации постов в Telegram каналы.\n\n"
            f"{tariff_info}\n"
            "<b>Доступные команды:</b>\n"
            "/newpost - создать и запланировать пост\n"
            "/mychannels - мои каналы\n"
            "/schedule - запланированные посты\n"
            "/tariffs - посмотреть тарифы\n"
            "/help - помощь\n\n"
            "<i>Используйте кнопки меню для удобства!</i>",
            parse_mode=ParseMode.HTML
        )

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Помощь по командам"""
    help_text = """
<b>📚 Помощь по командам:</b>

<b>Основные команды:</b>
/newpost - создать и запланировать новый пост
/mychannels - список ваших каналов
/schedule - ваши запланированные посты

<b>Управление каналами:</b>
/addchannel - добавить канал для публикации
/removechannel - удалить канал

<b>Тарифы и оплата:</b>
/tariffs - посмотреть доступные тарифы
/myplan - информация о вашем тарифе

<b>Для админов:</b>
/stats - статистика бота
/users - список пользователей
/broadcast - рассылка сообщений

<b>Как добавить канал:</b>
1. Добавьте бота в канал как администратора
2. Дайте права на публикацию сообщений
3. Используйте команду /addchannel
4. Отправьте ссылку на канал или перешлите пост из него
    """
    await message.answer(help_text, parse_mode=ParseMode.HTML)

@router.message(Command("tariffs"))
async def cmd_tariffs(message: Message):
    """Показывает доступные тарифы"""
    async with pool.acquire() as conn:
        tariffs = await conn.fetch('SELECT * FROM tariffs ORDER BY price_usd')
    
    tariffs_text = "<b>💎 Доступные тарифы:</b>\n\n"
    
    for tariff in tariffs:
        emoji = "🆓" if tariff['price_usd'] == 0 else "💎" if tariff['price_usd'] < 8 else "👑"
        tariffs_text += (
            f"{emoji} <b>{tariff['name'].upper()}</b>\n"
            f"💰 Цена: ${tariff['price_usd']}\n"
            f"📢 Каналов: {tariff['channels_limit']}\n"
            f"📝 Постов в день: {tariff['posts_per_day']}\n"
            f"📋 {tariff['description']}\n\n"
        )
    
    tariffs_text += (
        "<i>Для смены тарифа свяжитесь с администратором: @your_admin_username</i>\n"
        "Скоро появится автоматическая оплата через криптовалюты!"
    )
    
    await message.answer(tariffs_text, parse_mode=ParseMode.HTML)

@router.message(Command("newpost"))
async def cmd_newpost(message: Message, state: FSMContext):
    """Начинаем создание нового поста"""
    user_info = await get_user_info(message.from_user.id)
    if not user_info:
        await message.answer("❌ Ошибка! Сначала используйте /start")
        return
    
    # Проверяем лимиты
    async with pool.acquire() as conn:
        posts_today = await conn.fetchval('''
            SELECT COUNT(*) FROM scheduled_posts 
            WHERE user_id = $1 
            AND DATE(created_at) = CURRENT_DATE
            AND status IN ('scheduled', 'published')
        ''', message.from_user.id)
        
        if posts_today >= user_info['posts_per_day']:
            await message.answer(
                f"❌ <b>Лимит постов исчерпан!</b>\n"
                f"Ваш лимит: {user_info['posts_per_day']} в день\n"
                f"Использовано сегодня: {posts_today}\n\n"
                f"Хотите больше постов? Посмотрите тарифы /tariffs",
                parse_mode=ParseMode.HTML
            )
            return
    
    await message.answer(
        "📝 <b>Создание нового поста</b>\n\n"
        "Отправьте мне:\n"
        "• Текст поста\n"
        "• Фото/видео с подписью\n"
        "• Или несколько сообщений с контентом\n\n"
        "<i>Когда закончите, отправьте команду /done</i>\n"
        "<i>Для отмены отправьте /cancel</i>",
        parse_mode=ParseMode.HTML
    )
    await state.set_state(PostStates.waiting_for_content)
    await state.update_data(media_path=None, media_type=None, text="")

# ... (остальные обработчики из предыдущего кода остаются аналогичными, 
# но с улучшенным логированием для Railway)

# ========== WEB SERVER FOR RAILWAY ==========

from aiohttp import web

async def health_check(request):
    """Health check endpoint для Railway"""
    return web.json_response({
        "status": "ok",
        "service": "telegram-post-bot",
        "timestamp": datetime.now().isoformat()
    })

async def start_web_server():
    """Запускаем простой веб-сервер для Railway"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Railway предоставляет PORT переменную окружения
    port = int(os.getenv('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    logger.info(f"🌐 Веб-сервер запущен на порту {port}")
    return runner

# ========== MAIN FUNCTIONS ==========

async def on_startup():
    """Действия при запуске бота"""
    logger.info("🚀 Бот запускается...")
    
    # Проверяем обязательные переменные
    if not BOT_TOKEN or BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        logger.error("❌ BOT_TOKEN не установлен!")
        return
    
    # Инициализируем планировщик
    global scheduler
    scheduler = AsyncIOScheduler(timezone=DEFAULT_TIMEZONE)
    scheduler.start()
    
    # Инициализируем БД
    global pool
    try:
        pool = await create_db_pool()
        await init_database()
        logger.info("✅ База данных подключена")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        raise
    
    # Перепланируем существующие посты
    await reschedule_existing_posts()
    
    # Устанавливаем команды меню
    commands = [
        {"command": "start", "description": "Запустить бота"},
        {"command": "newpost", "description": "Создать пост"},
        {"command": "mychannels", "description": "Мои каналы"},
        {"command": "schedule", "description": "Запланированные посты"},
        {"command": "tariffs", "description": "Тарифы"},
        {"command": "help", "description": "Помощь"}
    ]
    
    await bot.set_my_commands(commands)
    logger.info("✅ Бот успешно запущен и готов к работе")

async def on_shutdown():
    """Действия при остановке бота"""
    logger.info("🛑 Бот останавливается...")
    if scheduler:
        scheduler.shutdown()
    if pool:
        await pool.close()

async def main():
    """Основная функция запуска бота"""
    global bot, dp
    
    # Проверяем наличие токена
    if not BOT_TOKEN or BOT_TOKEN == 'YOUR_BOT_TOKEN_HERE':
        logger.error("❌ Установите BOT_TOKEN в переменных окружения Railway!")
        logger.info("📝 Как получить токен: https://core.telegram.org/bots#how-do-i-create-a-bot")
        return
    
    # Инициализируем бота
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    # Регистрируем обработчики старта/остановки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Запускаем веб-серсер для Railway
    web_runner = await start_web_server()
    
    try:
        # Запускаем бота
        logger.info("🤖 Запускаем поллинг бота...")
        await dp.start_polling(bot)
    finally:
        # Очистка при завершении
        await web_runner.cleanup()

if __name__ == "__main__":
    # Для Railway важно обрабатывать KeyboardInterrupt
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {e}")
        sys.exit(1)
