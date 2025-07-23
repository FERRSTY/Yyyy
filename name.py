import os
import sys
import logging
from datetime import datetime, timedelta
import asyncio
import time
from threading import Thread

from telegram import Update, ChatPermissions, ChatMember
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ApplicationBuilder
)
from flask import Flask

# Импорты для базы данных
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.sql import func
from sqlalchemy import exc # Для обработки ошибок SQLAlchemy

# --- Настройка логирования ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Настройки бота ---
# ВНИМАНИЕ: ТОКЕН БОТА НЕ ДОЛЖЕН БЫТЬ ЖЕСТКО ЗАШИТ В КОД НА ПРОДАКШЕНЕ!
# ЛУЧШЕ ИСПОЛЬЗОВАТЬ os.getenv("BOT_TOKEN") И УСТАНАВЛИВАТЬ ЕГО КАК ПЕРЕМЕННУЮ ОКРУЖЕНИЯ НА ХОСТИНГЕ.
# Для этого примера я вставлю его напрямую по вашей просьбе.
TOKEN = "8111985642:AAEDd1S54Kw-yGf2KaLAa0WS0-nADJlgk-M" # Ваш токен бота

# ВАШИ ID АДМИНИСТРАТОРОВ БОТА! ОБЯЗАТЕЛЬНО УКАЖИТЕ СВОЙ ID!
# Это пользователи, которые смогут использовать команды /addADM и /offADM, а также команды модерации
SUPER_ADMIN_IDS = [5235941151, 5356428433] # Ваши ID администраторов

# --- Настройки базы данных SQLite ---
DATABASE_URL = "sqlite:///bot_data.db" # Файл базы данных будет создан в той же директории
Base = declarative_base()
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)

# --- Модели базы данных ---
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String)
    first_name = Column(String)
    last_name = Column(String)

class Chat(Base):
    __tablename__ = 'chats'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    title = Column(String)

class BotAdmin(Base):
    __tablename__ = 'bot_admins'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False) # ID из таблицы users

class Warning(Base):
    __tablename__ = 'warnings'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    chat_id = Column(Integer, ForeignKey('chats.id'), nullable=False)
    admin_id = Column(Integer, ForeignKey('users.id'), nullable=False) # Кто выдал варн
    timestamp = Column(DateTime, default=func.now())
    reason = Column(Text)

class MutedUser(Base):
    __tablename__ = 'muted_users'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    chat_id = Column(Integer, ForeignKey('chats.id'), nullable=False)
    until_date = Column(DateTime, nullable=True) # До какого времени замучен
    timestamp = Column(DateTime, default=func.now())

class AntiFlood(Base):
    __tablename__ = 'antiflood'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    chat_id = Column(Integer, ForeignKey('chats.id'), nullable=False)
    last_message_time = Column(DateTime, default=func.now())
    message_count = Column(Integer, default=1)

# Создание таблиц в базе данных при запуске
try:
    Base.metadata.create_all(engine)
    logger.info("База данных инициализирована.")
except exc.OperationalError as e:
    logger.error(f"Ошибка при инициализации базы данных: {e}")
    # Это может произойти, если файл БД заблокирован или поврежден.
    # На хостингах обычно это не проблема, так как файл создается при деплое.

# --- Настройки антифлуда ---
FLOOD_INTERVAL_SECONDS = 3  # Количество секунд, за которое учитываются сообщения
FLOOD_MAX_MESSAGES = 5      # Максимальное количество сообщений за интервал до предупреждения

# --- Вспомогательные функции для работы с БД ---
def get_or_create_user(session, telegram_id, username, first_name, last_name):
    user = session.query(User).filter_by(telegram_id=telegram_id).first()
    if not user:
        user = User(telegram_id=telegram_id, username=username, first_name=first_name, last_name=last_name)
        session.add(user)
        session.commit()
    return user

def get_or_create_chat(session, telegram_id, title):
    chat = session.query(Chat).filter_by(telegram_id=telegram_id).first()
    if not chat:
        chat = Chat(telegram_id=telegram_id, title=title)
        session.add(chat)
        session.commit()
    return chat

