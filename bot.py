from __future__ import annotations
import asyncio, logging, os, sqlite3
from datetime import date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from dotenv import load_dotenv
from db import Challenge, Database, Group

load_dotenv()
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip(); DATABASE_PATH=os.getenv('DATABASE_PATH','sport_challenge.db')
TIMEZONE=ZoneInfo(os.getenv('TIMEZONE','Europe/Moscow')); YESTERDAY_EDIT_UNTIL=time.fromisoformat(os.getenv('YESTERDAY_EDIT_UNTIL','12:00'))
ADMIN_IDS={int(v) for v in os.getenv('ADMIN_IDS','').split(',') if v.strip()}
if not BOT_TOKEN: raise RuntimeError('В .env не задан BOT_TOKEN')
db=Database(DATABASE_PATH); router=Router()

class ResultForm(StatesGroup): pushups=State(); pullups=State(); squats=State(); confirm=State()
class ChallengeForm(StatesGroup): title=State(); start_date=State(); duration=State()

def now(): return datetime.now(TIMEZONE)
def private(m:Message): return m.chat.type==ChatType.PRIVATE

def group_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='🏆 Общий рейтинг'),KeyboardButton(text='📊 Статистика челленджа')]],resize_keyboard=True,is_persistent=True)
def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='🏠 Выбрать группу')],[KeyboardButton(text='📝 Внести результат')],[KeyboardButton(text='📊 Моя статистика'),KeyboardButton(text='🏆 Общий рейтинг')],[KeyboardButton(text='🔔 Напоминания'),KeyboardButton(text='ℹ️ Правила')],[KeyboardButton(text='🛠 Управление группой')]],resize_keyboard=True)
def group_choice(rows, prefix='group'):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=('✅ ' if r['has_active'] else '')+r['title'],callback_data=f'{prefix}:{r["id"]}')] for r in rows])
def result_date_keyboard():
    rows=[[InlineKeyboardButton(text='Сегодня',callback_data='date:today')]]
    if now().time()<YESTERDAY_EDIT_UNTIL: rows.append([InlineKeyboardButton(text='Вчера',callback_data='date:yesterday')])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Сохранить',callback_data='save'),InlineKeyboardButton(text='✏️ Исправить',callback_data='edit')],[InlineKeyboardButton(text='❌ Отмена',callback_data='cancel')]])

def ranking_text(ch:Challenge)->str:
    rows=db.get_ranking(ch.id)
    if not rows:return f'<b>🏆 {escape(ch.title)}</b>\n\nПока результатов нет.'
    medals=['🥇','🥈','🥉']; out=[f'<b>🏆 {escape(ch.title)}</b>','']
    for i,r in enumerate(rows,1):
        mark=medals[i-1] if i<=3 else f'{i}.'
        out.append(f'{mark} <b>{escape(r["full_name"])}</b> — {float(r["points"]):.2f} балла · {r["days"]} дн.')
    return '\n'.join(out)
def group_stats_text(ch:Challenge)->str:
    s=db.get_group_stats(ch.id,now().date().isoformat()); members=int(s['members'] or 0); active=int(s['active_today'] or 0)
    pct=round(active/members*100) if members else 0; rows=db.get_ranking(ch.id); leader=escape(rows[0]['full_name']) if rows else 'пока нет'
    return (f'<b>📊 {escape(ch.title)}</b>\n\n👥 Участников: <b>{members}</b>\n✅ Внесли сегодня: <b>{active}</b> ({pct}%)\n'
            f'📅 Записей: <b>{s["result_days"]}</b>\n⭐ Идеальных дней: <b>{s["perfect_days"]}</b>\n'
            f'💪 Отжимания: <b>{s["pushups"]}</b>\n🧗 Подтягивания: <b>{s["pullups"]}</b>\n🦵 Приседания: <b>{s["squats"]}</b>\n'
            f'🏅 Баллы: <b>{float(s["points"]):.2f}</b>\n👑 Лидер: <b>{leader}</b>')
def selected(user_id:int)->tuple[Group|None,Challenge|None]:
    g=db.get_selected_group(user_id); return (g,db.get_active_challenge(g.id) if g else None)

async def ensure_group(message:Message)->Group:
    title=message.chat.title or f'Группа {message.chat.id}'
    return db.upsert_group(message.chat.id,title)