async def get_target_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_user = None
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
    elif context.args:
        target_mention = context.args[0]
        try:
            # Пытаемся получить пользователя по ID
            user_id_from_arg = int(target_mention)
            member = await context.bot.get_chat_member(chat_id=update.effective_chat.id, user_id=user_id_from_arg)
            target_user = member.user
        except (ValueError, TypeError):
            # Если не ID, то пытаемся по username (может быть ненадежно, если пользователь не активен или не найден)
            if target_mention.startswith('@'):
                username = target_mention[1:]
                # Для надежного поиска по username в Telegram API требуется предварительная активность бота
                # с этим пользователем или специфические права.
                # Для упрощения, если это не ID, мы предупреждаем.
                await update.message.reply_text(f"Не удалось найти пользователя по упоминанию '{target_mention}'. Пожалуйста, используйте ID или ответьте на сообщение.")
                return None
            else:
                await update.message.reply_text(f"Неверный формат пользователя '{target_mention}'. Используйте @username или ID.")
                return None
        except Exception as e:
            await update.message.reply_text(f"Ошибка при поиске пользователя: {e}")
            return None
    return target_user

# --- Проверка прав администратора бота ---
async def is_bot_admin_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id in SUPER_ADMIN_IDS: # Супер-админы всегда админы бота
        return True
    
    session = Session()
    db_user = get_or_create_user(session, user_id, update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name)
    is_admin_in_db = session.query(BotAdmin).filter_by(user_id=db_user.id).first() is not None
    session.close()
    
    if not is_admin_in_db:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
    return is_admin_in_db

# --- Команды бота ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        f"Привет, {user.mention_html()}! Я бот-менеджер для вашей группы. "
        "Напишите /help, чтобы узнать, что я умею."
    )
    # Добавляем пользователя в БД при старте
    session = Session()
    get_or_create_user(session, user.id, user.username, user.first_name, user.last_name)
    session.close()

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Вот что я умею (нужны права админа бота):\n"
        "/ban [@username / ID / Ответом] - Забанить пользователя.\n"
        "/unban [@username / ID / Ответом] - Разбанить пользователя.\n"
        "/mute [@username / ID / Ответом] [время в минутах] - Замутить пользователя. Без времени - бессрочно.\n"
        "/unmute [@username / ID / Ответом] - Размутить пользователя.\n"
        "/warn [@username / ID / Ответом] [причина] - Выдать предупреждение. 3 варна = бан.\n"
        "/unwarn [@username / ID / Ответом] - Снять все предупреждения.\n"
        "\nКоманды только для супер-админов бота:\n"
        "/addADM [@username / ID / Ответом] - Добавить пользователя в админы бота.\n"
        "/offADM [@username / ID / Ответом] - Удалить пользователя из админов бота."
    )

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_bot_admin_check(update, context): return
    
    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return

    # Нельзя забанить самого себя или админа бота
    if target_user.id == update.effective_user.id:
        await update.message.reply_text("Вы не можете забанить самого себя.")
        return
    
    session = Session()
    db_target_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    is_target_admin = session.query(BotAdmin).filter_by(user_id=db_target_user.id).first() is not None or target_user.id in SUPER_ADMIN_IDS
    session.close()
    if is_target_admin:
        await update.message.reply_text("Нельзя забанить администратора бота.")
        return

    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user.id)
        await update.message.reply_html(f"Пользователь {target_user.mention_html()} был забанен.")
        logger.info(f"User {target_user.id} banned by {update.effective_user.id} in chat {chat_id}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось забанить пользователя: {e}")
        logger.error(f"Failed to ban user {target_user.id}: {e}")

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_bot_admin_check(update, context): return
    
    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return

    chat_id = update.effective_chat.id
    try:
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_user.id)
        await update.message.reply_html(f"Пользователь {target_user.mention_html()} был разбанен.")
        logger.info(f"User {target_user.id} unbanned by {update.effective_user.id} in chat {chat_id}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось разбанить пользователя: {e}")
        logger.error(f"Failed to unban user {target_user.id}: {e}")

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_bot_admin_check(update, context): return
    
    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return

    # Нельзя замутить самого себя или админа бота
    if target_user.id == update.effective_user.id:
        await update.message.reply_text("Вы не можете замутить самого себя.")
        return
    session = Session()
    db_target_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    is_target_admin = session.query(BotAdmin).filter_by(user_id=db_target_user.id).first() is not None or target_user.id in SUPER_ADMIN_IDS
    session.close()
    if is_target_admin:
        await update.message.reply_text("Нельзя замутить администратора бота.")
        return

    chat_id = update.effective_chat.id
    mute_duration_minutes = 0
    
    # Определяем длительность мьюта из аргументов
    # Если команда ответом на сообщение, то args[0] это длительность
    # Если команда с ID/username, то args[1] это длительность
    duration_arg_index = 0
    if not update.message.reply_to_message and len(context.args) > 1: # Если есть ID/username и еще один аргумент
        duration_arg_index = 1 
    elif update.message.reply_to_message and len(context.args) > 0: # Если ответом на сообщение и есть аргумент
        duration_arg_index = 0
    else: # Нет аргументов для длительности
        pass

    if context.args and len(context.args) > duration_arg_index and context.args[duration_arg_index].isdigit():
        mute_duration_minutes = int(context.args[duration_arg_index])
    
    until_date = None
    if mute_duration_minutes > 0:
        until_date = datetime.now() + timedelta(minutes=mute_duration_minutes)

    session = Session()
    db_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    db_chat = get_or_create_chat(session, chat_id, update.effective_chat.title)

    try:
        permissions = ChatPermissions(can_send_messages=False)
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user.id,
            permissions=permissions,
            until_date=until_date
        )
        
        # Записываем в БД
        muted_entry = MutedUser(user_id=db_user.id, chat_id=db_chat.id, until_date=until_date)
        session.add(muted_entry)
        session.commit()

        if mute_duration_minutes > 0:
            await update.message.reply_html(f"Пользователь {target_user.mention_html()} замучен на {mute_duration_minutes} минут.")
        else:
            await update.message.reply_html(f"Пользователь {target_user.mention_html()} замучен бессрочно.")
        logger.info(f"User {target_user.id} muted by {update.effective_user.id} in chat {chat_id} for {mute_duration_minutes} min")
    except Exception as e:
        await update.message.reply_text(f"Не удалось замутить пользователя: {e}\nУбедитесь, что у бота есть права администратора для ограничения пользователей.")
        logger.error(f"Failed to mute user {target_user.id}: {e}")
    finally:
        session.close()