@router.message(CommandStart())
async def start(message:Message,state:FSMContext,bot:Bot):
    await state.clear()
    if not private(message):
        group=await ensure_group(message)
        if message.from_user:
            db.upsert_user(message.from_user.id,message.from_user.full_name,message.from_user.username)
            member=await bot.get_chat_member(message.chat.id,message.from_user.id)
            role='admin' if member.status in {'creator','administrator'} or message.from_user.id in ADMIN_IDS else 'member'
            db.add_member(group.id,message.from_user.id,role)
        me=await bot.get_me()
        join=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='➕ Вступить в челлендж',url=f'https://t.me/{me.username}?start=join_{group.id}')]])
        await message.answer('В этой группе доступны только рейтинг и статистика.',reply_markup=group_keyboard())
        await message.answer('Участникам нужно один раз связать личный чат с этой группой.',reply_markup=join)
        return
    if not message.from_user:return
    db.upsert_user(message.from_user.id,message.from_user.full_name,message.from_user.username)
    arg=(message.text or '').split(maxsplit=1)
    if len(arg)>1 and arg[1].startswith('join_'):
        try: gid=int(arg[1][5:]); g=db.get_group(gid)
        except ValueError: g=None
        if g:
            db.add_member(g.id,message.from_user.id); db.set_selected_group(message.from_user.id,g.id)
            await message.answer(f'✅ Вы присоединились к группе «{escape(g.title)}».',reply_markup=main_keyboard()); return
    groups=db.get_user_groups(message.from_user.id)
    if not groups: await message.answer('Сначала нажмите «Вступить в челлендж» в нужной общей группе.',reply_markup=main_keyboard())
    else: await message.answer('Личный кабинет Sport Challenge.',reply_markup=main_keyboard())

@router.message(Command('id'))
async def ids(message:Message): await message.answer(f'ID чата: <code>{message.chat.id}</code>')

@router.message(F.text=='🏠 Выбрать группу')
async def choose_group(message:Message):
    if not private(message) or not message.from_user:return
    rows=db.get_user_groups(message.from_user.id)
    if not rows: await message.answer('Вы ещё не присоединились ни к одной группе.'); return
    await message.answer('Выберите группу:',reply_markup=group_choice(rows))

@router.callback_query(F.data.startswith('group:'))
async def set_group(callback:CallbackQuery):
    gid=int(callback.data.split(':')[1]); rows=db.get_user_groups(callback.from_user.id)
    if gid not in {r['id'] for r in rows}: await callback.answer('Нет доступа',show_alert=True); return
    db.set_selected_group(callback.from_user.id,gid); g=db.get_group(gid)
    await callback.answer('Группа выбрана');
    if callback.message: await callback.message.answer(f'Активная группа: <b>{escape(g.title)}</b>')

@router.message(F.text=='📝 Внести результат')
async def result_start(message:Message,state:FSMContext):
    if not private(message) or not message.from_user:return
    g,ch=selected(message.from_user.id)
    if not g: await message.answer('Сначала выберите группу.'); return
    if not ch: await message.answer(f'В группе «{escape(g.title)}» нет активного челленджа.'); return
    await state.clear(); await state.update_data(group_id=g.id,challenge_id=ch.id)
    await message.answer(f'Группа: <b>{escape(g.title)}</b>\nЗа какой день внести результат?',reply_markup=result_date_keyboard())

@router.callback_query(F.data.startswith('date:'))
async def result_date(callback:CallbackQuery,state:FSMContext):
    data=await state.get_data(); ch_id=data.get('challenge_id')
    if not ch_id: await callback.answer('Начните ввод заново',show_alert=True); return
    target=now().date()-(timedelta(days=1) if callback.data.endswith('yesterday') else timedelta())
    await state.update_data(result_date=target.isoformat()); await state.set_state(ResultForm.pushups)
    if callback.message: await callback.message.answer('Сколько отжиманий? 0–200')
    await callback.answer()

def number(m:Message,limit:int):
    t=(m.text or '').strip(); return int(t) if t.isdigit() and 0<=int(t)<=limit else None
@router.message(ResultForm.pushups)
async def push(m:Message,state:FSMContext):
    v=number(m,200)
    if v is None: await m.answer('Введите число от 0 до 200.'); return
    await state.update_data(pushups=v); await state.set_state(ResultForm.pullups); await m.answer('Сколько подтягиваний? 0–50')