async def unmute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_bot_admin_check(update, context): return
    
    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return

    chat_id = update.effective_chat.id
    
    session = Session()
    db_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    db_chat = get_or_create_chat(session, chat_id, update.effective_chat.title)

    try:
        # Сброс ограничений до стандартных разрешений участника
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=False,
            can_invite_users=True,
            can_pin_messages=False,
            can_manage_topics=False
        )
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target_user.id,
            permissions=permissions,
            until_date=None # Снять ограничения бессрочно
        )
        
        # Удаляем из БД замученных
        session.query(MutedUser).filter_by(user_id=db_user.id, chat_id=db_chat.id).delete()
        session.commit()

        await update.message.reply_html(f"Пользователь {target_user.mention_html()} был размучен.")
        logger.info(f"User {target_user.id} unmuted by {update.effective_user.id} in chat {chat_id}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось размутить пользователя: {e}\nУбедитесь, что у бота есть права администратора для ограничения пользователей.")
        logger.error(f"Failed to unmute user {target_user.id}: {e}")
    finally:
        session.close()

async def warn_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_bot_admin_check(update, context): return

    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return

    # Нельзя выдать варн самому себе или админу бота
    if target_user.id == update.effective_user.id:
        await update.message.reply_text("Вы не можете выдать предупреждение самому себе.")
        return
    session = Session()
    db_target_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    is_target_admin = session.query(BotAdmin).filter_by(user_id=db_target_user.id).first() is not None or target_user.id in SUPER_ADMIN_IDS
    session.close()
    if is_target_admin:
        await update.message.reply_text("Нельзя выдать предупреждение администратору бота.")
        return

    chat_id = update.effective_chat.id
    
    # Определяем причину
    reason_parts = []
    if update.message.reply_to_message:
        reason_parts = context.args # Если ответом, то все args - это причина
    elif len(context.args) > 1: # Если не ответом, и есть ID/username + причина
        reason_parts = context.args[1:]
    
    reason = " ".join(reason_parts) if reason_parts else "Без причины"

    session = Session()
    db_target_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    db_chat = get_or_create_chat(session, chat_id, update.effective_chat.title)
    db_admin_user = get_or_create_user(session, update.effective_user.id, update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name)

    try:
        new_warn = Warning(user_id=db_target_user.id, chat_id=db_chat.id, admin_id=db_admin_user.id, reason=reason)
        session.add(new_warn)
        session.commit()

        current_warns = session.query(Warning).filter_by(user_id=db_target_user.id, chat_id=db_chat.id).count()

        await update.message.reply_html(f"Пользователю {target_user.mention_html()} выдано предупреждение. ({current_warns}/3)")
        logger.info(f"User {target_user.id} warned by {update.effective_user.id} in chat {chat_id}. Warns: {current_warns}")

        if current_warns >= 3:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=target_user.id)
            session.query(Warning).filter_by(user_id=db_target_user.id, chat_id=db_chat.id).delete() # Удаляем варны после бана
            session.commit()
            await update.message.reply_html(f"Пользователь {target_user.mention_html()} получил 3 предупреждения и был забанен.")
            logger.info(f"User {target_user.id} banned due to 3 warns in chat {chat_id}")

    except Exception as e:
        await update.message.reply_text(f"Не удалось выдать предупреждение: {e}")
        logger.error(f"Failed to warn user {target_user.id}: {e}")
    finally:
        session.close()

async def unwarn_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_bot_admin_check(update, context): return

    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return

    chat_id = update.effective_chat.id
    session = Session()
    db_target_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    db_chat = get_or_create_chat(session, chat_id, update.effective_chat.title)

    try:
        deleted_warns = session.query(Warning).filter_by(user_id=db_target_user.id, chat_id=db_chat.id).delete()
        session.commit()
        await update.message.reply_html(f"Все предупреждения для пользователя {target_user.mention_html()} были сняты. ({deleted_warns} снято).")
        logger.info(f"All warns for user {target_user.id} removed by {update.effective_user.id} in chat {chat_id}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось снять предупреждения: {e}")
        logger.error(f"Failed to unwarn user {target_user.id}: {e}")
    finally:
        session.close()

async def add_bot_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in SUPER_ADMIN_IDS:
        await update.message.reply_text("У вас нет прав супер-админа для этой команды.")
        return
    
    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return
    
    session = Session()
    db_target_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    
    try:
        if session.query(BotAdmin).filter_by(user_id=db_target_user.id).first():
            await update.message.reply_html(f"Пользователь {target_user.mention_html()} уже является админом бота.")
        else:
            new_admin = BotAdmin(user_id=db_target_user.id)
            session.add(new_admin)
            session.commit()
            await update.message.reply_html(f"Пользователь {target_user.mention_html()} теперь является админом бота.")
            logger.info(f"User {target_user.id} added as bot admin by {update.effective_user.id}")
    except Exception as e:
        await update.message.reply_text(f"Не удалось добавить пользователя в админы: {e}")
        logger.error(f"Failed to add bot admin {target_user.id}: {e}")
    finally:
        session.close()