@router.message(ResultForm.pullups)
async def pull(m:Message,state:FSMContext):
    v=number(m,50)
    if v is None: await m.answer('Введите число от 0 до 50.'); return
    await state.update_data(pullups=v); await state.set_state(ResultForm.squats); await m.answer('Сколько приседаний? 0–200')
@router.message(ResultForm.squats)
async def squat(m:Message,state:FSMContext):
    v=number(m,200)
    if v is None: await m.answer('Введите число от 0 до 200.'); return
    await state.update_data(squats=v); d=await state.get_data(); await state.set_state(ResultForm.confirm)
    pts=d['pushups']/200+d['pullups']/50+v/200
    await m.answer(f'Проверка:\n💪 {d["pushups"]}\n🧗 {d["pullups"]}\n🦵 {v}\nБаллы: <b>{pts:.2f}</b>',reply_markup=confirm_keyboard())
@router.callback_query(ResultForm.confirm,F.data=='save')
async def save(callback:CallbackQuery,state:FSMContext):
    d=await state.get_data(); db.save_result(d['challenge_id'],callback.from_user.id,d['result_date'],d['pushups'],d['pullups'],d['squats']); await state.clear()
    await callback.answer('Сохранено');
    if callback.message: await callback.message.answer('✅ Результат сохранён.',reply_markup=main_keyboard())
@router.callback_query(ResultForm.confirm,F.data=='edit')
async def edit(callback:CallbackQuery,state:FSMContext): await state.set_state(ResultForm.pushups); await callback.answer(); await callback.message.answer('Введите отжимания заново:')
@router.callback_query(ResultForm.confirm,F.data=='cancel')
async def cancel(callback:CallbackQuery,state:FSMContext): await state.clear(); await callback.answer(); await callback.message.answer('Отменено.',reply_markup=main_keyboard())

@router.message(F.text=='🏆 Общий рейтинг')
@router.message(Command('rating'))
async def rating(message:Message):
    if private(message):
        if not message.from_user:return
        g,ch=selected(message.from_user.id)
    else:
        g=await ensure_group(message); ch=db.get_active_challenge(g.id)
    await message.answer(ranking_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=main_keyboard() if private(message) else group_keyboard())

@router.message(F.text=='📊 Статистика челленджа')
@router.message(Command('stats'))
async def stats(message:Message):
    if private(message):
        if not message.from_user:return
        g,ch=selected(message.from_user.id)
    else:
        g=await ensure_group(message); ch=db.get_active_challenge(g.id)
    await message.answer(group_stats_text(ch) if ch else 'В этой группе нет активного челленджа.',reply_markup=main_keyboard() if private(message) else group_keyboard())

@router.message(F.text=='📊 Моя статистика')
async def my_stats(message:Message):
    if not private(message) or not message.from_user:return
    g,ch=selected(message.from_user.id)
    if not ch: await message.answer('В выбранной группе нет активного челленджа.'); return
    s=db.get_user_stats(ch.id,message.from_user.id); current,longest=db.calculate_streaks(ch.id,message.from_user.id,now().date().isoformat())
    await message.answer(f'<b>📊 {escape(g.title)}</b>\n\nБаллы: <b>{float(s["points"]):.2f}</b>\nДней: <b>{s["days"]}</b>\nСерия: <b>{current}</b>\nЛучшая серия: <b>{longest}</b>\n💪 {s["pushups"]} · 🧗 {s["pullups"]} · 🦵 {s["squats"]}')

@router.message(F.text=='🔔 Напоминания')
async def reminders(message:Message):
    if private(message) and message.from_user:
        enabled=db.toggle_reminders(message.from_user.id); await message.answer('🔔 Напоминания включены.' if enabled else '🔕 Напоминания выключены.')
@router.message(F.text=='ℹ️ Правила')
async def rules(message:Message):
    if private(message): await message.answer('До 200 отжиманий, 50 подтягиваний и 200 приседаний в день. Каждое упражнение даёт до 1 балла; максимум 3 балла в день.')