async def remove_bot_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in SUPER_ADMIN_IDS:
        await update.message.reply_text("У вас нет прав супер-админа для этой команды.")
        return
    
    target_user = await get_target_user(update, context)
    if not target_user: 
        await update.message.reply_text("Пожалуйста, ответьте на сообщение пользователя или укажите его ID/username.")
        return

    # Нельзя снять себя или супер-админа, если ты не супер-админ
    if target_user.id == update.effective_user.id:
        await update.message.reply_text("Вы не можете снять себя с прав админа бота этой командой.")
        return
    
    session = Session()
    db_target_user = get_or_create_user(session, target_user.id, target_user.username, target_user.first_name, target_user.last_name)
    is_target_super_admin = target_user.id in SUPER_ADMIN_IDS
    session.close()

    if is_target_super_admin:
        await update.message.reply_text("Вы не можете снять права супер-админа.")
        return

    session = Session() # Открываем новую сессию после проверки
    try:
        deleted = session.query(BotAdmin).filter_by(user_id=db_target_user.id).delete()
        session.commit()
        if deleted:
            await update.message.reply_html(f"Пользователь {target_user.mention_html()} больше не является админом бота.")
            logger.info(f"User {target_user.id} removed as bot admin by {update.effective_user.id}")
        else:
            await update.message.reply_html(f"Пользователь {target_user.mention_html()} не был админом бота.")
    except Exception as e:
        await update.message.reply_text(f"Не удалось удалить пользователя из админов: {e}")
        logger.error(f"Failed to remove bot admin {target_user.id}: {e}")
    finally:
        session.close()

# --- Антифлуд система ---
async def anti_flood_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    # Игнорируем админов бота
    session = Session()
    db_user_check = get_or_create_user(session, user_id, update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name)
    is_admin = session.query(BotAdmin).filter_by(user_id=db_user_check.id).first() is not None or user_id in SUPER_ADMIN_IDS
    session.close()
    if is_admin:
        return

    session = Session() # Открываем новую сессию для антифлуда
    db_user = get_or_create_user(session, user_id, update.effective_user.username, update.effective_user.first_name, update.effective_user.last_name)
    db_chat = get_or_create_chat(session, chat_id, update.effective_chat.title)

    try:
        anti_flood_entry = session.query(AntiFlood).filter_by(user_id=db_user.id, chat_id=db_chat.id).first()
        current_time = datetime.now()

        if anti_flood_entry:
            time_diff = (current_time - anti_flood_entry.last_message_time).total_seconds()

            if time_diff < FLOOD_INTERVAL_SECONDS:
                anti_flood_entry.message_count += 1
                if anti_flood_entry.message_count > FLOOD_MAX_MESSAGES:
                    # Пользователь флудит, мутим его
                    await update.message.reply_html(
                        f"Пользователь {update.effective_user.mention_html()} флудит и был временно замучен на 5 минут."
                    )
                    await context.bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=datetime.now() + timedelta(minutes=5)
                    )
                    # Сбрасываем счетчик после мута
                    anti_flood_entry.message_count = 1
                    anti_flood_entry.last_message_time = current_time
                    logger.info(f"User {user_id} muted for 5 min due to flood in chat {chat_id}")
            else:
                anti_flood_entry.message_count = 1
                anti_flood_entry.last_message_time = current_time
        else:
            new_anti_flood_entry = AntiFlood(user_id=db_user.id, chat_id=db_chat.id, last_message_time=current_time, message_count=1)
            session.add(new_anti_flood_entry)
        
        session.commit()

    except Exception as e:
        logger.error(f"Anti-flood check failed for user {user_id} in chat {chat_id}: {e}")
    finally:
        session.close()

# --- Веб-сервер Flask для поддержания активности на хостинге ---
app = Flask('')

@app.route('/')
def home():
    return "Бот работает!"

def run_flask_server():
    port = int(os.environ.get("PORT", 8080))
    # Привязываем Flask к 0.0.0.0, чтобы он был доступен извне
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask_server)
    t.start()

# --- Главная функция запуска бота ---
def main() -> None:
    # Запускаем Flask-сервер в отдельном потоке
    keep_alive()
    
    application = Application.builder().token(TOKEN).build()

    # Обработчики команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ban", ban_user))
    application.add_handler(CommandHandler("unban", unban_user))
    application.add_handler(CommandHandler("mute", mute_user))
    application.add_handler(CommandHandler("unmute", unmute_user))
    application.add_handler(CommandHandler("warn", warn_user))
    application.add_handler(CommandHandler("unwarn", unwarn_user))
    application.add_handler(CommandHandler("addADM", add_bot_admin))
    application.add_handler(CommandHandler("offADM", remove_bot_admin))

    # Обработчик для всех текстовых сообщений (для антифлуда)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, anti_flood_check))

    logger.info("Бот запущен и готов к работе!")
    application.run_polling()

if __name__ == "__main__":
    main()