@router.message(F.text=='🛠 Управление группой')
async def manage(message:Message):
    if not private(message) or not message.from_user:return
    g=db.get_selected_group(message.from_user.id)
    if not g: await message.answer('Сначала выберите группу.'); return
    if not (db.is_group_admin(g.id,message.from_user.id) or message.from_user.id in ADMIN_IDS): await message.answer('Управлять этой группой может только её администратор.'); return
    kb=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='➕ Новый челлендж'),KeyboardButton(text='🏁 Завершить челлендж')],[KeyboardButton(text='⬅️ Главное меню')]],resize_keyboard=True)
    await message.answer(f'Управление группой «{escape(g.title)}»',reply_markup=kb)
@router.message(F.text=='⬅️ Главное меню')
async def home(message:Message,state:FSMContext): await state.clear(); await message.answer('Главное меню.',reply_markup=main_keyboard())
@router.message(F.text=='➕ Новый челлендж')
async def new_challenge(message:Message,state:FSMContext):
    if not private(message) or not message.from_user:return
    g=db.get_selected_group(message.from_user.id)
    if not g or not (db.is_group_admin(g.id,message.from_user.id) or message.from_user.id in ADMIN_IDS): return
    if db.get_active_challenge(g.id): await message.answer('В этой группе уже есть активный челлендж.'); return
    await state.update_data(group_id=g.id); await state.set_state(ChallengeForm.title); await message.answer('Название челленджа:',reply_markup=ReplyKeyboardRemove())
@router.message(ChallengeForm.title)
async def new_title(message:Message,state:FSMContext): await state.update_data(title=(message.text or '').strip()); await state.set_state(ChallengeForm.start_date); await message.answer('Дата начала ГГГГ-ММ-ДД:')
@router.message(ChallengeForm.start_date)
async def new_date(message:Message,state:FSMContext):
    try: d=date.fromisoformat((message.text or '').strip())
    except ValueError: await message.answer('Неверная дата.'); return
    await state.update_data(start_date=d.isoformat()); await state.set_state(ChallengeForm.duration); await message.answer('Продолжительность в днях:')
@router.message(ChallengeForm.duration)
async def new_duration(message:Message,state:FSMContext):
    try: duration=int((message.text or '').strip()); assert 1<=duration<=365
    except: await message.answer('Введите число от 1 до 365.'); return
    d=await state.get_data(); start=date.fromisoformat(d['start_date']); end=start+timedelta(days=duration-1); g=db.get_group(d['group_id'])
    try: db.create_challenge(g.id,d['title'],start.isoformat(),end.isoformat(),g.telegram_chat_id)
    except sqlite3.IntegrityError: await message.answer('Активный челлендж уже существует.'); return
    await state.clear(); await message.answer(f'✅ Создан «{escape(d["title"])}»\n{start:%d.%m.%Y} — {end:%d.%m.%Y}',reply_markup=main_keyboard())
@router.message(F.text=='🏁 Завершить челлендж')
async def finish(message:Message,bot:Bot):
    if not private(message) or not message.from_user:return
    g,ch=selected(message.from_user.id)
    if not g or not ch:return
    if not (db.is_group_admin(g.id,message.from_user.id) or message.from_user.id in ADMIN_IDS): return
    db.finish_challenge(ch.id); text=f'🏁 Челлендж «{escape(ch.title)}» завершён.\n\n'+ranking_text(ch)
    await message.answer(text,reply_markup=main_keyboard()); await bot.send_message(g.telegram_chat_id,text)

@router.message(F.chat.type.in_({ChatType.GROUP,ChatType.SUPERGROUP}))
async def fallback(message:Message,bot:Bot):
    text=(message.text or '').strip(); public={'🏆 Общий рейтинг','📊 Статистика челленджа','/start','/rating','/stats','/id'}
    command=text.split('@',1)[0] if text.startswith('/') else text
    if command in public:return
    if text.startswith('/') or text in {'📝 Внести результат','📊 Моя статистика','🔔 Напоминания','ℹ️ Правила','🛠 Управление группой'}:
        me=await bot.get_me(); await message.answer('Эта функция доступна в личном чате.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Открыть личный чат',url=f'https://t.me/{me.username}')]]))

async def main():
    logging.basicConfig(level=logging.INFO); db.init(); bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher(storage=MemoryStorage()); dp.include_router(router)
    await bot.set_my_commands([BotCommand(command='start',description='Открыть меню'),BotCommand(command='rating',description='Рейтинг группы'),BotCommand(command='stats',description='Статистика группы'),BotCommand(command='id',description='ID чата')])
    await dp.start_polling(bot)
if __name__=='__main__': asyncio.run(main())
